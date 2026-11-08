"""Command line for pull requests: ``python -m review_bot.pr <subcommand>``.

``shadow`` and ``run`` write a shadow report in ``--out DIR``, and ``backfill`` one per merged
pull request in ``--out DIR/<number>``; ``label`` judges what downloaded shadow reports would
post. ``collect`` writes ``feedback.jsonl`` in ``--out DIR``, from the reactions and resolutions
on the bot's comments, with no model. ``baseline`` writes the ``baseline/v1`` document in
``--out FILE``, from a repository's merged pull requests, with no model. ``metrics`` shows
the numbers of downloaded shadow reports and labels on stdout, offline.

``run --mode comment`` also posts on the pull request, and ``collect`` and ``baseline`` read
GitHub, through its API with the token in ``GITHUB_TOKEN``; without it, they exit 2.

It uses ``review-bot``'s exit codes, except that findings aren't a failure: a written report
with outcome ``reviewed``, ``blocked_secret`` or ``skipped`` exits 0. An invalid
``.review-bot.toml`` exits 2, an unusable event or git command 19, and a failed GitHub call 18
(Phase 2 design, Commands and exit codes).
"""

import argparse
import contextlib
import os
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

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
from review_bot.errors import ConfigError, GitHubError, ProviderError, RepoConfigError
from review_bot.git import GIT_TIMEOUT_S, GitRepo
from review_bot.pr.config import DEFAULT_REPO_CONFIG
from review_bot.pr.event import REPO_NAME, PullRequestRef, read_event, read_pulls
from review_bot.pr.feedback import (
    FeedbackRecord,
    SignalFeedback,
    labels_from_signals,
    render_feedback_line,
)
from review_bot.pr.github import GitHubClient
from review_bot.pr.label import LABELLING_STOPPED, LabellingResult, run_labelling
from review_bot.pr.metrics import (
    Prices,
    render_baseline_json,
    render_baseline_text,
    summarise_baseline,
)
from review_bot.pr.metrics_report import render_metrics_json, render_metrics_text
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.reports import read_labels, read_report, read_reports, skipped_line
from review_bot.pr.rest_github import (
    API_URL_VARIABLE,
    RestGitHubClient,
    github_access,
)
from review_bot.pr.run_metrics import summarise_runs
from review_bot.pr.runner import backfill_pull_request, run_pull_request
from review_bot.pr.shadow import (
    REPORTED_ERRORS,
    Mode,
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
from review_bot.settings import Settings, in_ci, load_settings, take_github_token

__all__ = ["main"]

_AUDIT_LOG_VARIABLE: Final = "REVIEW_BOT_AUDIT_LOG"
_AUDIT_LOG_NAME: Final = "audit.jsonl"
_JSON_NAME: Final = "shadow.json"
_MARKDOWN_NAME: Final = "shadow.md"
_NO_CREDENTIAL_HINT: Final = (
    "review-bot pr: ANTHROPIC_API_KEY is unset or empty in CI, so nothing was reviewed. Add it "
    "as a repository secret to review pull requests."
)
# In CI, backfill must not spend (S13, Out of scope).
_BACKFILL_IN_CI: Final = (
    "backfill doesn't run in CI (CI is set): it spends the budget you approved, with your own "
    "login or key. Run it from your own shell."
)
_FEEDBACK_NAME: Final = "feedback.jsonl"
_COLLECT_WITHOUT_CREDENTIAL: Final = (
    "collect needs GITHUB_TOKEN, which is unset or empty, so nothing was collected. Run it in "
    "the collect job, which sets it, or set it to a token that can read the pull request."
)
_BASELINE_WITHOUT_CREDENTIAL: Final = (
    "baseline needs GITHUB_TOKEN, which is unset or empty, so nothing was read. Set it to a "
    "token that can read the repository's pull requests and issues, such as gh auth token's."
)
# The escaped-defect proxy's issue label (Phase 2 design, Q8).
_BUG_LABEL: Final = "bug"
# A price flag's value: digits, with an optional fraction.
_PRICE: Final = re.compile(r"[0-9]+(\.[0-9]+)?")
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
    mode: Mode
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


@dataclass(frozen=True, slots=True)
class _CollectCommand:
    event_path: Path
    out_dir: Path


@dataclass(frozen=True, slots=True)
class _MetricsCommand:
    reports_dir: Path
    labels_paths: tuple[Path, ...]
    since: date | None
    until: date | None
    prices: Prices | None
    as_json: bool


@dataclass(frozen=True, slots=True)
class _BaselineCommand:
    github_repo: str
    since: date
    until: date
    out_path: Path


def main(
    argv: Sequence[str] | None = None,
    *,
    provider: LLMProvider | None = None,
    github: GitHubClient | None = None,
) -> int:
    """Run one subcommand and return the process exit code.

    Args:
        argv: The arguments; None means the process's own.
        provider: The provider to use; None means ``review_bot.cli.build_provider`` makes it
            from the settings, and only if the run calls one.
        github: The GitHub client ``run --mode comment``, ``collect`` and ``baseline`` use;
            None means a ``RestGitHubClient`` from ``GITHUB_TOKEN`` and ``GITHUB_API_URL``.
            ``--mode shadow`` uses none, and builds no client.

    Returns:
        0 for a written report that isn't ``failed``; otherwise ``review-bot``'s code for the
        failure, such as 2 for a usage error, an unwritable ``--out`` or an invalid
        ``.review-bot.toml``, whose report is still written, 3 for a missing diff,
        4 for one the system can't read, 7 to 13 for a provider failure, whose report is still
        written, 18 for a failed GitHub call, whose report is still written unless listing the
        bot's comments failed, and 19 for an unusable event, pulls file or git command. A
        ``run --mode comment`` with no ``github`` exits 2 if ``GITHUB_TOKEN`` is missing or
        ``GITHUB_API_URL`` isn't allowed. ``backfill`` returns the first provider failure's
        or invalid config's code, an error's code if one stops it, and 2 in CI or over
        ``--max-calls``. ``label`` returns 0 when done or interrupted, and 2 with no terminal or
        an invalid labels file. ``collect`` returns 0 once ``feedback.jsonl`` is written; 2
        with no ``github`` and an unusable ``GITHUB_TOKEN`` or ``GITHUB_API_URL``, or for an
        unwritable ``--out``; 18 for a failed GitHub call, and 19 for an unusable event. A
        failed GitHub call or an unusable event writes nothing. ``baseline`` returns 0 once
        ``--out FILE`` is written; 2 for an invalid flag, an unwritable ``FILE``, or with no
        ``github`` and an unusable ``GITHUB_TOKEN`` or ``GITHUB_API_URL``; and 18 for a failed
        GitHub call, which writes nothing. ``metrics`` returns 0 once the numbers are shown,
        and 2 for an invalid flag, a --reports that isn't a directory, an --until before
        --since, or only one price.

    Every subcommand first removes ``GITHUB_TOKEN`` from the environment
    (``take_github_token``); ``run --mode comment``, ``collect`` and ``baseline`` keep it for
    their client.
    """
    keep_library_logs_off_stderr()
    github_token = take_github_token()
    try:
        command = _parse_args(sys.argv[1:] if argv is None else argv)
        if isinstance(
            command, _LabelCommand | _CollectCommand | _BaselineCommand | _MetricsCommand
        ):
            return _run_without_review(command, github, github_token)
        # Before --out is created: in CI, backfill touches nothing.
        if isinstance(command, _BackfillCommand):
            _refuse_backfill_in_ci()
        make_github: Callable[[str], GitHubClient] | None = None
        if isinstance(command, _RunCommand):
            make_github = _github_for(command.mode, github, github_token)
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
                shadow = _run(command, provider, make_github, audit)
        _write_report(shadow, command.out_dir)
        return _finish(shadow, command.out_dir, audit)
    except Exception as exc:  # noqa: BLE001 - `report_failure` maps any failure to its code
        exit_code = report_failure(exc)
        record_failed_end(audit, exit_code, exc)
        return exit_code


def _run_without_review(
    command: _LabelCommand | _CollectCommand | _BaselineCommand | _MetricsCommand,
    github: GitHubClient | None,
    github_token: str | None,
) -> int:
    """Run a subcommand that reviews nothing, so needs no settings, audit log or provider."""
    match command:
        case _LabelCommand():
            return _label(command)
        case _CollectCommand():
            client = _github_requiring_token(github, github_token, _COLLECT_WITHOUT_CREDENTIAL)
            return _collect(command, client)
        case _BaselineCommand():
            client = _github_requiring_token(github, github_token, _BASELINE_WITHOUT_CREDENTIAL)
            return _baseline(command, client(command.github_repo))
        case _MetricsCommand():
            return _metrics(command)


def _parse_args(
    argv: Sequence[str],
) -> (
    _ShadowCommand
    | _RunCommand
    | _BackfillCommand
    | _LabelCommand
    | _CollectCommand
    | _BaselineCommand
    | _MetricsCommand
):
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
        choices=[mode.value for mode in Mode],
        default=Mode.SHADOW.value,
        help="shadow (default) only reports; comment also posts on the pull request",
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
    collect = commands.add_parser(
        "collect",
        help="turn the reactions and resolutions on the bot's comments into labels, with no model",
        description="Write feedback.jsonl in --out: one feedback/v1 record per reaction or "
        "resolution label, read from the pull request through GitHub's API with GITHUB_TOKEN.",
        allow_abbrev=False,
    )
    collect.add_argument(
        "--event", type=Path, required=True, help="the pull_request event file (GITHUB_EVENT_PATH)"
    )
    collect.add_argument(
        "--out", type=Path, required=True, help="the directory to write feedback.jsonl"
    )
    _add_baseline_command(commands.add_parser)
    _add_metrics_command(commands.add_parser)
    flags = vars(parser.parse_args(argv))
    name = flags.pop("command")
    if name in ("label", "collect", "baseline", "metrics"):
        return _command_without_review(name, flags)
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
    event_path, repo_dir, mode = flags.pop("event"), flags.pop("repo"), Mode(flags.pop("mode"))
    return _RunCommand(event_path, repo_dir, out_dir, mode, load_settings(flags))


def _command_without_review(
    name: str,
    # Any, as argparse's parsed values have no static type.
    flags: Mapping[str, Any],
) -> _LabelCommand | _CollectCommand | _BaselineCommand | _MetricsCommand:
    """The subcommand ``name``, one that reviews nothing, from its parsed ``flags``.

    Raises:
        ConfigError: If its flags don't fit together.
    """
    if name == "label":
        return _LabelCommand(flags["reports"], flags["labels"])
    if name == "collect":
        return _CollectCommand(flags["event"], flags["out"])
    if name == "baseline":
        return _baseline_command(flags["repo"], flags["since"], flags["until"], flags["out"])
    return _metrics_command(flags)


def _github_repo(text: str) -> str:
    if not REPO_NAME.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} isn't a repository's OWNER/NAME")
    return text


def _add_baseline_command(add_parser: Callable[..., RaisingArgumentParser]) -> None:
    baseline = add_parser(
        "baseline",
        help="record how a repository's merged pull requests went in a window, with no model",
        description="Write the baseline/v1 document in --out: the open-to-merge hours, commits "
        "after opening and escaped defects of the pull requests merged from --since to --until "
        "(UTC days, both included), read through GitHub's API with GITHUB_TOKEN.",
        allow_abbrev=False,
    )
    baseline.add_argument(
        "--repo", type=_github_repo, required=True, help="the repository, as OWNER/NAME"
    )
    baseline.add_argument(
        "--since", type=_iso_date, required=True, help="the window's first day, as YYYY-MM-DD"
    )
    baseline.add_argument(
        "--until", type=_iso_date, required=True, help="the window's last day, as YYYY-MM-DD"
    )
    baseline.add_argument(
        "--out", type=Path, required=True, help="the file to write the baseline/v1 JSON to"
    )


def _baseline_command(
    github_repo: str, since: date, until: date, out_path: Path
) -> _BaselineCommand:
    """The ``baseline`` command, once its window is known to be in order.

    Raises:
        ConfigError: If ``--until`` is before ``--since``.
    """
    _require_window_in_order(since, until)
    return _BaselineCommand(github_repo, since, until, out_path)


def _require_window_in_order(since: date, until: date) -> None:
    """Raise ``ConfigError`` if ``--until`` is before ``--since``."""
    if until < since:
        raise ConfigError(
            f"--until {until} is before --since {since}. Pass an --until on or after --since. "
            "Run with --help for usage."
        )


def _add_metrics_command(add_parser: Callable[..., RaisingArgumentParser]) -> None:
    metrics = add_parser(
        "metrics",
        help="show latency, tokens, cost, outcomes and precision from downloaded reports and "
        "labels, offline",
        description="Count the shadow reports in --reports, and precision from the feedback/v1 "
        "labels in each --labels file, for the runs whose event_at falls from --since to "
        "--until (UTC days, both included).",
        allow_abbrev=False,
    )
    metrics.add_argument(
        "--reports",
        type=Path,
        required=True,
        help="the directory holding the downloaded shadow.json files, at any depth",
    )
    metrics.add_argument(
        "--labels",
        type=Path,
        nargs="+",
        action="extend",
        default=[],
        help="feedback/v1 JSONL files: label's, and collect's feedback.jsonl",
    )
    metrics.add_argument("--since", type=_iso_date, help="the window's first day, as YYYY-MM-DD")
    metrics.add_argument("--until", type=_iso_date, help="the window's last day, as YYYY-MM-DD")
    metrics.add_argument(
        "--price-input-usd-per-mtok", type=_price, help="USD per million input tokens"
    )
    metrics.add_argument(
        "--price-output-usd-per-mtok", type=_price, help="USD per million output tokens"
    )
    metrics.add_argument(
        "--format", choices=["text", "json"], default="text", help="text (default) or json"
    )


def _metrics_command(flags: Mapping[str, Any]) -> _MetricsCommand:
    """The ``metrics`` command, once its window and prices are known to be in order.

    Raises:
        ConfigError: If ``--until`` is before ``--since``, or only one price is given.
    """
    since, until = flags["since"], flags["until"]
    if since is not None and until is not None:
        _require_window_in_order(since, until)
    return _MetricsCommand(
        reports_dir=flags["reports"],
        labels_paths=tuple(flags["labels"]),
        since=since,
        until=until,
        prices=_prices(flags["price_input_usd_per_mtok"], flags["price_output_usd_per_mtok"]),
        as_json=flags["format"] == "json",
    )


def _prices(input_price: Decimal | None, output_price: Decimal | None) -> Prices | None:
    """Both prices, or None for neither.

    Raises:
        ConfigError: Naming the missing flag if only one is given.
    """
    if input_price is None and output_price is None:
        return None
    if input_price is None or output_price is None:
        missing = "input" if input_price is None else "output"
        raise ConfigError(
            f"--price-{missing}-usd-per-mtok is missing. Pass both prices to show the cost, or "
            "neither. Run with --help for usage."
        )
    return Prices(input_price, output_price)


def _price(text: str) -> Decimal:
    # Digits only, so a sign, an exponent, nan or inf is refused.
    if not _PRICE.fullmatch(text):
        raise argparse.ArgumentTypeError(
            f"{text!r} isn't a price: pass USD per million tokens as a number of 0 or more, such "
            "as 15 or 0.25"
        )
    return Decimal(text)


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} isn't an ISO 8601 date, such as 2026-11-08"
        ) from None


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


def _metrics(command: _MetricsCommand) -> int:
    """Show the metrics of the reports and labels on stdout, as text or ``metrics/v1`` JSON.

    Each report or labels file left out, and each orphan label, gets one line on stderr.

    Raises:
        ConfigError: If ``--reports`` isn't a directory.
    """
    reports, skipped = read_reports(command.reports_dir)
    for path, problem in skipped:
        write_diagnostic(skipped_line(path, problem))
    labels: list[FeedbackRecord] = []
    for path in command.labels_paths:
        records = read_labels(path)
        if isinstance(records, str):
            write_diagnostic(skipped_line(path, records))
        else:
            labels += records
    metrics = summarise_runs(
        reports, labels, since=command.since, until=command.until, prices=command.prices
    )
    for orphan in metrics.orphans:
        write_diagnostic(
            f"review-bot pr: orphan {orphan.channel} label for {orphan.repo}#{orphan.pr}, "
            f"fingerprint {orphan.fingerprint}: no report in --reports has its finding, so no "
            "number counts it. Download that pull request's reports too."
        )
    write_output(render_metrics_json(metrics) if command.as_json else render_metrics_text(metrics))
    return 0


def _labelled_line(result: LabellingResult) -> str:
    return (
        f"review-bot pr: label: judged={result.judged} skipped={result.skipped} "
        f"remaining={result.remaining} files_skipped={result.files_skipped}"
    )


def _collect(command: _CollectCommand, make_github: Callable[[str], GitHubClient]) -> int:
    """Read the pull request's signals, and write their labels to ``feedback.jsonl`` in ``--out``.

    ``make_github`` makes the client for the pull request's repository. Nothing is written
    unless every call succeeded.

    Raises:
        EventError: If the event file is unusable.
        GitHubError: If a GitHub call fails.
        ConfigError: If ``feedback.jsonl`` can't be written.
    """
    pr = read_event(command.event_path)
    signals = make_github(pr.repo).list_signals(pr)
    records = labels_from_signals(signals, repo=pr.repo, pr=pr.number, recorded_at=utc_now())
    _write_feedback(records, command.out_dir)
    channels = Counter(record.channel for record in records)
    write_diagnostic(
        f"review-bot pr: wrote {_FEEDBACK_NAME} in {redact(str(command.out_dir))}: "
        f"reaction={channels['reaction']} resolution={channels['resolution']}"
    )
    return 0


def _write_feedback(records: Sequence[SignalFeedback], out_dir: Path) -> None:
    """Write one line per record to ``feedback.jsonl`` in ``out_dir``, creating it if missing.

    Raises:
        ConfigError: If it can't be written.
    """
    lines = "".join(render_feedback_line(record) + "\n" for record in records)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / _FEEDBACK_NAME).write_text(lines, encoding="ascii", newline="\n")
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise ConfigError(
            f"Can't write {_FEEDBACK_NAME} in --out {redact(str(out_dir))}: {reason}. Pass a "
            "directory you can write to."
        ) from exc


def _baseline(command: _BaselineCommand, github: GitHubClient) -> int:
    """Read the window's merged pull requests and bug issues, write ``--out``, show the numbers.

    Nothing is written unless every call succeeded, and no pull request's title is written.

    Raises:
        GitHubError: If a GitHub call fails, or the window holds too many merged pull requests.
        ConfigError: If ``--out`` can't be written.
    """
    repo, since, until = command.github_repo, command.since, command.until
    pulls = github.list_merged_pull_requests(repo, since, until)
    bug_issues = github.count_issues(repo, _BUG_LABEL, since, until)
    baseline = summarise_baseline(
        pulls, repo=repo, since=since, until=until, bug_issues=bug_issues, captured_at=utc_now()
    )
    _write_baseline(render_baseline_json(baseline), command.out_path)
    write_output(render_baseline_text(baseline))
    return 0


def _write_baseline(document: str, out_path: Path) -> None:
    """Write ``document`` to ``out_path`` whole or not at all, creating its directory if missing.

    It goes to ``<out_path>.tmp`` first, then replaces ``out_path``, so a failure leaves any
    earlier file as it was.

    Raises:
        ConfigError: If it can't be written.
    """
    staged = out_path.with_name(f"{out_path.name}.tmp")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(document + "\n", encoding="ascii", newline="\n")
        staged.replace(out_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            staged.unlink(missing_ok=True)
        reason = exc.strerror or type(exc).__name__
        raise ConfigError(
            f"Can't write the baseline to --out {redact(str(out_path))}: {reason}. Pass a file "
            "you can write to."
        ) from exc


def _refuse_backfill_in_ci() -> None:
    """Raise ``ConfigError`` if this process runs in CI, where backfill must not run."""
    if in_ci(os.environ):
        raise ConfigError(_BACKFILL_IN_CI)


def _github_for(
    mode: Mode, github: GitHubClient | None, github_token: str | None
) -> Callable[[str], GitHubClient] | None:
    """What makes a run's GitHub client for its repository: none in shadow mode.

    In comment mode it is ``github`` if given, else a ``RestGitHubClient`` with
    ``github_token``, the one ``main`` took from the environment, and ``GITHUB_API_URL``,
    checked now, before anything runs.

    Raises:
        ConfigError: If ``GITHUB_TOKEN`` or ``GITHUB_API_URL`` is unusable.
    """
    if mode is Mode.SHADOW:
        return None
    return _github_client(github, github_token)


def _github_requiring_token(
    github: GitHubClient | None, github_token: str | None, missing: str
) -> Callable[[str], GitHubClient]:
    """What makes a read-only command's GitHub client, as ``_github_for`` does in comment mode.

    Raises:
        ConfigError: Saying ``missing`` if there is neither ``github`` nor a token; or if
            ``GITHUB_TOKEN`` or ``GITHUB_API_URL`` is unusable.
    """
    if github is None and not github_token:
        raise ConfigError(missing)
    return _github_client(github, github_token)


def _github_client(
    github: GitHubClient | None, github_token: str | None
) -> Callable[[str], GitHubClient]:
    """``github`` if given, else a ``RestGitHubClient`` with the token and ``GITHUB_API_URL``.

    Raises:
        ConfigError: If ``GITHUB_TOKEN`` or ``GITHUB_API_URL`` is unusable.
    """
    if github is not None:
        return lambda _repo: github
    access = github_access(github_token, os.environ.get(API_URL_VARIABLE))
    return lambda repo: RestGitHubClient(repo, access)


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
        mode=Mode.SHADOW,
        existing=(),
        settings=command.settings,
        # A local diff has no repository to read a config from.
        config=DEFAULT_REPO_CONFIG,
        run_id=audit.run_id,
        started_at=started_at,
        finished_at=utc_now(),
        origin=Origin.LOCAL,
        pr=None,
    )


def _run(
    command: _RunCommand,
    provider: LLMProvider | None,
    make_github: Callable[[str], GitHubClient] | None,
    audit: AuditLog,
) -> ShadowReport:
    """Review the pull request in the event file, and in comment mode apply it on GitHub.

    ``make_github`` makes the client for the pull request's repository; it is None in shadow
    mode.

    Raises:
        EventError: If the event file is unusable; nothing else runs.
        GitError: If a commit is missing or git fails; no provider is called.
        GitHubError: If the bot's summary or comments can't be read; nothing is posted.
    """
    pr = read_event(command.event_path)
    settings = command.settings
    run = run_pull_request(
        pr,
        mode=command.mode,
        git=GitRepo(command.repo_dir, timeout_s=GIT_TIMEOUT_S),
        make_provider=lambda: build_provider(settings) if provider is None else provider,
        github=None if make_github is None else make_github(pr.repo),
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
    git = GitRepo(command.repo_dir, timeout_s=GIT_TIMEOUT_S)
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
    if isinstance(error, ProviderError | GitHubError | RepoConfigError):
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
