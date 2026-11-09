"""Orders and their totals, in exact decimal money."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Order:
    order_id: str
    total: Decimal
    reason: str = ""
