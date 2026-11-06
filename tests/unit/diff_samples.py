"""Shared diff text for tests that need a real diff but don't care which.

Since P1-S2, text with no file header (such as `+x\n`) is rejected with exit 15, so a test that
only needs a review to run uses `MINIMAL_DIFF`: one code file with one added line, `x`.
"""


def section(path: str, body: str = "+x\n") -> str:
    """One `diff --git` file section with one hunk that adds `body` to a new file."""
    line_count = body.count("\n")
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{line_count} @@\n{body}"
    )


MINIMAL_DIFF = section("app.py")
