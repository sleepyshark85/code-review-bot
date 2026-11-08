import pytest

from review_bot.diff_parser import ParsedDiff, parse_diff
from review_bot.pr.incremental import changed_hunks

# The PR diff: two hunks of x.py (new lines 1-7 and 20-26), and one of y.py.
X_HEADER = "diff --git a/x.py b/x.py\nindex 1111111..2222222 100644\n--- a/x.py\n+++ b/x.py\n"
X_FIRST = "@@ -1,6 +1,7 @@\n a\n b\n c\n+new = 1\n d\n e\n f\n"
X_SECOND = "@@ -19,6 +20,7 @@\n s\n t\n u\n+other = 2\n v\n w\n x\n"
Y_FILE = (
    "diff --git a/y.py b/y.py\nindex 3333333..4444444 100644\n--- a/y.py\n+++ b/y.py\n"
    "@@ -1,3 +1,4 @@\n p\n+added = 3\n q\n r\n"
)
PR_DIFF = X_HEADER + X_FIRST + X_SECOND + Y_FILE
PARSED_PR_DIFF = parse_diff(PR_DIFF)


def since(path: str, hunk: str, *, old_path: str | None = None) -> ParsedDiff:
    """A diff since the last reviewed commit, changing ``path`` with one hunk."""
    old = path if old_path is None else old_path
    return parse_diff(f"diff --git a/{old} b/{path}\n--- a/{old}\n+++ b/{path}\n{hunk}")


def test_keeps_only_the_hunk_holding_an_added_line_and_its_files_header() -> None:
    changed = since("x.py", "@@ -21,3 +21,3 @@\n t\n-u\n+u2\n other = 2\n")

    assert changed_hunks(PR_DIFF, PARSED_PR_DIFF, changed) == X_HEADER + X_SECOND


def test_keeps_a_hunk_holding_a_context_line_changed_since_then() -> None:
    changed = since("x.py", "@@ -2,1 +2,1 @@\n-b0\n+b\n")

    assert changed_hunks(PR_DIFF, PARSED_PR_DIFF, changed) == X_HEADER + X_FIRST


def test_keeps_the_hunk_where_a_line_was_only_removed() -> None:
    changed = since("y.py", "@@ -1,4 +1,3 @@\n p\n-gone = 0\n added = 3\n q\n")

    assert changed_hunks(PR_DIFF, PARSED_PR_DIFF, changed) == Y_FILE


def test_keeps_hunks_of_several_files() -> None:
    changed = parse_diff(
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -3,1 +4,1 @@\n+new = 1\n"
        "diff --git a/y.py b/y.py\n--- a/y.py\n+++ b/y.py\n@@ -2,0 +2,1 @@\n+added = 3\n"
    )

    assert changed_hunks(PR_DIFF, PARSED_PR_DIFF, changed) == X_HEADER + X_FIRST + Y_FILE


@pytest.mark.parametrize(
    "changed",
    [
        ParsedDiff(files=(), reviewable_text="", decoded_paths=()),
        since("z.py", "@@ -1,1 +1,1 @@\n-old\n+new = 1\n"),
        since("x.py", "@@ -10,1 +11,1 @@\n-j0\n+j\n"),
    ],
    ids=["nothing-changed", "another-file", "between-the-hunks"],
)
def test_is_empty_when_no_hunk_holds_a_changed_line(changed: ParsedDiff) -> None:
    assert changed_hunks(PR_DIFF, PARSED_PR_DIFF, changed) == ""


def test_matches_lines_by_the_new_path_after_a_rename() -> None:
    changed = since("y.py", "@@ -2,1 +2,1 @@\n-added = 0\n+added = 3\n", old_path="old_y.py")

    assert changed_hunks(PR_DIFF, PARSED_PR_DIFF, changed) == Y_FILE


def test_leaves_out_a_file_the_pull_request_deletes() -> None:
    deleted = "diff --git a/d.py b/d.py\ndeleted file mode 100644\n--- a/d.py\n+++ /dev/null\n"
    pr_diff = deleted + "@@ -1,2 +0,0 @@\n-one\n-two\n"
    changed = parse_diff(deleted + "@@ -1,1 +0,0 @@\n-one\n")

    assert changed_hunks(pr_diff, parse_diff(pr_diff), changed) == ""


def test_raises_on_a_diff_without_gits_file_headers() -> None:
    unified = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"

    with pytest.raises(ValueError, match="diff --git"):
        changed_hunks(unified, parse_diff(unified), since("x.py", "@@ -1,1 +1,1 @@\n-a\n+b\n"))


def test_raises_when_the_parse_isnt_of_the_diff() -> None:
    other = parse_diff(X_HEADER + X_FIRST + Y_FILE)

    with pytest.raises(ValueError):
        changed_hunks(PR_DIFF, other, since("x.py", "@@ -2,1 +2,1 @@\n-b0\n+b\n"))
