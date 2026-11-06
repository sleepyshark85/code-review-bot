## Review

3 reported, 0 below threshold, 0 dropped, 0 deduped.

- **medium** reliability at `billing/invoice.py:35`: Exception swallowed\: the handler discards it\, so failures pass silently\. Handle it\, log it or re\-raise it\. (confidence 1.00, source: rule)
- **low** reliability at `billing/invoice.py:39`: open\(\) without encoding uses the platform default\. Pass encoding\=\"utf\-8\"\, or open in binary mode\. (confidence 1.00, source: rule)
- **high** logic at `billing/invoice.py:14`: Tax is applied to the discounted total twice\. (confidence 0.80, source: ai)
