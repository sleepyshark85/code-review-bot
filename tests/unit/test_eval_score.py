"""Unit tests for `score_case`: per-bug counts, thresholds, failed runs and unmatched findings."""

from typing import Any

import pytest
from eval_cases import AI_BUG, CALC_FILE, RULE_BUG

from review_bot.errors import ErrorCategory
from review_bot.evals.formats import EvalCase
from review_bot.evals.score import CaseEvidence, CaseResult, RunFindings, score_case
from review_bot.findings import Category, Finding, Severity, Source


def case(**fields: Any) -> EvalCase:
    data = {"schema": "eval-case/v1", "id": "calc", "diff": "d", "bugs": [RULE_BUG, AI_BUG]}
    return EvalCase.model_validate(data | fields)


def make_finding(line: int = 3, rationale: str = "Summing floats loses cents.") -> Finding:
    return Finding(
        file=CALC_FILE,
        line=line,
        severity=Severity.MEDIUM,
        category=Category.LOGIC,
        rationale=rationale,
        confidence=0.8,
    )


RULE_HIT = make_finding(4, "Exception swallowed.")
AI_HIT = make_finding()
AI_MISS = make_finding(2, "Unclear name.")


def run(*findings: Finding) -> RunFindings:
    return RunFindings(anchor_valid=findings, final=findings)


FAILED = RunFindings(anchor_valid=(), final=(), error=ErrorCategory.REFUSAL)


def test_score_case_with_no_runs_scores_rules_alone_and_leaves_ai_unmeasured() -> None:
    score = score_case(case(), CaseEvidence((RULE_HIT,), ()))

    rule, ai = score.bugs
    assert (rule.caught, rule.out_of, rule.passed) == (1, 1, True)
    assert not ai.is_measured
    assert score.recall(Source.RULE) == (1, 1)
    assert score.recall(Source.AI) is None
    assert score.problems == ()


def test_score_case_fails_a_missed_rule_bug() -> None:
    score = score_case(case(), CaseEvidence((), ()))

    assert score.recall(Source.RULE) == (0, 1)
    assert score.problems == ("rule bug swallowed wasn't caught",)


@pytest.mark.parametrize(("hits", "passes"), [(1, False), (2, True)])
def test_score_case_needs_an_ai_bug_in_min_runs_caught_runs(hits: int, passes: bool) -> None:
    runs = (*[run(AI_HIT)] * hits, *[run(AI_MISS)] * (3 - hits))

    score = score_case(case(), CaseEvidence((RULE_HIT,), runs))

    ai = score.bugs[1]
    assert (ai.caught, ai.out_of, ai.needed, ai.passed) == (hits, 3, 2, passes)
    expected = () if passes else (f"ai bug float-sum was caught in {hits} of 3 runs; it needs 2",)
    assert score.problems == expected


def test_score_case_counts_a_run_once_however_many_findings_match() -> None:
    score = score_case(case(), CaseEvidence((RULE_HIT,), (run(AI_HIT, AI_HIT),)))

    ai = score.bugs[1]
    assert ai.caught == 1
    assert [credit.run for credit in ai.credits] == [1, 1]


def test_score_case_scores_recall_before_dedup_and_the_final_column_after_it() -> None:
    deduped = RunFindings(anchor_valid=(AI_HIT,), final=())

    ai = score_case(case(), CaseEvidence((RULE_HIT,), (deduped, run(AI_HIT)))).bugs[1]

    assert (ai.caught, ai.caught_final) == (2, 1)


def test_score_case_lists_a_failed_run_by_category_and_it_catches_nothing() -> None:
    score = score_case(case(), CaseEvidence((RULE_HIT,), (run(AI_HIT), FAILED)))

    assert score.failed_runs == ((2, ErrorCategory.REFUSAL),)
    assert score.bugs[1].caught == 1


@pytest.mark.parametrize(("failed", "passes"), [(1, True), (2, False)])
def test_score_case_needs_min_passing_runs_and_a_failed_run_never_passes(
    failed: int, passes: bool
) -> None:
    runs = (*[run(AI_HIT)] * (3 - failed), *[FAILED] * failed)

    score = score_case(case(min_passing_runs=2), CaseEvidence((RULE_HIT,), runs))

    passing_problem = f"{3 - failed} of 3 runs passed; the case needs 2"
    assert (passing_problem in score.problems) is not passes


def test_score_case_lists_findings_that_credit_no_bug_of_their_source() -> None:
    # An AI finding on the rule bug's line credits only AI bugs, so it is unmatched.
    ai_on_rule_line = make_finding(4, "Exception swallowed.")
    stray_rule = make_finding(2, "Another rule hit.")

    score = score_case(
        case(), CaseEvidence((RULE_HIT, stray_rule), (run(AI_HIT, ai_on_rule_line),))
    )

    assert [(credit.run, credit.finding) for credit in score.unmatched] == [
        (None, stray_rule),
        (1, ai_on_rule_line),
    ]


def test_case_result_fails_on_its_own_problems_or_its_scores() -> None:
    clean = score_case(case(), CaseEvidence((RULE_HIT,), ()))
    missed = score_case(case(), CaseEvidence((), ()))

    assert CaseResult("calc", None, clean).passed
    assert not CaseResult("calc", None, clean, ("stale",)).passed
    assert CaseResult("calc", None, missed).all_problems == ("rule bug swallowed wasn't caught",)
    assert not CaseResult("calc", None, None, ("stale",)).passed
