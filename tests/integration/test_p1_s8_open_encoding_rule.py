"""P1-S8: catch `open()` without an encoding with no model.

Written from the spec's criteria and design.md's Interfaces, Fingerprints and Audit log sections,
before the developer's code existed. Runs go through `main` with an injected
`MockProvider(findings=[])`; `run_rules` and `fingerprint_for` are called directly where a
criterion is about a finding's identity rather than its line of output.

The spec's rule is "when unsure, stay silent" (AC4). The Limits cases below apply it to forms the
spec doesn't list: a definition named `open`, unpacked arguments, and an `open(` inside a string
with an escaped quote stay silent; a real call after a string or comment-like text, or a path
string holding `encoding=`, is still found. A fourth positional argument is `open`'s `encoding`,
so `open(p, "r", -1, "utf-8")` has one and gives nothing.

New modules are imported per test, so a missing one fails its own tests only.
"""

import importlib
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from diff_support import SAMPLE_RULE_LINE, section
from repo_paths import REPO_ROOT
from test_p1_s4_audit_log import holds, list_holding, only, read_records

from review_bot.diff_parser import parse_diff

pytestmark = pytest.mark.integration

SAMPLE = REPO_ROOT / "Assignment" / "sample_diff.txt"
INVOICE = "billing/invoice.py"
RULE_ID = "open-without-encoding"
SAMPLE_OPEN_TEXT = '        with open(path, "w") as f:'
AUDIT_FLAG = "--audit-log"
RULE_TEXT = (
    '[low/reliability] open() without encoding uses the platform default. Pass encoding="utf-8", '
    "or open in binary mode. (confidence 1.00)"
)
# Generous bounds: they catch a hang or quadratic work, not a slow CI machine.
BOUND_S = 30.0
# For a run with tens of thousands of findings, each written to the audit log.
MANY_FINDINGS_BOUND_S = 120.0


def rule_line(file: str, line: int) -> str:
    return f"{file}:{line} {RULE_TEXT}"


def no_findings(threshold: str = "low") -> str:
    return f"No findings at or above {threshold}.\n"


def summary(stderr: str) -> dict[str, str]:
    """The summary line's `name=value` fields."""
    line = stderr.splitlines()[-1]
    return dict(field.split("=", 1) for field in line.split(" ") if "=" in field)


def hunk_section(path: str, header: str, body: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{header}\n{body}"


def sample_text() -> str:
    return SAMPLE.read_bytes().decode("utf-8")


def run_rules(text: str) -> Any:
    return importlib.import_module("review_bot.rules").run_rules(parse_diff(text))


def open_findings(text: str) -> list[Any]:
    return [item for item in run_rules(text).findings if item.rule == RULE_ID]


def fingerprint_for(**parts: Any) -> str:
    module = importlib.import_module("review_bot.fingerprint")
    result: str = module.fingerprint_for(**parts)
    return result


def logged_open_entries(log_path: Path) -> list[Any]:
    """This rule's `static.rules` entries, in logged order, without other rules' entries."""
    data = only(read_records(log_path), "static.rules")
    entries = list_holding(data, lambda item: holds(item, RULE_ID))
    return [entry for entry in entries if holds(entry, RULE_ID)]


def logged_unanchored_count(log_path: Path) -> Any:
    """The value of the one `static.rules` key naming the unanchored hits."""
    data = only(read_records(log_path), "static.rules")
    counts = [value for key, value in data.items() if "unanchored" in key]
    assert len(counts) == 1, data
    return counts[0]


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def review(bot: Any, log_path: Path) -> Callable[..., Any]:
    """Review `diff` with `--provider mock`, no model findings, and the log at `log_path`."""

    def run(diff: Path, *flags: str) -> Any:
        provider = bot.mock(findings=[])
        return bot.review(diff, AUDIT_FLAG, str(log_path), *flags, provider=provider)

    return run


@pytest.fixture
def review_text(review: Callable[..., Any], write_diff: Callable[..., Path]) -> Callable[..., Any]:
    def run(text: str, *flags: str) -> Any:
        return review(write_diff(text), *flags)

    return run


def assert_one_finding(run: Any, file: str, line: int) -> None:
    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line(file, line) + "\n"
    assert summary(run.stderr)["rule_findings"] == "1"


def assert_none(run: Any) -> None:
    assert run.exit_code == 0, run.stderr
    assert run.stdout == no_findings()
    assert summary(run.stderr)["rule_findings"] == "0"


# AC1: the sample diff gives the P1-S7 line at 35, then this rule's line at 39.


def test_p1_s8_ac1_sample_diff_prints_line_35_then_line_39(review):
    run = review(SAMPLE)

    assert run.exit_code == 1, run.stderr
    assert run.stdout == SAMPLE_RULE_LINE + "\n" + rule_line(INVOICE, 39) + "\n"
    assert summary(run.stderr)["rule_findings"] == "2"


def test_p1_s8_ac1_documented_command_in_a_child_process_prints_both_lines(tmp_path):
    environment = os.environ | {"REVIEW_BOT_AUDIT_LOG": str(tmp_path / "audit.jsonl")}

    completed = subprocess.run(
        [sys.executable, "-m", "review_bot", "Assignment/sample_diff.txt", "--provider", "mock"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )

    assert completed.returncode == 1, completed.stderr
    assert completed.stdout == SAMPLE_RULE_LINE + "\n" + rule_line(INVOICE, 39) + "\n"
    assert summary(completed.stderr)["rule_findings"] == "2"


def test_p1_s8_ac1_run_rules_gives_a_low_reliability_rule_finding_at_39():
    findings_module = importlib.import_module("review_bot.findings")

    (item,) = open_findings(sample_text())

    assert item.source == findings_module.Source.RULE
    assert item.rule == findings_module.RuleId.OPEN_WITHOUT_ENCODING
    assert (item.finding.file, item.finding.line) == (INVOICE, 39)
    assert (item.finding.severity, item.finding.category) == ("low", "reliability")
    assert item.finding.confidence == 1.0


def test_p1_s8_ac1_design_fingerprint_uses_the_rule_id_and_the_anchored_line():
    """design.md Fingerprints: the rule part is the rule ID; the first such hit has ordinal 0."""
    (item,) = open_findings(sample_text())

    expected = fingerprint_for(file=INVOICE, rule=RULE_ID, anchor_text=SAMPLE_OPEN_TEXT, ordinal=0)
    assert item.fingerprint == expected


def test_p1_s8_ac1_design_static_rules_record_holds_the_finding(review, log_path):
    """design.md Audit log: `static.rules` holds fingerprint, rule, file, line and severity."""
    review(SAMPLE)

    (entry,) = logged_open_entries(log_path)
    assert holds(entry, RULE_ID, INVOICE, 39, "low"), entry
    assert open_findings(sample_text())[0].fingerprint in entry.values(), entry


def test_p1_s8_ac1_threshold_medium_drops_the_low_open_finding(review):
    run = review(SAMPLE, "--threshold", "medium")

    assert run.exit_code == 1, run.stderr
    assert run.stdout == SAMPLE_RULE_LINE + "\n"


# AC2: each call without an encoding gives one finding, at the line holding `open(`.

FOUND = {
    "open_p": ("+open(p)\n", 1),
    "open_p_w": ('+open(p, "w")\n', 1),
    "open_p_mode_a_plus": ('+open(p, mode="a+")\n', 1),
    "with_open_as_f": ("+with open(p) as f:\n+    f.read()\n", 1),
    "io_open_p": ("+io.open(p)\n", 1),
    "spread_over_3_added_lines": ('+x = 1\n+f = open(\n+    p, "w"\n+)\n', 2),
}


@pytest.mark.parametrize(("body", "line"), FOUND.values(), ids=FOUND.keys())
def test_p1_s8_ac2_call_without_encoding_gives_one_finding_at_the_open_line(
    review_text, body, line
):
    assert_one_finding(review_text(section("app.py", body)), "app.py", line)


FOUND_LIMITS = {
    "single_quoted_mode": ("+f = open(p, 'w')\n", 1),
    "text_mode_rt": ('+f = open(p, "rt")\n', 1),
    "other_keyword_only": ('+f = open(p, "w", newline="")\n', 1),
    "nested_call_argument": ("+f = open(os.path.join(a, b))\n", 1),
    "path_string_holding_encoding": ('+f = open("encoding=utf-8.txt")\n', 1),
    "encoding_only_in_a_trailing_comment": ('+f = open(p, "w")  # encoding="utf-8"\n', 1),
    "string_holding_open_then_a_real_call": ('+x = "open(" + p; f = open(p)\n', 1),
    "string_holding_hash_then_a_real_call": ('+x = "#"; f = open(p)\n', 1),
    "with_open_spread_over_3_lines": ("+with open(\n+    p\n+) as f:\n", 1),
}


@pytest.mark.parametrize(("body", "line"), FOUND_LIMITS.values(), ids=FOUND_LIMITS.keys())
def test_p1_s8_ac2_limits_other_calls_without_encoding_are_found(review_text, body, line):
    assert_one_finding(review_text(section("app.py", body)), "app.py", line)


def test_p1_s8_ac2_limits_hunk_not_starting_at_line_1_anchors_at_the_new_line(review_text):
    body = " def f(p):\n     x = 1\n+    return open(p)\n"

    run = review_text(hunk_section("app.py", "@@ -40,2 +57,3 @@ class A:", body))

    assert_one_finding(run, "app.py", 59)


def test_p1_s8_ac2_limits_crlf_diff_is_found_too(review_text):
    text = section("app.py", '+x = 1\n+f = open(\n+    p, "w"\n+)\n').replace("\n", "\r\n")

    assert_one_finding(review_text(text), "app.py", 2)


# AC3: an encoding, or a binary mode, gives nothing.

WITH_ENCODING_OR_BINARY = {
    "encoding_utf_8": '+open(p, encoding="utf-8")\n',
    "mode_and_encoding_variable": '+open(p, "w", encoding=enc)\n',
    "rb": '+open(p, "rb")\n',
    "mode_wb": '+open(p, mode="wb")\n',
    "r_plus_b": '+open(p, "r+b")\n',
    "multi_line_encoding_on_last_line": '+f = open(\n+    p,\n+    encoding="utf-8")\n',
}


@pytest.mark.parametrize(
    "body", WITH_ENCODING_OR_BINARY.values(), ids=WITH_ENCODING_OR_BINARY.keys()
)
def test_p1_s8_ac3_call_with_encoding_or_binary_mode_gives_no_finding(review_text, body):
    assert_none(review_text(section("app.py", body)))


WITH_ENCODING_OR_BINARY_LIMITS = {
    "spaces_around_equals": '+f = open(p, encoding = "utf-8")\n',
    "single_quoted_binary_mode": "+f = open(p, mode='rb')\n",
    "binary_flag_first": '+f = open(p, "br")\n',
    "wb_plus": '+f = open(p, "wb+")\n',
    "xb": '+f = open(p, "xb")\n',
    "binary_mode_and_buffering": '+f = open(p, mode="rb", buffering=0)\n',
    "positional_encoding_fourth_argument": '+f = open(p, "r", -1, "utf-8")\n',
    "with_open_encoding_over_4_lines": '+with open(\n+    p,\n+    encoding="utf-8",\n+) as f:\n',
}


@pytest.mark.parametrize(
    "body", WITH_ENCODING_OR_BINARY_LIMITS.values(), ids=WITH_ENCODING_OR_BINARY_LIMITS.keys()
)
def test_p1_s8_ac3_limits_other_encoded_or_binary_calls_give_no_finding(review_text, body):
    assert_none(review_text(section("app.py", body)))


# AC4: other `open`s, comments, strings, a non-literal mode, and an unclosed call: when unsure,
# stay silent.

NOT_THE_BUILTIN_OR_UNSURE = {
    "os_open": "+os.open(p, flags)\n",
    "self_open": "+self.open(p)\n",
    "path_open": "+Path(p).open()\n",
    "reopen": "+reopen(p)\n",
    "comment": "+# open(p)\n",
    "string": '+x = "open(p)"\n',
    "non_literal_mode": "+open(p, mode)\n",
    "unclosed_at_hunk_end": "+f = open(\n+    p,\n",
}


@pytest.mark.parametrize(
    "body", NOT_THE_BUILTIN_OR_UNSURE.values(), ids=NOT_THE_BUILTIN_OR_UNSURE.keys()
)
def test_p1_s8_ac4_other_opens_and_unsure_calls_give_no_finding(review_text, body):
    assert_none(review_text(section("app.py", body)))


NOT_THE_BUILTIN_OR_UNSURE_LIMITS = {
    "method_definition": "+    def open(self, p):\n+        return 1\n",
    "single_quoted_string": "+x = 'open(p)'\n",
    "string_with_an_escaped_quote": '+x = "a \\" open(p)"\n',
    "trailing_comment": "+x = 1  # open(p)\n",
    "unpacked_positional_arguments": "+f = open(*args)\n",
    "unpacked_keyword_arguments": "+f = open(p, **options)\n",
    "non_literal_mode_keyword": "+f = open(p, mode=mode)\n",
    "other_name_ending_in_open": "+f = fopen(p)\n",
    "other_name_starting_with_open": "+f = open_file(p)\n",
    "builtins_open_out_of_scope": "+f = builtins.open(p)\n",
}


@pytest.mark.parametrize(
    "body",
    NOT_THE_BUILTIN_OR_UNSURE_LIMITS.values(),
    ids=NOT_THE_BUILTIN_OR_UNSURE_LIMITS.keys(),
)
def test_p1_s8_ac4_limits_other_lookalikes_give_no_finding(review_text, body):
    assert_none(review_text(section("app.py", body)))


def test_p1_s8_ac4_limits_call_closed_only_in_the_next_hunk_gives_no_finding(review_text):
    hunks = "@@ -1,1 +1,3 @@\n x = 1\n+f = open(\n+    p,\n@@ -50,1 +52,2 @@\n y = 2\n+)\n"

    run = review_text(f"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n{hunks}")

    assert_none(run)


# AC4 with the spec's Notes (developer): what else counts as unsure.


def call_spanning(line_count: int) -> str:
    """An added `open(p)`, from `open(` to `)`, over exactly `line_count` lines.

    Comment lines pad it: more positional arguments would give it a fourth one, the encoding.
    """
    return "+f = open(\n+    p,\n" + "+    # note\n" * (line_count - 3) + "+)\n"


def test_p1_s8_ac4_notes_call_spanning_20_lines_is_found(review_text):
    assert_one_finding(review_text(section("app.py", call_spanning(20))), "app.py", 1)


@pytest.mark.parametrize("line_count", [21, 25])
def test_p1_s8_ac4_notes_call_spanning_more_than_20_lines_gives_no_finding(review_text, line_count):
    assert_none(review_text(section("app.py", call_spanning(line_count))))


UNSURE_BY_THE_NOTES = {
    "no_file_argument": "+f = open()\n",
    "closed_by_a_mismatched_bracket": "+f = open(p]\n",
    "inside_a_triple_quoted_string": '+x = """\n+f = open(p)\n+"""\n',
}


@pytest.mark.parametrize("body", UNSURE_BY_THE_NOTES.values(), ids=UNSURE_BY_THE_NOTES.keys())
def test_p1_s8_ac4_notes_other_unsure_forms_give_no_finding(review_text, body):
    assert_none(review_text(section("app.py", body)))


def test_p1_s8_ac4_notes_real_call_after_a_triple_quoted_string_is_found(review_text):
    body = '+x = """\n+open(p)\n+"""\n+f = open(p)\n'

    assert_one_finding(review_text(section("app.py", body)), "app.py", 4)


def test_p1_s8_ac3_encoding_after_a_backslash_continuation_gives_no_finding(review_text):
    body = '+f = open(p, "w", \\\n+    encoding="utf-8")\n'

    assert_none(review_text(section("app.py", body)))


def test_p1_s8_ac4_hunk_starting_inside_a_docstring_gives_no_finding(review_text):
    """The hunk opens mid-docstring, so its closing `\"\"\"` mustn't be read as an opening one."""
    body = ' Use it like this:\n+    open(p)\n """\n'

    assert_none(review_text(hunk_section("app.py", "@@ -10,2 +10,3 @@", body)))


# AC4 and AC7: a call that isn't a hit isn't an unanchored hit either.

NOT_A_HIT_UNDER_AN_UNREADABLE_HEADER = {
    "unclosed_call": "+f = open(\n+    p,\n",
    "call_spanning_21_lines": call_spanning(21),
}


@pytest.mark.parametrize(
    "body",
    NOT_A_HIT_UNDER_AN_UNREADABLE_HEADER.values(),
    ids=NOT_A_HIT_UNDER_AN_UNREADABLE_HEADER.keys(),
)
def test_p1_s8_ac4_call_that_isnt_a_hit_isnt_counted_as_unanchored(review_text, log_path, body):
    text = hunk_section("app.py", "@@ garbage @@", body)

    run = review_text(text)

    assert_none(run)
    assert run_rules(text).unanchored_hits == 0
    assert logged_unanchored_count(log_path) == 0


# AC5: only added lines of reviewed Python files.

SKIPPED_OR_OTHER_FILES = {
    "js_file": "x.js",
    "generated_python_file": "src/pb/x_pb2.py",
    "docs_file": "docs/notes.md",
}


@pytest.mark.parametrize("path", SKIPPED_OR_OTHER_FILES.values(), ids=SKIPPED_OR_OTHER_FILES.keys())
def test_p1_s8_ac5_call_in_a_file_not_reviewed_as_python_gives_no_finding(review_text, path):
    assert_none(review_text(section(path, "+f = open(p)\n") + section("app.py")))


def test_p1_s8_ac5_control_the_same_line_in_x_py_gives_a_finding(review_text):
    run = review_text(section("x.js", "+f = open(p)\n") + section("x.py", "+f = open(p)\n"))

    assert_one_finding(run, "x.py", 1)


NOT_ADDED = {
    "context": ("@@ -1,1 +1,2 @@", " f = open(p)\n+x = 1\n"),
    "removed": ("@@ -1,1 +1,1 @@", "-f = open(p)\n+x = 1\n"),
}


@pytest.mark.parametrize(("header", "body"), NOT_ADDED.values(), ids=NOT_ADDED.keys())
def test_p1_s8_ac5_call_on_a_context_or_removed_line_gives_no_finding(review_text, header, body):
    assert_none(review_text(hunk_section("app.py", header, body)))


def test_p1_s8_ac5_limits_encoding_removed_from_a_call_is_found(review_text):
    body = '-f = open(p, encoding="utf-8")\n+f = open(p)\n'

    run = review_text(hunk_section("app.py", "@@ -1,1 +1,1 @@", body))

    assert_one_finding(run, "app.py", 1)


def test_p1_s8_ac5_limits_deleted_python_file_gives_no_finding(review_text):
    deleted = (
        "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n-f = open(p)\n"
    )

    assert_none(review_text(deleted + section("app.py")))


@pytest.mark.parametrize("path", ["x.pyi", "x.py.txt"])
def test_p1_s8_ac5_limits_names_that_arent_py_files_give_no_finding(review_text, path):
    assert_none(review_text(section(path, "+f = open(p)\n") + section("app.py")))


# AC6: each call on a line is judged on its own.

ONE_OF_TWO = {
    "encoded_first": '+a, b = open(p, encoding="utf-8"), open(q)\n',
    "encoded_second": '+a, b = open(q), open(p, encoding="utf-8")\n',
    "binary_first": '+a, b = open(p, "rb"), open(q)\n',
    "inner_call_in_the_encoding_argument": "+f = open(p, encoding=detect(open(q)))\n",
}


@pytest.mark.parametrize("body", ONE_OF_TWO.values(), ids=ONE_OF_TWO.keys())
def test_p1_s8_ac6_one_call_with_encoding_and_one_without_give_one_finding(review_text, body):
    assert_one_finding(review_text(section("app.py", body)), "app.py", 1)


TWO_WITHOUT = section("app.py", "+a, b = open(p), open(q)\n")


def test_p1_s8_ac6_two_calls_without_encoding_on_one_line_give_two_findings(review_text):
    run = review_text(TWO_WITHOUT)

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line("app.py", 1) + "\n" + rule_line("app.py", 1) + "\n"
    assert summary(run.stderr)["rule_findings"] == "2"
    assert summary(run.stderr)["deduped"] == "0"


def test_p1_s8_ac6_two_calls_on_one_line_have_different_fingerprints():
    """design.md: same file, rule and anchored line, so only the ordinal (0, then 1) differs."""
    fingerprints = [item.fingerprint for item in open_findings(TWO_WITHOUT)]

    parts = {"file": "app.py", "rule": RULE_ID, "anchor_text": "a, b = open(p), open(q)"}
    assert fingerprints == [
        fingerprint_for(**parts, ordinal=0),
        fingerprint_for(**parts, ordinal=1),
    ]


def test_p1_s8_ac6_two_calls_on_one_line_log_two_fingerprints(review_text, log_path):
    review_text(TWO_WITHOUT)

    entries = logged_open_entries(log_path)
    assert len(entries) == 2
    for entry, item in zip(entries, open_findings(TWO_WITHOUT), strict=True):
        assert item.fingerprint in entry.values(), entry


# AC7: a hit in a hunk with an unreadable header can't anchor, so it's counted, not printed.

UNREADABLE = hunk_section("app.py", "@@ garbage @@", "+f = open(p)\n")


def test_p1_s8_ac7_hit_under_an_unreadable_header_is_counted_not_printed(review_text, log_path):
    run = review_text(UNREADABLE)

    assert_none(run)
    assert run_rules(UNREADABLE).unanchored_hits == 1
    assert logged_unanchored_count(log_path) == 1


# Limits: hostile sizes finish in bounded time, through `main`.


def test_p1_s8_limits_hunk_of_many_open_lines_finishes_in_bounded_time(review_text):
    count = 10_000

    started = time.monotonic()
    run = review_text(section("app.py", "+f = open(p)\n" * count))
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, elapsed
    assert run.exit_code == 1, run.stderr
    assert len(run.stdout.splitlines()) == count
    assert summary(run.stderr)["rule_findings"] == str(count)
    assert summary(run.stderr)["deduped"] == "0"


UNCLOSED_AT_SCALE = {
    "one_call_with_40k_argument_lines": "+f = open(\n" + "+    p,\n" * 40_000,
    "40k_nested_unclosed_calls": "+open(\n" * 40_000,
    "unclosed_triple_quoted_string_over_25k_calls": '+x = """\n' + "+f = open(p)\n" * 25_000,
}


@pytest.mark.parametrize("body", UNCLOSED_AT_SCALE.values(), ids=UNCLOSED_AT_SCALE.keys())
def test_p1_s8_limits_unclosed_multi_line_call_at_scale_finishes_in_bounded_time(review_text, body):
    text = section("app.py", body)
    assert 250_000 < len(text.encode("utf-8")) < 400_000

    started = time.monotonic()
    run = review_text(text)
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, elapsed
    assert_none(run)


def test_p1_s8_limits_one_added_line_near_the_size_limit_finishes_in_bounded_time(review_text):
    """Spec Limits: the largest case is one added line of about 400 KB, all `open(p)` calls.

    Its 43,000 findings each get an audit record, which reopens the log (P1-S4): about 0.5 ms
    each on Windows, 23 s in all here. Growth is linear, so a larger bound still catches a
    quadratic step, which at this size would take hours.
    """
    count = 43_000
    text = section("app.py", "+" + "open(p), " * count + "\n")
    assert 380_000 < len(text.encode("utf-8")) < 400_000

    started = time.monotonic()
    run = review_text(text)
    elapsed = time.monotonic() - started

    assert elapsed < MANY_FINDINGS_BOUND_S, elapsed
    assert run.exit_code == 1, run.stderr
    assert summary(run.stderr)["rule_findings"] == str(count)
    assert run.stdout == (rule_line("app.py", 1) + "\n") * count


HOSTILE_LINES = {
    "50k_unclosed_calls_on_one_line": ("+" + "open(" * 50_000 + "\n", 0),
    "20k_calls_on_one_line": ("+" + "open(p), " * 20_000 + "\n", 20_000),
    "100k_deep_parentheses": ("+f = open(" + "(" * 100_000 + "p" + ")" * 100_001 + "\n", 1),
    "30k_calls_inside_one_string": ('+x = "' + "open(p) " * 30_000 + '"\n', 0),
}


@pytest.mark.parametrize(("body", "count"), HOSTILE_LINES.values(), ids=HOSTILE_LINES.keys())
def test_p1_s8_limits_hostile_lines_finish_in_bounded_time(body, count):
    text = section("app.py", body)

    started = time.monotonic()
    findings = open_findings(text)
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, elapsed
    assert len(findings) == count
