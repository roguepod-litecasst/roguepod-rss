"""Object-storage backends.

The pipeline talks to this interface only, which keeps boto3 out of the core
logic and lets the test suite run the real pipeline against an in-memory bucket.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional, Protocol

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
        import boto3
        from botocore.config import Config as BotoConfig

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
