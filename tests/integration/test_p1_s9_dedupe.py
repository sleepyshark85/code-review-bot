"""P1-S9: see one finding when a rule and the model report the same problem.

Written from the spec's criteria and design.md's "Fingerprints and dedup", Interfaces and Audit
log sections, before the developer's code existed. Runs go through `main` with an injected
`MockProvider(findings=...)` or `MockProvider(raw_output=...)`; `verify_findings` and
`build_request` are called directly where a criterion is about a finding's identity or the prompt.

Over `Assignment/sample_diff.txt`, the rule hits are `swallowed-exception` (medium) on line 35 and
`open-without-encoding` (low) on line 39. The second hunk's added and context lines are 30 to 41;
its first hunk holds line 14.

Expected fingerprints are computed here, by `expected_fingerprint`, from design.md's Encoding, not
from `fingerprint_for`. It is checked against P1-S7's golden file first. An AI finding's ordinal
continues from the rule findings' counts over all kept findings, in `kept` order (spec Notes).

The `verify.checked` keys used (`fingerprint`, `outcome`, `reason`, `duplicate_of`) and the
`duplicate` outcome are design.md's Audit log table.

`Finding` gains `rule` in this slice, so findings are built inside each test: a missing field
fails its own tests, not the whole module.
"""

import hashlib
import importlib
import json
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from diff_support import FINDING_DIFF, FINDING_LINE, MINIMAL_DIFF, SAMPLE_RULE_LINES, section
from repo_paths import REPO_ROOT
from test_p1_s4_audit_log import only, read_records

from review_bot.diff_parser import parse_diff
from review_bot.findings import Finding

pytestmark = pytest.mark.integration

SAMPLE = REPO_ROOT / "Assignment" / "sample_diff.txt"
FINGERPRINT_GOLDEN = REPO_ROOT / "tests" / "fixtures" / "p1_s7" / "fingerprints.golden.json"
INVOICE = "billing/invoice.py"
SWALLOWED = "swallowed-exception"
OPEN_RULE = "open-without-encoding"
AUDIT_FLAG = "--audit-log"
# The sample's anchored lines, as they read in the diff, without the marker.
ANCHORS = {
    14: "        return subtotal * (1 + self.tax_rate) if self.tax_rate else subtotal",
    30: "    def apply_discount(self, code):",
    31: "        discount = DISCOUNT_CODES.get(code)",
    32: "        try:",
    35: "        except Exception:",
    36: "            pass",
    38: "    def export_csv(self, path):",
    39: '        with open(path, "w") as f:',
}
SWALLOWED_HANDLER = "+try:\n+    x()\n+except Exception:\n+    pass\n"
# Generous bounds: they catch a hang or quadratic work, not a slow CI machine.
BOUND_S = 60.0


def expected_fingerprint(file: str, rule_part: str, anchor: str, ordinal: int) -> str:
    """design.md "Fingerprints and dedup", Encoding, written out independently of the code."""
    collapsed = " ".join(anchor.split())
    anchor_sha256 = hashlib.sha256(collapsed.encode("utf-8", "surrogatepass")).hexdigest()
    joined = json.dumps(["v1", file, rule_part, anchor_sha256, ordinal])
    return hashlib.sha256(joined.encode("ascii")).hexdigest()[:16]


def sample_fingerprint(line: int, rule_part: str, ordinal: int = 0) -> str:
    return expected_fingerprint(INVOICE, rule_part, ANCHORS[line], ordinal)


RULE_35 = sample_fingerprint(35, SWALLOWED)
RULE_39 = sample_fingerprint(39, OPEN_RULE)


def finding(**overrides: Any) -> Finding:
    """An AI finding on the sample, tagged `swallowed-exception` unless overridden."""
    fields = {
        "file": INVOICE,
        "line": 35,
        "severity": "medium",
        "category": "reliability",
        "rationale": "The exception is swallowed, so a failed discount passes silently.",
        "confidence": 0.8,
        "rule": SWALLOWED,
    }
    return Finding(**(fields | overrides))


def ai_line(item: Finding, file: str | None = None) -> str:
    return (
        f"{file or item.file}:{item.line} [{item.severity}/{item.category}] {item.rationale} "
        f"(confidence {item.confidence:.2f})"
    )


def rule_lines() -> str:
    return "".join(line + "\n" for line in SAMPLE_RULE_LINES)


def summary(stderr: str) -> dict[str, str]:
    """The summary line's `name=value` fields."""
    line = stderr.splitlines()[-1]
    return dict(field.split("=", 1) for field in line.split(" ") if "=" in field)


def sample_text() -> str:
    return SAMPLE.read_bytes().decode("utf-8")


def verify(proposed: Sequence[Finding], text: str) -> Any:
    rules = importlib.import_module("review_bot.rules")
    module = importlib.import_module("review_bot.verify")
    parsed = parse_diff(text)
    return module.verify_findings(proposed, rules.run_rules(parsed).findings, parsed)


def kept_fingerprints(result: Any, source: str) -> list[str]:
    return [item.fingerprint for item in result.kept if item.source == source]


def verify_records(log_path: Path) -> list[dict[str, Any]]:
    records = [record for record in read_records(log_path) if record["stage"] == "verify"]
    assert {record["event"] for record in records} <= {"checked"}, records
    return [record["data"] for record in records]


def static_rule_fingerprints(log_path: Path) -> set[str]:
    data = only(read_records(log_path), "static.rules")
    text = json.dumps(data)
    return {fingerprint for fingerprint in (RULE_35, RULE_39) if fingerprint in text}


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def review(bot: Any, log_path: Path) -> Callable[..., Any]:
    """Review `diff` with these scripted findings and the log at `log_path`."""

    def run(diff: Path, findings: Sequence[Finding], *flags: str) -> Any:
        provider = bot.mock(findings=list(findings))
        return bot.review(diff, AUDIT_FLAG, str(log_path), *flags, provider=provider)

    return run


@pytest.fixture
def review_text(review: Callable[..., Any], write_diff: Callable[..., Path]) -> Callable[..., Any]:
    def run(text: str, findings: Sequence[Finding], *flags: str) -> Any:
        return review(write_diff(text), findings, *flags)

    return run


@pytest.fixture
def review_raw(bot: Any, write_diff: Callable[..., Path], log_path: Path) -> Callable[..., Any]:
    """Review `text` with the model's raw output scripted."""

    def run(text: str, raw: str) -> Any:
        provider = bot.mock(raw_output=raw)
        return bot.review(write_diff(text), AUDIT_FLAG, str(log_path), provider=provider)

    return run


def assert_deduped_into_the_rule(run: Any, log_path: Path) -> None:
    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_lines()
    fields = summary(run.stderr)
    assert (fields["deduped"], fields["dropped"], fields["rule_findings"]) == ("1", "0", "2")
    (record,) = verify_records(log_path)
    assert record["outcome"] == "duplicate", record
    assert record["duplicate_of"] == RULE_35, record
    assert record["reason"] is None, record


def assert_both_print(run: Any, ai: Finding) -> None:
    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_lines() + ai_line(ai) + "\n"
    assert summary(run.stderr)["deduped"] == "0"


# The independent encoding agrees with P1-S7's pinned values, so it can stand in for the design.


def test_p1_s9_expected_fingerprint_matches_the_p1_s7_golden_file():
    cases = json.loads(FINGERPRINT_GOLDEN.read_text(encoding="utf-8"))
    assert len(cases) >= 4

    for case in cases:
        parts = (case["file"], case["rule"], case["anchor_text"], case["ordinal"])
        assert expected_fingerprint(*parts) == case["fingerprint"], case


def test_p1_s9_rule_findings_log_the_designs_fingerprints(review, log_path):
    review(SAMPLE, [])

    assert static_rule_fingerprints(log_path) == {RULE_35, RULE_39}


# AC1: an AI finding on the rule's line, same rule, same severity, is a duplicate of it.


def test_p1_s9_ac1_same_rule_line_and_severity_prints_only_the_rule(review, log_path):
    run = review(SAMPLE, [finding()])

    assert_deduped_into_the_rule(run, log_path)


def test_p1_s9_ac1_verify_findings_names_the_rule_findings_fingerprint():
    result = verify([finding()], sample_text())

    assert kept_fingerprints(result, "ai") == []
    assert kept_fingerprints(result, "rule") == [RULE_35, RULE_39]
    assert len(result.deduped) == 1
    assert RULE_35 in repr(result.deduped)


def test_p1_s9_ac1_design_rule_category_isnt_compared(review, log_path):
    """design.md: file, `rule`, hunk, distance and severity decide; the category doesn't."""
    run = review(SAMPLE, [finding(category="logic", rationale="Another wording.")])

    assert_deduped_into_the_rule(run, log_path)


def test_p1_s9_ac1_limits_prefixed_file_is_compared_as_the_diff_path(review, log_path):
    """design.md: the file is the diff path the finding matched, so `b/` doesn't escape dedup."""
    run = review(SAMPLE, [finding(file=f"b/{INVOICE}")])

    assert_deduped_into_the_rule(run, log_path)


def test_p1_s9_ac1_limits_the_open_rule_dedupes_too(review, log_path):
    run = review(SAMPLE, [finding(line=39, rule=OPEN_RULE, severity="low")])

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_lines()
    assert summary(run.stderr)["deduped"] == "1"
    (record,) = verify_records(log_path)
    assert (record["outcome"], record["duplicate_of"]) == ("duplicate", RULE_39), record


def test_p1_s9_ac1_limits_two_tagged_findings_both_dedupe_into_the_rule(review):
    first = finding()
    second = finding(line=36, severity="low", rationale="The handler only passes.")

    run = review(SAMPLE, [first, second])

    assert run.stdout == rule_lines()
    assert summary(run.stderr)["deduped"] == "2"


def test_p1_s9_ac1_limits_identical_tagged_findings_both_dedupe(review):
    run = review(SAMPLE, [finding(), finding()])

    assert run.stdout == rule_lines()
    assert summary(run.stderr)["deduped"] == "2"


# AC2: within 3 lines, in the same hunk, at a lower severity, is the same report.

NEARBY = {"line_32_3_above": 32, "line_36_1_below": 36}


@pytest.mark.parametrize("line", NEARBY.values(), ids=NEARBY.keys())
def test_p1_s9_ac2_nearby_line_at_low_severity_prints_only_the_rule(review, log_path, line):
    run = review(SAMPLE, [finding(line=line, severity="low")])

    assert_deduped_into_the_rule(run, log_path)


NEARBY_LIMITS = {"line_34_1_above": 34, "line_37_2_below": 37, "line_38_3_below": 38}


@pytest.mark.parametrize("line", NEARBY_LIMITS.values(), ids=NEARBY_LIMITS.keys())
def test_p1_s9_ac2_limits_other_lines_within_3_are_deduped(review, log_path, line):
    run = review(SAMPLE, [finding(line=line, severity="low")])

    assert_deduped_into_the_rule(run, log_path)


# AC3: 4 or more lines away, or in another hunk, isn't the same report.

FAR = {"line_31_4_away": 31, "line_30_5_away": 30, "line_14_another_hunk": 14}


@pytest.mark.parametrize("line", FAR.values(), ids=FAR.keys())
def test_p1_s9_ac3_far_or_other_hunk_line_prints_both(review, log_path, line):
    ai = finding(line=line)

    run = review(SAMPLE, [ai])

    assert_both_print(run, ai)
    (record,) = verify_records(log_path)
    assert record["outcome"] == "kept", record
    assert record["duplicate_of"] is None, record


# Two hunks of one file: lines 1 and 2, then 3 to 6, with the handler's `except` on line 5.
ADJACENT_HUNKS = (
    "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
    "@@ -1,1 +1,2 @@\n a = 1\n+b = 2\n"
    "@@ -2,0 +3,4 @@\n" + SWALLOWED_HANDLER
)


def app_finding(line: int, file: str = "app.py") -> Finding:
    return finding(file=file, line=line, rationale="Swallowed nearby.")


def test_p1_s9_ac3_limits_line_3_away_in_the_previous_hunk_prints_both(review_text):
    ai = app_finding(2)

    run = review_text(ADJACENT_HUNKS, [ai])

    assert run.exit_code == 1, run.stderr
    assert run.stdout.splitlines()[-1] == ai_line(ai)
    assert summary(run.stderr)["deduped"] == "0"


def test_p1_s9_ac3_limits_control_line_2_away_in_the_same_hunk_is_deduped(review_text):
    run = review_text(ADJACENT_HUNKS, [app_finding(3)])

    assert run.exit_code == 1, run.stderr
    assert len(run.stdout.splitlines()) == 1
    assert summary(run.stderr)["deduped"] == "1"


def test_p1_s9_ac3_limits_same_line_in_another_file_prints_both(review_text):
    text = section("a.py", SWALLOWED_HANDLER) + section("b.py", "+x\n" * 4)
    ai = app_finding(3, file="b.py")

    run = review_text(text, [ai])

    assert run.exit_code == 1, run.stderr
    assert run.stdout.splitlines()[-1] == ai_line(ai)
    assert summary(run.stderr)["deduped"] == "0"


# AC4: another problem on a rule's line prints beside it.

UNVALIDATED_PATH = {
    "file": INVOICE,
    "line": 39,
    "severity": "medium",
    "category": "security",
    "rationale": "The path isn't validated, so a caller can write anywhere.",
    "confidence": 0.7,
}
OTHER_PROBLEM_TAGS = {"no_rule": {}, "swallowed_exception": {"rule": SWALLOWED}}


@pytest.mark.parametrize("tag", OTHER_PROBLEM_TAGS.values(), ids=OTHER_PROBLEM_TAGS.keys())
def test_p1_s9_ac4_unvalidated_path_on_line_39_prints_beside_the_rule(review, tag):
    ai = Finding(**(UNVALIDATED_PATH | tag))

    run = review(SAMPLE, [ai])

    assert_both_print(run, ai)


# AC5: a more serious tagged finding isn't hidden by the rule.

MORE_SERIOUS = ["high", "critical"]


@pytest.mark.parametrize("severity", MORE_SERIOUS)
def test_p1_s9_ac5_more_serious_tagged_finding_prints_beside_the_rule(review, severity):
    ai = finding(severity=severity)

    run = review(SAMPLE, [ai])

    assert_both_print(run, ai)


@pytest.mark.parametrize("severity", MORE_SERIOUS)
def test_p1_s9_ac5_threshold_high_prints_only_the_ai_line(review, severity):
    ai = finding(severity=severity)

    run = review(SAMPLE, [ai], "--threshold", "high")

    assert run.exit_code == 1, run.stderr
    assert run.stdout == ai_line(ai) + "\n"
    assert summary(run.stderr)["deduped"] == "0"


def test_p1_s9_ac5_limits_medium_on_the_open_rules_low_line_prints_both(review):
    ai = finding(line=39, rule=OPEN_RULE, severity="medium", rationale="Default encoding.")

    assert_both_print(review(SAMPLE, [ai]), ai)


def test_p1_s9_ac5_notes_ai_ordinal_continues_from_the_rule_finding():
    """Spec Notes: the rule takes ordinal 0 of these parts, so the model's finding takes 1."""
    result = verify([finding(severity="high")], sample_text())

    assert kept_fingerprints(result, "rule") == [RULE_35, RULE_39]
    assert kept_fingerprints(result, "ai") == [sample_fingerprint(35, SWALLOWED, 1)]


def test_p1_s9_ac5_notes_logged_fingerprint_has_ordinal_1(review, log_path):
    review(SAMPLE, [finding(severity="high")])

    (record,) = verify_records(log_path)
    assert record["outcome"] == "kept", record
    assert record["fingerprint"] == sample_fingerprint(35, SWALLOWED, 1), record
    assert static_rule_fingerprints(log_path) == {RULE_35, RULE_39}


def test_p1_s9_ac5_notes_two_serious_findings_take_ordinals_1_and_2():
    proposed = [
        finding(severity="high", rationale="First."),
        finding(severity="critical", rationale="Second."),
    ]

    result = verify(proposed, sample_text())

    assert kept_fingerprints(result, "ai") == [
        sample_fingerprint(35, SWALLOWED, 1),
        sample_fingerprint(35, SWALLOWED, 2),
    ]


def test_p1_s9_ac5_notes_a_deduped_finding_takes_no_ordinal():
    """design.md: ordinals count kept findings only, so a duplicate before it doesn't count."""
    proposed = [finding(), finding(severity="high", rationale="Serious.")]

    result = verify(proposed, sample_text())

    assert len(result.deduped) == 1
    assert kept_fingerprints(result, "ai") == [sample_fingerprint(35, SWALLOWED, 1)]


def test_p1_s9_ac5_notes_ordinal_counts_every_rule_finding_with_the_same_line_text():
    """Two identical handlers take ordinals 0 and 1, so a tagged finding on either takes 2."""
    text = section("app.py", SWALLOWED_HANDLER + "+y = 1\n" + SWALLOWED_HANDLER)
    ai = finding(file="app.py", line=3, severity="high")

    result = verify([ai], text)

    parts = ("app.py", SWALLOWED, "except Exception:")
    assert kept_fingerprints(result, "rule") == [
        expected_fingerprint(*parts, 0),
        expected_fingerprint(*parts, 1),
    ]
    assert kept_fingerprints(result, "ai") == [expected_fingerprint(*parts, 2)]


def test_p1_s9_ac5_notes_every_kept_finding_has_its_own_fingerprint():
    proposed = [
        finding(line=line, severity=severity, rule=rule, rationale=f"{line} {severity} {rule}")
        for line in (35, 36, 39)
        for severity in ("low", "high")
        for rule in (SWALLOWED, OPEN_RULE, None)
    ]
    proposed += [finding(line=35, severity="critical", rationale="Repeat.")] * 2

    result = verify(proposed, sample_text())

    fingerprints = [item.fingerprint for item in result.kept]
    assert len(fingerprints) == len(set(fingerprints)), fingerprints


# AC6: an invalid anchor is dropped before dedup is considered.


def test_p1_s9_ac6_invalid_line_near_the_rule_is_dropped_not_deduped(review, log_path):
    run = review(SAMPLE, [finding(line=42, rule=OPEN_RULE, severity="low")])

    assert run.exit_code == 1, run.stderr
    assert run.stdout == rule_lines()
    fields = summary(run.stderr)
    assert (fields["dropped"], fields["deduped"]) == ("1", "0")
    (record,) = verify_records(log_path)
    assert record["outcome"] == "dropped", record
    assert record["reason"] == "line_not_in_diff", record
    assert record["duplicate_of"] is None, record


def test_p1_s9_ac6_limits_unknown_file_with_the_rules_line_is_dropped_not_deduped(review, log_path):
    run = review(SAMPLE, [finding(file="billing/invoices.py")])

    assert run.stdout == rule_lines()
    fields = summary(run.stderr)
    assert (fields["dropped"], fields["deduped"]) == ("1", "0")
    (record,) = verify_records(log_path)
    assert (record["reason"], record["duplicate_of"]) == ("unknown_file", None), record


# AC7: the model's `rule` must be a known rule ID, or absent, or null.

MODEL_FINDING = {
    "file": "src/app.py",
    "line": 7,
    "severity": "high",
    "category": "logic",
    "rationale": "Rationale text.",
    "confidence": 0.9,
}
UNKNOWN_RULES = {
    "made_up": "made-up",
    "secret": "secret",
}
UNKNOWN_RULES_LIMITS = {
    "empty": "",
    "upper_case": "Swallowed-Exception",
    "underscored": "swallowed_exception",
    "padded": " swallowed-exception",
    "ai_rule_part": "ai:logic",
    "integer": 1,
    "list": [SWALLOWED],
    "object": {"id": SWALLOWED},
}


def raw_with(**fields: Any) -> str:
    return json.dumps({"findings": [MODEL_FINDING | fields]})


@pytest.mark.parametrize("rule", UNKNOWN_RULES.values(), ids=UNKNOWN_RULES.keys())
def test_p1_s9_ac7_unknown_rule_exits_13_with_empty_stdout(review_raw, rule):
    run = review_raw(FINDING_DIFF, raw_with(rule=rule))

    assert run.exit_code == 13, run.stderr
    assert run.stdout == ""


@pytest.mark.parametrize("rule", UNKNOWN_RULES_LIMITS.values(), ids=UNKNOWN_RULES_LIMITS.keys())
def test_p1_s9_ac7_limits_other_invalid_rules_exit_13(review_raw, rule):
    run = review_raw(FINDING_DIFF, raw_with(rule=rule))

    assert run.exit_code == 13, run.stderr
    assert run.stdout == ""


def test_p1_s9_ac7_limits_unknown_rule_on_a_diff_with_rule_hits_prints_nothing(review_raw):
    run = review_raw(sample_text(), raw_with(rule="made-up"))

    assert run.exit_code == 13, run.stderr
    assert run.stdout == ""


def test_p1_s9_ac7_limits_one_bad_rule_among_valid_findings_exits_13(review_raw):
    valid = [MODEL_FINDING, MODEL_FINDING | {"rule": SWALLOWED, "line": 6}]
    raw = json.dumps({"findings": [*valid, MODEL_FINDING | {"rule": "made-up", "line": 5}]})

    run = review_raw(FINDING_DIFF, raw)

    assert run.exit_code == 13, run.stderr
    assert run.stdout == ""


NORMAL = {"null": {"rule": None}, "absent": {}}


@pytest.mark.parametrize("fields", NORMAL.values(), ids=NORMAL.keys())
def test_p1_s9_ac7_null_or_absent_rule_is_a_normal_ai_finding(review_raw, log_path, fields):
    run = review_raw(FINDING_DIFF, raw_with(**fields))

    assert run.exit_code == 1, run.stderr
    assert run.stdout == FINDING_LINE + "\n"
    (record,) = verify_records(log_path)
    assert record["fingerprint"] == expected_fingerprint("src/app.py", "ai:logic", "x", 0)


@pytest.mark.parametrize("rule", [SWALLOWED, OPEN_RULE])
def test_p1_s9_ac7_known_rule_id_is_accepted(review_raw, rule):
    """Replaces P1-S7 AC13's `rule` case: a known ID is now the model's tag, not a claim."""
    run = review_raw(FINDING_DIFF, raw_with(rule=rule))

    assert run.exit_code == 1, run.stderr
    assert run.stdout == FINDING_LINE + "\n"


# AC8: a tagged finding's fingerprint uses the rule ID; an untagged one, `ai:<category>`.

TAGGED = {"swallowed_exception": SWALLOWED, "open_without_encoding": OPEN_RULE}


@pytest.mark.parametrize("rule", TAGGED.values(), ids=TAGGED.keys())
def test_p1_s9_ac8_tagged_finding_with_no_rule_finding_uses_the_rule_id(review_raw, log_path, rule):
    run = review_raw(FINDING_DIFF, raw_with(rule=rule))

    assert run.exit_code == 1, run.stderr
    assert run.stdout == FINDING_LINE + "\n"
    (record,) = verify_records(log_path)
    assert record["fingerprint"] == expected_fingerprint("src/app.py", rule, "x", 0), record


def test_p1_s9_ac8_untagged_finding_uses_ai_and_its_category(review_raw, log_path):
    run = review_raw(FINDING_DIFF, raw_with(category="security"))

    assert run.exit_code == 1, run.stderr
    (record,) = verify_records(log_path)
    assert record["fingerprint"] == expected_fingerprint("src/app.py", "ai:security", "x", 0)


def test_p1_s9_ac8_tagged_finding_far_from_the_rule_uses_the_rule_id_with_ordinal_0():
    result = verify([finding(line=14)], sample_text())

    assert kept_fingerprints(result, "ai") == [sample_fingerprint(14, SWALLOWED)]


def test_p1_s9_ac8_untagged_finding_on_the_rules_line_uses_ai_and_ordinal_0():
    result = verify([finding(rule=None, severity="high")], sample_text())

    assert kept_fingerprints(result, "ai") == [sample_fingerprint(35, "ai:reliability")]


def test_p1_s9_ac8_limits_tagged_and_untagged_twins_both_print(review_text):
    """design.md AI vs AI: the rule parts differ, so they aren't duplicates."""
    twins = [Finding(**MODEL_FINDING), Finding(**MODEL_FINDING, rule=SWALLOWED)]

    run = review_text(FINDING_DIFF, twins)

    assert run.stdout == FINDING_LINE + "\n" + FINDING_LINE + "\n"
    assert summary(run.stderr)["deduped"] == "0"


def test_p1_s9_ac8_limits_identical_tagged_twins_print_once(review_text, log_path):
    twin = Finding(**MODEL_FINDING, rule=SWALLOWED)

    run = review_text(FINDING_DIFF, [twin, twin])

    assert run.stdout == FINDING_LINE + "\n"
    assert summary(run.stderr)["deduped"] == "1"
    kept, duplicate = verify_records(log_path)
    assert duplicate["duplicate_of"] == kept["fingerprint"]
    assert kept["fingerprint"] == expected_fingerprint("src/app.py", SWALLOWED, "x", 0)


# AC9: the system prompt explains `rule` and anchoring.


def sentences(text: str) -> list[str]:
    """The prompt split into lines, then sentences, lower-cased."""
    parts = re.split(r"\n|(?<=[.;!?])\s+", text.lower())
    return [part for part in parts if part.strip()]


@pytest.fixture
def system_prompt(bot: Any, write_diff: Callable[..., Path]) -> str:
    prompt: str = bot.request_for(write_diff(MINIMAL_DIFF)).system_prompt
    return prompt


def test_p1_s9_ac9_system_prompt_labels_rule(system_prompt):
    assert re.search(r"\brule\b\s*:", system_prompt)


@pytest.mark.parametrize("rule", [SWALLOWED, OPEN_RULE])
def test_p1_s9_ac9_system_prompt_names_each_rule_id(system_prompt, rule):
    assert rule in system_prompt


def test_p1_s9_ac9_system_prompt_says_when_to_omit_rule(system_prompt):
    """In the line that labels `rule`, which describes it."""
    omit = re.compile(r"\bomit|\bleave (it |this )?out\b|\bnull\b|\bdon'?t (set|include)\b")
    labelled = re.compile(r"\brule\b\s*:")

    described = [line for line in system_prompt.lower().splitlines() if labelled.search(line)]

    assert any(omit.search(line) for line in described), described


def test_p1_s9_ac9_system_prompt_says_line_is_an_added_or_context_line_in_a_hunk(system_prompt):
    """A requirement, so the sentence on how to count lines alone doesn't satisfy it."""
    obligation = re.compile(r"\bmust\b|\bonly\b|\bdiscard")

    assert any(
        all(word in part for word in ("added", "context", "hunk")) and obligation.search(part)
        for part in sentences(system_prompt)
    )


def test_p1_s9_ac9_system_prompt_says_hunk_header_counts_may_be_wrong(system_prompt):
    doubt = re.compile(r"wrong|incorrect|inaccurate|unreliable|may not|might not|can'?t be trusted")

    assert any(
        "header" in part and "count" in part and doubt.search(part)
        for part in sentences(system_prompt)
    )


def test_p1_s9_out_of_scope_rule_findings_dont_reach_the_prompt(bot, write_diff):
    """Out of scope: the system prompt is the same whether or not the diff has rule hits."""
    with_hits = bot.request_for(SAMPLE, exit_code=1)
    without = bot.request_for(write_diff(MINIMAL_DIFF))

    assert with_hits.system_prompt == without.system_prompt
    assert "except Exception" not in with_hits.system_prompt
    assert "confidence 1.00" not in with_hits.user_prompt


# AC10: the prompt version is `p1-v1` in the summary and in `run.start`.


def test_p1_s9_ac10_summary_and_run_start_carry_p1_v1(review, log_path):
    run = review(SAMPLE, [])

    assert run.exit_code == 1, run.stderr
    assert summary(run.stderr)["prompt"] == "p1-v1"
    assert "p1-v1" in json.dumps(only(read_records(log_path), "run.start"))


def test_p1_s9_ac10_no_findings_run_carries_p1_v1(review_text, log_path):
    run = review_text(MINIMAL_DIFF, [])

    assert run.exit_code == 0, run.stderr
    assert summary(run.stderr)["prompt"] == "p1-v1"
    assert "p1-v1" in json.dumps(only(read_records(log_path), "run.start"))


# AC11: the prompt's text and framing are pinned to its version.

# No rule hits it, so a review of it exits 0.
PINNED_DIFF = section("pinned.py", "+def f(path):\n+    return path.read_text()\n")
# If this test fails, the prompt or its framing changed. Bump `PROMPT_VERSION`, re-record the evals
# (P1-S14 to P1-S16) for the new version, then add the new digest here under the new version.
# Never update a version's digest in place.
PINNED_PROMPT_SHA256 = {
    "p1-v1": "46eab3939ee084440e86bab00640da3a931ac72851903a46c2f57d38feaac7f7",
}


def prompt_digest(system_prompt: str, user_prompt: str) -> str:
    """Both texts, as a JSON array, so text moved from one to the other changes the digest."""
    joined = json.dumps([system_prompt, user_prompt])
    return hashlib.sha256(joined.encode("ascii")).hexdigest()


def request_digest(request: Any) -> str:
    return prompt_digest(request.system_prompt, request.user_prompt)


def build_request(diff: str) -> Any:
    module = importlib.import_module("review_bot.prompt")
    return module.build_request(diff, model="claude-opus-5-5")


def test_p1_s9_ac11_prompt_digest_is_pinned_to_its_version():
    version = importlib.import_module("review_bot.prompt").PROMPT_VERSION

    assert version in PINNED_PROMPT_SHA256, f"no digest pinned for {version}: see the comment above"
    assert request_digest(build_request(PINNED_DIFF)) == PINNED_PROMPT_SHA256[version]


def test_p1_s9_ac11_pinned_request_is_what_the_provider_receives(bot, write_diff):
    received = bot.request_for(write_diff(PINNED_DIFF))

    assert request_digest(received) == request_digest(build_request(PINNED_DIFF))


def test_p1_s9_ac11_one_character_of_prompt_or_framing_changes_the_digest():
    request = build_request(PINNED_DIFF)
    system, user = request.system_prompt, request.user_prompt

    variants = [
        prompt_digest(system + " ", user),
        prompt_digest(system, user.replace("\n", "\n ", 1)),
        prompt_digest(system + user[:1], user[1:]),
    ]

    assert request_digest(request) not in variants


# Limits: many tagged findings beside many rule hits finish in bounded time.


def test_p1_s9_limits_thousands_of_tagged_findings_finish_in_bounded_time(review_text):
    count = 2_000
    text = section("app.py", SWALLOWED_HANDLER * count)
    duplicates = [
        finding(file="app.py", line=4 * index + 4, severity="low", rationale=f"Dup {index}.")
        for index in range(count)
    ]
    serious = [
        finding(file="app.py", line=4 * index + 3, severity="high", rationale=f"High {index}.")
        for index in range(count)
    ]

    started = time.monotonic()
    run = review_text(text, duplicates + serious)
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, elapsed
    assert run.exit_code == 1, run.stderr
    assert summary(run.stderr)["deduped"] == str(count)
    assert len(run.stdout.splitlines()) == 2 * count


def test_p1_s9_limits_thousands_of_shared_line_texts_keep_distinct_fingerprints():
    count = 2_000
    text = section("app.py", SWALLOWED_HANDLER * count)
    serious = [
        finding(file="app.py", line=4 * index + 3, severity="high", rationale=f"High {index}.")
        for index in range(count)
    ]

    started = time.monotonic()
    result = verify(serious, text)
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, elapsed
    parts = ("app.py", SWALLOWED, "except Exception:")
    assert kept_fingerprints(result, "ai")[-1] == expected_fingerprint(*parts, 2 * count - 1)
    fingerprints = [item.fingerprint for item in result.kept]
    assert len(set(fingerprints)) == 2 * count
