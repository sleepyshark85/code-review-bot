"""The Markdown the bot would post: each comment's body and marker, and the summary comment.

Model and diff text reaches these bodies only through ``render.escape_markdown`` and
``render.markdown_path``, so it can't mention, link, reference or hide anything (Phase 2 design,
Safety on the PR path). ``read_marker`` and ``is_summary`` read posted bodies back.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from review_bot.errors import ErrorCategory
from review_bot.findings import Category, RuleId, Severity
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.plan import CommentPlan, PlannedComment
from review_bot.render import escape_markdown, markdown_path
from review_bot.secret_scan import SecretHit

__all__ = [
    "Marker",
    "blocked_summary",
    "comment_body",
    "is_summary",
    "not_reviewed_summary",
    "read_marker",
    "reviewed_summary",
    "with_summary_marker",
]

_MAX_RATIONALE_CHARS: Final = 2_000
# How many withheld findings or secrets a summary names.
_MAX_LISTED: Final = 20
# Bounds each listed path, so a summary stays under the design's 60,000 characters: escaped and
# fenced, 100 characters take at most about 1,000, and a summary names at most 20 paths.
_MAX_LISTED_PATH_CHARS: Final = 100
_HEADLINE: Final = "**Review bot:**"
_REACTION_REQUEST: Final = (
    "Is this right and worth acting on? React with a thumbs-up if so, or a thumbs-down if not."
)
_NEW_PARAGRAPH: Final = "\n\n"
# What ``comment_body`` writes on its first and last lines, read back exactly.
_SEVERITY_HEADING: Final = re.compile(rf"\*\*({'|'.join(Severity)})\*\* ")
_MARKER: Final = re.compile(
    r"<!-- review-bot:v1 fingerprint=([0-9a-f]{16}) rule=(\S+) anchor=([0-9a-f]{16}) -->"
)
# The summary's last line (ADR 0009). P2-S7 records the commit reviewed in it; until then it says
# ``none``, which isn't a SHA, so no run takes it for one.
_SUMMARY_MARKER: Final = "<!-- review-bot:v1 summary reviewed=none -->"
_ANY_SUMMARY_MARKER: Final = re.compile(r"<!-- review-bot:v1 summary reviewed=\S+ -->")
_RULE_PARTS: Final = frozenset((*RuleId, *(f"ai:{category}" for category in Category)))


@dataclass(frozen=True, slots=True)
class Marker:
    """What a posted comment's body says about its finding: its marker and its severity."""

    fingerprint: str
    rule_part: str
    anchor: str
    severity: Severity


def comment_body(comment: PlannedComment) -> str:
    """Return the comment's body; its last line is its marker (ADR 0009).

    The body shows the finding's severity, category, source, rule if any, confidence and
    rationale, then asks for a reaction. A rationale over 2,000 characters shows its first 2,000
    and ``(cut)``.
    """
    reviewed = comment.reviewed
    finding = reviewed.finding
    rule = "" if reviewed.rule is None else f", rule {reviewed.rule}"
    heading = (
        f"**{finding.severity}** {finding.category} (source {reviewed.source}{rule}, "
        f"confidence {finding.confidence:.2f})"
    )
    marker = (
        f"<!-- review-bot:v1 fingerprint={reviewed.fingerprint} rule={reviewed.rule_part} "
        f"anchor={comment.anchor} -->"
    )
    paragraphs = (heading, _shown_rationale(finding.rationale), _REACTION_REQUEST, marker)
    return _NEW_PARAGRAPH.join(paragraphs)


def read_marker(body: str) -> Marker | None:
    """Read back a body ``comment_body`` wrote, or return None if it isn't one.

    The last line must be exactly a marker with a known rule part, and the first line must start
    with the severity heading. Call it only on the bot's own comments, so nobody else's text is
    parsed (ADR 0009).
    """
    lines = body.split("\n")
    marker = _MARKER.fullmatch(lines[-1])
    heading = _SEVERITY_HEADING.match(lines[0])
    if marker is None or heading is None:
        return None
    fingerprint, rule_part, anchor = marker.groups()
    if rule_part not in _RULE_PARTS:
        return None
    return Marker(fingerprint, rule_part, anchor, Severity(heading.group(1)))


def with_summary_marker(body: str) -> str:
    """Return the summary ``body`` as posted: its last line is the summary marker."""
    return f"{body}{_NEW_PARAGRAPH}{_SUMMARY_MARKER}"


def is_summary(body: str) -> bool:
    """Say whether ``body``'s last line is exactly a summary marker.

    Call it only on the bot's own comments, as ``read_marker``.
    """
    return _ANY_SUMMARY_MARKER.fullmatch(body.rsplit("\n", maxsplit=1)[-1]) is not None


def _shown_rationale(rationale: str) -> str:
    if len(rationale) <= _MAX_RATIONALE_CHARS:
        return escape_markdown(rationale)
    return f"{escape_markdown(rationale[:_MAX_RATIONALE_CHARS])} (cut)"


def reviewed_summary(plan: CommentPlan, *, threshold: Severity) -> str:
    """Return the summary of a review: its counts, then up to 20 withheld findings.

    With nothing planned or withheld, it says there are no findings at or above ``threshold``.
    """
    headline = f"{_HEADLINE} `{Outcome.REVIEWED}`."
    if not plan.comments and not plan.withheld:
        return f"{headline} No findings at or above {threshold}."
    counts = f"{headline} {len(plan.comments)} comments, {len(plan.withheld)} withheld."
    if not plan.withheld:
        return counts
    listed = [
        f"{_location(item.reviewed.finding.file, item.reviewed.finding.line)}: `{item.reason}`"
        for item in plan.withheld[:_MAX_LISTED]
    ]
    return _NEW_PARAGRAPH.join(
        (counts, "Withheld findings:", _listed(listed, total=len(plan.withheld)))
    )


def _location(path: str, line: int) -> str:
    """The path and line as a code span of bounded size, in git's quoted form if not UTF-8."""
    if len(path) <= _MAX_LISTED_PATH_CHARS:
        return markdown_path(path, suffix=f":{line}")
    return f"{markdown_path(path[:_MAX_LISTED_PATH_CHARS], suffix=f':{line}')} (path cut)"


def blocked_summary(hits: Sequence[SecretHit]) -> str:
    """Return the summary of a run stopped by possible secrets: their kinds and diff lines only."""
    listed = [f"{hit.kind} at diff line {hit.diff_line}" for hit in hits[:_MAX_LISTED]]
    return _NEW_PARAGRAPH.join(
        (
            f"{_HEADLINE} `{Outcome.BLOCKED_SECRET}`. The diff holds possible secrets, so it "
            "wasn't sent to the model:",
            _listed(listed, total=len(hits)),
            "Remove them, and rotate any that are real.",
        )
    )


def not_reviewed_summary(outcome: Outcome, reason: Reason | ErrorCategory) -> str:
    """Return the summary of a skipped or failed run, which has no findings."""
    if outcome is Outcome.FAILED:
        detail = "The review is unavailable: it didn't complete, so there are no findings."
    else:
        detail = "Nothing was sent to the model."
    return f"{_HEADLINE} `{outcome}` (`{reason}`). {detail}"


def _listed(items: Sequence[str], *, total: int) -> str:
    """``items`` as a Markdown list, then ``and K more`` for the rest of ``total``."""
    lines = [f"- {item}" for item in items]
    if total > len(items):
        lines.append(f"- and {total - len(items)} more")
    return "\n".join(lines)
