"""Orders as the shop stores them."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Order:
    """One paid order."""

    order_id: str
    placed_on: date
    amount: Decimal
