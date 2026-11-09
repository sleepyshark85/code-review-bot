"""The text, JSON and Markdown outputs for findings, and escaping untrusted text."""

import json
import re
import string
from collections.abc import Iterable, Sequence
from typing import Final

from review_bot.context.ledger import ToolOutcome, merged_spans_by_file
from review_bot.findings import EvidenceSpan, Finding, ReviewedFinding, Severity
from review_bot.report import (
    PipelineResult,
    TriageDecision,
    TriagedFinding,
    not_rejected,
    path_fields,
    section_path,
)
from review_bot.settings import Settings
from review_bot.suggestion import fenced_block

__all__ = [
    "escape_markdown",
    "escape_unprintable",
    "evidence_json",
    "indented_in_list_item",
    "markdown_path",
    "render_findings",
    "render_json",
    "render_markdown",
    "report_document",
]

_REPORT_SCHEMA: Final = "report/v1"
_MARKDOWN_HEADING: Final = "## Review"
_MAX_FILES_READ: Final = 200  # Phase 3 design, Report keys
# CommonMark's ASCII punctuation, each of which a backslash makes literal.
_MARKDOWN_PUNCTUATION: Final = frozenset(string.punctuation)
_BACKTICK_RUN: Final = re.compile("`+")
# A word that goes in a code span: one GitHub could turn into a mention or a link, e-mail
# addresses included (P1-S11 spec), or into an issue, PR or commit reference (`#12`, `GH-3`,
# `owner/repo#4` or a word of 7 to 40 hex digits), or one that opens an HTML comment (P2-S1 spec,
# fixes #37). Each alternative matches in time linear in the word.
_CODE_SPAN_WORD: Final = re.compile(
    r"@|://|www\.|#[0-9]|\bGH-[0-9]|\b[0-9a-f]{7,40}\b|<!--", re.IGNORECASE
)


def render_findings(findings: Sequence[Finding], threshold: Severity) -> str:
    """Render already filtered findings, one line each, with no trailing newline."""
    if not findings:
        return f"No findings at or above {threshold}."
    return "\n".join(_render_finding(finding) for finding in findings)


def _render_finding(finding: Finding) -> str:
    return (
        f"{escape_unprintable(finding.file)}:{finding.line} "
        f"[{finding.severity}/{finding.category}] "
        f"{escape_unprintable(finding.rationale)} (confidence {finding.confidence:.2f})"
    )


def render_json(
    result: PipelineResult,
    settings: Settings,
    run_id: str,
    *,
    triaged: Sequence[TriagedFinding] | None = None,
) -> str:
    """Render the review as one line of pure ASCII JSON: ``report_document``'s object.

    Every non-ASCII or control character is a JSON escape.

    Raises:
        ValueError: If ``triaged`` doesn't hold the reported findings, in order.
    """
    document = report_document(result, settings, run_id, triaged=triaged)
    return json.dumps(document, ensure_ascii=True, allow_nan=False)


def report_document(
    result: PipelineResult,
    settings: Settings,
    run_id: str,
    *,
    triaged: Sequence[TriagedFinding] | None = None,
) -> dict[str, object]:
    """Return the review as a ``report/v1`` object, ready for ``json.dumps``.

    Keys come in a fixed order. Paths are written as they are, unredacted, so they match the
    text output; one that isn't valid UTF-8 is C-quoted, with ``path_quoted: true`` on its object
    (``report.path_fields``).

    Args:
        result: The review.
        settings: The run's settings.
        run_id: The run's identifier, as in the audit log.
        triaged: The reported findings with their triage decisions and any edits, or None if
            triage didn't run, which makes every finding's ``triage`` null. Rejected findings
            stay listed, but ``counts.reported`` leaves them out.

    Raises:
        ValueError: If ``triaged`` doesn't hold the reported findings, in order.
    """
    findings = _findings_json(result, triaged)
    decisions = [item.decision for item in triaged or ()]
    return {
        "schema": _REPORT_SCHEMA,
        "run_id": run_id,
        "provider": settings.provider,
        "model": settings.model,
        "prompt_version": result.prompt_version,
        "threshold": settings.threshold,
        "findings": findings,
        "counts": {
            "reported": len(findings) - decisions.count(TriageDecision.REJECT),
            "below_threshold": len(result.decision.below_threshold),
            "dropped": len(result.verification.dropped),
            "deduped": len(result.verification.deduped),
            "rejected": decisions.count(TriageDecision.REJECT),
            "undecided": decisions.count(TriageDecision.UNDECIDED),
        },
        "skipped_files": [
            {**path_fields(section_path(file)), "reason": file.skip_reason}
            for file in result.skipped_files
        ],
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cost_usd": None if result.cost_usd is None else str(result.cost_usd),
        },
        "mode": "diff" if result.context_commit is None else "context",
        "context": _context_json(result),
    }


def _context_json(result: PipelineResult) -> dict[str, object] | None:
    """The tools' commit and what they returned, or None in diff mode."""
    if result.context_commit is None:
        return None
    ledger = result.ledger
    by_file = merged_spans_by_file(span for call in ledger for span in call.spans)
    return {
        "commit": result.context_commit,
        "tool_calls": len(ledger),
        "refused": sum(call.outcome is ToolOutcome.REFUSED for call in ledger),
        "result_bytes": sum(call.result_bytes for call in ledger),
        "files_read": [
            {
                "path": path_fields(path)["file"],
                "spans": [[span.first_line, span.last_line] for span in spans],
            }
            for path, spans in list(by_file.items())[:_MAX_FILES_READ]
        ],
    }


def _findings_json(
    result: PipelineResult, triaged: Sequence[TriagedFinding] | None
) -> list[dict[str, object]]:
    """The reported findings' objects, with ``triage`` null for each if triage didn't run.

    Raises:
        ValueError: If ``triaged`` doesn't hold the reported findings, in order.
    """
    if triaged is None:
        return [_finding_json(reviewed) for reviewed in result.decision.reported]
    _check_triaged(result, triaged)
    return [
        _finding_json(item.reviewed, item.decision, item.original_rationale) for item in triaged
    ]


def _check_triaged(result: PipelineResult, triaged: Sequence[TriagedFinding]) -> None:
    """Raise ``ValueError`` unless ``triaged`` holds the reported findings, in order."""
    reported = result.decision.reported
    if [item.reviewed.fingerprint for item in triaged] != [item.fingerprint for item in reported]:
        raise ValueError("The triaged findings must be the reported findings, in order.")


def _finding_json(
    reviewed: ReviewedFinding,
    decision: TriageDecision | None = None,
    original_rationale: str | None = None,
) -> dict[str, object]:
    # Field by field, never ``model_dump_json``: a rule finding's path can hold lone surrogates,
    # which pydantic refuses to serialise.
    finding = reviewed.finding
    return {
        "fingerprint": reviewed.fingerprint,
        "source": reviewed.source,
        "rule": reviewed.rule,
        **path_fields(finding.file),
        "line": finding.line,
        "severity": finding.severity,
        "category": finding.category,
        "rationale": finding.rationale,
        "confidence": finding.confidence,
        "triage": decision,
        "original_rationale": original_rationale,
        "evidence": evidence_json(finding.evidence),
        "evidence_dropped": reviewed.evidence_dropped,
        "suggestion": finding.suggestion,
        "suggestion_dropped": reviewed.suggestion_dropped,
    }


def evidence_json(evidence: Iterable[EvidenceSpan]) -> list[dict[str, object]]:
    """Each evidence span as ``{file, start_line, end_line}``, its path as ``files_read`` has it."""
    return [
        {
            "file": path_fields(span.file)["file"],
            "start_line": span.start_line,
            "end_line": span.end_line,
        }
        for span in evidence
    ]


def render_markdown(
    result: PipelineResult,
    threshold: Severity,
    *,
    triaged: Sequence[TriagedFinding] | None = None,
) -> str:
    """Render the review as GitHub-flavoured Markdown, with no trailing newline.

    A ``## Review`` heading, then the counts unless all are zero, then one list item per reported
    finding (or the no-findings line), with its suggestion, if any, as a plain fenced code block
    inside it, then a ``### Skipped files`` section if any were skipped.
    With ``triaged``, the reported findings' triage decisions, rejected findings are left out of
    both the list and the counts.

    Raises:
        ValueError: If ``triaged`` doesn't hold the reported findings, in order.

    Text from the diff or the model is made inert, so it can't add structure, while every
    character stays visible. A rationale has its unprintable characters escaped as in the text
    output, so it stays on its own line. Then each space-separated word holding any ``@``,
    ``://`` or ``www.``, in any case, goes in a code span, since GitHub could turn it into a
    mention or a link. So does each word holding ``#`` then a digit, ``GH-`` then a digit, or a
    run of 7 to 40 hex digits standing alone, which GitHub could turn into an issue, pull
    request or commit reference, and each word holding ``<!--``, so that a forged comment marker
    stays visibly code. Every ASCII punctuation character in the other words is
    backslash-escaped: no heading, HTML, link, image, mention, emphasis, table or code span can
    start inside it. A path is shown as ``report.path_fields`` writes it, then
    unprintable-escaped, in a code span. A code span's or code block's backtick fence is longer
    than any backtick run in its text, so nothing inside it is interpreted.
    """
    reported = result.decision.reported
    if triaged is not None:
        _check_triaged(result, triaged)
        reported = not_rejected(triaged)
    blocks = [_MARKDOWN_HEADING]
    counts = _markdown_counts(result, len(reported))
    if counts:
        blocks.append(counts)
    if reported:
        blocks.append("\n".join(_markdown_finding(finding) for finding in reported))
    else:
        blocks.append(f"No findings at or above {threshold}.")
    if result.skipped_files:
        blocks.append("### Skipped files")
        blocks.append(
            "\n".join(
                f"- {markdown_path(section_path(file))}: {file.skip_reason}"
                for file in result.skipped_files
            )
        )
    return "\n\n".join(blocks)


def _markdown_counts(result: PipelineResult, reported_count: int) -> str:
    """The counts as one sentence, or an empty string when there is nothing to count."""
    counts = {
        "reported": reported_count,
        "below threshold": len(result.decision.below_threshold),
        "dropped": len(result.verification.dropped),
        "deduped": len(result.verification.deduped),
    }
    if not any(counts.values()):
        return ""
    return ", ".join(f"{count} {label}" for label, count in counts.items()) + "."


def _markdown_finding(reviewed: ReviewedFinding) -> str:
    finding = reviewed.finding
    location = markdown_path(finding.file, suffix=f":{finding.line}")
    item = (
        f"- **{finding.severity}** {finding.category} at {location}: "
        f"{escape_markdown(finding.rationale)} "
        f"(confidence {finding.confidence:.2f}, source: {reviewed.source})"
    )
    if finding.suggestion is None:
        return item
    return f"{item}\n{indented_in_list_item(fenced_block(finding.suggestion))}"


def indented_in_list_item(text: str) -> str:
    """``text`` with each non-empty line indented two spaces, so it stays inside a ``- `` item."""
    return "\n".join(f"  {line}" if line else "" for line in text.split("\n"))


def escape_markdown(text: str) -> str:
    """Make untrusted text inert in GitHub-flavoured Markdown, keeping every character visible.

    The rules are ``render_markdown``'s: unprintable characters are escaped, each word GitHub
    could link or that opens an HTML comment goes in a code span, and the other words have their
    punctuation backslash-escaped. Linear in the text's length.
    """
    # Unprintable characters are escaped first, so a plain space is the only whitespace left.
    return " ".join(_markdown_word(word) for word in escape_unprintable(text).split(" "))


def _markdown_word(word: str) -> str:
    # GitHub links mentions, URLs and references in text after CommonMark has dropped the
    # backslashes, but never inside a code span.
    if _CODE_SPAN_WORD.search(word):
        return _code_span(word)
    return "".join(rf"\{char}" if char in _MARKDOWN_PUNCTUATION else char for char in word)


def markdown_path(path: str, *, suffix: str = "") -> str:
    """A path, plus ``suffix``, as a code span that shows exactly its characters."""
    # ``str()`` narrows the field's ``str | bool`` type for mypy: ``file`` is always a string.
    return _code_span(escape_unprintable(str(path_fields(path)["file"])) + suffix)


def _code_span(text: str) -> str:
    """``text``, holding no line break, as a code span that shows exactly its characters."""
    longest_run = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    fence = "`" * (longest_run + 1)
    # CommonMark drops one space from each end of a span, and a backtick at an end would join
    # the fence, so such text gets a space of padding on each side. CommonMark never strips a
    # span of only spaces, so empty or all-space text shows those two spaces too.
    is_padded = not text or text[0] in "` " or text[-1] in "` "
    padding = " " if is_padded else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def escape_unprintable(text: str) -> str:
    """Escape control and line-break characters, so untrusted text can't forge lines or drive a TTY.

    Escaping rather than replacing keeps an odd file name visibly different from a normal one.
    """
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in text
    )
