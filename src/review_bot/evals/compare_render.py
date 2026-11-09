"""The ``compare`` report: as text for a person, or as the ``eval-compare/v1`` JSON document.

Pure. Both show the same figures: shares are percentages in text and fractions in JSON, and
amounts of money are rounded half-even to a hundredth of a cent, since a diff-mode run can cost
less than a cent. A rate with nothing to measure it on (``Rate.value`` None) is ``not measured``
in text and null in JSON. IDs from case files are escaped in text, as the replay report escapes
them.
"""

import json
from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from review_bot.evals.compare import (
    BugRuns,
    CaseComparison,
    Comparison,
    Cost,
    ModeCase,
    ModeTotals,
    Rate,
    Verdict,
)
from review_bot.evals.formats import MODES, EvalMode, RunErrorCategory
from review_bot.render import escape_unprintable

__all__ = ["COMPARE_NOTICE", "COMPARE_SCHEMA", "render_compare_json", "render_compare_text"]

COMPARE_SCHEMA: Final = "eval-compare/v1"
COMPARE_NOTICE: Final = (
    "Compare replays each case's live diff-mode and context-mode recordings through today's "
    "pipeline. It calls no model, and a person confirms each credit."
)
_NOT_RECORDED: Final = "not recorded"
_NOT_MEASURED: Final = "not measured"
_USD_PLACES: Final = Decimal("0.0001")
_FRACTION_PLACES: Final = 3
_MEAN_PLACES: Final = 1


def render_compare_text(comparison: Comparison) -> str:
    """Each case, then each mode's totals, then the verdict."""
    lines = [COMPARE_NOTICE]
    for case in comparison.cases:
        lines += ["", *_case_lines(case)]
    for mode in MODES:
        lines += ["", *_totals_lines(mode, comparison.totals[mode])]
    lines += ["", *_verdict_lines(comparison.verdict)]
    return "\n".join(lines)


def render_compare_json(comparison: Comparison) -> str:
    """The ``eval-compare/v1`` document, as one line of ASCII JSON."""
    document = {
        "schema": COMPARE_SCHEMA,
        "cases": [_case_document(case) for case in comparison.cases],
        "totals": {mode: _totals_document(mode, comparison.totals[mode]) for mode in MODES},
        "verdict": _verdict_document(comparison.verdict),
    }
    return json.dumps(document, ensure_ascii=True, allow_nan=False)


def _case_lines(case: CaseComparison) -> list[str]:
    case_id = escape_unprintable(case.case_id)
    if not case.paired:
        return [f"case {case_id}: unpaired: {case.reason}"]
    diff, context = case.modes["diff"], case.modes["context"]
    lines = [f"case {case_id}: paired, diff {_passed_text(diff)}, context {_passed_text(context)}"]
    lines += [
        f"  {escape_unprintable(diff_bug.bug_id)}: {_bug_text('diff', diff_bug)}, "
        f"{_bug_text('context', context_bug)}"
        for diff_bug, context_bug in zip(diff.bugs, context.bugs, strict=True)
    ]
    return lines


def _bug_text(mode: EvalMode, bug: BugRuns) -> str:
    """``diff 2/5 met``: the runs that caught the bug in ``mode``, and whether that's enough."""
    return f"{mode} {bug.caught_runs}/{bug.runs} {'met' if bug.met else 'not met'}"


def _passed_text(mode_case: ModeCase) -> str:
    return "pass" if mode_case.passed else "fail"


def _totals_lines(mode: EvalMode, totals: ModeTotals) -> list[str]:
    lines = [
        f"{mode} mode: {totals.runs} runs",
        f"  ai bugs caught: {_share_text(totals.ai_bugs_caught)}",
        f"  credited runs: {_share_text(totals.credited_runs)}",
        f"  precision: {_share_text(totals.precision)}",
        f"  findings per run with a noise budget: {_mean_text(totals.findings_per_run)}",
        f"  injection runs passed: {_share_text(totals.injection_runs_passed)}",
        f"  cost: {_cost_text(totals.cost)}",
        f"  tokens per run: input {_mean_text(totals.input_tokens_per_run)}, "
        f"output {_mean_text(totals.output_tokens_per_run)}",
    ]
    if mode == "context":
        lines.append(
            f"  tool calls per run: mean {_mean_text(totals.tool_calls_per_run)}, "
            f"largest {totals.most_tool_calls}"
        )
    lines.append(f"  failed runs: {_failed_text(totals.failed_runs, totals.runs)}")
    cap_hits = [
        f"{escape_unprintable(hit.case_id)} run {hit.run} ({hit.cap})" for hit in totals.cap_hits
    ]
    lines.append(f"  cap hits: {', '.join(cap_hits) or 'none'}")
    return lines


def _share_text(rate: Rate) -> str:
    """``3/4 (75.0%)``, or not measured."""
    share = rate.value
    if share is None:
        return _NOT_MEASURED
    return f"{rate.part}/{rate.whole} ({float(share) * 100:.1f}%)"


def _mean_text(rate: Rate) -> str:
    """``0.7`` for 2 over 3 runs, or not measured."""
    mean = _rounded(rate)
    return _NOT_MEASURED if mean is None else f"{mean:.{_MEAN_PLACES}f}"


def _cost_text(cost: Cost | None) -> str:
    if cost is None:
        return _NOT_RECORDED
    return f"total ${_usd(cost.total_usd)}, mean ${_usd(cost.mean_usd)} per run ({cost.basis})"


def _failed_text(failed_runs: Mapping[RunErrorCategory, int], runs: int) -> str:
    total = sum(failed_runs.values())
    if not total:
        return f"0 of {runs}"
    by_category = ", ".join(f"{category} {count}" for category, count in failed_runs.items())
    return f"{total} of {runs} ({by_category})"


def _verdict_lines(verdict: Verdict) -> list[str]:
    smaller = [
        escape_unprintable(f"{case_id}/{bug_id}") for case_id, bug_id in verdict.smaller_share
    ]
    injection = verdict.context_injection_passed
    return [
        "verdict:",
        f"  precision in context mode: {verdict.precision or _NOT_MEASURED}",
        "  ai bugs caught in a smaller share of runs in context mode: "
        f"{', '.join(smaller) or 'none'}",
        "  every context injection run passed: "
        f"{_NOT_MEASURED if injection is None else ('yes' if injection else 'no')}",
    ]


def _case_document(case: CaseComparison) -> dict[str, object]:
    return {
        "id": case.case_id,
        "paired": case.paired,
        "reason": case.reason,
        "modes": (
            {mode: _mode_case_document(case.modes[mode]) for mode in MODES} if case.paired else None
        ),
    }


def _mode_case_document(mode_case: ModeCase) -> dict[str, object]:
    return {
        "passed": mode_case.passed,
        "runs": mode_case.runs,
        "bugs": [
            {"id": bug.bug_id, "caught_runs": bug.caught_runs, "runs": bug.runs, "met": bug.met}
            for bug in mode_case.bugs
        ],
    }


def _totals_document(mode: EvalMode, totals: ModeTotals) -> dict[str, object]:
    return {
        "runs": totals.runs,
        "ai_bugs": _share_document(totals.ai_bugs_caught, "met", "bugs"),
        "credited_runs": _share_document(totals.credited_runs, "credited", "runs"),
        "precision": _share_document(totals.precision, "credited", "findings"),
        "findings_per_run": {
            "findings": totals.findings_per_run.part,
            "runs": totals.findings_per_run.whole,
            "mean": _rounded(totals.findings_per_run),
        },
        "injection_runs": _share_document(totals.injection_runs_passed, "passed", "runs"),
        "cost": _cost_document(totals.cost),
        "tokens": {
            "mean_input": _rounded(totals.input_tokens_per_run),
            "mean_output": _rounded(totals.output_tokens_per_run),
        },
        "tool_calls": (
            {"mean": _rounded(totals.tool_calls_per_run), "max": totals.most_tool_calls}
            if mode == "context"
            else None
        ),
        "failed_runs": dict(totals.failed_runs),
        "cap_hits": [
            {"case": hit.case_id, "run": hit.run, "cap": hit.cap} for hit in totals.cap_hits
        ],
    }


def _share_document(rate: Rate, part_name: str, whole_name: str) -> dict[str, object]:
    """The two counts under their names, and the share, null when not measured."""
    share = rate.value
    return {
        part_name: rate.part,
        whole_name: rate.whole,
        "share": None if share is None else round(float(share), _FRACTION_PLACES),
    }


def _cost_document(cost: Cost | None) -> dict[str, str | None]:
    if cost is None:
        return {"basis": _NOT_RECORDED, "total_usd": None, "mean_usd": None}
    return {
        "basis": cost.basis,
        "total_usd": str(_usd(cost.total_usd)),
        "mean_usd": str(_usd(cost.mean_usd)),
    }


def _verdict_document(verdict: Verdict) -> dict[str, object]:
    return {
        "precision": verdict.precision,
        "smaller_share": [
            {"case": case_id, "bug": bug_id} for case_id, bug_id in verdict.smaller_share
        ],
        "context_injection_passed": verdict.context_injection_passed,
    }


def _rounded(rate: Rate) -> float | None:
    """A mean per run, rounded for display, or None when not measured."""
    value = rate.value
    return None if value is None else round(float(value), _MEAN_PLACES)


def _usd(amount: Decimal) -> Decimal:
    return amount.quantize(_USD_PLACES, rounding=ROUND_HALF_EVEN)
