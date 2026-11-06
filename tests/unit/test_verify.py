import pytest
from diff_samples import section

from review_bot.diff_parser import parse_diff
from review_bot.findings import Category, Finding, ReviewedFinding, RuleId, Severity, Source
from review_bot.fingerprint import fingerprint_for
from review_bot.verify import DropReason, ProposalCheck, verify_findings

# `app.py` has added and context lines 10 to 12 and a removed line; `README.md` is skipped.
DIFF = (
    "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
    "@@ -10,2 +10,3 @@\n a = 1\n-b = 2\n+b = 3\n+c = 4\n"
) + section("README.md")


def make_finding(file: str = "app.py", line: int = 10, rationale: str = "Why.") -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=Severity.HIGH,
        category=Category.LOGIC,
        rationale=rationale,
        confidence=0.9,
    )


def rule_finding(finding: Finding) -> ReviewedFinding:
    return ReviewedFinding(finding, Source.RULE, RuleId.SWALLOWED_EXCEPTION, "0123456789abcdef")


def reasons(diff: str, *findings: Finding) -> list[DropReason | None]:
    result = verify_findings(findings, (), parse_diff(diff))
    return [check.drop_reason for check in result.checks]


@pytest.mark.parametrize("line", [10, 11, 12])
def test_verify_findings_keeps_an_added_or_context_line(line: int) -> None:
    result = verify_findings([make_finding(line=line)], (), parse_diff(DIFF))

    [kept] = result.kept
    assert (kept.finding, kept.source, kept.rule) == (make_finding(line=line), Source.AI, None)
    assert result.dropped == ()


@pytest.mark.parametrize("line", [1, 9, 13, 1_000_000])
def test_verify_findings_drops_a_line_outside_the_hunk_without_moving_it(line: int) -> None:
    result = verify_findings([make_finding(line=line)], (), parse_diff(DIFF))

    assert result.kept == ()
    assert [check.drop_reason for check in result.dropped] == [DropReason.LINE_NOT_IN_DIFF]
    assert result.dropped[0].proposed == make_finding(line=line)


@pytest.mark.parametrize(
    "file", ["other.py", "APP.PY", "../app.py", "./a/app.py", "/app.py", "app.py ", "\x1bapp.py"]
)
def test_verify_findings_drops_a_file_not_in_the_diff_as_unknown(file: str) -> None:
    assert reasons(DIFF, make_finding(file=file)) == [DropReason.UNKNOWN_FILE]


@pytest.mark.parametrize("file", ["./app.py", "a/app.py", "b/app.py"])
def test_verify_findings_strips_one_leading_prefix_and_reports_the_diff_path(file: str) -> None:
    result = verify_findings([make_finding(file=file)], (), parse_diff(DIFF))

    assert [reviewed.finding.file for reviewed in result.kept] == ["app.py"]
    assert result.checks[0].proposed.file == file
    assert result.checks[0].diff_path == "app.py"


def test_verify_findings_prefers_an_exact_path_that_has_a_prefix() -> None:
    diff = section("b/x.py") + section("x.py", "+x\n+y\n")

    result = verify_findings([make_finding(file="b/x.py", line=1)], (), parse_diff(diff))

    assert [reviewed.finding.file for reviewed in result.kept] == ["b/x.py"]


def test_verify_findings_does_not_fall_back_when_the_exact_path_misses_the_line() -> None:
    diff = section("b/x.py") + section("x.py", "+x\n+y\n")

    assert reasons(diff, make_finding(file="b/x.py", line=2)) == [DropReason.LINE_NOT_IN_DIFF]


def test_verify_findings_drops_a_skipped_file_even_on_its_lines() -> None:
    result = verify_findings([make_finding(file="README.md", line=1)], (), parse_diff(DIFF))

    assert [check.drop_reason for check in result.checks] == [DropReason.SKIPPED_FILE]
    assert result.checks[0].diff_path == "README.md"


def test_verify_findings_drops_the_old_path_of_a_deleted_file_as_unknown() -> None:
    diff = "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"

    assert reasons(diff, make_finding(file="gone.py", line=1)) == [DropReason.UNKNOWN_FILE]


@pytest.mark.parametrize(
    "hunk",
    [
        "@@ garbage @@\n+a\n b\n",
        "@@ -1,2 +1,0 @@\n-a\n-b\n",
        "@@@ -1,2 -1,2 +1,3 @@@\n++x\n +y\n  z\n",
    ],
    ids=["unreadable_header", "removed_lines_only", "combined_hunk"],
)
@pytest.mark.parametrize("line", [1, 2, 3])
def test_verify_findings_drops_any_line_of_a_hunk_with_no_line_numbers(
    hunk: str, line: int
) -> None:
    diff = f"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n{hunk}"

    assert reasons(diff, make_finding(file="x.py", line=line)) == [DropReason.LINE_NOT_IN_DIFF]


def test_verify_findings_merges_the_lines_of_sections_sharing_a_path() -> None:
    diff = (
        section("x.py") + "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -5 +5 @@\n-a\n+b\n"
    )

    assert reasons(diff, make_finding(file="x.py", line=1), make_finding(file="x.py", line=5)) == [
        None,
        None,
    ]


def test_verify_findings_keeps_valid_findings_in_the_order_proposed_and_checks_each() -> None:
    proposed = [
        make_finding(line=12, rationale="first"),
        make_finding(file="other.py", rationale="dropped"),
        make_finding(line=10, rationale="second"),
    ]

    result = verify_findings(proposed, (), parse_diff(DIFF))

    assert [reviewed.finding.rationale for reviewed in result.kept] == ["first", "second"]
    assert [check.proposed for check in result.checks] == proposed
    assert [check.proposed.rationale for check in result.dropped] == ["dropped"]


def test_verify_findings_puts_rule_findings_first_in_the_order_given() -> None:
    rules = [
        rule_finding(make_finding(file="b.py", line=1)),
        rule_finding(make_finding(file="a.py", line=9)),
    ]

    result = verify_findings([make_finding()], rules, parse_diff(DIFF))

    assert [(item.finding.file, item.finding.line, item.source) for item in result.kept] == [
        ("b.py", 1, Source.RULE),
        ("a.py", 9, Source.RULE),
        ("app.py", 10, Source.AI),
    ]
    assert len(result.checks) == 1


@pytest.mark.parametrize(
    "outcomes",
    [
        {},
        {"drop_reason": DropReason.UNKNOWN_FILE, "fingerprint": "f"},
        {"duplicate_of": "f", "fingerprint": "g"},
    ],
    ids=["none", "dropped_and_kept", "duplicate_and_kept"],
)
def test_proposal_check_needs_exactly_one_outcome(outcomes: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ProposalCheck(make_finding(), "app.py", **outcomes)  # type: ignore[arg-type]


def test_verify_findings_with_nothing_proposed_checks_nothing() -> None:
    result = verify_findings([], (), parse_diff(DIFF))

    assert result.kept == ()
    assert result.checks == ()


def test_verify_findings_fingerprints_a_kept_finding_by_its_anchored_line() -> None:
    result = verify_findings([make_finding(file="./app.py", line=11)], (), parse_diff(DIFF))

    expected = fingerprint_for(file="app.py", rule="ai:logic", anchor_text="b = 3", ordinal=0)
    assert [reviewed.fingerprint for reviewed in result.kept] == [expected]
    assert result.checks[0].fingerprint == expected


def test_verify_findings_keeps_alike_findings_with_different_rationales_apart() -> None:
    proposed = [make_finding(rationale="One."), make_finding(rationale="Two.")]

    result = verify_findings(proposed, (), parse_diff(DIFF))

    assert [reviewed.fingerprint for reviewed in result.kept] == [
        fingerprint_for(file="app.py", rule="ai:logic", anchor_text="a = 1", ordinal=0),
        fingerprint_for(file="app.py", rule="ai:logic", anchor_text="a = 1", ordinal=1),
    ]
    assert result.deduped == ()


def test_verify_findings_keeps_the_first_of_findings_alike_in_all_but_severity_and_prefix() -> None:
    first = make_finding()
    repeat = make_finding(file="b/app.py").model_copy(
        update={"severity": Severity.LOW, "confidence": 0.1}
    )

    result = verify_findings([first, repeat], (), parse_diff(DIFF))

    assert [reviewed.finding for reviewed in result.kept] == [first]
    [duplicate] = result.deduped
    assert duplicate.proposed == repeat
    assert duplicate.duplicate_of == result.kept[0].fingerprint
    assert (duplicate.drop_reason, duplicate.fingerprint) == (None, None)
    assert result.dropped == ()


@pytest.mark.parametrize(
    "other",
    [
        make_finding(line=11),
        make_finding(rationale="Other."),
        make_finding().model_copy(update={"category": Category.SECURITY}),
    ],
    ids=["line", "rationale", "category"],
)
def test_verify_findings_does_not_dedupe_findings_differing_in_an_identifying_part(
    other: Finding,
) -> None:
    result = verify_findings([make_finding(), other], (), parse_diff(DIFF))

    assert len(result.kept) == 2
    assert result.deduped == ()


def test_verify_findings_does_not_count_a_dropped_finding_as_the_first_of_its_kind() -> None:
    proposed = [make_finding(line=99), make_finding(line=99), make_finding()]

    result = verify_findings(proposed, (), parse_diff(DIFF))

    assert len(result.dropped) == 2
    assert result.deduped == ()
    assert [reviewed.fingerprint for reviewed in result.kept] == [
        fingerprint_for(file="app.py", rule="ai:logic", anchor_text="a = 1", ordinal=0)
    ]


def test_verify_findings_keeps_a_rule_findings_own_fingerprint() -> None:
    rule = rule_finding(make_finding(line=10))

    result = verify_findings([make_finding()], [rule], parse_diff(DIFF))

    assert result.kept[0] == rule
    assert result.kept[1].fingerprint == fingerprint_for(
        file="app.py", rule="ai:logic", anchor_text="a = 1", ordinal=0
    )
