"""Exception types. Every failure that should stop the run is a MirrorError."""


class MirrorError(Exception):
    """Base class for hard failures."""


class ConfigError(MirrorError):
    """Missing or invalid configuration."""


class SourceFeedError(MirrorError):
    """The source feed could not be fetched or parsed."""


class DownloadError(MirrorError):
    """An enclosure download failed or failed validation.

    Carries the episode title so the message names the episode, per spec.
    """

    def __init__(self, title: str, reason: str) -> None:
        super().__init__(f"{title!r}: {reason}")
        self.title = title
        self.reason = reason


class FeedBuildError(MirrorError):
    """The output feed could not be built."""


class ManifestError(MirrorError):
    """The stored manifest is missing or unreadable."""


class StorageError(MirrorError):
    """The storage backend rejected a request or returned an unexpected response.

    ``status`` is the HTTP status (0 for a connection failure, None when not
    HTTP-related). Server-side and connection failures are ``retryable``.
    """

    def __init__(self, message: str, status=None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status == 0 or (self.status is not None and self.status >= 500)


class VerificationError(MirrorError):
    """A post-publish verification assertion failed."""
