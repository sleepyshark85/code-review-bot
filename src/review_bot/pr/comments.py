"""The Markdown the bot would post: each comment's body and marker, and the summary comment.

Model and diff text reaches these bodies only through ``render.escape_markdown`` and
``render.markdown_path``, so it can't mention, link, reference or hide anything (Phase 2 design,
Safety on the PR path). The one exception is a verified suggestion, which goes whole in a fenced
block it can't close (``suggestion.fenced_block``). A code owner from CODEOWNERS goes in a code
span, which its syntax keeps whole. ``read_marker``, ``is_summary``, ``read_reviewed_sha`` and
``read_summary_marker`` read posted bodies back.

The summary marker's grammar lives here: its writer, ``with_summary_marker``, and its readers
(Phase 4 design, Summary marker).
"""

import re
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final

from review_bot.context.codeowners import NO_CODEOWNERS, CodeOwners, owners_for
from review_bot.errors import ErrorCategory
from review_bot.findings import Category, EvidenceSpan, RuleId, Severity
from review_bot.governance.gate import (
    GATE_CLASS_NAMES,
    POSSIBLE_SECRET,
    Blocker,
    GateDecision,
    GateReason,
    GateResult,
    is_ai_class,
)
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.plan import CommentPlan, PlannedComment
from review_bot.render import escape_markdown, markdown_path
from review_bot.secret_scan import SecretHit
from review_bot.suggestion import fenced_block

__all__ = [
    "GATE_HEADING",
    "MAX_MARKER_ENTRIES",
    "Marker",
    "MarkerEntry",
    "SummaryMarker",
    "blocked_summary",
    "body_before_gate",
    "comment_body",
    "gate_summary",
    "holds_gate_state",
    "invalid_config_summary",
    "is_summary",
    "no_new_changes_summary",
    "not_reviewed_summary",
    "read_marker",
    "read_reviewed_sha",
    "read_summary_marker",
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
_CONFIDENCE_SHOWN: Final = re.compile(r", confidence ([01]\.[0-9]{2})\)$")
_MARKER: Final = re.compile(
    r"<!-- review-bot:v1 fingerprint=([0-9a-f]{16}) rule=(\S+) anchor=([0-9a-f]{16}) -->"
)
# The summary's last line records the last commit reviewed, or ``none`` (ADR 0009), and with
# gating on, the blockers. Any line with this start and end, and a value after ``reviewed=``,
# marks the bot's summary, so one written before P2-S7, by a gated run, or edited by hand, is
# still found; only ``read_summary_marker`` reads it under the full grammar.
_NO_REVIEWED_SHA: Final = "none"
_SUMMARY_MARKER_START: Final = "<!-- review-bot:v1 summary reviewed="
_MARKER_END: Final = " -->"
_SHA: Final = re.compile(r"[0-9a-f]{40}")
_RULE_PARTS: Final = frozenset((*RuleId, *(f"ai:{category}" for category in Category)))
# The summary marker's gate fields (Phase 4 design, Summary marker).
MAX_MARKER_ENTRIES: Final = 25
_MAX_MARKER_CHARS: Final = 2_000
_MAX_MORE: Final = 999_999
_TAMPERED_FIELD: Final = "tampered"
_GATE_FIELD_PREFIXES: Final = ("gate=", "more=")
_ENTRY: Final = rf"[0-9a-f]{{16}}:(?:{'|'.join(map(re.escape, sorted(GATE_CLASS_NAMES)))})"
_GATED_SUMMARY_MARKER: Final = re.compile(
    rf"<!-- review-bot:v1 summary reviewed=(?P<reviewed>[0-9a-f]{{40}}|{_NO_REVIEWED_SHA})"
    rf"(?: gate=(?P<gate>{_ENTRY}(?:,{_ENTRY}){{0,{MAX_MARKER_ENTRIES - 1}}}))?"
    r"(?: more=(?P<more>[1-9][0-9]{0,5}))?"
    rf"(?P<tampered> {_TAMPERED_FIELD})? -->"
)


@dataclass(frozen=True, slots=True)
class Marker:
    """What a posted comment's body says about its finding: its marker, severity and confidence.

    ``confidence`` is None if its heading shows none.
    """

    fingerprint: str
    rule_part: str
    anchor: str
    severity: Severity
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class MarkerEntry:
    """One blocker a summary marker lists: its fingerprint and its class."""

    fingerprint: str
    gate_class: str

    @property
    def is_ai(self) -> bool:
        """Whether its class is a model finding's, the only kind that carries between runs."""
        return is_ai_class(self.gate_class)


@dataclass(frozen=True, slots=True)
class SummaryMarker:
    """What a summary's marker records, read under the full grammar.

    ``reviewed_sha`` is the last commit reviewed, or None for ``none``. ``entries`` are the
    blockers it lists, ``ai:`` ones first; ``more`` how many it left out; ``tampered`` whether
    a tamper's rebuild is pending.
    """

    reviewed_sha: str | None
    entries: tuple[MarkerEntry, ...] = ()
    more: int = 0
    tampered: bool = False


def comment_body(comment: PlannedComment) -> str:
    """Return the comment's body; its last line is its marker (ADR 0009).

    The body shows the finding's severity, category, source, rule if any, confidence and
    rationale, then its evidence spans if it has any, then its suggestion if it has one, as a
    GitHub suggestion block, then asks for a reaction. A rationale over 2,000 characters shows its
    first 2,000 and ``(cut)``.
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
    if finding.suggestion is not None:
        paragraphs.append(fenced_block(finding.suggestion, info="suggestion"))
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
    with the severity heading; the confidence is read from that line's end. Call it only on the
    bot's own comments, so nobody else's text is parsed (ADR 0009).
    """
    lines = body.split("\n")
    marker = _MARKER.fullmatch(lines[-1])
    heading = _SEVERITY_HEADING.match(lines[0])
    if marker is None or heading is None:
        return None
    fingerprint, rule_part, anchor = marker.groups()
    if rule_part not in _RULE_PARTS:
        return None
    confidence = _CONFIDENCE_SHOWN.search(lines[0])
    shown = None if confidence is None else float(confidence.group(1))
    return Marker(fingerprint, rule_part, anchor, Severity(heading.group(1)), shown)


def with_summary_marker(
    body: str, *, reviewed_sha: str | None, gate: GateResult | None = None
) -> str:
    """Return the summary ``body`` as posted: its last line is the summary marker.

    The marker records ``reviewed_sha``, the last commit reviewed, or ``none`` if there is none.
    An enabled ``gate`` adds its first 25 blockers as ``gate=``, in its order, how many it left
    out as ``more=``, and `` tampered`` if that is its reason; with no blocker and no tamper,
    or no enabled ``gate``, the marker is Phase 3's.

    Raises:
        ValueError: If ``reviewed_sha`` isn't 40 lowercase hex digits.
    """
    if reviewed_sha is not None and _SHA.fullmatch(reviewed_sha) is None:
        raise ValueError(f"A reviewed commit is 40 lowercase hex digits, not {reviewed_sha!r}.")
    fields = [f"{_SUMMARY_MARKER_START}{reviewed_sha or _NO_REVIEWED_SHA}"]
    if gate is not None and gate.enabled:
        listed = gate.blocking[:MAX_MARKER_ENTRIES]
        if listed:
            entries = ",".join(f"{blocker.fingerprint}:{blocker.gate_class}" for blocker in listed)
            fields.append(f"gate={entries}")
        left_out = len(gate.blocking) - len(listed)
        if left_out:
            fields.append(f"more={min(left_out, _MAX_MORE)}")
        if gate.reason is GateReason.TAMPERED:
            fields.append(_TAMPERED_FIELD)
    return f"{body}{_NEW_PARAGRAPH}{' '.join(fields)}{_MARKER_END}"


def is_summary(body: str) -> bool:
    """Say whether ``body``'s last line is a summary marker, whatever it records.

    That is a line starting ``<!-- review-bot:v1 summary reviewed=`` and a value, and ending
    `` -->``. Call it only on the bot's own comments, as ``read_marker``.
    """
    return _marker_fields(body) is not None


def read_reviewed_sha(body: str) -> str | None:
    """Return the commit a summary's marker records, or None if it records no 40-hex SHA.

    Any gate fields after it are passed over. Call it only on the bot's own comments, as
    ``read_marker``.
    """
    fields = _marker_fields(body)
    if fields is None or _SHA.fullmatch(fields[0]) is None:
        return None
    return fields[0]


def holds_gate_state(body: str) -> bool:
    """Say whether a summary's marker has a gate field: ``gate=``, ``more=`` or ``tampered``.

    Any such field counts, even in a marker the full grammar refuses, so a run that can't read
    its config never takes a gated summary for an ungated one (P4-S5 AC11).
    """
    fields = _marker_fields(body) or []
    return any(
        field.startswith(_GATE_FIELD_PREFIXES) or field == _TAMPERED_FIELD for field in fields[1:]
    )


def read_summary_marker(body: str) -> SummaryMarker | None:
    """Read a summary's marker under the full grammar, or return None if it is malformed.

    The last line must be at most 2,000 characters, and exactly ``reviewed=`` a 40-hex SHA or
    ``none``, then optionally ``gate=`` with 1 to 25 entries, ``ai:`` ones first, then
    ``more=`` only after 25 entries, then `` tampered`` (Phase 4 design, Summary marker). The
    marker is untrusted: call it only on the bot's own summary.
    """
    line = body.rsplit("\n", maxsplit=1)[-1]
    if len(line) > _MAX_MARKER_CHARS:
        return None
    match = _GATED_SUMMARY_MARKER.fullmatch(line)
    if match is None:
        return None
    listed = match["gate"]
    entries = tuple(
        MarkerEntry(*entry.split(":", maxsplit=1))
        for entry in (listed.split(",") if listed else ())
    )
    more = int(match["more"] or 0)
    is_ai = [entry.is_ai for entry in entries]
    if (more and len(entries) < MAX_MARKER_ENTRIES) or is_ai != sorted(is_ai, reverse=True):
        return None
    reviewed = match["reviewed"]
    return SummaryMarker(
        reviewed_sha=None if reviewed == _NO_REVIEWED_SHA else reviewed,
        entries=entries,
        more=more,
        tampered=match["tampered"] is not None,
    )


def _marker_fields(body: str) -> list[str] | None:
    """The space-separated fields after ``reviewed=`` on a summary marker, or None if none."""
    line = body.rsplit("\n", maxsplit=1)[-1]
    if not (line.startswith(_SUMMARY_MARKER_START) and line.endswith(_MARKER_END)):
        return None
    value = line[len(_SUMMARY_MARKER_START) : -len(_MARKER_END)]
    if not value or value[0].isspace():
        return None
    return value.split(" ")


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


def _location(path: str, line: int | None) -> str:
    """The path, and line if any, as a bounded code span, in git's quoted form if not UTF-8."""
    suffix = "" if line is None else f":{line}"
    if len(path) <= _MAX_LISTED_PATH_CHARS:
        return markdown_path(path, suffix=suffix)
    return f"{markdown_path(path[:_MAX_LISTED_PATH_CHARS], suffix=suffix)} (path cut)"


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


GATE_HEADING: Final = "**Merge gate:**"
_TAMPERED: Final = (
    "A tamper occurred: the bot's summary, or one of its inline comments, was edited by someone "
    "else, or the summary was deleted or unreadable. The merge stays blocked (`tampered`) until "
    "a review can rebuild the blockers from the bot's own unedited comments; withheld findings, "
    "and any whose comment was deleted, can't be rebuilt and are lost."
)
_REBUILT: Final = (
    "A tamper occurred: the bot's previous summary was edited by someone else, deleted or "
    "unreadable, so its blockers were rebuilt from the bot's own inline comments. Withheld "
    "findings, and any whose comment was deleted, couldn't be rebuilt and are lost."
)
_APPEAL: Final = (
    "To appeal, reply on the finding's thread. A maintainer overrides a finding by replying "
    "`/review-bot override: <reason>` on the bot's thread; for one with no thread of the bot's "
    "(a secret, a withheld finding, or a comment that is gone), by submitting a PR review whose "
    "body starts `/review-bot override <fingerprint>: <reason>`, with the fingerprint shown "
    "above."
)


def gate_summary(gate: GateResult, *, would: bool, threaded: Collection[str]) -> str:
    """Return the summary's gate section: the decision, each blocker, and how to appeal.

    ``gate`` must be enabled. ``would`` words it for shadow mode, which only reports ("Would
    block"), and leaves out the appeal. ``threaded`` holds the fingerprints of the bot's
    comments on the pull request; a blocker without one shows its fingerprint, which a PR
    review overriding it names. The first 25 blockers are listed, as the marker lists them,
    then how many more there are, and how many ``ai:`` ones of those won't carry.
    """
    sections = [f"{GATE_HEADING} {_gate_headline(gate, would=would)}"]
    if gate.reason is GateReason.TAMPERED:
        sections.append(_TAMPERED)
    elif gate.rebuilt:
        sections.append(_REBUILT)
    if gate.blocking:
        listed = [
            _blocker_line(blocker, threaded) for blocker in gate.blocking[:MAX_MARKER_ENTRIES]
        ]
        sections.append(_listed(listed, total=len(gate.blocking)))
        uncarried = sum(blocker.is_ai for blocker in gate.blocking[MAX_MARKER_ENTRIES:])
        if uncarried:
            sections.append(
                f"{uncarried} `ai:` findings beyond the {MAX_MARKER_ENTRIES} listed aren't "
                "carried to the next review unless the model reports them again."
            )
        if not would:
            sections.append(_APPEAL)
    return _NEW_PARAGRAPH.join(sections)


def _gate_headline(gate: GateResult, *, would: bool) -> str:
    """The decision in a sentence, naming the blockers' classes, or the classes gated on."""
    gated = ", ".join(gate.classes)
    match gate.decision:
        case GateDecision.BLOCK:
            verb = "Would block" if would else "Blocks"
            if not gate.blocking:
                return f"{verb} the merge (`{gate.reason}`)."
            classes = ", ".join(dict.fromkeys(blocker.gate_class for blocker in gate.blocking))
            count = len(gate.blocking)
            return f"{verb} the merge: {count} finding{'' if count == 1 else 's'} ({classes})."
        case GateDecision.PASS:
            return f"{'Would pass' if would else 'Passes'}: no finding of a gated class ({gated})."
        case _:
            return (
                f"`{GateDecision.UNJUDGED}`: the review didn't complete, and no finding of a "
                f"gated class ({gated}) was found, so the merge isn't decided."
            )


def _blocker_line(blocker: Blocker, threaded: Collection[str]) -> str:
    """Where a blocker is and its class, with its fingerprint if no comment of the bot's has it."""
    if blocker.file is not None:
        where = _location(blocker.file, blocker.line)
    elif blocker.carried:
        where = "carried from an earlier review, no comment"
    elif blocker.gate_class == POSSIBLE_SECRET:
        where = "a file path holding a possible secret"
    else:
        where = "a finding in a file whose path holds a possible secret"
    line = f"{where}: {blocker.gate_class}"
    if blocker.fingerprint in threaded:
        return line
    return f"{line}, fingerprint `{blocker.fingerprint}`"


def body_before_gate(summary: str) -> str:
    """Return a summary the bot posted without its marker, and without its gate section if any.

    The gate section starts with ``GATE_HEADING`` on a paragraph of its own, which no escaped
    text can.
    """
    body = summary.rsplit("\n", maxsplit=1)[0].removesuffix("\n")
    return body.partition(f"{_NEW_PARAGRAPH}{GATE_HEADING}")[0]
