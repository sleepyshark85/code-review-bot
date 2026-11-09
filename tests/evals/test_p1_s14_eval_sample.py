"""P1-S14: score the sample diff per planted bug and per source, offline.

Written from the spec's criteria, ADR 0007 and design.md's "What the evals prove", before the
developer's code existed. Every run goes through the command's Python entry point in process,
`review_bot.evals.main(["replay", ...])`, so AC10's guard can see it; no subprocess is started.

Two names aren't in the spec, and each is used in one helper only:
- `review_bot.evals.main(argv) -> int`, the entry point, the tester's reading (`replay` fixture);
- the developer's `review_bot.evals.replay.freshness_fields(case_json) -> dict[str, str]`, today's
  `case_sha256`, `diff_sha256`, `prompt_version` and `prompt_sha256` for a case (`fresh_hashes`).

Recordings are written into a temporary copy of `tests/evals/cases/sample/`, with today's hashes
filled in by `fresh_hashes`, so a prompt change needs no hand edit. The committed synthetic
controls supply the findings. The recording's `schema` value and each run's `error` shape
(`{"category": ...}`) are readings too: the spec names the fields, not their values.

Report reading, from the developer's format: a bug's row is the first output line naming its ID,
as `<id>: ai, caught N/5, final N/5, needs 3, pass|fail, evidence=..., model=..., recorded=...`;
the lines indented under it, up to the next row, are its credits. Per-source recall is
`rules N/2` and `ai N/6`. Stdout and stderr are read together.
"""

import dataclasses
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from review_bot.findings import RuleId
from review_bot.settings import Settings

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES = REPO_ROOT / "tests" / "evals" / "cases"
SAMPLE = CASES / "sample"
SYNTHETIC = SAMPLE / "recordings" / "synthetic"
ASSIGNMENT_DIFF = REPO_ROOT / "Assignment" / "sample_diff.txt"

INVOICE = "billing/invoice.py"
RULE_BUGS = ("swallowed-exception", "open-without-encoding")
AI_BUGS = (
    "discount-dropped-on-export",
    "tax-discount-ordering",
    "double-tax",
    "float-money",
    "csv-path",
    "missing-tests",
)
ALL_BUGS = RULE_BUGS + AI_BUGS
HASH_FIELDS = ("case_sha256", "diff_sha256", "prompt_version", "prompt_sha256")
FIXED_LINE = (
    "Replay checks the pipeline against a past model response. "
    "It is not a new measurement of the model."
)
DEFAULT_MODEL = Settings.model_fields["model"].default
OTHER_MODEL = "claude-other-model-9"
RECORDED_AT = "2026-11-07T09:30:00Z"
RECORDED_DATE = "2026-11-07"
RUNS = 5
# Generous: it catches a hang or quadratic matching, not a slow CI machine.
BOUND_S = 60.0


# --- The command -----------------------------------------------------------------------------


def evals_module() -> Any:
    return importlib.import_module("review_bot.evals")


def fresh_hashes(case_json: Path) -> Mapping[str, str]:
    """Today's hashes for the case, from the developer's public function (a reading)."""
    hashes = importlib.import_module("review_bot.evals.replay").freshness_fields(case_json)
    return {field: hashes[field] for field in HASH_FIELDS}


@dataclasses.dataclass(frozen=True, slots=True)
class Replay:
    exit_code: int
    output: str

    def row(self, bug_id: str) -> str:
        """The first output line naming `bug_id` as a whole ID."""
        pattern = re.compile(rf"(?<![\w-]){re.escape(bug_id)}(?![\w-])")
        for line in self.output.splitlines():
            if pattern.search(line):
                return line
        raise AssertionError(f"no row for {bug_id!r} in:\n{self.output}")

    def recall(self, source: str) -> str:
        """Per-source recall, as `rules N/M` or `ai N/M`."""
        match = re.search(rf"(?<![\w-]){source} (\d+/\d+)", self.output)
        assert match, f"no {source} recall in:\n{self.output}"
        return match.group(1)


@pytest.fixture
def replay(capsys: pytest.CaptureFixture[str]) -> Callable[..., Replay]:
    def run(*args: str | Path) -> Replay:
        main = evals_module().main
        capsys.readouterr()
        try:
            exit_code = main(["replay", *map(str, args)])
        except SystemExit as exc:  # argparse reports usage errors this way
            exit_code = exc.code if isinstance(exc.code, int) else 2
        captured = capsys.readouterr()
        return Replay(exit_code, captured.out + captured.err)

    return run


# --- Cases and recordings ----------------------------------------------------------------------


@pytest.fixture
def cases(tmp_path: Path) -> Path:
    """A `--cases` directory holding a copy of the sample case, without recordings."""
    root = tmp_path / "cases"
    (root / "sample").mkdir(parents=True)
    for name in ("case.json", "sample_diff.txt"):
        shutil.copyfile(SAMPLE / name, root / "sample" / name)
    return root


def finding(line: int, rationale: str, **fields: Any) -> dict[str, Any]:
    """A finding as the model returns it; `fields` overrides any key, or adds `rule`."""
    defaults = {"file": INVOICE, "severity": "medium", "category": "logic", "confidence": 0.8}
    return defaults | {"line": line, "rationale": rationale} | fields


def run_of(*findings: dict[str, Any]) -> dict[str, Any]:
    return {
        "findings": list(findings),
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "warnings": [],
    }


def failed_run(category: str) -> dict[str, Any]:
    return {
        "error": {"category": category},
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "warnings": [],
    }


def every_run(*findings: dict[str, Any]) -> list[dict[str, Any]]:
    return [run_of(*findings) for _ in range(RUNS)]


def committed(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SYNTHETIC / f"{name}.json").read_text(encoding="utf-8"))
    return data


def recording(case_dir: Path, runs: Sequence[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": committed("all_ai_bugs")["schema"],
        "case_id": "sample",
        "kind": "synthetic",
        **fresh_hashes(case_dir / "case.json"),
        "model": DEFAULT_MODEL,
        "sdk_version": "synthetic",
        "recorded_at": RECORDED_AT,
        "runs": list(runs),
    }
    return data | overrides


def write_json(path: Path, data: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def write_synthetic(
    cases: Path, runs: Sequence[dict[str, Any]], name: str = "control", **overrides: Any
) -> Path:
    case_dir = cases / "sample"
    data = recording(case_dir, runs, **overrides)
    return write_json(case_dir / "recordings" / "synthetic" / f"{name}.json", data)


def write_live(cases: Path, runs: Sequence[dict[str, Any]], **overrides: Any) -> Path:
    case_dir = cases / "sample"
    data = recording(case_dir, runs, **({"kind": "live"} | overrides))
    return write_json(case_dir / "recordings" / "live" / "recording.json", data)


def write_control(cases: Path, name: str) -> Path:
    """A committed synthetic control, with today's hashes."""
    return write_synthetic(cases, committed(name)["runs"], name=name)


def patch_everywhere(monkeypatch: pytest.MonkeyPatch, original: object, replacement: object) -> int:
    """Replace every `review_bot` module's reference to `original`, as a test double."""
    evals_module()  # load the evals modules, so their references are patched too
    patched = 0
    for name, module in list(sys.modules.items()):
        if name != "review_bot" and not name.startswith("review_bot."):
            continue
        for attribute, value in list(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, attribute, replacement)
                patched += 1
    return patched


# --- An independent reading of the matcher, for the fixtures' self-check ------------------------


def phrase_matches(phrase: str, text: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def bug_matches(bug: Mapping[str, Any], entry: Mapping[str, Any]) -> bool:
    if entry["file"] != bug["file"]:
        return False
    lines = bug["lines"]
    if lines != "any" and not any(start <= entry["line"] <= end for start, end in lines):
        return False
    if "categories" in bug and entry["category"] not in bug["categories"]:
        return False
    return all(
        any(phrase_matches(phrase, entry["rationale"]) for phrase in group)
        for group in bug["keywords"]
    )


def credited(entry: Mapping[str, Any]) -> set[str]:
    case = json.loads((SAMPLE / "case.json").read_text(encoding="utf-8"))
    return {bug["id"] for bug in case["bugs"] if bug["source"] == "ai" and bug_matches(bug, entry)}


# --- The committed case ----------------------------------------------------------------------


def test_p1_s14_case_diff_is_the_assignments_sample_byte_for_byte() -> None:
    assert (SAMPLE / "sample_diff.txt").read_bytes() == ASSIGNMENT_DIFF.read_bytes()


def test_p1_s14_case_holds_the_specs_bug_table() -> None:
    case = json.loads((SAMPLE / "case.json").read_text(encoding="utf-8"))

    assert case["schema"] == "eval-case/v1"
    assert case["id"] == "sample"
    assert [bug["id"] for bug in case["bugs"]] == list(ALL_BUGS)
    assert {bug["id"]: bug["source"] for bug in case["bugs"]} == (
        dict.fromkeys(RULE_BUGS, "rule") | dict.fromkeys(AI_BUGS, "ai")
    )
    assert all(bug["file"] == INVOICE for bug in case["bugs"])
    assert all(bug.get("min_runs_caught") == 3 for bug in case["bugs"] if bug["source"] == "ai")


@pytest.mark.parametrize("name", sorted(path.stem for path in SYNTHETIC.glob("*.json")))
def test_p1_s14_committed_controls_are_synthetic_five_run_recordings(name: str) -> None:
    data = committed(name)

    assert data["kind"] == "synthetic"
    assert data["case_id"] == "sample"
    assert len(data["runs"]) == RUNS


def test_p1_s14_each_control_finding_credits_exactly_its_bug() -> None:
    """Self-check, with the reading above: the controls mean what the tests assume."""
    findings = committed("all_ai_bugs")["runs"][0]["findings"]

    assert [credited(entry) for entry in findings] == [{bug} for bug in AI_BUGS]


@pytest.mark.parametrize(
    ("name", "caught"),
    [
        ("all_ai_bugs", dict.fromkeys(AI_BUGS, 5)),
        ("float_money_only", dict.fromkeys(AI_BUGS, 0) | {"float-money": 5}),
        ("double_tax_2_of_5", dict.fromkeys(AI_BUGS, 5) | {"double-tax": 2}),
        ("double_tax_3_of_5", dict.fromkeys(AI_BUGS, 5) | {"double-tax": 3}),
        ("failed_runs", dict.fromkeys(AI_BUGS, 3)),
    ],
)
def test_p1_s14_controls_catch_what_their_names_say(name: str, caught: dict[str, int]) -> None:
    runs = committed(name)["runs"]
    counts = dict.fromkeys(AI_BUGS, 0)
    for run in runs:
        found = set().union(*(credited(entry) for entry in run.get("findings", [])))
        for bug in found:
            counts[bug] += 1

    assert counts == caught


# --- AC1: no recording -------------------------------------------------------------------------


def test_p1_s14_ac1_no_recording_catches_both_rule_bugs_and_exits_0(
    replay: Callable[..., Replay], cases: Path
) -> None:
    result = replay("--cases", cases)

    assert result.exit_code == 0, result.output
    assert result.recall("rules") == "2/2"


def test_p1_s14_ac1_no_recording_has_one_row_per_bug_ai_not_measured(
    replay: Callable[..., Replay], cases: Path
) -> None:
    result = replay("--cases", cases)

    for bug in AI_BUGS:
        assert "not measured" in result.row(bug)
    for bug in RULE_BUGS:
        assert "not measured" not in result.row(bug)


def test_p1_s14_ac1_committed_cases_without_their_live_recordings_exit_0(
    replay: Callable[..., Replay], tmp_path: Path
) -> None:
    """A copy of the repository's cases with only rules to score: synthetic files are ignored.

    P1-S17 commits live recordings, so the copy drops them; P1-S17's own tests replay them.
    """
    root = tmp_path / "committed"
    shutil.copytree(CASES, root)
    live = sorted(root.glob("*/recordings/live"))
    assert live, "the copy should hold P1-S17's live recordings before they are removed"
    for folder in live:
        shutil.rmtree(folder)

    result = replay("--cases", root)

    assert result.exit_code == 0, result.output
    for bug in AI_BUGS:
        assert "not measured" in result.row(bug)


def test_p1_s14_ac1_no_recording_with_require_live_exits_1(
    replay: Callable[..., Replay], cases: Path
) -> None:
    result = replay("--cases", cases, "--require-live")

    assert result.exit_code == 1, result.output


# --- AC2: every AI bug in every run -----------------------------------------------------------


def test_p1_s14_ac2_all_ai_bugs_each_show_5_of_5_and_exit_0(
    replay: Callable[..., Replay], cases: Path
) -> None:
    result = replay("--cases", cases, "--recording", write_control(cases, "all_ai_bugs"))

    assert result.exit_code == 0, result.output
    for bug in AI_BUGS:
        assert "caught 5/5" in result.row(bug)


def test_p1_s14_ac2_per_source_recall_is_rules_2_of_2_and_ai_6_of_6(
    replay: Callable[..., Replay], cases: Path
) -> None:
    result = replay("--cases", cases, "--recording", write_control(cases, "all_ai_bugs"))

    assert result.recall("rules") == "2/2"
    assert result.recall("ai") == "6/6"


def test_p1_s14_ac2_each_ai_row_says_evidence_synthetic(
    replay: Callable[..., Replay], cases: Path
) -> None:
    result = replay("--cases", cases, "--recording", write_control(cases, "all_ai_bugs"))

    for bug in AI_BUGS:
        assert "evidence=synthetic" in result.row(bug)


# --- AC3: thresholds ---------------------------------------------------------------------------


def test_p1_s14_ac3_float_money_only_gives_ai_1_of_6_and_exits_1(
    replay: Callable[..., Replay], cases: Path
) -> None:
    result = replay("--cases", cases, "--recording", write_control(cases, "float_money_only"))

    assert result.exit_code == 1, result.output
    assert result.recall("ai") == "1/6"
    assert "caught 5/5" in result.row("float-money")
    for bug in set(AI_BUGS) - {"float-money"}:
        assert "caught 0/5" in result.row(bug)


def test_p1_s14_ac3_double_tax_in_2_of_5_runs_exits_1(
    replay: Callable[..., Replay], cases: Path
) -> None:
    result = replay("--cases", cases, "--recording", write_control(cases, "double_tax_2_of_5"))

    assert result.exit_code == 1, result.output
    assert "caught 2/5" in result.row("double-tax")
    assert ", fail," in result.row("double-tax")
    assert result.recall("ai") == "5/6"


def test_p1_s14_ac3_double_tax_in_3_of_5_runs_passes(
    replay: Callable[..., Replay], cases: Path
) -> None:
    result = replay("--cases", cases, "--recording", write_control(cases, "double_tax_3_of_5"))

    assert result.exit_code == 0, result.output
    assert "caught 3/5" in result.row("double-tax")
    assert ", pass," in result.row("double-tax")
    assert result.recall("ai") == "6/6"


# --- AC4: the rules score can fall --------------------------------------------------------------


# The sample's two rules. P3-S10's `decimal-money` runs only where a case's config enables it, and
# the sample has none; its own disabled-rule check is in P3-S10's eval suite.
SAMPLE_RULES = (RuleId.SWALLOWED_EXCEPTION, RuleId.OPEN_WITHOUT_ENCODING)


@pytest.mark.parametrize("disabled", SAMPLE_RULES)
def test_p1_s14_ac4_rule_disabled_by_a_test_double_lowers_rules_and_exits_1(
    replay: Callable[..., Replay],
    cases: Path,
    monkeypatch: pytest.MonkeyPatch,
    disabled: RuleId,
) -> None:
    rules = importlib.import_module("review_bot.rules")
    real_run_rules = rules.run_rules

    def run_rules_without_one(parsed: Any, **options: Any) -> Any:
        result = real_run_rules(parsed, **options)  # P3-S10 added `domain_rules`
        kept = tuple(found for found in result.findings if found.rule != disabled)
        return dataclasses.replace(result, findings=kept)

    assert patch_everywhere(monkeypatch, real_run_rules, run_rules_without_one) >= 1

    result = replay("--cases", cases)

    assert result.exit_code == 1, result.output
    assert result.recall("rules") == "1/2"


def test_p1_s14_ac4_line_39_with_an_encoding_lowers_rules_and_exits_1(
    replay: Callable[..., Replay], cases: Path
) -> None:
    diff = cases / "sample" / "sample_diff.txt"
    text = diff.read_text(encoding="utf-8")
    fixed = text.replace('open(path, "w")', 'open(path, "w", encoding="utf-8")')
    assert fixed != text
    diff.write_text(fixed, encoding="utf-8", newline="\n")

    result = replay("--cases", cases)

    assert result.exit_code == 1, result.output
    assert result.recall("rules") == "1/2"


# --- AC5: through the real pipeline -------------------------------------------------------------


def test_p1_s14_ac5_matching_finding_on_line_42_doesnt_count(
    replay: Callable[..., Replay], cases: Path
) -> None:
    runs = every_run(finding(42, "Money is held as a float; use Decimal."))

    result = replay("--cases", cases, "--recording", write_synthetic(cases, runs))

    assert "caught 0/5" in result.row("float-money")
    assert result.recall("ai") == "0/6"


def test_p1_s14_ac5_any_line_bug_isnt_credited_by_an_invalid_anchor(
    replay: Callable[..., Replay], cases: Path
) -> None:
    """`missing-tests` takes any line, so only verify can stop a finding on line 42."""
    runs = every_run(finding(42, "Nothing covers export_csv.", category="testing"))

    result = replay("--cases", cases, "--recording", write_synthetic(cases, runs))

    assert "caught 0/5" in result.row("missing-tests")
    assert result.recall("ai") == "0/6"


def test_p1_s14_ac5_finding_on_a_file_not_in_the_diff_doesnt_count(
    replay: Callable[..., Replay], cases: Path
) -> None:
    other = finding(14, "Nothing covers total().", category="testing", file="billing/other.py")

    result = replay("--cases", cases, "--recording", write_synthetic(cases, every_run(other)))

    assert result.recall("ai") == "0/6"


def test_p1_s14_ac5_control_same_finding_on_a_valid_line_counts(
    replay: Callable[..., Replay], cases: Path
) -> None:
    runs = every_run(finding(14, "Nothing covers export_csv.", category="testing"))

    result = replay("--cases", cases, "--recording", write_synthetic(cases, runs))

    assert "caught 5/5" in result.row("missing-tests")
    assert result.recall("ai") == "1/6"


def test_p1_s14_ac5_ai_recall_is_before_dedup_and_final_after(
    replay: Callable[..., Replay], cases: Path
) -> None:
    """Tagged `open-without-encoding` on line 39 at the rule's `low`, the finding is deduped.

    P1-S9 AC5: a tag dedupes only at or below the rule's severity. It still meets `csv-path`, so
    AI recall credits it, while the `final` column can't.
    """
    deduped = finding(
        39,
        "Unvalidated path is opened without an encoding; validate the path first.",
        category="security",
        rule=RuleId.OPEN_WITHOUT_ENCODING.value,
        severity="low",
    )

    result = replay("--cases", cases, "--recording", write_synthetic(cases, every_run(deduped)))

    row = result.row("csv-path")
    assert "5/5" in row
    assert "caught 5/5" in row
    assert "final 0/5" in row, "the final column, after dedup, should show no run caught"


# --- AC6: freshness and the model ---------------------------------------------------------------


@pytest.mark.parametrize("field", HASH_FIELDS)
def test_p1_s14_ac6_stale_field_exits_1_naming_it_and_live_without_scoring(
    replay: Callable[..., Replay], cases: Path, field: str
) -> None:
    path = write_synthetic(cases, committed("all_ai_bugs")["runs"], **{field: "stale-0000"})

    result = replay("--cases", cases, "--recording", path)

    assert result.exit_code == 1, result.output
    assert field in result.output
    assert "evals live" in result.output
    assert "5/5" not in result.output
    assert "6/6" not in result.output


@pytest.mark.parametrize("field", HASH_FIELDS)
def test_p1_s14_ac6_stale_live_recording_exits_1_naming_the_field(
    replay: Callable[..., Replay], cases: Path, field: str
) -> None:
    write_live(cases, committed("all_ai_bugs")["runs"], **{field: "stale-0000"})

    result = replay("--cases", cases)

    assert result.exit_code == 1, result.output
    assert field in result.output
    assert "evals live" in result.output
    assert "6/6" not in result.output


def test_p1_s14_ac6_editing_the_case_after_recording_makes_case_sha256_stale(
    replay: Callable[..., Replay], cases: Path
) -> None:
    path = write_control(cases, "all_ai_bugs")
    case_json = cases / "sample" / "case.json"
    case = json.loads(case_json.read_text(encoding="utf-8"))
    case["bugs"][-1]["min_runs_caught"] = 2
    write_json(case_json, case)

    result = replay("--cases", cases, "--recording", path)

    assert result.exit_code == 1, result.output
    assert "case_sha256" in result.output


def test_p1_s14_ac6_editing_the_diff_after_recording_makes_diff_sha256_stale(
    replay: Callable[..., Replay], cases: Path
) -> None:
    path = write_control(cases, "all_ai_bugs")
    diff = cases / "sample" / "sample_diff.txt"
    diff.write_text(diff.read_text(encoding="utf-8") + "+    # end\n", encoding="utf-8")

    result = replay("--cases", cases, "--recording", path)

    assert result.exit_code == 1, result.output
    assert "diff_sha256" in result.output


def _system_prompt_changed(request: Any) -> Any:
    return dataclasses.replace(request, system_prompt=request.system_prompt + " ")


def _framing_changed(request: Any) -> Any:
    return dataclasses.replace(request, user_prompt="<<<FRAMED>>>\n" + request.user_prompt)


@pytest.mark.parametrize(
    "change", [_system_prompt_changed, _framing_changed], ids=["system_prompt", "framing"]
)
def test_p1_s14_ac6_changed_prompt_makes_prompt_sha256_stale(
    replay: Callable[..., Replay],
    cases: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[Any], Any],
) -> None:
    path = write_control(cases, "all_ai_bugs")
    prompt = importlib.import_module("review_bot.prompt")
    real_build_request = prompt.build_request

    def changed_build_request(diff: str, *, model: str) -> Any:
        return change(real_build_request(diff, model=model))

    assert patch_everywhere(monkeypatch, real_build_request, changed_build_request) >= 1

    result = replay("--cases", cases, "--recording", path)

    assert result.exit_code == 1, result.output
    assert "prompt_sha256" in result.output


def test_p1_s14_ac6_changed_skipping_makes_prompt_sha256_stale(
    replay: Callable[..., Replay], cases: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A docs file is skipped today; a double that stops skipping changes what the model sees."""
    diff = cases / "sample" / "sample_diff.txt"
    readme = (
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        "@@ -1,1 +1,2 @@\n # Billing\n+Invoices are exported as CSV.\n"
    )
    diff.write_text(diff.read_text(encoding="utf-8") + readme, encoding="utf-8", newline="\n")
    path = write_control(cases, "all_ai_bugs")
    parser = importlib.import_module("review_bot.diff_parser")
    real_parse_diff = parser.parse_diff

    def parse_without_skipping(text: str) -> Any:
        return dataclasses.replace(real_parse_diff(text), reviewable_text=text)

    assert patch_everywhere(monkeypatch, real_parse_diff, parse_without_skipping) >= 1

    result = replay("--cases", cases, "--recording", path)

    assert result.exit_code == 1, result.output
    assert "prompt_sha256" in result.output
    assert "diff_sha256" not in result.output


def test_p1_s14_ac6_recording_of_another_model_is_scored_and_shows_the_model(
    replay: Callable[..., Replay], cases: Path
) -> None:
    write_live(cases, committed("all_ai_bugs")["runs"], model=OTHER_MODEL)

    result = replay("--cases", cases)

    assert OTHER_MODEL in result.output
    assert result.recall("ai") == "6/6"
    assert result.exit_code == 0, result.output


def test_p1_s14_ac6_another_model_with_require_live_exits_1(
    replay: Callable[..., Replay], cases: Path
) -> None:
    write_live(cases, committed("all_ai_bugs")["runs"], model=OTHER_MODEL)

    result = replay("--cases", cases, "--require-live")

    assert result.exit_code == 1, result.output
    assert OTHER_MODEL in result.output


def test_p1_s14_ac6_control_default_model_live_recording_with_require_live_exits_0(
    replay: Callable[..., Replay], cases: Path
) -> None:
    write_live(cases, committed("all_ai_bugs")["runs"])

    result = replay("--cases", cases, "--require-live")

    assert result.exit_code == 0, result.output
    assert result.recall("ai") == "6/6"


def test_p1_s14_ac6_hashes_track_the_case_and_diff_bytes(cases: Path) -> None:
    before = fresh_hashes(cases / "sample" / "case.json")
    diff = cases / "sample" / "sample_diff.txt"
    diff.write_text(diff.read_text(encoding="utf-8") + "+    # end\n", encoding="utf-8")
    after = fresh_hashes(cases / "sample" / "case.json")

    assert before["case_sha256"] == after["case_sha256"]
    assert before["diff_sha256"] != after["diff_sha256"]
    assert before["prompt_sha256"] != after["prompt_sha256"]
    assert after["diff_sha256"] == hashlib.sha256(diff.read_bytes()).hexdigest()


# --- AC7: the fixed line, the date and the model --------------------------------------------------


def _no_recording(cases: Path) -> tuple[str, ...]:
    return ("--cases", str(cases))


def _synthetic(cases: Path) -> tuple[str, ...]:
    return ("--cases", str(cases), "--recording", str(write_control(cases, "all_ai_bugs")))


def _failing(cases: Path) -> tuple[str, ...]:
    return ("--cases", str(cases), "--recording", str(write_control(cases, "float_money_only")))


def _require_live(cases: Path) -> tuple[str, ...]:
    return ("--cases", str(cases), "--require-live")


def _stale(cases: Path) -> tuple[str, ...]:
    path = write_synthetic(cases, committed("all_ai_bugs")["runs"], prompt_sha256="stale")
    return ("--cases", str(cases), "--recording", str(path))


@pytest.mark.parametrize(
    "arguments",
    [_no_recording, _synthetic, _failing, _require_live, _stale],
    ids=["no_recording", "synthetic", "failing", "require_live", "stale"],
)
def test_p1_s14_ac7_every_run_prints_the_fixed_line(
    replay: Callable[..., Replay], cases: Path, arguments: Callable[[Path], tuple[str, ...]]
) -> None:
    result = replay(*arguments(cases))

    assert FIXED_LINE in result.output.splitlines()


def test_p1_s14_ac7_each_row_shows_the_recordings_date_and_model(
    replay: Callable[..., Replay], cases: Path
) -> None:
    write_live(cases, committed("all_ai_bugs")["runs"], model=OTHER_MODEL)

    result = replay("--cases", cases)

    for bug in AI_BUGS:
        assert RECORDED_DATE in result.row(bug)
        assert OTHER_MODEL in result.row(bug)


# --- AC8: failed runs ----------------------------------------------------------------------------


def test_p1_s14_ac8_failed_runs_catch_nothing(replay: Callable[..., Replay], cases: Path) -> None:
    result = replay("--cases", cases, "--recording", write_control(cases, "failed_runs"))

    for bug in AI_BUGS:
        assert "caught 3/5" in result.row(bug)


def test_p1_s14_ac8_failed_runs_are_listed_by_category(
    replay: Callable[..., Replay], cases: Path
) -> None:
    result = replay("--cases", cases, "--recording", write_control(cases, "failed_runs"))

    assert "run 1 (refusal)" in result.output
    assert "run 2 (invalid_output)" in result.output


def test_p1_s14_ac8_all_runs_failed_fails_every_ai_bug(
    replay: Callable[..., Replay], cases: Path
) -> None:
    runs = [failed_run("transient") for _ in range(RUNS)]

    result = replay("--cases", cases, "--recording", write_synthetic(cases, runs))

    assert result.exit_code == 1, result.output
    assert result.recall("ai") == "0/6"
    assert "transient" in result.output


# --- AC9: format and layout errors exit 2, naming the file ----------------------------------------


def _edit_case(cases: Path, edit: Callable[[dict[str, Any]], None]) -> Path:
    case_json = cases / "sample" / "case.json"
    case = json.loads(case_json.read_text(encoding="utf-8"))
    edit(case)
    return write_json(case_json, case)


@pytest.mark.parametrize(
    "edit",
    [
        lambda case: case.update(unexpected=True),
        lambda case: case.update(schema="eval-case/v2"),
        lambda case: case["bugs"][0].update(unexpected=True),
        lambda case: case["bugs"][2].update(lines="some"),
        lambda case: case.pop("bugs"),
        lambda case: case["bugs"][2].update(lines=[[41, 38]]),
        lambda case: case["bugs"][3].update(id="discount-dropped-on-export"),
        lambda case: case.update(id="not-its-directory"),
        lambda case: case.update(diff="../../outside.diff"),
        lambda case: case["bugs"][3].pop("min_runs_caught"),
        lambda case: case["bugs"][0].update(min_runs_caught=3),
    ],
    ids=[
        "unknown_field",
        "unknown_version",
        "unknown_bug_field",
        "bad_lines",
        "missing_bugs",
        "reversed_window",
        "duplicate_bug_id",
        "id_not_directory",
        "diff_outside_missing",
        "ai_bug_without_min_runs",
        "rule_bug_with_min_runs",
    ],
)
def test_p1_s14_ac9_invalid_case_exits_2_naming_the_file(
    replay: Callable[..., Replay], cases: Path, edit: Callable[[dict[str, Any]], None]
) -> None:
    _edit_case(cases, edit)

    result = replay("--cases", cases)

    assert result.exit_code == 2, result.output
    assert "case.json" in result.output


def test_p1_s14_ac9_case_that_isnt_json_exits_2_naming_the_file(
    replay: Callable[..., Replay], cases: Path
) -> None:
    (cases / "sample" / "case.json").write_text("{not json", encoding="utf-8")

    result = replay("--cases", cases)

    assert result.exit_code == 2, result.output
    assert "case.json" in result.output


def test_p1_s14_ac9_missing_diff_exits_2_naming_the_file(
    replay: Callable[..., Replay], cases: Path
) -> None:
    (cases / "sample" / "sample_diff.txt").unlink()

    result = replay("--cases", cases)

    assert result.exit_code == 2, result.output
    assert "sample_diff.txt" in result.output


def test_p1_s14_ac9_cases_directory_missing_exits_2_naming_it(
    replay: Callable[..., Replay], tmp_path: Path
) -> None:
    missing = tmp_path / "no-such-cases"

    result = replay("--cases", missing)

    assert result.exit_code == 2, result.output
    assert "no-such-cases" in result.output


def test_p1_s14_ac9_cases_flag_missing_exits_2(replay: Callable[..., Replay]) -> None:
    result = replay()

    assert result.exit_code == 2, result.output
    assert "--cases" in result.output


def test_p1_s14_ac9_synthetic_file_under_live_exits_2_naming_it(
    replay: Callable[..., Replay], cases: Path
) -> None:
    write_live(cases, committed("all_ai_bugs")["runs"], kind="synthetic")

    result = replay("--cases", cases)

    assert result.exit_code == 2, result.output
    assert "recording.json" in result.output


def test_p1_s14_ac9_live_file_under_synthetic_exits_2_naming_it(
    replay: Callable[..., Replay], cases: Path
) -> None:
    write_synthetic(cases, committed("all_ai_bugs")["runs"], name="misplaced", kind="live")

    result = replay("--cases", cases)

    assert result.exit_code == 2, result.output
    assert "misplaced.json" in result.output


def test_p1_s14_ac9_recording_flag_with_a_live_file_exits_2_naming_it(
    replay: Callable[..., Replay], cases: Path, tmp_path: Path
) -> None:
    data = recording(cases / "sample", committed("all_ai_bugs")["runs"], kind="live")
    path = write_json(tmp_path / "elsewhere" / "live_one.json", data)

    result = replay("--cases", cases, "--recording", path)

    assert result.exit_code == 2, result.output
    assert "live_one.json" in result.output


def test_p1_s14_ac9_recording_flag_with_require_live_exits_2(
    replay: Callable[..., Replay], cases: Path
) -> None:
    path = write_control(cases, "all_ai_bugs")

    result = replay("--cases", cases, "--recording", path, "--require-live")

    assert result.exit_code == 2, result.output
    assert "--require-live" in result.output


def test_p1_s14_ac9_recording_flag_with_a_missing_file_exits_2_naming_it(
    replay: Callable[..., Replay], cases: Path, tmp_path: Path
) -> None:
    result = replay("--cases", cases, "--recording", tmp_path / "absent_recording.json")

    assert result.exit_code == 2, result.output
    assert "absent_recording.json" in result.output


# --- AC10: offline and leaves nothing behind -----------------------------------------------


@pytest.fixture
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record and refuse any child process the run tries to start."""
    started: list[object] = []

    def refuse(*args: Any, **_kwargs: Any) -> Any:
        started.append(args)
        raise AssertionError(f"a subprocess was started: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr("asyncio.create_subprocess_exec", refuse)
    monkeypatch.setattr("asyncio.create_subprocess_shell", refuse)
    return started


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    """An empty working directory and an empty temporary directory, each checked afterwards."""
    work = tmp_path / "work"
    temp = tmp_path / "temp"
    work.mkdir()
    temp.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    yield work, temp


@pytest.mark.parametrize("with_recording", [False, True], ids=["rules_only", "recording"])
def test_p1_s14_ac10_whole_run_is_offline_and_leaves_nothing_behind(  # noqa: PLR0913, PLR0917 - fixtures
    replay: Callable[..., Replay],
    cases: Path,
    isolated: tuple[Path, Path],
    no_subprocess: list[object],
    real_query_calls: list[dict[str, Any]],
    with_recording: bool,
) -> None:
    work, temp = isolated
    arguments: list[str | Path] = ["--cases", cases]
    if with_recording:
        arguments += ["--recording", write_control(cases, "all_ai_bugs")]
    audit_variable = Path(os.environ["REVIEW_BOT_AUDIT_LOG"])
    audit_before = audit_variable.read_bytes() if audit_variable.exists() else None
    cases_before = _tree(cases)
    committed_before = _tree(CASES)

    result = replay(*arguments)

    assert result.exit_code == 0, result.output
    assert real_query_calls == []
    assert no_subprocess == []
    assert list(work.iterdir()) == [], "nothing may be written in the working directory"
    assert list(temp.iterdir()) == [], "the temporary audit path must be discarded"
    assert _tree(cases) == cases_before
    assert _tree(CASES) == committed_before
    after = audit_variable.read_bytes() if audit_variable.exists() else None
    assert after == audit_before, "replay must not append to REVIEW_BOT_AUDIT_LOG"


# --- AC11: word boundaries ------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["reorder", "disorder"])
def test_p1_s14_ac11_order_doesnt_match_inside_a_word(
    replay: Callable[..., Replay], cases: Path, word: str
) -> None:
    rationale = f"The tax and the discount need a {word}."
    assert credited(finding(14, rationale)) == set()

    result = replay(
        "--cases", cases, "--recording", write_synthetic(cases, every_run(finding(14, rationale)))
    )

    assert "caught 0/5" in result.row("tax-discount-ordering")
    assert result.recall("ai") == "0/6"


def test_p1_s14_ac11_control_order_as_a_word_matches(
    replay: Callable[..., Replay], cases: Path
) -> None:
    rationale = "The tax and the discount are applied in the wrong order."

    result = replay(
        "--cases", cases, "--recording", write_synthetic(cases, every_run(finding(14, rationale)))
    )

    assert "caught 5/5" in result.row("tax-discount-ordering")


@pytest.mark.parametrize(
    "rationale",
    [
        "export_csv ignores the discount.",
        "export_total ignores the discount.",  # `export` before `_`
        "write_csv ignores the discount.",  # `csv` after `_`
        "The discount is lost in EXPORT.",  # case-insensitive
    ],
)
def test_p1_s14_ac11_underscore_is_a_boundary(
    replay: Callable[..., Replay], cases: Path, rationale: str
) -> None:
    result = replay(
        "--cases", cases, "--recording", write_synthetic(cases, every_run(finding(40, rationale)))
    )

    assert "caught 5/5" in result.row("discount-dropped-on-export")


@pytest.mark.parametrize(
    ("line", "rationale", "bug"),
    [
        (40, "csvfile ignores the discount.", "discount-dropped-on-export"),
        (40, "The exporter ignores the discount.", "discount-dropped-on-export"),
        (40, "The export ignores discounted prices.", "discount-dropped-on-export"),
        (14, "Taxonomy and discount are in the wrong order.", "tax-discount-ordering"),
        (34, "Floatation of the amount.", "float-money"),
        (14, "Tax is applied; callersX may be wrong.", "double-tax"),
    ],
)
def test_p1_s14_ac11_phrase_inside_a_longer_word_doesnt_match(
    replay: Callable[..., Replay], cases: Path, line: int, rationale: str, bug: str
) -> None:
    result = replay(
        "--cases", cases, "--recording", write_synthetic(cases, every_run(finding(line, rationale)))
    )

    assert "caught 0/5" in result.row(bug)


# --- AC12: no cross-credit ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "rationale", "category", "expected"),
    [
        (34, "Float arithmetic on the discount", "logic", "float-money"),
        (40, "CSV export drops the discount", "logic", "discount-dropped-on-export"),
        (14, "Tax is applied before the discount", "logic", "tax-discount-ordering"),
        (39, "open() uses the platform encoding for this path", "reliability", None),
        (14, "No test covers the new tax branch", "logic", None),
        (14, "This branch has no coverage", "testing", "missing-tests"),
    ],
)
def test_p1_s14_ac12_finding_credits_only_its_own_bug(  # noqa: PLR0913, PLR0917 - parametrized
    replay: Callable[..., Replay],
    cases: Path,
    line: int,
    rationale: str,
    category: str,
    expected: str | None,
) -> None:
    entry = finding(line, rationale, category=category)
    assert credited(entry) == ({expected} if expected else set())

    result = replay("--cases", cases, "--recording", write_synthetic(cases, every_run(entry)))

    for bug in AI_BUGS:
        count = "caught 5/5" if bug == expected else "caught 0/5"
        assert count in result.row(bug)
    assert result.recall("ai") == ("1/6" if expected else "0/6")


def test_p1_s14_ac12_finding_meeting_two_matchers_credits_both(
    replay: Callable[..., Replay], cases: Path
) -> None:
    entry = finding(14, "Float money is taxed twice when callers add tax_rate.")
    assert credited(entry) == {"float-money", "double-tax"}

    result = replay("--cases", cases, "--recording", write_synthetic(cases, every_run(entry)))

    assert "caught 5/5" in result.row("float-money")
    assert "caught 5/5" in result.row("double-tax")
    assert result.recall("ai") == "2/6"


def bug_block(result: Replay, bug_id: str) -> str:
    """The lines after `bug_id`'s row, up to the next bug's row or the recall line."""
    lines = result.output.splitlines()
    start = lines.index(result.row(bug_id)) + 1
    rows = {result.row(other) for other in ALL_BUGS}
    end = next(
        (n for n in range(start, len(lines)) if lines[n] in rows or "recall:" in lines[n]),
        len(lines),
    )
    return "\n".join(lines[start:end])


# --- AC13: rationales for a person to confirm ----------------------------------------------


def test_p1_s14_ac13_each_matching_rationale_is_listed_under_its_bug(
    replay: Callable[..., Replay], cases: Path
) -> None:
    result = replay("--cases", cases, "--recording", write_control(cases, "all_ai_bugs"))

    for run in committed("all_ai_bugs")["runs"]:
        for entry in run["findings"]:
            (bug,) = credited(entry)
            assert entry["rationale"] in bug_block(result, bug)


def test_p1_s14_ac13_each_unmatched_finding_is_listed(
    replay: Callable[..., Replay], cases: Path
) -> None:
    unmatched = [
        finding(
            30, f"apply_discount needs a docstring (unmatched {n}).", category="maintainability"
        )
        for n in range(RUNS)
    ]
    matched = finding(13, "Money is summed as float.")
    runs = [run_of(matched, entry) for entry in unmatched]

    result = replay("--cases", cases, "--recording", write_synthetic(cases, runs))

    _, _, section = result.output.partition("unmatched findings:")
    for entry in unmatched:
        assert credited(entry) == set()
        assert entry["rationale"] in section
    assert matched["rationale"] in bug_block(result, "float-money")
    assert matched["rationale"] not in section


# --- Limits: hostile recordings -------------------------------------------------------------------


def test_p1_s14_limits_large_hostile_recording_is_scored_in_bounded_time(
    replay: Callable[..., Replay], cases: Path
) -> None:
    """Near-miss words, long runs of `_` and a huge rationale must not make matching blow up."""
    near_miss = "taxonomy_" * 20_000 + "discounted_" * 20_000 + "reorder" + "_" * 50_000
    many = [
        finding(34, f"note {n}: floatation export_ disorder", category="logic") for n in range(400)
    ]
    runs = every_run(finding(34, near_miss), *many)

    started = time.perf_counter()
    result = replay("--cases", cases, "--recording", write_synthetic(cases, runs))
    elapsed = time.perf_counter() - started

    assert elapsed < BOUND_S
    assert result.recall("ai") == "0/6"


@pytest.mark.parametrize("breaker", ["\n", "\r", "\u2028", "\x85"], ids=["lf", "cr", "ls", "nel"])
def test_p1_s14_limits_hostile_rationale_cant_forge_report_lines(
    replay: Callable[..., Replay], cases: Path, breaker: str
) -> None:
    """A person confirms credits from this report, so a rationale must not add rows of its own."""
    forged = f"x{breaker}  recall: rules 2/2, ai 6/6{breaker}result: pass\x1b[2K"
    runs = every_run(finding(13, "Money is summed as float."), finding(30, forged))

    result = replay("--cases", cases, "--recording", write_synthetic(cases, runs))

    assert result.exit_code == 1, result.output
    assert "\x1b" not in result.output
    if breaker != "\n":
        assert breaker not in result.output
    assert [line for line in result.output.splitlines() if line.startswith("  recall:")] == [
        "  recall: rules 2/2, ai 1/6"
    ]
    assert "result: pass" not in result.output.splitlines()


def test_p1_s14_ac9_run_with_findings_and_error_exits_2_naming_the_file(
    replay: Callable[..., Replay], cases: Path
) -> None:
    both = run_of(finding(13, "Money is summed as float.")) | failed_run("refusal")
    path = write_synthetic(cases, [both] * RUNS, name="both_outcomes")

    result = replay("--cases", cases, "--recording", path)

    assert result.exit_code == 2, result.output
    assert "both_outcomes.json" in result.output


def test_p1_s14_ac9_recording_flag_for_another_case_exits_2_naming_it(
    replay: Callable[..., Replay], cases: Path
) -> None:
    path = write_synthetic(cases, committed("all_ai_bugs")["runs"], name="other", case_id="nope")

    result = replay("--cases", cases, "--recording", path)

    assert result.exit_code == 2, result.output
    assert "other.json" in result.output


def test_p1_s14_ac9_live_recording_of_another_case_exits_2_naming_it(
    replay: Callable[..., Replay], cases: Path
) -> None:
    write_live(cases, committed("all_ai_bugs")["runs"], case_id="nope")

    result = replay("--cases", cases)

    assert result.exit_code == 2, result.output
    assert "recording.json" in result.output


def test_p1_s14_ac9_cases_directory_with_no_case_exits_2_naming_it(
    replay: Callable[..., Replay], tmp_path: Path
) -> None:
    empty = tmp_path / "empty-cases"
    empty.mkdir()

    result = replay("--cases", empty)

    assert result.exit_code == 2, result.output
    assert "empty-cases" in result.output


@pytest.mark.parametrize(
    "content",
    [b"just some text\n", b"\xff\xfe not utf-8\n"],
    ids=["not_a_diff", "not_utf8"],
)
@pytest.mark.parametrize("with_recording", [False, True], ids=["rules_only", "recording"])
def test_p1_s14_ac9_unreviewable_diff_exits_2_naming_the_case(
    replay: Callable[..., Replay], cases: Path, content: bytes, with_recording: bool
) -> None:
    arguments: list[str | Path] = ["--cases", cases]
    if with_recording:
        arguments += ["--recording", write_control(cases, "all_ai_bugs")]
    (cases / "sample" / "sample_diff.txt").write_bytes(content)

    result = replay(*arguments)

    assert result.exit_code == 2, result.output
    assert "case.json" in result.output
