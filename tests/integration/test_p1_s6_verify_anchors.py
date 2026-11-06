"""P1-S6: drop model findings whose file or line isn't in the diff.

Written from the spec's criteria, design.md's line-anchor rule and its Interfaces, before the
developer's code existed. Every run goes through `main` with an injected `MockProvider`, except
AC11's, which uses `--provider mock`'s own provider, in process and in a child process.

The spec fixes what a `verify` and a `policy` record hold but not their key names, so, as in
P1-S4's tests, most values a criterion names are looked for wherever they sit in `data`. The
event names (`verify.checked`, `policy.decided`), the `outcome` values (`kept`, `dropped`;
`reported`, `below_threshold`) and the logged `file` of a kept finding are the developer's
choices, which the spec leaves open; the AC9 tests pin them.

Most criteria run over `tests/fixtures/p1_s6/anchors.diff`: the sample diff with its exact hunk
headers and line numbers, but `except ValueError:` then `raise`, and `open` with an encoding, so
later rule slices don't change these results. Its added and context lines are 12 to 14 and 30 to
41 (P1-S2 AC1).

`verify_findings` is imported per test, so a missing module fails its own tests only.
"""

import hashlib
import importlib
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from diff_support import MINIMAL_DIFF, section
from hypothesis import HealthCheck, given, seed, settings
from hypothesis import strategies as st
from repo_paths import REPO_ROOT
from test_p1_s4_audit_log import read_records, scalars

from review_bot.diff_parser import LineKind, ParsedDiff, parse_diff
from review_bot.errors import DiffNotParsableError
from review_bot.findings import Finding

pytestmark = pytest.mark.integration

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "p1_s6"
ANCHORS = FIXTURES / "anchors.diff"
INVOICE = "billing/invoice.py"
AUDIT_FLAG = "--audit-log"
NO_FINDINGS_LOW = "No findings at or above low.\n"
NO_FINDINGS_HIGH = "No findings at or above high.\n"
CANNED_NOTICE = "review-bot: warning: MockProvider output is canned, not a real review."
DROP_REASONS = frozenset({"unknown_file", "skipped_file", "line_not_in_diff"})
# The developer's names, which the spec leaves open: one event per stage, and its `outcome`.
STAGE_EVENTS = {"verify": "checked", "policy": "decided"}
# Generous bounds: they catch a hang or quadratic work, not a slow CI machine.
BOUND_S = 30.0


def all_dropped_warning(count: int) -> str:
    return (
        f"review-bot: warning: all {count} model findings were dropped: "
        "their file or line isn't in the diff."
    )


def finding(**overrides: Any) -> Finding:
    fields = {
        "file": INVOICE,
        "line": 12,
        "severity": "high",
        "category": "logic",
        "rationale": "Rationale text.",
        "confidence": 0.9,
    }
    return Finding(**(fields | overrides))


def golden_line(line: int) -> str:
    """The Phase 0 line for `finding(line=line)`, from the slice's golden file."""
    lines = (FIXTURES / "kept_findings.golden.txt").read_text("utf-8").splitlines()
    matches = [text for text in lines if text.startswith(f"{INVOICE}:{line} ")]
    assert len(matches) == 1, f"no golden line for {line}"
    return matches[0]


def summary(stderr: str) -> dict[str, str]:
    """The summary line's `name=value` fields."""
    line = stderr.splitlines()[-1]
    return dict(field.split("=", 1) for field in line.split(" ") if "=" in field)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Reading the log.


def stage_records(log_path: Path, stage: str) -> list[dict[str, Any]]:
    """The `data` of each record from `stage`, in log order. Each must be the stage's one event."""
    records = [record for record in read_records(log_path) if record["stage"] == stage]
    assert {record["event"] for record in records} <= {STAGE_EVENTS[stage]}, records
    return [record["data"] for record in records]


def drop_reason(data: dict[str, Any]) -> str | None:
    """The one `DropReason` value a `verify` record holds, or `None` if it holds none."""
    reasons = [value for value in scalars(data) if value in DROP_REASONS]
    assert len(reasons) <= 1, f"more than one drop reason in {data}"
    return reasons[0] if reasons else None


def logged_reasons(log_path: Path) -> list[str | None]:
    return [drop_reason(data) for data in stage_records(log_path, "verify")]


def holds_digest(data: dict[str, Any], digest: str) -> bool:
    """True if a string in `data` holds `digest`, alone or with a prefix such as `sha256:`."""
    return any(isinstance(value, str) and digest in value for value in scalars(data))


def dropped_warnings(stderr: str) -> list[str]:
    """Stderr's warnings about dropped findings, in either number."""
    return [
        line
        for line in stderr.splitlines()
        if line.startswith("review-bot: warning:") and " dropped:" in line
    ]


def log_text(log_path: Path) -> str:
    """The log as written, and once more with its JSON escapes decoded."""
    raw = log_path.read_text(encoding="ascii")
    decoded = "\n".join(
        str(value) for record in read_records(log_path) for value in scalars(record)
    )
    return raw + "\n" + decoded


# Fixtures.


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def review(bot: Any, log_path: Path) -> Callable[..., Any]:
    """Review `diff` with `--provider mock`, the log at `log_path`, and these scripted findings."""

    def run(diff: Path, findings: Sequence[Finding], *flags: str) -> Any:
        provider = bot.mock(findings=list(findings))
        return bot.review(diff, AUDIT_FLAG, str(log_path), *flags, provider=provider)

    return run


def deleted_section(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\ndeleted file mode 100644\nindex 1111111..0000000\n"
        f"--- a/{path}\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a = 1\n-b = 2\n"
    )


def hunk_section(path: str, header: str, body: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{header}\n{body}"


# AC1: a finding on an added or context line prints unchanged.

KEPT_LINES = [12, 14, 30, 37, 41]


@pytest.mark.parametrize("line", KEPT_LINES)
def test_p1_s6_ac1_finding_on_a_diff_line_prints_in_the_unchanged_format_and_exits_1(review, line):
    run = review(ANCHORS, [finding(line=line)])

    assert run.exit_code == 1, run.stderr
    assert run.stdout == golden_line(line) + "\n"
    assert summary(run.stderr)["dropped"] == "0"


def test_p1_s6_ac1_all_five_findings_print_as_the_golden_file(review):
    run = review(ANCHORS, [finding(line=line) for line in KEPT_LINES])

    assert run.exit_code == 1, run.stderr
    assert run.stdout == (FIXTURES / "kept_findings.golden.txt").read_text("utf-8")


@pytest.mark.parametrize("line", [13, 31, 32, 36, 38])
def test_p1_s6_ac1_design_rule_every_other_added_or_context_line_is_kept(review, line):
    run = review(ANCHORS, [finding(line=line)])

    assert run.exit_code == 1, run.stderr
    assert run.stdout.startswith(f"{INVOICE}:{line} [high/logic] ")


# AC2: a line outside the body is dropped, even inside the header's declared range.

DROPPED_LINES = [1, 11, 15, 18, 29, 42, 44]


@pytest.mark.parametrize("line", DROPPED_LINES)
def test_p1_s6_ac2_finding_off_the_bodys_lines_is_dropped_and_exits_0(review, log_path, line):
    run = review(ANCHORS, [finding(line=line)])

    assert run.exit_code == 0, run.stderr
    assert run.stdout == NO_FINDINGS_LOW
    assert summary(run.stderr)["dropped"] == "1"
    assert logged_reasons(log_path) == ["line_not_in_diff"]


@pytest.mark.parametrize("line", [2**63 + 1, 10**18])
def test_p1_s6_ac2_limits_huge_line_number_is_dropped(review, log_path, line):
    run = review(ANCHORS, [finding(line=line)])

    assert run.exit_code == 0, run.stderr
    assert run.stdout == NO_FINDINGS_LOW
    assert logged_reasons(log_path) == ["line_not_in_diff"]


# AC3: a file that isn't a diff path is dropped as `unknown_file`.

UNKNOWN_FILES = {
    "other_file": "other.py",
    "parent_directory": "../billing/invoice.py",
    "upper_case": "BILLING/INVOICE.PY",
    "escape_after": "billing/invoice.py\x1b[0m",
    "escape_before": "\x1b[31mbilling/invoice.py",
}

# design.md: exact, then once more without one leading `./`, `a/` or `b/`; nothing else.
UNKNOWN_BY_DESIGN_RULE = {
    "two_prefixes_a_b": "a/b/billing/invoice.py",
    "two_prefixes_dot_b": "./b/billing/invoice.py",
    "double_dot_slash": ".//billing/invoice.py",
    "other_prefix": "c/billing/invoice.py",
    "leading_slash": "/billing/invoice.py",
    "backslashes": "billing\\invoice.py",
    "trailing_space": "billing/invoice.py ",
    "trailing_newline": "billing/invoice.py\n",
    "suffix_only": "invoice.py",
    "prefix_only": "billing/invoice",
}


@pytest.mark.parametrize("file", UNKNOWN_FILES.values(), ids=UNKNOWN_FILES.keys())
def test_p1_s6_ac3_file_not_in_the_diff_is_dropped_as_unknown_file(review, log_path, file):
    run = review(ANCHORS, [finding(file=file, line=14)])

    assert run.exit_code == 0, run.stderr
    assert run.stdout == NO_FINDINGS_LOW
    assert summary(run.stderr)["dropped"] == "1"
    assert logged_reasons(log_path) == ["unknown_file"]


@pytest.mark.parametrize("file", UNKNOWN_BY_DESIGN_RULE.values(), ids=UNKNOWN_BY_DESIGN_RULE.keys())
def test_p1_s6_ac3_design_rule_only_one_known_prefix_is_ever_stripped(review, log_path, file):
    run = review(ANCHORS, [finding(file=file, line=14)])

    assert run.exit_code == 0, run.stderr
    assert run.stdout == NO_FINDINGS_LOW
    assert logged_reasons(log_path) == ["unknown_file"]


# AC4: one leading `./`, `a/` or `b/` is stripped, but only after an exact match fails.


@pytest.mark.parametrize("file", ["./billing/invoice.py", "a/billing/invoice.py", "b/" + INVOICE])
def test_p1_s6_ac4_prefixed_file_is_kept_and_reported_as_the_diff_path(review, file):
    run = review(ANCHORS, [finding(file=file, line=14)])

    assert run.exit_code == 1, run.stderr
    assert run.stdout == golden_line(14) + "\n"


def test_p1_s6_ac4_real_path_starting_with_b_stays_as_it_is(review, write_diff):
    text = section("b/x.py")
    assert [file.path for file in parse_diff(text).files] == ["b/x.py"]

    run = review(write_diff(text), [finding(file="b/x.py", line=1)])

    assert run.exit_code == 1, run.stderr
    assert run.stdout.startswith("b/x.py:1 [high/logic] ")


@pytest.mark.parametrize(
    ("proposed", "reported"),
    [("b/x.py", "b/x.py"), ("x.py", "x.py"), ("a/b/x.py", "b/x.py"), ("./x.py", "x.py")],
)
def test_p1_s6_ac4_exact_match_wins_over_the_stripped_one(review, write_diff, proposed, reported):
    text = section("x.py") + section("b/x.py")

    run = review(write_diff(text), [finding(file=proposed, line=1)])

    assert run.exit_code == 1, run.stderr
    assert run.stdout.startswith(f"{reported}:1 [high/logic] ")


# AC5: a finding on a skipped file is dropped as `skipped_file`.


@pytest.mark.parametrize("skipped", ["README.md", "uv.lock", "src/pb/x_pb2.py"])
def test_p1_s6_ac5_finding_on_a_skipped_file_is_dropped(review, write_diff, log_path, skipped):
    text = section(skipped) + section("app.py")

    run = review(write_diff(text), [finding(file=skipped, line=1)])

    assert run.exit_code == 0, run.stderr
    assert run.stdout == NO_FINDINGS_LOW
    assert summary(run.stderr)["dropped"] == "1"
    assert logged_reasons(log_path) == ["skipped_file"]


def test_p1_s6_ac5_reviewed_file_beside_it_still_prints(review, write_diff, log_path):
    text = section("README.md") + section("app.py")

    run = review(
        write_diff(text), [finding(file="README.md", line=1), finding(file="app.py", line=1)]
    )

    assert run.exit_code == 1, run.stderr
    assert run.stdout.startswith("app.py:1 [high/logic] ")
    assert len(run.stdout.splitlines()) == 1
    assert logged_reasons(log_path) == ["skipped_file", None]


# AC6: deleted files, unreadable headers and removed-only hunks give nothing to anchor to.


@pytest.mark.parametrize("line", [1, 2])
def test_p1_s6_ac6_finding_on_a_deleted_files_old_path_is_unknown_file(
    review, write_diff, log_path, line
):
    text = deleted_section("old.py") + section("app.py")

    run = review(write_diff(text), [finding(file="old.py", line=line)])

    assert run.exit_code == 0, run.stderr
    assert run.stdout == NO_FINDINGS_LOW
    assert logged_reasons(log_path) == ["unknown_file"]


@pytest.mark.parametrize("line", [1, 2, 3])
def test_p1_s6_ac6_finding_in_a_hunk_with_an_unreadable_header_is_line_not_in_diff(
    review, write_diff, log_path, line
):
    text = hunk_section("app.py", "@@ garbage @@", "+a\n+b\n c\n")
    assert all(entry.new_line is None for entry in parse_diff(text).files[0].hunks[0].lines)

    run = review(write_diff(text), [finding(file="app.py", line=line)])

    assert run.exit_code == 0, run.stderr
    assert run.stdout == NO_FINDINGS_LOW
    assert logged_reasons(log_path) == ["line_not_in_diff"]


@pytest.mark.parametrize("line", [1, 2, 3])
def test_p1_s6_ac6_finding_on_a_file_with_only_removed_lines_is_line_not_in_diff(
    review, write_diff, log_path, line
):
    text = hunk_section("app.py", "@@ -2,2 +1,0 @@", "-a\n-b\n")

    run = review(write_diff(text), [finding(file="app.py", line=line)])

    assert run.exit_code == 0, run.stderr
    assert run.stdout == NO_FINDINGS_LOW
    assert logged_reasons(log_path) == ["line_not_in_diff"]


# AC7: what's left prints in order; a warning only when every finding was dropped.


def test_p1_s6_ac7_one_valid_of_three_prints_alone_with_dropped_2_and_no_warning(review):
    findings = [
        finding(line=15, rationale="First."),
        finding(line=14, rationale="Second."),
        finding(file="other.py", line=14, rationale="Third."),
    ]

    run = review(ANCHORS, findings)

    assert run.exit_code == 1, run.stderr
    assert run.stdout == f"{INVOICE}:14 [high/logic] Second. (confidence 0.90)\n"
    assert summary(run.stderr)["dropped"] == "2"
    assert dropped_warnings(run.stderr) == []


def test_p1_s6_ac7_beyond_kept_findings_keep_the_order_proposed(review):
    """design.md Interfaces: AI findings are kept as proposed, not resorted by line."""
    findings = [
        finding(line=41, rationale="A."),
        finding(line=15, rationale="Dropped."),
        finding(line=12, rationale="B."),
        finding(file="other.py", rationale="Dropped."),
        finding(line=30, rationale="C."),
    ]

    run = review(ANCHORS, findings)

    assert run.exit_code == 1, run.stderr
    assert [line.split(" ")[0] for line in run.stdout.splitlines()] == [
        f"{INVOICE}:41",
        f"{INVOICE}:12",
        f"{INVOICE}:30",
    ]


def test_p1_s6_ac7_every_finding_dropped_exits_0_with_the_warning(review):
    findings = [finding(line=15), finding(file="other.py"), finding(line=1)]

    run = review(ANCHORS, findings)

    assert run.exit_code == 0, run.stderr
    assert run.stdout == NO_FINDINGS_LOW
    assert run.stderr.splitlines().count(all_dropped_warning(3)) == 1
    assert summary(run.stderr)["dropped"] == "3"


def test_p1_s6_ac7_one_finding_dropped_warns_in_the_singular(review):
    run = review(ANCHORS, [finding(line=15)])

    assert run.exit_code == 0, run.stderr
    assert dropped_warnings(run.stderr) == [
        "review-bot: warning: the 1 model finding was dropped: its file or line isn't in the diff."
    ]


def test_p1_s6_ac7_beyond_no_findings_at_all_gives_no_dropped_warning(review):
    run = review(ANCHORS, [])

    assert run.exit_code == 0, run.stderr
    assert dropped_warnings(run.stderr) == []
    assert summary(run.stderr)["dropped"] == "0"


# AC8: invalid findings are dropped before the threshold applies.


def test_p1_s6_ac8_invalid_critical_doesnt_count_toward_the_threshold(review):
    findings = [finding(line=12, severity="low"), finding(line=15, severity="critical")]

    run = review(ANCHORS, findings, "--threshold", "high")

    assert run.exit_code == 0, run.stderr
    assert run.stdout == NO_FINDINGS_HIGH
    assert summary(run.stderr)["dropped"] == "1"


def test_p1_s6_ac8_control_the_same_critical_on_a_diff_line_exits_1(review):
    findings = [finding(line=12, severity="low"), finding(line=14, severity="critical")]

    run = review(ANCHORS, findings, "--threshold", "high")

    assert run.exit_code == 1, run.stderr
    assert run.stdout.startswith(f"{INVOICE}:14 [critical/logic] ")


# AC9: a `verify` record per proposal and a `policy` record per kept finding.

KEPT_RATIONALE = "SENTINEL-KEPT: tax is applied twice."
DROPPED_RATIONALE = "SENTINEL-DROPPED: the line is off by one."
UNKNOWN_RATIONALE = "SENTINEL-UNKNOWN: names another file."


def three_proposals() -> list[Finding]:
    return [
        finding(line=14, severity="high", rationale=KEPT_RATIONALE),
        finding(line=15, severity="medium", rationale=DROPPED_RATIONALE),
        finding(file="other.py", line=37, severity="critical", rationale=UNKNOWN_RATIONALE),
    ]


def test_p1_s6_ac9_each_proposal_has_a_verify_record_in_order(review, log_path):
    review(ANCHORS, three_proposals())

    records = stage_records(log_path, "verify")
    assert len(records) == 3, records
    for data, proposed in zip(records, three_proposals(), strict=True):
        assert proposed.line in [value for value in scalars(data) if type(value) is int], data
        assert str(proposed.severity) in scalars(data), data
        assert holds_digest(data, sha256(proposed.rationale)), data


def test_p1_s6_ac9_verify_records_hold_the_reason_and_an_outcome(review, log_path):
    review(ANCHORS, three_proposals())

    kept, off_line, unknown = stage_records(log_path, "verify")
    assert [drop_reason(kept), drop_reason(off_line), drop_reason(unknown)] == [
        None,
        "line_not_in_diff",
        "unknown_file",
    ]
    assert [kept["outcome"], off_line["outcome"], unknown["outcome"]] == [
        "kept",
        "dropped",
        "dropped",
    ]


def test_p1_s6_ac9_no_rationale_text_reaches_the_log(review, log_path):
    review(ANCHORS, three_proposals())

    assert len(stage_records(log_path, "verify")) == 3
    text = log_text(log_path)
    for sentinel in ("SENTINEL-KEPT", "SENTINEL-DROPPED", "SENTINEL-UNKNOWN", "applied twice"):
        assert sentinel not in text


def test_p1_s6_ac9_policy_record_for_each_kept_finding_only(review, log_path):
    findings = [
        finding(line=12, severity="low", rationale="Below."),
        finding(line=15, severity="critical", rationale="Dropped."),
        finding(line=14, severity="high", rationale="Reported."),
    ]

    review(ANCHORS, findings, "--threshold", "high")

    records = stage_records(log_path, "policy")
    assert len(records) == 2, records
    lines = [{value for value in scalars(data) if type(value) is int} for data in records]
    assert all(15 not in found for found in lines), records
    assert sorted(line for found in lines for line in found & {12, 14}) == [12, 14]
    outcomes = {data["line"]: data["outcome"] for data in records}
    assert outcomes == {12: "below_threshold", 14: "reported"}, records


@pytest.mark.parametrize("file", [INVOICE, "./billing/invoice.py"])
def test_p1_s6_ac9_a_file_matching_a_diff_path_is_in_the_record(review, log_path, file):
    review(ANCHORS, [finding(file=file, line=14)])

    # The developer's choice, which the spec leaves open: a kept finding is logged by diff path.
    (data,) = stage_records(log_path, "verify")
    assert data["file"] == INVOICE, data


OTHER_FILES = {
    "sentinel": "SENTINEL-FILE.py",
    "ten_thousand_characters": "Q" * 9_997 + ".py",
    "escape": "billing/invoice.py\x1b[0m",
}


@pytest.mark.parametrize("file", OTHER_FILES.values(), ids=OTHER_FILES.keys())
def test_p1_s6_ac9_any_other_file_is_only_its_sha256_and_length(review, log_path, file):
    review(ANCHORS, [finding(file=file, line=14)])

    (data,) = stage_records(log_path, "verify")
    assert holds_digest(data, sha256(file)), data
    assert len(file) in [value for value in scalars(data) if type(value) is int], data
    text = log_text(log_path)
    assert "SENTINEL-FILE" not in text
    assert "Q" * 64 not in text
    assert "\x1b" not in text
    assert "\\u001b" not in text


# AC10: no invalid anchor survives, over generated diffs and findings.

PATHS = ["app.py", "src/app.py", "x.py", "b/x.py", "pkg/mod.py", "README.md"]
BODY_TEXT = st.sampled_from(
    ["x", "y = 1", "", "-- a/evil.py", "++ b/evil.py", "@ -1 +1 @@", "\\ No newline", "  z"]
)
HOSTILE_HEADERS = [
    "@@ garbage @@",
    "@@ -1,2 +x,1 @@",
    "@@ -1,2 +-5,1 @@",
    "@@ -1,2 +99999999999999999999,1 @@",
    "@@ -1 +٣ @@",
    "@@@ -1 -1 +1 @@@",
    "@@ -1,2 +3,4 @@@",
    "@@ +5 @@",
    "@@-1+1@@",
    "@@ -1,2 +3,4",
]


@st.composite
def hunk_header(draw: st.DrawFn) -> str:
    if draw(st.integers(min_value=0, max_value=2)) == 0:
        return draw(st.sampled_from(HOSTILE_HEADERS))
    start = draw(st.integers(min_value=0, max_value=40))
    old = draw(st.integers(min_value=0, max_value=9))
    new = draw(st.integers(min_value=0, max_value=9))
    return draw(st.sampled_from([f"@@ -1,{old} +{start},{new} @@", f"@@ -1 +{start} @@"]))


@st.composite
def unified_body(draw: st.DrawFn) -> str:
    lines = draw(
        st.lists(st.tuples(st.sampled_from(["+", "-", " ", ""]), BODY_TEXT), min_size=1, max_size=8)
    )
    return "".join(f"{marker}{text}\n" for marker, text in lines)


@st.composite
def combined_body(draw: st.DrawFn) -> str:
    markers = st.sampled_from(["++", "+ ", " +", "  ", "--", "- ", " -"])
    lines = draw(st.lists(st.tuples(markers, BODY_TEXT), min_size=1, max_size=6))
    return "".join(f"{marker}{text}\n" for marker, text in lines)


@st.composite
def file_section(draw: st.DrawFn, path: str) -> str:
    kind = draw(st.sampled_from(["unified", "unified", "combined", "deleted"]))
    if kind == "deleted":
        return deleted_section(path)
    if kind == "combined":
        start = draw(st.sampled_from(["diff --cc", "diff --combined"]))
        files = draw(st.sampled_from(["", f"--- a/{path}\n+++ b/{path}\n"]))
        start_line = draw(st.integers(min_value=1, max_value=20))
        header = draw(st.sampled_from([f"@@@ -1,2 -1,2 +{start_line},3 @@@", "@@@ garbage @@@"]))
        return f"{start} {path}\n{files}{header}\n{draw(combined_body())}"
    hunks = draw(st.lists(st.tuples(hunk_header(), unified_body()), min_size=1, max_size=3))
    body = "".join(f"{header}\n{lines}" for header, lines in hunks)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{body}"


@st.composite
def generated_diff(draw: st.DrawFn) -> str:
    paths = draw(st.lists(st.sampled_from(PATHS), min_size=1, max_size=4, unique=True))
    return "".join(draw(file_section(path)) for path in paths)


@st.composite
def proposals(draw: st.DrawFn, text: str) -> list[Finding]:
    paths = [
        line.split(" b/", 1)[-1] for line in text.splitlines() if line.startswith("diff --git")
    ]
    paths += [line.split(" ", 2)[-1] for line in text.splitlines() if line.startswith("diff --c")]
    variants = [*paths, *(f"{prefix}{path}" for path in paths for prefix in ("./", "a/", "b/"))]
    files = st.sampled_from([*variants, "other.py", "evil.py", "mock/example.py", "APP.PY"])
    lines = st.one_of(st.integers(min_value=1, max_value=45), st.just(10**9))
    pair = st.tuples(files, lines)
    numbered = sorted(numbered_lines(text))
    if numbered:
        # Real numbered lines, maybe with a prefix, and their neighbours: the near misses a snap
        # would take.
        prefixes = st.sampled_from(["", "", "./", "a/", "b/"])
        real = st.tuples(prefixes, st.sampled_from(numbered)).map(
            lambda item: (item[0] + item[1][0], max(1, item[1][1]))
        )
        near = st.tuples(st.sampled_from(numbered), st.sampled_from([-1, 1])).map(
            lambda item: (item[0][0], max(1, item[0][1] + item[1]))
        )
        pair = st.one_of(pair, real, real, near)
    pairs = draw(st.lists(pair, max_size=8))
    return [
        finding(file=file, line=line, rationale=f"R{index}.", confidence=0.5)
        for index, (file, line) in enumerate(pairs)
    ]


def numbered_lines(text: str) -> set[tuple[str, int]]:
    """Every path and new-version line number the parser gives, whatever its kind or skip."""
    try:
        files = parse_diff(text).files
    except DiffNotParsableError:
        return set()
    return {
        (file.path, line.new_line)
        for file in files
        if file.path is not None
        for hunk in file.hunks
        for line in hunk.lines
        if line.new_line is not None
    }


def valid_anchors(parsed: ParsedDiff) -> set[tuple[str, int]]:
    """design.md's valid anchors: an added or context line, with a number, of a reviewed path."""
    return {
        (file.path, line.new_line)
        for file in parsed.files
        if file.path is not None and file.skip_reason is None
        for hunk in file.hunks
        for line in hunk.lines
        if line.kind in (LineKind.ADDED, LineKind.CONTEXT) and line.new_line is not None
    }


def resolved(proposed: str, parsed: ParsedDiff) -> str | None:
    """design.md: the diff path a proposed `file` names, exact first, then one prefix stripped."""
    paths = {file.path for file in parsed.files if file.path is not None}
    if proposed in paths:
        return proposed
    for prefix in ("./", "a/", "b/"):
        if proposed.startswith(prefix) and proposed.removeprefix(prefix) in paths:
            return proposed.removeprefix(prefix)
    return None


PRINTED = re.compile(
    r"(?P<file>.+):(?P<line>\d+) \[high/logic\] R(?P<index>\d+)\. \(confidence 0\.50\)"
)


def test_p1_s6_ac10_property_every_printed_finding_is_on_an_added_or_context_line(bot, tmp_path):
    diff_path = tmp_path / "generated.diff"
    reached = Counter[str]()

    @seed(20261106)
    @settings(
        max_examples=300, deadline=None, database=None, suppress_health_check=[HealthCheck.too_slow]
    )
    @given(data=st.data())
    def check(data: st.DataObject) -> None:
        text = data.draw(generated_diff())
        findings = data.draw(proposals(text))
        diff_path.write_bytes(text.encode("utf-8"))
        try:
            parsed = parse_diff(text)
        except DiffNotParsableError:
            return
        anchors = valid_anchors(parsed)

        run = bot.review(diff_path, provider=bot.mock(findings=findings))

        assert run.exit_code in (0, 1), (text, run.stderr)
        printed = [] if run.stdout.startswith("No findings") else run.stdout.splitlines()
        for line in printed:
            match = PRINTED.fullmatch(line)
            assert match, (line, text)
            file, number = match["file"], int(match["line"])
            assert (file, number) in anchors, (line, text, sorted(anchors))
            assert number == findings[int(match["index"])].line, (line, "moved")
        # Counted from the oracle, not the output, so a wrong verify can't starve the guard.
        resolved_files = {resolved(item.file, parsed) for item in findings}
        combined_paths = {
            line.split(" ", 2)[-1] for line in text.splitlines() if line.startswith("diff --c")
        }
        reached["ran"] += 1
        reached["anchorable"] += any(
            (resolved(item.file, parsed), item.line) in anchors for item in findings
        )
        reached["invalid"] += any(
            (resolved(item.file, parsed), item.line) not in anchors for item in findings
        )
        reached["combined"] += bool(resolved_files & combined_paths)
        reached["hostile"] += any(header in text for header in HOSTILE_HEADERS) and bool(findings)

    check()

    # Guard against a vacuous property: enough examples must reach each kind of case.
    assert reached["ran"] >= 250, reached
    assert reached["anchorable"] >= 50, reached
    assert reached["invalid"] >= 100, reached
    assert reached["combined"] >= 30, reached
    assert reached["hostile"] >= 50, reached


def test_p1_s6_ac10_property_verify_findings_keeps_exactly_the_valid_anchors():
    """Beyond the CLI: through `verify_findings`, valid anchors are all kept, and only they are."""
    verify = importlib.import_module("review_bot.verify")
    reached = Counter[str]()

    @seed(20261106)
    @settings(
        max_examples=500, deadline=None, database=None, suppress_health_check=[HealthCheck.too_slow]
    )
    @given(data=st.data())
    def check(data: st.DataObject) -> None:
        text = data.draw(generated_diff())
        findings = data.draw(proposals(text))
        try:
            parsed = parse_diff(text)
        except DiffNotParsableError:
            return
        anchors = valid_anchors(parsed)
        expected = [
            (path, item.line, item.rationale)
            for item in findings
            if (path := resolved(item.file, parsed)) is not None and (path, item.line) in anchors
        ]

        result = verify.verify_findings(findings, (), parsed)

        kept = [
            (item.finding.file, item.finding.line, item.finding.rationale) for item in result.kept
        ]
        assert kept == expected, (text, findings)
        assert len(result.dropped) == len(findings) - len(expected)
        reached["anchorable"] += bool(expected)
        reached["invalid"] += len(expected) < len(findings)

    check()

    # Counted from the oracle, so a wrong verify can't starve the guard.
    assert reached["anchorable"] >= 80, reached
    assert reached["invalid"] >= 250, reached


# AC11: `--provider mock`'s canned finding is dropped too.


def test_p1_s6_ac11_canned_mock_finding_is_dropped_and_exits_0(bot, write_diff, log_path):
    run = bot.review(write_diff(MINIMAL_DIFF), AUDIT_FLAG, str(log_path))

    assert run.exit_code == 0, run.stderr
    assert run.stdout == NO_FINDINGS_LOW
    assert CANNED_NOTICE in run.stderr.splitlines()
    assert summary(run.stderr)["dropped"] == "1"
    assert logged_reasons(log_path) == ["unknown_file"]


def test_p1_s6_ac11_canned_finding_is_dropped_in_a_child_process(write_diff):
    path = write_diff(MINIMAL_DIFF)

    completed = subprocess.run(  # noqa: S603 - fixed argument list, our own interpreter
        [sys.executable, "-m", "review_bot", str(path), "--provider", "mock"],
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == NO_FINDINGS_LOW
    assert "mock/example.py" not in completed.stdout
    assert CANNED_NOTICE in completed.stderr.splitlines()
    assert summary(completed.stderr)["dropped"] == "1"


# AC12: a combined (`diff --cc`) hunk has no line numbers, so nothing anchors to it.


def combined_section(start: str, *, with_file_lines: bool) -> str:
    files = "index 1111111,2222222..3333333\n--- a/src/app.py\n+++ b/src/app.py\n"
    return (
        f"{start} src/app.py\n{files if with_file_lines else ''}"
        "@@@ -1,2 -1,2 +1,3 @@@\n++x\n +y\n  z\n"
    )


COMBINED = {
    f"{start.split()[1]}_{'with' if lines else 'no'}_file_lines": combined_section(
        start, with_file_lines=lines
    )
    for start in ("diff --cc", "diff --combined")
    for lines in (True, False)
}


@pytest.mark.parametrize("line", [1, 2, 3])
@pytest.mark.parametrize("combined", COMBINED.values(), ids=COMBINED.keys())
def test_p1_s6_ac12_finding_in_a_combined_hunk_is_line_not_in_diff(
    review, write_diff, log_path, combined, line
):
    text = combined + MINIMAL_DIFF
    assert "src/app.py" in [file.path for file in parse_diff(text).files]

    run = review(write_diff(text), [finding(file="src/app.py", line=line)])

    assert run.exit_code == 0, run.stderr
    assert run.stdout == NO_FINDINGS_LOW
    assert summary(run.stderr)["dropped"] == "1"
    assert logged_reasons(log_path) == ["line_not_in_diff"]


@pytest.mark.parametrize("combined_first", [True, False], ids=["combined_first", "combined_last"])
@pytest.mark.parametrize("combined", COMBINED.values(), ids=COMBINED.keys())
def test_p1_s6_ac12_added_line_of_the_normal_file_still_prints_and_none_moves(
    review, write_diff, log_path, combined, combined_first
):
    text = combined + MINIMAL_DIFF if combined_first else MINIMAL_DIFF + combined
    findings = [finding(file="src/app.py", line=line, rationale=f"C{line}.") for line in (1, 2, 3)]

    run = review(write_diff(text), [*findings, finding(file="app.py", line=1)])

    assert run.exit_code == 1, run.stderr
    assert run.stdout == "app.py:1 [high/logic] Rationale text. (confidence 0.90)\n"
    assert summary(run.stderr)["dropped"] == "3"
    assert logged_reasons(log_path) == ["line_not_in_diff"] * 3 + [None]


# Limits: hostile sizes finish in bounded time.


def test_p1_s6_limits_many_findings_over_a_large_diff_finish_in_bounded_time(review, write_diff):
    body = "".join(f"+value_{index} = {index}\n" for index in range(10_000))
    findings = [
        finding(file="app.py", line=line, rationale=f"F{line}.") for line in range(5, 20_001, 5)
    ]
    expected_kept = sum(1 for item in findings if item.line <= 10_000)

    started = time.monotonic()
    run = review(write_diff(section("app.py", body)), findings)
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, elapsed
    assert run.exit_code == 1, run.stderr
    assert len(run.stdout.splitlines()) == expected_kept
    assert summary(run.stderr)["dropped"] == str(len(findings) - expected_kept)
