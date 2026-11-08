"""Command line for pull requests: ``python -m review_bot.pr shadow|run|backfill|label ...``.

``shadow`` and ``run`` write a shadow report in ``--out DIR``, and ``backfill`` one per merged
pull request in ``--out DIR/<number>``; ``label`` judges what downloaded shadow reports would
post.

``run --mode`` accepts only ``shadow``, its default, until comment mode arrives (P2-S5, P2-S6).

It uses ``review-bot``'s exit codes, except that findings aren't a failure: a written report
with outcome ``reviewed``, ``blocked_secret`` or ``skipped`` exits 0. An unusable event or git
command exits 19 (Phase 2 design, Commands and exit codes).
"""

import argparse
import contextlib
import os
import sys
import tempfile
from collections import Counter
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
    write_output,
)
from review_bot.errors import ConfigError, ProviderError
from review_bot.pr.event import REPO_NAME, PullRequestRef, read_event, read_pulls
from review_bot.pr.git import GitRepo
from review_bot.pr.label import LABELLING_STOPPED, LabellingResult, read_report, run_labelling
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.runner import backfill_pull_request, run_pull_request
from review_bot.pr.shadow import (
    REPORTED_ERRORS,
    Origin,
    ReportedError,
    ShadowReport,
    build_shadow_report,
    render_shadow_json,
    render_shadow_markdown,
)
from review_bot.pr.units import ShadowFile
from review_bot.providers.base import LLMProvider
from review_bot.report import PipelineResult
from review_bot.secret_scan import redact
from review_bot.settings import Settings, in_ci, load_settings

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
# In CI, backfill must not spend (S13, Out of scope).
_BACKFILL_IN_CI: Final = (
    "backfill doesn't run in CI (CI is set): it spends the budget you approved, with your own "
    "login or key. Run it from your own shell."
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
class _BackfillCommand:
    pulls_path: Path
    github_repo: str
    repo_dir: Path
    out_dir: Path
    max_calls: int
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
        written, and 19 for an unusable event, pulls file or git command. ``backfill`` returns
        the first provider failure's code, an error's code if one stops it, and 2 in CI or over
        ``--max-calls``. ``label`` returns 0 when done or interrupted, and 2 with no terminal or
        an invalid labels file.
    """
    keep_library_logs_off_stderr()
    try:
        command = _parse_args(sys.argv[1:] if argv is None else argv)
        if isinstance(command, _LabelCommand):
            return _label(command)
        # Before --out is created: in CI, backfill touches nothing.
        if isinstance(command, _BackfillCommand):
            _refuse_backfill_in_ci()
        # Before any input is read, so an unwritable --out costs no review.
        _require_writable_directory(command.out_dir)
        if isinstance(command, _BackfillCommand):
            return _backfill(command, provider)
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


def _parse_args(
    argv: Sequence[str],
) -> _ShadowCommand | _RunCommand | _BackfillCommand | _LabelCommand:
    """Parse the command line into the subcommand, its paths and the settings.

    The audit log goes in the output directory unless ``REVIEW_BOT_AUDIT_LOG`` names one.
    ``backfill`` always has ``max_retries`` 0, whatever a flag or variable says.

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
    backfill = _add_command(
        commands.add_parser,
        "backfill",
        "review merged pull requests from a pulls file and a clone, posting nothing; each call "
        "is one attempt, whatever --max-retries says",
    )
    backfill.add_argument(
        "--pulls",
        type=Path,
        required=True,
        help="the JSON from gh pr list --state merged --json "
        "number,baseRefOid,headRefOid,mergedAt,isCrossRepository",
    )
    backfill.add_argument(
        "--github-repo",
        type=_github_repo,
        required=True,
        help="the repository the pull requests belong to, as OWNER/NAME",
    )
    backfill.add_argument(
        "--repo",
        type=Path,
        required=True,
        help="a clone of the repository, holding each pull request's base and head commits",
    )
    backfill.add_argument(
        "--max-calls",
        type=_whole_number,
        required=True,
        help="the most model calls allowed: one per pull request without a report in --out",
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
    if name == "backfill":
        pulls_path, github_repo = flags.pop("pulls"), flags.pop("github_repo")
        repo_dir, max_calls = flags.pop("repo"), flags.pop("max_calls")
        # Checked first, so an invalid value is still refused. Then one attempt per call, as
        # ``evals live`` makes, so --max-calls bounds the attempts.
        settings = load_settings(flags).model_copy(update={"max_retries": 0})
        return _BackfillCommand(pulls_path, github_repo, repo_dir, out_dir, max_calls, settings)
    del flags["mode"]
    event_path, repo_dir = flags.pop("event"), flags.pop("repo")
    return _RunCommand(event_path, repo_dir, out_dir, load_settings(flags))


def _github_repo(text: str) -> str:
    if not REPO_NAME.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} isn't a repository's OWNER/NAME")
    return text


def _whole_number(text: str) -> int:
    if not (text.isascii() and text.isdigit()):
        raise argparse.ArgumentTypeError(f"{text!r} isn't a whole number")
    return int(text)


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


def _refuse_backfill_in_ci() -> None:
    """Raise ``ConfigError`` if this process runs in CI, where backfill must not run."""
    if in_ci(os.environ):
        raise ConfigError(_BACKFILL_IN_CI)


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
        origin=Origin.LOCAL,
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


def _backfill(command: _BackfillCommand, provider: LLMProvider | None) -> int:
    """Review each merged pull request in the pulls file that has no report in ``--out`` yet.

    A pull request whose report there is ``failed`` is reviewed again, and its report replaced.
    Nothing is called over ``--max-calls``. Each pull request is its own run, with its own
    ``run_id``, report and audit records. Once any could run, stdout ends with the counts.

    Raises:
        ConfigError: If the planned calls exceed ``--max-calls``.
        EventError: If the pulls file is unusable.
        GitError: If git fails.
        AuditError: If the audit log can't be opened.
    """
    pulls = read_pulls(command.pulls_path)
    pending = [pull for pull in pulls if not _is_done(command.out_dir, pull.number)]
    # Counted before any is skipped, so the approved budget is the most that can be spent.
    if len(pending) > command.max_calls:
        raise ConfigError(
            f"backfill would make up to {len(pending)} calls, more than --max-calls "
            f"{command.max_calls}. Pass --max-calls {len(pending)} if that budget is approved."
        )
    git = GitRepo(command.repo_dir, timeout_s=_GIT_TIMEOUT_S)
    settings = command.settings
    # Filled as each pull request ends, so the counts cover those done before any stop.
    reports: list[ShadowReport] = []
    backfill = _backfill_pulls(
        [pull.ref(command.github_repo) for pull in pending],
        reports,
        git=git,
        make_provider=lambda: build_provider(settings) if provider is None else provider,
        settings=settings,
        out_dir=command.out_dir,
    )
    if uses_own_claude(settings, provider):
        backfill = then_discard_unraisable(backfill)
    try:
        return run_to_completion(backfill)
    finally:
        already = len(pulls) - len(pending)
        write_output(_backfill_counts(reports, entries=len(pulls), already=already))


async def _backfill_pulls(  # noqa: PLR0913 - keyword-only collaborators, as the runner's
    prs: Sequence[PullRequestRef],
    reports: list[ShadowReport],
    *,
    git: GitRepo,
    make_provider: Callable[[], LLMProvider],
    settings: Settings,
    out_dir: Path,
) -> int:
    """Run and report each pull request in turn, appending each report to ``reports``.

    A failed review doesn't stop the rest. Any other error, such as git failing or a report
    that can't be written, stops them at that pull request, with its message.

    Returns:
        The stopping error's exit code if one stopped them, else the first failed review's, or
        0 if none failed.

    Raises:
        AuditError: If the audit log can't be opened.
    """
    first_failure = 0
    for pr in prs:
        audit = AuditLog(settings.audit_log, clock=utc_now)
        pr_dir = out_dir / str(pr.number)
        try:
            shadow = await backfill_pull_request(
                pr,
                git=git,
                make_provider=make_provider,
                settings=settings,
                audit=audit,
                clock=utc_now,
            )
            _write_report(shadow, pr_dir)
        except Exception as exc:  # noqa: BLE001 - `report_failure` maps any failure to its code
            exit_code = report_failure(exc)
            record_failed_end(audit, exit_code, exc)
            return exit_code
        reports.append(shadow)
        exit_code = _finish(shadow, pr_dir, audit)
        first_failure = first_failure or exit_code
    return first_failure


def _is_done(out_dir: Path, number: int) -> bool:
    """Whether ``out_dir`` holds the pull request's report, with any outcome but ``failed``.

    A report that can't be read as ``shadow/v1`` counts as done, so it is never overwritten.
    """
    path = out_dir / str(number) / _JSON_NAME
    if not path.exists():
        return False
    report = read_report(path)
    return not (isinstance(report, ShadowFile) and report.outcome is Outcome.FAILED)


def _backfill_counts(reports: Sequence[ShadowReport], *, entries: int, already: int) -> str:
    """Two lines of ``name=N`` pairs: the totals, then each outcome and each reason given.

    Every outcome is shown, and so is ``too_large``: it is how many the reviewable-bytes cap
    left out (Phase 2 design, PR supply). Outcome, skip reason and error category names never
    coincide.
    """
    outcomes = Counter(report.outcome for report in reports)
    reasons = Counter(str(report.reason) for report in reports if report.reason is not None)
    reasons.setdefault(Reason.TOO_LARGE, 0)
    pairs = [f"{outcome}={outcomes[outcome]}" for outcome in Outcome]
    pairs += [f"{reason}={count}" for reason, count in sorted(reasons.items())]
    totals = f"entries={entries} already_in_out={already} run={len(reports)}"
    return f"backfill: {totals}\nbackfill: {' '.join(pairs)}"


def _write_report(shadow: ShadowReport, out_dir: Path) -> None:
    """Write ``shadow.json`` and ``shadow.md`` in ``out_dir``, creating it if it is missing.

    Raises:
        ConfigError: If either can't be written.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
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
    elif shadow.reason is Reason.MISSING_COMMIT and shadow.pr is not None:
        write_diagnostic(_missing_commit_hint(shadow.pr.number))
    write_diagnostic(_written_line(shadow, out_dir))
    audit.record(Stage.RUN, "end", {"exit_code": 0, "error": None})
    return 0


def _missing_commit_hint(number: int) -> str:
    return (
        f"review-bot pr: pull request {number}'s base or head commit isn't in --repo, so it "
        f"wasn't reviewed. Fetch refs/pull/{number}/head with git fetch origin "
        f"refs/pull/{number}/head:refs/pull/{number}/head, and if the base commit is missing, "
        "fetch the base branch's full history too, then run again."
    )


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
