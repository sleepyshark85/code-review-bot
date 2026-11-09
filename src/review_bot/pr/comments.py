"""The Markdown the bot would post: each comment's body and marker, and the summary comment.

Model and diff text reaches these bodies only through ``render.escape_markdown`` and
``render.markdown_path``, so it can't mention, link, reference or hide anything (Phase 2 design,
Safety on the PR path). A code owner from CODEOWNERS goes in a code span, which its syntax
keeps whole. ``read_marker``, ``is_summary`` and ``read_reviewed_sha`` read posted
bodies back.
"""

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from review_bot.context.codeowners import NO_CODEOWNERS, CodeOwners, owners_for
from review_bot.errors import ErrorCategory
from review_bot.findings import Category, EvidenceSpan, RuleId, Severity
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.plan import CommentPlan, PlannedComment
from review_bot.render import escape_markdown, markdown_path
from review_bot.secret_scan import SecretHit

__all__ = [
    "Marker",
    "blocked_summary",
    "comment_body",
    "invalid_config_summary",
    "is_summary",
    "no_new_changes_summary",
    "not_reviewed_summary",
    "read_marker",
    "read_reviewed_sha",
    "reviewed_summary",
    "with_summary_marker",
]

_MAX_RATIONALE_CHARS: Final = 2_000
# How many withheld findings or secrets a summary names.
_MAX_LISTED: Final = 20
# How many owners a summary names.
_MAX_LISTED_OWNERS: Final = 10
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
# The summary's last line records the last commit reviewed, or ``none`` (ADR 0009). Any value
# marks the bot's summary, so one written before P2-S7, or edited by hand, is still found.
_NO_REVIEWED_SHA: Final = "none"
_ANY_SUMMARY_MARKER: Final = re.compile(r"<!-- review-bot:v1 summary reviewed=(\S+) -->")
_SHA: Final = re.compile(r"[0-9a-f]{40}")
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
    rationale, then its evidence spans if it has any, then asks for a reaction. A rationale over
    2,000 characters shows its first 2,000 and ``(cut)``.
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
    paragraphs = [heading, _shown_rationale(finding.rationale)]
    if finding.evidence:
        paragraphs.append(_evidence_line(finding.evidence))
    paragraphs += [_REACTION_REQUEST, marker]
    return _NEW_PARAGRAPH.join(paragraphs)


def _evidence_line(evidence: Sequence[EvidenceSpan]) -> str:
    """``Context:`` and each span as a code span, ``path:12`` or ``path:10-14``."""
    spans = [markdown_path(span.file, suffix=f":{_line_range(span)}") for span in evidence]
    return f"Context: {', '.join(spans)}"


def _line_range(span: EvidenceSpan) -> str:
    if span.start_line == span.end_line:
        return str(span.start_line)
    return f"{span.start_line}-{span.end_line}"


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


def with_summary_marker(body: str, *, reviewed_sha: str | None) -> str:
    """Return the summary ``body`` as posted: its last line is the summary marker.

    The marker records ``reviewed_sha``, the last commit reviewed, or ``none`` if there is none.

    Raises:
        ValueError: If ``reviewed_sha`` isn't 40 lowercase hex digits.
    """
    if reviewed_sha is not None and _SHA.fullmatch(reviewed_sha) is None:
        raise ValueError(f"A reviewed commit is 40 lowercase hex digits, not {reviewed_sha!r}.")
    marker = f"<!-- review-bot:v1 summary reviewed={reviewed_sha or _NO_REVIEWED_SHA} -->"
    return f"{body}{_NEW_PARAGRAPH}{marker}"


def is_summary(body: str) -> bool:
    """Say whether ``body``'s last line is exactly a summary marker, whatever it records.

    Call it only on the bot's own comments, as ``read_marker``.
    """
    return _summary_marker(body) is not None


def read_reviewed_sha(body: str) -> str | None:
    """Return the commit a summary's marker records, or None if it records no 40-hex SHA.

    Call it only on the bot's own comments, as ``read_marker``.
    """
    marker = _summary_marker(body)
    if marker is None or _SHA.fullmatch(marker.group(1)) is None:
        return None
    return marker.group(1)


def _summary_marker(body: str) -> re.Match[str] | None:
    return _ANY_SUMMARY_MARKER.fullmatch(body.rsplit("\n", maxsplit=1)[-1])


def _shown_rationale(rationale: str) -> str:
    if len(rationale) <= _MAX_RATIONALE_CHARS:
        return escape_markdown(rationale)
    return f"{escape_markdown(rationale[:_MAX_RATIONALE_CHARS])} (cut)"


def reviewed_summary(
    plan: CommentPlan,
    *,
    threshold: Severity,
    since: str | None = None,
    open_comments: int = 0,
    codeowners: CodeOwners = NO_CODEOWNERS,
) -> str:
    """Return the summary of a review: its counts, up to 20 withheld findings, then owners.

    With nothing planned or withheld, it says there are no findings at or above ``threshold``.
    A review of only the changes since the commit ``since`` names that commit, calls its
    findings new, and always gives ``open_comments``, the bot's comments open on the pull
    request after the run; a whole review gives that count only when it isn't zero. Last, it
    counts the findings of each owner ``codeowners`` names (``_owners_section``).
    """
    headline = f"{_HEADLINE} `{Outcome.REVIEWED}`."
    if since is not None:
        headline = f"{headline} Changes since {_short_sha(since)} reviewed."
    if not plan.comments and not plan.withheld:
        new = "" if since is None else "new "
        counts = f"{headline} No {new}findings at or above {threshold}."
    else:
        counts = f"{headline} {len(plan.comments)} comments, {len(plan.withheld)} withheld."
    if since is not None or open_comments:
        counts = f"{counts} {_open_count(open_comments)}"
    sections = [counts]
    if plan.withheld:
        listed = [
            f"{_location(item.reviewed.finding.file, item.reviewed.finding.line)}: `{item.reason}`"
            for item in plan.withheld[:_MAX_LISTED]
        ]
        sections += ["Withheld findings:", _listed(listed, total=len(plan.withheld))]
    owners = _owners_section(plan, codeowners)
    if owners is not None:
        sections += ["Owners:", owners]
    return _NEW_PARAGRAPH.join(sections)


def _owners_section(plan: CommentPlan, codeowners: CodeOwners) -> str | None:
    """How many planned and withheld findings each code owner has, or None if none has any.

    Owners go most findings first, then by name, at most 10; then how many findings have no
    owner, if any. Each owner is in a code span, so it notifies nobody (Phase 3 design, Q8).
    """
    findings = (
        *(comment.reviewed.finding for comment in plan.comments),
        *(item.reviewed.finding for item in plan.withheld),
    )
    counts: Counter[str] = Counter()
    unowned = 0
    for finding in findings:
        owners = owners_for(codeowners, finding.file)
        counts.update(owners)
        unowned += not owners
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    listed = [f"`{owner}`: {count}" for owner, count in ranked[:_MAX_LISTED_OWNERS]]
    section = _listed(listed, total=len(ranked))
    return f"{section}\n- no owner: {unowned}" if unowned else section


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


def no_new_changes_summary(*, since: str, open_comments: int) -> str:
    """Return the summary of a run with nothing new since ``since``, the last commit reviewed.

    It gives ``open_comments``, the bot's comments open on the pull request after the run.
    """
    return (
        f"{_HEADLINE} `{Outcome.REVIEWED}` (`{Reason.NO_NEW_CHANGES}`). No changes to review "
        f"since {_short_sha(since)}, so nothing was sent to the model. "
        f"{_open_count(open_comments)}"
    )


def _short_sha(sha: str) -> str:
    """A commit's first 7 characters, in a code span so it never links."""
    return f"`{sha[:7]}`"


def _open_count(open_comments: int) -> str:
    return f"Open bot comments on this pull request: {open_comments}."


def not_reviewed_summary(outcome: Outcome, reason: Reason | ErrorCategory) -> str:
    """Return the summary of a skipped or failed run, which has no findings."""
    if outcome is Outcome.FAILED:
        detail = "The review is unavailable: it didn't complete, so there are no findings."
    else:
        detail = "Nothing was sent to the model."
    return f"{_HEADLINE} `{outcome}` (`{reason}`). {detail}"


def invalid_config_summary(problem: str) -> str:
    """Return the summary of a run whose repository config is invalid, saying what is wrong."""
    reason = Reason.INVALID_CONFIG
    return (
        f"{_HEADLINE} `{Outcome.FAILED}` (`{reason}`). Nothing was sent to the model. "
        f"{escape_markdown(problem)}"
    )


def _listed(items: Sequence[str], *, total: int) -> str:
    """``items`` as a Markdown list, then ``and K more`` for the rest of ``total``."""
    lines = [f"- {item}" for item in items]
    if total > len(items):
        lines.append(f"- and {total - len(items)} more")
    return "\n".join(lines)
