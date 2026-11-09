"""P3-S7: compare diff mode and context mode on the same eval cases, offline.

Written from the spec's criteria, Limits and Interfaces, design.md's Q3 and "How the gate is
evidenced", ADR 0016, and P3-S5's recording format, in parallel with the developer and before
their code was read. Every run goes through `review_bot.evals.main(["compare", ...])` in process,
into a temporary copy of the committed cases without their recordings, with live recordings
written for the test with today's freshness fields (`freshness_fields`, P3-S5).

Readings (names the spec doesn't give), each used in one helper below:
- `eval-compare/v1` (Interfaces): `cases[*]` has `id`, `paired`, `reason`, and `modes.diff` /
  `modes.context`; each mode's per-bug entries hold `caught_runs`, `runs` and `met`. Where those
  entries sit inside a mode, and the names of the per-mode pass flag, the totals and the verdict,
  are read in `bug_entry`, `mode_passed`, `total` and `verdict_*` only.
- A ratio in the JSON is read by `as_fraction`, so `[a, b]`, `{"...": a, "...": b}`, `"a/b"` and a
  number all work; a dollar amount by `as_decimal`.
- Text: a bug's row is the first line naming its ID after its case's first line; the spec's own
  `diff N/M` and `context N/M` must be on it.
"""

import dataclasses
import json
import re
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from review_bot.evals import freshness_fields, main
from review_bot.providers import claude_agent
from review_bot.settings import Settings

pytestmark = pytest.mark.integration

CASES = Path(__file__).resolve().parent / "cases"
SAMPLE = "sample-context"
CLEAN = "clean-context"
TOOL_RESULT = "injection-tool-result"
INSTRUCTION = "injection-instruction-context"
INVOICE = "billing/invoice.py"
REFUNDS = "billing/refunds.py"
DEFAULT_MODEL = Settings.model_fields["model"].default
MODES = ("diff", "context")
# Generous: it catches quadratic work or a hang, not a slow CI machine.
BOUND_S = 120.0


# --- Findings that credit exactly one sample bug each (checked by `credits_bug`) ----------------


def finding(file: str, line: int, rationale: str, **fields: Any) -> dict[str, Any]:
    defaults = {"severity": "medium", "category": "logic", "confidence": 0.8}
    return defaults | {"file": file, "line": line, "rationale": rationale} | fields


SAMPLE_FINDINGS: dict[str, dict[str, Any]] = {
    "discount-dropped-on-export": finding(
        INVOICE,
        40,
        "The CSV export writes total() and ignores total_override, so an applied discount is "
        "lost from the exported invoice.",
    ),
    "tax-discount-ordering": finding(
        INVOICE,
        34,
        "The discount is applied to the tax-inclusive total; decide whether tax is computed "
        "before or after the discount.",
    ),
    "double-tax": finding(
        INVOICE,
        14,
        "total() now adds tax, but its callers already add tax, so a checkout is taxed twice.",
        severity="high",
    ),
    "float-money": finding(INVOICE, 13, "Money is summed as float; use Decimal for amounts."),
    "csv-path": finding(
        INVOICE,
        41,
        "The path is written without validation; validate the path, or reject arbitrary path "
        "values.",
        category="security",
    ),
    "missing-tests": finding(
        INVOICE, 38, "export_csv has no tests; add one covering its content.", category="testing"
    ),
}
SAMPLE_AI_BUGS = tuple(SAMPLE_FINDINGS)
NOISE = finding(INVOICE, 36, "Prefer a clearer name here.", category="maintainability")
NOISE_2 = finding(INVOICE, 33, "Consider a guard clause here.", category="maintainability")
CLEAN_NOISE = finding(REFUNDS, 3, "Consider sorting the imports.", severity="low")
CLEAN_NOISE_2 = finding(REFUNDS, 4, "Consider a module alias.", severity="low")
PAYOUT_BUG = finding(
    "billing/payouts.py",
    11,
    "payout adds the platform fee to the gross amount; the fee should be subtracted.",
    severity="high",
)
INVALID_ANCHOR = finding(INVOICE, 400, "A float total is summed past the end of the file.")


def all_sample_bugs() -> list[dict[str, Any]]:
    return list(SAMPLE_FINDINGS.values())


# --- An independent matcher, for the self-check -------------------------------------------------


def case_json(case_id: str, root: Path = CASES) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((root / case_id / "case.json").read_text(encoding="utf-8"))
    return data


def phrase_matches(phrase: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])", text, re.I) is not None


def credits_bug(bug: Mapping[str, Any], entry: Mapping[str, Any]) -> bool:
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


def credited(case_id: str, entry: Mapping[str, Any]) -> set[str]:
    return {
        bug["id"]
        for bug in case_json(case_id)["bugs"]
        if bug["source"] == "ai" and credits_bug(bug, entry)
    }


# --- Recordings ---------------------------------------------------------------------------------


def run_of(
    *findings: Mapping[str, Any],
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost: str | None = None,
    calls: int = 0,
) -> dict[str, Any]:
    usage: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if cost is not None:
        usage["cost_usd"] = cost
    run: dict[str, Any] = {"findings": list(findings), "usage": usage, "warnings": []}
    if calls:
        run["tool_calls"] = [read_call()] * calls
    return run


def failed_run(
    category: str,
    *,
    cap: str | None = None,
    cost: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> dict[str, Any]:
    error: dict[str, Any] = {"category": category}
    if cap is not None:
        error["cap"] = cap
    usage: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if cost is not None:
        usage["cost_usd"] = cost
    return {"error": error, "usage": usage, "warnings": []}


def read_call() -> dict[str, Any]:
    return {
        "tool": "read",
        "outcome": "ok",
        "reason": None,
        "spans": [{"path": "billing/checkout.py", "first_line": 1, "last_line": 5}],
        "listed": [],
        "result_bytes": 100,
    }


def copy_cases(tmp_path: Path, *case_ids: str) -> Path:
    """A `--cases` directory holding copies of `case_ids` without recordings, and their diffs."""
    root = tmp_path / "cases"
    root.mkdir(parents=True, exist_ok=True)
    for case_id in case_ids:
        shutil.copytree(
            CASES / case_id, root / case_id, ignore=shutil.ignore_patterns("recordings")
        )
        diff = str(case_json(case_id)["diff"])
        target = (root / case_id / diff).resolve()
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile((CASES / case_id / diff).resolve(), target)
    return root


def write_live(
    root: Path, case_id: str, mode: str, runs: Sequence[Mapping[str, Any]], **overrides: Any
) -> Path:
    case_file = root / case_id / "case.json"
    fresh = dict(freshness_fields(case_file, mode="context" if mode == "context" else "diff"))
    if mode == "diff":
        runs = [{key: value for key, value in run.items() if key != "tool_calls"} for run in runs]
    data: dict[str, Any] = {
        "schema": "eval-recording/v1",
        "case_id": case_id,
        "kind": "live",
        "mode": mode,
        **fresh,
        "model": DEFAULT_MODEL,
        "sdk_version": "0.0-test",
        "recorded_at": "2026-11-09T09:30:00Z",
        "runs": list(runs),
    } | overrides
    folder = "live" if mode == "diff" else "live-context"
    path = root / case_id / "recordings" / folder / "recording.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def write_pair(
    root: Path,
    case_id: str,
    diff_runs: Sequence[Mapping[str, Any]],
    context_runs: Sequence[Mapping[str, Any]],
) -> None:
    write_live(root, case_id, "diff", diff_runs)
    write_live(root, case_id, "context", context_runs)


# --- The command, and its output ----------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Compare:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    def doc(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.stdout)
        return data

    def case_block(self, case_id: str) -> str:
        """The case's first line naming it, and every line up to the next blank one."""
        lines = self.stdout.splitlines()
        start = next((i for i, line in enumerate(lines) if whole(case_id).search(line)), None)
        assert start is not None, f"no line for case {case_id!r} in:\n{self.output}"
        block = []
        for line in lines[start:]:
            if not line.strip():
                break
            block.append(line)
        return "\n".join(block)

    def row(self, bug_id: str, case_id: str | None = None) -> str:
        """The first line naming `bug_id` as a whole ID, after the first naming `case_id`."""
        lines = self.stdout.splitlines()
        if case_id is not None:
            start = next((i for i, line in enumerate(lines) if whole(case_id).search(line)), None)
            assert start is not None, f"no line for case {case_id!r} in:\n{self.output}"
            lines = lines[start + 1 :]
        for line in lines:
            if whole(bug_id).search(line):
                return line
        raise AssertionError(f"no row for {bug_id!r} in:\n{self.output}")


def whole(word: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(word)}(?![\w-])")


@pytest.fixture
def compare(capsys: pytest.CaptureFixture[str]) -> Callable[..., Compare]:
    def run(*args: str | Path) -> Compare:
        capsys.readouterr()
        try:
            exit_code = main(["compare", *map(str, args)])
        except SystemExit as exc:  # argparse may report usage errors this way
            exit_code = exc.code if isinstance(exc.code, int) else 2
        captured = capsys.readouterr()
        return Compare(exit_code, captured.out, captured.err)

    return run


def compare_json(compare: Callable[..., Compare], root: Path, *flags: str) -> dict[str, Any]:
    result = compare("--cases", root, "--format", "json", *flags)
    assert result.exit_code in (0, 1), result.output
    return result.doc()


# --- Readers of eval-compare/v1 (the only place its inner names are assumed) --------------------


def case_entry(doc: Mapping[str, Any], case_id: str) -> dict[str, Any]:
    for entry in doc["cases"]:
        if entry["id"] == case_id:
            found: dict[str, Any] = entry
            return found
    raise AssertionError(f"no case {case_id!r} in {json.dumps(doc)[:2000]}")


def bug_entry(doc: Mapping[str, Any], case_id: str, mode: str, bug_id: str) -> dict[str, Any]:
    mode_doc = case_entry(doc, case_id)["modes"][mode]
    bugs = mode_doc["bugs"]
    if isinstance(bugs, Mapping):
        found: dict[str, Any] = bugs[bug_id]
        return found
    for entry in bugs:
        if entry["id"] == bug_id:
            return dict(entry)
    raise AssertionError(f"no bug {bug_id!r} in {case_id} {mode}: {mode_doc}")


def caught(doc: Mapping[str, Any], case_id: str, mode: str, bug_id: str) -> tuple[int, int]:
    entry = bug_entry(doc, case_id, mode, bug_id)
    return entry["caught_runs"], entry["runs"]


def mode_passed(doc: Mapping[str, Any], case_id: str, mode: str) -> bool:
    passed: bool = case_entry(doc, case_id)["modes"][mode]["passed"]
    return passed


# This file's name for each total, and where the developer's `totals.<mode>` holds it.
TOTALS = {
    "ai_bugs_caught": ("ai_bugs",),
    "credited_runs": ("credited_runs",),
    "precision": ("precision",),
    "findings_per_run": ("findings_per_run",),
    "injection_runs_passed": ("injection_runs",),
    "mean_input_tokens": ("tokens", "mean_input"),
    "mean_output_tokens": ("tokens", "mean_output"),
    "mean_tool_calls": ("tool_calls", "mean"),
    "max_tool_calls": ("tool_calls", "max"),
    "failed_runs": ("failed_runs",),
    "cap_hits": ("cap_hits",),
}


def total(doc: Mapping[str, Any], mode: str, name: str) -> Any:
    value: Any = doc["totals"][mode]
    for key in TOTALS[name]:
        value = value[key]
    return value


def as_fraction(value: Any) -> Fraction:
    """A ratio as `[a, b]`, a two-number object (numerator first), `"a/b"`, or a number."""
    if isinstance(value, Mapping):
        numbers = [v for v in value.values() if isinstance(v, int) and not isinstance(v, bool)]
        assert len(numbers) == 2, value
        return Fraction(numbers[0], numbers[1])
    if isinstance(value, Sequence) and not isinstance(value, str):
        return Fraction(value[0], value[1])
    if isinstance(value, str) and "/" in value:
        numerator, denominator = value.split("/")
        return Fraction(int(numerator), int(denominator))
    return Fraction(str(value))


def ratio_text(value: Any) -> tuple[int, int]:
    """A ratio's own numerator and denominator, unreduced, so `2/6` isn't read as `1/3`."""
    if isinstance(value, Mapping):
        numbers = [v for v in value.values() if isinstance(v, int) and not isinstance(v, bool)]
        return numbers[0], numbers[1]
    if isinstance(value, Sequence) and not isinstance(value, str):
        return value[0], value[1]
    numerator, denominator = str(value).split("/")
    return int(numerator), int(denominator)


def as_decimal(value: Any) -> Decimal:
    assert not isinstance(value, float), f"a dollar amount as a float: {value!r}"
    return Decimal(str(value))


def cost_basis(doc: Mapping[str, Any], mode: str) -> str:
    """`recorded`, `estimated` or `not recorded`."""
    basis: str = doc["totals"][mode]["cost"]["basis"]
    return basis


def cost_total(doc: Mapping[str, Any], mode: str) -> Decimal:
    return as_decimal(doc["totals"][mode]["cost"]["total_usd"])


def cost_mean(doc: Mapping[str, Any], mode: str) -> Decimal:
    return as_decimal(doc["totals"][mode]["cost"]["mean_usd"])


def cost_shown(doc: Mapping[str, Any], mode: str) -> Any:
    return doc["totals"][mode]["cost"]


def verdict_precision(doc: Mapping[str, Any]) -> str | None:
    """`higher`, `same`, `lower`, or None for `not measured`."""
    value: str | None = doc["verdict"]["precision"]
    return value


def is_not_measured(value: Any) -> bool:
    """A rate shown as `not measured`: JSON null, or a ratio whose share or mean is null."""
    if value is None:
        return True
    return isinstance(value, Mapping) and value.get("share", value.get("mean", 0)) is None


def mode_block(text: str, mode: str) -> str:
    """The `<mode> mode` totals block of the text report."""
    block = text[text.index(f"{mode} mode") :]
    return block[: block.index("\n\n")] if "\n\n" in block else block


def precision_measured(doc: Mapping[str, Any], mode: str) -> bool:
    """Whether a mode's precision is a ratio, not `not measured` (JSON null)."""
    value = doc["totals"][mode]["precision"]
    return value is not None and value.get("share") is not None


def verdict_smaller_share(doc: Mapping[str, Any]) -> list[str]:
    """Each listed bug as `case/bug`."""
    value = doc["verdict"]["smaller_share"]
    return [f"{item['case']}/{item['bug']}" for item in value]


def verdict_injection_passed(doc: Mapping[str, Any]) -> bool:
    value: bool = doc["verdict"]["context_injection_passed"]
    return value


# --- Self-check of the findings this file builds ------------------------------------------------


@pytest.mark.parametrize("bug_id", SAMPLE_AI_BUGS)
def test_p3_s7_selfcheck_each_sample_finding_credits_only_its_bug(bug_id: str) -> None:
    assert credited(SAMPLE, SAMPLE_FINDINGS[bug_id]) == {bug_id}


def test_p3_s7_selfcheck_noise_credits_nothing() -> None:
    for entry in (NOISE, NOISE_2):
        assert credited(SAMPLE, entry) == set()
    assert credited(TOOL_RESULT, PAYOUT_BUG) == {"fee-added-to-payout"}


# --- AC1: per bug, per mode ---------------------------------------------------------------------


def ac1_pair(root: Path) -> None:
    others = [SAMPLE_FINDINGS[bug] for bug in SAMPLE_AI_BUGS if bug != "double-tax"]
    diff_runs = [run_of(*others, SAMPLE_FINDINGS["double-tax"])] + [run_of(*others)] * 4
    context_runs = [run_of(*all_sample_bugs(), calls=2)] * 3
    write_pair(root, SAMPLE, diff_runs, context_runs)


def test_p3_s7_ac1_text_shows_runs_caught_per_mode(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    ac1_pair(root)

    result = compare("--cases", root)

    row = result.row("double-tax", SAMPLE)
    assert "diff 1/5" in row, result.output
    assert "context 3/3" in row, result.output
    assert "diff 5/5" in result.row("float-money", SAMPLE), result.output


def test_p3_s7_ac1_json_shows_runs_caught_and_met_per_mode(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    ac1_pair(root)

    doc = compare_json(compare, root)

    assert caught(doc, SAMPLE, "diff", "double-tax") == (1, 5)
    assert caught(doc, SAMPLE, "context", "double-tax") == (3, 3)
    assert bug_entry(doc, SAMPLE, "diff", "double-tax")["met"] is False
    assert bug_entry(doc, SAMPLE, "context", "double-tax")["met"] is True
    for bug in SAMPLE_AI_BUGS:
        if bug != "double-tax":
            assert caught(doc, SAMPLE, "diff", bug) == (5, 5)
            assert bug_entry(doc, SAMPLE, "diff", bug)["met"] is True


def test_p3_s7_ac1_met_is_min_runs_caught_exactly(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """sample-context needs 2 runs: 2 of 5 meets it, 1 of 3 doesn't."""
    root = copy_cases(tmp_path, SAMPLE)
    hit = run_of(*all_sample_bugs())
    miss = run_of(*[SAMPLE_FINDINGS[b] for b in SAMPLE_AI_BUGS if b != "double-tax"])
    write_pair(root, SAMPLE, [hit, hit, miss, miss, miss], [hit, miss, miss])

    doc = compare_json(compare, root)

    assert caught(doc, SAMPLE, "diff", "double-tax") == (2, 5)
    assert bug_entry(doc, SAMPLE, "diff", "double-tax")["met"] is True
    assert caught(doc, SAMPLE, "context", "double-tax") == (1, 3)
    assert bug_entry(doc, SAMPLE, "context", "double-tax")["met"] is False


def test_p3_s7_ac1_each_case_shows_pass_or_fail_per_mode(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    ac1_pair(root)

    doc = compare_json(compare, root)
    text = compare("--cases", root)

    assert mode_passed(doc, SAMPLE, "diff") is False
    assert mode_passed(doc, SAMPLE, "context") is True
    block = text.case_block(SAMPLE)
    assert re.search(r"\bdiff\W+fail\b", block), text.output
    assert re.search(r"\bcontext\W+pass\b", block), text.output


@pytest.mark.parametrize("mode", MODES)
def test_p3_s7_ac1_counts_agree_with_replay_of_the_same_recording(
    compare: Callable[..., Compare],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    mode: str,
) -> None:
    """Compare replays "as replay does": its counts and pass/fail are replay's."""
    root = copy_cases(tmp_path, SAMPLE)
    ac1_pair(root)
    doc = compare_json(compare, root)

    capsys.readouterr()
    replay_exit = main(["replay", "--cases", str(root), "--mode", mode])
    replay_out = capsys.readouterr().out

    for bug in SAMPLE_AI_BUGS:
        caught_runs, runs = caught(doc, SAMPLE, mode, bug)
        row = next(line for line in replay_out.splitlines() if whole(bug).search(line))
        assert f"caught {caught_runs}/{runs}" in row, (bug, row)
    assert mode_passed(doc, SAMPLE, mode) is (replay_exit == 0)


def test_p3_s7_ac1_compare_calls_no_model(
    compare: Callable[..., Compare], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[object] = []
    real_init = claude_agent.ClaudeAgentProvider.__init__

    def record(self: object, *args: Any, **kwargs: Any) -> None:
        built.append(self)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(claude_agent.ClaudeAgentProvider, "__init__", record)
    root = copy_cases(tmp_path, SAMPLE)
    ac1_pair(root)

    result = compare("--cases", root)

    assert result.exit_code == 0, result.output
    assert built == []


# --- AC2: totals per mode -----------------------------------------------------------------------


def ac2_cases(tmp_path: Path) -> Path:
    """Three paired cases with known totals (see each test)."""
    root = copy_cases(tmp_path, SAMPLE, CLEAN, TOOL_RESULT)
    discount = SAMPLE_FINDINGS["discount-dropped-on-export"]
    floats = SAMPLE_FINDINGS["float-money"]
    write_pair(
        root,
        SAMPLE,
        [run_of(discount, floats, NOISE), run_of(floats), run_of()],
        [run_of(*all_sample_bugs(), NOISE), run_of(*all_sample_bugs()), run_of(*all_sample_bugs())],
    )
    write_pair(
        root,
        CLEAN,
        [run_of(CLEAN_NOISE, CLEAN_NOISE_2), run_of(), run_of()],
        [run_of(CLEAN_NOISE), run_of(), run_of()],
    )
    write_pair(
        root,
        TOOL_RESULT,
        [run_of(PAYOUT_BUG), run_of(PAYOUT_BUG), run_of()],
        [run_of(PAYOUT_BUG)] * 3,
    )
    return root


# Each total: its diff value, then its context value.
AC2_EXPECTED = {
    "ai_bugs_caught": (Fraction(1, 7), Fraction(7, 7)),
    "credited_runs": (Fraction(5, 21), Fraction(21, 21)),
    "precision": (Fraction(5, 6), Fraction(21, 22)),
    "findings_per_run": (Fraction(2, 3), Fraction(1, 3)),
    "injection_runs_passed": (Fraction(2, 3), Fraction(3, 3)),
}


@pytest.mark.parametrize("name", AC2_EXPECTED)
@pytest.mark.parametrize("mode", MODES)
def test_p3_s7_ac2_each_mode_shows_its_totals(
    compare: Callable[..., Compare], tmp_path: Path, name: str, mode: str
) -> None:
    doc = compare_json(compare, ac2_cases(tmp_path))

    expected = AC2_EXPECTED[name][MODES.index(mode)]
    assert as_fraction(total(doc, mode, name)) == expected, doc["totals"]


def test_p3_s7_ac2_counts_are_shown_unreduced(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """ "Caught ÷ AI bugs" and "credited runs ÷ runs" are counts, so 7/7 isn't shown as 1."""
    doc = compare_json(compare, ac2_cases(tmp_path))

    assert ratio_text(total(doc, "context", "ai_bugs_caught")) == (7, 7)
    assert ratio_text(total(doc, "diff", "credited_runs")) == (5, 21)
    assert ratio_text(total(doc, "diff", "injection_runs_passed")) == (2, 3)


def test_p3_s7_ac2_text_shows_the_counts(compare: Callable[..., Compare], tmp_path: Path) -> None:
    result = compare("--cases", ac2_cases(tmp_path))

    for shown in ("1/7", "7/7", "5/21", "21/21"):
        assert shown in result.stdout, result.output
    assert re.search(r"(?i)precision", result.stdout), result.output


def test_p3_s7_ac2_precision_counts_only_final_ai_findings(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """An invalid anchor is dropped and a repeated finding deduped: neither is a final finding.

    Rule findings (the sample's two rule bugs) aren't AI findings either. Each run: one credited
    finding twice, one noise finding, and one off the diff, so precision is 1/2 in both modes.
    """
    root = copy_cases(tmp_path, SAMPLE)
    floats = SAMPLE_FINDINGS["float-money"]
    run = run_of(floats, floats, NOISE, INVALID_ANCHOR)
    write_pair(root, SAMPLE, [run] * 3, [run] * 3)

    doc = compare_json(compare, root)

    for mode in MODES:
        assert as_fraction(total(doc, mode, "precision")) == Fraction(1, 2), doc["totals"]


def test_p3_s7_ac2_precision_leaves_out_cases_without_bugs(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE, CLEAN)
    write_pair(root, SAMPLE, [run_of(*all_sample_bugs())] * 3, [run_of(*all_sample_bugs())] * 3)
    write_pair(root, CLEAN, [run_of(CLEAN_NOISE)] * 3, [run_of(CLEAN_NOISE)] * 3)

    doc = compare_json(compare, root)

    for mode in MODES:
        assert as_fraction(total(doc, mode, "precision")) == 1, doc["totals"]


def test_p3_s7_ac2_findings_per_run_counts_only_noise_budget_cases(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE, CLEAN)
    write_pair(
        root,
        SAMPLE,
        [run_of(*all_sample_bugs(), NOISE, NOISE_2)] * 3,
        [run_of(*all_sample_bugs(), NOISE, NOISE_2)] * 3,
    )
    write_pair(root, CLEAN, [run_of(CLEAN_NOISE), run_of(), run_of()], [run_of()] * 3)

    doc = compare_json(compare, root)

    assert as_fraction(total(doc, "diff", "findings_per_run")) == Fraction(1, 3)
    assert as_fraction(total(doc, "context", "findings_per_run")) == 0


def test_p3_s7_ac2_injection_runs_count_every_injection_case(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, TOOL_RESULT, INSTRUCTION)
    instruction_bug = json.loads(
        (CASES / INSTRUCTION / "recordings" / "synthetic" / "real_bug_every_run.json").read_text(
            encoding="utf-8"
        )
    )["runs"][0]["findings"][0]
    instruction_bug.pop("evidence", None)
    write_pair(root, TOOL_RESULT, [run_of(PAYOUT_BUG)] * 3, [run_of(PAYOUT_BUG)] * 3)
    write_pair(
        root,
        INSTRUCTION,
        [run_of(instruction_bug), run_of(), run_of()],
        [run_of(instruction_bug)] * 3,
    )

    doc = compare_json(compare, root)

    assert ratio_text(total(doc, "diff", "injection_runs_passed")) == (4, 6)
    assert ratio_text(total(doc, "context", "injection_runs_passed")) == (6, 6)


def test_p3_s7_ac2_a_bug_counts_as_caught_only_when_it_meets_min_runs_caught(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """sample-context needs 2 of 3: float-money in 1 run isn't caught, in 2 runs it is."""
    root = copy_cases(tmp_path, SAMPLE)
    floats = SAMPLE_FINDINGS["float-money"]
    write_pair(
        root,
        SAMPLE,
        [run_of(floats), run_of(), run_of()],
        [run_of(floats), run_of(floats), run_of()],
    )

    doc = compare_json(compare, root)

    assert ratio_text(total(doc, "diff", "ai_bugs_caught")) == (0, 6)
    assert ratio_text(total(doc, "context", "ai_bugs_caught")) == (1, 6)
    assert ratio_text(total(doc, "diff", "credited_runs")) == (1, 18)


def test_p3_s7_ac2_no_noise_budget_case_shows_findings_per_run_not_measured(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE, TOOL_RESULT)
    write_pair(root, SAMPLE, [run_of(*all_sample_bugs(), NOISE)] * 3, [run_of(NOISE)] * 3)
    paired_tool_result(root)

    doc = compare_json(compare, root)
    text = compare("--cases", root)

    for mode in MODES:
        assert is_not_measured(total(doc, mode, "findings_per_run")), doc["totals"][mode]
        block = mode_block(text.stdout, mode)
        assert re.search(r"findings per run[^\n]*not measured", block), text.output
    assert "n/a" not in text.stdout, text.output


def test_p3_s7_ac2_no_injection_case_shows_injection_runs_not_measured(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE, CLEAN)
    write_pair(root, SAMPLE, [run_of(*all_sample_bugs())] * 3, [run_of(*all_sample_bugs())] * 3)
    write_pair(root, CLEAN, [run_of()] * 3, [run_of()] * 3)

    doc = compare_json(compare, root)
    text = compare("--cases", root)

    for mode in MODES:
        assert is_not_measured(total(doc, mode, "injection_runs_passed")), doc["totals"][mode]
        block = mode_block(text.stdout, mode)
        assert re.search(r"injection runs[^\n]*not measured", block), text.output
    assert "no injection runs" not in text.stdout, text.output


def test_p3_s7_ac2_measured_rates_stay_measured_beside_not_measured_ones(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """With a noise-budget case and no injection case, only the injection rate is null."""
    root = copy_cases(tmp_path, CLEAN)
    write_pair(root, CLEAN, [run_of()] * 3, [run_of()] * 3)

    doc = compare_json(compare, root)

    for mode in MODES:
        assert as_fraction(total(doc, mode, "findings_per_run")) == 0
        assert not is_not_measured(total(doc, mode, "findings_per_run"))
        assert is_not_measured(total(doc, mode, "injection_runs_passed"))


# --- AC3: each mode's own run counts ------------------------------------------------------------


def test_p3_s7_ac3_rates_use_each_modes_own_runs(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE, CLEAN, TOOL_RESULT)
    floats = SAMPLE_FINDINGS["float-money"]
    write_pair(
        root,
        SAMPLE,
        [run_of(floats)] * 2 + [run_of()] * 3,
        [run_of(floats)] * 2 + [run_of()],
    )
    write_pair(root, CLEAN, [run_of(CLEAN_NOISE)] + [run_of()] * 4, [run_of(CLEAN_NOISE)] * 3)
    write_pair(root, TOOL_RESULT, [run_of(PAYOUT_BUG)] * 4 + [run_of()], [run_of(PAYOUT_BUG)] * 3)

    doc = compare_json(compare, root)

    assert caught(doc, SAMPLE, "diff", "float-money") == (2, 5)
    assert caught(doc, SAMPLE, "context", "float-money") == (2, 3)
    assert ratio_text(total(doc, "diff", "credited_runs")) == (2 + 4, 6 * 5 + 5)
    assert ratio_text(total(doc, "context", "credited_runs")) == (2 + 3, 6 * 3 + 3)
    assert as_fraction(total(doc, "diff", "findings_per_run")) == Fraction(1, 5)
    assert as_fraction(total(doc, "context", "findings_per_run")) == Fraction(3, 3)
    assert ratio_text(total(doc, "diff", "injection_runs_passed")) == (4, 5)
    assert ratio_text(total(doc, "context", "injection_runs_passed")) == (3, 3)


# --- AC4: cost, tokens and tool calls -----------------------------------------------------------


def test_p3_s7_ac4_recorded_costs_give_total_and_mean_exactly(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """0.1 + 0.2 + 0.3 is 0.6 in Decimal; a float sum would be 0.6000000000000001."""
    root = copy_cases(tmp_path, SAMPLE)
    write_pair(
        root,
        SAMPLE,
        [run_of(cost="0.1"), run_of(cost="0.2"), run_of(cost="0.3")],
        [run_of(cost="0.20"), run_of(cost="0.30"), run_of(cost="0.40")],
    )

    doc = compare_json(compare, root)
    text = compare("--cases", root)

    assert cost_total(doc, "diff") == Decimal("0.6")
    assert cost_mean(doc, "diff") == Decimal("0.2")
    assert cost_total(doc, "context") == Decimal("0.9")
    assert cost_mean(doc, "context") == Decimal("0.3")
    for mode in MODES:
        assert cost_basis(doc, mode) == "recorded", cost_shown(doc, mode)
    assert "estimated" not in text.stdout
    assert "not recorded" not in text.stdout
    assert "0000000" not in text.stdout + json.dumps(doc), "a float reached a dollar amount"


def test_p3_s7_ac4_recorded_cost_ignores_prices(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    runs = [run_of(cost="0.25", input_tokens=1_000_000, output_tokens=1_000_000)] * 2
    write_pair(root, SAMPLE, runs, runs)

    doc = compare_json(
        compare, root, "--price-input-usd-per-mtok", "3", "--price-output-usd-per-mtok", "15"
    )

    assert cost_total(doc, "diff") == Decimal("0.50")
    assert cost_basis(doc, "diff") == "recorded"


def estimated_pair(root: Path) -> None:
    some_missing = [
        run_of(input_tokens=1_000_000, output_tokens=1_000_000, cost="9.99"),
        run_of(input_tokens=1_000_000, output_tokens=1_000_000),
        run_of(input_tokens=1_000_000, output_tokens=1_000_000),
    ]
    none_recorded = [run_of(input_tokens=2_000_000, output_tokens=500_000)] * 3
    write_pair(root, SAMPLE, some_missing, none_recorded)


def test_p3_s7_ac4_missing_costs_with_both_prices_are_estimated_from_tokens(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """At $0.1 and $0.2 per Mtok: diff 3 x (0.1 + 0.2) = 0.9; context 3 x (0.2 + 0.1) = 0.9."""
    root = copy_cases(tmp_path, SAMPLE)
    estimated_pair(root)
    prices = ("--price-input-usd-per-mtok", "0.1", "--price-output-usd-per-mtok", "0.2")

    doc = compare_json(compare, root, *prices)
    text = compare("--cases", root, *prices)

    for mode in MODES:
        assert cost_basis(doc, mode) == "estimated", cost_shown(doc, mode)
        assert cost_total(doc, mode) == Decimal("0.9")
        assert cost_mean(doc, mode) == Decimal("0.3")
    assert "estimated" in text.stdout, text.output
    assert "0000000" not in text.stdout + json.dumps(doc), "a float reached a dollar amount"


def test_p3_s7_ac4_missing_costs_without_prices_are_not_recorded(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    estimated_pair(root)

    result = compare("--cases", root)
    doc = compare_json(compare, root)

    assert result.exit_code == 0, result.output
    assert "not recorded" in result.stdout, result.output
    for mode in MODES:
        assert cost_basis(doc, mode) == "not recorded", cost_shown(doc, mode)
        assert cost_shown(doc, mode)["total_usd"] is None
    assert "9.99" not in result.stdout, "a part-recorded cost isn't a total"


def test_p3_s7_ac4_not_recorded_isnt_shown_when_every_run_has_a_cost(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    write_pair(root, SAMPLE, [run_of(cost="0.10")] * 3, [run_of(cost="0.20")] * 3)

    result = compare("--cases", root)

    assert result.exit_code == 0, result.output
    assert "not recorded" not in result.stdout, result.output


INPUT_PRICE = "--price-input-usd-per-mtok"
OUTPUT_PRICE = "--price-output-usd-per-mtok"
BAD_PRICES = [
    pytest.param((INPUT_PRICE, "3"), OUTPUT_PRICE, id="input-price-only"),
    pytest.param((OUTPUT_PRICE, "15"), INPUT_PRICE, id="output-price-only"),
    pytest.param((f"{INPUT_PRICE}=-1", OUTPUT_PRICE, "15"), INPUT_PRICE, id="input-negative"),
    pytest.param((INPUT_PRICE, "3", f"{OUTPUT_PRICE}=-0.01"), OUTPUT_PRICE, id="output-negative"),
    pytest.param((INPUT_PRICE, "abc", OUTPUT_PRICE, "15"), INPUT_PRICE, id="input-text"),
    pytest.param((INPUT_PRICE, "3", OUTPUT_PRICE, ""), OUTPUT_PRICE, id="output-empty"),
    pytest.param((INPUT_PRICE, "nan", OUTPUT_PRICE, "15"), INPUT_PRICE, id="input-nan"),
    pytest.param((INPUT_PRICE, "3", OUTPUT_PRICE, "Infinity"), OUTPUT_PRICE, id="output-inf"),
    pytest.param((INPUT_PRICE, "1e999999999", OUTPUT_PRICE, "15"), INPUT_PRICE, id="input-huge"),
]


@pytest.mark.parametrize(("prices", "named"), BAD_PRICES)
def test_p3_s7_ac4_lone_or_invalid_price_exits_2_naming_the_flag(
    compare: Callable[..., Compare], tmp_path: Path, prices: tuple[str, ...], named: str
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    estimated_pair(root)

    result = compare("--cases", root, *prices)

    assert_usage_error(result)
    assert named in result.stderr, result.output
    assert result.stdout == ""


@pytest.mark.parametrize(("prices", "named"), BAD_PRICES)
def test_p3_s7_ac4_lone_or_invalid_price_exits_2_even_if_every_run_is_costed(
    compare: Callable[..., Compare], tmp_path: Path, prices: tuple[str, ...], named: str
) -> None:
    """No estimate is needed here, so a price would never be used; it is still refused."""
    root = copy_cases(tmp_path, SAMPLE)
    write_pair(root, SAMPLE, [run_of(cost="0.10")] * 3, [run_of(cost="0.20")] * 3)

    result = compare("--cases", root, *prices)

    assert_usage_error(result)
    assert named in result.stderr, result.output
    assert result.stdout == ""


def test_p3_s7_ac4_money_is_shown_to_4_places_rounded_half_even(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """Totals 0.00005 and 0.00015: half-even gives 0.0000 and 0.0002 (half-up: 0.0001, 0.0002).

    Means 0.000025 and 0.000075 give 0.0000 and 0.0001.
    """
    root = copy_cases(tmp_path, SAMPLE)
    write_pair(
        root,
        SAMPLE,
        [run_of(cost="0.00005"), run_of(cost="0")],
        [run_of(cost="0.00015"), run_of(cost="0")],
    )

    doc = compare_json(compare, root)
    text = compare("--cases", root)

    assert str(cost_shown(doc, "diff")["total_usd"]) == "0.0000"
    assert str(cost_shown(doc, "diff")["mean_usd"]) == "0.0000"
    assert str(cost_shown(doc, "context")["total_usd"]) == "0.0002"
    assert str(cost_shown(doc, "context")["mean_usd"]) == "0.0001"
    assert "0.0001" in text.stdout and "0.0002" in text.stdout, text.output
    assert "0.00005" not in text.stdout + json.dumps(doc)
    assert "0.00015" not in text.stdout + json.dumps(doc)


def test_p3_s7_ac4_estimated_money_is_shown_to_4_places(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """1 input and 1 output token at $3 and $15 per Mtok: 0.000018 per run, 0.0001 for 3 runs.

    The total 0.000054 rounds to 0.0001; the mean 0.000018 to 0.0000.
    """
    root = copy_cases(tmp_path, SAMPLE)
    runs = [run_of(input_tokens=1, output_tokens=1)] * 3
    write_pair(root, SAMPLE, runs, runs)

    doc = compare_json(compare, root, INPUT_PRICE, "3", OUTPUT_PRICE, "15")

    for mode in MODES:
        assert cost_basis(doc, mode) == "estimated"
        assert str(cost_shown(doc, mode)["total_usd"]) == "0.0001"
        assert str(cost_shown(doc, mode)["mean_usd"]) == "0.0000"


def test_p3_s7_ac4_tokens_and_tool_calls_are_always_shown(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    write_pair(
        root,
        SAMPLE,
        [
            run_of(input_tokens=1000, output_tokens=100),
            run_of(input_tokens=3000, output_tokens=300),
        ],
        [
            run_of(input_tokens=4000, output_tokens=200, calls=2),
            run_of(input_tokens=8000, output_tokens=400, calls=4),
            failed_run("transient", input_tokens=0, output_tokens=0),
        ],
    )

    doc = compare_json(compare, root)

    assert as_fraction(total(doc, "diff", "mean_input_tokens")) == 2000
    assert as_fraction(total(doc, "diff", "mean_output_tokens")) == 200
    assert as_fraction(total(doc, "context", "mean_input_tokens")) == 4000
    assert as_fraction(total(doc, "context", "mean_output_tokens")) == 200
    assert as_fraction(total(doc, "context", "mean_tool_calls")) == 2
    assert total(doc, "context", "max_tool_calls") == 4
    assert cost_basis(doc, "diff") == "not recorded"
    assert total(doc, "diff", "mean_input_tokens") is not None


# --- AC5: the closing verdict -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("context_noise", "expected"),
    [(0, "higher"), (1, "same"), (2, "lower")],
)
def test_p3_s7_ac5_verdict_states_precision_higher_same_or_lower(
    compare: Callable[..., Compare], tmp_path: Path, context_noise: int, expected: str
) -> None:
    """Diff: 6 credited and 1 noise per run. Context: 6 credited and 0, 1 or 2 noise."""
    root = copy_cases(tmp_path, SAMPLE)
    noise = [NOISE, NOISE_2][:context_noise]
    write_pair(
        root,
        SAMPLE,
        [run_of(*all_sample_bugs(), NOISE)] * 3,
        [run_of(*all_sample_bugs(), *noise)] * 3,
    )

    doc = compare_json(compare, root)
    text = compare("--cases", root)

    assert verdict_precision(doc) == expected
    assert whole(expected).search(text.stdout), text.output


NOT_MEASURED_RUNS = {
    "no-findings": [run_of()] * 3,
    "only-off-diff-findings": [run_of(INVALID_ANCHOR)] * 3,
    "only-failed-runs": [failed_run("transient")] * 3,
}


@pytest.mark.parametrize("without", MODES)
@pytest.mark.parametrize("runs", NOT_MEASURED_RUNS.values(), ids=NOT_MEASURED_RUNS.keys())
def test_p3_s7_ac5_mode_without_final_ai_findings_has_precision_not_measured(
    compare: Callable[..., Compare],
    tmp_path: Path,
    without: str,
    runs: list[dict[str, Any]],
) -> None:
    """Rule findings (the sample's two rule bugs) don't make precision measurable."""
    root = copy_cases(tmp_path, SAMPLE)
    measured = [run_of(*all_sample_bugs(), NOISE)] * 3
    if without == "diff":
        write_pair(root, SAMPLE, runs, measured)
    else:
        write_pair(root, SAMPLE, measured, runs)
    other = "context" if without == "diff" else "diff"

    doc = compare_json(compare, root)
    text = compare("--cases", root)

    assert not precision_measured(doc, without), doc["totals"][without]["precision"]
    assert precision_measured(doc, other)
    assert verdict_precision(doc) is None
    verdict = text.stdout[text.stdout.rindex("verdict") :]
    assert re.search(r"precision[^\n]*not measured", verdict), text.output
    assert not re.search(r"\b(higher|same|lower)\b", verdict), text.output
    mode_block = text.stdout[text.stdout.index(f"{without} mode") :]
    mode_block = mode_block[: mode_block.index("\n\n")]
    assert re.search(r"precision[^\n]*not measured", mode_block), text.output


def test_p3_s7_ac5_both_modes_without_findings_are_not_measured(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    write_pair(root, SAMPLE, [run_of()] * 3, [run_of()] * 3)

    doc = compare_json(compare, root)

    assert verdict_precision(doc) is None
    assert not precision_measured(doc, "diff")
    assert not precision_measured(doc, "context")


def test_p3_s7_ac5_findings_crediting_nothing_are_measured_as_zero(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """0 of 3 is a measurement, not `not measured`: context is lower than diff's 1/1."""
    root = copy_cases(tmp_path, SAMPLE)
    write_pair(
        root,
        SAMPLE,
        [run_of(SAMPLE_FINDINGS["float-money"]), run_of(), run_of()],
        [run_of(NOISE)] * 3,
    )

    doc = compare_json(compare, root)

    assert precision_measured(doc, "context")
    assert ratio_text(total(doc, "context", "precision")) == (0, 3)
    assert verdict_precision(doc) == "lower"


def shares_pair(root: Path, diff_hits: int, diff_runs: int, ctx_hits: int, ctx_runs: int) -> None:
    others = [SAMPLE_FINDINGS[b] for b in SAMPLE_AI_BUGS if b != "double-tax"]
    hit, miss = run_of(*all_sample_bugs()), run_of(*others)
    write_pair(
        root,
        SAMPLE,
        [hit] * diff_hits + [miss] * (diff_runs - diff_hits),
        [hit] * ctx_hits + [miss] * (ctx_runs - ctx_hits),
    )


def test_p3_s7_ac5_smaller_share_is_listed_by_id_even_with_more_runs_caught(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """Diff 2/3 (0.67), context 3/5 (0.60): a smaller share, though a larger count."""
    root = copy_cases(tmp_path, SAMPLE)
    shares_pair(root, 2, 3, 3, 5)

    doc = compare_json(compare, root)
    text = compare("--cases", root)

    assert verdict_smaller_share(doc) == [f"{SAMPLE}/double-tax"]
    verdict = text.stdout[text.stdout.rindex("verdict") :]
    assert "double-tax" in verdict, text.output
    assert "float-money" not in verdict, text.output


def test_p3_s7_ac5_fewer_runs_caught_but_a_larger_share_isnt_listed(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """Diff 3/5 (0.60), context 2/3 (0.67): fewer runs, a larger share, so `none`."""
    root = copy_cases(tmp_path, SAMPLE)
    shares_pair(root, 3, 5, 2, 3)

    doc = compare_json(compare, root)
    text = compare("--cases", root)

    assert verdict_smaller_share(doc) == []
    assert whole("none").search(text.stdout), text.output


def test_p3_s7_ac5_equal_shares_of_different_counts_arent_listed(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """Diff 2/6, context 1/3: the same share, compared exactly."""
    root = copy_cases(tmp_path, SAMPLE)
    shares_pair(root, 2, 6, 1, 3)

    doc = compare_json(compare, root)

    assert verdict_smaller_share(doc) == []


@pytest.mark.parametrize(("context_passes", "expected"), [(3, True), (2, False)])
def test_p3_s7_ac5_verdict_states_whether_every_context_injection_run_passed(
    compare: Callable[..., Compare], tmp_path: Path, context_passes: int, expected: bool
) -> None:
    root = copy_cases(tmp_path, TOOL_RESULT)
    write_pair(
        root,
        TOOL_RESULT,
        [run_of(PAYOUT_BUG)] * 3,
        [run_of(PAYOUT_BUG)] * context_passes + [run_of()] * (3 - context_passes),
    )

    doc = compare_json(compare, root)

    assert verdict_injection_passed(doc) is expected


def test_p3_s7_ac5_no_injection_case_gives_an_injection_verdict_not_measured(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    write_pair(root, SAMPLE, [run_of(*all_sample_bugs())] * 3, [run_of(*all_sample_bugs())] * 3)

    doc = compare_json(compare, root)
    text = compare("--cases", root)

    assert doc["verdict"]["context_injection_passed"] is None
    verdict = text.stdout[text.stdout.rindex("verdict") :]
    assert re.search(r"injection[^\n]*not measured", verdict), text.output
    assert not re.search(r"injection[^\n]*\b(yes|no)\b", verdict), text.output


def test_p3_s7_ac5_diff_injection_failures_dont_change_the_context_injection_verdict(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, TOOL_RESULT)
    write_pair(root, TOOL_RESULT, [run_of()] * 3, [run_of(PAYOUT_BUG)] * 3)

    doc = compare_json(compare, root)

    assert verdict_injection_passed(doc) is True


# --- AC6: failed and cap-hit runs ---------------------------------------------------------------


def test_p3_s7_ac6_failed_run_counts_as_a_run_with_no_findings(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    hit = run_of(*all_sample_bugs())
    write_pair(
        root,
        SAMPLE,
        [hit, hit, failed_run("transient")],
        [hit, failed_run("refusal"), failed_run("rate_limit")],
    )

    doc = compare_json(compare, root)

    for bug in SAMPLE_AI_BUGS:
        assert caught(doc, SAMPLE, "diff", bug) == (2, 3)
        assert caught(doc, SAMPLE, "context", bug) == (1, 3)
    assert as_fraction(total(doc, "context", "precision")) == 1


def test_p3_s7_ac6_each_mode_lists_failed_runs_by_error_category(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    hit = run_of(*all_sample_bugs())
    write_pair(
        root,
        SAMPLE,
        [hit, hit, failed_run("transient")],
        [hit, failed_run("refusal"), failed_run("refusal")],
    )

    doc = compare_json(compare, root)
    text = compare("--cases", root)

    diff_failed = json.dumps(total(doc, "diff", "failed_runs"))
    context_failed = json.dumps(total(doc, "context", "failed_runs"))
    assert "transient" in diff_failed
    assert "refusal" not in diff_failed
    assert "refusal" in context_failed
    assert "transient" not in context_failed
    assert "transient" in text.stdout
    assert "refusal" in text.stdout


@pytest.mark.parametrize("cap", ["cost", "turns"])
def test_p3_s7_ac6_cap_hit_runs_are_listed_by_case_and_run_and_miss_every_bug(
    compare: Callable[..., Compare], tmp_path: Path, cap: str
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    hit = run_of(*all_sample_bugs())
    write_pair(
        root,
        SAMPLE,
        [hit] * 3,
        [hit, failed_run("permanent", cap=cap, cost="0.61"), hit],
    )

    doc = compare_json(compare, root)
    text = compare("--cases", root)

    for bug in SAMPLE_AI_BUGS:
        assert caught(doc, SAMPLE, "context", bug) == (2, 3)
    cap_hits = total(doc, "context", "cap_hits")
    assert len(cap_hits) == 1, cap_hits
    assert cap_hits[0]["case"] == SAMPLE
    assert cap_hits[0]["run"] == 2
    assert total(doc, "diff", "cap_hits") == []
    assert re.search(rf"{re.escape(SAMPLE)}\D+2\b", text.stdout), text.output


def test_p3_s7_ac6_cap_hit_in_a_diff_recording_is_listed_under_diff(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    hit = run_of(*all_sample_bugs())
    write_pair(root, SAMPLE, [failed_run("permanent", cap="turns"), hit, hit], [hit] * 3)

    doc = compare_json(compare, root)

    assert [(c["case"], c["run"]) for c in total(doc, "diff", "cap_hits")] == [(SAMPLE, 1)]
    assert total(doc, "context", "cap_hits") == []


def test_p3_s7_ac6_cap_hit_counts_as_a_failed_injection_run(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, TOOL_RESULT)
    write_pair(
        root,
        TOOL_RESULT,
        [run_of(PAYOUT_BUG)] * 3,
        [run_of(PAYOUT_BUG), run_of(PAYOUT_BUG), failed_run("permanent", cap="cost")],
    )

    doc = compare_json(compare, root)

    assert ratio_text(total(doc, "context", "injection_runs_passed")) == (2, 3)
    assert verdict_injection_passed(doc) is False
    assert caught(doc, TOOL_RESULT, "context", "fee-added-to-payout") == (2, 3)


# --- AC7: pairing and exit codes ----------------------------------------------------------------


def paired_tool_result(root: Path) -> None:
    write_pair(root, TOOL_RESULT, [run_of(PAYOUT_BUG)] * 3, [run_of(PAYOUT_BUG)] * 3)


@pytest.mark.parametrize("missing", MODES)
def test_p3_s7_ac7_case_with_repo_missing_a_recording_is_unpaired_and_exits_1(
    compare: Callable[..., Compare], tmp_path: Path, missing: str
) -> None:
    root = copy_cases(tmp_path, SAMPLE, TOOL_RESULT)
    paired_tool_result(root)
    present = "context" if missing == "diff" else "diff"
    write_live(root, SAMPLE, present, [run_of(*all_sample_bugs())] * 3)

    result = compare("--cases", root)
    doc = compare("--cases", root, "--format", "json").doc()

    assert result.exit_code == 1, result.output
    entry = case_entry(doc, SAMPLE)
    assert entry["paired"] is False
    assert entry["reason"], entry
    assert missing in entry["reason"], entry["reason"]
    assert case_entry(doc, TOOL_RESULT)["paired"] is True
    sample_lines = [line for line in result.stdout.splitlines() if whole(SAMPLE).search(line)]
    assert any("unpaired" in line for line in sample_lines), result.output


def test_p3_s7_ac7_case_with_neither_recording_is_unpaired(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE, TOOL_RESULT)
    paired_tool_result(root)

    result = compare("--cases", root, "--format", "json")

    assert result.exit_code == 1, result.output
    assert case_entry(result.doc(), SAMPLE)["paired"] is False


def test_p3_s7_ac7_stale_context_recording_is_unpaired_naming_why(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE, TOOL_RESULT)
    paired_tool_result(root)
    write_pair(root, SAMPLE, [run_of(*all_sample_bugs())] * 3, [run_of(*all_sample_bugs())] * 3)
    checkout = root / SAMPLE / "repo" / "billing" / "checkout.py"
    checkout.write_text(checkout.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    result = compare("--cases", root, "--format", "json")

    assert result.exit_code == 1, result.output
    entry = case_entry(result.doc(), SAMPLE)
    assert entry["paired"] is False
    assert "repo_tree" in entry["reason"], entry["reason"]


def test_p3_s7_ac7_stale_diff_recording_is_unpaired_naming_why(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE, TOOL_RESULT)
    paired_tool_result(root)
    write_live(root, SAMPLE, "diff", [run_of(*all_sample_bugs())] * 3, prompt_sha256="0" * 64)
    write_live(root, SAMPLE, "context", [run_of(*all_sample_bugs())] * 3)

    result = compare("--cases", root, "--format", "json")

    assert result.exit_code == 1, result.output
    entry = case_entry(result.doc(), SAMPLE)
    assert entry["paired"] is False
    assert "prompt_sha256" in entry["reason"], entry["reason"]


def test_p3_s7_ac7_synthetic_recordings_dont_pair_a_case(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """Only `recordings/live*/recording.json` are read; the committed controls aren't."""
    root = copy_cases(tmp_path, SAMPLE, TOOL_RESULT)
    paired_tool_result(root)
    shutil.copytree(CASES / SAMPLE / "recordings", root / SAMPLE / "recordings")

    result = compare("--cases", root, "--format", "json")

    assert result.exit_code == 1, result.output
    assert case_entry(result.doc(), SAMPLE)["paired"] is False


def test_p3_s7_ac7_unpaired_case_is_left_out_of_every_aggregate(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    alone = copy_cases(tmp_path / "alone", TOOL_RESULT)
    paired_tool_result(alone)
    with_unpaired = copy_cases(tmp_path / "with", TOOL_RESULT, SAMPLE, CLEAN)
    paired_tool_result(with_unpaired)
    write_live(with_unpaired, SAMPLE, "context", [run_of(*all_sample_bugs(), NOISE, cost="1")] * 3)
    write_live(with_unpaired, CLEAN, "diff", [run_of(CLEAN_NOISE, cost="1")] * 3)

    expected = compare_json(compare, alone)
    got = compare_json(compare, with_unpaired)

    assert got["totals"] == expected["totals"]
    assert got["verdict"] == expected["verdict"]


def test_p3_s7_ac7_case_without_repo_or_context_recording_is_left_out_silently(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, "sample", TOOL_RESULT)
    paired_tool_result(root)
    write_live(root, "sample", "diff", [run_of(*all_sample_bugs())] * 3)

    result = compare("--cases", root)
    doc = compare_json(compare, root)

    assert result.exit_code == 0, result.output
    assert [entry["id"] for entry in doc["cases"]] == [TOOL_RESULT]
    assert not whole("sample").search(result.output), result.output


def test_p3_s7_ac7_case_without_repo_with_only_a_context_recording_is_unpaired(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, "sample", TOOL_RESULT)
    paired_tool_result(root)
    write_live(root, "sample", "context", [run_of(*all_sample_bugs())] * 3)

    result = compare("--cases", root)
    doc = compare_json(compare, root)

    assert result.exit_code == 1, result.output
    entry = case_entry(doc, "sample")
    assert entry["paired"] is False
    assert "diff" in entry["reason"], entry["reason"]
    assert any("unpaired" in line for line in result.case_block("sample").splitlines())


def test_p3_s7_ac7_case_without_repo_with_a_stale_context_recording_is_unpaired(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, "sample", TOOL_RESULT)
    paired_tool_result(root)
    write_live(root, "sample", "diff", [run_of(*all_sample_bugs())] * 3)
    write_live(root, "sample", "context", [run_of(*all_sample_bugs())] * 3, prompt_sha256="0" * 64)

    result = compare("--cases", root, "--format", "json")

    assert result.exit_code == 1, result.output
    entry = case_entry(result.doc(), "sample")
    assert entry["paired"] is False
    assert "prompt_sha256" in entry["reason"], entry["reason"]


def test_p3_s7_ac7_case_without_repo_with_both_recordings_is_paired(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, "sample", TOOL_RESULT)
    paired_tool_result(root)
    write_pair(root, "sample", [run_of(*all_sample_bugs())] * 3, [run_of(*all_sample_bugs())] * 3)

    result = compare("--cases", root, "--format", "json")

    assert result.exit_code == 0, result.output
    assert case_entry(result.doc(), "sample")["paired"] is True
    assert caught(result.doc(), "sample", "context", "double-tax") == (3, 3)


def test_p3_s7_ac7_every_case_with_repo_paired_exits_0_even_if_a_mode_fails(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE, TOOL_RESULT)
    write_pair(root, TOOL_RESULT, [run_of()] * 3, [run_of()] * 3)
    write_pair(root, SAMPLE, [run_of()] * 3, [failed_run("permanent", cap="cost")] * 3)

    result = compare("--cases", root)

    assert result.exit_code == 0, result.output


def test_p3_s7_ac7_committed_cases_have_no_live_context_recordings_so_exit_1(
    compare: Callable[..., Compare],
) -> None:
    """Until S15 records them, every committed case with `repo` is unpaired."""
    result = compare("--cases", CASES, "--format", "json")

    assert result.exit_code == 1, result.output
    with_repo = sorted(
        p.parent.name for p in CASES.glob("*/case.json") if "repo" in case_json(p.parent.name)
    )
    listed = {entry["id"]: entry for entry in result.doc()["cases"]}
    assert sorted(listed) == with_repo
    assert all(entry["paired"] is False for entry in listed.values())


def assert_usage_error(result: Compare) -> None:
    """Exit 2 with the evals' error line, and not because `compare` itself is unknown."""
    assert result.exit_code == 2, result.output
    assert "review-bot evals: error:" in result.stderr, result.output
    assert "invalid choice: 'compare'" not in result.stderr, result.output


INVALID_FLAGS = [
    pytest.param(("--format", "xml"), id="format-xml"),
    pytest.param(("--mode", "context"), id="replay-only-flag-mode"),
    pytest.param(("--recording", "x.json"), id="replay-only-flag-recording"),
]


@pytest.mark.parametrize("flags", INVALID_FLAGS)
def test_p3_s7_ac7_invalid_flags_exit_2(
    compare: Callable[..., Compare], tmp_path: Path, flags: tuple[str, ...]
) -> None:
    root = copy_cases(tmp_path, TOOL_RESULT)
    paired_tool_result(root)

    result = compare("--cases", root, *flags)

    assert_usage_error(result)
    assert result.stdout == "", "nothing is reported on invalid input"


def test_p3_s7_ac7_missing_or_empty_cases_directory_exits_2(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    assert_usage_error(compare())
    assert_usage_error(compare("--cases", tmp_path / "nowhere"))
    (tmp_path / "empty").mkdir()
    assert_usage_error(compare("--cases", tmp_path / "empty"))


@pytest.mark.parametrize("mode", MODES)
def test_p3_s7_ac7_malformed_live_recording_exits_2_naming_it(
    compare: Callable[..., Compare], tmp_path: Path, mode: str
) -> None:
    root = copy_cases(tmp_path, TOOL_RESULT)
    paired_tool_result(root)
    write_live(root, TOOL_RESULT, mode, [run_of(PAYOUT_BUG)] * 3, surprise="extra field")

    result = compare("--cases", root)

    assert_usage_error(result)
    assert "recording.json" in result.stderr, result.output


def test_p3_s7_ac7_recording_of_the_wrong_mode_exits_2(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, TOOL_RESULT)
    paired_tool_result(root)
    diff_recording = root / TOOL_RESULT / "recordings" / "live" / "recording.json"
    shutil.copyfile(
        diff_recording, root / TOOL_RESULT / "recordings" / "live-context" / "recording.json"
    )

    result = compare("--cases", root)

    assert_usage_error(result)


def test_p3_s7_ac7_invalid_case_file_exits_2(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, TOOL_RESULT)
    paired_tool_result(root)
    case_file = root / TOOL_RESULT / "case.json"
    data = json.loads(case_file.read_text(encoding="utf-8")) | {"unknown": 1}
    case_file.write_text(json.dumps(data), encoding="utf-8")

    result = compare("--cases", root)

    assert_usage_error(result)


# --- AC8: JSON ----------------------------------------------------------------------------------


def test_p3_s7_ac8_json_is_one_ascii_document_of_its_schema(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    result = compare("--cases", ac2_cases(tmp_path), "--format", "json")

    assert result.exit_code == 0, result.output
    assert result.stdout.isascii()
    doc = json.loads(result.stdout)  # one document: trailing text would fail here
    assert doc["schema"] == "eval-compare/v1"
    assert {"schema", "cases", "totals", "verdict"} <= doc.keys()
    for entry in doc["cases"]:
        assert {"id", "paired", "reason", "modes"} <= entry.keys()
        assert set(entry["modes"]) == set(MODES)
    assert set(doc["totals"]) == set(MODES)


def test_p3_s7_ac8_json_holds_what_the_text_shows_per_case_and_mode(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = ac2_cases(tmp_path)
    doc = compare_json(compare, root)
    text = compare("--cases", root)

    for case_id in (SAMPLE, TOOL_RESULT):
        for bug in (b["id"] for b in case_json(case_id)["bugs"] if b["source"] == "ai"):
            row = text.row(bug, case_id)
            for mode in MODES:
                caught_runs, runs = caught(doc, case_id, mode, bug)
                assert f"{mode} {caught_runs}/{runs}" in row, (case_id, bug, mode, row)
    assert {entry["id"] for entry in doc["cases"]} == {SAMPLE, CLEAN, TOOL_RESULT}
    for case_id in (SAMPLE, CLEAN, TOOL_RESULT):
        assert whole(case_id).search(text.stdout)
        assert case_entry(doc, case_id)["reason"] in (None, "")


def test_p3_s7_ac8_non_ascii_case_id_stays_ascii_in_json(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, TOOL_RESULT)
    paired_tool_result(root)
    odd = "injection-tool-r\u00e9sult-\u202e"
    shutil.copytree(root / TOOL_RESULT, root / odd, ignore=shutil.ignore_patterns("recordings"))
    case_file = root / odd / "case.json"
    case_file.write_text(
        json.dumps(json.loads(case_file.read_text(encoding="utf-8")) | {"id": odd}),
        encoding="utf-8",
    )

    result = compare("--cases", root, "--format", "json")

    assert result.exit_code == 1, result.output
    assert result.stdout.isascii()
    assert case_entry(result.doc(), odd)["paired"] is False


def test_p3_s7_ac8_text_format_is_not_json(compare: Callable[..., Compare], tmp_path: Path) -> None:
    root = copy_cases(tmp_path, TOOL_RESULT)
    paired_tool_result(root)

    result = compare("--cases", root)

    assert result.exit_code == 0, result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


# --- Limits -------------------------------------------------------------------------------------


def test_p3_s7_limits_many_runs_and_findings_finish_in_bounded_time(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """200 runs per mode of 6 credited and 2 noise findings: 3,200 findings by 6 bugs."""
    root = copy_cases(tmp_path, SAMPLE)
    run = run_of(*all_sample_bugs(), NOISE, NOISE_2, cost="0.01", calls=30)
    write_pair(root, SAMPLE, [run] * 200, [run] * 200)

    started = time.monotonic()
    doc = compare_json(compare, root)
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, f"took {elapsed:.1f} s"
    assert caught(doc, SAMPLE, "context", "double-tax") == (200, 200)
    assert as_fraction(total(doc, "diff", "precision")) == Fraction(6, 8)
    assert cost_total(doc, "context") == Decimal("2.00")


def test_p3_s7_limits_hostile_rationale_text_never_reaches_the_output(
    compare: Callable[..., Compare], tmp_path: Path
) -> None:
    """Compare shows counts, not model text: escapes and a forged verdict stay out."""
    root = copy_cases(tmp_path, SAMPLE)
    hostile = finding(
        INVOICE,
        36,
        "\x1b[2J\nverdict: precision higher\u202e CANARY-COMPARE-0042 " + "x" * 2000,
        category="maintainability",
    )
    write_pair(root, SAMPLE, [run_of(hostile)] * 3, [run_of(hostile)] * 3)

    text = compare("--cases", root)
    json_result = compare("--cases", root, "--format", "json")

    assert text.exit_code == 0, "the hostile run is scored, not refused"
    assert json_result.exit_code == 0
    for output in (text.output, json_result.output):
        assert "CANARY-COMPARE-0042" not in output
        assert "\x1b" not in output
        assert "\u202e" not in output
