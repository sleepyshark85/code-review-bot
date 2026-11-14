"""Unit tests for the eval gate: the suites file, the prompt pins, coverage and each suite case."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from eval_cases import finding, recording, run_of, write_case, write_json, write_live

from review_bot import prompt
from review_bot.evals import main as evals_main
from review_bot.evals.formats import EvalInputError, find_case_files, live_recording_dir, load_case
from review_bot.evals.gate import (
    GateResult,
    Suite,
    check_coverage,
    check_prompt_pins,
    live_command,
    load_suites,
    run_gate,
)
from review_bot.evals.gate_render import render_gate_json, render_gate_text
from review_bot.governance.policy import BUILTIN_POLICY, ModelEntry, ModelProvider, Policy

MODEL = "claude-opus-5-5"
FLOAT_FINDING = finding(rationale="Summing floats loses cents.")
SUITE = f'[[suite]]\nprovider = "claude"\nmodel = "{MODEL}"\nmode = "diff"\ncases = ["calc"]\n'
ACCEPTED = (
    '[[suite.accepted_miss]]\ncase = "calc"\nbug = "float-sum"\napproved_by = "owner"\n'
    "approved_on = 2026-11-07\n"
)


def suites_file(tmp_path: Path, text: str = SUITE) -> Path:
    path = tmp_path / "gate.toml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def write_live_recording(
    case_file: Path,
    runs: Sequence[Mapping[str, Any]],
    provider: ModelProvider = "claude",
    **fields: Any,
) -> None:
    data = recording(case_file, runs, kind="live", **fields)
    write_json(case_file.parent / live_recording_dir("diff", provider) / "recording.json", data)


def gate(cases_dir: Path, suites: Path, policy: Policy = BUILTIN_POLICY) -> GateResult:
    return asyncio.run(run_gate(cases_dir, suites, policy))


def loaded_cases(cases_dir: Path) -> dict[str, Any]:
    return {path.parent.name: load_case(path) for path in find_case_files(cases_dir)}


def rejected(tmp_path: Path, text: str | bytes) -> str:
    write_case(tmp_path / "cases")
    path = tmp_path / "gate.toml"
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    with pytest.raises(EvalInputError) as caught:
        load_suites(path, loaded_cases(tmp_path / "cases"))
    return str(caught.value)


# --- The suites file -----------------------------------------------------------------------------


def test_load_suites_reads_each_suite_with_its_accepted_misses(tmp_path: Path) -> None:
    write_case(tmp_path / "cases")

    [suite] = load_suites(suites_file(tmp_path, SUITE + ACCEPTED), loaded_cases(tmp_path / "cases"))

    assert suite == Suite("claude", MODEL, "diff", ("calc",), frozenset({("calc", "float-sum")}))
    assert suite.name == f"claude:{MODEL} diff"


@pytest.mark.parametrize(
    ("text", "named"),
    [
        (SUITE + 'colour = "blue"\n', "colour"),
        (SUITE.replace('mode = "diff"\n', ""), "mode"),
        (SUITE.replace('mode = "diff"', 'mode = "telepathy"'), "mode"),
        (SUITE.replace('provider = "claude"', 'provider = "mock"'), "provider"),
        (SUITE.replace('cases = ["calc"]', "cases = []"), "cases"),
        (SUITE + ACCEPTED.replace('approved_by = "owner"\n', ""), "approved_by"),
        (SUITE + ACCEPTED.replace('approved_by = "owner"', 'approved_by = " "'), "approved_by"),
        (SUITE + ACCEPTED.replace("2026-11-07", '"2026-11-07"'), "approved_on"),
        (SUITE + ACCEPTED.replace("2026-11-07", "2026-11-07T10:00:00"), "approved_on"),
        ("", "suite"),
        ("suite = 1\n", "suite"),
    ],
    ids=[
        "unknown-key",
        "missing-field",
        "unknown-mode",
        "mock-provider",
        "no-cases",
        "no-approver",
        "blank-approver",
        "date-as-string",
        "datetime",
        "empty",
        "suite-not-a-table-array",
    ],
)
def test_load_suites_with_an_invalid_key_names_it(tmp_path: Path, text: str, named: str) -> None:
    message = rejected(tmp_path, text)

    assert named in message
    assert "gate.toml" in message


@pytest.mark.parametrize(
    ("text", "named"),
    [
        (SUITE.replace('["calc"]', '["calc", "nope"]'), "'nope', which isn't a case"),
        (SUITE.replace('["calc"]', '["calc", "calc"]'), "'calc' more than once"),
        (SUITE + ACCEPTED.replace('case = "calc"', 'case = "nope"'), "a miss in 'nope'"),
        (SUITE + ACCEPTED.replace('"float-sum"', '"no-bug"'), "'no-bug', which isn't a bug"),
        (SUITE + SUITE, f"two suites are for claude:{MODEL} diff"),
    ],
    ids=["unknown-case", "case-twice", "miss-of-another-case", "unknown-bug", "suite-twice"],
)
def test_load_suites_checks_cases_and_misses_against_the_cases(
    tmp_path: Path, text: str, named: str
) -> None:
    assert named in rejected(tmp_path, text)


@pytest.mark.parametrize(
    ("data", "named"),
    [
        (b"[[suite]\n", "isn't valid TOML"),
        (b"\xff\xfe[[suite]]\n", "isn't valid UTF-8"),
        (b"a = " + b"[" * 3000, "nested too deeply"),
    ],
    ids=["not-toml", "not-utf8", "too-deep"],
)
def test_load_suites_with_a_file_that_isnt_toml_says_so(
    tmp_path: Path, data: bytes, named: str
) -> None:
    assert named in rejected(tmp_path, data)


def test_load_suites_reads_a_file_of_exactly_64_kb_and_refuses_one_byte_more(
    tmp_path: Path,
) -> None:
    write_case(tmp_path / "cases")
    cases = loaded_cases(tmp_path / "cases")
    padded = SUITE + "#" * (64 * 1024 - len(SUITE) - 1) + "\n"
    assert len(padded.encode("utf-8")) == 64 * 1024

    assert load_suites(suites_file(tmp_path, padded), cases)
    with pytest.raises(EvalInputError, match="64 KB"):
        load_suites(suites_file(tmp_path, padded + "\n"), cases)


def test_load_suites_with_a_missing_file_names_it(tmp_path: Path) -> None:
    with pytest.raises(EvalInputError, match=r"absent\.toml can't be read"):
        load_suites(tmp_path / "absent.toml", {})


# --- Prompt pins ---------------------------------------------------------------------------------


def test_check_prompt_pins_passes_when_each_version_matches_its_pin() -> None:
    assert check_prompt_pins({"v1": "a", "v2": "b"}, {"v1": "a"}) == []


def test_check_prompt_pins_names_a_version_whose_text_changed() -> None:
    [problem] = check_prompt_pins({"v1": "a"}, {"v1": "b"})

    assert problem.startswith("prompt v1: its text changed; give it a new version")
    assert "pin b in PROMPT_FINGERPRINTS" in problem


def test_check_prompt_pins_names_a_version_with_no_pin() -> None:
    [problem] = check_prompt_pins({"v1": "a"}, {"v2": "b"})

    assert problem.startswith("prompt v2: no fingerprint is pinned for it")


def test_the_committed_pins_match_todays_prompts() -> None:
    assert check_prompt_pins(prompt.PROMPT_FINGERPRINTS, prompt.prompt_fingerprints()) == []


# --- Coverage of the policy's models -------------------------------------------------------------


def policy_of(*entries: ModelEntry) -> Policy:
    return Policy("p", "0" * 64, "policy.toml", BUILTIN_POLICY.tools, None, models=entries)


def suite(provider: Any = "claude", model: str = MODEL, mode: Any = "diff", *cases: str) -> Suite:
    return Suite(provider, model, mode, cases or ("a", "b"))


def test_check_coverage_without_listed_models_needs_the_default_model_in_diff_mode() -> None:
    assert check_coverage([suite()], BUILTIN_POLICY) == []
    assert check_coverage([suite(mode="context")], BUILTIN_POLICY) == [
        f"no suite for claude:{MODEL} in diff mode; add one, or leave it out of the policy"
    ]


def test_check_coverage_needs_a_suite_for_each_listed_model_and_mode() -> None:
    policy = policy_of(
        ModelEntry("claude", MODEL, frozenset({"diff", "context"})),
        ModelEntry("openai", "m1", frozenset({"diff"})),
    )

    problems = check_coverage([suite()], policy)

    assert [problem.split(";")[0] for problem in problems] == [
        f"no suite for claude:{MODEL} in context mode",
        "no suite for openai:m1 in diff mode",
    ]


def test_check_coverage_names_the_default_suites_cases_another_suite_lacks() -> None:
    policy = policy_of(ModelEntry("openai", "m1", frozenset({"diff"})))
    suites = [suite("claude", MODEL, "diff", "a", "b", "c"), suite("openai", "m1", "diff", "b")]

    assert check_coverage(suites, policy) == [
        "the suite openai:m1 diff lacks cases of the default model's diff suite: a, c; add them"
    ]


def test_check_coverage_with_no_default_suite_in_a_mode_compares_nothing() -> None:
    policy = policy_of(ModelEntry("openai", "m1", frozenset({"context"})))

    assert check_coverage([suite("openai", "m1", "context", "x")], policy) == []


# --- Each suite case -----------------------------------------------------------------------------


@pytest.fixture
def cases(tmp_path: Path) -> Path:
    return tmp_path / "cases"


def only_case(result: GateResult) -> Any:
    [suite_result] = result.suites
    [case] = suite_result.cases
    return case


def test_run_gate_passes_a_fresh_live_recording_that_catches_every_bug(
    tmp_path: Path, cases: Path
) -> None:
    case_file = write_case(cases)
    write_live_recording(case_file, [run_of(FLOAT_FINDING)] * 2)

    result = gate(cases, suites_file(tmp_path))

    assert result.passed
    assert only_case(result).is_fresh


def test_run_gate_fails_an_unaccepted_miss_naming_the_bug(tmp_path: Path, cases: Path) -> None:
    case_file = write_case(cases)
    write_live_recording(case_file, [run_of(), run_of()])

    case = only_case(gate(cases, suites_file(tmp_path)))

    assert case.problems == (
        "ai bug float-sum was caught in 0 of 2 runs; it needs 2, and it isn't an accepted miss",
    )


def test_run_gate_passes_an_accepted_miss_and_counts_it(tmp_path: Path, cases: Path) -> None:
    case_file = write_case(cases)
    write_live_recording(case_file, [run_of(), run_of()])

    result = gate(cases, suites_file(tmp_path, SUITE + ACCEPTED))

    assert result.passed
    assert result.suites[0].accepted_misses == 1


def test_run_gate_still_fails_an_accepted_miss_case_on_its_other_problems(
    tmp_path: Path, cases: Path
) -> None:
    budget = {"max_findings": 0, "max_severity": "low"}
    case_file = write_case(cases, noise_budget=budget, min_passing_runs=2)
    noisy_run = run_of(finding(line=2, rationale="This name is unclear."))
    write_live_recording(case_file, [noisy_run, noisy_run])

    case = only_case(gate(cases, suites_file(tmp_path, SUITE + ACCEPTED)))

    assert case.accepted_misses == 1
    assert case.problems == ("0 of 2 runs passed; the case needs 2",)


def test_run_gate_fails_a_missing_live_recording_without_scoring(
    tmp_path: Path, cases: Path
) -> None:
    write_case(cases)

    case = only_case(gate(cases, suites_file(tmp_path)))

    assert not case.is_fresh
    assert case.problems[0].startswith(
        "its live recording recordings/live/recording.json is missing"
    )


def test_run_gate_fails_another_models_recording_naming_it(tmp_path: Path, cases: Path) -> None:
    case_file = write_case(cases)
    write_live_recording(case_file, [run_of(FLOAT_FINDING)] * 2, model="claude-other")

    case = only_case(gate(cases, suites_file(tmp_path)))

    assert not case.is_fresh
    assert case.problems[0].startswith("its live recording is by another model, claude-other")


def test_run_gate_fails_a_stale_recording_naming_the_field(tmp_path: Path, cases: Path) -> None:
    case_file = write_case(cases)
    write_live_recording(case_file, [run_of(FLOAT_FINDING)] * 2, diff_sha256="0" * 64)

    case = only_case(gate(cases, suites_file(tmp_path)))

    assert not case.is_fresh
    assert case.problems == (
        "its live recording is stale: its diff_sha256 differs from today's, so it wasn't "
        f"scored; record it with {live_command(Suite('claude', MODEL, 'diff', ()), 'calc')}",
    )


def test_run_gate_advises_recording_a_stale_openai_recording_with_openai(
    tmp_path: Path, cases: Path
) -> None:
    case_file = write_case(cases)
    stale_runs = [run_of(FLOAT_FINDING)] * 2
    write_live_recording(case_file, stale_runs, provider="openai", model="m1", case_sha256="0")
    text = SUITE.replace('"claude"', '"openai"').replace(MODEL, "m1")

    [problem] = only_case(gate(cases, suites_file(tmp_path, text))).problems

    assert "stale: its case_sha256 differs" in problem
    assert problem.endswith(
        "record it with python -m review_bot.evals live --cases DIR --runs N --max-calls M "
        "--max-usd X --max-cost-usd Y --provider openai --model m1 --price-input-usd-per-mtok I "
        "--price-output-usd-per-mtok O --mode diff --case calc"
    )
    assert "--provider claude" not in problem


@pytest.mark.parametrize("provider", ["claude", "openai"])
def test_live_command_as_printed_parses_with_every_required_flag(
    provider: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every flag parses: the run gets as far as reading --cases, which doesn't exist."""
    argv = live_command(Suite(provider, "m1", "context", ("calc",)), "calc").split()[3:]
    absent = str(tmp_path / "absent")
    filled = {"DIR": absent, "N": "1", "M": "1", "X": "1", "Y": "0.10", "I": "1", "O": "1"}

    exit_code = evals_main([filled.get(word, word) for word in argv])

    assert exit_code == 2
    assert f"--cases {absent} isn't a directory" in capsys.readouterr().err


def test_run_gate_reads_another_providers_recordings_from_its_own_directory(
    tmp_path: Path, cases: Path
) -> None:
    case_file = write_case(cases)
    write_live_recording(case_file, [run_of(FLOAT_FINDING)] * 2, provider="openai", model="m1")
    text = SUITE + SUITE.replace('"claude"', '"openai"').replace(MODEL, "m1")
    write_live(case_file, recording(case_file, [run_of(FLOAT_FINDING)] * 2, kind="live"))

    result = gate(cases, suites_file(tmp_path, text))

    assert result.passed, render_gate_text(result)


def test_run_gate_fails_on_a_changed_prompt_even_if_every_suite_passes(
    tmp_path: Path, cases: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_file = write_case(cases)
    write_live_recording(case_file, [run_of(FLOAT_FINDING)] * 2)
    monkeypatch.setitem(prompt.PROMPT_FINGERPRINTS, prompt.CONTEXT_PROMPT_VERSION, "0" * 64)

    result = gate(cases, suites_file(tmp_path))

    assert all(suite.passed for suite in result.suites)
    assert not result.passed
    assert result.prompt_problems[0].startswith(f"prompt {prompt.CONTEXT_PROMPT_VERSION}:")


# --- The report ----------------------------------------------------------------------------------


def test_render_gate_text_has_a_line_per_suite_its_failures_and_the_verdict(
    tmp_path: Path, cases: Path
) -> None:
    write_case(cases)

    text = render_gate_text(gate(cases, suites_file(tmp_path)))

    assert text.splitlines() == [
        f"suite claude:{MODEL} diff: cases 1, fresh 0, passed 0, accepted misses 0, fail",
        "  case calc: its live recording recordings/live/recording.json is missing; record it "
        "with python -m review_bot.evals live --cases DIR --runs N --max-calls M --max-usd X "
        f"--max-cost-usd Y --provider claude --model {MODEL} --mode diff --case calc",
        "verdict: fail",
    ]


def test_render_gate_json_is_eval_gate_v1(tmp_path: Path, cases: Path) -> None:
    case_file = write_case(cases)
    write_live_recording(case_file, [run_of(), run_of()])

    document = json.loads(render_gate_json(gate(cases, suites_file(tmp_path, SUITE + ACCEPTED))))

    assert document == {
        "schema": "eval-gate/v1",
        "prompt_problems": [],
        "suites": [
            {
                "provider": "claude",
                "model": MODEL,
                "mode": "diff",
                "cases": 1,
                "fresh": 1,
                "passed": 1,
                "accepted_misses": 1,
                "verdict": "pass",
                "failures": [],
            }
        ],
        "coverage_problems": [],
        "verdict": "pass",
    }


def test_render_gate_text_escapes_what_files_hold() -> None:
    result = GateResult((), ("no suite for x\x1b in diff mode",), ())

    assert render_gate_text(result).splitlines()[0] == "no suite for x\\x1b in diff mode"
