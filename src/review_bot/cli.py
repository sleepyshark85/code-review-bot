"""Command line: arguments, the one event loop, printing, and errors to exit codes."""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Final, NoReturn, override

from review_bot.audit import AuditLog, Stage, utc_now
from review_bot.console import (
    discard_unraisable_reports,
    is_terminal,
    keep_library_logs_off_stderr,
    read_answer,
    write_diagnostic,
    write_output,
)
from review_bot.context.tools import RepoRef
from review_bot.diff_parser import FileDiff
from review_bot.errors import (
    AuditError,
    ConfigError,
    DiffEmptyError,
    DiffNotFoundError,
    DiffNotParsableError,
    DiffNotUtf8Error,
    DiffTooLargeError,
    DiffUnreadableError,
    ErrorCategory,
    EventError,
    GitError,
    GitHubError,
    InputError,
    ProviderError,
    RepoConfigError,
    ReviewBotError,
    SecretDetectedError,
)
from review_bot.findings import ReviewedFinding, Severity, Source
from review_bot.git import GIT_TIMEOUT_S, GitRepo
from review_bot.governance.policy import (
    BUILTIN_POLICY,
    MAX_POLICY_BYTES,
    Policy,
    PolicyError,
    check_settings,
    parse_policy,
)
from review_bot.providers.base import LLMProvider
from review_bot.providers.mock import MockProvider
from review_bot.render import render_findings, render_json, render_markdown
from review_bot.report import PipelineResult, not_rejected
from review_bot.review import ReviewContext, run_pipeline
from review_bot.secret_scan import redact
from review_bot.settings import Settings, load_settings, take_github_token
from review_bot.triage import TriageResult, run_triage

__all__ = [
    "RaisingArgumentParser",
    "add_setting_arguments",
    "add_tool_cap_arguments",
    "build_provider",
    "main",
    "parse_args",
    "read_policy",
    "record_failed_end",
    "report_failure",
    "run_review",
    "run_to_completion",
    "settings_within_policy",
    "then_discard_unraisable",
    "uses_own_claude",
]

_INPUT_ERROR_EXIT_CODES: Final[Mapping[type[InputError], int]] = {
    ConfigError: 2,
    PolicyError: 2,
    DiffNotFoundError: 3,
    DiffUnreadableError: 4,
    DiffNotUtf8Error: 4,
    DiffEmptyError: 5,
    DiffTooLargeError: 6,
    DiffNotParsableError: 15,
    SecretDetectedError: 16,
    # Only ``python -m review_bot.pr`` raises these (Phase 2 design, Commands and exit codes),
    # and ``GitError`` too when git times out or can't start under ``--context-repo``.
    EventError: 19,
    GitError: 19,
    RepoConfigError: 2,
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
_CONTEXT_FLAG: Final = "--context-repo"
_AUDIT_ERROR_EXIT_CODE: Final = 17
# Only ``python -m review_bot.pr`` calls GitHub (Phase 2 design, Commands and exit codes).
_GITHUB_ERROR_EXIT_CODE: Final = 18

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

_NO_TERMINAL_FOR_TRIAGE: Final = (
    "--triage needs a terminal: stdin and stderr must both be attached to one. Run it from an "
    "interactive shell, or leave out --triage."
)


class _ReviewAbortedError(ReviewBotError):
    """The review raised a ``BaseException`` such as ``SystemExit``, so it has no result."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__()
        # Its class name only, which the audit log records: its text can hold CLI output.
        self.cause_name = type(cause).__name__


class RaisingArgumentParser(argparse.ArgumentParser):
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


def add_setting_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the review's setting flags, each named after its ``Settings`` field."""
    parser.add_argument("--provider", help="the provider to use: claude (default), mock")
    parser.add_argument("--model", help="the model to request")
    parser.add_argument("--threshold", help=f"the lowest severity to report: {', '.join(Severity)}")
    parser.add_argument("--max-diff-bytes", help="the largest diff file to review, in bytes")
    parser.add_argument(
        "--max-review-bytes",
        help="the most bytes of files to send to the model, after skipping (default: no cap)",
    )
    parser.add_argument(
        "--max-output-tokens", help="the Claude CLI's output token cap (default: the CLI's own)"
    )
    parser.add_argument("--max-retries", help="retries the Claude CLI makes for a failed API call")
    parser.add_argument("--api-timeout-ms", help="the Claude CLI's API timeout, in milliseconds")
    parser.add_argument("--deadline-s", help="the time limit for the whole call, in seconds")
    parser.add_argument(
        "--max-cost-usd",
        help=(
            "the most a review may cost, in USD, from 0.01 to 1000; the Claude CLI stops past it "
            "(default: the policy's ceiling, else no cap)"
        ),
    )
    parser.add_argument(
        "--policy", help="the policy file the run is under (default: the built-in policy)"
    )


def parse_args(argv: Sequence[str]) -> tuple[Path, Settings]:
    """Parse the command line into the diff path and the settings.

    Raises:
        ConfigError: If an argument, or a setting from the environment, is invalid.
    """
    parser = RaisingArgumentParser(
        prog="review-bot",
        description="Review a diff file.",
        epilog=(
            "Each option except --triage can also be set with REVIEW_BOT_<OPTION>, e.g. "
            "REVIEW_BOT_MODEL."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("diff_path", help="the diff file to review")
    add_setting_arguments(parser)
    parser.add_argument(
        "--audit-log",
        help="the JSONL file each run appends its records to (default: .review-bot/audit.jsonl)",
    )
    parser.add_argument(
        "--format", help="the report's format on stdout: text (default), json, markdown"
    )
    parser.add_argument(
        "--triage",
        action="store_true",
        help="ask on the terminal to accept or reject each reported finding",
    )
    _add_context_arguments(parser)
    args = vars(parser.parse_args(argv))
    return Path(args.pop("diff_path")), load_settings(args)


def _add_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--context-repo",
        help="a git repository the model may read, search and list at one commit (context mode)",
    )
    parser.add_argument(
        "--context-rev", help="the commit of --context-repo the model reads (default: HEAD)"
    )
    add_tool_cap_arguments(parser)


def add_tool_cap_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the context mode's tool cap flags, each named after its ``Settings`` field."""
    parser.add_argument("--max-tool-calls", help="the most tool calls in a review (default: 12)")
    parser.add_argument(
        "--max-tool-result-bytes", help="the most bytes one tool call returns (default: 16000)"
    )
    parser.add_argument(
        "--max-tool-total-bytes",
        help="the most bytes all tool calls in a review return (default: 96000)",
    )


def _open_context(settings: Settings) -> ReviewContext | None:
    """Resolve ``--context-repo`` and ``--context-rev`` to the commit the tools read.

    Returns:
        The context, or None in diff mode, when ``--context-repo`` isn't given.

    Raises:
        ConfigError: If ``--context-rev`` is given without ``--context-repo``, the directory
            isn't in a git repository, or the revision names no commit.
        GitError: If git fails, times out or can't start.
    """
    if settings.context_repo is None:
        if "context_rev" in settings.model_fields_set:
            raise ConfigError(
                "--context-rev (or REVIEW_BOT_CONTEXT_REV) needs --context-repo: pass the "
                "repository too, or leave out the revision."
            )
        return None
    shown_repo = redact(str(settings.context_repo))
    directory = GitRepo(settings.context_repo, timeout_s=GIT_TIMEOUT_S, flag=_CONTEXT_FLAG)
    top_level = directory.top_level()
    if top_level is None:
        raise ConfigError(
            f"--context-repo {shown_repo} isn't in a git repository. Pass the directory of a "
            "git clone."
        )
    git = GitRepo(top_level, timeout_s=GIT_TIMEOUT_S, flag=_CONTEXT_FLAG)
    commit = git.resolve_commit(settings.context_rev)
    if commit is None:
        raise ConfigError(
            f"--context-rev {settings.context_rev} names no commit in --context-repo "
            f"{shown_repo}. Pass a branch, tag or SHA it holds."
        )
    return ReviewContext(repo=RepoRef(git=git, commit=commit))


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
    keep_library_logs_off_stderr()
    # Before any provider is built, so the Claude CLI never inherits it.
    take_github_token()
    try:
        # A usage error or ``--help`` records nothing, because the log's path isn't known yet.
        diff_path, settings = parse_args(sys.argv[1:] if argv is None else argv)
        settings, policy = settings_within_policy(settings)
        # Before anything else runs, so triage can never wait on input nobody will give.
        _require_terminal_for_triage(settings)
        # Before the audit log, like any other bad argument: no provider is called.
        context = _open_context(settings)
        # Opened before the diff is read, so an untraceable run never reaches a provider.
        audit = AuditLog(settings.audit_log, clock=utc_now)
    except Exception as exc:  # noqa: BLE001 - `report_failure` maps any failure to its code
        return report_failure(exc)
    try:
        return _review(diff_path, settings, policy, provider, audit, context)
    except Exception as exc:  # noqa: BLE001 - `report_failure` maps any failure to its code
        exit_code = report_failure(exc)
        record_failed_end(audit, exit_code, exc)
        return exit_code


def settings_within_policy(settings: Settings) -> tuple[Settings, Policy]:
    """The policy ``settings.policy`` names, and ``settings`` checked against it.

    Raises:
        PolicyError: If the policy file can't be read or is invalid.
        ConfigError: If ``settings`` ask for more than the policy allows (``check_settings``).
    """
    policy = read_policy(settings.policy)
    return check_settings(settings, policy), policy


def read_policy(path: str | None) -> Policy:
    """The policy in the file at ``path``, or the built-in policy if ``path`` is None.

    The file is read once, at most one byte past the limit, so an oversized file is reported.

    Raises:
        PolicyError: If the file is missing, can't be read or is invalid, naming it.
    """
    if path is None:
        return BUILTIN_POLICY
    try:
        with Path(path).open("rb") as file:
            data = file.read(MAX_POLICY_BYTES + 1)
    except FileNotFoundError as exc:
        raise _unusable_policy(path, "it doesn't exist") from exc
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise _unusable_policy(path, f"it can't be read: {reason}") from exc
    return parse_policy(data, source=path)


def _unusable_policy(path: str, problem: str) -> PolicyError:
    return PolicyError(
        f"The policy file {path} can't be used: {problem}. Pass a policy file you can read, or "
        "leave out --policy and REVIEW_BOT_POLICY to use the built-in policy."
    )


def _require_terminal_for_triage(settings: Settings) -> None:
    """Raise ``ConfigError`` if ``--triage`` is given and nobody can answer it."""
    if settings.triage and not (is_terminal(sys.stdin) and is_terminal(sys.stderr)):
        raise ConfigError(_NO_TERMINAL_FOR_TRIAGE)


def report_failure(exc: Exception) -> int:
    """Write the one error line for ``exc`` and return its exit code.

    The codes are the Phase 0 and Phase 1 tables', which ``python -m review_bot.pr`` shares.
    """
    if isinstance(exc, InputError):
        _write_error(str(exc))
        return _INPUT_ERROR_EXIT_CODES.get(type(exc), _INTERNAL_ERROR_EXIT_CODE)
    if isinstance(exc, ProviderError):
        _write_error(f"{exc.category}: {exc}")
        return _PROVIDER_ERROR_EXIT_CODES.get(exc.category, _INTERNAL_ERROR_EXIT_CODE)
    if isinstance(exc, AuditError):
        _write_error(str(exc))
        return _AUDIT_ERROR_EXIT_CODE
    if isinstance(exc, GitHubError):
        _write_error(str(exc))
        return _GITHUB_ERROR_EXIT_CODE
    # An escaped exception would exit 1, "findings found". The exception's text can hold untrusted
    # CLI output, so it is never shown.
    _write_error(_INTERNAL_ERROR_MESSAGE)
    return _INTERNAL_ERROR_EXIT_CODE


def record_failed_end(audit: AuditLog, exit_code: int, exc: Exception) -> None:
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


def _review(  # noqa: PLR0913, PLR0917 - the run's inputs, passed through
    diff_path: Path,
    settings: Settings,
    policy: Policy,
    provider: LLMProvider | None,
    audit: AuditLog,
    context: ReviewContext | None,
) -> int:
    result = run_review(diff_path, settings, provider, audit, policy=policy, context=context)
    # Before any output, so stdout holds only the report, written after the last decision.
    triage = None
    if settings.triage:
        triage = run_triage(
            result.decision.reported, read_line=read_answer, write_line=write_diagnostic
        )
        # Before stdout, so a failure writing the report can't lose the decisions.
        _record_labels(triage, settings, result.prompt_version, audit)

    # Stdout goes first, so a failure writing it leaves only the error line on stderr.
    output = _render_report(result, settings, audit.run_id, triage)
    dropped_count = len(result.verification.dropped)
    summary = _summarise(result, settings, audit.run_id)
    exit_code = 1 if _shown(result, triage) else 0

    write_output(output)
    if not result.model_called:
        write_diagnostic(_nothing_reviewed_warning(result.skipped_files))
    elif settings.provider == "mock":
        write_diagnostic(_CANNED_WARNING)
    if dropped_count and dropped_count == len(result.verification.checks):
        # Otherwise the run would look clean (ADR 0006).
        write_diagnostic(_all_dropped_warning(dropped_count))
    write_diagnostic(summary)
    # After stdout, so a failure here exits 17 with the report already delivered.
    audit.record(Stage.RUN, "end", {"exit_code": exit_code, "error": None})
    return exit_code


def run_review(  # noqa: PLR0913 - policy and context are keyword-only
    diff_path: Path,
    settings: Settings,
    provider: LLMProvider | None,
    audit: AuditLog,
    *,
    policy: Policy,
    context: ReviewContext | None = None,
) -> PipelineResult:
    """Run ``run_pipeline`` in the one event loop, with ``provider`` or the settings' own.

    ``policy`` is the policy the review is under, which ``settings`` are within. ``context`` is
    the commit the model's tools read; None reviews in diff mode.

    Raises:
        ReviewBotError: As ``run_pipeline`` does; any other ``BaseException`` the review raises
            becomes one too, so it can't set the exit code itself.
    """
    selected_provider = build_provider(settings) if provider is None else provider
    review = run_pipeline(
        diff_path, settings, selected_provider, audit, context=context, policy=policy
    )
    if uses_own_claude(settings, provider):
        review = then_discard_unraisable(review)
    return run_to_completion(review)


def uses_own_claude(settings: Settings, provider: LLMProvider | None) -> bool:
    """Whether the run builds the settings' own Claude provider, with no provider injected."""
    return provider is None and settings.provider == "claude"


def run_to_completion[T](review: Coroutine[object, object, T]) -> T:
    """Run ``review`` in the one event loop and return its result.

    Raises:
        Exception: Whatever ``review`` raises; any other ``BaseException`` becomes a
            ``ReviewBotError``, so it can't set the exit code itself.
    """
    try:
        return asyncio.run(review)
    except Exception:
        raise
    except BaseException as exc:
        # Left alone, this skips ``main``'s handlers and can set the exit code itself, so
        # ``SystemExit(0)`` would read as a clean review. Only the review is wrapped because
        # ``--help`` exits through ``SystemExit`` too.
        raise _ReviewAbortedError(exc) from exc


def _record_labels(
    triage: TriageResult, settings: Settings, prompt_version: str, audit: AuditLog
) -> None:
    for label in triage.labels:
        audit.record(
            Stage.TRIAGE,
            "labelled",
            {
                "fingerprint": label.fingerprint,
                "label": label.label,
                "severity_before": label.severity_before,
                "severity_after": label.severity_after,
                "rationale_changed": label.rationale_changed,
                "source": label.source,
                "model": settings.model,
                "prompt_version": prompt_version,
            },
        )


def _shown(result: PipelineResult, triage: TriageResult | None) -> tuple[ReviewedFinding, ...]:
    """The reported findings a person didn't reject: all of them if triage didn't run."""
    return result.decision.reported if triage is None else not_rejected(triage.findings)


def _render_report(
    result: PipelineResult, settings: Settings, run_id: str, triage: TriageResult | None
) -> str:
    triaged = None if triage is None else triage.findings
    if settings.format == "json":
        return render_json(result, settings, run_id, triaged=triaged)
    if settings.format == "markdown":
        return render_markdown(result, settings.threshold, triaged=triaged)
    findings = [reviewed.finding for reviewed in _shown(result, triage)]
    return render_findings(findings, settings.threshold)


async def then_discard_unraisable[T](review: Awaitable[T]) -> T:
    """Await ``review``, then discard ``sys.unraisablehook`` reports until the process exits.

    For a review with the settings' own Claude provider. The hook goes in only once the review
    has ended, so reports during the review still show (``console.discard_unraisable_reports``).
    """
    try:
        return await review
    finally:
        discard_unraisable_reports()


def _nothing_reviewed_warning(skipped_files: Sequence[FileDiff]) -> str:
    # A deleted file has no new path. Each path is redacted on its own, so a private key's
    # ``END`` marker in one can't redact the rest of the line.
    paths = [redact(file.path) if file.path else "(deleted file)" for file in skipped_files]
    named = ", ".join(paths[:_MAX_NAMED_SKIPPED_FILES])
    return f"{_NOTHING_REVIEWED_WARNING}{named} ({len(paths)} in total)"


def _all_dropped_warning(dropped_count: int) -> str:
    if dropped_count == 1:
        return (
            "review-bot: warning: the 1 model finding was dropped: its file or line isn't in "
            "the diff."
        )
    return (
        f"review-bot: warning: all {dropped_count} model findings were dropped: their file or "
        "line isn't in the diff."
    )


def _summarise(result: PipelineResult, settings: Settings, run_id: str) -> str:
    # Phase 1 appends fields after Phase 0's, in the design's order (Compatibility), and
    # context mode appends its own.
    verification = result.verification
    rule_count = sum(reviewed.source is Source.RULE for reviewed in verification.kept)
    tool_calls = "" if result.context_commit is None else f" tool_calls={len(result.ledger)}"
    return (
        f"provider={settings.provider} model={settings.model} prompt={result.prompt_version} "
        f"input_tokens={result.usage.input_tokens} output_tokens={result.usage.output_tokens} "
        f"rule_findings={rule_count} dropped={len(verification.dropped)} "
        f"deduped={len(verification.deduped)} "
        f"skipped_files={len(result.skipped_files)} "
        f"model_called={str(result.model_called).lower()} run_id={run_id}{tool_calls}"
    )


def _write_error(message: str) -> None:
    # Messages redact the paths they name; this is the backstop for text they don't control,
    # such as argparse quoting a stray argument.
    write_diagnostic(f"review-bot: error: {redact(message)}")
