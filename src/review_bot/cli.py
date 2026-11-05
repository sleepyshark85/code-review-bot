"""Command line: arguments, the one event loop, printing, and errors to exit codes."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, NoReturn, TextIO, override

from review_bot.errors import (
    ConfigError,
    DiffEmptyError,
    DiffNotFoundError,
    DiffTooLargeError,
    DiffUnreadableError,
    ErrorCategory,
    InputError,
    ProviderError,
    ReviewBotError,
)
from review_bot.findings import Severity
from review_bot.prompt import PROMPT_VERSION
from review_bot.providers.base import LLMProvider, ReviewResponse
from review_bot.providers.claude_agent import ClaudeAgentProvider
from review_bot.providers.mock import MockProvider
from review_bot.render import at_or_above, escape_unprintable, render_findings
from review_bot.review import review_diff
from review_bot.settings import Settings, load_settings

__all__ = ["build_provider", "main", "parse_args"]

_INPUT_ERROR_EXIT_CODES: Final[Mapping[type[InputError], int]] = {
    ConfigError: 2,
    DiffNotFoundError: 3,
    DiffUnreadableError: 4,
    DiffEmptyError: 5,
    DiffTooLargeError: 6,
}

_PROVIDER_ERROR_EXIT_CODES: Final[Mapping[ErrorCategory, int]] = {
    ErrorCategory.UNAVAILABLE: 7,
    ErrorCategory.AUTH: 8,
    ErrorCategory.RATE_LIMIT: 9,
    ErrorCategory.REFUSAL: 10,
    ErrorCategory.TRANSIENT: 11,
    ErrorCategory.PERMANENT: 12,
    ErrorCategory.INVALID_OUTPUT: 13,
}

_INTERNAL_ERROR_EXIT_CODE: Final = 14

_INTERNAL_ERROR_MESSAGE: Final = (
    "The review did not complete, so there is no result. With --provider claude, check the "
    "Claude login and network. Check that stdout is writable, then try again."
)

_CANNED_WARNING: Final = "review-bot: warning: MockProvider output is canned, not a real review."


class _ReviewAbortedError(ReviewBotError):
    """The review raised a ``BaseException`` such as ``SystemExit``, so it has no result."""


class _ArgumentParser(argparse.ArgumentParser):
    """Raises instead of exiting, so usage errors reach ``main`` like any other input error."""

    @override
    def error(self, message: str) -> NoReturn:
        raise ConfigError(f"{message}. Run with --help for usage.")

    @override
    def print_help(self, file: object = None) -> None:
        """Write the help like any other output, so a failing stdout can't surface at shutdown.

        ``file`` is ignored: only argparse's ``--help`` action calls this, and it passes none.
        """
        _write_output(self.format_help().removesuffix("\n"))


def parse_args(argv: Sequence[str]) -> tuple[Path, Settings]:
    """Parse the command line into the diff path and the settings.

    Raises:
        ConfigError: If an argument, or a setting from the environment, is invalid.
    """
    parser = _ArgumentParser(
        prog="review-bot",
        description="Review a diff file.",
        epilog="Each option can also be set with REVIEW_BOT_<OPTION>, e.g. REVIEW_BOT_MODEL.",
        allow_abbrev=False,
    )
    parser.add_argument("diff_path", help="the diff file to review")
    parser.add_argument("--provider", help="the provider to use: mock, claude")
    parser.add_argument("--model", help="the model to request")
    parser.add_argument("--threshold", help=f"the lowest severity to report: {', '.join(Severity)}")
    parser.add_argument("--max-diff-bytes", help="reject a larger diff file")
    parser.add_argument("--max-retries", help="retries the Claude CLI makes for a failed API call")
    parser.add_argument("--api-timeout-ms", help="the Claude CLI's API timeout, in milliseconds")
    parser.add_argument("--deadline-s", help="the time limit for the whole call, in seconds")
    args = vars(parser.parse_args(argv))
    return Path(args.pop("diff_path")), load_settings(args)


def build_provider(settings: Settings) -> LLMProvider:
    """Create the selected provider.

    Raises:
        ConfigError: If no provider is selected: canned findings are never a silent fallback.
    """
    if settings.provider is None:
        raise ConfigError(
            "No provider selected. Pass --provider claude for a real review, "
            "or --provider mock for an offline run."
        )
    if settings.provider == "claude":
        return ClaudeAgentProvider(settings)
    return MockProvider()


def main(argv: Sequence[str] | None = None, *, provider: LLMProvider | None = None) -> int:
    """Run one review and return the process exit code.

    Args:
        argv: The arguments; None means the process's own.
        provider: The provider to use; None means ``build_provider`` picks it from the settings.

    Returns:
        The exit code from the table in ``docs/phases/phase-0/design.md``.
    """
    # With no handler, ``logging.lastResort`` prints the SDK's and asyncio's error records to
    # stderr, and those can hold raw CLI output.
    logging.getLogger().addHandler(logging.NullHandler())
    try:
        return _run(sys.argv[1:] if argv is None else argv, provider)
    except InputError as exc:
        _write_error(str(exc))
        return _INPUT_ERROR_EXIT_CODES.get(type(exc), _INTERNAL_ERROR_EXIT_CODE)
    except ProviderError as exc:
        _write_error(f"{exc.category}: {exc}")
        return _PROVIDER_ERROR_EXIT_CODES.get(exc.category, _INTERNAL_ERROR_EXIT_CODE)
    # An escaped exception would exit 1, "findings found". The exception's text can hold untrusted
    # CLI output, so it is never shown.
    except Exception:  # noqa: BLE001 - see above
        _write_error(_INTERNAL_ERROR_MESSAGE)
        return _INTERNAL_ERROR_EXIT_CODE


def _run(argv: Sequence[str], provider: LLMProvider | None) -> int:
    diff_path, settings = parse_args(argv)
    selected_provider = build_provider(settings) if provider is None else provider
    try:
        response = asyncio.run(review_diff(diff_path, settings, selected_provider))
    except Exception:
        raise
    except BaseException as exc:
        # Left alone, this skips ``main``'s handlers and can set the exit code itself, so
        # ``SystemExit(0)`` would read as a clean review. Only the review is wrapped because
        # ``--help`` exits through ``SystemExit`` too.
        raise _ReviewAbortedError from exc

    # A malformed response fails here, before anything is written. Stdout then goes first, so a
    # failure writing it leaves only the error line on stderr.
    findings = at_or_above(response.findings, settings.threshold)
    output = render_findings(findings, settings.threshold)
    summary = _summarise(response, settings)

    _write_output(output)
    if settings.provider == "mock":
        _write_diagnostic(_CANNED_WARNING)
    _write_diagnostic(summary)
    return 1 if findings else 0


def _summarise(response: ReviewResponse, settings: Settings) -> str:
    return (
        f"provider={settings.provider} model={settings.model} prompt={PROMPT_VERSION} "
        f"input_tokens={response.usage.input_tokens} output_tokens={response.usage.output_tokens}"
    )


def _write_output(text: str) -> None:
    """Write to stdout and flush, so a failure surfaces here and not at interpreter shutdown."""
    try:
        _write_line(sys.stdout, text)
        sys.stdout.flush()
    except Exception:
        # CPython flushes the standard streams again at shutdown, and a failure there overrides
        # the exit code with 120, so the failed stream has to go.
        sys.stdout = None
        raise


def _write_error(message: str) -> None:
    _write_diagnostic(f"review-bot: error: {message}")


def _write_diagnostic(text: str) -> None:
    """Write one line to stderr. A stderr that can't be written to never changes the exit code.

    The text is escaped here because any line can hold untrusted text: a path, a flag, or token
    counts from the CLI's JSON.
    """
    try:
        _write_line(sys.stderr, escape_unprintable(text))
        sys.stderr.flush()
    # Nothing may escape ``main``, and with stderr gone there is nowhere left to report this.
    except Exception:  # noqa: BLE001 - see above
        # See ``_write_output``: the shutdown flush covers stderr too.
        sys.stderr = None


def _write_line(stream: TextIO, text: str) -> None:
    # Encoding first means a stdout that can't show a character (Windows consoles, ASCII CI logs)
    # gets an escape instead of raising UnicodeEncodeError. ``encoding`` is None on a stream that
    # doesn't encode, such as ``io.StringIO``.
    encoding = stream.encoding or "utf-8"
    encoded = text.encode(encoding, "backslashreplace")
    stream.write(encoded.decode(encoding) + "\n")
