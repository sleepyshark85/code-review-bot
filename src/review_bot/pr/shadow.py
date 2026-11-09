"""The ``shadow/v1`` report of what the bot would post, and its Markdown (ADR 0010).

Pure: the caller runs the review and passes its result, the error that stopped it, or the
reason it was refused or had nothing new to review, to ``build_shadow_report``.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, assert_never

from review_bot.context.codeowners import NO_CODEOWNERS, CodeOwners, owners_for
from review_bot.diff_parser import ParsedDiff
from review_bot.errors import (
    DiffEmptyError,
    DiffNotParsableError,
    DiffNotUtf8Error,
    DiffTooLargeError,
    ErrorCategory,
    GitHubError,
    ProviderError,
    RepoConfigError,
    SecretDetectedError,
)
from review_bot.findings import ReviewedFinding
from review_bot.pr.comments import (
    blocked_summary,
    comment_body,
    invalid_config_summary,
    no_new_changes_summary,
    not_reviewed_summary,
    reviewed_summary,
)
from review_bot.pr.config import RepoConfig
from review_bot.pr.event import PullRequestRef
from review_bot.pr.github import BotComment
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.plan import (
    Action,
    CommentPlan,
    PlannedComment,
    WithheldFinding,
    plan_comments,
)
from review_bot.providers.base import TokenUsage
from review_bot.render import (
    evidence_json,
    indented_in_list_item,
    markdown_path,
    report_document,
)
from review_bot.report import PipelineResult, path_fields
from review_bot.secret_scan import redact
from review_bot.settings import Settings

__all__ = [
    "REPORTED_ERRORS",
    "Applied",
    "Incremental",
    "Mode",
    "Origin",
    "ReportedError",
    "Scope",
    "ShadowComment",
    "ShadowReport",
    "ShadowResolve",
    "build_shadow_report",
    "render_shadow_json",
    "render_shadow_markdown",
]

_SCHEMA: Final = "shadow/v1"

# The errors that still end in a report, as ``skipped``, ``blocked_secret`` or ``failed``. The
# tuple is for ``except``, the alias for types; a unit test keeps them equal.
REPORTED_ERRORS: Final = (
    DiffTooLargeError,
    DiffEmptyError,
    DiffNotParsableError,
    DiffNotUtf8Error,
    SecretDetectedError,
    ProviderError,
    RepoConfigError,
)
type ReportedError = (
    DiffTooLargeError
    | DiffEmptyError
    | DiffNotParsableError
    | DiffNotUtf8Error
    | SecretDetectedError
    | ProviderError
    | RepoConfigError
)

type _Reason = Reason | ErrorCategory | None


class Origin(StrEnum):
    """What started a run. Only a ``local`` run, from a diff file, has no pull request."""

    LOCAL = "local"
    EVENT = "event"
    BACKFILL = "backfill"


class Mode(StrEnum):
    """Whether a run only reports what it would post, or also posts it on the pull request."""

    SHADOW = "shadow"
    COMMENT = "comment"


class Scope(StrEnum):
    """Whether a run reviewed the whole pull request, or only what changed since a commit."""

    FULL = "full"
    INCREMENTAL = "incremental"


@dataclass(frozen=True, slots=True)
class Incremental:
    """An incremental run's diffs, which its comments are planned against.

    ``pr_diff`` is the whole PR diff, though only its hunks changed since the last reviewed
    commit were reviewed; ``changed`` is the diff from that commit to the head.
    """

    pr_diff: ParsedDiff
    changed: ParsedDiff


class Applied(StrEnum):
    """Whether a planned change reached the pull request. Nothing is applied in shadow mode."""

    DONE = "done"
    FAILED = "failed"
    NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True, slots=True)
class ShadowComment:
    """A planned comment, the body it would be posted with, and whether that was applied."""

    planned: PlannedComment
    body: str
    applied: Applied = Applied.NOT_ATTEMPTED


@dataclass(frozen=True, slots=True)
class ShadowResolve:
    """One of the bot's comments to resolve, and whether that was applied."""

    comment: BotComment
    applied: Applied = Applied.NOT_ATTEMPTED


@dataclass(frozen=True, slots=True)
class ShadowReport:
    """One run's ``shadow/v1`` report, before it is written.

    ``pr`` is None exactly when ``origin`` is ``local``. ``report`` is the ``report/v1`` object,
    with any secret in it redacted, or None if nothing was reviewed. ``comments`` and
    ``withheld`` are in comment order, and ``resolves`` oldest first. ``caps`` are the run's
    effective limits, None where there is no cap. ``config`` is the review policy the run used,
    or None if it read none: it was refused, or its repository config was invalid.
    ``codeowners`` is the base commit's CODEOWNERS, which names each entry's owners, or None if
    the run read none: it planned no comments, or ran on a local diff. ``since`` is the last
    reviewed commit a comment mode run started from, the head or an ancestor of it, else None.
    ``error`` is the error that ended the run, if any, for the caller's message and exit code;
    it is never written.
    """

    run_id: str
    mode: Mode
    origin: Origin
    pr: PullRequestRef | None
    outcome: Outcome
    reason: _Reason
    started_at: datetime
    finished_at: datetime
    report: Mapping[str, object] | None
    summary_body: str
    comments: tuple[ShadowComment, ...]
    resolves: tuple[ShadowResolve, ...]
    withheld: tuple[WithheldFinding, ...]
    caps: Mapping[str, int | float | None]
    config: Mapping[str, object] | None
    codeowners: CodeOwners | None
    over_budget: bool
    since: str | None
    error: ReportedError | GitHubError | None

    @property
    def scope(self) -> Scope:
        """``incremental`` if the run started from a reviewed commit, else ``full``."""
        return Scope.FULL if self.since is None else Scope.INCREMENTAL


def build_shadow_report(  # noqa: PLR0913 - keyword-only, each a separate input of the report
    review: PipelineResult | ReportedError | Reason,
    *,
    mode: Mode,
    existing: Sequence[BotComment],
    settings: Settings,
    config: RepoConfig | None,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    origin: Origin,
    pr: PullRequestRef | None,
    since: str | None = None,
    incremental: Incremental | None = None,
    codeowners: CodeOwners | None = None,
) -> ShadowReport:
    """Build the report of a review, of the error that stopped it, or of a refused run.

    A review that called the model is ``reviewed``, with its reported findings planned as
    comments against the reviewed diff and the bot's ``existing`` comments, which a shadow mode
    run never has. Nothing is applied yet. A comment mode run that started from the last
    reviewed commit ``since`` has its ``incremental`` diffs once git produced them, and its
    comments are planned against those instead. With nothing new since that commit
    (``no_new_changes``), it is ``reviewed`` with no findings, so it only resolves comments
    whose anchored lines are gone. A comment mode summary also counts the bot's comments open
    once the plan is applied (``reviewed_summary``). A review that skipped every file is
    ``skipped``, ``nothing_to_review``; a refused run is ``skipped`` with its reason; a secret
    is ``blocked_secret``; a diff that can't be reviewed is ``skipped`` with its reason; an
    invalid repository config is ``failed``, ``invalid_config``; and a provider error is
    ``failed`` with its category. A review is over budget when it reports more output tokens
    than ``max_output_tokens``.

    ``settings`` are those the review ran with, the repository's threshold applied, and
    ``config`` the repository's policy, or None if the run read none. Comments are planned up
    to its ``max_open_comments``. ``codeowners``, if the run read it, names the owners of each
    entry, and a reviewed summary groups the findings by owner.

    Raises:
        ValueError: If ``pr`` is None for an ``origin`` other than ``local``, or given for it;
            if ``existing`` comments or ``since`` are given in shadow mode; if ``incremental``
            is given without ``since``; if ``no_new_changes`` comes without ``incremental``;
            or if comments are to be planned with no ``config``.
    """
    if (pr is None) != (origin is Origin.LOCAL):
        raise ValueError("A shadow report has a pull request exactly when its origin isn't local.")
    if mode is Mode.SHADOW and (existing or since is not None):
        raise ValueError("A shadow mode run reads no comments or summary from the pull request.")
    if incremental is not None and since is None:
        raise ValueError("An incremental run's diffs come with the commit it started from.")
    comments: tuple[ShadowComment, ...] = ()
    resolves: tuple[ShadowResolve, ...] = ()
    withheld: tuple[WithheldFinding, ...] = ()
    report: dict[str, object] | None = None
    error: ReportedError | None = None
    reason: _Reason
    if review is Reason.NO_NEW_CHANGES:
        if incremental is None or since is None:
            raise ValueError("Only an incremental run can have no new changes.")
        plan = _plan((), existing, incremental.pr_diff, incremental, config)
        comments, resolves, withheld = _planned(plan)
        outcome, reason = Outcome.REVIEWED, review
        summary = no_new_changes_summary(since=since, open_comments=plan.open_after)
    elif isinstance(review, Reason):
        outcome, reason, summary = _skipped(review)
    elif isinstance(review, PipelineResult) and review.model_called:
        plan = _plan(review.decision.reported, existing, review.parsed, incremental, config)
        comments, resolves, withheld = _planned(plan)
        # A rationale can quote a secret that withholds its comment; no file may hold it.
        document = report_document(review, settings, run_id)
        report = {key: _redacted(value) for key, value in document.items()}
        outcome, reason = Outcome.REVIEWED, None
        summary = reviewed_summary(
            plan,
            threshold=settings.threshold,
            since=since,
            # Shadow mode posts nothing, so no comment of the bot's is open.
            open_comments=0 if mode is Mode.SHADOW else plan.open_after,
            codeowners=codeowners or NO_CODEOWNERS,
        )
    elif isinstance(review, PipelineResult):
        outcome, reason, summary = _skipped(Reason.NOTHING_TO_REVIEW)
    else:
        error = review
        outcome, reason, summary = _not_reviewed(review)
    return ShadowReport(
        run_id=run_id,
        mode=mode,
        origin=origin,
        pr=pr,
        outcome=outcome,
        reason=reason,
        started_at=started_at,
        finished_at=finished_at,
        report=report,
        summary_body=summary,
        comments=comments,
        resolves=resolves,
        withheld=withheld,
        caps={
            "max_diff_bytes": settings.max_diff_bytes,
            "max_review_bytes": settings.max_review_bytes,
            "max_output_tokens": settings.max_output_tokens,
            "max_retries": settings.max_retries,
            "deadline_s": settings.deadline_s,
        },
        config=None if config is None else _config_json(config, settings),
        codeowners=codeowners,
        over_budget=isinstance(review, PipelineResult) and _is_over_budget(review.usage, settings),
        since=since,
        error=error,
    )


def _plan(
    findings: Sequence[ReviewedFinding],
    existing: Sequence[BotComment],
    reviewed_diff: ParsedDiff,
    incremental: Incremental | None,
    config: RepoConfig | None,
) -> CommentPlan:
    """Plan against the reviewed diff, or an incremental run's whole PR diff and its changes.

    Raises:
        ValueError: If ``config`` is None, so the comment limit is unknown.
    """
    if config is None:
        raise ValueError("Comments are planned under the repository config the run read.")
    return plan_comments(
        findings,
        existing,
        pr_diff=reviewed_diff if incremental is None else incremental.pr_diff,
        changed=None if incremental is None else incremental.changed,
        max_open_comments=config.max_open_comments,
    )


def _config_json(config: RepoConfig, settings: Settings) -> dict[str, object]:
    """The policy as the report shows it, with the threshold the review used."""
    return {
        "source": config.source,
        "threshold": settings.threshold,
        "exclude": list(config.exclude),
        "max_open_comments": config.max_open_comments,
        "domain_rules": [
            {"rule": rule.rule, "paths": list(rule.paths), "severity": rule.severity}
            for rule in config.domain_rules
        ],
    }


def _planned(
    plan: CommentPlan,
) -> tuple[tuple[ShadowComment, ...], tuple[ShadowResolve, ...], tuple[WithheldFinding, ...]]:
    """The plan's comments with their bodies, its resolves and its withheld findings."""
    comments = tuple(ShadowComment(planned, comment_body(planned)) for planned in plan.comments)
    resolves = tuple(ShadowResolve(comment) for comment in plan.resolved)
    return comments, resolves, plan.withheld


def _is_over_budget(usage: TokenUsage, settings: Settings) -> bool:
    cap = settings.max_output_tokens
    return cap is not None and usage.output_tokens > cap


def _not_reviewed(error: ReportedError) -> tuple[Outcome, _Reason, str]:  # noqa: PLR0911 - one return per error type
    """The outcome, reason and summary of a run that an error stopped before any review."""
    match error:
        case SecretDetectedError():
            return Outcome.BLOCKED_SECRET, None, blocked_summary(error.hits)
        case ProviderError():
            reason = error.category
            return Outcome.FAILED, reason, not_reviewed_summary(Outcome.FAILED, reason)
        case DiffTooLargeError():
            return _skipped(Reason.TOO_LARGE)
        case DiffEmptyError():
            return _skipped(Reason.EMPTY)
        case DiffNotParsableError():
            return _skipped(Reason.NOT_A_DIFF)
        case DiffNotUtf8Error():
            return _skipped(Reason.UNREADABLE)
        case RepoConfigError():
            summary = invalid_config_summary(redact(str(error)))
            return Outcome.FAILED, Reason.INVALID_CONFIG, summary
        case _:
            assert_never(error)


def _skipped(reason: Reason) -> tuple[Outcome, Reason, str]:
    return Outcome.SKIPPED, reason, not_reviewed_summary(Outcome.SKIPPED, reason)


def _redacted(value: object) -> object:
    """``value`` with ``redact`` applied to every string in it, at any depth."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {key: _redacted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


def render_shadow_json(shadow: ShadowReport) -> str:
    """Render the report as one line of pure ASCII JSON, schema ``shadow/v1``, keys in order.

    A run with a pull request has the ``event_at`` it holds: its ``updated_at`` from an event,
    or its ``mergedAt`` when backfilled. A local run's ``event_at`` is its ``started_at``.
    """
    pr = shadow.pr
    codeowners = shadow.codeowners or NO_CODEOWNERS
    document = {
        "schema": _SCHEMA,
        "run_id": shadow.run_id,
        "mode": shadow.mode,
        "origin": shadow.origin,
        "outcome": shadow.outcome,
        "reason": shadow.reason,
        "pr": None if pr is None else _pr_json(pr),
        "event_at": _utc(shadow.started_at if pr is None else pr.event_at),
        "started_at": _utc(shadow.started_at),
        "finished_at": _utc(shadow.finished_at),
        "report": shadow.report,
        "summary_body": shadow.summary_body,
        "comments": [
            *(_comment_json(comment, codeowners) for comment in shadow.comments),
            *(_resolve_json(resolve, codeowners) for resolve in shadow.resolves),
        ],
        "withheld": [_withheld_json(item, codeowners) for item in shadow.withheld],
        "caps": shadow.caps,
        "over_budget": shadow.over_budget,
        "scope": shadow.scope,
        "since": shadow.since,
        "config": shadow.config,
        "codeowners": _codeowners_json(shadow.codeowners),
    }
    return json.dumps(document, ensure_ascii=True, allow_nan=False)


def _pr_json(pr: PullRequestRef) -> dict[str, object]:
    return {
        "repo": pr.repo,
        "number": pr.number,
        "base_sha": pr.base_sha,
        "head_sha": pr.head_sha,
    }


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _codeowners_json(codeowners: CodeOwners | None) -> dict[str, object] | None:
    if codeowners is None:
        return None
    return {
        "source": codeowners.source,
        "status": codeowners.status,
        "rules": len(codeowners.rules),
        "invalid_lines": codeowners.invalid_lines,
    }


def _comment_json(comment: ShadowComment, codeowners: CodeOwners) -> dict[str, object]:
    # The plan never holds a path that isn't UTF-8, so ``file`` is the path as it is.
    planned = comment.planned
    reviewed = planned.reviewed
    return {
        "action": planned.action,
        "fingerprint": reviewed.fingerprint,
        "rule_part": reviewed.rule_part,
        "anchor": planned.anchor,
        "source": reviewed.source,
        "severity": reviewed.finding.severity,
        "file": reviewed.finding.file,
        "line": reviewed.finding.line,
        "body": comment.body,
        "applied": comment.applied,
        "owners": list(owners_for(codeowners, reviewed.finding.file)),
        "evidence": evidence_json(reviewed.finding.evidence),
        "evidence_dropped": reviewed.evidence_dropped,
        "suggestion": reviewed.finding.suggestion,
    }


def _resolve_json(resolve: ShadowResolve, codeowners: CodeOwners) -> dict[str, object]:
    # The bot's comment, read back from the pull request, holds no source, body, evidence or
    # suggestion.
    comment = resolve.comment
    return {
        "action": Action.RESOLVE,
        "fingerprint": comment.fingerprint,
        "rule_part": comment.rule_part,
        "anchor": comment.anchor,
        "source": None,
        "severity": comment.severity,
        "file": comment.path,
        "line": comment.line,
        "body": None,
        "applied": resolve.applied,
        "owners": list(owners_for(codeowners, comment.path)),
        "evidence": None,
        "evidence_dropped": None,
        "suggestion": None,
    }


def _withheld_json(item: WithheldFinding, codeowners: CodeOwners) -> dict[str, object]:
    finding = item.reviewed.finding
    return {
        "fingerprint": item.reviewed.fingerprint,
        # Git's quoted form for a path that isn't UTF-8, as the summary lists it.
        "file": path_fields(finding.file)["file"],
        "line": finding.line,
        "reason": item.reason,
        "owners": list(owners_for(codeowners, finding.file)),
    }


def render_shadow_markdown(shadow: ShadowReport) -> str:
    """Render the summary body, then each planned comment as a list item under its location."""
    items = [_markdown_comment(comment) for comment in shadow.comments]
    return "\n\n".join((shadow.summary_body, *items))


def _markdown_comment(comment: ShadowComment) -> str:
    finding = comment.planned.reviewed.finding
    location = markdown_path(finding.file, suffix=f":{finding.line}")
    return f"- {location}\n\n{indented_in_list_item(comment.body)}"
