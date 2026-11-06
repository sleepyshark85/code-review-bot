import time

import pytest
from diff_samples import section
from hypothesis import given
from hypothesis import strategies as st

from review_bot.diff_parser import DiffLine, LineKind, SkipReason, parse_diff
from review_bot.errors import DiffNotParsableError


def only_file_path(text: str) -> str | None:
    [file] = parse_diff(text).files
    return file.path


@pytest.mark.parametrize(
    ("header_line", "path"),
    [
        ('+++ "b/tab\\there.py"', "tab\there.py"),
        ('+++ "b/say \\"hi\\".py"', 'say "hi".py'),
        ('+++ "b/back\\\\slash.py"', "back\\slash.py"),
        ('+++ "b/not\\8octal.py"', "not8octal.py"),
        ("+++ b/spaced name.py\t", "spaced name.py"),
        ("+++ new.py\t2026-11-05 10:00:00.000000000 +0000", "new.py"),
    ],
    ids=["tab", "quote", "backslash", "not-octal", "trailing-tab", "timestamp"],
)
def test_parse_diff_reads_the_new_path_from_the_plus_line(header_line: str, path: str) -> None:
    text = f"--- a/old.py\n{header_line}\n@@ -1 +1 @@\n-a\n+b\n"

    assert only_file_path(text) == path


@pytest.mark.parametrize(
    ("git_line", "path"),
    [
        ("diff --git a/run.sh b/run.sh", "run.sh"),
        ("diff --git a/with space.sh b/with space.sh", "with space.sh"),
        ('diff --git "a/caf\\303\\251.sh" "b/caf\\303\\251.sh"', "café.sh"),
        ("diff --git run.sh run.sh", "run.sh"),
    ],
    ids=["prefixed", "space", "quoted", "no-prefix"],
)
def test_parse_diff_with_only_a_mode_change_reads_the_git_header_path(
    git_line: str, path: str
) -> None:
    text = f"{git_line}\nold mode 100644\nnew mode 100755\n"

    assert only_file_path(text) == path


def test_parse_diff_with_a_pure_rename_reads_the_rename_target() -> None:
    text = (
        "diff --git a/old.py b/new.py\nsimilarity index 100%\n"
        "rename from old.py\nrename to b/new.py\n"
    )

    assert only_file_path(text) == "b/new.py"


def test_parse_diff_with_an_empty_deleted_file_has_no_path() -> None:
    assert only_file_path("diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n") is None


def test_parse_diff_with_a_deleted_binary_file_skips_it_with_no_path() -> None:
    text = (
        "diff --git a/x.bin b/x.bin\ndeleted file mode 100644\n"
        "Binary files a/x.bin and /dev/null differ\n"
    )

    [file] = parse_diff(text).files

    assert (file.path, file.skip_reason) == (None, SkipReason.BINARY)


def test_parse_diff_with_a_git_binary_patch_skips_it_as_binary() -> None:
    text = (
        "diff --git a/logo.png b/logo.png\nindex 1111111..2222222 100644\n"
        "GIT binary patch\nliteral 4\nLcmZ?wbhEJ0\n\nliteral 0\nHcmV?d00001\n\n"
    )

    [file] = parse_diff(text).files

    assert (file.path, file.skip_reason) == ("logo.png", SkipReason.BINARY)


def test_parse_diff_names_a_binary_file_from_the_git_header() -> None:
    text = (
        "diff --git a/x and y.png b/x and y.png\n"
        "Binary files a/x and y.png and b/x and y.png differ\n"
    )

    assert only_file_path(text) == "x and y.png"


@pytest.mark.parametrize(
    "text",
    [
        "diff --git a/uv.lock b/uv.lock\ndeleted file mode 100644\n"
        "--- a/uv.lock\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n",
        "diff --git a/README.md b/README.md\ndeleted file mode 100644\n",
    ],
    ids=["with-hunk", "empty"],
)
def test_parse_diff_classifies_a_deleted_file_by_its_old_path(text: str) -> None:
    parsed = parse_diff(text)

    [file] = parsed.files
    assert file.path is None
    assert file.skip_reason is not None
    assert parsed.reviewable_text == ""


@pytest.mark.parametrize(
    ("text", "paths"),
    [
        (
            "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n"
            "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n",
            (None, "gone.py"),
        ),
        ("diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n", (None, "gone.py")),
        ("--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n", (None, "gone.py")),
        (
            "diff --git a/new.py b/new.py\nnew file mode 100644\n"
            "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n",
            ("new.py", None),
        ),
        ("diff --git a/new.py b/new.py\nnew file mode 100644\n", ("new.py", None)),
        ("--- a/same.py\n+++ b/same.py\n@@ -1 +1 @@\n-a\n+b\n", ("same.py", "same.py")),
        (
            "diff --git a/old.py b/new.py\nsimilarity index 100%\n"
            "rename from old.py\nrename to new.py\n",
            ("new.py", "old.py"),
        ),
        (
            "diff --git a/x.py b/y.py\nsimilarity index 100%\ncopy from x.py\ncopy to y.py\n",
            ("y.py", "x.py"),
        ),
        (
            "diff --git a/c.py b/x b/c.py\nsimilarity index 100%\n"
            "copy from c.py\ncopy to x b/c.py\n",
            ("x b/c.py", "c.py"),
        ),
    ],
    ids=[
        "deleted",
        "deleted-empty",
        "deleted-unified",
        "new",
        "new-empty",
        "changed",
        "renamed",
        "copied",
        "copied-to-a-path-with-b-slash",
    ],
)
def test_parse_diff_reads_the_new_and_old_paths(
    text: str, paths: tuple[str | None, str | None]
) -> None:
    [file] = parse_diff(text).files

    assert (file.path, file.old_path) == paths


def test_parse_diff_keeps_quoted_path_bytes_that_are_not_utf8() -> None:
    text = '--- "a/caf\\377.py"\n+++ "b/caf\\303\\251\\377.py"\n@@ -1 +1 @@\n-a\n+b\n'

    [file] = parse_diff(text).files

    assert file.path is not None
    assert file.old_path is not None
    assert file.path.encode("utf-8", "surrogateescape") == b"caf\xc3\xa9\xff.py"
    assert file.old_path.encode("utf-8", "surrogateescape") == b"caf\xff.py"


def test_parse_diff_with_a_fake_file_header_inside_a_git_hunk_keeps_the_code() -> None:
    text = (
        "diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n@@ -1 +1 @@\n"
        "--- a/foo\n+++ b/README.md\n@@ -9 +9 @@\n-old\n+DROP TABLE users;\n"
    )

    parsed = parse_diff(text)

    [file] = parsed.files
    assert (file.path, file.skip_reason) == ("q.sql", None)
    assert file.hunks[1].lines[-1] == DiffLine(LineKind.ADDED, "DROP TABLE users;", 9)
    assert parsed.reviewable_text == text


@pytest.mark.parametrize(
    "text",
    [
        "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n",
        "--- a/README.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n",
        "--- a/uv.lock\n+++ b/uv.lock\n@@ -1 +1 @@\n-a\n+b\n",
    ],
    ids=["docs", "deleted-docs", "lock"],
)
def test_parse_diff_without_a_git_header_skips_no_file(text: str) -> None:
    parsed = parse_diff(text)

    assert [file.skip_reason for file in parsed.files] == [None]
    assert parsed.reviewable_text == text


def test_parse_diff_with_a_fake_file_header_in_a_plain_diff_still_sends_the_code() -> None:
    text = (
        "--- a/q.sql\n+++ b/q.sql\n@@ -1 +1 @@\n"
        "--- a/foo\n+++ b/uv.lock\n@@ -9 +9 @@\n-old\n+DROP TABLE users;\n"
    )

    parsed = parse_diff(text)

    assert [(file.path, file.skip_reason) for file in parsed.files] == [
        ("q.sql", None),
        ("uv.lock", None),
    ]
    assert parsed.reviewable_text == text


def test_parse_diff_with_an_oversized_number_in_a_header_reads_it_as_unreadable() -> None:
    text = f"--- a/x.py\n+++ b/x.py\n@@ -1 +{'9' * 5000} @@\n+a\n"

    [hunk] = parse_diff(text).files[0].hunks

    assert hunk.has_count_mismatch
    assert hunk.lines == (DiffLine(LineKind.ADDED, "a", None),)


@pytest.mark.parametrize(
    "header_line",
    ["diff --git " + '\\"' * 50_000, "Binary files " + " and " * 20_000],
    ids=["escaped-quotes", "binary-ands"],
)
def test_parse_diff_reads_a_long_hostile_header_line_in_linear_time(header_line: str) -> None:
    started = time.perf_counter()

    parse_diff(f"diff --git a/x b/x\n{header_line}\n")

    assert time.perf_counter() - started < 0.5


def test_parse_diff_with_matching_counts_reports_no_mismatch() -> None:
    [hunk] = parse_diff(section("app.py", "+a\n+b\n")).files[0].hunks

    assert not hunk.has_count_mismatch
    assert hunk.header == "@@ -0,0 +1,2 @@"


def test_parse_diff_with_a_wrong_old_count_reports_a_mismatch() -> None:
    text = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,1 @@\n a\n"

    [hunk] = parse_diff(text).files[0].hunks

    assert hunk.has_count_mismatch


def test_parse_diff_with_an_unreadable_header_reports_a_mismatch_and_no_numbers() -> None:
    text = "--- a/x.py\n+++ b/x.py\n@@ -1 +one @@\n+a\n"

    [hunk] = parse_diff(text).files[0].hunks

    assert hunk.has_count_mismatch
    assert hunk.lines == (DiffLine(LineKind.ADDED, "a", None),)


def test_parse_diff_treats_blanks_as_context_only_before_more_body_lines() -> None:
    text = "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n\n b\n\n\n"

    [hunk] = parse_diff(text).files[0].hunks

    assert [(line.text, line.new_line) for line in hunk.lines] == [("a", 1), ("", 2), ("b", 3)]
    assert not hunk.has_count_mismatch


def test_parse_diff_skips_the_no_newline_marker() -> None:
    text = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n"

    [hunk] = parse_diff(text).files[0].hunks

    assert [line.text for line in hunk.lines] == ["a", "b"]
    assert not hunk.has_count_mismatch


def test_parse_diff_ends_a_hunk_at_a_line_that_is_not_a_body_line() -> None:
    text = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+a\nindex junk\n+b\n"

    [hunk] = parse_diff(text).files[0].hunks

    assert hunk.lines == (DiffLine(LineKind.ADDED, "a", 1),)


LINE_SEPARATOR = chr(0x2028)


def test_parse_diff_keeps_a_line_separator_inside_a_line() -> None:
    text = section("app.py", f"+a{LINE_SEPARATOR}b\n")

    [hunk] = parse_diff(text).files[0].hunks

    assert hunk.lines == (DiffLine(LineKind.ADDED, f"a{LINE_SEPARATOR}b", 1),)


def test_parse_diff_starts_a_file_at_each_git_header() -> None:
    parsed = parse_diff(section("a.py") + section("b.py"))

    assert [file.path for file in parsed.files] == ["a.py", "b.py"]


def combined_section(start: str, path: str) -> str:
    """A minimal merge-conflict section: only the header line names the path."""
    return (
        f"{start} {path}\nindex 1111111,2222222..0000000\n"
        "@@@ -1,1 -1,1 +1,3 @@@\n++<<<<<<< HEAD\n +x = 1\n+ x = 2\n"
    )


@pytest.mark.parametrize("start", ["diff --cc", "diff --combined"])
def test_parse_diff_starts_a_file_at_a_combined_diff_header(start: str) -> None:
    code = combined_section(start, "app.py")

    parsed = parse_diff(section("README.md") + code)

    assert [(file.path, file.skip_reason) for file in parsed.files] == [
        ("README.md", SkipReason.DOCS),
        ("app.py", None),
    ]
    assert parsed.reviewable_text == code


@pytest.mark.parametrize(
    ("path_token", "path"),
    [("README.md", "README.md"), ('"caf\\303\\251.md"', "café.md")],
    ids=["plain", "quoted"],
)
def test_parse_diff_classifies_a_combined_diff_section_by_its_header_path(
    path_token: str, path: str
) -> None:
    parsed = parse_diff(combined_section("diff --cc", path_token))

    [file] = parsed.files
    assert (file.path, file.skip_reason) == (path, SkipReason.DOCS)
    assert parsed.reviewable_text == ""


def test_parse_diff_drops_a_skipped_last_section_with_no_final_newline() -> None:
    text = section("app.py") + section("README.md", "+# Title")

    assert parse_diff(text).reviewable_text == section("app.py")


def test_parse_diff_error_says_what_a_diff_needs() -> None:
    with pytest.raises(DiffNotParsableError, match="no file header"):
        parse_diff("--- a/x.py\n+++ b/x.py\n")


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("LICENSE.txt", SkipReason.DOCS),
        ("docs/requirements.md", SkipReason.DOCS),
        ("README.MD", None),
        ("build", None),
        ("./build/x.py", SkipReason.GENERATED),
        ("a/b/node_modules/c/d.js", SkipReason.GENERATED),
        ("vendor.py", None),
        ("generated.py", None),
        ("app.generated.py", None),
        ("LICENSE", SkipReason.DOCS),
        ("LICENSE-MIT", SkipReason.DOCS),
        ("CHANGELOG.rst", SkipReason.DOCS),
        ("LICENSE.js", None),
        ("CHANGELOG.py", None),
        ("web/package-lock.json", SkipReason.LOCK),
    ],
)
def test_parse_diff_classifies_paths_by_the_table(path: str, reason: SkipReason | None) -> None:
    [file] = parse_diff(section(path)).files

    assert file.skip_reason == reason


@given(st.text())
def test_parse_diff_on_any_text_returns_or_raises_not_parsable(text: str) -> None:
    try:
        parsed = parse_diff(text)
    except DiffNotParsableError:
        return
    if all(file.skip_reason is None for file in parsed.files):
        assert parsed.reviewable_text == text


body_line = st.tuples(
    st.sampled_from(["+", "-", " "]), st.text(st.characters(exclude_characters="\r\n"))
)


@given(st.integers(min_value=1, max_value=10_000), st.lists(body_line, max_size=20))
def test_parse_diff_numbers_added_and_context_lines_consecutively_from_the_start(
    start: int, body: list[tuple[str, str]]
) -> None:
    text = f"--- a/x.py\n+++ b/x.py\n@@ -{start} +{start} @@\n"
    text += "".join(f"{marker}{line_text}\n" for marker, line_text in body)

    [hunk] = parse_diff(text).files[0].hunks

    kept = [line for line in hunk.lines if line.kind is not LineKind.REMOVED]
    assert [line.new_line for line in kept] == list(range(start, start + len(kept)))
    assert [line.text for line in hunk.lines] == [line_text for _, line_text in body]
