"""Shipping charges for an order."""

from decimal import Decimal

FLAT_RATE = Decimal("4.99")


def shipping_for(weight_kg: Decimal) -> Decimal:
    """The flat rate, whatever the weight."""
    return FLAT_RATE
