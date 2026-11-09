"""Customers and their balances."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Customer:
    """A customer. ``balance`` is what they owe: a charge raises it, a credit lowers it."""

    customer_id: str
    balance: Decimal = Decimal(0)
