"""Unit tests for `matches_bug`: file, line windows, categories and keyword boundaries."""

from typing import Any

import pytest

from review_bot.evals.formats import Bug
from review_bot.evals.match import matches_bug
from review_bot.findings import Category, Finding, Severity

FILE = "billing/invoice.py"


def bug(**fields: Any) -> Bug:
    return Bug.model_validate(
        {"id": "b", "source": "ai", "file": FILE, "lines": "any", "min_runs_caught": 3} | fields
    )


def finding(
    rationale: str = "Why.", line: int = 34, category: Category = Category.LOGIC, file: str = FILE
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=Severity.MEDIUM,
        category=category,
        rationale=rationale,
        confidence=0.9,
    )


SAMPLE_BUGS = {
    "discount-dropped-on-export": bug(
        lines=[[34, 34], [38, 41]],
        keywords=[["discount", "discounts", "total_override"], ["export", "exports", "csv"]],
    ),
    "tax-discount-ordering": bug(
        lines=[[14, 14], [32, 36]],
        keywords=[["tax", "tax_rate"], ["discount"], ["order", "ordering", "before", "after"]],
    ),
    "double-tax": bug(
        lines=[[14, 14], [34, 34]],
        keywords=[["tax", "taxed", "tax_rate"], ["double", "twice", "already", "caller"]],
    ),
    "float-money": bug(lines=[[13, 14], [34, 34], [40, 40]], keywords=[["float", "decimal"]]),
    "csv-path": bug(
        lines=[[38, 41]], keywords=[["unvalidated path", "validate", "traversal", "csv module"]]
    ),
    "missing-tests": bug(categories=["testing"]),
}


def credited(target: Finding) -> list[str]:
    return [bug_id for bug_id, candidate in SAMPLE_BUGS.items() if matches_bug(candidate, target)]


@pytest.mark.parametrize("word", ["reorder", "disorder", "orders", "order2"])
def test_matches_bug_needs_a_boundary_around_a_phrase(word: str) -> None:
    assert not matches_bug(bug(keywords=[["order"]]), finding(f"The {word} is wrong."))


@pytest.mark.parametrize("phrase", ["export", "csv", "EXPORT"])
def test_matches_bug_counts_an_underscore_as_a_boundary_and_ignores_case(phrase: str) -> None:
    assert matches_bug(bug(keywords=[[phrase]]), finding("export_csv writes it."))


def test_matches_bug_treats_a_letter_in_another_script_as_no_boundary() -> None:
    assert not matches_bug(bug(keywords=[["tax"]]), finding("taxé twice"))


def test_matches_bug_matches_a_phrase_with_punctuation_literally() -> None:
    assert matches_bug(bug(keywords=[["csv.writer"]]), finding("Use csv.writer here."))
    assert not matches_bug(bug(keywords=[["csv.writer"]]), finding("Use csvXwriter here."))


def test_matches_bug_needs_every_keyword_group() -> None:
    target = bug(keywords=[["tax"], ["twice"]])

    assert matches_bug(target, finding("Tax is added twice."))
    assert not matches_bug(target, finding("Tax is added."))


@pytest.mark.parametrize(("line", "expected"), [(13, False), (14, True), (33, False), (34, True)])
def test_matches_bug_needs_a_line_inside_a_window(line: int, expected: bool) -> None:
    assert matches_bug(bug(lines=[[14, 14], [34, 36]]), finding(line=line)) is expected


def test_matches_bug_needs_the_same_file() -> None:
    assert not matches_bug(bug(), finding(file="billing/other.py"))


@pytest.mark.parametrize(
    ("rationale", "line", "expected"),
    [
        ("Float arithmetic on the discount", 34, ["float-money"]),
        ("CSV export drops the discount", 40, ["discount-dropped-on-export"]),
        ("Tax is applied before the discount", 14, ["tax-discount-ordering"]),
        ("open() uses the platform encoding for this path", 39, []),
    ],
)
def test_matches_bug_gives_no_cross_credit_on_the_sample_bugs(
    rationale: str, line: int, expected: list[str]
) -> None:
    assert credited(finding(rationale, line=line)) == expected


def test_matches_bug_credits_missing_tests_by_category_not_by_the_word_test() -> None:
    assert credited(finding("No test covers this.", category=Category.LOGIC)) == []
    assert credited(finding("Add coverage.", line=1, category=Category.TESTING)) == [
        "missing-tests"
    ]


def test_matches_bug_lets_one_finding_credit_two_bugs_only_if_it_meets_both() -> None:
    both = finding("Float tax is already applied by callers", line=14)

    assert credited(both) == ["double-tax", "float-money"]
