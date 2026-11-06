"""The shared real diff for tests that only need some diff (P1-S2 AC5 rejects `+x\\n` with 15).

It comes from the developer's `tests/unit/diff_samples.py`. `tests/unit` is on the path only when
unit tests are collected, but `tests/` always is, so it's imported here as `unit.diff_samples`.

Since P1-S7, `Assignment/sample_diff.txt`'s `except Exception: pass` is a rule finding, so a
review of it exits 1 and prints `SAMPLE_RULE_LINE` first, whatever the model returns.
"""

from typing import Final

from unit.diff_samples import FINDING_DIFF, FINDING_FIELDS, FINDING_LINE, MINIMAL_DIFF, section

__all__ = [
    "FINDING_DIFF",
    "FINDING_FIELDS",
    "FINDING_LINE",
    "MINIMAL_DIFF",
    "SAMPLE_RULE_LINE",
    "section",
]

SAMPLE_RULE_LINE: Final = (
    "billing/invoice.py:35 [medium/reliability] Exception swallowed: the handler discards it, so "
    "failures pass silently. Handle it, log it or re-raise it. (confidence 1.00)"
)
