"""Unit tests for comparing diff mode with context mode on scored eval cases (P3-S7)."""

import json
from collections.abc import Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final

import pytest

from review_bot.errors import ErrorCategory
from review_bot.evals.compare import Comparison, Rate, compare_cases
from review_bot.evals.compare_render import (
    COMPARE_SCHEMA,
    render_compare_json,
    render_compare_text,
)
from review_bot.evals.formats import EvalCase, EvalMode, Recording
from review_bot.evals.score import (
    CaseEvidence,
    CaseReplays,
    CaseResult,
    RunFindings,
    score_case,
)
from review_bot.findings import Finding
from review_bot.pr.metrics import Prices

FILE: Final = "app/calc.py"
FLOAT_BUG: Final = {
    "id": "float-sum",
    "source": "ai",
    "file": FILE,
    "lines": [[3, 3]],
    "keywords": [["float"]],
    "min_runs_caught": 2,
}
NAME_BUG: Final = FLOAT_BUG | {"id": "bad-name", "lines": [[5, 5]], "keywords": [["name"]]}


def case(case_id: str = "calc", **fields: Any) -> EvalCase:
    data = {"schema": "eval-case/v1", "id": case_id, "diff": "change.diff", "repo": "repo"}
    return EvalCase.model_validate(data | {"bugs": [FLOAT_BUG]} | fields)


def finding(line: int = 3, rationale: str = "A float sum loses cents.") -> Finding:
    return Finding.model_validate(
        {
            "file": FILE,
            "line": line,
            "severity": "low",
            "category": "logic",
            "rationale": rationale,
            "confidence": 0.8,
        }
    )


HIT: Final = (finding(),)
NOISE: Final = (finding(line=4, rationale="Odd spacing."),)


def run(
    findings: Sequence[Finding] = (),
    *,
    cost: str | None = None,
    tokens: tuple[int, int] = (0, 0),
    tool_calls: int = 0,
) -> tuple[RunFindings, dict[str, Any]]:
    """A run that returned ``findings``, as replay scores it and as it was recorded."""
    usage: dict[str, Any] = {"input_tokens": tokens[0], "output_tokens": tokens[1]}
    if cost is not None:
        usage["cost_usd"] = cost
    calls = [{"tool": "read", "outcome": "ok", "result_bytes": 1}] * tool_calls
    recorded = {"findings": [], "usage": usage, "warnings": [], "tool_calls": calls}
    return RunFindings(tuple(findings), tuple(findings), (), tuple(findings)), recorded


def failed(
    category: str, *, cap: str | None = None, cost: str | None = None
) -> tuple[RunFindings, dict[str, Any]]:
    """A run that failed with ``category``, as replay scores it and as it was recorded."""
    usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}
    if cost is not None:
        usage["cost_usd"] = cost
    recorded = {"error": {"category": category, "cap": cap}, "usage": usage, "warnings": []}
    return RunFindings((), (), (), (), error=ErrorCategory(category)), recorded


def result(
    eval_case: EvalCase, mode: EvalMode, runs: Sequence[tuple[RunFindings, dict[str, Any]]]
) -> CaseResult:
    recording = Recording.model_validate(
        {
            "schema": "eval-recording/v1",
            "case_id": eval_case.id,
            "kind": "live",
            "mode": mode,
            "case_sha256": "c",
            "diff_sha256": "d",
            "prompt_version": "p",
            "prompt_sha256": "s",
            "repo_tree": "t" if mode == "context" else None,
            "model": "m",
            "sdk_version": "1",
            "recorded_at": "2026-11-09T10:00:00+00:00",
            "runs": [recorded for _, recorded in runs],
        }
    )
    evidence = CaseEvidence((), tuple(replayed for replayed, _ in runs))
    return CaseResult(eval_case.id, recording, score_case(eval_case, evidence))


def paired(
    eval_case: EvalCase,
    diff_runs: Sequence[tuple[RunFindings, dict[str, Any]]],
    context_runs: Sequence[tuple[RunFindings, dict[str, Any]]],
) -> CaseReplays:
    return CaseReplays(
        eval_case, result(eval_case, "diff", diff_runs), result(eval_case, "context", context_runs)
    )


def compare(*replays: CaseReplays, prices: Prices | None = None) -> Comparison:
    return compare_cases(replays, prices)


# --- Per case -----------------------------------------------------------------------------------


def test_compare_cases_shows_each_ai_bugs_caught_runs_and_whether_it_met_per_mode() -> None:
    eval_case = case(bugs=[FLOAT_BUG, NAME_BUG])

    comparison = compare(paired(eval_case, [run(HIT), run(), run()], [run(HIT), run(HIT)]))

    (compared,) = comparison.cases
    diff, context = compared.modes["diff"], compared.modes["context"]
    assert compared.paired
    assert [(b.bug_id, b.caught_runs, b.runs, b.met) for b in diff.bugs] == [
        ("float-sum", 1, 3, False),
        ("bad-name", 0, 3, False),
    ]
    assert [(b.caught_runs, b.runs, b.met) for b in context.bugs] == [(2, 2, True), (0, 2, False)]
    assert (diff.passed, context.passed) == (False, False)


def test_compare_cases_leaves_out_a_case_without_repo_or_context_recording() -> None:
    eval_case = case(repo=None)

    comparison = compare(CaseReplays(eval_case, result(eval_case, "diff", [run(HIT)]), None))

    assert comparison.cases == ()
    assert comparison.totals["diff"].runs == 0
    assert comparison.passed


def test_compare_cases_lists_a_repo_case_missing_a_recording_as_unpaired_and_fails() -> None:
    eval_case = case()

    comparison = compare(CaseReplays(eval_case, result(eval_case, "diff", [run(HIT)]), None))

    (compared,) = comparison.cases
    assert not compared.paired
    assert compared.reason == "context: no live recording"
    assert compared.modes == {}
    assert comparison.totals["diff"].runs == 0
    assert not comparison.passed


def test_compare_cases_gives_a_stale_recordings_problem_as_the_reason() -> None:
    eval_case = case()
    stale = CaseResult(eval_case.id, result(eval_case, "diff", [run()]).recording, None, ("old",))

    comparison = compare(CaseReplays(eval_case, stale, None))

    assert comparison.cases[0].reason == "diff: old; context: no live recording"


def test_compare_cases_fails_with_an_unpaired_context_recording_without_repo() -> None:
    eval_case = case(repo=None)

    comparison = compare(CaseReplays(eval_case, None, result(eval_case, "context", [run()])))

    assert comparison.cases[0].reason == "diff: no live recording"
    assert not comparison.passed


def test_compare_cases_passes_a_paired_case_without_repo() -> None:
    eval_case = case(repo=None)

    comparison = compare(paired(eval_case, [run(HIT)], [run(HIT)]))

    assert comparison.cases[0].paired
    assert comparison.passed


# --- Totals -------------------------------------------------------------------------------------


def test_compare_cases_totals_each_mode_over_its_own_runs() -> None:
    clean = case(
        "clean",
        bugs=[],
        noise_budget={"max_findings": 1, "max_severity": "low"},
        min_passing_runs=1,
    )
    clean_runs = [run(NOISE), run(NOISE + NOISE), run()]

    comparison = compare(
        paired(case(), [run(HIT + NOISE)] * 4 + [run()], [run(HIT)] * 3),
        paired(clean, clean_runs, clean_runs[:2]),
    )

    diff, context = comparison.totals["diff"], comparison.totals["context"]
    assert (diff.runs, context.runs) == (8, 5)
    assert diff.ai_bugs_caught == Rate(1, 1)
    assert diff.credited_runs == Rate(4, 5)
    assert context.credited_runs == Rate(3, 3)
    # The clean case has no bugs, so its findings count as noise but not towards precision.
    assert diff.precision == Rate(4, 8)
    assert context.precision == Rate(3, 3)
    assert diff.findings_per_run == Rate(3, 3)
    assert context.findings_per_run == Rate(3, 2)
    assert diff.injection_runs_passed.value is None


def test_compare_cases_counts_a_failed_run_as_a_run_without_findings_by_category() -> None:
    diff_runs = [run(HIT), failed("invalid_output"), failed("refusal")]

    comparison = compare(paired(case(), diff_runs, [run(HIT)]))

    diff = comparison.totals["diff"]
    assert diff.credited_runs == Rate(1, 3)
    assert diff.failed_runs == {"invalid_output": 1, "refusal": 1}
    assert diff.cap_hits == ()


def test_compare_cases_lists_cap_hits_as_misses_and_failed_injection_runs() -> None:
    injection = case("inject", injection={"canary": "C"}, min_passing_runs=1)
    context_runs = [run(HIT), failed("transient", cap="turns"), failed("transient")]

    comparison = compare(paired(injection, [run(HIT)], context_runs))

    context = comparison.totals["context"]
    assert [(hit.case_id, hit.run, hit.cap) for hit in context.cap_hits] == [("inject", 2, "turns")]
    assert context.credited_runs == Rate(1, 3)
    assert context.injection_runs_passed == Rate(1, 3)
    assert comparison.verdict.context_injection_passed is False


def test_compare_cases_counts_tokens_and_tool_calls() -> None:
    context_runs = [run(tokens=(100, 10), tool_calls=2), run(tokens=(300, 30), tool_calls=5)]

    totals = compare(paired(case(), [run()], context_runs)).totals["context"]

    assert (totals.input_tokens_per_run, totals.output_tokens_per_run) == (
        Rate(400, 2),
        Rate(40, 2),
    )
    assert (totals.tool_calls_per_run, totals.most_tool_calls) == (Rate(7, 2), 5)


# --- Cost ---------------------------------------------------------------------------------------


def test_compare_cases_sums_the_reported_cost_when_every_run_has_one() -> None:
    comparison = compare(paired(case(), [run(cost="0.01")], [run(cost="0.20"), run(cost="0.25")]))

    cost = comparison.totals["context"].cost
    assert cost is not None
    assert (cost.basis, cost.total_usd, cost.mean_usd) == (
        "recorded",
        Decimal("0.45"),
        Decimal("0.225"),
    )


def test_compare_cases_estimates_the_cost_from_tokens_when_a_run_has_none() -> None:
    runs = [run(cost="9.99", tokens=(1_000_000, 0)), run(tokens=(0, 1_000_000))]
    prices = Prices(Decimal(3), Decimal(15))

    cost = compare(paired(case(), runs, runs), prices=prices).totals["diff"].cost

    assert cost is not None
    assert (cost.basis, cost.total_usd, cost.mean_usd) == ("estimated", Decimal(18), Decimal(9))


@pytest.mark.parametrize(
    ("runs", "prices"),
    [([run(cost="0.1"), run()], None), ([], Prices(Decimal(3), Decimal(15)))],
    ids=["some-cost-missing-without-prices", "no-runs"],
)
def test_compare_cases_leaves_the_cost_unrecorded(
    runs: list[tuple[RunFindings, dict[str, Any]]], prices: Prices | None
) -> None:
    replays = [paired(case(), runs, runs)] if runs else []

    comparison = compare_cases(replays, prices)

    assert comparison.totals["diff"].cost is None


# --- Verdict ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("diff_findings", "context_findings", "expected"),
    [
        (HIT + NOISE, HIT, "higher"),
        (HIT, HIT, "same"),
        (HIT, HIT + NOISE, "lower"),
        ((), HIT, None),
    ],
)
def test_compare_cases_states_how_context_precision_compares(
    diff_findings: tuple[Finding, ...], context_findings: tuple[Finding, ...], expected: str | None
) -> None:
    comparison = compare(paired(case(), [run(diff_findings)], [run(context_findings)]))

    assert comparison.verdict.precision == expected


def test_compare_cases_names_bugs_caught_in_a_smaller_share_not_count() -> None:
    # 2 of 5 is a larger share than 1 of 3; 3 of 5 is a smaller one than 2 of 3.
    smaller = case("smaller")
    larger = case("larger")

    comparison = compare(
        paired(smaller, [run(HIT)] * 2 + [run()] * 3, [run(HIT), run(), run()]),
        paired(larger, [run(HIT)] * 3 + [run()] * 2, [run(HIT), run(HIT), run()]),
    )

    assert comparison.verdict.smaller_share == (("smaller", "float-sum"),)


def test_compare_cases_has_no_injection_verdict_without_injection_runs() -> None:
    comparison = compare(paired(case(), [run(HIT)], [run(HIT)]))

    assert comparison.verdict.context_injection_passed is None


# --- Rendering ----------------------------------------------------------------------------------


def sample_comparison() -> Comparison:
    eval_case = case()
    return compare(
        paired(
            eval_case,
            [run(HIT, cost="0.004", tokens=(100, 10)), failed("refusal", cost="0.002")],
            [run(HIT, cost="0.30", tool_calls=3), failed("transient", cap="cost", cost="0.61")],
        ),
        CaseReplays(case("lonely"), None, None),
    )


def test_render_compare_text_shows_cases_totals_and_verdict() -> None:
    text = render_compare_text(sample_comparison())

    assert "case calc: paired, diff fail, context fail\n" in text
    assert "  float-sum: diff 1/2 not met, context 1/2 not met\n" in text
    assert "case lonely: unpaired: diff: no live recording; context: no live recording\n" in text
    assert (
        "diff mode: 2 runs\n"
        "  ai bugs caught: 0/1 (0.0%)\n"
        "  credited runs: 1/2 (50.0%)\n"
        "  precision: 1/1 (100.0%)\n"
        "  findings per run with a noise budget: not measured\n"
        "  injection runs passed: not measured\n"
        "  cost: total $0.0060, mean $0.0030 per run (recorded)\n"
        "  tokens per run: input 50.0, output 5.0\n"
        "  failed runs: 1 of 2 (refusal 1)\n"
        "  cap hits: none\n"
    ) in text
    assert "  tool calls per run: mean 1.5, largest 3\n" in text
    assert "  cap hits: calc run 2 (cost)\n" in text
    assert text.endswith(
        "verdict:\n"
        "  precision in context mode: same\n"
        "  ai bugs caught in a smaller share of runs in context mode: none\n"
        "  every context injection run passed: not measured"
    )


def test_render_compare_text_says_what_isnt_recorded_or_measured() -> None:
    text = render_compare_text(compare())

    assert "  cost: not recorded\n" in text
    assert "  precision: not measured\n" in text
    assert "  precision in context mode: not measured\n" in text
    assert "  failed runs: 0 of 0\n" in text


def test_render_compare_json_holds_what_the_text_shows() -> None:
    output = render_compare_json(sample_comparison())

    document = json.loads(output)
    assert output.isascii()
    assert "\n" not in output
    assert document["schema"] == COMPARE_SCHEMA
    calc, lonely = document["cases"]
    assert calc["modes"]["diff"] == {
        "passed": False,
        "runs": 2,
        "bugs": [{"id": "float-sum", "caught_runs": 1, "runs": 2, "met": False}],
    }
    assert lonely == {
        "id": "lonely",
        "paired": False,
        "reason": "diff: no live recording; context: no live recording",
        "modes": None,
    }
    diff, context = document["totals"]["diff"], document["totals"]["context"]
    assert diff["credited_runs"] == {"credited": 1, "runs": 2, "share": 0.5}
    assert diff["cost"] == {"basis": "recorded", "total_usd": "0.0060", "mean_usd": "0.0030"}
    assert diff["tool_calls"] is None
    assert context["tool_calls"] == {"mean": 1.5, "max": 3}
    assert context["cap_hits"] == [{"case": "calc", "run": 2, "cap": "cost"}]
    assert diff["failed_runs"] == {"refusal": 1}
    assert document["verdict"] == {
        "precision": "same",
        "smaller_share": [],
        "context_injection_passed": None,
    }


def test_render_compare_json_writes_an_unrecorded_cost_with_null_amounts() -> None:
    document = json.loads(render_compare_json(compare()))

    assert document["totals"]["diff"]["cost"] == {
        "basis": "not recorded",
        "total_usd": None,
        "mean_usd": None,
    }
    diff = document["totals"]["diff"]
    assert diff["precision"]["share"] is None
    assert diff["findings_per_run"] == {"findings": 0, "runs": 0, "mean": None}
    assert diff["injection_runs"] == {"passed": 0, "runs": 0, "share": None}
    assert diff["tokens"] == {"mean_input": None, "mean_output": None}
    assert document["verdict"]["context_injection_passed"] is None


def test_render_compare_text_escapes_a_bug_id_that_would_forge_a_line() -> None:
    forged = case(bugs=[FLOAT_BUG | {"id": "float\nverdict: forged"}])

    text = render_compare_text(compare(paired(forged, [run(HIT)], [run()])))

    assert "  float\\nverdict: forged: diff 1/1" in text
    assert "calc/float\\nverdict: forged" in text


@pytest.mark.parametrize(("rate", "value"), [(Rate(2, 3), Fraction(2, 3)), (Rate(0, 0), None)])
def test_rate_is_not_measured_only_with_nothing_to_measure_it_on(
    rate: Rate, value: Fraction | None
) -> None:
    assert rate.value == value


def test_render_compare_text_shows_findings_per_run_and_injection_runs_when_measured() -> None:
    clean = case(
        "clean",
        bugs=[FLOAT_BUG],
        noise_budget={"max_findings": 1, "max_severity": "low"},
        injection={"canary": "C"},
        min_passing_runs=1,
    )
    runs = [run(HIT + NOISE), run(HIT), run(NOISE)]

    text = render_compare_text(compare(paired(clean, runs, runs)))

    assert "  findings per run with a noise budget: 1.3\n" in text
    assert "  injection runs passed: 2/3 (66.7%)\n" in text
    assert "  every context injection run passed: no" in text
