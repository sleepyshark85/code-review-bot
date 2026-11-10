"""An invoice and the amounts finance reports on."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Invoice:
    """One invoice: what was charged, before and after its discount."""

    invoice_id: str
    customer: str
    subtotal: Decimal
    discount: Decimal

    def total(self) -> Decimal:
        """The amount charged: the subtotal less the discount."""
        return self.subtotal - self.discount
