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


class VerificationError(MirrorError):
    """A post-publish verification assertion failed."""
