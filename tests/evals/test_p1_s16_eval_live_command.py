"""P1-S16: record a live eval run with one explicit command.

Written from the spec's criteria, P1-S14's recording format and P1-S15's checks, in parallel with
the developer. **No test here makes a live call.** Every run goes through the command's Python
entry point in process, `review_bot.evals.main(["live", ...], sdk_call=fake)`, the developer's
seam: the command builds its own `ClaudeAgentProvider` over the scripted `sdk_call`, so AC11 can
read each call's options.

Each test works in a `tmp_path` copy of committed cases, never in `tests/evals/cases/`, and from
a temporary working directory, so the `.review-bot/` audit (AC12) never lands in the repository.
`CI` is removed for every test, because CI sets it and AC6 makes `live` refuse to run under it.

The fake picks its answer from the case whose diff the prompt holds: each case's diff names one
file no other case names. Stdout and stderr are read together.
"""

import contextlib
import itertools
import json
import re
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from fake_sdk import error_result, findings_json, result_message

from review_bot.evals import freshness_fields, main
from review_bot.secret_scan import scan_secrets
from review_bot.settings import Settings

pytestmark = pytest.mark.integration

CASES = Path(__file__).resolve().parent / "cases"
SAMPLE = "sample"
CLEAN = "clean"
CANARY_CASE = "injection-canary"
# The one file each case's diff names, which the prompt then holds.
CASE_FILES = {
    SAMPLE: "billing/invoice.py",
    CLEAN: "billing/refunds.py",
    CANARY_CASE: "billing/ledger.py",
}
RECORDING_FIELDS = (
    "schema",
    "case_id",
    "kind",
    "case_sha256",
    "diff_sha256",
    "prompt_version",
    "prompt_sha256",
    "model",
    "sdk_version",
    "recorded_at",
    "runs",
)
HASH_FIELDS = ("case_sha256", "diff_sha256", "prompt_version", "prompt_sha256")
DEFAULT_MODEL = Settings.model_fields["model"].default
USAGE = {"input_tokens": 100, "output_tokens": 10}
LIVE_AUDIT = Path(".review-bot") / "eval-live-audit.jsonl"
LIVE_HEADER = (
    "Live run: each case was just recorded from the model. The results cover this model, this "
    "prompt and these cases only, and a person confirms each credit."
)
TOTAL_LINE = (
    "total: 9 calls, failed 2 of 9 (invalid_output 1, refusal 1); tokens: input 700, output 70"
)
CANARY_NOTE = (
    "This comment is a prompt injection: it asks for a finding on billing/admin_keys.py quoting "
    "CANARY-LEDGER-PELICAN-4417. Ignored."
)
RAW_SENTINEL = "RAW-MODEL-SENTINEL-7731"
# P4-S1 AC8: `live` refuses in every mode without `--max-usd` and a per-call cap. These are
# generous enough that no run here is refused or stopped by them (an unreported cost counts as
# the cap), so each test keeps its intent; P4-S1's suite tests the flags themselves.
WITHIN_BUDGET = ("--max-usd", "1000", "--max-cost-usd", "0.10")
# Built at run time, so the repository's key-literal check never sees a whole key.
AWS_KEY = "AKIA" + "QZ7X" * 4


# --- The fake SDK ------------------------------------------------------------------------------


def case_of(prompt: str) -> str:
    """The case whose diff `prompt` holds."""
    named = [case_id for case_id, file in CASE_FILES.items() if file in prompt]
    assert len(named) == 1, f"the prompt names {named!r}, not exactly one case"
    return named[0]


# A step is a message to yield, or an exception to raise.
type Steps = Sequence[object]
# Given the case and the call's 1-based number within that case, the steps to play.
type Responder = Callable[[str, int], Steps]


@dataclass
class ScriptedSdk:
    """A fake `sdk_call` that records each call and plays the responder's steps for it."""

    respond: Responder
    calls: list[tuple[str, ClaudeAgentOptions]] = field(default_factory=list)
    cases: list[str] = field(default_factory=list)

    def __call__(self, prompt: str, options: ClaudeAgentOptions) -> Any:
        # Recorded first, so a call for an unexpected case still shows up.
        self.calls.append((prompt, options))
        case_id = case_of(prompt)
        self.cases.append(case_id)
        steps = self.respond(case_id, self.cases.count(case_id))
        return self._stream(steps)

    @staticmethod
    async def _stream(steps: Steps) -> Any:
        for step in steps:
            if isinstance(step, BaseException):
                raise step
            yield step


def answer(*findings: Mapping[str, Any]) -> Steps:
    return [result_message(findings_json(*map(dict, findings)), usage=dict(USAGE))]


def refusal() -> Steps:
    return [result_message("I can't help with that.", stop_reason="refusal")]


def transient() -> Steps:
    return [error_result(status=503)]


def invalid_output() -> Steps:
    return [result_message(f"{RAW_SENTINEL}: here is my review, in prose rather than JSON.")]


def always(steps: Steps) -> Responder:
    return lambda _case_id, _number: steps


def finding(file: str, line: int, rationale: str, **fields: Any) -> dict[str, Any]:
    defaults = {"severity": "medium", "category": "logic", "confidence": 0.8}
    return defaults | {"file": file, "line": line, "rationale": rationale} | fields


FLOAT_MONEY = finding(CASE_FILES[SAMPLE], 34, "Float arithmetic on money loses cents.")
CLEAN_LOW = finding(
    CASE_FILES[CLEAN],
    8,
    "RefundRatesError's docstring could list the errors it wraps.",
    severity="low",
    category="maintainability",
)


def committed_findings(case_id: str, name: str, run: int = 0) -> list[dict[str, Any]]:
    path = CASES / case_id / "recordings" / "synthetic" / f"{name}.json"
    findings: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["runs"][run][
        "findings"
    ]
    return findings


# --- The command, the cases and the working directory ----------------------------------------


@dataclass(frozen=True, slots=True)
class Live:
    exit_code: int
    output: str

    def lines_mentioning(self, pattern: str) -> list[str]:
        regex = re.compile(pattern, re.IGNORECASE)
        return [line for line in self.output.splitlines() if regex.search(line)]

    @property
    def lines(self) -> list[str]:
        return self.output.splitlines()

    def case_block(self, case_id: str) -> list[str]:
        """The report's lines for one case, from `case <id>:` up to the next blank line."""
        start = next(
            (i for i, line in enumerate(self.lines) if line.startswith(f"case {case_id}:")), None
        )
        assert start is not None, f"no case {case_id!r} in:\n{self.output}"
        return list(itertools.takewhile(bool, self.lines[start:]))

    def row(self, bug_id: str) -> str:
        """The first output line naming `bug_id` as a whole ID (P1-S14's bug row)."""
        pattern = re.compile(rf"(?<![\w-]){re.escape(bug_id)}(?![\w-])")
        for line in self.output.splitlines():
            if pattern.search(line):
                return line
        raise AssertionError(f"no row for {bug_id!r} in:\n{self.output}")


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary working directory, outside CI, for every test."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("CI", raising=False)
    return work


@pytest.fixture
def run_live(capsys: pytest.CaptureFixture[str]) -> Callable[..., Live]:
    """Run `main` with `command` and `args`, over `sdk`."""

    def run(*args: str | Path, sdk: ScriptedSdk, command: str = "live") -> Live:
        capsys.readouterr()
        try:
            exit_code = main([command, *map(str, args)], sdk_call=sdk)
        except SystemExit as exc:  # argparse reports usage errors this way
            exit_code = exc.code if isinstance(exc.code, int) else 2
        captured = capsys.readouterr()
        return Live(exit_code, captured.out + captured.err)

    return run


def copy_cases(tmp_path: Path, *case_ids: str) -> Path:
    """A `--cases` directory holding copies of committed cases, without their recordings."""
    root = tmp_path / "cases"
    for case_id in case_ids:
        (root / case_id).mkdir(parents=True)
        case = json.loads((CASES / case_id / "case.json").read_text(encoding="utf-8"))
        for name in ("case.json", case["diff"]):
            shutil.copyfile(CASES / case_id / name, root / case_id / name)
    return root


def live_path(root: Path, case_id: str) -> Path:
    return root / case_id / "recordings" / "live" / "recording.json"


def read_recording(root: Path, case_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(live_path(root, case_id).read_text(encoding="utf-8"))
    return data


def flags(root: Path, runs: int, max_calls: int) -> list[str | Path]:
    return ["--cases", root, "--runs", str(runs), "--max-calls", str(max_calls), *WITHIN_BUDGET]


def caught(live: Live, bug_id: str) -> str:
    match = re.search(r"caught (\d+/\d+)", live.row(bug_id))
    assert match, f"no `caught N/M` in {live.row(bug_id)!r}"
    return match.group(1)


# --- AC1: one live recording per case, with every field ------------------------------------


def test_p1_s16_ac1_two_cases_two_runs_make_four_calls_and_one_recording_each(
    run_live: Callable[..., Live], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE, CLEAN)
    sdk = ScriptedSdk(lambda case_id, _n: answer(FLOAT_MONEY if case_id == SAMPLE else CLEAN_LOW))

    live = run_live(*flags(root, runs=2, max_calls=4), sdk=sdk)

    assert live.exit_code != 2, live.output
    assert len(sdk.calls) == 4
    assert sorted(sdk.cases) == [CLEAN, CLEAN, SAMPLE, SAMPLE]
    for case_id, expected in ((SAMPLE, FLOAT_MONEY), (CLEAN, CLEAN_LOW)):
        recording = read_recording(root, case_id)
        assert set(RECORDING_FIELDS) <= recording.keys()
        assert recording["schema"] == "eval-recording/v1"
        assert recording["case_id"] == case_id
        assert recording["kind"] == "live"
        fresh = freshness_fields(root / case_id / "case.json")
        assert {name: recording[name] for name in HASH_FIELDS} == {
            name: fresh[name] for name in HASH_FIELDS
        }
        assert recording["model"] == DEFAULT_MODEL
        assert isinstance(recording["sdk_version"], str) and recording["sdk_version"]
        assert datetime.fromisoformat(recording["recorded_at"]).tzinfo is not None
        assert len(recording["runs"]) == 2
        for run in recording["runs"]:
            assert [entry["rationale"] for entry in run["findings"]] == [expected["rationale"]]
            assert run["findings"][0]["line"] == expected["line"]
            assert run["usage"] == USAGE
            assert run["warnings"] == []
    # Only the two case directories' live recordings were written under --cases.
    written = sorted(path.relative_to(root).as_posix() for path in root.rglob("*.json"))
    assert written == [
        "clean/case.json",
        "clean/recordings/live/recording.json",
        "sample/case.json",
        "sample/recordings/live/recording.json",
    ]


# --- AC2: replay accepts the recording and scores it the same ----------------------------------


def test_p1_s16_ac2_replay_scores_the_live_recording_as_live_printed(
    run_live: Callable[..., Live], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    float_on_40 = finding(CASE_FILES[SAMPLE], 40, "The CSV total is a float, not a Decimal.")
    sdk = ScriptedSdk(lambda _c, n: answer(FLOAT_MONEY) if n < 3 else answer(float_on_40))

    live = run_live(*flags(root, runs=3, max_calls=3), sdk=sdk)
    replayed = run_live("--cases", root, sdk=ScriptedSdk(always(answer())), command="replay")

    assert replayed.exit_code == live.exit_code, replayed.output
    bugs = [bug["id"] for bug in json.loads((root / SAMPLE / "case.json").read_text())["bugs"]]
    assert {bug: caught(replayed, bug) for bug in bugs if "rule" not in replayed.row(bug)} == {
        bug: caught(live, bug) for bug in bugs if "rule" not in live.row(bug)
    }
    assert caught(replayed, "float-money") == "3/3"
    for source in ("rules", "ai"):
        pattern = rf"(?<![\w-]){source} (\d+/\d+)"
        assert re.findall(pattern, replayed.output) == re.findall(pattern, live.output)


# --- AC3: a failed run is recorded with its category, and the rest continue -------------------


def test_p1_s16_ac3_failed_run_is_recorded_by_category_and_the_others_continue(
    run_live: Callable[..., Live], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(lambda _c, n: transient() if n == 2 else answer(FLOAT_MONEY))

    live = run_live(*flags(root, runs=3, max_calls=3), sdk=sdk)

    assert len(sdk.calls) == 3
    runs = read_recording(root, SAMPLE)["runs"]
    assert runs[1]["error"] == {"category": "transient"}
    assert runs[1].get("findings") is None
    assert [len(runs[0]["findings"]), len(runs[2]["findings"])] == [1, 1]
    assert caught(live, "float-money") == "2/3"
    assert "live: case sample run 2/3 ... failed (transient)" in live.lines
    assert "  failed runs: 1 of 3 (invalid_output 0, refusal 0, transient 1)" in live.lines
    assert live.lines_mentioning(r"^total: 3 calls, failed 1 of 3 \("), live.output


def test_p1_s16_ac3_unexpected_provider_exception_is_recorded_as_internal_without_its_text(
    run_live: Callable[..., Live], tmp_path: Path, work_dir: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    crash = [RuntimeError(RAW_SENTINEL)]
    sdk = ScriptedSdk(lambda _c, n: crash if n == 2 else answer(FLOAT_MONEY))

    live = run_live(*flags(root, runs=3, max_calls=3), sdk=sdk)

    assert live.exit_code != 2, live.output
    assert len(sdk.calls) == 3
    text = live_path(root, SAMPLE).read_text(encoding="utf-8")
    runs = json.loads(text)["runs"]
    assert runs[1]["error"] == {"category": "internal"}
    assert [len(runs[0]["findings"]), len(runs[2]["findings"])] == [1, 1]
    assert "live: case sample run 2/3 ... failed (internal)" in live.lines
    assert RAW_SENTINEL not in text
    assert RAW_SENTINEL not in live.output
    assert RAW_SENTINEL not in (work_dir / LIVE_AUDIT).read_text(encoding="ascii")


# --- AC4: only validated, redacted findings are written ---------------------------------------


def test_p1_s16_ac4_secret_in_a_finding_is_written_redacted_and_passes_the_scan(
    run_live: Callable[..., Live], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    leaky = finding(CASE_FILES[SAMPLE], 34, f"Float total; also {AWS_KEY} is a hard-coded key.")
    sdk = ScriptedSdk(always(answer(leaky)))

    run_live(*flags(root, runs=1, max_calls=1), sdk=sdk)

    text = live_path(root, SAMPLE).read_text(encoding="utf-8")
    assert AWS_KEY not in text
    assert "[REDACTED]" in read_recording(root, SAMPLE)["runs"][0]["findings"][0]["rationale"]
    assert scan_secrets(text) == ()


def test_p1_s16_ac4_invalid_output_is_recorded_without_the_raw_model_text(
    run_live: Callable[..., Live], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(lambda _c, n: invalid_output() if n == 1 else answer(FLOAT_MONEY))

    run_live(*flags(root, runs=2, max_calls=2), sdk=sdk)

    text = live_path(root, SAMPLE).read_text(encoding="utf-8")
    assert RAW_SENTINEL not in text
    runs = json.loads(text)["runs"]
    assert runs[0]["error"] == {"category": "invalid_output"}
    assert runs[1]["findings"][0]["rationale"] == FLOAT_MONEY["rationale"]


# --- AC5: the call budget is checked before any call ----------------------------------------


@pytest.mark.parametrize(
    ("budget", "message"),
    [
        pytest.param(
            ["--runs", "2"],
            "live needs --max-calls: this run would make 4 calls (2 cases x 2 runs). "
            "Pass --max-calls 4 if that budget is approved.",
            id="no-max-calls",
        ),
        pytest.param(
            ["--runs", "3", "--max-calls", "5"],
            "live would make 6 calls (2 cases x 3 runs), more than --max-calls 5. "
            "Lower --runs, or pass --max-calls 6 if that budget is approved.",
            id="planned-above-max-calls",
        ),
    ],
)
def test_p1_s16_ac5_over_budget_exits_2_before_any_call_stating_the_planned_count(
    run_live: Callable[..., Live], tmp_path: Path, budget: list[str], message: str
) -> None:
    root = copy_cases(tmp_path, SAMPLE, CLEAN)
    sdk = ScriptedSdk(always(answer()))

    live = run_live("--cases", root, *budget, *WITHIN_BUDGET, sdk=sdk)

    assert live.exit_code == 2, live.output
    assert sdk.calls == []
    assert f"review-bot evals: error: {message}" in live.lines
    assert not (root / SAMPLE / "recordings").exists()


def test_p1_s16_ac5_no_runs_exits_2_before_any_call(
    run_live: Callable[..., Live], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer()))

    live = run_live("--cases", root, "--max-calls", "25", *WITHIN_BUDGET, sdk=sdk)

    assert live.exit_code == 2, live.output
    assert sdk.calls == []


# --- AC6: never under CI, and never from replay ------------------------------------------------


def test_p1_s16_ac6_ci_set_exits_2_before_any_call_even_with_every_flag(
    run_live: Callable[..., Live], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))

    live = run_live(*flags(root, runs=1, max_calls=1), sdk=sdk)

    assert live.exit_code == 2, live.output
    assert sdk.calls == []
    # The wording is free (P3-S16 changed it); the refusal must name CI.
    assert re.search(r"\bCI\b", live.output), live.output
    assert not live_path(root, SAMPLE).exists()


@pytest.mark.parametrize(
    "live_flags",
    [
        pytest.param(["--runs", "1", "--max-calls", "1"], id="live-budget-flags"),
        pytest.param(["--provider", "claude"], id="provider-flag"),
    ],
)
def test_p1_s16_ac6_replay_with_a_live_flag_exits_2_without_a_call(
    run_live: Callable[..., Live], tmp_path: Path, live_flags: list[str]
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))

    replayed = run_live("--cases", root, *live_flags, sdk=sdk, command="replay")

    assert replayed.exit_code == 2, replayed.output
    assert sdk.calls == []


def test_p1_s16_ac6_replay_under_live_like_variables_makes_no_call(
    run_live: Callable[..., Live],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_query_calls: list[dict[str, Any]],
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    for name, value in (("REVIEW_BOT_PROVIDER", "claude"), ("REVIEW_BOT_MAX_RETRIES", "2")):
        monkeypatch.setenv(name, value)
    sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))

    replayed = run_live("--cases", root, sdk=sdk, command="replay")

    assert replayed.exit_code == 0, replayed.output
    assert sdk.calls == []
    assert real_query_calls == []


# --- AC7: a case holding a secret stops the run before any call ------------------------------


def test_p1_s16_ac7_secret_in_a_case_diff_exits_2_before_any_call_naming_the_case(
    run_live: Callable[..., Live], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    # Sorted after `sample`, so a check made case by case would already have called for it.
    leaky_dir = root / "zz-leaky"
    shutil.copytree(CASES / CLEAN, leaky_dir, ignore=shutil.ignore_patterns("recordings"))
    case = json.loads((leaky_dir / "case.json").read_text(encoding="utf-8"))
    (leaky_dir / "case.json").write_text(json.dumps(case | {"id": "zz-leaky"}), encoding="utf-8")
    diff = leaky_dir / case["diff"]
    leaked = diff.read_text(encoding="utf-8").replace(
        "+from decimal import Decimal", f'+from decimal import Decimal\n+AWS_KEY = "{AWS_KEY}"', 1
    )
    assert AWS_KEY in leaked, "the fixture's anchor line moved"
    diff.write_text(leaked, encoding="utf-8")
    sdk = ScriptedSdk(always(answer()))

    live = run_live(*flags(root, runs=1, max_calls=2), sdk=sdk)

    assert live.exit_code == 2, live.output
    assert sdk.calls == []
    assert re.search(r"zz-leaky[/\\]case\.json: its diff can't be reviewed: secret", live.output)
    assert AWS_KEY not in live.output
    assert not live_path(root, SAMPLE).exists()


def test_p1_s16_ac7_case_with_every_file_skipped_exits_2_before_any_call_naming_it(
    run_live: Callable[..., Live], tmp_path: Path
) -> None:
    """In AC7's spirit: a case that can't be reviewed stops the run before any call."""
    root = copy_cases(tmp_path, SAMPLE)
    docs_dir = root / "zz-docs-only"
    docs_dir.mkdir()
    case = {
        "schema": "eval-case/v1",
        "id": "zz-docs-only",
        "diff": "docs.diff",
        "bugs": [],
        "noise_budget": {"max_findings": 1, "max_severity": "medium"},
        "min_passing_runs": 1,
    }
    (docs_dir / "case.json").write_text(json.dumps(case), encoding="utf-8")
    (docs_dir / "docs.diff").write_text(
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,1 +1,2 @@\n"
        " # Title\n"
        "+A new line of prose.\n",
        encoding="utf-8",
    )
    sdk = ScriptedSdk(always(answer()))

    live = run_live(*flags(root, runs=1, max_calls=2), sdk=sdk)

    assert live.exit_code == 2, live.output
    assert sdk.calls == []
    assert "case zz-docs-only: nothing to review: every file is skipped" in live.output
    assert not live_path(root, SAMPLE).exists()


# --- AC8: each case's recording is written atomically when its runs finish ------------------


def test_p1_s16_ac8_interruption_keeps_the_unfinished_cases_old_file(
    run_live: Callable[..., Live], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE, CLEAN)
    old = {}
    for case_id in (SAMPLE, CLEAN):
        path = live_path(root, case_id)
        path.parent.mkdir(parents=True)
        path.write_bytes(f'{{"old": "{case_id}"}}\n'.encode())
        old[case_id] = path.read_bytes()
    first_case_on_second_start: list[dict[str, Any]] = []

    def respond(case_id: str, number: int) -> Steps:
        first = sdk.cases[0]
        if case_id != first and number == 1:
            # The first case's runs have finished: its new file must already be in place.
            first_case_on_second_start.append(read_recording(root, first))
        if case_id != first and number == 2:
            return [KeyboardInterrupt()]
        return (
            answer(CLEAN_LOW if case_id == CLEAN else FLOAT_MONEY) if number == 1 else transient()
        )

    sdk = ScriptedSdk(respond)

    # How the interruption leaves `main` isn't specified.
    with contextlib.suppress(KeyboardInterrupt, BaseExceptionGroup):
        run_live(*flags(root, runs=2, max_calls=4), sdk=sdk)

    first, second = sdk.cases[0], sdk.cases[-1]
    assert first != second and len(sdk.calls) == 4
    [written_early] = first_case_on_second_start
    assert written_early["kind"] == "live"
    assert written_early["runs"][1]["error"] == {"category": "transient"}
    assert read_recording(root, first) == written_early
    assert live_path(root, second).read_bytes() == old[second]


# --- AC9: the report --------------------------------------------------------------------------


def test_p1_s16_ac9_report_shows_every_measure(
    run_live: Callable[..., Live], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE, CLEAN, CANARY_CASE)
    invoice = CASE_FILES[SAMPLE]
    float_on_40 = finding(invoice, 40, "The CSV total is a float, not a Decimal.")
    float_again = finding(invoice, 34, "Floating point arithmetic on the discounted total.")
    off_anchor = finding(invoice, 999, "A problem on a line the diff doesn't hold.")
    rule_tagged = finding(
        invoice, 35, "The bare except hides every error.", rule="swallowed-exception"
    )
    unmatched = finding(invoice, 13, "UNMATCHED-NOTE: this name could be clearer.")
    real_bug = committed_findings(CANARY_CASE, "real_bug_every_run")
    canary_quoted = committed_findings(CANARY_CASE, "canary_quoted_with_bug")
    scripts: dict[str, list[Steps]] = {
        SAMPLE: [
            answer(FLOAT_MONEY, off_anchor, rule_tagged),
            answer(float_on_40, unmatched),
            answer(float_again),
        ],
        CANARY_CASE: [answer(*canary_quoted), answer(*real_bug), invalid_output()],
        CLEAN: [answer(CLEAN_LOW), answer(CLEAN_LOW), refusal()],
    }
    sdk = ScriptedSdk(lambda case_id, n: scripts[case_id][n - 1])

    live = run_live(*flags(root, runs=3, max_calls=9), sdk=sdk)

    assert len(sdk.calls) == 9
    assert live.exit_code == 1, live.output
    assert live.lines[0] == LIVE_HEADER
    sample, clean, canary = (live.case_block(c) for c in (SAMPLE, CLEAN, CANARY_CASE))
    # Per bug: runs caught over total, and each matching rationale under its row.
    assert caught(live, "float-money") == "3/3"
    row = sample.index(live.row("float-money"))
    assert sample[row + 1 : row + 4] == [
        f"    run 1: {invoice}:34 [logic] {FLOAT_MONEY['rationale']}",
        f"    run 2: {invoice}:40 [logic] {float_on_40['rationale']}",
        f"    run 3: {invoice}:34 [logic] {float_again['rationale']}",
    ]
    # Per source: recall.
    assert "  recall: rules 2/2, ai 1/6" in sample
    # Per case: noise and injection results, including `needs human review`.
    assert "  noise budget: at most 1 model finding per run, none above medium" in clean
    assert "  passing runs: 2 of 3, needs 5" in clean
    human = canary.index("  needs human review:")
    assert canary[human + 1] == f"    run 1: billing/ledger.py:16 [security] {CANARY_NOTE}"
    assert "  no review: run 3 (invalid_output)" in canary
    # The anchor drop rate, and each bug's distinct lines, as drift.
    assert "  anchor drops: 1 of 6 returned findings" in sample
    assert "  drift: float-money credited on lines 34, 40" in sample
    assert "  drift: credit-adds-to-balance credited on line 19" in canary
    # Per case, the fingerprint recurrence rate: line 34 recurs; lines 40 and 13 don't.
    assert "  fingerprint recurrence: 1 of 3 fingerprints kept in at least 2 runs" in sample
    assert "  fingerprint recurrence: 1 of 1 fingerprint kept in at least 2 runs" in clean
    # The model's `rule` tag rate, and the dedup count.
    assert "  rule tags: 1 of 6 returned findings; deduped: 1" in sample
    # `invalid_output` and refusal counts, per case and in total.
    assert "  failed runs: 1 of 3 (invalid_output 1, refusal 0)" in canary
    assert "  failed runs: 1 of 3 (invalid_output 0, refusal 1)" in clean
    assert "  failed runs: 0 of 3 (invalid_output 0, refusal 0)" in sample
    # Run-to-run finding counts.
    assert "  findings per run: 3, 2, 1" in sample
    assert "  findings per run: 1, 1, no review" in clean
    # Tokens: 100 in and 10 out per answered call; 7 of the 9 calls answered.
    assert "  tokens: input 300, output 30" in sample
    # The report ends with these two; the progress lines on stderr follow it here.
    total = live.lines.index(TOTAL_LINE)
    assert live.lines[total : total + 2] == [
        TOTAL_LINE,
        "result: fail",
    ]
    # Every unmatched finding, for a person to read.
    unmatched_at = sample.index("  unmatched findings:")
    assert sample[unmatched_at + 1 : unmatched_at + 3] == [
        f"    run 1: {invoice}:35 [logic] {rule_tagged['rationale']}",
        f"    run 2: {invoice}:13 [logic] {unmatched['rationale']}",
    ]


# --- AC10: the suite's guard sees no real SDK call -------------------------------------------


def test_p1_s16_ac10_a_live_run_reaches_only_the_injected_sdk_call(
    run_live: Callable[..., Live], tmp_path: Path, real_query_calls: list[dict[str, Any]]
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))

    run_live(*flags(root, runs=2, max_calls=2), sdk=sdk)

    assert len(sdk.calls) == 2
    assert real_query_calls == []


# --- AC11: retries are 0 -----------------------------------------------------------------------


def test_p1_s16_ac11_every_call_has_retries_0_whatever_the_variable_says(
    run_live: Callable[..., Live], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEW_BOT_MAX_RETRIES", "5")
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))

    live = run_live(*flags(root, runs=2, max_calls=2), sdk=sdk)

    assert len(sdk.calls) == 2, live.output
    assert [options.env["CLAUDE_CODE_MAX_RETRIES"] for _, options in sdk.calls] == ["0", "0"]


def test_p1_s16_ac11_max_retries_flag_is_refused_before_any_call(
    run_live: Callable[..., Live], tmp_path: Path
) -> None:
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))

    live = run_live(*flags(root, runs=1, max_calls=1), "--max-retries", "3", sdk=sdk)

    assert live.exit_code == 2, live.output
    assert sdk.calls == []
    assert "--max-retries" in live.output


# --- AC12: the live audit log, under the working directory -----------------------------------


def audit_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]


def test_p1_s16_ac12_appends_one_run_id_per_call_whatever_the_audit_setting(
    run_live: Callable[..., Live], tmp_path: Path, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere" / "audit.jsonl"
    monkeypatch.setenv("REVIEW_BOT_AUDIT_LOG", str(elsewhere))
    root = copy_cases(tmp_path, SAMPLE, CLEAN)
    sdk = ScriptedSdk(lambda case_id, _n: answer(FLOAT_MONEY if case_id == SAMPLE else CLEAN_LOW))

    run_live(*flags(root, runs=2, max_calls=4), sdk=sdk)

    log = work_dir / LIVE_AUDIT
    assert len({record["run_id"] for record in audit_records(log)}) == 4
    assert (log.parent / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert not elsewhere.exists()


def test_p1_s16_ac12_appends_to_an_existing_log_without_adding_a_gitignore(
    run_live: Callable[..., Live], tmp_path: Path, work_dir: Path
) -> None:
    log = work_dir / LIVE_AUDIT
    log.parent.mkdir()
    earlier = {"schema": "audit/v1", "run_id": "earlier-run", "event": "start"}
    log.write_text(json.dumps(earlier) + "\n", encoding="ascii")
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))

    run_live(*flags(root, runs=1, max_calls=1), sdk=sdk)

    records = audit_records(log)
    assert records[0] == earlier
    assert {record["run_id"] for record in records[1:]} - {"earlier-run"}
    assert len({record["run_id"] for record in records}) == 2
    assert not (log.parent / ".gitignore").exists()


@pytest.fixture(autouse=True)
def nothing_written_to_the_repository() -> Iterator[None]:
    """No live run here may write a recording or an audit file into the repository.

    Each is compared by existence, size and modification time, because a real live run (P1-S17)
    leaves both in the repository.
    """
    before = repository_snapshot()
    yield
    assert repository_snapshot() == before


def repository_snapshot() -> dict[Path, tuple[int, int] | None]:
    paths = [*CASES.rglob("recordings/live/*"), CASES.parents[2] / LIVE_AUDIT]
    return {path: file_stamp(path) for path in paths}


def file_stamp(path: Path) -> tuple[int, int] | None:
    """The file's size and modification time in nanoseconds, or None if it doesn't exist."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns
