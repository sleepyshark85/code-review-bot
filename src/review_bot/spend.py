"""A live command's dollar allowance, which each call's cost cap is planned and charged against.

Pure (ADR 0015): before any call, the planned calls at their cap must fit the allowance; during
the run, a call starts only if one more at its cap still fits, and a call that reported no cost
counts as its cap. ``usd_amount`` reads either amount from a command-line flag.
"""

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

__all__ = ["DollarBudget", "usd_amount"]


def usd_amount(text: str) -> Decimal:
    """``text`` as an amount of USD above 0, for a flag's ``type``.

    Raises:
        argparse.ArgumentTypeError: If it isn't a finite decimal above 0.
    """
    try:
        amount = Decimal(text)
    except InvalidOperation:
        amount = None
    if amount is None or not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} isn't an amount of USD above 0, such as 7.20")
    return amount


@dataclass(frozen=True, slots=True)
class DollarBudget:
    """``max_usd``, a command's allowance, and ``max_cost_usd``, each call's cap.

    Raises:
        ValueError: If either is not above 0.
    """

    max_usd: Decimal
    max_cost_usd: Decimal

    def __post_init__(self) -> None:
        """Check both amounts are above 0."""
        if not (self.max_usd > 0 and self.max_cost_usd > 0):
            raise ValueError("A dollar budget's allowance and per-call cap are both above 0.")

    def planned_usd(self, planned_calls: int) -> Decimal:
        """The most ``planned_calls`` can cost, each at its cap."""
        return planned_calls * self.max_cost_usd

    def covers(self, planned_calls: int) -> bool:
        """Whether ``planned_calls``, each at its cap, fit in the allowance."""
        return self.planned_usd(planned_calls) <= self.max_usd

    def has_room(self, spent_usd: Decimal) -> bool:
        """Whether one more call at its cap still fits after ``spent_usd``."""
        return spent_usd + self.max_cost_usd <= self.max_usd

    def charge(self, cost_usd: Decimal | None) -> Decimal:
        """What one call counts towards the allowance: its reported cost, or its cap if none."""
        return self.max_cost_usd if cost_usd is None else cost_usd
