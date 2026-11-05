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
    InputError,
)
from review_bot.findings import Severity
from review_bot.prompt import PROMPT_VERSION
from review_bot.providers.base import LLMProvider, ReviewResponse
from review_bot.providers.claude_agent import ClaudeAgentProvider
from review_bot.providers.mock import MockProvider
from review_bot.render import at_or_above, render_findings
from review_bot.review import review_diff
from review_bot.settings import Settings, load_settings

__all__ = ["build_provider", "main", "parse_args"]

_EXIT_CODES: Final[Mapping[type[InputError], int]] = {
    ConfigError: 2,
    DiffNotFoundError: 3,
    DiffUnreadableError: 4,
    DiffEmptyError: 5,
    DiffTooLargeError: 6,
}

_INTERNAL_ERROR_EXIT_CODE: Final = 14

_INTERNAL_ERROR_MESSAGE: Final = (
    "The review did not complete, so there is no result. With --provider claude, check the "
    "Claude login and network. Check that stdout is writable, then try again."
)

_CANNED_WARNING: Final = "review-bot: warning: MockProvider output is canned, not a real review."

# Raised for a stream that is failing (OSError), closed (ValueError) or absent (AttributeError).
_STREAM_FAILURES: Final = (OSError, ValueError, AttributeError)


class _ArgumentParser(argparse.ArgumentParser):
    """Raises instead of exiting, so usage errors reach ``main`` like any other input error."""

    @override
    def error(self, message: str) -> NoReturn:
        raise ConfigError(f"{message}. Run with --help for usage.")


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
        _write_diagnostic(f"review-bot: error: {exc}")
        return _EXIT_CODES.get(type(exc), _INTERNAL_ERROR_EXIT_CODE)
    # An escaped exception would exit 1, "findings found". Cancellation derives from BaseException,
    # so Exception alone wouldn't stop it. The exception's text can hold untrusted CLI output, so
    # it is never shown.
    except (Exception, asyncio.CancelledError):  # noqa: BLE001 - see above
        _write_diagnostic(f"review-bot: error: {_INTERNAL_ERROR_MESSAGE}")
        return _INTERNAL_ERROR_EXIT_CODE


def _run(argv: Sequence[str], provider: LLMProvider | None) -> int:
    diff_path, settings = parse_args(argv)
    selected_provider = build_provider(settings) if provider is None else provider
    response = asyncio.run(review_diff(diff_path, settings, selected_provider))

    if settings.provider == "mock":
        _write_diagnostic(_CANNED_WARNING)
    _write_diagnostic(_summarise(response, settings))

    findings = at_or_above(response.findings, settings.threshold)
    _write_output(render_findings(findings, settings.threshold))
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
    except _STREAM_FAILURES:
        # CPython flushes the standard streams again at shutdown, and a failure there overrides
        # the exit code with 120, so the failed stream has to go.
        sys.stdout = None
        raise


def _write_diagnostic(text: str) -> None:
    """Write to stderr. A stderr that can't be written to never changes the exit code."""
    try:
        _write_line(sys.stderr, text)
        sys.stderr.flush()
    except _STREAM_FAILURES:
        # See ``_write_output``: the shutdown flush covers stderr too.
        sys.stderr = None


def _write_line(stream: TextIO, text: str) -> None:
    # Encoding first means a stdout that can't show a character (Windows consoles, ASCII CI logs)
    # gets an escape instead of raising UnicodeEncodeError.
    encoded = text.encode(stream.encoding, "backslashreplace")
    stream.write(encoded.decode(stream.encoding) + "\n")
