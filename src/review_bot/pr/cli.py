"""Command line for pull requests: ``python -m review_bot.pr shadow|run|label ...``.

``shadow`` and ``run`` write a shadow report in ``--out DIR``; ``label`` judges what downloaded
shadow reports would post.

``run --mode`` accepts only ``shadow``, its default, until comment mode arrives (P2-S5, P2-S6).

It uses ``review-bot``'s exit codes, except that findings aren't a failure: a written report
with outcome ``reviewed``, ``blocked_secret`` or ``skipped`` exits 0. An unusable event or git
command exits 19 (Phase 2 design, Commands and exit codes).
"""

import contextlib
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from review_bot.audit import AuditLog, Stage, utc_now
from review_bot.cli import (
    RaisingArgumentParser,
    add_setting_arguments,
    build_provider,
    record_failed_end,
    report_failure,
    run_review,
    run_to_completion,
    then_discard_unraisable,
    uses_own_claude,
)
from review_bot.console import (
    is_terminal,
    keep_library_logs_off_stderr,
    read_answer,
    write_diagnostic,
)
from review_bot.errors import ConfigError, ProviderError
from review_bot.pr.event import read_event
from review_bot.pr.git import GitRepo
from review_bot.pr.label import LABELLING_STOPPED, LabellingResult, run_labelling
from review_bot.pr.outcome import Reason
from review_bot.pr.runner import run_pull_request
from review_bot.pr.shadow import (
    REPORTED_ERRORS,
    ReportedError,
    ShadowReport,
    build_shadow_report,
    render_shadow_json,
    render_shadow_markdown,
)
from review_bot.providers.base import LLMProvider
from review_bot.report import PipelineResult
from review_bot.secret_scan import redact
from review_bot.settings import Settings, load_settings

__all__ = ["main"]

_AUDIT_LOG_VARIABLE: Final = "REVIEW_BOT_AUDIT_LOG"
_AUDIT_LOG_NAME: Final = "audit.jsonl"
_JSON_NAME: Final = "shadow.json"
_MARKDOWN_NAME: Final = "shadow.md"
_GIT_TIMEOUT_S: Final = 60
_NO_CREDENTIAL_HINT: Final = (
    "review-bot pr: ANTHROPIC_API_KEY is unset or empty in CI, so nothing was reviewed. Add it "
    "as a repository secret to review pull requests."
)
_NO_TERMINAL_FOR_LABEL: Final = (
    "label needs a terminal: stdin and stderr must both be attached to one. Run it from an "
    "interactive shell."
)


@dataclass(frozen=True, slots=True)
class _ShadowCommand:
    diff_path: Path
    out_dir: Path
    settings: Settings


@dataclass(frozen=True, slots=True)
class _RunCommand:
    event_path: Path
    repo_dir: Path
    out_dir: Path
    settings: Settings


@dataclass(frozen=True, slots=True)
class _LabelCommand:
    reports_dir: Path
    labels_path: Path


def main(argv: Sequence[str] | None = None, *, provider: LLMProvider | None = None) -> int:
    """Run one subcommand and return the process exit code.

    Args:
        argv: The arguments; None means the process's own.
        provider: The provider to use; None means ``review_bot.cli.build_provider`` makes it
            from the settings, and only if the run calls one.

    Returns:
        0 for a written report that isn't ``failed``; otherwise ``review-bot``'s code for the
        failure, such as 2 for a usage error or an unwritable ``--out``, 3 for a missing diff,
        4 for one the system can't read, 7 to 13 for a provider failure, whose report is still
        written, and 19 for an unusable event or git command. ``label`` returns 0 when done or
        interrupted, and 2 with no terminal or an invalid labels file.
    """
    keep_library_logs_off_stderr()
    try:
        command = _parse_args(sys.argv[1:] if argv is None else argv)
        if isinstance(command, _LabelCommand):
            return _label(command)
        # Before any input is read, so an unwritable --out costs no review.
        _require_writable_directory(command.out_dir)
        audit = AuditLog(command.settings.audit_log, clock=utc_now)
    except Exception as exc:  # noqa: BLE001 - `report_failure` maps any failure to its code
        return report_failure(exc)
    try:
        match command:
            case _ShadowCommand():
                shadow = _shadow(command, provider, audit)
            case _RunCommand():
                shadow = _run(command, provider, audit)
        _write_report(shadow, command.out_dir)
        return _finish(shadow, command.out_dir, audit)
    except Exception as exc:  # noqa: BLE001 - `report_failure` maps any failure to its code
        exit_code = report_failure(exc)
        record_failed_end(audit, exit_code, exc)
        return exit_code


def _parse_args(argv: Sequence[str]) -> _ShadowCommand | _RunCommand | _LabelCommand:
    """Parse the command line into the subcommand, its paths and the settings.

    The audit log goes in the output directory unless ``REVIEW_BOT_AUDIT_LOG`` names one.

    Raises:
        ConfigError: If an argument, or a setting from the environment, is invalid.
    """
    parser = RaisingArgumentParser(
        prog="python -m review_bot.pr",
        description="Report what the review bot would post on a pull request.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    shadow = _add_command(
        commands.add_parser,
        "shadow",
        "write the comments and summary the bot would post for a diff file, posting nothing",
    )
    shadow.add_argument("diff_path", help="the diff file to review")
    run = _add_command(
        commands.add_parser,
        "run",
        "review a pull request from its event file and a clone, posting nothing",
    )
    run.add_argument(
        "--event", type=Path, required=True, help="the pull_request event file (GITHUB_EVENT_PATH)"
    )
    run.add_argument(
        "--repo", type=Path, required=True, help="the clone holding the base and head commits"
    )
    run.add_argument(
        "--mode",
        choices=["shadow"],
        default="shadow",
        help="shadow (default), the only mode for now",
    )
    label = commands.add_parser(
        "label",
        help="accept or reject, on the terminal, each comment the shadow reports would post",
        description="Ask about each would-be comment not yet labelled, appending each verdict "
        "to the labels file at once.",
        allow_abbrev=False,
    )
    label.add_argument(
        "--reports",
        type=Path,
        required=True,
        help="the directory holding the downloaded shadow.json files, at any depth",
    )
    label.add_argument(
        "--labels", type=Path, required=True, help="the feedback/v1 JSONL file to append to"
    )
    flags = vars(parser.parse_args(argv))
    name = flags.pop("command")
    if name == "label":
        return _LabelCommand(flags["reports"], flags["labels"])
    out_dir = flags.pop("out")
    if _AUDIT_LOG_VARIABLE not in os.environ:
        flags["audit_log"] = str(out_dir / _AUDIT_LOG_NAME)
    if name == "shadow":
        diff_path = Path(flags.pop("diff_path"))
        return _ShadowCommand(diff_path, out_dir, load_settings(flags))
    del flags["mode"]
    event_path, repo_dir = flags.pop("event"), flags.pop("repo")
    return _RunCommand(event_path, repo_dir, out_dir, load_settings(flags))


def _add_command(
    add_parser: Callable[..., RaisingArgumentParser], name: str, summary: str
) -> RaisingArgumentParser:
    """Add a subcommand with ``--out`` and the setting flags."""
    command = add_parser(
        name,
        help=summary,
        description="The setting options, from --provider to --deadline-s, can also be set with "
        "REVIEW_BOT_<OPTION>.",
        allow_abbrev=False,
    )
    command.add_argument(
        "--out", type=Path, required=True, help="the directory to write shadow.json and shadow.md"
    )
    add_setting_arguments(command)
    return command


def _label(command: _LabelCommand) -> int:
    """Judge the would-be comments on the terminal, then show what the session did.

    Ctrl+C before the first question, or after the last, stops it with no counts, since
    ``run_labelling`` handles one during the questions and nothing is judged outside them.

    Raises:
        ConfigError: If stdin or stderr isn't a terminal, checked before anything is read, or
            as ``run_labelling`` raises it.
    """
    if not (is_terminal(sys.stdin) and is_terminal(sys.stderr)):
        raise ConfigError(_NO_TERMINAL_FOR_LABEL)
    try:
        result = run_labelling(
            command.reports_dir,
            command.labels_path,
            read_line=read_answer,
            write_line=write_diagnostic,
            clock=utc_now,
        )
        write_diagnostic(_labelled_line(result))
    except KeyboardInterrupt:
        # A second Ctrl+C only loses this notice.
        with contextlib.suppress(KeyboardInterrupt):
            write_diagnostic(LABELLING_STOPPED)
    return 0


def _labelled_line(result: LabellingResult) -> str:
    return (
        f"review-bot pr: label: judged={result.judged} skipped={result.skipped} "
        f"remaining={result.remaining} files_skipped={result.files_skipped}"
    )


def _require_writable_directory(out_dir: Path) -> None:
    """Create ``out_dir`` if it is missing, and prove a file can be written in it.

    Raises:
        ConfigError: If it can't be created or written.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=out_dir):
            pass
    except OSError as exc:
        raise _unwritable(out_dir, exc) from exc


def _shadow(command: _ShadowCommand, provider: LLMProvider | None, audit: AuditLog) -> ShadowReport:
    started_at = utc_now()
    review: PipelineResult | ReportedError
    try:
        review = run_review(command.diff_path, command.settings, provider, audit)
    except REPORTED_ERRORS as exc:
        review = exc
    return build_shadow_report(
        review,
        settings=command.settings,
        run_id=audit.run_id,
        started_at=started_at,
        finished_at=utc_now(),
        pr=None,
    )


def _run(command: _RunCommand, provider: LLMProvider | None, audit: AuditLog) -> ShadowReport:
    """Review the pull request in the event file.

    Raises:
        EventError: If the event file is unusable; nothing else runs.
        GitError: If a commit is missing or git fails; no provider is called.
    """
    pr = read_event(command.event_path)
    settings = command.settings
    run = run_pull_request(
        pr,
        git=GitRepo(command.repo_dir, timeout_s=_GIT_TIMEOUT_S),
        make_provider=lambda: build_provider(settings) if provider is None else provider,
        settings=settings,
        audit=audit,
        environ=os.environ,
        clock=utc_now,
    )
    if uses_own_claude(settings, provider):
        run = then_discard_unraisable(run)
    return run_to_completion(run)


def _write_report(shadow: ShadowReport, out_dir: Path) -> None:
    """Write ``shadow.json`` and ``shadow.md`` in ``out_dir``.

    Raises:
        ConfigError: If either can't be written.
    """
    try:
        (out_dir / _JSON_NAME).write_text(
            render_shadow_json(shadow) + "\n", encoding="ascii", newline="\n"
        )
        (out_dir / _MARKDOWN_NAME).write_text(
            render_shadow_markdown(shadow) + "\n", encoding="utf-8", newline="\n"
        )
    except OSError as exc:
        raise _unwritable(out_dir, exc) from exc


def _finish(shadow: ShadowReport, out_dir: Path, audit: AuditLog) -> int:
    """Say what was written and why, record the run's end, and return its exit code."""
    error = shadow.error
    if isinstance(error, ProviderError):
        write_diagnostic(_written_line(shadow, out_dir))
        exit_code = report_failure(error)
        record_failed_end(audit, exit_code, error)
        return exit_code
    if error is not None:
        # The error's own message says what to do; it names paths only redacted.
        write_diagnostic(f"review-bot pr: {redact(str(error))}")
    elif shadow.reason is Reason.NO_CREDENTIAL:
        write_diagnostic(_NO_CREDENTIAL_HINT)
    write_diagnostic(_written_line(shadow, out_dir))
    audit.record(Stage.RUN, "end", {"exit_code": 0, "error": None})
    return 0


def _unwritable(out_dir: Path, exc: OSError) -> ConfigError:
    reason = exc.strerror or type(exc).__name__
    return ConfigError(
        f"Can't write the shadow report in --out {redact(str(out_dir))}: {reason}. Pass a "
        "directory you can write to."
    )


def _written_line(shadow: ShadowReport, out_dir: Path) -> str:
    reason = "" if shadow.reason is None else f" reason={shadow.reason}"
    return (
        f"review-bot pr: wrote {_JSON_NAME} and {_MARKDOWN_NAME} in {redact(str(out_dir))}: "
        f"outcome={shadow.outcome}{reason} comments={len(shadow.comments)} "
        f"withheld={len(shadow.withheld)} run_id={shadow.run_id}"
    )
