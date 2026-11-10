"""Formatting shared by the ``metrics/v1`` and ``compare/v1`` renderers. Pure.

JSON holds shares as fractions, as ``baseline/v1`` does; text shows them as percentages.
"""

from typing import Final

from review_bot.pr.run_metrics import Percentiles

__all__ = [
    "fraction",
    "number_text",
    "percent_text",
    "percentiles_document",
    "percentiles_text",
]

_FRACTION_PLACES: Final = 3
_PERCENT_PLACES: Final = 1


def fraction(share: float | None) -> float | None:
    """A share for JSON: a fraction rounded to 3 places, or None."""
    return None if share is None else round(share, _FRACTION_PLACES)


def percentiles_document(percentiles: Percentiles) -> dict[str, float | None]:
    """Durations' count, p50 and p95 for JSON."""
    return {"runs": percentiles.runs, "p50": percentiles.p50, "p95": percentiles.p95}


def number_text(value: float | None) -> str:
    """A number for text, ``n/a`` when missing."""
    return "n/a" if value is None else str(value)


def percent_text(share: float | None) -> str:
    """A share for text: a percentage to 1 place, ``n/a`` when missing."""
    return "n/a" if share is None else f"{share * 100:.{_PERCENT_PLACES}f}%"


def percentiles_text(percentiles: Percentiles) -> str:
    """Durations' count, p50 and p95 as ``name=value`` text."""
    return (
        f"runs={percentiles.runs} p50={number_text(percentiles.p50)} "
        f"p95={number_text(percentiles.p95)}"
    )
