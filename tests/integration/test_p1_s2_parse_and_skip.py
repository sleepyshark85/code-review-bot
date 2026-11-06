"""P1-S2: parse diffs tolerantly, skip docs, lock and generated files, reject non-diff text.

Written from the spec's criteria and Interfaces. `diff_parser.parse_diff` is called through its
module, so a check can swap the parser in one place.
"""

import re
import subprocess
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from review_bot import diff_parser
from review_bot.diff_parser import LineKind, SkipReason
from review_bot.errors import DiffNotParsableError

pytestmark = pytest.mark.integration

WARNING = "review-bot: warning: nothing was reviewed: every changed file was skipped: "


def section(path: str, body: str = "+value = 1\n") -> str:
    """One `diff --git` file section with one hunk adding `body`."""
    count = body.count("\n")
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{count} @@\n"
        f"{body}"
    )


def framed_diff(user_prompt: str) -> str:
    """The text between the opening and closing delimiter lines (P0-S1 AC13)."""
    opening, rest = user_prompt.split("\n", 1)
    diff, closing = rest.rsplit("\n", 1)
    assert opening.strip(), "no opening delimiter"
    assert closing.strip(), "no closing delimiter"
    return diff


def new_lines(hunk, *kinds) -> list[int | None]:
    return [line.new_line for line in hunk.lines if line.kind in kinds]


def only_file(text: str):
    parsed = diff_parser.parse_diff(text)
    assert len(parsed.files) == 1, [file.path for file in parsed.files]
    return parsed.files[0]


def reviewed_text(bot, write_diff, text: str) -> str:
    """Run `main` over `text` and return the diff the provider received."""
    return framed_diff(bot.request_for(write_diff(text)).user_prompt)


# AC1: the sample diff, whose hunk headers overstate their bodies.


@pytest.fixture
def sample_text(sample_diff) -> str:
    return sample_diff.read_bytes().decode("utf-8")


def test_p1_s2_ac1_sample_is_one_file_with_two_mismatched_hunks(sample_text):
    file = only_file(sample_text)

    assert file.path == "billing/invoice.py"
    assert file.skip_reason is None
    assert len(file.hunks) == 2
    assert [hunk.has_count_mismatch for hunk in file.hunks] == [True, True]


def test_p1_s2_ac1_sample_added_and_context_lines_are_12_to_14_and_30_to_41(sample_text):
    first, second = only_file(sample_text).hunks

    assert new_lines(first, LineKind.ADDED, LineKind.CONTEXT) == [12, 13, 14]
    assert new_lines(second, LineKind.ADDED, LineKind.CONTEXT) == list(range(30, 42))


def test_p1_s2_ac1_sample_added_lines_are_14_32_to_36_and_38_to_41(sample_text):
    hunks = only_file(sample_text).hunks

    added = [number for hunk in hunks for number in new_lines(hunk, LineKind.ADDED)]

    assert added == [14, 32, 33, 34, 35, 36, 38, 39, 40, 41]


def test_p1_s2_ac1_blank_before_the_second_header_is_not_a_body_line(sample_text):
    first, _ = only_file(sample_text).hunks

    assert [line.kind for line in first.lines] == [
        LineKind.CONTEXT,
        LineKind.CONTEXT,
        LineKind.REMOVED,
        LineKind.ADDED,
    ]
    assert first.lines[-1].text == (
        "        return subtotal * (1 + self.tax_rate) if self.tax_rate else subtotal"
    )


def test_p1_s2_ac1_blank_before_export_csv_is_context_line_37(sample_text):
    _, second = only_file(sample_text).hunks

    by_number = {line.new_line: line for line in second.lines if line.new_line is not None}

    assert by_number[37].kind == LineKind.CONTEXT
    assert by_number[37].text == ""
    assert by_number[38].text == "    def export_csv(self, path):"
    assert second.lines[-1].text == "        return path"


def test_p1_s2_ac1_sample_is_reviewed_whole(bot, write_diff, sample_text):
    assert reviewed_text(bot, write_diff, sample_text) == sample_text


BODY_TEXT = st.text(alphabet="abc xyz=()_:'.#", max_size=12)
BODY_LINE = st.tuples(st.sampled_from([" ", "+", "-"]), BODY_TEXT)


@settings(max_examples=60, deadline=None)
@given(
    body=st.lists(BODY_LINE, min_size=1, max_size=15),
    start=st.integers(min_value=1, max_value=500),
    declared=st.one_of(st.none(), st.tuples(st.integers(0, 30), st.integers(0, 30))),
)
def test_p1_s2_ac1_numbering_follows_the_body_not_the_declared_counts(body, start, declared):
    old_count = sum(marker != "+" for marker, _ in body)
    new_count = sum(marker != "-" for marker, _ in body)
    old_declared, new_declared = declared or (old_count, new_count)
    text = (
        f"--- a/x.py\n+++ b/x.py\n@@ -{start},{old_declared} +{start},{new_declared} @@\n"
        + "".join(f"{marker}{line}\n" for marker, line in body)
    )

    file = only_file(text)

    [hunk] = file.hunks
    kinds = {" ": LineKind.CONTEXT, "+": LineKind.ADDED, "-": LineKind.REMOVED}
    assert [(line.kind, line.text) for line in hunk.lines] == [(kinds[m], t) for m, t in body]
    assert new_lines(hunk, LineKind.CONTEXT, LineKind.ADDED) == list(
        range(start, start + new_count)
    )
    assert hunk.has_count_mismatch == ((old_declared, new_declared) != (old_count, new_count))
    assert diff_parser.parse_diff(text).reviewable_text == text


# AC2: tolerated forms.


def test_p1_s2_ac2_header_with_no_counts_numbers_from_its_start():
    text = "--- a/x.py\n+++ b/x.py\n@@ -5 +5 @@\n ctx\n-old\n+new\n"

    hunk = only_file(text).hunks[0]

    assert new_lines(hunk, LineKind.CONTEXT, LineKind.ADDED) == [5, 6]


def test_p1_s2_ac2_no_newline_marker_is_not_a_line():
    text = (
        "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,3 @@\n a\n-b\n"
        "\\ No newline at end of file\n+c\n+d\n\\ No newline at end of file\n"
    )

    hunk = only_file(text).hunks[0]

    assert [(line.kind, line.text, line.new_line) for line in hunk.lines] == [
        (LineKind.CONTEXT, "a", 1),
        (LineKind.REMOVED, "b", hunk.lines[1].new_line),
        (LineKind.ADDED, "c", 2),
        (LineKind.ADDED, "d", 3),
    ]


def test_p1_s2_ac2_crlf_endings_leave_no_carriage_return():
    text = "--- a/x.py\r\n+++ b/x.py\r\n@@ -1,2 +1,2 @@\r\n a\r\n-b\r\n+c\r\n"

    file = only_file(text)

    assert file.path == "x.py"
    assert [(line.text, line.new_line) for line in file.hunks[0].lines if line.new_line] == [
        ("a", 1),
        ("c", 2),
    ]


def test_p1_s2_ac2_file_headers_inside_a_hunk_without_a_hunk_header_are_body_lines():
    text = (
        "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n--- x\n+++ y\n b\n"
        "--- a/z.py\n+++ b/z.py\n@@ -1 +1 @@\n+z\n"
    )

    parsed = diff_parser.parse_diff(text)

    assert [file.path for file in parsed.files] == ["x.py", "z.py"]
    assert [(line.kind, line.text, line.new_line) for line in parsed.files[0].hunks[0].lines] == [
        (LineKind.CONTEXT, "a", 1),
        (LineKind.REMOVED, "-- x", parsed.files[0].hunks[0].lines[1].new_line),
        (LineKind.ADDED, "++ y", 2),
        (LineKind.CONTEXT, "b", 3),
    ]
    assert new_lines(parsed.files[1].hunks[0], LineKind.ADDED) == [1]


# AC3: an unreadable new start.


@pytest.mark.parametrize("header", ["@@ garbage @@", "@@ -1,2 +x,2 @@"])
def test_p1_s2_ac3_unreadable_new_start_gives_no_line_numbers(header):
    text = f"--- a/x.py\n+++ b/x.py\n{header}\n a\n+b\n@@ -9,1 +9,2 @@\n c\n+d\n"

    first, second = only_file(text).hunks

    assert [line.new_line for line in first.lines] == [None, None]
    assert first.has_count_mismatch
    assert new_lines(second, LineKind.CONTEXT, LineKind.ADDED) == [9, 10]


def test_p1_s2_ac3_unreadable_hunk_still_reaches_the_prompt(bot, write_diff):
    text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ garbage @@\n a\n+b\n"

    assert reviewed_text(bot, write_diff, text) == text


# AC4: paths.

PATH_CASES = {
    "rename": (
        "diff --git a/old.py b/new.py\nsimilarity index 90%\nrename from old.py\n"
        "rename to new.py\n--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-a\n+b\n",
        "new.py",
    ),
    "new_file": (
        "diff --git a/n.py b/n.py\nnew file mode 100644\nindex 0000000..1111111\n"
        "--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,2 @@\n+a\n+b\n",
        "n.py",
    ),
    "deleted_file": (
        "diff --git a/d.py b/d.py\ndeleted file mode 100644\nindex 1111111..0000000\n"
        "--- a/d.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n",
        None,
    ),
    "no_prefix": (
        "diff --git src/m.py src/m.py\n--- src/m.py\n+++ src/m.py\n@@ -1 +1 @@\n-a\n+b\n",
        "src/m.py",
    ),
    "quoted_octal": (
        'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
        '--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -1 +1 @@\n-a\n+b\n',
        "café.py",
    ),
    "plain_unified": ("--- a/p.py\n+++ b/p.py\n@@ -1 +1 @@\n-a\n+b\n", "p.py"),
    "one_prefix_stripped": ("--- a/b/q.py\n+++ b/b/q.py\n@@ -1 +1 @@\n-a\n+b\n", "b/q.py"),
}


@pytest.mark.parametrize(("text", "path"), PATH_CASES.values(), ids=PATH_CASES.keys())
def test_p1_s2_ac4_path_is_the_new_path_or_none_when_deleted(text, path):
    assert only_file(text).path == path


def test_p1_s2_ac4_new_file_numbers_from_line_1():
    text, _ = PATH_CASES["new_file"]

    assert new_lines(only_file(text).hunks[0], LineKind.ADDED) == [1, 2]


# AC5: text with no file header.

NOT_A_DIFF = {
    "plus_x": "+x\n",
    "prose": "just some notes, parsing is Phase 1\n",
    "orphan_hunks": "@@ -1,2 +1,2 @@\n a\n-b\n+c\n@@ -9 +9 @@\n+d\n",
}


@pytest.mark.parametrize("text", NOT_A_DIFF.values(), ids=NOT_A_DIFF.keys())
def test_p1_s2_ac5_parse_diff_raises_for_text_with_no_file_header(text):
    with pytest.raises(DiffNotParsableError):
        diff_parser.parse_diff(text)


@pytest.mark.parametrize("text", NOT_A_DIFF.values(), ids=NOT_A_DIFF.keys())
def test_p1_s2_ac5_text_with_no_file_header_exits_15_naming_the_path(bot, write_diff, text):
    path = write_diff(text)
    provider = bot.mock()

    run = bot.review(path, provider=provider)

    assert run.exit_code == 15, run.stderr
    assert run.stdout == ""
    lines = run.stderr.splitlines()
    assert len(lines) == 1, run.stderr
    assert lines[0].startswith("review-bot: error: ")
    assert str(path) in lines[0]
    assert provider.requests == []


# AC6: a commit-message preamble.

PREAMBLE = (
    "From 1234567890abcdef Mon Sep 17 00:00:00 2001\n"
    "Subject: [PATCH] Fix the total\n"
    "\n"
    "The total ignored tax.\n"
    "---\n"
    " app.py | 2 +-\n"
    "\n"
)


def test_p1_s2_ac6_preamble_is_not_rejected_and_stays_in_reviewable_text():
    text = PREAMBLE + section("app.py")

    parsed = diff_parser.parse_diff(text)

    assert [file.path for file in parsed.files] == ["app.py"]
    assert parsed.reviewable_text == text


def test_p1_s2_ac6_preamble_reaches_the_prompt(bot, write_diff):
    text = PREAMBLE + section("app.py")

    assert reviewed_text(bot, write_diff, text) == text


# AC7: skipped sections leave the prompt; everything else stays byte for byte.

APP = section("app.py", "+def total():\n+    return 1\n")
MIXED = (
    PREAMBLE
    + section("README.md", "+# Read me\n")
    + APP
    + section("uv.lock", '+version = "1"\n')
    + section("src/pb/x_pb2.py", "+DESCRIPTOR = None\n")
)


def test_p1_s2_ac7_prompt_holds_only_the_preamble_and_app_section(bot, write_diff):
    assert reviewed_text(bot, write_diff, MIXED) == PREAMBLE + APP


def test_p1_s2_ac7_skip_reasons_follow_the_path_rules():
    parsed = diff_parser.parse_diff(MIXED)

    assert [(file.path, file.skip_reason) for file in parsed.files] == [
        ("README.md", SkipReason.DOCS),
        ("app.py", None),
        ("uv.lock", SkipReason.LOCK),
        ("src/pb/x_pb2.py", SkipReason.GENERATED),
    ]
    assert parsed.reviewable_text == PREAMBLE + APP


def test_p1_s2_ac7_summary_counts_skipped_files_and_the_model_call(bot, write_diff):
    run = bot.review(write_diff(MIXED), provider=bot.mock())

    assert run.exit_code == 1, run.stderr
    assert "skipped_files=3 model_called=true" in run.stderr.splitlines()[-1]


# AC8: nothing skipped, so the input passes unchanged.


def test_p1_s2_ac8_crlf_diff_reaches_the_prompt_byte_for_byte(bot, write_diff):
    text = (section("a.py") + section("b/c.py", "+x = 1\n+y = 2")).replace("\n", "\r\n")

    assert diff_parser.parse_diff(text).reviewable_text == text
    assert reviewed_text(bot, write_diff, text) == text


# AC9: every file skipped.

ALL_SKIPPED = section("README.md") + section("uv.lock") + section("docs/guide.md")


def test_p1_s2_ac9_all_skipped_exits_0_with_warning(bot, write_diff):
    provider = bot.mock()

    run = bot.review(write_diff(ALL_SKIPPED), provider=provider)

    assert run.exit_code == 0, run.stderr
    assert provider.requests == []
    assert run.stdout == "No findings at or above low.\n"
    warnings = [line for line in run.stderr.splitlines() if line.startswith(WARNING)]
    assert len(warnings) == 1, run.stderr
    named = warnings[0].removeprefix(WARNING)
    assert all(path in named for path in ("README.md", "uv.lock", "docs/guide.md")), named


def test_p1_s2_ac9_all_skipped_summary_says_the_model_wasnt_called(bot, write_diff):
    run = bot.review(write_diff(ALL_SKIPPED), provider=bot.mock())

    assert "skipped_files=3 model_called=false" in run.stderr.splitlines()[-1]


def test_p1_s2_ac9_warning_names_ten_paths_then_a_count(bot, write_diff):
    paths = [f"docs/{letter}.md" for letter in "abcdefghijkl"]
    provider = bot.mock()

    run = bot.review(write_diff("".join(section(path) for path in paths)), provider=provider)

    assert run.exit_code == 0, run.stderr
    assert provider.requests == []
    [warning] = [line for line in run.stderr.splitlines() if line.startswith(WARNING)]
    named = warning.removeprefix(WARNING)
    assert [path for path in paths if path in named] == paths[:10], named
    rest = named.split(paths[9], 1)[1]
    assert re.search(r"\b(2|12)\b", rest), named
    assert "skipped_files=12 model_called=false" in run.stderr.splitlines()[-1]


# AC10: content can't opt a file out; binary sections are skipped.


@pytest.mark.parametrize("marker", ["# @generated", "# DO NOT EDIT"])
def test_p1_s2_ac10_generated_marker_in_content_is_still_reviewed(bot, write_diff, marker):
    text = section("app.py", f"+{marker}\n+x = 1\n")

    assert only_file(text).skip_reason is None
    assert reviewed_text(bot, write_diff, text) == text


BINARY = (
    "diff --git a/x.bin b/x.bin\n"
    "index 1111111..2222222 100644\n"
    "Binary files a/x.bin and b/x.bin differ\n"
)


def test_p1_s2_ac10_binary_section_is_skipped_as_binary():
    parsed = diff_parser.parse_diff(APP + BINARY)

    assert [(file.path, file.skip_reason) for file in parsed.files] == [
        ("app.py", None),
        ("x.bin", SkipReason.BINARY),
    ]


def test_p1_s2_ac10_binary_section_leaves_the_prompt(bot, write_diff):
    assert reviewed_text(bot, write_diff, BINARY + APP) == APP


# AC11: the path rules' edges.

REVIEWED = [
    "requirements.txt",
    "requirements-dev.txt",
    "CMakeLists.txt",
    "docs/conf.py",
    "src/build/x.py",
    "src/dist/x.py",
]
SKIPPED = {
    "notes.txt": SkipReason.DOCS,
    "docs/guide.md": SkipReason.DOCS,
    "build/x.py": SkipReason.GENERATED,
    "dist/x.py": SkipReason.GENERATED,
    "src/vendor/x.py": SkipReason.GENERATED,
}


@pytest.mark.parametrize("path", REVIEWED)
def test_p1_s2_ac11_path_is_not_skipped(path):
    assert only_file(section(path)).skip_reason is None


@pytest.mark.parametrize("path", REVIEWED)
def test_p1_s2_ac11_path_is_reviewed(bot, write_diff, path):
    text = section(path)

    assert reviewed_text(bot, write_diff, text) == text


@pytest.mark.parametrize(("path", "reason"), SKIPPED.items(), ids=SKIPPED.keys())
def test_p1_s2_ac11_path_is_skipped(path, reason):
    assert only_file(section(path)).skip_reason == reason


@pytest.mark.parametrize("path", SKIPPED)
def test_p1_s2_ac11_skipped_path_leaves_the_prompt(bot, write_diff, path):
    assert reviewed_text(bot, write_diff, section(path) + APP) == APP


# The rest of the Interfaces table, at parse level.

LOCK_FILES = (
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "package-lock.json",
    "web/yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "Gemfile.lock",
)

TABLE = {
    "docs/a.rst": SkipReason.DOCS,
    "guide.adoc": SkipReason.DOCS,
    "LICENSE": SkipReason.DOCS,
    "LICENSE-MIT": SkipReason.DOCS,
    "LICENSE.md": SkipReason.DOCS,
    "CHANGELOG.md": SkipReason.DOCS,
    "CHANGELOG.rst": SkipReason.DOCS,
    "CHANGELOG": SkipReason.DOCS,
    **dict.fromkeys(LOCK_FILES, SkipReason.LOCK),
    "static/app.min.js": SkipReason.GENERATED,
    "static/app.min.css": SkipReason.GENERATED,
    "static/app.js.map": SkipReason.GENERATED,
    "api/x_pb2_grpc.py": SkipReason.GENERATED,
    "api/x.pb.go": SkipReason.GENERATED,
    "web/node_modules/left-pad/index.js": SkipReason.GENERATED,
    "vendor/lib.py": SkipReason.GENERATED,
}
# Fix round 1 tightened the table: naming a file can't hide its code.
NAMED_LIKE_DOCS_OR_GENERATED = [
    "CHANGELOG.py",
    "LICENSE.js",
    "x.generated.py",
    "api/client.generated.ts",
]


@pytest.mark.parametrize(("path", "reason"), TABLE.items(), ids=TABLE.keys())
def test_p1_s2_ac11_interfaces_table_path_is_skipped_with_its_reason(path, reason):
    assert only_file(section(path)).skip_reason == reason


@pytest.mark.parametrize("path", NAMED_LIKE_DOCS_OR_GENERATED)
def test_p1_s2_ac11_code_named_like_docs_or_generated_is_not_skipped(path):
    assert only_file(section(path)).skip_reason is None


@pytest.mark.parametrize("path", NAMED_LIKE_DOCS_OR_GENERATED)
def test_p1_s2_ac11_code_named_like_docs_or_generated_is_reviewed(bot, write_diff, path):
    text = section(path)

    assert reviewed_text(bot, write_diff, text) == text


# Fix round 1 regressions.

# AC7: a file header faked inside a hunk. The hunk's declared counts are already met by the two
# lines before the second `@@`, so only `diff --git` can say where the file ends.
FAKED_BODY = (
    "--- a/q.sql\n+++ b/q.sql\n@@ -1 +1 @@\n"
    "--- a/foo\n+++ b/README.md\n@@ -9 +9 @@\n-old\n+DROP TABLE users;\n"
)


def test_p1_s2_ac7_faked_file_header_in_a_git_diff_stays_in_one_file():
    parsed = diff_parser.parse_diff("diff --git a/q.sql b/q.sql\n" + FAKED_BODY)

    assert [(file.path, file.skip_reason) for file in parsed.files] == [("q.sql", None)]


def test_p1_s2_ac7_faked_file_header_in_a_git_diff_cant_hide_lines(bot, write_diff):
    text = "diff --git a/q.sql b/q.sql\n" + FAKED_BODY

    prompt = bot.request_for(write_diff(text)).user_prompt

    assert "DROP TABLE users;" in prompt
    assert framed_diff(prompt) == text


# Spec Notes, decided 2026-11-06: with no `diff --git` line, a faked header can't be told from a
# real one, so a plain unified diff skips no files.


def test_p1_s2_ac7_plain_unified_faked_header_cant_hide_lines():
    parsed = diff_parser.parse_diff(FAKED_BODY)

    assert [file.skip_reason for file in parsed.files if file.skip_reason] == []
    assert parsed.reviewable_text == FAKED_BODY


def test_p1_s2_ac7_plain_unified_faked_header_reaches_the_prompt(bot, write_diff):
    prompt = bot.request_for(write_diff(FAKED_BODY)).user_prompt

    assert "DROP TABLE users;" in prompt
    assert framed_diff(prompt) == FAKED_BODY


def test_p1_s2_ac9_plain_unified_readme_is_reviewed_not_skipped(bot, write_diff):
    text = "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n"
    provider = bot.mock()

    run = bot.review(write_diff(text), provider=provider)

    assert only_file(text).skip_reason is None
    assert run.exit_code == 1, run.stderr
    assert len(provider.requests) == 1
    assert framed_diff(provider.requests[0].user_prompt) == text
    assert "model_called=true" in run.stderr.splitlines()[-1]


# AC3: numbers too long for `int()` under Python's 4300-digit limit.

HUGE = "9" * 5000
HUGE_HEADERS = {"new_start": f"@@ -1 +{HUGE},1 @@", "new_count": f"@@ -1 +1,{HUGE} @@"}


@pytest.mark.parametrize("header", HUGE_HEADERS.values(), ids=HUGE_HEADERS.keys())
def test_p1_s2_ac3_5000_digit_header_number_gives_no_line_numbers(header):
    hunk = only_file(f"--- a/x.py\n+++ b/x.py\n{header}\n a\n+b\n").hunks[0]

    assert [line.new_line for line in hunk.lines] == [None, None]
    assert hunk.has_count_mismatch


@pytest.mark.parametrize("header", HUGE_HEADERS.values(), ids=HUGE_HEADERS.keys())
def test_p1_s2_ac3_5000_digit_header_number_is_reviewed_not_exit_14(bot, write_diff, header):
    text = f"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n{header}\n a\n+b\n"

    assert reviewed_text(bot, write_diff, text) == text


# Parse time: crafted lines near the default size limit. Before the fix they took 53 s at 120 KB
# and 74 s at 300 KB; after it, about 1 ms at 400 KB. The 1 s bound leaves a margin of about
# 1000 times, yet a quadratic scan that only copies each suffix (2 to 5 s here) still fails.
# It runs in a child, so a regression fails at the timeout instead of blocking the suite.

DEFAULT_LIMIT_BYTES = 400_000
QUOTE_PAIR = chr(92) + '"'
CRAFTED = {
    "git_line_of_escaped_quotes": "diff --git "
    + QUOTE_PAIR * ((DEFAULT_LIMIT_BYTES - 200) // 2)
    + "\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+a\n",
    "binary_line_of_ands": "diff --git a/x b/x\nBinary files "
    + " and " * ((DEFAULT_LIMIT_BYTES - 200) // 5)
    + "\n",
}
TIMED_PARSE = """
import pathlib, sys, time
from review_bot.diff_parser import parse_diff
from review_bot.errors import DiffNotParsableError
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
start = time.perf_counter()
try:
    parse_diff(text)
except DiffNotParsableError:
    pass
print(time.perf_counter() - start)
"""


@pytest.mark.parametrize("text", CRAFTED.values(), ids=CRAFTED.keys())
def test_p1_s2_crafted_line_near_the_size_limit_parses_quickly(write_diff, text):
    path = write_diff(text)
    assert DEFAULT_LIMIT_BYTES - 300 < path.stat().st_size <= DEFAULT_LIMIT_BYTES

    completed = subprocess.run(  # noqa: S603 - fixed arguments, our own interpreter
        [sys.executable, "-c", TIMED_PARSE, str(path)],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert float(completed.stdout) < 1.0


# Deleted files are classified by their old path.


def deleted(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\ndeleted file mode 100644\nindex 1111111..0000000\n"
        f"--- a/{path}\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-old one\n-old two\n"
    )


@pytest.mark.parametrize(
    ("path", "reason"), [("uv.lock", SkipReason.LOCK), ("README.md", SkipReason.DOCS)]
)
def test_p1_s2_ac11_deleted_file_is_skipped_by_its_old_path(path, reason):
    file = only_file(deleted(path))

    assert (file.path, file.skip_reason) == (None, reason)


def test_p1_s2_ac11_deleted_skipped_file_leaves_the_prompt(bot, write_diff):
    assert reviewed_text(bot, write_diff, deleted("README.md") + APP) == APP


def test_p1_s2_ac11_deleted_code_file_is_still_reviewed(bot, write_diff):
    text = deleted("app.py")

    assert only_file(text).skip_reason is None
    assert reviewed_text(bot, write_diff, text) == text


def test_p1_s2_ac9_only_deleted_skipped_files_warn_as_deleted_file(bot, write_diff):
    provider = bot.mock()

    run = bot.review(write_diff(deleted("uv.lock") + deleted("README.md")), provider=provider)

    assert run.exit_code == 0, run.stderr
    assert provider.requests == []
    [warning] = [line for line in run.stderr.splitlines() if line.startswith(WARNING)]
    assert warning.removeprefix(WARNING).count("(deleted file)") == 2, warning
    assert "skipped_files=2 model_called=false" in run.stderr.splitlines()[-1]


# AC10: a `GIT binary patch` section (`git diff --binary`).

GIT_BINARY = (
    "diff --git a/x.bin b/x.bin\nindex 1111111..2222222 100644\nGIT binary patch\n"
    "literal 5\nMcmZ?b00001\n\nliteral 0\nHcmV?d00001\n\n"
)


def test_p1_s2_ac10_git_binary_patch_is_skipped_as_binary():
    parsed = diff_parser.parse_diff(GIT_BINARY + APP)

    assert [(file.path, file.skip_reason) for file in parsed.files] == [
        ("x.bin", SkipReason.BINARY),
        ("app.py", None),
    ]


def test_p1_s2_ac10_git_binary_patch_leaves_the_prompt(bot, write_diff):
    assert reviewed_text(bot, write_diff, GIT_BINARY + APP) == APP
