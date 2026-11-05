import pytest

from review_bot.findings import Category, Finding, Severity
from review_bot.render import at_or_above, render_findings


def make_finding(
    *,
    file: str = "a.py",
    severity: Severity = Severity.HIGH,
    rationale: str = "Why.",
) -> Finding:
    return Finding(
        file=file,
        line=7,
        severity=severity,
        category=Category.LOGIC,
        rationale=rationale,
        confidence=0.876,
    )


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [
        (Severity.LOW, list(Severity)),
        (Severity.MEDIUM, [Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]),
        (Severity.HIGH, [Severity.HIGH, Severity.CRITICAL]),
        (Severity.CRITICAL, [Severity.CRITICAL]),
    ],
)
def test_at_or_above_keeps_threshold_and_higher(
    threshold: Severity, expected: list[Severity]
) -> None:
    findings = [make_finding(severity=severity) for severity in Severity]

    kept = at_or_above(findings, threshold)

    assert [finding.severity for finding in kept] == expected


def test_render_findings_with_none_names_the_threshold() -> None:
    assert render_findings((), Severity.HIGH) == "No findings at or above high."


def test_render_findings_formats_each_finding_on_its_own_line() -> None:
    findings = [make_finding(), make_finding(file="b.py", severity=Severity.LOW)]

    assert render_findings(findings, Severity.LOW) == (
        "a.py:7 [high/logic] Why. (confidence 0.88)\nb.py:7 [low/logic] Why. (confidence 0.88)"
    )


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("x\ny", r"x\ny"),
        ("x\r\ny", r"x\r\ny"),
        ("x\ty", r"x\ty"),
        ("\x1b[31mred", r"\x1b[31mred"),
        ("x\x85y", r"x\x85y"),
        ("x\u2028y", r"x\u2028y"),
        ("x\u2029y", r"x\u2029y"),
        ("x\u202ey", r"x\u202ey"),
    ],
    ids=["newline", "crlf", "tab", "escape", "next-line", "line-sep", "para-sep", "bidi-override"],
)
def test_render_findings_escapes_unprintable_characters(raw: str, escaped: str) -> None:
    rendered_file = render_findings([make_finding(file=raw)], Severity.LOW)
    rendered_rationale = render_findings([make_finding(rationale=raw)], Severity.LOW)

    assert rendered_file.startswith(f"{escaped}:7 ")
    assert f"] {escaped} (confidence" in rendered_rationale
    assert len(rendered_file.splitlines()) == 1
    assert len(rendered_rationale.splitlines()) == 1


def test_render_findings_keeps_printable_unicode() -> None:
    rendered = render_findings([make_finding(rationale="a → b é 日本")], Severity.LOW)

    assert "a → b é 日本" in rendered
