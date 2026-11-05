"""The severity threshold filter, the text output for findings, and escaping untrusted text."""

from collections.abc import Iterable, Sequence
from typing import Final

from review_bot.findings import Finding, Severity

__all__ = ["at_or_above", "escape_unprintable", "render_findings"]

_SEVERITY_RANK: Final = {severity: rank for rank, severity in enumerate(Severity)}


def at_or_above(findings: Iterable[Finding], threshold: Severity) -> tuple[Finding, ...]:
    """Return the findings whose severity is at least the threshold."""
    minimum_rank = _SEVERITY_RANK[threshold]
    return tuple(
        finding for finding in findings if _SEVERITY_RANK[finding.severity] >= minimum_rank
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


def escape_unprintable(text: str) -> str:
    """Escape control and line-break characters, so untrusted text can't forge lines or drive a TTY.

    Escaping rather than replacing keeps an odd file name visibly different from a normal one.
    """
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in text
    )
