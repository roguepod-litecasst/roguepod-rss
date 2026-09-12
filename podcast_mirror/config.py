"""Configuration, assembled from environment variables and CLI overrides.

Credentials are read from the environment and never written to logs or to the
manifest. ``Config.__repr__`` is deliberately not customised because the
credential fields are excluded from the dataclass ``repr``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .errors import ConfigError

DEFAULT_USER_AGENT = "podcast-mirror/1.0 (+https://roguepod.show)"

# Anything smaller than this is the placeholder-response failure mode, not audio.
MIN_AUDIO_BYTES = 1024 * 1024


@dataclass
class Config:
    """Everything the pipeline needs to run."""

    feed_url: str
    bucket: str
    base_url: str
    account_id: str = ""
    endpoint: str = ""
    region: str = "auto"
    access_key_id: str = field(default="", repr=False)
    secret_access_key: str = field(default="", repr=False)

    state_dir: str = "state"
    download_dir: str = "downloads"
    manifest_key: str = "manifest.json"
    feed_key: str = "feed.xml"
    audio_prefix: str = "audio"

    delay: float = 2.0
    timeout: float = 120.0
    retries: int = 3
    min_bytes: int = MIN_AUDIO_BYTES
    max_new: int = 10  # 0 disables the cap
    user_agent: str = DEFAULT_USER_AGENT

    @property
    def endpoint_url(self) -> str:
        """The S3 API endpoint.

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

    def public_url(self, key: str) -> str:
        return f"{self.base_url.rstrip('/')}/{key.lstrip('/')}"

    @property
    def feed_public_url(self) -> str:
        return self.public_url(self.feed_key)

    def require_credentials(self) -> None:
        """Validate the credential set. Raises before any network call."""
        missing = [
            name
            for name, value in (
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
    """First non-empty value among these environment variables.

    Provider-neutral STORAGE_* names are preferred; the original R2_* names are
    still honoured so an existing .env keeps working.
    """
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def build_config(args) -> Config:
    """Merge environment variables with CLI overrides.

    CLI flags win over the environment so a one-off run can target a different
    bucket without editing the shell profile.
    """
    feed_url = getattr(args, "feed_url", None) or _env("PODCAST_FEED_URL")
    bucket = getattr(args, "bucket", None) or _env("STORAGE_BUCKET", "R2_BUCKET")
    base_url = getattr(args, "base_url", None) or _env(
        "STORAGE_PUBLIC_BASE_URL", "R2_PUBLIC_BASE_URL"
    )

    missing = [
        name
        for name, value in (
            ("PODCAST_FEED_URL (or --feed-url)", feed_url),
            ("STORAGE_BUCKET (or --bucket)", bucket),
            ("STORAGE_PUBLIC_BASE_URL (or --base-url)", base_url),
        )
        if not value
    ]
    if missing:
        raise ConfigError("Missing required setting(s): " + ", ".join(missing))

    cfg = Config(
        feed_url=feed_url,
        bucket=bucket,
        base_url=base_url,
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
