"""Object-storage backends.

The pipeline talks to this interface only, which keeps boto3 out of the core
logic and lets the test suite run the real pipeline against an in-memory bucket.

``GitHubStorage`` is the production backend: audio goes to GitHub Release
assets, everything else to plain files in the repo working tree. ``S3Storage``
stays as the Cloudflare R2 fallback should GitHub's ``application/octet-stream``
download Content-Type turn out to matter to YouTube.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Protocol

from .errors import StorageError

log = logging.getLogger(__name__)


class Storage(Protocol):
    """Minimal object-store surface used by the pipeline."""

    def get_bytes(self, key: str) -> Optional[bytes]:
        """Return object bytes, or None if the key does not exist."""

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        ...

    def put_file(self, path: str, key: str, content_type: str) -> None:
        ...

    def head(self, key: str) -> Optional[dict]:
        """Return {'size': int, 'content_type': str} or None if absent."""


class S3Storage:
    """Any S3-compatible object store: Bunny, R2, Backblaze, Wasabi, AWS."""

    def __init__(self, cfg) -> None:
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise StorageError(
                "S3Storage needs boto3, which is an optional extra: "
                "pip install -r requirements-s3.txt"
            ) from exc

        cfg.require_credentials()
        self._bucket = cfg.bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint_url,
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=cfg.secret_access_key,
            # R2 ignores the region; other providers sign with it, so it is
            # configurable via STORAGE_REGION.
            region_name=cfg.region,
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    def _missing(self, exc) -> bool:
        from botocore.exceptions import ClientError

        if not isinstance(exc, ClientError):
            return False
        code = exc.response.get("Error", {}).get("Code", "")
        return code in ("404", "NoSuchKey", "NotFound")

    def get_bytes(self, key: str) -> Optional[bytes]:
        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except Exception as exc:  # noqa: BLE001 - narrowed by _missing
            if self._missing(exc):
                return None
            raise

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )

    def put_file(self, path: str, key: str, content_type: str) -> None:
        # boto3 switches to multipart automatically for large files.
        self._client.upload_file(
            path, self._bucket, key, ExtraArgs={"ContentType": content_type}
        )

    def head(self, key: str) -> Optional[dict]:
        try:
            resp = self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - narrowed by _missing
            if self._missing(exc):
                return None
            raise
        return {
            "size": int(resp["ContentLength"]),
            "content_type": resp.get("ContentType", ""),
        }


class GitHubStorage:
    """GitHub-hosted storage, routed by key prefix.

    - ``audio/...`` keys are assets on one GitHub Release (``release_tag``).
      Release assets are a flat namespace, so the asset name is the key's
      basename; ``audio_key()`` already makes that unique via the guid digest.
    - Every other key (``feed.xml``, ``state/manifest.json``) is a plain file
      under ``local_dir`` -- the repo checkout. The workflow commits them, and
      GitHub Pages serves them.

    Only the standard library is used; the GitHub REST API is small enough that
    a client library would be more code than it saves.
    """

    API_BASE = "https://api.github.com"
    UPLOAD_BASE = "https://uploads.github.com"
    API_VERSION = "2022-11-28"
    _PER_PAGE = 100          # GitHub's maximum page size for asset listings
    _UPLOAD_ATTEMPTS = 3     # asset uploads are the one call that flakes (502s)
    _RETRY_DELAY = 2.0       # seconds, multiplied by the attempt number

    def __init__(
        self,
        repo: str,
        token: str,
        *,
        release_tag: str = "audio",
        audio_prefix: str = "audio",
        local_dir: str = ".",
        api_base: str = API_BASE,
        upload_base: str = UPLOAD_BASE,
        user_agent: str = "podcast-mirror",
        timeout: float = 120.0,
    ) -> None:
        if not repo or "/" not in repo:
            raise StorageError(
                f"GitHub repository must be 'owner/name', got {repo!r} "
                "(set GITHUB_REPOSITORY)."
            )
        if not token:
            raise StorageError("GitHub token is empty (set GITHUB_TOKEN).")
        self._repo = repo
        self._token = token
        self._tag = release_tag
        self._audio_prefix = audio_prefix.strip("/") + "/"
        self._local_dir = os.path.abspath(local_dir)
        self._api = api_base.rstrip("/")
        self._upload = upload_base.rstrip("/")
        self._user_agent = user_agent
        self._timeout = timeout
        self._release: Optional[dict] = None   # cached release object

    @classmethod
    def from_config(cls, cfg) -> "GitHubStorage":
        return cls(
            cfg.github_repo,
            cfg.github_token,
            release_tag=cfg.release_tag,
            audio_prefix=cfg.audio_prefix,
            user_agent=cfg.user_agent,
            timeout=cfg.timeout,
        )

    # ---- routing -----------------------------------------------------

    def _is_audio(self, key: str) -> bool:
        return key.lstrip("/").startswith(self._audio_prefix)

    def _local_path(self, key: str) -> str:
        path = os.path.abspath(os.path.join(self._local_dir, key.lstrip("/")))
        if os.path.commonpath([path, self._local_dir]) != self._local_dir:
            raise StorageError(f"Refusing to write outside the repo: {key!r}")
        return path

    @staticmethod
    def _asset_name(key: str) -> str:
        return os.path.basename(key)

    # ---- Storage protocol --------------------------------------------

    def get_bytes(self, key: str) -> Optional[bytes]:
        if self._is_audio(key):
            asset = self._find_asset(self._asset_name(key))
            if asset is None:
                return None
            # The public download URL needs no auth and is what listeners hit;
            # the API URL redirects to a signed S3 URL that rejects our token.
            request = urllib.request.Request(
                asset["browser_download_url"], headers={"User-Agent": self._user_agent}
            )
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                return resp.read()
        try:
            with open(self._local_path(key), "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        if self._is_audio(key):
            fd, tmp = tempfile.mkstemp(suffix=".upload")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                self.put_file(tmp, key, content_type)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return

        path = self._local_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)  # atomic, so a crash never leaves a torn feed.xml
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        log.debug("wrote %s (%d bytes)", os.path.relpath(path, self._local_dir), len(data))

    def put_file(self, path: str, key: str, content_type: str) -> None:
        if not self._is_audio(key):
            with open(path, "rb") as fh:
                self.put_bytes(key, fh.read(), content_type)
            return

        name = self._asset_name(key)
        release = self._ensure_release()
        size = os.path.getsize(path)
        url = (
            f"{self._upload}/repos/{self._repo}/releases/{release['id']}/assets?"
            + urllib.parse.urlencode({"name": name})
        )
        last_error: Optional[Exception] = None
        for attempt in range(1, self._UPLOAD_ATTEMPTS + 1):
            # The API rejects duplicate names, and a failed upload can leave a
            # half-created asset behind, so always clear the name first.
            existing = self._find_asset(name)
            if existing is not None:
                log.info("  replacing existing release asset %s", name)
                self._api_call("DELETE", f"/repos/{self._repo}/releases/assets/{existing['id']}")
                self._release = None
            try:
                with open(path, "rb") as fh:
                    self._api_call(
                        "POST", url, body=fh,
                        headers={"Content-Type": content_type, "Content-Length": str(size)},
                    )
                self._release = None
                return
            except StorageError as exc:
                last_error = exc
                if attempt < self._UPLOAD_ATTEMPTS and exc.retryable:
                    log.warning("  upload of %s failed (%s); retrying", name, exc)
                    time.sleep(self._RETRY_DELAY * attempt)
                    continue
                raise
        raise StorageError(f"upload of {name} failed: {last_error}")  # pragma: no cover

    def head(self, key: str) -> Optional[dict]:
        if self._is_audio(key):
            asset = self._find_asset(self._asset_name(key))
            if asset is None:
                return None
            return {"size": int(asset["size"]), "content_type": asset.get("content_type", "")}
        try:
            size = os.path.getsize(self._local_path(key))
        except FileNotFoundError:
            return None
        guessed, _ = mimetypes.guess_type(key)
        return {"size": size, "content_type": guessed or "application/octet-stream"}

    # ---- GitHub API --------------------------------------------------

    def _api_call(self, method: str, path_or_url: str, *, body=None, headers=None) -> Optional[dict]:
        url = path_or_url if "://" in path_or_url else f"{self._api}{path_or_url}"
        request_headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.API_VERSION,
            "User-Agent": self._user_agent,
            **(headers or {}),
        }
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise StorageError(
                f"GitHub API {method} {url} -> HTTP {exc.code}: {detail}",
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise StorageError(f"GitHub API {method} {url} unreachable: {exc.reason}", status=0) from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise StorageError(f"GitHub API {method} {url} returned non-JSON") from exc

    def _get_release(self) -> Optional[dict]:
        """The release object for the tag, or None if it does not exist yet."""
        if self._release is None:
            try:
                self._release = self._api_call(
                    "GET", f"/repos/{self._repo}/releases/tags/{urllib.parse.quote(self._tag)}"
                )
            except StorageError as exc:
                if exc.status != 404:
                    raise
                return None
        return self._release

    def _ensure_release(self) -> dict:
        release = self._get_release()
        if release is None:
            log.info("Creating GitHub release %r on %s", self._tag, self._repo)
            self._release = self._api_call(
                "POST", f"/repos/{self._repo}/releases",
                body={
                    "tag_name": self._tag,
                    "name": "Mirrored audio",
                    "body": "Episode audio re-hosted for the YouTube RSS mirror. "
                            "Managed by mirror.py; do not edit by hand.",
                    "draft": False,
                    "prerelease": False,
                },
            )
            release = self._release
        return release

    def _list_assets(self) -> List[dict]:
        release = self._get_release()
        if release is None:
            return []
        # The release object embeds assets too, but the dedicated endpoint is
        # the one documented to paginate, and a full backfill is 54 files.
        assets: List[dict] = []
        page = 1
        while True:
            batch = self._api_call(
                "GET",
                f"/repos/{self._repo}/releases/{release['id']}/assets?"
                + urllib.parse.urlencode({"per_page": self._PER_PAGE, "page": page}),
            ) or []
            assets.extend(batch)
            if len(batch) < self._PER_PAGE:
                return assets
            page += 1

    def _find_asset(self, name: str) -> Optional[dict]:
        for asset in self._list_assets():
            if asset.get("name") == name:
                return asset
        return None


class MemoryStorage:
    """In-memory bucket used by the test suite.

    Records every write so tests can assert that a second run is a no-op.
    """

    def __init__(self) -> None:
        self.objects: Dict[str, tuple] = {}
        self.writes: list = []
        self._lock = threading.Lock()

    def get_bytes(self, key: str) -> Optional[bytes]:
        entry = self.objects.get(key)
        return entry[0] if entry else None

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        with self._lock:
            self.objects[key] = (data, content_type)
            self.writes.append(key)

    def put_file(self, path: str, key: str, content_type: str) -> None:
        with open(path, "rb") as fh:
            self.put_bytes(key, fh.read(), content_type)

    def head(self, key: str) -> Optional[dict]:
        entry = self.objects.get(key)
        if entry is None:
            return None
        return {"size": len(entry[0]), "content_type": entry[1]}
