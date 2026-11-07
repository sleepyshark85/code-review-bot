## Review

5 reported, 0 below threshold, 0 dropped, 2 deduped.

- **medium** reliability at `billing/invoice.py:35`: Exception swallowed\: the handler discards it\, so failures pass silently\. Handle it\, log it or re\-raise it\. (confidence 1.00, source: rule)
- **low** reliability at `billing/invoice.py:39`: open\(\) without encoding uses the platform default\. Pass encoding\=\"utf\-8\"\, or open in binary mode\. (confidence 1.00, source: rule)
- **medium** logic at `billing/invoice.py:40`: \`export\_csv\` writes \`self\.total\(\)\`\, which in the visible code ignores \`total\_override\`\. An invoice that had a discount applied is therefore exported at the undiscounted total\. Export the effective total\, which is \`total\_override\` when it is set\. (confidence 0.60, source: ai)
- **medium** logic at `billing/invoice.py:14`: \`total\(\)\` now includes tax\, which changes the meaning of an existing public method\. Any caller that already adds tax on top of \`total\(\)\` will now apply tax twice\. \`apply\_discount\` also now discounts a tax\-inclusive amount\. Check every caller of \`total\(\)\`\, or add a separate \`total\_with\_tax\(\)\` method\. Money is also computed with float arithmetic\; use \`Decimal\` and explicit rounding\. (confidence 0.60, source: ai)
- **low** testing at `billing/invoice.py:38`: No tests accompany this change\. It needs tests for tax inclusion in \`total\(\)\`\, including \`tax\_rate\` of 0 or None\, for discount application on the taxed total\, for the failure behavior of \`apply\_discount\`\, and for the \`export\_csv\` output format and its use of the effective total\. (confidence 0.70, source: ai)
