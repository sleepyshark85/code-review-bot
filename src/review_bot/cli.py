"""Command line: arguments, the one event loop, printing, and errors to exit codes."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, NoReturn, override

from review_bot.console import write_diagnostic, write_output
from review_bot.diff_parser import FileDiff
from review_bot.errors import (
    ConfigError,
    DiffEmptyError,
    DiffNotFoundError,
    DiffNotParsableError,
    DiffTooLargeError,
    DiffUnreadableError,
    ErrorCategory,
    InputError,
    ProviderError,
    ReviewBotError,
    SecretDetectedError,
)
from review_bot.findings import Severity
from review_bot.prompt import PROMPT_VERSION
from review_bot.providers.base import LLMProvider
from review_bot.providers.mock import MockProvider
from review_bot.render import at_or_above, render_findings
from review_bot.review import ReviewResult, review_diff
from review_bot.settings import Settings, load_settings

__all__ = ["build_provider", "main", "parse_args"]

_INPUT_ERROR_EXIT_CODES: Final[Mapping[type[InputError], int]] = {
    ConfigError: 2,
    DiffNotFoundError: 3,
    DiffUnreadableError: 4,
    DiffEmptyError: 5,
    DiffTooLargeError: 6,
    DiffNotParsableError: 15,
    SecretDetectedError: 16,
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
    "The review did not complete, so there is no result. Check the Claude Code login and "
    "network, that the dependencies are installed (uv sync), and that stdout is writable, then "
    "try again."
)

_CANNED_WARNING: Final = "review-bot: warning: MockProvider output is canned, not a real review."

_NOTHING_REVIEWED_WARNING: Final = (
    "review-bot: warning: nothing was reviewed: every changed file was skipped: "
)
_MAX_NAMED_SKIPPED_FILES: Final = 10


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
        write_output(self.format_help().removesuffix("\n"))


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
    parser.add_argument("--provider", help="the provider to use: claude (default), mock")
    parser.add_argument("--model", help="the model to request")
    parser.add_argument("--threshold", help=f"the lowest severity to report: {', '.join(Severity)}")
    parser.add_argument("--max-diff-bytes", help="reject a larger diff file")
    parser.add_argument("--max-retries", help="retries the Claude CLI makes for a failed API call")
    parser.add_argument("--api-timeout-ms", help="the Claude CLI's API timeout, in milliseconds")
    parser.add_argument("--deadline-s", help="the time limit for the whole call, in seconds")
    args = vars(parser.parse_args(argv))
    return Path(args.pop("diff_path")), load_settings(args)


def build_provider(settings: Settings) -> LLMProvider:
    """Create the selected provider. Nothing starts until its ``review`` is called."""
    if settings.provider == "claude":
        # Deferred so only a Claude run pays to load the SDK. If it can't be imported, the error
        # reaches ``main``'s catch-all, like any other failure.
        from review_bot.providers.claude_agent import ClaudeAgentProvider  # noqa: PLC0415 - see above

        return ClaudeAgentProvider(settings)
    return MockProvider()


def main(argv: Sequence[str] | None = None, *, provider: LLMProvider | None = None) -> int:
    """Run one review and return the process exit code.

    Args:
        argv: The arguments; None means the process's own.
        provider: The provider to use; None means ``build_provider`` picks it from the settings.

    Returns:
        The exit code from the table in ``docs/phases/phase-0/design.md``, with the codes
        ``docs/phases/phase-1/design.md`` appends.
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
    is_own_claude = provider is None and settings.provider == "claude"
    review = _review_then_discard_unraisable if is_own_claude else review_diff
    try:
        result = asyncio.run(review(diff_path, settings, selected_provider))
    except Exception:
        raise
    except BaseException as exc:
        # Left alone, this skips ``main``'s handlers and can set the exit code itself, so
        # ``SystemExit(0)`` would read as a clean review. Only the review is wrapped because
        # ``--help`` exits through ``SystemExit`` too.
        raise _ReviewAbortedError from exc

    # A malformed response fails here, before anything is written. Stdout then goes first, so a
    # failure writing it leaves only the error line on stderr.
    findings = at_or_above(result.findings, settings.threshold)
    output = render_findings(findings, settings.threshold)
    summary = _summarise(result, settings)

    write_output(output)
    if not result.model_called:
        write_diagnostic(_nothing_reviewed_warning(result.skipped_files))
    elif settings.provider == "mock":
        write_diagnostic(_CANNED_WARNING)
    write_diagnostic(summary)
    return 1 if findings else 0


async def _review_then_discard_unraisable(
    diff_path: Path, settings: Settings, provider: LLMProvider
) -> ReviewResult:
    """Run ``review_diff``, then discard ``sys.unraisablehook`` reports until the process exits.

    The SDK's ``close()`` waits for the Claude CLI to exit, not for its pipes to close, so a
    helper that holds them open leaves a transport that raises from ``__del__`` once
    ``asyncio.run`` has closed the loop. CPython would print that as ``Exception ignored in``,
    which breaks the stderr contract. The hook goes in only once the review has ended, so reports
    during the review still show, and it is never restored because the process is about to exit.
    """
    try:
        return await review_diff(diff_path, settings, provider)
    finally:
        sys.unraisablehook = _discard_unraisable


def _discard_unraisable(_report: "sys.UnraisableHookArgs") -> None:
    """Drop the report, since stderr may hold only the bot's own lines."""


def _nothing_reviewed_warning(skipped_files: Sequence[FileDiff]) -> str:
    # A deleted file has no new path.
    paths = [file.path or "(deleted file)" for file in skipped_files]
    named = ", ".join(paths[:_MAX_NAMED_SKIPPED_FILES])
    return f"{_NOTHING_REVIEWED_WARNING}{named} ({len(paths)} in total)"


def _summarise(result: ReviewResult, settings: Settings) -> str:
    # Phase 1 appends fields after Phase 0's, in the design's order (Compatibility).
    return (
        f"provider={settings.provider} model={settings.model} prompt={PROMPT_VERSION} "
        f"input_tokens={result.usage.input_tokens} output_tokens={result.usage.output_tokens} "
        f"skipped_files={len(result.skipped_files)} "
        f"model_called={str(result.model_called).lower()}"
    )


def _write_error(message: str) -> None:
    write_diagnostic(f"review-bot: error: {message}")
