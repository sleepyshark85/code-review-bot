"""Unit tests for `live`: recording the model's runs within a budget, never by accident.

Every call goes through `ClaudeAgentProvider` over a scripted `sdk_call`, so nothing reaches
the real SDK. Each test runs in its own working directory, which receives the live audit log.
"""

import json
import logging
import sys
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Final

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage
from eval_cases import CALC_DIFF, CALC_FILE, finding, recording, run_of, write_case, write_live

from review_bot.errors import ErrorCategory
from review_bot.evals import main
from review_bot.evals.formats import load_case, load_recording
from review_bot.evals.metrics import live_metrics
from review_bot.evals.render import LIVE_NOTICE
from review_bot.evals.score import CaseEvidence, RunFindings, score_case
from review_bot.findings import Finding
from review_bot.secret_scan import scan_secrets

# Assembled, so the repository's secret scanning sees no literal credential.
CREDENTIAL: Final = "q7" + "Lm" + "Zx92" + "Pw4R" + "tK8v"
LIVE_AUDIT: Final = Path(".review-bot") / "eval-live-audit.jsonl"


def answer(*findings: dict[str, object]) -> str:
    return json.dumps({"findings": list(findings)})


class ScriptedSdk:
    """Answers the n-th call with the n-th scripted result text, or raises it if it's an error."""

    def __init__(self, *answers: str | BaseException) -> None:
        self.answers = answers
        self.options: list[ClaudeAgentOptions] = []

    async def __call__(
        self, _prompt: str, options: ClaudeAgentOptions
    ) -> AsyncGenerator[ResultMessage, None]:
        self.options.append(options)
        scripted = self.answers[(len(self.options) - 1) % len(self.answers)]
        if isinstance(scripted, BaseException):
            raise scripted
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            result=scripted,
            usage={"input_tokens": 10, "output_tokens": 5},
        )


@pytest.fixture(autouse=True)
def outside_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run outside CI, in a working directory of the test's own."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.chdir(tmp_path)


# Covers every test's calls; live needs both amounts in either mode (P4-S1).
BUDGET = ("--max-usd", "100", "--max-cost-usd", "1")


def live(
    capsys: pytest.CaptureFixture[str], sdk: ScriptedSdk, *args: str | Path
) -> tuple[int, str, str]:
    exit_code = main(["live", *BUDGET, *map(str, args)], sdk_call=sdk)
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def test_live_records_every_run_of_every_case_and_reports_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = tmp_path / "cases"
    first, second = write_case(cases, "calc"), write_case(cases, "calc-2")
    sdk = ScriptedSdk(answer(finding()))

    exit_code, out, _ = live(capsys, sdk, "--cases", cases, "--runs", 2, "--max-calls", 4)

    assert len(sdk.options) == 4
    for case_file in (first, second):
        recording = load_recording(case_file.parent / "recordings" / "live" / "recording.json")
        assert recording.kind == "live"
        assert recording.model == "claude-opus-5-5"
        assert [len(run.findings or ()) for run in recording.runs] == [1, 1]
        assert recording.runs[0].usage.input_tokens == 10
    assert exit_code == 0
    assert out.splitlines()[0] == LIVE_NOTICE
    assert "  findings per run: 1, 1" in out
    assert "  drift: swallowed credited on line 4\n  drift: float-sum credited on line 3\n" in out
    assert "  fingerprint recurrence: 1 of 1 fingerprint kept in at least 2 runs" in out
    assert (
        "total: 4 calls, failed 0 of 4 (invalid_output 0, refusal 0); tokens: input 40, output 20"
    ) in out
    assert out.endswith("result: pass\nspent=$4 of --max-usd $100\n")


def test_live_result_matches_replay_of_the_written_recording(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_case(tmp_path / "cases")
    sdk = ScriptedSdk(answer(finding()), answer())

    live_exit, live_out, _ = live(
        capsys, sdk, "--cases", tmp_path / "cases", "--runs", 2, "--max-calls", 2
    )
    replay_exit = main(["replay", "--cases", str(tmp_path / "cases")])
    replay_out = capsys.readouterr().out

    assert live_exit == replay_exit == 1
    row = next(line for line in live_out.splitlines() if line.startswith("  float-sum:"))
    assert row in replay_out.splitlines()


def test_live_records_a_failed_run_and_carries_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = write_case(tmp_path / "cases")
    sdk = ScriptedSdk(answer(finding()), "not json", answer(finding()))

    _, out, err = live(capsys, sdk, "--cases", tmp_path / "cases", "--runs", 3, "--max-calls", 3)

    assert err == (
        "live: case calc run 1/3 ... ok\n"
        "live: case calc run 2/3 ... failed (invalid_output)\n"
        "live: case calc run 3/3 ... ok\n"
    )
    assert "live: case" not in out
    runs = load_recording(case_file.parent / "recordings" / "live" / "recording.json").runs
    assert [run.error_category for run in runs] == [None, ErrorCategory.INVALID_OUTPUT, None]
    assert runs[1].findings is None
    assert "  findings per run: 1, no review, 1" in out
    assert "  failed runs: 1 of 3 (invalid_output 1, refusal 0)" in out
    assert "total: 3 calls, failed 1 of 3 (invalid_output 1, refusal 0)" in out


@pytest.mark.parametrize(
    "rationale",
    [
        f'Hard-coded password = "{CREDENTIAL}" here.',
        # Apart, the scan misses it; in the file's JSON the line feed joins it into one line.
        f"Hard-coded password = '\n{CREDENTIAL}' here.",
    ],
)
def test_live_writes_findings_with_secret_values_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rationale: str
) -> None:
    case_file = write_case(tmp_path / "cases")
    sdk = ScriptedSdk(answer(finding(rationale=rationale)))

    live(capsys, sdk, "--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 1)

    path = case_file.parent / "recordings" / "live" / "recording.json"
    text = path.read_text(encoding="utf-8")
    assert CREDENTIAL not in text
    assert "[REDACTED]" in text
    assert scan_secrets(text) == ()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--runs", "2"], "live needs --max-calls: this run would make 2 calls (1 case x 2 runs)"),
        (["--runs", "2", "--max-calls", "1"], "would make 2 calls (1 case x 2 runs), more than"),
        (["--max-calls", "4"], "the following arguments are required: --runs"),
        (["--runs", "0", "--max-calls", "4"], "'0' isn't a whole number of at least 1"),
    ],
)
def test_live_without_a_sufficient_budget_exits_2_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], args: list[str], expected: str
) -> None:
    write_case(tmp_path / "cases")
    sdk = ScriptedSdk(answer())

    exit_code, out, err = live(capsys, sdk, "--cases", tmp_path / "cases", *args)

    assert (exit_code, out, sdk.options) == (2, "", [])
    assert expected in err
    assert not LIVE_AUDIT.exists()


@pytest.mark.parametrize(
    ("event", "flags"),
    [
        ("pull_request", ["--allow-dispatch"]),
        ("push", ["--allow-dispatch"]),
        ("schedule", ["--allow-dispatch"]),
        (None, ["--allow-dispatch"]),
        ("workflow_dispatch", []),
    ],
)
def test_live_in_ci_exits_2_before_any_call_unless_a_dispatch_given_allow_dispatch(
    event: str | None,
    flags: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ci_run: Callable[[str | None], None],
) -> None:
    write_case(tmp_path / "cases")
    ci_run(event)
    sdk = ScriptedSdk(answer())

    exit_code, _, err = live(
        capsys, sdk, "--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 1, *flags
    )

    assert (exit_code, sdk.options) == (2, [])
    assert "live runs in CI only from a workflow_dispatch run given --allow-dispatch" in err
    assert not LIVE_AUDIT.exists()


def test_live_in_a_ci_workflow_dispatch_run_given_allow_dispatch_calls_the_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], ci_run: Callable[[str | None], None]
) -> None:
    write_case(tmp_path / "cases")
    ci_run("workflow_dispatch")
    sdk = ScriptedSdk(answer(finding()))

    _, _, err = live(
        capsys,
        sdk,
        *("--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 1, "--allow-dispatch"),
    )

    assert len(sdk.options) == 1
    assert err == "live: case calc run 1/1 ... ok\n"


def test_live_outside_ci_runs_the_same_with_allow_dispatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_case(tmp_path / "cases")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    sdk = ScriptedSdk(answer(finding()))

    _, _, err = live(
        capsys,
        sdk,
        *("--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 1, "--allow-dispatch"),
    )

    assert len(sdk.options) == 1
    assert err == "live: case calc run 1/1 ... ok\n"


def test_live_with_a_secret_in_a_case_diff_exits_2_naming_it_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_case(tmp_path / "cases", "calc")
    leaky = CALC_DIFF.replace("+        pass", f'+        password = "{CREDENTIAL}"')
    write_case(tmp_path / "cases", "leaky", diff=leaky)
    sdk = ScriptedSdk(answer())

    exit_code, _, err = live(
        capsys, sdk, "--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 2
    )

    assert (exit_code, sdk.options) == (2, [])
    assert "leaky" in err
    assert "possible secret" in err


def test_live_makes_one_attempt_per_call_and_audits_each_call_in_its_own_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_case(tmp_path / "cases")
    monkeypatch.setenv("REVIEW_BOT_MAX_RETRIES", "3")
    elsewhere = tmp_path / "elsewhere.jsonl"
    monkeypatch.setenv("REVIEW_BOT_AUDIT_LOG", str(elsewhere))
    sdk = ScriptedSdk(answer())

    live(capsys, sdk, "--cases", tmp_path / "cases", "--runs", 2, "--max-calls", 2)

    assert [options.env["CLAUDE_CODE_MAX_RETRIES"] for options in sdk.options] == ["0", "0"]
    records = [json.loads(line) for line in LIVE_AUDIT.read_text(encoding="ascii").splitlines()]
    assert len({record["run_id"] for record in records}) == 2
    assert [record["event"] for record in records].count("called") == 2
    assert (LIVE_AUDIT.parent / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert not elsewhere.exists()


def test_live_interrupted_during_a_case_leaves_its_old_recording_intact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = write_case(tmp_path / "cases")
    old = write_live(case_file, {"old": "recording"})
    sdk = ScriptedSdk(answer(), KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        live(capsys, sdk, "--cases", tmp_path / "cases", "--runs", 2, "--max-calls", 2)

    assert json.loads(old.read_text(encoding="utf-8")) == {"old": "recording"}
    assert [path.name for path in old.parent.iterdir()] == ["recording.json"]


def test_replay_never_uses_the_sdk_call(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_case(tmp_path / "cases")
    sdk = ScriptedSdk(answer())

    exit_code = main(["replay", "--cases", str(tmp_path / "cases")], sdk_call=sdk)

    assert (exit_code, sdk.options) == (0, [])
    assert not LIVE_AUDIT.exists()
    capsys.readouterr()


def test_live_metrics_count_drops_tags_dedup_and_recurring_fingerprints() -> None:
    case_file = write_case(Path("cases"))
    hit = Finding.model_validate(finding(rule="swallowed-exception"))
    stray = Finding.model_validate(finding(line=2, rationale="Odd name."))
    runs = (
        RunFindings((hit, stray), (hit, hit), (stray,), (hit,), fingerprints=("a",)),
        RunFindings((hit,), (hit,), (), (hit,), fingerprints=("a", "b")),
        RunFindings((), (), (), (), error=ErrorCategory.REFUSAL),
    )
    usage = {"input_tokens": 7, "output_tokens": 3}
    recorded = recording(case_file, [run_of() | {"usage": usage}] * 3, kind="live")
    score = score_case(load_case(case_file).case, CaseEvidence((), runs))

    metrics = live_metrics(score, load_recording(write_live(case_file, recorded)))

    assert metrics.findings_per_run == (2, 1, None)
    assert (metrics.returned, metrics.dropped, metrics.rule_tagged) == (3, 1, 2)
    assert metrics.deduped == 1
    assert (metrics.recurring_fingerprints, metrics.fingerprints) == (1, 2)
    assert metrics.failures == {ErrorCategory.REFUSAL: 1}
    assert dict(metrics.lines_by_bug) == {"swallowed": (), "float-sum": (3,)}
    assert (metrics.input_tokens, metrics.output_tokens) == (21, 9)


def test_live_with_an_invalid_setting_exits_2_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_case(tmp_path / "cases")
    monkeypatch.setenv("REVIEW_BOT_DEADLINE_S", "never")
    sdk = ScriptedSdk(answer())

    exit_code, _, err = live(
        capsys, sdk, "--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 1
    )

    assert (exit_code, sdk.options) == (2, [])
    assert "REVIEW_BOT_DEADLINE_S" in err


def test_live_with_an_unwritable_audit_log_exits_2_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_case(tmp_path / "cases")
    Path(".review-bot").write_text("a file, not a directory", encoding="utf-8")
    sdk = ScriptedSdk(answer())

    exit_code, _, err = live(
        capsys, sdk, "--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 1
    )

    assert (exit_code, sdk.options) == (2, [])
    assert "live can't write its audit log" in err


def test_live_with_an_unwritable_recording_exits_2_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = write_case(tmp_path / "cases")
    (case_file.parent / "recordings").write_text("not a directory", encoding="utf-8")
    sdk = ScriptedSdk(answer())

    exit_code, _, err = live(
        capsys, sdk, "--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 1
    )

    assert exit_code == 2
    assert "recording.json can't be written" in err


def test_live_never_writes_a_recording_that_would_hold_a_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A case id is the one string in a recording that neither the model nor the code chose.
    aws_key = "AK" + "IA" + "ABCDEFGH" + "23456789"
    case_file = write_case(tmp_path / "cases", aws_key)
    sdk = ScriptedSdk(answer())

    exit_code, _, err = live(
        capsys, sdk, "--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 1
    )

    assert exit_code == 2
    assert "possible secret that redaction missed" in err
    assert not (case_file.parent / "recordings").exists()


def test_live_with_every_file_skipped_exits_2_naming_the_case_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_case(tmp_path / "cases", "calc")
    docs_only = CALC_DIFF.replace(CALC_FILE, "docs/notes.md")
    write_case(
        tmp_path / "cases",
        "docs",
        bugs=(),
        diff=docs_only,
        noise_budget={"max_findings": 0, "max_severity": "low"},
        min_passing_runs=1,
    )
    sdk = ScriptedSdk(answer())

    exit_code, out, err = live(
        capsys, sdk, "--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 2
    )

    assert (exit_code, out, sdk.options) == (2, "", [])
    assert "case docs: nothing to review: every file is skipped" in err
    assert not (tmp_path / "cases" / "calc" / "recordings").exists()


class _RaisesWhenFreed:
    def __del__(self) -> None:
        raise RuntimeError("SENTINEL")


def test_live_discards_unraisable_reports_once_its_calls_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_case(tmp_path / "cases")
    reports: list[object] = []
    sys.unraisablehook = reports.append
    sdk = ScriptedSdk(answer())

    live(capsys, sdk, "--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 1)
    _RaisesWhenFreed()

    assert reports == []


def test_live_refused_before_any_call_leaves_unraisable_reports_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_case(tmp_path / "cases")
    hook = sys.unraisablehook

    live(capsys, ScriptedSdk(answer()), "--cases", tmp_path / "cases", "--runs", 2)

    assert sys.unraisablehook is hook


class LoggingSdk(ScriptedSdk):
    """Logs an error record holding raw CLI text before it answers, as the SDK can."""

    async def __call__(
        self, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncGenerator[ResultMessage, None]:
        logging.getLogger("claude_agent_sdk").error("RAW-CLI-OUTPUT")
        async for message in super().__call__(prompt, options):
            yield message


def test_live_keeps_library_log_records_off_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_case(tmp_path / "cases")
    # pytest's own root handlers would hide ``logging.lastResort``, which is what a real run hits.
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    sdk = LoggingSdk(answer())

    exit_code, _, err = live(
        capsys, sdk, "--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 1
    )

    assert (exit_code, len(sdk.options)) == (1, 1)
    assert "RAW-CLI-OUTPUT" not in err


SENTINEL: Final = "RAW-CLI-OUTPUT-SENTINEL"


def test_live_records_an_unexpected_exception_as_an_internal_failure_and_carries_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = write_case(tmp_path / "cases")
    sdk = ScriptedSdk(answer(finding()), RuntimeError(SENTINEL), answer(finding()))

    exit_code, out, err = live(
        capsys, sdk, "--cases", tmp_path / "cases", "--runs", 3, "--max-calls", 3
    )

    path = case_file.parent / "recordings" / "live" / "recording.json"
    runs = load_recording(path).runs
    assert [run.error_category for run in runs] == [None, "internal", None]
    assert "live: case calc run 2/3 ... failed (internal)\n" in err
    assert "  failed runs: 1 of 3 (invalid_output 0, refusal 0, internal 1)" in out
    failed = [json.loads(line) for line in LIVE_AUDIT.read_text(encoding="ascii").splitlines()]
    assert [r["data"] for r in failed if r["event"] == "failed"] == [
        {"category": "internal", "error": "RuntimeError"}
    ]
    for text in (out, err, path.read_text(encoding="utf-8"), LIVE_AUDIT.read_text("ascii")):
        assert SENTINEL not in text
    assert exit_code == 0
    assert main(["replay", "--cases", str(tmp_path / "cases")]) == 0
    assert "  no review: run 2 (internal)" in capsys.readouterr().out


def test_main_turns_any_other_exception_into_one_fixed_line_and_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    case_file = write_case(tmp_path / "cases")

    async def broken_scoring(*_args: object) -> None:
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr("review_bot.evals.live.score_recording", broken_scoring)

    exit_code, out, err = live(
        capsys,
        ScriptedSdk(answer()),
        "--cases",
        tmp_path / "cases",
        "--runs",
        1,
        "--max-calls",
        1,
    )

    assert (exit_code, out) == (2, "")
    assert err.splitlines()[-1] == (
        "review-bot evals: error: internal error. Its text isn't shown, as it can hold model or "
        "CLI output."
    )
    assert SENTINEL not in err
    assert (case_file.parent / "recordings" / "live" / "recording.json").is_file()


def test_live_reads_no_variable_for_a_setting_it_fixes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_case(tmp_path / "cases")
    for name, value in [
        ("REVIEW_BOT_FORMAT", "bogus"),
        ("REVIEW_BOT_THRESHOLD", "bogus"),
        ("REVIEW_BOT_MAX_DIFF_BYTES", "1"),
        ("REVIEW_BOT_PROVIDER", "mock"),
    ]:
        monkeypatch.setenv(name, value)
    sdk = ScriptedSdk(answer())

    exit_code, _, err = live(
        capsys, sdk, "--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 1
    )

    assert len(sdk.options) == 1, err
    assert exit_code == 1


def test_live_writes_a_suggestion_with_secret_values_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = write_case(tmp_path / "cases")
    suggestion = f'password = "{CREDENTIAL}"'
    sdk = ScriptedSdk(answer(finding(suggestion=suggestion)))

    live(capsys, sdk, "--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 1)

    recording = load_recording(case_file.parent / "recordings" / "live" / "recording.json")
    [recorded] = recording.runs[0].findings or ()
    assert recorded.suggestion == 'password = "[REDACTED]"'
