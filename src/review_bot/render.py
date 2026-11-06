"""The text output for findings, and escaping untrusted text."""

from collections.abc import Sequence

from review_bot.findings import Finding, Severity

__all__ = ["escape_unprintable", "render_findings"]


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
