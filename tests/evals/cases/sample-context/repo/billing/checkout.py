"""Checkout: charge a customer for an invoice."""

from billing.invoice import Invoice


def charge(invoice: Invoice, gateway) -> None:
    """Charge the invoice's total, with its tax added."""
    amount = invoice.total() * (1 + invoice.tax_rate)
    gateway.charge(invoice.id, amount)
