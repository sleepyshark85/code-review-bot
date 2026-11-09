"""How an invoice's lines are stored."""

# An invoice holds its lines in a list, in the order they were added. The first line is at
# index 0, and every line counts towards the subtotal.
INVOICE_LINES_ARE_ZERO_INDEXED = True
