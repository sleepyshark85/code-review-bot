"""The shared real diff for tests that only need some diff (P1-S2 AC5 rejects `+x\\n` with 15).

It comes from the developer's `tests/unit/diff_samples.py`. `tests/unit` is on the path only when
unit tests are collected, but `tests/` always is, so it's imported here as `unit.diff_samples`.
"""

from unit.diff_samples import FINDING_DIFF, FINDING_FIELDS, FINDING_LINE, MINIMAL_DIFF, section

__all__ = ["FINDING_DIFF", "FINDING_FIELDS", "FINDING_LINE", "MINIMAL_DIFF", "section"]
