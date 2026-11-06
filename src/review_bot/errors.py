"""Domain errors. The CLI maps them to exit codes and messages."""

from enum import StrEnum, auto

__all__ = [
    "ConfigError",
    "DiffEmptyError",
    "DiffNotFoundError",
    "DiffNotParsableError",
    "DiffTooLargeError",
    "DiffUnreadableError",
    "ErrorCategory",
    "InputError",
    "ProviderError",
    "ReviewBotError",
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
    """The diff path exists but can't be read as UTF-8 text."""


class DiffEmptyError(InputError):
    """The diff file is empty or contains only whitespace."""


class DiffTooLargeError(InputError):
    """The diff file is over the byte limit."""


class DiffNotParsableError(InputError):
    """The diff file's text has no file header, so it isn't a diff."""


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
    """A provider failed in a known way."""

    def __init__(self, category: ErrorCategory, message: str) -> None:
        """Record the failure's category alongside its message."""
        super().__init__(message)
        self.category = category
