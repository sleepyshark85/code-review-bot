## Review

1 reported, 1 below threshold, 3 dropped, 1 deduped.

- **medium** reliability at `billing/invoice.py:35`: Exception swallowed\: the handler discards it\, so failures pass silently\. Handle it\, log it or re\-raise it\. (confidence 1.00, source: rule)

### Skipped files

- `README.md`: docs
