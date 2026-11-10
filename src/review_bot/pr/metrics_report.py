"""Render ``RunMetrics`` as ``metrics/v1``, and a ``Comparison`` as ``compare/v1``.

Each as one JSON document, or as text for a person. Pure. Text from reports, such as a reason
or a rule part, is written only as JSON writes it.
"""

import json
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from review_bot.pr.compare import Comparison, Cost, Side
from review_bot.pr.feedback import RejectReason
from review_bot.pr.metrics import Precision, Verdicts
from review_bot.pr.run_metrics import Percentiles, RunMetrics, TokenTotals

__all__ = [
    "COMPARE_SCHEMA",
    "METRICS_SCHEMA",
    "render_compare_json",
    "render_compare_text",
    "render_metrics_json",
    "render_metrics_text",
]

METRICS_SCHEMA: Final = "metrics/v1"
COMPARE_SCHEMA: Final = "compare/v1"
# JSON holds shares as fractions, as baseline/v1 does; text shows them as percentages.
_FRACTION_PLACES: Final = 3
_PERCENT_PLACES: Final = 1
_MEAN_PLACES: Final = 1
# Compare shows money to a hundredth of a cent, as evals compare does: a diff review can cost
# less than a cent.
_USD_PLACES: Final = Decimal("0.0001")
_NOT_AVAILABLE: Final = "not available"
_NOT_MEASURED: Final = "not measured"


def render_metrics_json(metrics: RunMetrics) -> str:
    """The ``metrics/v1`` document, as one line of ASCII JSON.

    Shares are fractions rounded to 3 places, and money is a string of whole cents.
    """
    document = {
        "schema": METRICS_SCHEMA,
        "since": None if metrics.since is None else metrics.since.isoformat(),
        "until": None if metrics.until is None else metrics.until.isoformat(),
        "runs": {
            "count": metrics.runs,
            "outcomes": [
                {
                    "mode": count.mode,
                    "origin": count.origin,
                    "outcome": count.outcome,
                    "reason": count.reason,
                    "count": count.count,
                }
                for count in metrics.outcomes
            ],
            "latency_s": {
                "end_to_end": _percentiles_document(metrics.end_to_end_s),
                "review": _percentiles_document(metrics.review_s),
            },
        },
        "tokens": {
            "runs": metrics.token_runs,
            "input": _tokens_document(metrics.input_tokens),
            "output": _tokens_document(metrics.output_tokens),
            "over_budget": metrics.over_budget,
        },
        "cost": _cost_document(metrics),
        "shadow_precision": _precision_document(metrics.shadow),
        "comment_precision": _precision_document(metrics.comment),
        "orphans": len(metrics.orphans),
    }
    return json.dumps(document, ensure_ascii=True, allow_nan=False)


def render_metrics_text(metrics: RunMetrics) -> str:
    """The same numbers as ``render_metrics_json``, as ``name=value`` lines for a person.

    Shares are percentages, a missing number reads ``n/a``, and cost without prices
    ``not priced``. Text from reports, such as a reason or a rule part, is JSON-quoted.
    """
    since, until = metrics.since or "any", metrics.until or "any"
    lines = [
        f"runs count={metrics.runs} since={since} until={until}",
        *(
            f"outcome mode={count.mode} origin={count.origin} outcome={count.outcome} "
            f"reason={_quoted(count.reason)} count={count.count}"
            for count in metrics.outcomes
        ),
        f"latency_s end_to_end {_percentiles_text(metrics.end_to_end_s)}",
        f"latency_s review {_percentiles_text(metrics.review_s)}",
        f"tokens runs={metrics.token_runs} input {_tokens_text(metrics.input_tokens)} "
        f"output {_tokens_text(metrics.output_tokens)} over_budget={metrics.over_budget}",
        _cost_text(metrics),
        *_precision_text("shadow_precision", metrics.shadow),
        *_precision_text("comment_precision", metrics.comment),
        f"orphans={len(metrics.orphans)}",
    ]
    return "\n".join(f"metrics: {line}" for line in lines)


def _fraction(share: float | None) -> float | None:
    return None if share is None else round(share, _FRACTION_PLACES)


def _percentiles_document(percentiles: Percentiles) -> dict[str, float | None]:
    return {"runs": percentiles.runs, "p50": percentiles.p50, "p95": percentiles.p95}


def _tokens_document(tokens: TokenTotals) -> dict[str, float | None]:
    return {"total": tokens.total, "mean": tokens.mean, "max": tokens.largest}


def _cost_document(metrics: RunMetrics) -> dict[str, str | None] | None:
    if metrics.cost_usd is None:
        return None
    per_accepted = metrics.cost_per_accepted_usd
    return {
        "usd": str(metrics.cost_usd),
        "per_accepted_finding_usd": None if per_accepted is None else str(per_accepted),
    }


def _verdicts_document(verdicts: Verdicts) -> dict[str, float | None]:
    return {
        "accepted": verdicts.accepted,
        "rejected": verdicts.rejected,
        "duplicates": verdicts.duplicates,
        "precision": _fraction(verdicts.precision),
    }


def _precision_document(precision: Precision) -> dict[str, object]:
    verdicts = precision.verdicts
    low, high = verdicts.wilson_95 or (None, None)
    splits: dict[str, object] = {
        trait: {value: _verdicts_document(split) for value, split in values.items()}
        for trait, values in precision.splits.items()
    }
    splits["reject_reason"] = {
        reason: {"count": precision.reject_reasons.get(reason, 0)} for reason in RejectReason
    }
    return {
        "units": precision.units,
        "judged": verdicts.judged,
        "coverage": _fraction(precision.coverage),
        **_verdicts_document(verdicts),
        "wilson_low": _fraction(low),
        "wilson_high": _fraction(high),
        "lower_bound": _fraction(verdicts.lower_bound),
        "prs": precision.pr_count,
        "largest_pr_share": _fraction(precision.largest_pr_share),
        "conflict": precision.conflicts,
        "resolved": precision.resolved,
        "splits": splits,
    }


def _quoted(text: str | None) -> str:
    return json.dumps(text, ensure_ascii=True)


def _number_text(value: float | None) -> str:
    return "n/a" if value is None else str(value)


def _percent_text(share: float | None) -> str:
    return "n/a" if share is None else f"{share * 100:.{_PERCENT_PLACES}f}%"


def _percentiles_text(percentiles: Percentiles) -> str:
    return (
        f"runs={percentiles.runs} p50={_number_text(percentiles.p50)} "
        f"p95={_number_text(percentiles.p95)}"
    )


def _tokens_text(tokens: TokenTotals) -> str:
    return (
        f"total={tokens.total} mean={_number_text(tokens.mean)} max={_number_text(tokens.largest)}"
    )


def _cost_text(metrics: RunMetrics) -> str:
    if metrics.cost_usd is None:
        return "cost=not priced"
    per_accepted = metrics.cost_per_accepted_usd
    per_accepted_text = "n/a" if per_accepted is None else f"{per_accepted} USD"
    return f"cost total={metrics.cost_usd} USD per_accepted_finding={per_accepted_text}"


def _precision_text(name: str, precision: Precision) -> list[str]:
    verdicts = precision.verdicts
    interval = verdicts.wilson_95
    wilson = "n/a" if interval is None else " to ".join(map(_percent_text, interval))
    reasons = " ".join(
        f"{reason}={precision.reject_reasons.get(reason, 0)}" for reason in RejectReason
    )
    return [
        f"{name} units={precision.units} judged={verdicts.judged} "
        f"coverage={_percent_text(precision.coverage)} {_verdicts_text(verdicts)}",
        f"{name} wilson_95={wilson} lower_bound={_percent_text(verdicts.lower_bound)} "
        f"({verdicts.accepted}/{verdicts.judged}) prs={precision.pr_count} "
        f"largest_pr_share={_percent_text(precision.largest_pr_share)} "
        f"conflict={precision.conflicts} resolved={precision.resolved}",
        *(
            f"{name} split {trait}={_quoted(value)} {_verdicts_text(split)}"
            for trait, values in precision.splits.items()
            for value, split in values.items()
        ),
        f"{name} split reject_reason {reasons}",
    ]


def _verdicts_text(verdicts: Verdicts) -> str:
    return (
        f"accepted={verdicts.accepted} rejected={verdicts.rejected} "
        f"duplicates={verdicts.duplicates} "
        f"precision={_percent_text(verdicts.precision)} ({verdicts.accepted}/{verdicts.counted})"
    )


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
                "baseline": _fraction(comparison.baseline.coverage),
                "candidate": _fraction(comparison.candidate.coverage),
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
            "coverage": _fraction(side.coverage),
        },
        "precision": {
            "accepted": verdicts.accepted,
            "counted": verdicts.counted,
            "share": _fraction(verdicts.precision),
            "wilson_low": _fraction(low),
            "wilson_high": _fraction(high),
            "lower_bound": _fraction(verdicts.lower_bound),
        },
        "recall": {
            "issues": side.issues,
            "pool": comparison.pool.issues,
            "share": _fraction(comparison.recall(side)),
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
        "latency": {"review_s": _percentiles_document(side.review_s)},
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
    wilson = "n/a" if interval is None else " to ".join(map(_percent_text, interval))
    reasons = " ".join(f"{reason}={side.reject_reasons[reason]}" for reason in RejectReason)
    return [
        f"{name} units={side.units} judged={verdicts.judged} accepted={verdicts.accepted} "
        f"rejected {reasons} unjudged={side.unjudged} coverage={_percent_text(side.coverage)}",
        f"{name} precision={_percent_text(verdicts.precision)} "
        f"({verdicts.accepted}/{verdicts.counted}) wilson_95={wilson} "
        f"lower_bound={_percent_text(verdicts.lower_bound)} "
        f"({verdicts.accepted}/{verdicts.judged})",
        f"{name} recall={_percent_text(comparison.recall(side))} "
        f"({side.issues}/{comparison.pool.issues})",
        f"{name} {_compare_cost_text(side.cost)}",
        f"{name} tokens_per_pr mean_input={_number_text(_mean(side.mean_input_tokens))} "
        f"mean_output={_number_text(_mean(side.mean_output_tokens))}",
        f"{name} tool_calls_per_pr mean={_number_text(_mean(side.mean_tool_calls))} "
        f"max={side.most_tool_calls}",
        f"{name} latency_s review {_percentiles_text(side.review_s)}",
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
            f"baseline_coverage={_percent_text(comparison.baseline.coverage)} "
            f"candidate_coverage={_percent_text(comparison.candidate.coverage)}"
        )
    return (
        f"verdict precision={verdict.precision or _NOT_MEASURED} "
        f"recall={verdict.recall or _NOT_MEASURED}"
    )


def _mean(value: float | None) -> float | None:
    return None if value is None else round(value, _MEAN_PLACES)


def _usd(amount: Decimal) -> Decimal:
    return amount.quantize(_USD_PLACES, rounding=ROUND_HALF_EVEN)
