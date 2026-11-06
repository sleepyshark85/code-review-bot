"""The shared real diff for tests that only need some diff (P1-S2 AC5 rejects `+x\\n` with 15).

It comes from the developer's `tests/unit/diff_samples.py`. `tests/unit` is on the path only when
unit tests are collected, but `tests/` always is, so it's imported here as `unit.diff_samples`.

Since P1-S7, `Assignment/sample_diff.txt`'s `except Exception: pass` is a rule finding, so a
review of it exits 1 and prints `SAMPLE_RULE_LINE` first, whatever the model returns. Since P1-S8,
its `open(path, "w")` is one too, so `SAMPLE_OPEN_LINE` follows: `SAMPLE_RULE_LINES` holds both.
"""

from typing import Final

from unit.diff_samples import FINDING_DIFF, FINDING_FIELDS, FINDING_LINE, MINIMAL_DIFF, section

__all__ = [
    "FINDING_DIFF",
    "FINDING_FIELDS",
    "FINDING_LINE",
    "MINIMAL_DIFF",
    "SAMPLE_OPEN_LINE",
    "SAMPLE_RULE_LINE",
    "SAMPLE_RULE_LINES",
    "section",
]

SAMPLE_RULE_LINE: Final = (
    "billing/invoice.py:35 [medium/reliability] Exception swallowed: the handler discards it, so "
    "failures pass silently. Handle it, log it or re-raise it. (confidence 1.00)"
)
SAMPLE_OPEN_LINE: Final = (
    "billing/invoice.py:39 [low/reliability] open() without encoding uses the platform default. "
    'Pass encoding="utf-8", or open in binary mode. (confidence 1.00)'
)
# The sample's rule lines, in the order they print (P1-S8 AC1).
SAMPLE_RULE_LINES: Final = (SAMPLE_RULE_LINE, SAMPLE_OPEN_LINE)
