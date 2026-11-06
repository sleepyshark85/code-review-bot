"""Shared diff text, and the default finding with a diff it anchors to.

Since P1-S2, text with no file header (such as `+x\n`) is rejected with exit 15, so a test that
only needs a review to run uses `MINIMAL_DIFF`: one code file with one added line, `x`.

Since P1-S6, a finding prints only if its file and line are in the diff. A test that needs a
finding to print uses `FINDING_FIELDS` (`src/app.py` line 7) over `FINDING_DIFF`, whose
`src/app.py` adds lines 1 to 7.
"""

from typing import Final


def section(path: str, body: str = "+x\n") -> str:
    """One `diff --git` file section with one hunk that adds `body` to a new file."""
    line_count = body.count("\n")
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{line_count} @@\n{body}"
    )


MINIMAL_DIFF = section("app.py")

FINDING_FIELDS: Final = {
    "file": "src/app.py",
    "line": 7,
    "severity": "high",
    "category": "logic",
    "rationale": "Rationale text.",
    "confidence": 0.9,
}
FINDING_DIFF = section("src/app.py", "+x\n" * 7)
# The text output's line for `Finding(**FINDING_FIELDS)`.
FINDING_LINE: Final = "src/app.py:7 [high/logic] Rationale text. (confidence 0.90)"
