import time
from pathlib import Path

import pytest
from diff_samples import section

from review_bot.diff_parser import parse_diff
from review_bot.findings import Category, RuleId, Severity, Source
from review_bot.fingerprint import fingerprint_for
from review_bot.rules import RuleResult, run_rules

RATIONALE = (
    "Exception swallowed: the handler discards it, so failures pass silently. "
    "Handle it, log it or re-raise it."
)
SAMPLE_DIFF = Path(__file__).parents[2] / "Assignment" / "sample_diff.txt"


def hunk_diff(body: str, *, path: str = "app.py", header: str = "@@ -10,9 +10,9 @@") -> str:
    """One file with one hunk at new line 10, so body line ``n`` (0-based) is line ``10 + n``."""
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{header}\n{body}"


def hit_lines(diff: str) -> list[tuple[str, int]]:
    return hit_lines_of(run_rules(parse_diff(diff)))


def hit_lines_of(result: RuleResult) -> list[tuple[str, int]]:
    return [(item.finding.file, item.finding.line) for item in result.findings]


def test_run_rules_on_the_sample_diff_finds_the_swallowed_exception_at_line_35() -> None:
    result = run_rules(parse_diff(SAMPLE_DIFF.read_text(encoding="utf-8")))

    [reviewed] = result.findings
    assert reviewed.finding.model_dump() == {
        "file": "billing/invoice.py",
        "line": 35,
        "severity": Severity.MEDIUM,
        "category": Category.RELIABILITY,
        "rationale": RATIONALE,
        "confidence": 1.0,
    }
    assert (reviewed.source, reviewed.rule) == (Source.RULE, RuleId.SWALLOWED_EXCEPTION)
    assert reviewed.fingerprint == fingerprint_for(
        file="billing/invoice.py",
        rule="swallowed-exception",
        anchor_text="except Exception:",
        ordinal=0,
    )
    assert result.unanchored_hits == 0


@pytest.mark.parametrize(
    "body",
    [
        "+try:\n+    x()\n+except Exception:\n+    pass\n",
        "+try:\n+    x()\n+except:\n+    ...\n",
        "+try:\n+    x()\n+except (A, B) as e:\n+    # ignored on purpose\n+\n+    pass\n",
        "+try:\n+    x()\n+except E: pass\n",
        "+try:\n+    x()\n+except E:  # why\n+    pass  # noqa\n",
        "+try:\n+    x()\n+except* E:\n+    pass\n",
    ],
    ids=["pass", "ellipsis", "comment_and_blank_first", "one_line", "comments", "except_star"],
)
def test_run_rules_anchors_a_handler_doing_nothing_at_its_except_line(body: str) -> None:
    assert hit_lines(hunk_diff(body)) == [("app.py", 12)]


@pytest.mark.parametrize(
    "body",
    [
        "+except E:\n+    log(e)\n+    pass\n",
        "+except E:\n+    raise\n",
        "+except E:\n+    return None\n",
        "+except E: log(e)\n",
        "+except E:\n+    passed = True\n",
        "+except E:\n+    pass; log(e)\n",
        "+# except E:\n+    pass\n",
        "+except_handler = 1\n+pass\n",
        "+    raise ValueError('except E:')\n+    pass\n",
    ],
    ids=[
        "log_then_pass",
        "raise",
        "return",
        "one_line_call",
        "pass_prefix",
        "pass_then_call",
        "commented_out",
        "identifier",
        "string",
    ],
)
def test_run_rules_ignores_a_handler_that_does_something_first(body: str) -> None:
    assert hit_lines(hunk_diff(body)) == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (" except E:\n+    pass\n", [("app.py", 10)]),
        ("+except E:\n     pass\n", [("app.py", 10)]),
        (" except E:\n     pass\n", []),
        ("-except E:\n-    pass\n", []),
        (" except E:\n-    log(e)\n+    pass\n", [("app.py", 10)]),
        (" except E:\n-    pass\n+    raise\n", []),
    ],
    ids=[
        "added_pass",
        "added_except",
        "both_context",
        "both_removed",
        "pass_replaces_a_call",
        "raise_replaces_pass",
    ],
)
def test_run_rules_reports_a_handler_only_if_the_clause_or_statement_is_added(
    body: str, expected: list[tuple[str, int]]
) -> None:
    assert hit_lines(hunk_diff(body)) == expected


@pytest.mark.parametrize("body", ["+except E:\n", "+except E:\n+    # nothing yet\n+\n"])
def test_run_rules_ignores_a_handler_whose_body_the_hunk_does_not_show(body: str) -> None:
    assert hit_lines(hunk_diff(body)) == []


def test_run_rules_does_not_read_a_handler_across_hunks() -> None:
    diff = hunk_diff("+except E:\n@@ -20 +20 @@\n+    pass\n")

    assert hit_lines(diff) == []


@pytest.mark.parametrize("path", ["x.js", "x.pyi", "README.md", "pkg/x_pb2.py", "x.py.txt"])
def test_run_rules_checks_only_python_files_that_are_not_skipped(path: str) -> None:
    assert hit_lines(hunk_diff("+except E:\n+    pass\n", path=path)) == []


def test_run_rules_ignores_a_deleted_file() -> None:
    diff = (
        "diff --git a/x.py b/x.py\ndeleted file mode 100644\n--- a/x.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-except E:\n-    pass\n"
    )

    assert hit_lines(diff) == []


@pytest.mark.parametrize(
    "header", ["@@ -10,2 +ten,2 @@", "@@@ -1,2 -1,2 +1,3 @@@", "@@ -1,2 +0,2 @@"]
)
def test_run_rules_counts_a_hit_it_cannot_anchor_and_reports_none(header: str) -> None:
    result = run_rules(parse_diff(hunk_diff("+except E:\n+    pass\n", header=header)))

    assert result.findings == ()
    assert result.unanchored_hits == 1


def test_run_rules_orders_findings_by_file_and_line_and_numbers_identical_handlers() -> None:
    handler = "+except E:\n+    pass\n"
    diff = (
        hunk_diff(handler, path="b.py", header="@@ -1,2 +40,2 @@")
        + hunk_diff(handler, path="a.py", header="@@ -1,2 +30,2 @@")
        + "@@ -1,2 +5,2 @@\n"
        + handler
    )

    result = run_rules(parse_diff(diff))

    assert [(item.finding.file, item.finding.line) for item in result.findings] == [
        ("a.py", 5),
        ("a.py", 30),
        ("b.py", 40),
    ]
    assert [item.fingerprint for item in result.findings] == [
        fingerprint_for(
            file="a.py", rule=RuleId.SWALLOWED_EXCEPTION, anchor_text="except E:", ordinal=0
        ),
        fingerprint_for(
            file="a.py", rule=RuleId.SWALLOWED_EXCEPTION, anchor_text="except E:", ordinal=1
        ),
        fingerprint_for(
            file="b.py", rule=RuleId.SWALLOWED_EXCEPTION, anchor_text="except E:", ordinal=0
        ),
    ]


def test_run_rules_reports_a_python_path_that_is_not_utf8_as_parsed() -> None:
    diff = (
        'diff --git "a/caf\\377.py" "b/caf\\377.py"\n--- "a/caf\\377.py"\n+++ "b/caf\\377.py"\n'
        "@@ -1,2 +1,2 @@\n+except E:\n+    pass\n"
    )

    assert hit_lines(diff) == [("caf\udcff.py", 1)]


def test_run_rules_with_no_python_file_finds_nothing() -> None:
    result = run_rules(parse_diff(section("app.js", "+except E:\n+    pass\n")))

    assert (result.findings, result.unanchored_hits) == ((), 0)


def test_run_rules_reads_a_hunk_of_80000_except_lines_in_linear_time() -> None:
    # Each clause reads only up to its handler's first statement, never the rest of the hunk.
    # Quadratic work took about 24 s here; linear takes about 0.3 s, so the bound is generous.
    parsed = parse_diff(hunk_diff("+except E:\n" * 80_000 + "+    pass\n"))

    started = time.perf_counter()
    result = run_rules(parsed)

    assert time.perf_counter() - started < 3.0
    assert hit_lines_of(result) == [("app.py", 80_009)]
