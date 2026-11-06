"""Command line: arguments, the one event loop, printing, and errors to exit codes."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NoReturn, override

from review_bot.audit import AuditLog, Stage
from review_bot.console import write_diagnostic, write_output
from review_bot.diff_parser import FileDiff
from review_bot.errors import (
    AuditError,
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
from review_bot.review import PipelineResult, run_pipeline
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
_AUDIT_ERROR_EXIT_CODE: Final = 17

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

    def __init__(self, cause: BaseException) -> None:
        super().__init__()
        # Its class name only, which the audit log records: its text can hold CLI output.
        self.cause_name = type(cause).__name__


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
    parser.add_argument(
        "--audit-log",
        help="the JSONL file each run appends its records to (default: .review-bot/audit.jsonl)",
    )
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
        # A usage error or ``--help`` records nothing, because the log's path isn't known yet.
        diff_path, settings = parse_args(sys.argv[1:] if argv is None else argv)
        # Opened before the diff is read, so an untraceable run never reaches a provider.
        audit = AuditLog(settings.audit_log, clock=_utc_now)
    except Exception as exc:  # noqa: BLE001 - `_report_failure` maps any failure to its code
        return _report_failure(exc)
    try:
        return _review(diff_path, settings, provider, audit)
    except Exception as exc:  # noqa: BLE001 - `_report_failure` maps any failure to its code
        exit_code = _report_failure(exc)
        _record_failed_end(audit, exit_code, exc)
        return exit_code


def _report_failure(exc: Exception) -> int:
    """Write the one error line for ``exc`` and return its exit code."""
    if isinstance(exc, InputError):
        _write_error(str(exc))
        return _INPUT_ERROR_EXIT_CODES.get(type(exc), _INTERNAL_ERROR_EXIT_CODE)
    if isinstance(exc, ProviderError):
        _write_error(f"{exc.category}: {exc}")
        return _PROVIDER_ERROR_EXIT_CODES.get(exc.category, _INTERNAL_ERROR_EXIT_CODE)
    if isinstance(exc, AuditError):
        _write_error(str(exc))
        return _AUDIT_ERROR_EXIT_CODE
    # An escaped exception would exit 1, "findings found". The exception's text can hold untrusted
    # CLI output, so it is never shown.
    _write_error(_INTERNAL_ERROR_MESSAGE)
    return _INTERNAL_ERROR_EXIT_CODE


def _record_failed_end(audit: AuditLog, exit_code: int, exc: Exception) -> None:
    """Record the failed run's end. If that fails too, the run's own exit code stands."""
    # A category or class name only: an exception's text can hold diff or CLI output.
    if isinstance(exc, ProviderError):
        error: str = exc.category
    elif isinstance(exc, _ReviewAbortedError):
        error = exc.cause_name
    else:
        error = type(exc).__name__
    try:
        audit.record(Stage.RUN, "end", {"exit_code": exit_code, "error": error})
    except AuditError as audit_exc:
        # Already reported when the log itself is what failed.
        if not isinstance(exc, AuditError):
            write_diagnostic(f"review-bot: warning: {audit_exc}")


def _review(
    diff_path: Path, settings: Settings, provider: LLMProvider | None, audit: AuditLog
) -> int:
    selected_provider = build_provider(settings) if provider is None else provider
    is_own_claude = provider is None and settings.provider == "claude"
    pipeline = _run_pipeline_then_discard_unraisable if is_own_claude else run_pipeline
    try:
        result = asyncio.run(pipeline(diff_path, settings, selected_provider, audit))
    except Exception:
        raise
    except BaseException as exc:
        # Left alone, this skips ``main``'s handlers and can set the exit code itself, so
        # ``SystemExit(0)`` would read as a clean review. Only the review is wrapped because
        # ``--help`` exits through ``SystemExit`` too.
        raise _ReviewAbortedError(exc) from exc

    # A malformed response fails here, before anything is written. Stdout then goes first, so a
    # failure writing it leaves only the error line on stderr.
    findings = at_or_above(result.findings, settings.threshold)
    output = render_findings(findings, settings.threshold)
    summary = _summarise(result, settings, audit.run_id)
    exit_code = 1 if findings else 0

    write_output(output)
    if not result.model_called:
        write_diagnostic(_nothing_reviewed_warning(result.skipped_files))
    elif settings.provider == "mock":
        write_diagnostic(_CANNED_WARNING)
    write_diagnostic(summary)
    # After stdout, so a failure here exits 17 with the report already delivered.
    audit.record(Stage.RUN, "end", {"exit_code": exit_code, "error": None})
    return exit_code


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _run_pipeline_then_discard_unraisable(
    diff_path: Path, settings: Settings, provider: LLMProvider, audit: AuditLog
) -> PipelineResult:
    """Run ``run_pipeline``, then discard ``sys.unraisablehook`` reports until the process exits.

    The SDK's ``close()`` waits for the Claude CLI to exit, not for its pipes to close, so a
    helper that holds them open leaves a transport that raises from ``__del__`` once
    ``asyncio.run`` has closed the loop. CPython would print that as ``Exception ignored in``,
    which breaks the stderr contract. The hook goes in only once the review has ended, so reports
    during the review still show, and it is never restored because the process is about to exit.
    """
    try:
        return await run_pipeline(diff_path, settings, provider, audit)
    finally:
        sys.unraisablehook = _discard_unraisable


def _discard_unraisable(_report: "sys.UnraisableHookArgs") -> None:
    """Drop the report, since stderr may hold only the bot's own lines."""


def _nothing_reviewed_warning(skipped_files: Sequence[FileDiff]) -> str:
    # A deleted file has no new path.
    paths = [file.path or "(deleted file)" for file in skipped_files]
    named = ", ".join(paths[:_MAX_NAMED_SKIPPED_FILES])
    return f"{_NOTHING_REVIEWED_WARNING}{named} ({len(paths)} in total)"


def _summarise(result: PipelineResult, settings: Settings, run_id: str) -> str:
    # Phase 1 appends fields after Phase 0's, in the design's order (Compatibility).
    return (
        f"provider={settings.provider} model={settings.model} prompt={PROMPT_VERSION} "
        f"input_tokens={result.usage.input_tokens} output_tokens={result.usage.output_tokens} "
        f"skipped_files={len(result.skipped_files)} "
        f"model_called={str(result.model_called).lower()} run_id={run_id}"
    )


def _write_error(message: str) -> None:
    write_diagnostic(f"review-bot: error: {message}")
