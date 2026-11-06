"""The text and JSON outputs for findings, and escaping untrusted text."""

import json
from collections.abc import Sequence
from typing import Final

from review_bot.findings import Finding, ReviewedFinding, Severity
from review_bot.prompt import PROMPT_VERSION
from review_bot.report import PipelineResult, path_fields, section_path
from review_bot.settings import Settings

__all__ = ["escape_unprintable", "render_findings", "render_json"]

_REPORT_SCHEMA: Final = "report/v1"


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


def escape_unprintable(text: str) -> str:
    """Escape control and line-break characters, so untrusted text can't forge lines or drive a TTY.

    Escaping rather than replacing keeps an odd file name visibly different from a normal one.
    """
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in text
    )
