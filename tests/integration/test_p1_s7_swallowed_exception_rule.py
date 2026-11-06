"""P1-S7: catch swallowed exceptions with no model, with stable fingerprints.

Written from the spec's criteria, design.md's Fingerprints and dedup section, its Interfaces and
ADR 0003, before the developer's code existed. Runs go through `main` with an injected
`MockProvider(findings=[])` unless a criterion scripts model output; `run_rules`,
`fingerprint_for` and `verify_findings` are called directly where a criterion names them.

The spec fixes the rule's text line, the summary's `rule_findings=` and `deduped=`, and what a
`static.rules` record holds, but not its key names. As in P1-S4's tests, values are looked for
wherever they sit in `data`; only the unanchored count is found by a key naming it.

AC9's golden values were computed independently from design.md's "Fingerprints and dedup",
Encoding (a JSON array of `v1`, file, rule part, anchor SHA-256 and ordinal, with `json.dumps`'
default escapes, and the anchor encoded with `surrogatepass`), then checked against
`fingerprint_for`. The rows reach each encoding branch: whitespace collapse, a lone surrogate, an
astral character, a tab and a `"`. The parts themselves (rule ID, `ai:<category>`, anchored line,
0-based ordinal) are also checked against `fingerprint_for` in the AC11 tests.

New modules are imported per test, so a missing one fails its own tests only.
"""

import importlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from diff_support import FINDING_DIFF, section
from repo_paths import REPO_ROOT
from test_p1_s4_audit_log import holds, list_holding, only, quoted_section, read_records

from review_bot.diff_parser import parse_diff
from review_bot.findings import Finding

pytestmark = pytest.mark.integration

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "p1_s7"
FINGERPRINT_GOLDEN = FIXTURES / "fingerprints.golden.json"
SAMPLE = REPO_ROOT / "Assignment" / "sample_diff.txt"
INVOICE = "billing/invoice.py"
RULE_ID = "swallowed-exception"
AUDIT_FLAG = "--audit-log"
RULE_TEXT = (
    "[medium/reliability] Exception swallowed: the handler discards it, so failures pass "
    "silently. Handle it, log it or re-raise it. (confidence 1.00)"
)
HEX16 = re.compile(r"[0-9a-f]{16}")
# Generous bounds: they catch a hang or quadratic work, not a slow CI machine.
BOUND_S = 30.0


def rule_line(file: str, line: int) -> str:
    return f"{file}:{line} {RULE_TEXT}"


def no_findings(threshold: str = "low") -> str:
    return f"No findings at or above {threshold}.\n"


def summary(stderr: str) -> dict[str, str]:
    """The summary line's `name=value` fields."""
    line = stderr.splitlines()[-1]
    return dict(field.split("=", 1) for field in line.split(" ") if "=" in field)


def finding(**overrides: Any) -> Finding:
    fields = {
        "file": INVOICE,
        "line": 14,
        "severity": "high",
        "category": "logic",
        "rationale": "Tax is applied twice.",
        "confidence": 0.9,
    }
    return Finding(**(fields | overrides))


def ai_line(item: Finding) -> str:
    return (
        f"{item.file}:{item.line} [{item.severity}/{item.category}] {item.rationale} "
        f"(confidence {item.confidence:.2f})"
    )


def hunk_section(path: str, header: str, body: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{header}\n{body}"


def sample_text() -> str:
    return SAMPLE.read_bytes().decode("utf-8")


def rules_module() -> Any:
    return importlib.import_module("review_bot.rules")


def fingerprint_for(**parts: Any) -> str:
    module = importlib.import_module("review_bot.fingerprint")
    result: str = module.fingerprint_for(**parts)
    return result


def run_rules(text: str) -> Any:
    return rules_module().run_rules(parse_diff(text))


def rule_fingerprints(text: str) -> list[str]:
    return [item.fingerprint for item in run_rules(text).findings]


def verify(proposed: Sequence[Finding], text: str) -> Any:
    module = importlib.import_module("review_bot.verify")
    parsed = parse_diff(text)
    rules = rules_module().run_rules(parsed).findings
    return module.verify_findings(proposed, rules, parsed)


def static_rules(log_path: Path) -> dict[str, Any]:
    return only(read_records(log_path), "static.rules")


def logged_rule_entries(log_path: Path) -> list[Any]:
    return list_holding(static_rules(log_path), lambda item: holds(item, RULE_ID))


def unanchored_count(data: dict[str, Any]) -> Any:
    """The value of the one `static.rules` key naming the unanchored hits."""
    counts = [value for key, value in data.items() if "unanchored" in key]
    assert len(counts) == 1, data
    return counts[0]


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def review(bot: Any, log_path: Path) -> Callable[..., Any]:
    """Review `diff` with `--provider mock`, the log at `log_path`, and these scripted findings."""

    def run(diff: Path, findings: Sequence[Finding] = (), *flags: str) -> Any:
        provider = bot.mock(findings=list(findings))
        return bot.review(diff, AUDIT_FLAG, str(log_path), *flags, provider=provider)

    return run


@pytest.fixture
def review_text(review: Callable[..., Any], write_diff: Callable[..., Path]) -> Callable[..., Any]:
    def run(text: str, findings: Sequence[Finding] = (), *flags: str) -> Any:
        return review(write_diff(text), findings, *flags)

    return run


# AC1: the sample diff's `except Exception: pass` is found with no model.


def test_p1_s7_ac1_sample_diff_prints_the_rule_line_at_line_35_and_exits_1(review):
    run = review(SAMPLE)

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line(INVOICE, 35) + "\n"
    assert summary(run.stderr)["rule_findings"] == "1"


def test_p1_s7_ac1_documented_command_in_a_child_process_reports_the_rule(tmp_path):
    """`--provider mock`'s own canned finding is dropped (P1-S6), so only the rule prints."""
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
    assert completed.stdout == rule_line(INVOICE, 35) + "\n"
    assert summary(completed.stderr)["rule_findings"] == "1"


def test_p1_s7_ac1_design_rule_summary_appends_its_fields_in_order(review):
    """design.md Compatibility: Phase 0's fields, then these, in this order."""
    run = review(SAMPLE)

    names = list(summary(run.stderr))
    appended = ["rule_findings", "dropped", "deduped", "skipped_files", "model_called", "run_id"]
    assert names[-len(appended) :] == appended, run.stderr
    assert summary(run.stderr)["deduped"] == "0"


def test_p1_s7_ac1_design_rule_static_rules_record_holds_the_finding(review, log_path):
    """design.md Audit log: `static.rules` holds fingerprint, rule, file, line and severity."""
    review(SAMPLE)

    (entry,) = logged_rule_entries(log_path)
    assert holds(entry, RULE_ID, INVOICE, 35, "medium"), entry
    assert any(isinstance(v, str) and HEX16.fullmatch(v) for v in entry.values()), entry
    assert unanchored_count(static_rules(log_path)) == 0


def test_p1_s7_ac1_logged_fingerprint_is_the_rule_findings_fingerprint(review, log_path):
    review(SAMPLE)

    (entry,) = logged_rule_entries(log_path)
    assert rule_fingerprints(sample_text())[0] in entry.values(), entry


def test_p1_s7_ac1_run_rules_gives_a_rule_sourced_finding():
    findings_module = importlib.import_module("review_bot.findings")

    result = run_rules(sample_text())

    (item,) = result.findings
    assert item.source == findings_module.Source.RULE
    assert item.rule == findings_module.RuleId.SWALLOWED_EXCEPTION
    assert (item.finding.file, item.finding.line) == (INVOICE, 35)
    assert (item.finding.severity, item.finding.category) == ("medium", "reliability")
    assert item.finding.confidence == 1.0
    assert HEX16.fullmatch(item.fingerprint)
    assert result.unanchored_hits == 0


# AC2: each swallowing form gives one finding, anchored at the `except` line.

SWALLOWING_HANDLERS = {
    "except_exception_pass": "+try:\n+    x()\n+except Exception:\n+    pass\n",
    "bare_except_ellipsis": "+try:\n+    x()\n+except:\n+    ...\n",
    "tuple_as_comment_blank_pass": (
        "+try:\n+    x()\n+except (A, B) as e:\n+    # deliberately ignored\n+\n+    pass\n"
    ),
    "one_line_except_pass": "+try:\n+    x()\n+except E: pass\n",
}


@pytest.mark.parametrize("body", SWALLOWING_HANDLERS.values(), ids=SWALLOWING_HANDLERS.keys())
def test_p1_s7_ac2_swallowing_handler_gives_one_finding_at_the_except_line(review_text, body):
    run = review_text(section("app.py", body))

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line("app.py", 3) + "\n"
    assert summary(run.stderr)["rule_findings"] == "1"


DEF_TRY = "+def f():\n+    try:\n+        x()\n"
INDENTED_AND_SPACED = {
    "in_a_method": (
        "+class A:\n+    def f(self):\n+        try:\n+            x()\n"
        "+        except Exception:\n+            pass\n",
        5,
    ),
    "tab_indented": ("+def f():\n+\ttry:\n+\t\tx()\n+\texcept Exception:\n+\t\tpass\n", 4),
    "one_line_ellipsis": (DEF_TRY + "+    except E: ...\n", 4),
    "one_line_no_space": (DEF_TRY + "+    except E:pass\n", 4),
    "comment_after_colon": (DEF_TRY + "+    except E:  # why\n+        pass\n", 4),
    "comment_after_pass": (DEF_TRY + "+    except E:\n+        pass  # why\n", 4),
    "dotted_exception": (DEF_TRY + "+    except os.error:\n+        pass\n", 4),
}


@pytest.mark.parametrize(
    ("body", "line"), INDENTED_AND_SPACED.values(), ids=INDENTED_AND_SPACED.keys()
)
def test_p1_s7_ac2_limits_indented_and_spaced_forms_are_found_at_the_except_line(
    review_text, body, line
):
    run = review_text(section("app.py", body))

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line("app.py", line) + "\n"


def test_p1_s7_ac2_limits_crlf_diff_is_found_too(review_text):
    text = section("app.py", SWALLOWED).replace("\n", "\r\n")

    run = review_text(text)

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line("app.py", 3) + "\n"


def test_p1_s7_ac2_limits_nested_handlers_each_swallowing_give_two_findings(review_text):
    body = (
        "+try:\n+    x()\n+except A:\n+    try:\n+        y()\n+    except B:\n+        pass\n"
        "+try:\n+    z()\n+except C:\n+    pass\n"
    )

    run = review_text(section("app.py", body))

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line("app.py", 6) + "\n" + rule_line("app.py", 10) + "\n"
    assert summary(run.stderr)["rule_findings"] == "2"


# AC3: a handler whose first statement is anything else gives nothing.

HANDLING_HANDLERS = {
    "log_then_pass": "+try:\n+    x()\n+except Exception as e:\n+    log(e)\n+    pass\n",
    "raise": "+try:\n+    x()\n+except Exception:\n+    raise\n",
    "return_none": "+try:\n+    x()\n+except Exception:\n+    return None\n",
}

NOT_SWALLOWING = {
    "one_line_raise": "+try:\n+    x()\n+except E: raise\n",
    "one_line_log_then_pass": "+try:\n+    x()\n+except E: log(e); pass\n",
    "name_starting_with_pass": "+try:\n+    x()\n+except E:\n+    passed = True\n",
    "call_starting_with_pass": "+try:\n+    x()\n+except E:\n+    pass_through(e)\n",
    "assignment_named_except": "+excepted = 1\n+pass\n",
    "commented_out_handler": "+# except Exception:\n+#     pass\n",
    "try_finally_pass": "+try:\n+    x()\n+finally:\n+    pass\n",
    "else_pass": "+try:\n+    x()\n+except E:\n+    raise\n+else:\n+    pass\n",
}


@pytest.mark.parametrize("body", HANDLING_HANDLERS.values(), ids=HANDLING_HANDLERS.keys())
def test_p1_s7_ac3_handler_doing_something_first_gives_no_finding(review_text, body):
    run = review_text(section("app.py", body))

    assert run.exit_code == 0, run.stderr
    assert run.stdout == no_findings()
    assert summary(run.stderr)["rule_findings"] == "0"


@pytest.mark.parametrize("body", NOT_SWALLOWING.values(), ids=NOT_SWALLOWING.keys())
def test_p1_s7_ac3_limits_lookalikes_give_no_finding(review_text, body):
    run = review_text(section("app.py", body))

    assert run.exit_code == 0, run.stderr
    assert run.stdout == no_findings()
    assert summary(run.stderr)["rule_findings"] == "0"


# AC4: an added `pass` under a context `except` counts; unchanged or removed code doesn't.


def test_p1_s7_ac4_context_except_with_added_pass_gives_a_finding(review_text):
    body = " try:\n     x()\n except Exception:\n+    pass\n"
    text = hunk_section("app.py", "@@ -1,3 +1,4 @@", body)

    run = review_text(text)

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line("app.py", 3) + "\n"


def test_p1_s7_ac4_limits_removed_line_between_context_except_and_added_pass(review_text):
    """The new version's handler body is `pass`: the removed `log(e)` isn't in it."""
    body = " try:\n     x()\n except Exception:\n-    log(e)\n+    pass\n"
    text = hunk_section("app.py", "@@ -1,4 +1,4 @@", body)

    run = review_text(text)

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line("app.py", 3) + "\n"


def test_p1_s7_ac4_limits_hunk_not_starting_at_line_1_anchors_at_the_new_line(review_text):
    body = " try:\n     x()\n except Exception:\n+    pass\n"
    text = hunk_section("app.py", "@@ -40,3 +57,4 @@ def f():", body)

    run = review_text(text)

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line("app.py", 59) + "\n"


UNCHANGED_HANDLERS = {
    "both_context": ("@@ -1,4 +1,5 @@", " try:\n     x()\n except Exception:\n     pass\n+y = 1\n"),
    "both_removed": ("@@ -1,4 +1,3 @@", " try:\n     x()\n-except Exception:\n-    pass\n+y = 1\n"),
}


@pytest.mark.parametrize(
    ("header", "body"), UNCHANGED_HANDLERS.values(), ids=UNCHANGED_HANDLERS.keys()
)
def test_p1_s7_ac4_handler_not_added_gives_no_finding(review_text, header, body):
    run = review_text(hunk_section("app.py", header, body))

    assert run.exit_code == 0, run.stderr
    assert run.stdout == no_findings()
    assert summary(run.stderr)["rule_findings"] == "0"


# AC5: a hunk ending at the `except` line hides the body, so nothing is found.


def test_p1_s7_ac5_hunk_ending_right_after_except_gives_no_finding(review_text):
    run = review_text(section("app.py", "+try:\n+    x()\n+except E:\n"))

    assert run.exit_code == 0, run.stderr
    assert run.stdout == no_findings()
    assert summary(run.stderr)["rule_findings"] == "0"


HIDDEN_BODIES = {
    "only_a_removed_line_after": ("@@ -1,3 +1,3 @@", " try:\n     x()\n+except E:\n-    raise\n"),
    "only_a_comment_after": ("@@ -0,0 +1,4 @@", "+try:\n+    x()\n+except E:\n+    # todo\n"),
}


@pytest.mark.parametrize(("header", "body"), HIDDEN_BODIES.values(), ids=HIDDEN_BODIES.keys())
def test_p1_s7_ac5_limits_body_not_visible_gives_no_finding(review_text, header, body):
    run = review_text(hunk_section("app.py", header, body))

    assert run.exit_code == 0, run.stderr
    assert run.stdout == no_findings()


def test_p1_s7_ac5_limits_pass_in_the_next_hunk_isnt_the_body(review_text):
    hunks = "@@ -1,2 +1,3 @@\n try:\n     x()\n+except E:\n@@ -50,1 +51,2 @@\n y = 1\n+    pass\n"

    run = review_text(f"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n{hunks}")

    assert run.exit_code == 0, run.stderr
    assert run.stdout == no_findings()


# AC6: only reviewed Python files: not `.js`, not `.pyi`, not skipped files.

SWALLOWED = "+try:\n+    x()\n+except Exception:\n+    pass\n"


@pytest.mark.parametrize("path", ["x.js", "x.pyi", "src/pb/x_pb2.py", "docs/notes.md"])
def test_p1_s7_ac6_same_lines_in_a_file_not_reviewed_as_python_give_no_finding(review_text, path):
    run = review_text(section(path, SWALLOWED) + section("app.py"))

    assert run.exit_code == 0, run.stderr
    assert run.stdout == no_findings()
    assert summary(run.stderr)["rule_findings"] == "0"


def test_p1_s7_ac6_control_the_same_lines_in_x_py_give_a_finding(review_text):
    run = review_text(section("x.js", SWALLOWED) + section("x.py", SWALLOWED))

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line("x.py", 3) + "\n"


@pytest.mark.parametrize("path", ["x.py.txt", "x_py", "x.pyc"])
def test_p1_s7_ac6_limits_names_that_only_look_like_python_give_no_finding(review_text, path):
    run = review_text(section(path, SWALLOWED))

    assert run.stdout == no_findings(), run.stderr
    assert summary(run.stderr)["rule_findings"] == "0"


def test_p1_s7_ac6_limits_non_utf8_python_path_is_reported_escaped_and_logged_quoted(
    review_text, log_path
):
    """design.md "Paths in `audit/v1`": git's `"b/caf\\377.py"` is quoted in every record."""
    run = review_text(quoted_section("caf\\377.py", SWALLOWED))

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line("caf\\udcff.py", 3) + "\n"
    quoted = {"file": '"caf\\377.py"', "path_quoted": True}
    (entry,) = logged_rule_entries(log_path)
    assert entry.items() >= quoted.items(), entry
    decided = [record["data"] for record in read_records(log_path) if record["stage"] == "policy"]
    assert len(decided) == 1, decided
    assert decided[0].items() >= quoted.items(), decided


def test_p1_s7_ac6_limits_deleted_python_file_gives_no_finding(review_text):
    deleted = (
        "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n"
        "@@ -1,4 +0,0 @@\n-try:\n-    x()\n-except Exception:\n-    pass\n"
    )

    run = review_text(deleted + section("app.py"))

    assert run.exit_code == 0, run.stderr
    assert run.stdout == no_findings()


# AC7: a hit in a hunk with an unreadable header can't anchor, so it's counted, not printed.

UNREADABLE = hunk_section("app.py", "@@ garbage @@", SWALLOWED)


def test_p1_s7_ac7_hit_under_an_unreadable_header_prints_nothing(review_text):
    run = review_text(UNREADABLE)

    assert run.exit_code == 0, run.stderr
    assert run.stdout == no_findings()
    assert summary(run.stderr)["rule_findings"] == "0"


def test_p1_s7_ac7_run_rules_counts_it_as_an_unanchored_hit():
    result = run_rules(UNREADABLE)

    assert result.findings == ()
    assert result.unanchored_hits == 1


def test_p1_s7_ac7_static_rules_record_has_the_count(review_text, log_path):
    review_text(UNREADABLE)

    assert unanchored_count(static_rules(log_path)) == 1


def test_p1_s7_ac7_limits_readable_hunk_beside_it_still_prints(review_text, log_path):
    text = (
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        f"@@ garbage @@\n{SWALLOWED}@@ -0,0 +20,4 @@\n{SWALLOWED}"
    )

    run = review_text(text)

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line("app.py", 22) + "\n"
    assert unanchored_count(static_rules(log_path)) == 1


# AC8: the rule line prints before AI lines, and the threshold applies to both.

AI_HIGH = finding(line=14, severity="high")


def test_p1_s7_ac8_rule_line_prints_first_then_the_ai_line(review):
    run = review(SAMPLE, [AI_HIGH])

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line(INVOICE, 35) + "\n" + ai_line(AI_HIGH) + "\n"
    assert summary(run.stderr)["rule_findings"] == "1"


def test_p1_s7_ac8_threshold_high_keeps_only_the_ai_high(review):
    run = review(SAMPLE, [AI_HIGH], "--threshold", "high")

    assert run.exit_code == 1, run.stderr
    assert run.stdout == ai_line(AI_HIGH) + "\n"


def test_p1_s7_ac8_threshold_high_with_an_ai_medium_exits_0(review):
    run = review(SAMPLE, [finding(severity="medium")], "--threshold", "high")

    assert run.exit_code == 0, run.stderr
    assert run.stdout == no_findings("high")


def test_p1_s7_ac8_threshold_medium_keeps_the_rule_and_drops_an_ai_low(review):
    run = review(SAMPLE, [finding(severity="low")], "--threshold", "medium")

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line(INVOICE, 35) + "\n"


def test_p1_s7_ac8_threshold_critical_on_the_rule_alone_exits_0(review):
    """Notes: P0-S1 AC2's `--threshold critical` on the sample still exits 0."""
    run = review(SAMPLE, [], "--threshold", "critical")

    assert run.exit_code == 0, run.stderr
    assert run.stdout == no_findings("critical")


def test_p1_s7_ac8_design_rule_rule_findings_are_ordered_by_file_then_line(review_text):
    """design.md Interfaces: `kept` holds rules by file and line, then AI as proposed."""
    late = "+a = 1\n+b = 2\n" + SWALLOWED
    text = section("b.py", SWALLOWED) + section("a.py", late)
    ai = finding(file="a.py", line=1, rationale="First by file.")

    run = review_text(text, [ai])

    assert run.exit_code == 1, run.stderr
    assert run.stdout.splitlines() == [rule_line("a.py", 5), rule_line("b.py", 3), ai_line(ai)]


# AC9: `fingerprint_for` gives one pinned value per input, on every platform.


def golden_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = json.loads(FINGERPRINT_GOLDEN.read_text(encoding="utf-8"))
    return cases


def parts_of(case: dict[str, Any]) -> dict[str, Any]:
    return {name: case[name] for name in ("file", "rule", "anchor_text", "ordinal")}


def test_p1_s7_ac9_fingerprint_matches_the_golden_file():
    cases = golden_cases()

    assert len(cases) >= 4
    for case in cases:
        assert fingerprint_for(**parts_of(case)) == case["fingerprint"], case


def test_p1_s7_ac9_fingerprint_is_16_lowercase_hex_digits():
    for case in golden_cases():
        assert HEX16.fullmatch(fingerprint_for(**parts_of(case))), case


GOLDEN_IN_A_CHILD = """
import json, sys
from review_bot.fingerprint import fingerprint_for
cases = json.loads(open(sys.argv[1], encoding="utf-8").read())
names = ("file", "rule", "anchor_text", "ordinal")
print(json.dumps([fingerprint_for(**{n: c[n] for n in names}) for c in cases]))
"""


@pytest.mark.parametrize("hash_seed", ["0", "1", "4242"])
def test_p1_s7_ac9_fingerprint_doesnt_depend_on_the_process(hash_seed):
    """A fresh interpreter, with a different string-hash seed each time, gives the golden values."""
    environment = os.environ | {"PYTHONHASHSEED": hash_seed}

    completed = subprocess.run(  # noqa: S603 - fixed argument list, our own interpreter
        [sys.executable, "-c", GOLDEN_IN_A_CHILD, str(FINGERPRINT_GOLDEN)],
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == [case["fingerprint"] for case in golden_cases()]


def test_p1_s7_ac9_each_part_changes_the_fingerprint():
    base = {"file": INVOICE, "rule": RULE_ID, "anchor_text": "except Exception:", "ordinal": 0}
    variants = [
        base | {"file": "billing/invoices.py"},
        base | {"rule": "open-without-encoding"},
        base | {"rule": "ai:reliability"},
        base | {"anchor_text": "except BaseException:"},
        base | {"ordinal": 1},
    ]

    values = {fingerprint_for(**parts) for parts in [base, *variants]}

    assert len(values) == 6


def test_p1_s7_ac9_limits_parts_dont_run_into_each_other():
    """A boundary moved between two parts must not give the same fingerprint."""
    first = fingerprint_for(file="a/b", rule="c", anchor_text="x", ordinal=0)
    second = fingerprint_for(file="a", rule="b/c", anchor_text="x", ordinal=0)
    third = fingerprint_for(file="a/bc", rule="", anchor_text="x", ordinal=0)

    assert len({first, second, third}) == 3


# AC10: moving or reindenting keeps the fingerprint; changing the code or the file doesn't.


def moved_sample(lines_down: int) -> str:
    text = sample_text()
    return text.replace("@@ -12,7 +12,7 @@", f"@@ -12,7 +{12 + lines_down},7 @@").replace(
        "@@ -30,6 +30,15 @@", f"@@ -30,6 +{30 + lines_down},15 @@"
    )


def test_p1_s7_ac10_hunk_moved_50_lines_down_keeps_the_fingerprint():
    moved = run_rules(moved_sample(50)).findings

    assert moved[0].finding.line == 85
    assert [item.fingerprint for item in moved] == rule_fingerprints(sample_text())


def test_p1_s7_ac10_moved_hunk_logs_the_same_fingerprint(review, review_text, log_path):
    fingerprint = rule_fingerprints(sample_text())[0]
    review(SAMPLE)
    (before,) = logged_rule_entries(log_path)
    log_path.unlink()

    review_text(moved_sample(50))

    (after,) = logged_rule_entries(log_path)
    assert holds(after, 85), after
    assert fingerprint in before.values()
    assert fingerprint in after.values()


ORIGINAL_HANDLER = "+        except Exception:\n+            pass\n"


@pytest.mark.parametrize(
    "indent",
    ["    ", "  ", "\t", "            "],
    ids=["four_spaces", "two_spaces", "tab", "twelve_spaces"],
)
def test_p1_s7_ac10_reindented_handler_keeps_the_fingerprint(indent):
    text = sample_text()
    reindented = text.replace(ORIGINAL_HANDLER, f"+{indent}except Exception:\n+{indent}    pass\n")
    assert reindented != text

    assert rule_fingerprints(reindented) == rule_fingerprints(text)


def test_p1_s7_ac10_whitespace_inside_the_line_is_collapsed():
    """design.md: whitespace collapsed and ends stripped, so spacing alone keeps the identity."""
    parts = {"file": INVOICE, "rule": RULE_ID, "ordinal": 0}

    plain = fingerprint_for(**parts, anchor_text="except Exception:")

    for spaced in ["        except Exception:", "except  Exception:", "\texcept\tException:  "]:
        assert fingerprint_for(**parts, anchor_text=spaced) == plain, spaced


def test_p1_s7_ac10_changed_code_on_the_anchored_line_changes_the_fingerprint():
    changed = sample_text().replace(
        "+        except Exception:\n", "+        except BaseException:\n"
    )

    assert len(rule_fingerprints(changed)) == 1
    assert rule_fingerprints(changed) != rule_fingerprints(sample_text())


def test_p1_s7_ac10_renamed_file_changes_the_fingerprint():
    renamed = sample_text().replace("billing/invoice.py", "billing/invoices.py")

    assert len(rule_fingerprints(renamed)) == 1
    assert rule_fingerprints(renamed) != rule_fingerprints(sample_text())


def test_p1_s7_ac10_ai_finding_on_a_moved_line_keeps_its_fingerprint():
    """Beyond rules: an AI finding's identity follows its line's text, not its number."""
    before = verify([finding(line=14)], sample_text()).kept
    after = verify([finding(line=64)], moved_sample(50)).kept

    ai_before = [item.fingerprint for item in before if item.source == "ai"]
    ai_after = [item.fingerprint for item in after if item.source == "ai"]
    assert len(ai_before) == 1
    assert ai_after == ai_before


# AC11: identical findings that aren't duplicates get different fingerprints.

TWO_HANDLERS = section("app.py", SWALLOWED + "+y = 1\n" + SWALLOWED)


def test_p1_s7_ac11_two_identical_handlers_print_two_findings(review_text):
    run = review_text(TWO_HANDLERS)

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line("app.py", 3) + "\n" + rule_line("app.py", 8) + "\n"
    assert summary(run.stderr)["rule_findings"] == "2"
    assert summary(run.stderr)["deduped"] == "0"


def test_p1_s7_ac11_two_identical_handlers_have_ordinals_0_and_1():
    """design.md: the rule part is the rule ID, and the ordinal counts from 0 in `kept` order."""
    fingerprints = rule_fingerprints(TWO_HANDLERS)

    parts = {"file": "app.py", "rule": RULE_ID, "anchor_text": "except Exception:"}
    assert fingerprints == [
        fingerprint_for(**parts, ordinal=0),
        fingerprint_for(**parts, ordinal=1),
    ]


def test_p1_s7_ac11_two_handlers_log_two_fingerprints(review_text, log_path):
    review_text(TWO_HANDLERS)

    entries = logged_rule_entries(log_path)
    assert len(entries) == 2
    assert [holds(entry, 3) for entry in entries] == [True, False]
    for entry, fingerprint in zip(entries, rule_fingerprints(TWO_HANDLERS), strict=True):
        assert fingerprint in entry.values(), entry


TWO_RATIONALES = [
    finding(line=14, rationale="Tax is applied twice."),
    finding(line=14, rationale="A zero tax rate is treated as no tax."),
]


def test_p1_s7_ac11_two_ai_findings_on_one_line_with_different_rationales_both_print(review):
    run = review(SAMPLE, TWO_RATIONALES)

    assert run.exit_code == 1, run.stderr
    assert run.stdout.splitlines()[1:] == [ai_line(item) for item in TWO_RATIONALES]
    assert summary(run.stderr)["deduped"] == "0"


def test_p1_s7_ac11_two_ai_findings_on_one_line_have_ordinals_0_and_1():
    """design.md: an untagged AI finding's rule part is `ai:<category>`."""
    kept = verify(TWO_RATIONALES, sample_text()).kept

    ai = [item.fingerprint for item in kept if item.source == "ai"]
    anchor = "return subtotal * (1 + self.tax_rate) if self.tax_rate else subtotal"
    parts = {"file": INVOICE, "rule": "ai:logic", "anchor_text": anchor}
    assert ai == [fingerprint_for(**parts, ordinal=0), fingerprint_for(**parts, ordinal=1)]


def test_p1_s7_ac11_ai_finding_and_rule_on_one_line_differ():
    on_the_handler = finding(line=35, category="reliability", rationale="Swallowed.")

    kept = verify([on_the_handler], sample_text()).kept

    assert len(kept) == 2
    assert kept[0].fingerprint != kept[1].fingerprint


# AC12: AI findings identical in file, category, line and rationale are printed once.

SAME = finding(line=14)


def test_p1_s7_ac12_identical_ai_findings_print_once_with_deduped_1(review):
    run = review(SAMPLE, [SAME, SAME])

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_line(INVOICE, 35) + "\n" + ai_line(SAME) + "\n"
    assert summary(run.stderr)["deduped"] == "1"


def test_p1_s7_ac12_verify_records_one_duplicate_of_the_kept_fingerprint():
    result = verify([SAME, SAME], sample_text())

    ai = [item for item in result.kept if item.source == "ai"]
    assert len(ai) == 1
    assert len(result.deduped) == 1
    assert ai[0].fingerprint in repr(result.deduped)


def test_p1_s7_ac12_limits_identical_but_for_confidence_is_still_a_duplicate(review):
    """design.md: only file, rule part, line and rationale are compared."""
    run = review(SAMPLE, [SAME, finding(line=14, confidence=0.4)])

    assert run.stdout.splitlines()[1:] == [ai_line(SAME)]
    assert summary(run.stderr)["deduped"] == "1"


def test_p1_s7_ac12_limits_three_identical_give_deduped_2(review):
    run = review(SAMPLE, [SAME, SAME, SAME])

    assert run.stdout.splitlines()[1:] == [ai_line(SAME)]
    assert summary(run.stderr)["deduped"] == "2"


NOT_DUPLICATES = {
    "other_category": finding(line=14, category="maintainability"),
    "other_line": finding(line=12),
    "other_rationale_by_case": finding(line=14, rationale="tax is applied twice."),
}


@pytest.mark.parametrize("other", NOT_DUPLICATES.values(), ids=NOT_DUPLICATES.keys())
def test_p1_s7_ac12_limits_one_part_differing_keeps_both(review, other):
    run = review(SAMPLE, [SAME, other])

    assert run.stdout.splitlines()[1:] == [ai_line(SAME), ai_line(other)]
    assert summary(run.stderr)["deduped"] == "0"


# AC13: the model can't claim `source`, `fingerprint` or `rule`.

MODEL_FINDING = {
    "file": "src/app.py",
    "line": 7,
    "severity": "high",
    "category": "logic",
    "rationale": "Rationale text.",
    "confidence": 0.9,
}
CLAIMS = {
    "source": "rule",
    "fingerprint": "0123456789abcdef",
    "rule": RULE_ID,
}


@pytest.mark.parametrize(("name", "value"), CLAIMS.items(), ids=CLAIMS.keys())
def test_p1_s7_ac13_model_output_claiming_a_code_set_field_exits_13(bot, write_diff, name, value):
    raw = json.dumps({"findings": [MODEL_FINDING | {name: value}]})

    run = bot.review(write_diff(FINDING_DIFF), provider=bot.mock(raw_output=raw))

    assert run.exit_code == 13, run.stderr
    assert run.stdout == ""


def test_p1_s7_ac13_control_the_same_output_without_the_field_prints(bot, write_diff):
    raw = json.dumps({"findings": [MODEL_FINDING]})

    run = bot.review(write_diff(FINDING_DIFF), provider=bot.mock(raw_output=raw))

    assert run.exit_code == 1, run.stderr
    assert run.stdout.startswith("src/app.py:7 [high/logic] ")


def test_p1_s7_ac13_limits_claim_on_a_diff_with_a_rule_hit_still_exits_13(bot, sample_diff):
    raw = json.dumps({"findings": [MODEL_FINDING | {"source": "rule"}]})

    run = bot.review(sample_diff, provider=bot.mock(raw_output=raw))

    assert run.exit_code == 13, run.stderr
    assert run.stdout == ""


# Limits: hostile sizes finish in bounded time.


def test_p1_s7_limits_many_handlers_finish_in_bounded_time(review_text):
    started = time.monotonic()
    run = review_text(section("app.py", SWALLOWED * 5_000))
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, elapsed
    assert run.exit_code == 1, run.stderr
    assert len(run.stdout.splitlines()) == 5_000
    assert summary(run.stderr)["deduped"] == "0"


def test_p1_s7_limits_40k_bare_except_lines_near_the_size_limit_finish_in_bounded_time(
    review_text,
):
    """Each `except:` is followed by another, so every clause looks ahead for a body."""
    text = section("app.py", "+except:\n" * 40_000)
    assert 350_000 < len(text.encode("utf-8")) < 400_000

    started = time.monotonic()
    run = review_text(text)
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, elapsed
    assert run.exit_code == 0, run.stderr
    assert run.stdout == no_findings()


HOSTILE_LINES = {
    "long_tuple": "+except (" + "A, " * 100_000 + "B):\n+    pass\n",
    "long_spaces_no_colon": "+except" + " " * 200_000 + "Exception\n+    pass\n",
    "long_spaces_then_colon": "+except" + " " * 200_000 + ":\n+    pass\n",
    "many_except_words": "+" + "except " * 50_000 + "\n+    pass\n",
}


@pytest.mark.parametrize("body", HOSTILE_LINES.values(), ids=HOSTILE_LINES.keys())
def test_p1_s7_limits_hostile_lines_finish_in_bounded_time(body):
    text = section("app.py", "+try:\n+    x()\n" + body)

    started = time.monotonic()
    result = run_rules(text)
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, elapsed
    assert len(result.findings) <= 1
