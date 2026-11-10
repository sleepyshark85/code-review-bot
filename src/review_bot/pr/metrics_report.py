"""Render ``RunMetrics`` as ``metrics/v1``: one JSON document, or text for a person.

Pure. Text from reports, such as a reason or a rule part, is written only as JSON writes it.
"""

import json
from typing import Final

from review_bot.pr.feedback import RejectReason
from review_bot.pr.metrics import Precision, Verdicts
from review_bot.pr.report_format import (
    fraction,
    number_text,
    percent_text,
    percentiles_document,
    percentiles_text,
)
from review_bot.pr.run_metrics import RunMetrics, TokenTotals

__all__ = ["METRICS_SCHEMA", "render_metrics_json", "render_metrics_text"]

METRICS_SCHEMA: Final = "metrics/v1"


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
                "end_to_end": percentiles_document(metrics.end_to_end_s),
                "review": percentiles_document(metrics.review_s),
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
        f"latency_s end_to_end {percentiles_text(metrics.end_to_end_s)}",
        f"latency_s review {percentiles_text(metrics.review_s)}",
        f"tokens runs={metrics.token_runs} input {_tokens_text(metrics.input_tokens)} "
        f"output {_tokens_text(metrics.output_tokens)} over_budget={metrics.over_budget}",
        _cost_text(metrics),
        *_precision_text("shadow_precision", metrics.shadow),
        *_precision_text("comment_precision", metrics.comment),
        f"orphans={len(metrics.orphans)}",
    ]
    return "\n".join(f"metrics: {line}" for line in lines)


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
        "precision": fraction(verdicts.precision),
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
        "coverage": fraction(precision.coverage),
        **_verdicts_document(verdicts),
        "wilson_low": fraction(low),
        "wilson_high": fraction(high),
        "lower_bound": fraction(verdicts.lower_bound),
        "prs": precision.pr_count,
        "largest_pr_share": fraction(precision.largest_pr_share),
        "conflict": precision.conflicts,
        "resolved": precision.resolved,
        "splits": splits,
    }


def _quoted(text: str | None) -> str:
    return json.dumps(text, ensure_ascii=True)


def _tokens_text(tokens: TokenTotals) -> str:
    return f"total={tokens.total} mean={number_text(tokens.mean)} max={number_text(tokens.largest)}"


def _cost_text(metrics: RunMetrics) -> str:
    if metrics.cost_usd is None:
        return "cost=not priced"
    per_accepted = metrics.cost_per_accepted_usd
    per_accepted_text = "n/a" if per_accepted is None else f"{per_accepted} USD"
    return f"cost total={metrics.cost_usd} USD per_accepted_finding={per_accepted_text}"


def _precision_text(name: str, precision: Precision) -> list[str]:
    verdicts = precision.verdicts
    interval = verdicts.wilson_95
    wilson = "n/a" if interval is None else " to ".join(map(percent_text, interval))
    reasons = " ".join(
        f"{reason}={precision.reject_reasons.get(reason, 0)}" for reason in RejectReason
    )
    return [
        f"{name} units={precision.units} judged={verdicts.judged} "
        f"coverage={percent_text(precision.coverage)} {_verdicts_text(verdicts)}",
        f"{name} wilson_95={wilson} lower_bound={percent_text(verdicts.lower_bound)} "
        f"({verdicts.accepted}/{verdicts.judged}) prs={precision.pr_count} "
        f"largest_pr_share={percent_text(precision.largest_pr_share)} "
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
        f"precision={percent_text(verdicts.precision)} ({verdicts.accepted}/{verdicts.counted})"
    )
