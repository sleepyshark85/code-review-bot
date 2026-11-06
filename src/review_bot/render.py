"""The text, JSON and Markdown outputs for findings, and escaping untrusted text."""

import json
import re
import string
from collections.abc import Sequence
from typing import Final

from review_bot.findings import Finding, ReviewedFinding, Severity
from review_bot.prompt import PROMPT_VERSION
from review_bot.report import PipelineResult, path_fields, section_path
from review_bot.settings import Settings

__all__ = ["escape_unprintable", "render_findings", "render_json", "render_markdown"]

_REPORT_SCHEMA: Final = "report/v1"
_MARKDOWN_HEADING: Final = "## Review"
# CommonMark's ASCII punctuation, each of which a backslash makes literal.
_MARKDOWN_PUNCTUATION: Final = frozenset(string.punctuation)
_BACKTICK_RUN: Final = re.compile("`+")
# A word GitHub could turn into a mention or a link, e-mail addresses included (P1-S11 spec).
_LINKABLE_WORD: Final = re.compile(r"@|://|www\.", re.IGNORECASE)


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


def render_json(result: PipelineResult, settings: Settings, run_id: str) -> str:
    """Render the review as one line of pure ASCII JSON, schema ``report/v1``.

    Keys come in a fixed order, and every non-ASCII or control character is a JSON escape. Paths
    are written as they are, unredacted, so they match the text output; one that isn't valid
    UTF-8 is C-quoted, with ``path_quoted: true`` on its object (``report.path_fields``).
    """
    report: dict[str, object] = {
        "schema": _REPORT_SCHEMA,
        "run_id": run_id,
        "provider": settings.provider,
        "model": settings.model,
        "prompt_version": PROMPT_VERSION,
        "threshold": settings.threshold,
        "findings": [_finding_json(reviewed) for reviewed in result.decision.reported],
        "counts": {
            "reported": len(result.decision.reported),
            "below_threshold": len(result.decision.below_threshold),
            "dropped": len(result.verification.dropped),
            "deduped": len(result.verification.deduped),
            # Triage (P1-S12) is what rejects findings or leaves them undecided.
            "rejected": 0,
            "undecided": 0,
        },
        "skipped_files": [
            {**path_fields(section_path(file)), "reason": file.skip_reason}
            for file in result.skipped_files
        ],
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        },
    }
    return json.dumps(report, ensure_ascii=True, allow_nan=False)


def _finding_json(reviewed: ReviewedFinding) -> dict[str, object]:
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
        # Set by triage (P1-S12) and editing (P1-S13).
        "triage": None,
        "original_rationale": None,
    }


def render_markdown(result: PipelineResult, threshold: Severity) -> str:
    """Render the review as GitHub-flavoured Markdown, with no trailing newline.

    A ``## Review`` heading, then the counts unless all are zero, then one list item per reported
    finding (or the no-findings line), then a ``### Skipped files`` section if any were skipped.

    Text from the diff or the model is made inert, so it can't add structure, while every
    character stays visible. A rationale has its unprintable characters escaped as in the text
    output, so it stays on its own line. Then each space-separated word holding any ``@``,
    ``://`` or ``www.``, in any case, goes in a code span, since GitHub could turn it into a
    mention or a link. Every ASCII punctuation character in the other words is
    backslash-escaped: no heading, HTML, link, image, mention, emphasis, table or code span can
    start inside it. A path is shown as ``report.path_fields`` writes it, then
    unprintable-escaped, in a code span. A code span's backtick fence is longer than any backtick
    run in its text, so nothing inside it is interpreted.
    """
    blocks = [_MARKDOWN_HEADING]
    counts = _markdown_counts(result)
    if counts:
        blocks.append(counts)
    reported = result.decision.reported
    if reported:
        blocks.append("\n".join(_markdown_finding(finding) for finding in reported))
    else:
        blocks.append(f"No findings at or above {threshold}.")
    if result.skipped_files:
        blocks.append("### Skipped files")
        blocks.append(
            "\n".join(
                f"- {_markdown_path(section_path(file))}: {file.skip_reason}"
                for file in result.skipped_files
            )
        )
    return "\n\n".join(blocks)


def _markdown_counts(result: PipelineResult) -> str:
    """The counts as one sentence, or an empty string when there is nothing to count."""
    counts = {
        "reported": len(result.decision.reported),
        "below threshold": len(result.decision.below_threshold),
        "dropped": len(result.verification.dropped),
        "deduped": len(result.verification.deduped),
    }
    if not any(counts.values()):
        return ""
    return ", ".join(f"{count} {label}" for label, count in counts.items()) + "."


def _markdown_finding(reviewed: ReviewedFinding) -> str:
    finding = reviewed.finding
    location = _markdown_path(finding.file, suffix=f":{finding.line}")
    return (
        f"- **{finding.severity}** {finding.category} at {location}: "
        f"{_escape_markdown(finding.rationale)} "
        f"(confidence {finding.confidence:.2f}, source: {reviewed.source})"
    )


def _escape_markdown(text: str) -> str:
    # Unprintable characters are escaped first, so a plain space is the only whitespace left.
    return " ".join(_markdown_word(word) for word in escape_unprintable(text).split(" "))


def _markdown_word(word: str) -> str:
    # GitHub links mentions and URLs in text after CommonMark has dropped the backslashes, but
    # never inside a code span.
    if _LINKABLE_WORD.search(word):
        return _code_span(word)
    return "".join(rf"\{char}" if char in _MARKDOWN_PUNCTUATION else char for char in word)


def _markdown_path(path: str, *, suffix: str = "") -> str:
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
