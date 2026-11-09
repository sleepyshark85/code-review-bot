import time
from dataclasses import replace

import pytest
from diff_samples import section

from review_bot.context.ledger import LineSpan, ToolCall, ToolName, ToolOutcome
from review_bot.diff_parser import parse_diff
from review_bot.findings import (
    Category,
    EvidenceSpan,
    Finding,
    ReviewedFinding,
    RuleId,
    Severity,
    Source,
)
from review_bot.fingerprint import Fingerprinter, fingerprint_for
from review_bot.suggestion import SuggestionDropReason
from review_bot.verify import DropReason, ProposalCheck, VerifyResult, verify_findings

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


# `app.py` has hunk 1 with lines 1 and 2, and hunk 2 with lines 20 to 28; the rule finding is on
# line 24, the `except` clause.
RULE_DIFF = (
    "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
    "@@ -1 +1,2 @@\n a = 1\n+b = 2\n"
    "@@ -18,0 +20,9 @@\n+try:\n+    x()\n+    y()\n+    z()\n+except Exception:\n+    pass\n"
    "+w()\n+v()\n+u()\n"
)
SWALLOWED = RuleId.SWALLOWED_EXCEPTION
RULE_FINGERPRINT = fingerprint_for(
    file="app.py", rule=SWALLOWED, anchor_text="except Exception:", ordinal=0
)


def swallowed_rule_finding(file: str = "app.py", line: int = 24) -> ReviewedFinding:
    finding = make_finding(file=file, line=line).model_copy(update={"severity": Severity.MEDIUM})
    return ReviewedFinding(finding, Source.RULE, SWALLOWED, RULE_FINGERPRINT)


def tagged_finding(
    line: int = 24, severity: Severity = Severity.MEDIUM, rule: RuleId | None = SWALLOWED
) -> Finding:
    return make_finding(line=line).model_copy(update={"severity": severity, "rule": rule})


def verify_against_rule(*proposed: Finding, rule: ReviewedFinding | None = None) -> VerifyResult:
    rule_findings = [swallowed_rule_finding() if rule is None else rule]
    return verify_findings(proposed, rule_findings, parse_diff(RULE_DIFF))


@pytest.mark.parametrize("line", [21, 23, 24, 25, 27])
@pytest.mark.parametrize("severity", [Severity.LOW, Severity.MEDIUM])
def test_verify_findings_dedupes_a_tagged_finding_near_the_rule_finding(
    line: int, severity: Severity
) -> None:
    result = verify_against_rule(tagged_finding(line=line, severity=severity))

    assert [reviewed.source for reviewed in result.kept] == [Source.RULE]
    [duplicate] = result.deduped
    assert duplicate.duplicate_of == RULE_FINGERPRINT


@pytest.mark.parametrize("line", [20, 28, 2], ids=["4_above", "4_below", "other_hunk"])
def test_verify_findings_keeps_a_tagged_finding_too_far_from_the_rule_finding(line: int) -> None:
    result = verify_against_rule(tagged_finding(line=line))

    assert len(result.kept) == 2
    assert result.deduped == ()


@pytest.mark.parametrize("severity", [Severity.HIGH, Severity.CRITICAL])
def test_verify_findings_keeps_a_tagged_finding_more_severe_than_the_rule_finding(
    severity: Severity,
) -> None:
    result = verify_against_rule(tagged_finding(severity=severity))

    assert [reviewed.finding.severity for reviewed in result.kept] == [Severity.MEDIUM, severity]
    assert result.deduped == ()


@pytest.mark.parametrize(
    "rule", [None, RuleId.OPEN_WITHOUT_ENCODING], ids=["untagged", "other_rule"]
)
def test_verify_findings_keeps_a_finding_not_tagged_with_the_rule_on_its_line(
    rule: RuleId | None,
) -> None:
    result = verify_against_rule(tagged_finding(rule=rule))

    assert len(result.kept) == 2
    assert result.deduped == ()


def test_verify_findings_keeps_a_tagged_finding_in_another_file_than_the_rule_finding() -> None:
    result = verify_against_rule(tagged_finding(), rule=swallowed_rule_finding(file="other.py"))

    assert len(result.kept) == 2
    assert result.deduped == ()


def test_verify_findings_drops_a_tagged_finding_with_a_bad_line_instead_of_deduping_it() -> None:
    result = verify_against_rule(tagged_finding(line=29))

    assert [check.drop_reason for check in result.checks] == [DropReason.LINE_NOT_IN_DIFF]
    assert result.deduped == ()


def test_verify_findings_fingerprints_a_tagged_finding_by_its_rule_id_after_the_rules() -> None:
    result = verify_against_rule(tagged_finding(severity=Severity.HIGH))

    [_, kept] = result.kept
    assert kept.rule is SWALLOWED
    assert kept.fingerprint == fingerprint_for(
        file="app.py", rule=SWALLOWED, anchor_text="except Exception:", ordinal=1
    )
    assert kept.fingerprint != RULE_FINGERPRINT


def test_verify_findings_fingerprints_an_untagged_finding_by_its_category() -> None:
    result = verify_against_rule(tagged_finding(rule=None))

    [_, kept] = result.kept
    assert kept.rule is None
    assert kept.fingerprint == fingerprint_for(
        file="app.py", rule="ai:logic", anchor_text="except Exception:", ordinal=0
    )


def test_verify_findings_dedupes_alike_tagged_findings_away_from_any_rule_finding() -> None:
    result = verify_against_rule(tagged_finding(line=2), tagged_finding(line=2))

    assert len(result.kept) == 2
    [duplicate] = result.deduped
    assert duplicate.duplicate_of == result.kept[1].fingerprint


def test_verify_findings_dedupes_many_tagged_findings_against_many_rule_findings_quickly() -> None:
    # One hunk adding 20,000 lines, a rule finding on each, and 4,000 tagged model findings: half
    # at `low`, which repeat the rule finding on their line, and half at `high`, which don't.
    line_count, tagged_count = 20_000, 4_000
    parsed = parse_diff(section("app.py", "+x\n" * line_count))
    fingerprinter = Fingerprinter()
    rules = [
        ReviewedFinding(
            make_finding(line=line).model_copy(update={"severity": Severity.MEDIUM}),
            Source.RULE,
            SWALLOWED,
            fingerprinter.fingerprint(file="app.py", rule=SWALLOWED, anchor_text="x"),
        )
        for line in range(1, line_count + 1)
    ]
    proposed = [
        tagged_finding(line=line, severity=Severity.LOW if line % 2 else Severity.HIGH)
        for line in range(1, tagged_count + 1)
    ]

    started = time.perf_counter()
    result = verify_findings(proposed, rules, parsed)
    elapsed = time.perf_counter() - started

    assert (len(result.deduped), len(result.kept)) == (
        tagged_count // 2,
        line_count + tagged_count // 2,
    )
    assert len({reviewed.fingerprint for reviewed in result.kept}) == len(result.kept)
    assert elapsed < 5.0


def test_verify_findings_names_the_lowest_matching_rule_finding_as_the_one_repeated() -> None:
    below = replace(swallowed_rule_finding(line=26), fingerprint="bbbbbbbbbbbbbbbb")
    above = replace(swallowed_rule_finding(line=22), fingerprint="aaaaaaaaaaaaaaaa")

    result = verify_findings([tagged_finding()], [below, above], parse_diff(RULE_DIFF))

    assert [check.duplicate_of for check in result.deduped] == ["aaaaaaaaaaaaaaaa"]


def test_ai_anchor_valid_keeps_duplicates_and_leaves_out_dropped_findings() -> None:
    first = make_finding(line=11)
    repeat = make_finding(line=11)
    missed = make_finding(line=99)

    result = verify_findings([first, missed, repeat], (), parse_diff(DIFF))

    assert [check.proposed for check in result.ai_anchor_valid] == [first, repeat]
    assert len(result.deduped) == 1


def cited(file: str, start: int, end: int) -> EvidenceSpan:
    return EvidenceSpan(file=file, start_line=start, end_line=end)


def read_call(path: str, first: int, last: int) -> ToolCall:
    return ToolCall(
        ToolName.READ, ToolOutcome.OK, None, (LineSpan(path, first, last),), (), 10, "0" * 64
    )


def with_evidence(*spans: EvidenceSpan, line: int = 10) -> Finding:
    return make_finding(line=line).model_copy(update={"evidence": spans})


def test_verify_findings_keeps_only_the_shown_evidence_of_a_kept_finding() -> None:
    finding = with_evidence(cited("lib.py", 2, 3), cited("lib.py", 4, 9), cited("app.py", 11, 12))

    result = verify_findings([finding], (), parse_diff(DIFF), ledger=[read_call("lib.py", 1, 5)])

    [kept] = result.kept
    assert kept.finding.evidence == (cited("lib.py", 2, 3), cited("app.py", 11, 12))
    assert kept.evidence_dropped == 1
    assert result.checks[0].evidence.dropped == 1


def test_verify_findings_checks_evidence_against_the_diff_alone_without_a_ledger() -> None:
    finding = with_evidence(cited("app.py", 10, 12), cited("app.py", 9, 10), cited("lib.py", 1, 1))

    [kept] = verify_findings([finding], (), parse_diff(DIFF)).kept

    assert kept.finding.evidence == (cited("app.py", 10, 12),)
    assert kept.evidence_dropped == 2


@pytest.mark.parametrize(("is_diff_head", "dropped"), [(True, 0), (False, 1)])
def test_verify_findings_counts_tool_lines_of_a_skipped_file_only_at_the_diffs_head(
    is_diff_head: bool, dropped: int
) -> None:
    finding = with_evidence(cited("README.md", 1, 1))
    ledger = [read_call("README.md", 1, 5)]

    result = verify_findings(
        [finding], (), parse_diff(DIFF), ledger=ledger, is_diff_head=is_diff_head
    )

    assert result.kept[0].evidence_dropped == dropped


def test_verify_findings_never_counts_a_skipped_files_diff_lines_as_evidence() -> None:
    finding = with_evidence(cited("README.md", 1, 1))

    assert verify_findings([finding], (), parse_diff(DIFF)).kept[0].evidence_dropped == 1


@pytest.mark.parametrize(("is_diff_head", "kept_count"), [(True, 1), (False, 0)])
def test_verify_findings_counts_tool_lines_of_a_changed_file_only_at_the_diffs_head(
    is_diff_head: bool, kept_count: int
) -> None:
    finding = with_evidence(cited("app.py", 1, 5))
    ledger = [read_call("app.py", 1, 20)]

    result = verify_findings(
        [finding], (), parse_diff(DIFF), ledger=ledger, is_diff_head=is_diff_head
    )

    assert len(result.kept[0].finding.evidence) == kept_count


def test_verify_findings_checks_the_evidence_of_dropped_and_duplicate_findings() -> None:
    first = with_evidence(cited("app.py", 11, 11))
    duplicate = with_evidence(cited("app.py", 11, 11), cited("app.py", 1, 1))
    missed = with_evidence(cited("app.py", 1, 1), line=99)

    result = verify_findings([first, duplicate, missed], (), parse_diff(DIFF))

    assert [(len(c.evidence.kept), c.evidence.dropped) for c in result.checks] == [
        (1, 0),
        (1, 1),
        (0, 1),
    ]


def with_suggestion(suggestion: str, *, line: int = 11) -> Finding:
    return make_finding(line=line).model_copy(update={"suggestion": suggestion})


def test_verify_findings_keeps_a_requested_suggestion_for_an_added_line() -> None:
    result = verify_findings(
        [with_suggestion("b = 4")], (), parse_diff(DIFF), suggestions_requested=True
    )

    [kept] = result.kept
    assert (kept.finding.suggestion, kept.suggestion_dropped) == ("b = 4", None)


@pytest.mark.parametrize(
    ("finding", "requested", "reason"),
    [
        (with_suggestion("b = 4"), False, SuggestionDropReason.NOT_REQUESTED),
        (with_suggestion("a = 2", line=10), True, SuggestionDropReason.NOT_ADDED_LINE),
        (with_suggestion("b = 3 "), True, SuggestionDropReason.UNCHANGED),
    ],
    ids=["diff-mode", "context-line", "unchanged"],
)
def test_verify_findings_drops_a_suggestion_with_its_reason_and_keeps_the_finding(
    finding: Finding, requested: bool, reason: SuggestionDropReason
) -> None:
    result = verify_findings([finding], (), parse_diff(DIFF), suggestions_requested=requested)

    [kept] = result.kept
    assert (kept.finding.suggestion, kept.suggestion_dropped) == (None, reason)
    assert kept.fingerprint == result.checks[0].fingerprint


def test_verify_findings_leaves_a_finding_without_a_suggestion_with_no_reason() -> None:
    [kept] = verify_findings([make_finding()], (), parse_diff(DIFF)).kept

    assert (kept.finding.suggestion, kept.suggestion_dropped) == (None, None)


def test_verify_findings_records_the_suggestion_reason_on_the_kept_check_only() -> None:
    kept = with_suggestion("b = 3")
    duplicate = with_suggestion("b = 3")
    missed = with_suggestion("b = 3", line=99)

    result = verify_findings(
        [kept, duplicate, missed], (), parse_diff(DIFF), suggestions_requested=True
    )

    assert [check.suggestion_dropped for check in result.checks] == [
        SuggestionDropReason.UNCHANGED,
        None,
        None,
    ]
