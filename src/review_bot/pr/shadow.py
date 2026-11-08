"""The ``shadow/v1`` report of what the bot would post, and its Markdown (ADR 0010).

Pure: the command line runs the review and passes its result, or the error that stopped it, to
``build_shadow_report``.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, assert_never

from review_bot.errors import (
    DiffEmptyError,
    DiffNotParsableError,
    DiffNotUtf8Error,
    DiffTooLargeError,
    ErrorCategory,
    ProviderError,
    SecretDetectedError,
)
from review_bot.pr.comments import (
    blocked_summary,
    comment_body,
    not_reviewed_summary,
    reviewed_summary,
)
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.plan import (
    DEFAULT_MAX_OPEN_COMMENTS,
    PlannedComment,
    WithheldFinding,
    plan_comments,
)
from review_bot.render import markdown_path, report_document
from review_bot.report import PipelineResult, path_fields
from review_bot.secret_scan import redact
from review_bot.settings import Settings

__all__ = [
    "REPORTED_ERRORS",
    "ReportedError",
    "ShadowComment",
    "ShadowReport",
    "build_shadow_report",
    "render_shadow_json",
    "render_shadow_markdown",
]

_SCHEMA: Final = "shadow/v1"
# A local run has no event and no pull request, and it never posts.
_MODE: Final = "shadow"
_ORIGIN: Final = "local"

# The errors that still end in a report, as ``skipped``, ``blocked_secret`` or ``failed``. The
# tuple is for ``except``, the alias for types; a unit test keeps them equal.
REPORTED_ERRORS: Final = (
    DiffTooLargeError,
    DiffEmptyError,
    DiffNotParsableError,
    DiffNotUtf8Error,
    SecretDetectedError,
    ProviderError,
)
type ReportedError = (
    DiffTooLargeError
    | DiffEmptyError
    | DiffNotParsableError
    | DiffNotUtf8Error
    | SecretDetectedError
    | ProviderError
)

type _Reason = Reason | ErrorCategory | None


@dataclass(frozen=True, slots=True)
class ShadowComment:
    """A planned comment and the body it would be posted with."""

    planned: PlannedComment
    body: str


@dataclass(frozen=True, slots=True)
class ShadowReport:
    """One run's ``shadow/v1`` report, before it is written.

    ``report`` is the ``report/v1`` object, with any secret in it redacted, or None if nothing
    was reviewed. ``comments`` and ``withheld`` are in comment order. ``caps`` are the run's
    effective limits.
    """

    run_id: str
    outcome: Outcome
    reason: _Reason
    started_at: datetime
    finished_at: datetime
    report: Mapping[str, object] | None
    summary_body: str
    comments: tuple[ShadowComment, ...]
    withheld: tuple[WithheldFinding, ...]
    caps: Mapping[str, int | float]


def build_shadow_report(
    review: PipelineResult | ReportedError,
    *,
    settings: Settings,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> ShadowReport:
    """Build the report of a review, or of the error that stopped it.

    A review that called the model is ``reviewed``, with its reported findings planned as
    comments against the reviewed diff. A review that skipped every file is ``skipped``,
    ``nothing_to_review``; a secret is ``blocked_secret``; a diff that can't be reviewed is
    ``skipped`` with its reason; and a provider error is ``failed`` with its category.
    """
    comments: tuple[ShadowComment, ...] = ()
    withheld: tuple[WithheldFinding, ...] = ()
    report: dict[str, object] | None = None
    reason: _Reason
    if isinstance(review, PipelineResult) and review.model_called:
        plan = plan_comments(
            review.decision.reported,
            pr_diff=review.parsed,
            max_open_comments=DEFAULT_MAX_OPEN_COMMENTS,
        )
        comments = tuple(ShadowComment(planned, comment_body(planned)) for planned in plan.comments)
        withheld = plan.withheld
        # A rationale can quote a secret that withholds its comment; no file may hold it.
        document = report_document(review, settings, run_id)
        report = {key: _redacted(value) for key, value in document.items()}
        outcome, reason = Outcome.REVIEWED, None
        summary = reviewed_summary(plan, threshold=settings.threshold)
    elif isinstance(review, PipelineResult):
        outcome, reason, summary = _skipped(Reason.NOTHING_TO_REVIEW)
    else:
        outcome, reason, summary = _not_reviewed(review)
    return ShadowReport(
        run_id=run_id,
        outcome=outcome,
        reason=reason,
        started_at=started_at,
        finished_at=finished_at,
        report=report,
        summary_body=summary,
        comments=comments,
        withheld=withheld,
        caps={
            "max_diff_bytes": settings.max_diff_bytes,
            "max_retries": settings.max_retries,
            "deadline_s": settings.deadline_s,
        },
    )


def _not_reviewed(error: ReportedError) -> tuple[Outcome, _Reason, str]:
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

    A local run's ``event_at`` is its ``started_at``, and it has no usage cap to exceed.
    """
    started_at = _utc(shadow.started_at)
    document = {
        "schema": _SCHEMA,
        "run_id": shadow.run_id,
        "mode": _MODE,
        "origin": _ORIGIN,
        "outcome": shadow.outcome,
        "reason": shadow.reason,
        "pr": None,
        "event_at": started_at,
        "started_at": started_at,
        "finished_at": _utc(shadow.finished_at),
        "report": shadow.report,
        "summary_body": shadow.summary_body,
        "comments": [_comment_json(comment) for comment in shadow.comments],
        "withheld": [_withheld_json(item) for item in shadow.withheld],
        "caps": shadow.caps,
        "over_budget": False,
    }
    return json.dumps(document, ensure_ascii=True, allow_nan=False)


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _comment_json(comment: ShadowComment) -> dict[str, object]:
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
    }


def _withheld_json(item: WithheldFinding) -> dict[str, object]:
    finding = item.reviewed.finding
    return {
        "fingerprint": item.reviewed.fingerprint,
        # Git's quoted form for a path that isn't UTF-8, as the summary lists it.
        "file": path_fields(finding.file)["file"],
        "line": finding.line,
        "reason": item.reason,
    }


def render_shadow_markdown(shadow: ShadowReport) -> str:
    """Render the summary body, then each planned comment as a list item under its location."""
    items = [_markdown_comment(comment) for comment in shadow.comments]
    return "\n\n".join((shadow.summary_body, *items))


def _markdown_comment(comment: ShadowComment) -> str:
    finding = comment.planned.reviewed.finding
    # Indented two spaces, so every paragraph of the body stays inside the list item.
    body = "\n".join(f"  {line}" if line else "" for line in comment.body.split("\n"))
    return f"- {markdown_path(finding.file, suffix=f':{finding.line}')}\n\n{body}"
