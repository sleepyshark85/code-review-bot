"""Render a ``Comparison`` as ``compare/v1``: one JSON document, or text for a person. Pure."""

import json
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from review_bot.pr.compare import Comparison, Cost, Side
from review_bot.pr.feedback import RejectReason
from review_bot.pr.report_format import (
    fraction,
    number_text,
    percent_text,
    percentiles_document,
    percentiles_text,
)

__all__ = ["COMPARE_SCHEMA", "render_compare_json", "render_compare_text"]

COMPARE_SCHEMA: Final = "compare/v1"
_MEAN_PLACES: Final = 1
# Money to a hundredth of a cent, as evals compare shows it: a diff review can cost less than a
# cent.
_USD_PLACES: Final = Decimal("0.0001")
_NOT_AVAILABLE: Final = "not available"
_NOT_MEASURED: Final = "not measured"


def render_compare_json(comparison: Comparison) -> str:
    """The ``compare/v1`` document, as one line of ASCII JSON.

    Shares are fractions rounded to 3 places, means to 1, and money is a string of USD to 4
    places. A change or share with nothing to measure it on is null.
    """
    verdict = comparison.verdict
    document = {
        "schema": COMPARE_SCHEMA,
        "prs": {
            "compared": [
                {"repo": pr.repo, "number": pr.number, "head_sha": pr.head_sha}
                for pr in comparison.compared
            ],
            "excluded": [
                {"repo": pr.repo, "number": pr.number, "reason": pr.reason}
                for pr in comparison.excluded
            ],
        },
        "baseline": _side_document(comparison, comparison.baseline),
        "candidate": _side_document(comparison, comparison.candidate),
        "pool": {
            "issues": comparison.pool.issues,
            "both": comparison.pool.both,
            "baseline_only": comparison.pool.baseline_only,
            "candidate_only": comparison.pool.candidate_only,
        },
        "verdict": {
            "decidable": verdict.is_decidable,
            "reason": None if verdict.is_decidable else "coverage",
            "precision": verdict.precision,
            "recall": verdict.recall,
            "coverage": {
                "baseline": fraction(comparison.baseline.coverage),
                "candidate": fraction(comparison.candidate.coverage),
            },
        },
    }
    return json.dumps(document, ensure_ascii=True, allow_nan=False)


def render_compare_text(comparison: Comparison) -> str:
    """The same figures as ``render_compare_json``, as ``name=value`` lines for a person.

    Shares are percentages, a missing number reads ``n/a``, a change with nothing to measure it
    on ``not measured``, and cost with neither reported costs nor prices ``not available``.
    """
    pool = comparison.pool
    lines = [
        f"prs compared={len(comparison.compared)} excluded={len(comparison.excluded)}",
        *(
            f"compared repo={pr.repo} number={pr.number} head_sha={pr.head_sha}"
            for pr in comparison.compared
        ),
        *(
            f"excluded repo={pr.repo} number={pr.number} reason={pr.reason}"
            for pr in comparison.excluded
        ),
        *_side_text("baseline", comparison, comparison.baseline),
        *_side_text("candidate", comparison, comparison.candidate),
        f"pool issues={pool.issues} both={pool.both} baseline_only={pool.baseline_only} "
        f"candidate_only={pool.candidate_only}",
        _compare_verdict_text(comparison),
    ]
    return "\n".join(f"compare: {line}" for line in lines)


def _side_document(comparison: Comparison, side: Side) -> dict[str, object]:
    verdicts = side.verdicts
    low, high = verdicts.wilson_95 or (None, None)
    return {
        "units": {
            "total": side.units,
            "judged": verdicts.judged,
            "accepted": verdicts.accepted,
            "rejected": {reason: side.reject_reasons[reason] for reason in RejectReason},
            "unjudged": side.unjudged,
            "coverage": fraction(side.coverage),
        },
        "precision": {
            "accepted": verdicts.accepted,
            "counted": verdicts.counted,
            "share": fraction(verdicts.precision),
            "wilson_low": fraction(low),
            "wilson_high": fraction(high),
            "lower_bound": fraction(verdicts.lower_bound),
        },
        "recall": {
            "issues": side.issues,
            "pool": comparison.pool.issues,
            "share": fraction(comparison.recall(side)),
        },
        "cost": _compare_cost_document(side.cost),
        "tokens": {
            "mean_input_per_pr": _mean(side.mean_input_tokens),
            "mean_output_per_pr": _mean(side.mean_output_tokens),
        },
        "tool_calls": {
            "mean_per_pr": _mean(side.mean_tool_calls),
            "max_per_pr": side.most_tool_calls,
        },
        "latency": {"review_s": percentiles_document(side.review_s)},
    }


def _compare_cost_document(cost: Cost | None) -> dict[str, str | None]:
    if cost is None:
        return {
            "basis": _NOT_AVAILABLE,
            "total_usd": None,
            "mean_per_pr_usd": None,
            "per_accepted_unit_usd": None,
        }
    per_accepted = cost.per_accepted_unit_usd
    return {
        "basis": cost.basis,
        "total_usd": str(_usd(cost.total_usd)),
        "mean_per_pr_usd": str(_usd(cost.mean_per_pr_usd)),
        "per_accepted_unit_usd": None if per_accepted is None else str(_usd(per_accepted)),
    }


def _side_text(name: str, comparison: Comparison, side: Side) -> list[str]:
    verdicts = side.verdicts
    interval = verdicts.wilson_95
    wilson = "n/a" if interval is None else " to ".join(map(percent_text, interval))
    reasons = " ".join(f"{reason}={side.reject_reasons[reason]}" for reason in RejectReason)
    return [
        f"{name} units={side.units} judged={verdicts.judged} accepted={verdicts.accepted} "
        f"rejected {reasons} unjudged={side.unjudged} coverage={percent_text(side.coverage)}",
        f"{name} precision={percent_text(verdicts.precision)} "
        f"({verdicts.accepted}/{verdicts.counted}) wilson_95={wilson} "
        f"lower_bound={percent_text(verdicts.lower_bound)} "
        f"({verdicts.accepted}/{verdicts.judged})",
        f"{name} recall={percent_text(comparison.recall(side))} "
        f"({side.issues}/{comparison.pool.issues})",
        f"{name} {_compare_cost_text(side.cost)}",
        f"{name} tokens_per_pr mean_input={number_text(_mean(side.mean_input_tokens))} "
        f"mean_output={number_text(_mean(side.mean_output_tokens))}",
        f"{name} tool_calls_per_pr mean={number_text(_mean(side.mean_tool_calls))} "
        f"max={side.most_tool_calls}",
        f"{name} latency_s review {percentiles_text(side.review_s)}",
    ]


def _compare_cost_text(cost: Cost | None) -> str:
    if cost is None:
        return f"cost={_NOT_AVAILABLE}"
    per_accepted = cost.per_accepted_unit_usd
    per_accepted_text = "n/a" if per_accepted is None else f"{_usd(per_accepted)} USD"
    return (
        f"cost total={_usd(cost.total_usd)} USD mean_per_pr={_usd(cost.mean_per_pr_usd)} USD "
        f"per_accepted_unit={per_accepted_text} basis={cost.basis}"
    )


def _compare_verdict_text(comparison: Comparison) -> str:
    verdict = comparison.verdict
    if not verdict.is_decidable:
        return (
            "verdict=not decidable: coverage "
            f"baseline_coverage={percent_text(comparison.baseline.coverage)} "
            f"candidate_coverage={percent_text(comparison.candidate.coverage)}"
        )
    return (
        f"verdict precision={verdict.precision or _NOT_MEASURED} "
        f"recall={verdict.recall or _NOT_MEASURED}"
    )


def _mean(value: float | None) -> float | None:
    return None if value is None else round(value, _MEAN_PLACES)


def _usd(amount: Decimal) -> Decimal:
    return amount.quantize(_USD_PLACES, rounding=ROUND_HALF_EVEN)
