"""Configuration, assembled from environment variables and CLI overrides.

Credentials are read from the environment and never written to logs or to the
manifest. ``Config.__repr__`` is deliberately not customised because the
credential fields are excluded from the dataclass ``repr``.

The mirror is served from two hosts, and this module is the one place that
split is expressed:

- audio: GitHub Release assets, ``github.com/<repo>/releases/download/<tag>/``
- feed:  GitHub Pages, ``https://<owner>.github.io/<name>/``

Both base URLs are derived from ``GITHUB_REPOSITORY`` and can be overridden
individually (that is how the Cloudflare R2 fallback would be pointed at).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from .errors import ConfigError

DEFAULT_USER_AGENT = "podcast-mirror/1.0 (+https://roguepod.show)"

# Anything smaller than this is the placeholder-response failure mode, not audio.
MIN_AUDIO_BYTES = 1024 * 1024


@dataclass
class Config:
    """Everything the pipeline needs to run."""

    feed_url: str
    github_repo: str = ""                                   # owner/name
    github_token: str = field(default="", repr=False)
    release_tag: str = "audio"
    audio_base_url: str = ""                                # "" -> derived from repo
    feed_base_url: str = ""                                 # "" -> derived from repo

    # S3-compatible fallback (Cloudflare R2). Only S3Storage reads these; the
    # GitHub path never requires them.
    bucket: str = ""
    account_id: str = ""
    endpoint: str = ""
    region: str = "auto"
    access_key_id: str = field(default="", repr=False)
    secret_access_key: str = field(default="", repr=False)

    state_dir: str = "state"
    download_dir: str = "downloads"
    # Lives under state/ so Manifest.save's local write and the storage write
    # (a plain file in the checkout) are the same file, which git then tracks.
    manifest_key: str = "state/manifest.json"
    feed_key: str = "feed.xml"
    audio_prefix: str = "audio"

    delay: float = 2.0
    timeout: float = 120.0
    retries: int = 3
    min_bytes: int = MIN_AUDIO_BYTES
    max_new: int = 10  # 0 disables the cap
    user_agent: str = DEFAULT_USER_AGENT

    # ---- public URLs ---------------------------------------------------

    def __post_init__(self) -> None:
        if self.github_repo and "/" not in self.github_repo:
            raise ConfigError(
                f"GITHUB_REPOSITORY must be 'owner/name', got {self.github_repo!r}."
            )
        if not self.audio_base_url:
            self.audio_base_url = self._default_audio_base_url()
        if not self.feed_base_url:
            self.feed_base_url = self._default_feed_base_url()
        self.audio_base_url = self.audio_base_url.rstrip("/")
        self.feed_base_url = self.feed_base_url.rstrip("/")

    def _default_audio_base_url(self) -> str:
        if not self.github_repo:
            return ""
        return f"https://github.com/{self.github_repo}/releases/download/{self.release_tag}"

    def _default_feed_base_url(self) -> str:
        if not self.github_repo:
            return ""
        owner, name = self.github_repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}"

    def is_audio_key(self, key: str) -> bool:
        return key.lstrip("/").startswith(self.audio_prefix.strip("/") + "/")

    def public_url(self, key: str) -> str:
        """The URL a listener (or YouTube) fetches ``key`` from.

        Audio keys are release assets, which are a flat namespace, so only the
        basename goes on the URL. Everything else is served by Pages from the
        checkout, at its path.
        """
        key = key.lstrip("/")
        if self.is_audio_key(key):
            base, tail = self.audio_base_url, os.path.basename(key)
        else:
            base, tail = self.feed_base_url, key
        if not base:
            raise ConfigError(
                "No public base URL for %r. Set GITHUB_REPOSITORY, or set "
                "AUDIO_PUBLIC_BASE_URL and FEED_PUBLIC_BASE_URL explicitly." % key
            )
        return f"{base}/{tail}"

    def key_for_url(self, url: str) -> Optional[str]:
        """Inverse of ``public_url`` for audio; None if the URL is not ours."""
        prefix = self.audio_base_url + "/"
        if not self.audio_base_url or not url.startswith(prefix):
            return None
        name = url[len(prefix):]
        if not name or "/" in name:
            return None
        return f"{self.audio_prefix.strip('/')}/{name}"

    @property
    def feed_public_url(self) -> str:
        return self.public_url(self.feed_key)

    # ---- S3 fallback ---------------------------------------------------

    @property
    def endpoint_url(self) -> str:
        """The S3 API endpoint, for the R2 fallback only.

        Any S3-compatible provider works (Bunny, Backblaze, Wasabi, AWS, R2).
        An explicit endpoint wins; otherwise it is derived from the Cloudflare
        account id, which is the only provider whose endpoint is predictable
        from a credential.
        """
        if self.endpoint:
            return self.endpoint.rstrip("/")
        if self.account_id:
            return f"https://{self.account_id}.r2.cloudflarestorage.com"
        raise ConfigError(
            "No S3 endpoint configured. Set STORAGE_ENDPOINT_URL (e.g. "
            "https://la-s3.storage.bunnycdn.com for Bunny, or "
            "https://s3.us-west-2.amazonaws.com for AWS), or set R2_ACCOUNT_ID "
            "to use Cloudflare R2."
        )

    def require_credentials(self) -> None:
        """Validate the S3 credential set. Raises before any network call."""
        missing = [
            name
            for name, value in (
                ("STORAGE_BUCKET", self.bucket),
                ("STORAGE_ACCESS_KEY_ID", self.access_key_id),
                ("STORAGE_SECRET_ACCESS_KEY", self.secret_access_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". Export them (or set them as repo secrets) and re-run."
            )
        self.endpoint_url  # raises ConfigError if no endpoint is resolvable


def _env(*names: str) -> str:
    """First non-empty value among these environment variables."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def build_config(args) -> Config:
    """Merge environment variables with CLI overrides.

    CLI flags win over the environment so a one-off run can target a different
    repo without editing the shell profile. ``GITHUB_REPOSITORY`` and
    ``GITHUB_TOKEN`` are set automatically inside Actions; locally, export
    them (``GITHUB_TOKEN=$(gh auth token)``).
    """
    feed_url = getattr(args, "feed_url", None) or _env("PODCAST_FEED_URL")
    repo = getattr(args, "repo", None) or _env("GITHUB_REPOSITORY")

    missing = [
        name
        for name, value in (
            ("PODCAST_FEED_URL (or --feed-url)", feed_url),
            ("GITHUB_REPOSITORY (or --repo)", repo),
        )
        if not value
    ]
    if missing:
        raise ConfigError("Missing required setting(s): " + ", ".join(missing))

    cfg = Config(
        feed_url=feed_url,
        github_repo=repo,
        github_token=_env("GITHUB_TOKEN"),
        release_tag=getattr(args, "release_tag", None) or _env("RELEASE_TAG") or "audio",
        audio_base_url=getattr(args, "audio_base_url", None) or _env("AUDIO_PUBLIC_BASE_URL"),
        feed_base_url=getattr(args, "feed_base_url", None) or _env("FEED_PUBLIC_BASE_URL"),
        # R2 fallback; harmless when unset.
        bucket=_env("STORAGE_BUCKET", "R2_BUCKET"),
        account_id=_env("R2_ACCOUNT_ID"),
        endpoint=_env("STORAGE_ENDPOINT_URL"),
        region=_env("STORAGE_REGION") or "auto",
        access_key_id=_env("STORAGE_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID"),
        secret_access_key=_env("STORAGE_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY"),
    )
    for attr in ("state_dir", "download_dir", "delay", "max_new", "timeout"):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(cfg, attr, value)
    return cfg
