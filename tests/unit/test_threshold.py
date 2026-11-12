import pytest

from review_bot.findings import Category, Finding, ReviewedFinding, Severity, Source
from review_bot.threshold import Decision, decide


def reviewed(severity: Severity, line: int = 7) -> ReviewedFinding:
    finding = Finding(
        file="a.py",
        line=line,
        severity=severity,
        category=Category.LOGIC,
        rationale="Why.",
        confidence=0.9,
    )
    return ReviewedFinding(finding, Source.AI, None, f"fingerprint-{line}")


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [
        (Severity.LOW, list(Severity)),
        (Severity.MEDIUM, [Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]),
        (Severity.HIGH, [Severity.HIGH, Severity.CRITICAL]),
        (Severity.CRITICAL, [Severity.CRITICAL]),
    ],
)
def test_decide_reports_threshold_and_higher(threshold: Severity, expected: list[Severity]) -> None:
    decision = decide([reviewed(severity) for severity in Severity], threshold)

    assert [item.finding.severity for item in decision.reported] == expected
    assert [item.finding.severity for item in decision.below_threshold] == [
        severity for severity in Severity if severity not in expected
    ]


def test_decide_keeps_the_order_kept_within_each_part() -> None:
    kept = [
        reviewed(Severity.HIGH, line=3),
        reviewed(Severity.LOW, line=1),
        reviewed(Severity.CRITICAL, line=2),
        reviewed(Severity.LOW, line=4),
    ]

    decision = decide(kept, Severity.HIGH)

    assert [item.finding.line for item in decision.reported] == [3, 2]
    assert [item.finding.line for item in decision.below_threshold] == [1, 4]


def test_decide_with_nothing_kept_reports_nothing() -> None:
    assert decide((), Severity.LOW) == Decision(reported=(), below_threshold=())
