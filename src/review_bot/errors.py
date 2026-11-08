"""Domain errors. The CLI maps them to exit codes and messages."""

from collections.abc import Sequence
from enum import StrEnum, auto

from review_bot.secret_scan import SecretHit

__all__ = [
    "AuditError",
    "ConfigError",
    "DiffEmptyError",
    "DiffNotFoundError",
    "DiffNotParsableError",
    "DiffNotUtf8Error",
    "DiffTooLargeError",
    "DiffUnreadableError",
    "ErrorCategory",
    "EventError",
    "GitError",
    "GitHubError",
    "InputError",
    "NotARegularFileError",
    "ProviderError",
    "RepoConfigError",
    "ReviewBotError",
    "SecretDetectedError",
]


class ReviewBotError(Exception):
    """Base class for every error the review bot raises deliberately."""


class InputError(ReviewBotError):
    """The run can't start because of its arguments, settings or diff file."""


class ConfigError(InputError):
    """A command-line argument or setting is missing or invalid."""


class DiffNotFoundError(InputError):
    """The diff path doesn't exist."""


class DiffUnreadableError(InputError):
    """The diff path exists but the system can't read it."""


class DiffNotUtf8Error(DiffUnreadableError):
    """The diff file was read, but its bytes aren't valid UTF-8."""


class DiffEmptyError(InputError):
    """The diff file is empty or contains only whitespace."""


class DiffTooLargeError(InputError):
    """The diff file is over the byte limit."""


class DiffNotParsableError(InputError):
    """The diff file's text has no file header, so it isn't a diff."""


class SecretDetectedError(InputError):
    """The diff file's text holds a possible secret, so it must not be sent to the model.

    ``hits`` say where, never what, so they can be reported safely.
    """

    def __init__(self, message: str, *, hits: Sequence[SecretHit]) -> None:
        """Record where the possible secrets are alongside the message."""
        super().__init__(message)
        self.hits = tuple(hits)


class EventError(InputError):
    """An event payload or pulls file can't be read, or a field a run needs is invalid."""


class GitError(InputError):
    """A git command in a clone failed, timed out or couldn't start.

    ``exit_code`` is git's when it ran and failed, and None otherwise.
    """

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        """Record git's exit code, if any, alongside the message."""
        super().__init__(message)
        self.exit_code = exit_code


class NotARegularFileError(InputError):
    """A path in a commit is a directory, symbolic link or submodule, not a regular file.

    ``kind`` names what it is instead, such as ``directory``.
    """

    def __init__(self, message: str, *, kind: str) -> None:
        """Record what the path is alongside the message."""
        super().__init__(message)
        self.kind = kind


class RepoConfigError(ConfigError):
    """The repository's ``.review-bot.toml`` at the base commit is invalid; the message says how."""


class GitHubError(ReviewBotError):
    """A GitHub API call failed; the message names the call."""


class AuditError(ReviewBotError):
    """The audit log can't be written, so the run can't be traced (ADR 0005)."""


class ErrorCategory(StrEnum):
    """Why a provider failed. Each category has its own exit code."""

    AUTH = auto()
    RATE_LIMIT = auto()
    REFUSAL = auto()
    TRANSIENT = auto()
    PERMANENT = auto()
    UNAVAILABLE = auto()
    INVALID_OUTPUT = auto()


class ProviderError(ReviewBotError):
    """A provider failed in a known way.

    ``diagnostics`` are raw, untrusted text, such as the CLI's stderr: only ``scrub_diagnostics``
    may turn them into output (ADR 0004). The message is shown to the user, so they never go in
    it.
    """

    def __init__(
        self, category: ErrorCategory, message: str, *, diagnostics: Sequence[str] = ()
    ) -> None:
        """Record the failure's category and diagnostics alongside its message."""
        super().__init__(message)
        self.category = category
        self.diagnostics = tuple(diagnostics)
