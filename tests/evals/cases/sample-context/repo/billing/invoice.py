"""Invoices: line items, tax and discount codes."""

DISCOUNT_CODES = {"SAVE10": 0.10, "SAVE20": 0.20}

class Invoice:
    def __init__(self, invoice_id, line_items, tax_rate=0.0):
        self.id = invoice_id
        self.line_items = line_items
        self.tax_rate = tax_rate
        self.total_override = None

    def total(self):
        subtotal = sum(item.price * item.qty for item in self.line_items)
        return subtotal

    def line_count(self):
        return len(self.line_items)

    def add_item(self, item):
        self.line_items.append(item)

    def remove_item(self, sku):
        self.line_items = [item for item in self.line_items if item.sku != sku]

    def has_discount(self):
        return self.total_override is not None

    # Discount codes come from DISCOUNT_CODES above.

    def apply_discount(self, code):
        discount = DISCOUNT_CODES.get(code)
        if discount:
            self.total_override = self.total() * (1 - discount)
