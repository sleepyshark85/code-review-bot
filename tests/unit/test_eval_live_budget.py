"""Unit tests for `live` with a dollar budget, in context and diff mode (P3-S6).

Every call goes through `ClaudeAgentProvider` over a fake `sdk_call`, which can call the repo
tools through the options' MCP server as the CLI would, so nothing reaches the real SDK.
"""

import json
from collections.abc import AsyncGenerator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage
from eval_cases import finding, recording, run_of, write_case, write_live, write_repo

from review_bot.evals import main
from review_bot.evals.formats import load_recording

LIVE_AUDIT: Final = Path(".review-bot") / "eval-live-audit.jsonl"
NOTES_FILE: Final = "app/notes.py"
# A string only the repo's file holds, so its presence anywhere else means file text leaked.
UNIQUE_TEXT: Final = "zebra-quokka-7731"


class ToolUsingSdk:
    """The n-th call reads the n-th of `reads` through the repo tools in context mode, then ends
    as the n-th scripted result; both lists repeat.

    A result is a cost (None for none reported) for a review that returned findings, or a result
    subtype, such as `error_max_budget_usd`, for one that failed.
    """

    def __init__(
        self, *results: float | str | None, reads: Sequence[Sequence[str]] = ((NOTES_FILE,),)
    ) -> None:
        self.results = results or (0.25,)
        self.reads = reads
        self.options: list[ClaudeAgentOptions] = []

    async def __call__(
        self, _prompt: str, options: ClaudeAgentOptions
    ) -> AsyncGenerator[ResultMessage, None]:
        index = len(self.options)
        self.options.append(options)
        if options.mcp_servers:
            for path in self.reads[index % len(self.reads)]:
                await read_through_server(options, path)
        scripted = self.results[index % len(self.results)]
        failed = isinstance(scripted, str)
        yield ResultMessage(
            subtype=scripted if isinstance(scripted, str) else "success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=failed,
            num_turns=1,
            session_id="session",
            result=None if failed else json.dumps({"findings": [finding()]}),
            usage={"input_tokens": 10, "output_tokens": 5},
            total_cost_usd=None if isinstance(scripted, str) else scripted,
        )


async def read_through_server(options: ClaudeAgentOptions, path: str) -> None:
    """Call the `read` tool through the options' MCP server, as the CLI would."""
    server = options.mcp_servers["repo"]["instance"]
    entry = server.get_request_handler("tools/call")
    await entry.handler(None, entry.params_type(name="read", arguments={"path": path}))


@pytest.fixture(autouse=True)
def outside_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run outside CI, in a working directory of the test's own."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.chdir(tmp_path)


def context_case(cases_dir: Path, case_id: str = "calc") -> Path:
    """A case whose `repo` is the diff's base, plus a notes file holding `UNIQUE_TEXT`."""
    case_file = write_case(cases_dir, case_id, repo="repo")
    write_repo(
        case_file,
        {
            "app/calc.py": "def total(items):\n    return sum(items)\n",
            NOTES_FILE: f"# {UNIQUE_TEXT}\nRATE = 2\n",
        },
    )
    return case_file


def context_path(case_file: Path) -> Path:
    return case_file.parent / "recordings" / "live-context" / "recording.json"


def live(
    capsys: pytest.CaptureFixture[str], sdk: ToolUsingSdk, *args: str | Path
) -> tuple[int, str, str]:
    exit_code = main(["live", *map(str, args)], sdk_call=sdk)
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def context_live(
    capsys: pytest.CaptureFixture[str], sdk: ToolUsingSdk, cases_dir: Path, *args: str | int
) -> tuple[int, str, str]:
    """`live --mode context` with 1 run and the design's amounts; a flag in `args` overrides."""
    defaults = ["--runs", "1", "--max-calls", "27", "--max-usd", "8.10", "--max-cost-usd", "0.30"]
    return live(capsys, sdk, "--cases", cases_dir, "--mode", "context", *defaults, *map(str, args))


def test_context_run_records_tool_calls_and_cost_freshly_enough_for_replay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = tmp_path / "cases"
    first, second = context_case(cases, "calc"), context_case(cases, "calc-2")
    sdk = ToolUsingSdk(0.25)

    exit_code, _, _ = context_live(capsys, sdk, cases, "--runs", 3)

    assert len(sdk.options) == 6
    for case_file in (first, second):
        recorded = load_recording(context_path(case_file))
        assert (recorded.kind, recorded.mode, len(recorded.runs)) == ("live", "context", 3)
        assert recorded.repo_tree is not None
        for run in recorded.runs:
            [call] = run.tool_calls
            assert (call.tool, call.outcome) == ("read", "ok")
            assert [span.path for span in call.spans] == [NOTES_FILE]
            assert run.cost_usd == Decimal("0.25")
    assert exit_code == 0
    assert main(["replay", "--cases", str(cases), "--mode", "context"]) == 0
    assert "stale" not in capsys.readouterr().out


def test_context_recording_holds_no_text_of_a_file_the_model_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = context_case(tmp_path / "cases")

    context_live(capsys, ToolUsingSdk(0.25), tmp_path / "cases")

    text = context_path(case_file).read_text(encoding="utf-8")
    assert UNIQUE_TEXT not in text
    [call] = json.loads(text)["runs"][0]["tool_calls"]
    assert set(call) == {"tool", "outcome", "spans", "listed", "result_bytes"}


def test_failed_run_records_the_cap_that_ended_it_and_no_cap_otherwise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = context_case(tmp_path / "cases")
    sdk = ToolUsingSdk("error_max_budget_usd", "error_max_turns", "error_during_execution")

    _, out, err = context_live(capsys, sdk, tmp_path / "cases", "--runs", 3)

    errors = [run["error"] for run in json.loads(context_path(case_file).read_text())["runs"]]
    assert errors == [
        {"category": "permanent", "cap": "cost"},
        {"category": "permanent", "cap": "turns"},
        {"category": "permanent"},
    ]
    assert "live: case calc run 1/3 ... failed (permanent)" in err
    assert "  cap-hit runs: run 1 (cost), run 2 (turns)\n" in out


@pytest.mark.parametrize(
    ("amounts", "expected"),
    [
        (
            ["--max-cost-usd", "0.30"],
            "live with --mode context needs both --max-usd and --max-cost-usd: this run would "
            "make 6 calls (2 cases x 3 runs).",
        ),
        (
            ["--max-usd", "8.10"],
            "live with --mode context needs both --max-usd and --max-cost-usd",
        ),
        (
            ["--max-usd", "1.50", "--max-cost-usd", "0.30"],
            "live would make 6 calls (2 cases x 3 runs) of up to --max-cost-usd 0.30 each, 1.80 "
            "USD in all, more than --max-usd 1.50. Lower --runs, or pick fewer cases with --case, "
            "or pass --max-usd 1.80 if that budget is approved.",
        ),
    ],
)
def test_context_mode_without_a_covering_dollar_budget_exits_2_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], amounts: list[str], expected: str
) -> None:
    cases = tmp_path / "cases"
    context_case(cases, "calc")
    context_case(cases, "calc-2")
    sdk = ToolUsingSdk()

    exit_code, out, err = live(
        capsys,
        sdk,
        *["--cases", cases, "--mode", "context", "--runs", "3", "--max-calls", "6"],
        *amounts,
    )

    assert (exit_code, out, sdk.options) == (2, "", [])
    assert expected in err
    assert not LIVE_AUDIT.exists()


@pytest.mark.parametrize(
    ("amount", "given"), [("--max-usd", "--max-usd"), ("--max-cost-usd", "--max-cost-usd")]
)
def test_diff_mode_with_only_one_amount_exits_2_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], amount: str, given: str
) -> None:
    write_case(tmp_path / "cases")
    sdk = ToolUsingSdk()

    exit_code, _, err = live(
        capsys, sdk, "--cases", tmp_path / "cases", "--runs", 1, "--max-calls", 1, amount, "1"
    )

    assert (exit_code, sdk.options) == (2, [])
    assert f"live with {given} needs both --max-usd and --max-cost-usd" in err


@pytest.mark.parametrize("amount", ["0", "-1", "nan", "inf", "abc"])
def test_an_amount_that_isnt_above_0_exits_2_naming_its_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], amount: str
) -> None:
    context_case(tmp_path / "cases")
    sdk = ToolUsingSdk()

    exit_code, _, err = live(
        capsys,
        sdk,
        *["--cases", tmp_path / "cases", "--mode", "context", "--runs", "1", "--max-calls", "1"],
        *["--max-usd", amount, "--max-cost-usd", "0.30"],
    )

    assert (exit_code, sdk.options) == (2, [])
    assert f"argument --max-usd: {amount!r} isn't an amount of USD above 0" in err


def test_a_cost_cap_below_a_cent_exits_2_naming_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    context_case(tmp_path / "cases")
    sdk = ToolUsingSdk()

    exit_code, _, err = context_live(capsys, sdk, tmp_path / "cases", "--max-cost-usd", "0.009")

    assert (exit_code, sdk.options) == (2, [])
    assert "Invalid value for --max-cost-usd" in err


def test_a_run_cut_short_by_the_budget_keeps_the_old_recording_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = tmp_path / "cases"
    first, second = context_case(cases, "calc"), context_case(cases, "calc-2")
    old = context_path(second)
    old.parent.mkdir(parents=True)
    old.write_text("the old recording", encoding="utf-8")
    # Each call reports more than its cap, so after 4 calls 2.0 is spent, past the 1.80 planned.
    sdk = ToolUsingSdk(0.5)

    exit_code, out, err = context_live(capsys, sdk, cases, "--runs", 3, "--max-usd", "1.80")

    assert len(sdk.options) == 4
    assert len(load_recording(context_path(first)).runs) == 3
    assert old.read_text(encoding="utf-8") == "the old recording"
    assert err.endswith(
        "live: case calc-2 run 1/3 ... ok\n"
        "live: stopped: 2.0 USD spent leaves less than --max-cost-usd 0.30 of --max-usd 1.80, "
        "so no further call started. Not recorded, so each keeps its old recording: calc-2. "
        "Record them with --case and an approved budget.\n"
    )
    assert "case calc-2" not in out
    assert out.endswith("result: fail\nstopped: budget\nspent=$2.0 of --max-usd $1.80\n")
    assert exit_code == 1


def test_a_diff_mode_case_cut_short_keeps_its_old_recording(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = context_case(tmp_path / "cases")
    old = write_live(case_file, recording(case_file, [run_of()]))
    old_text = old.read_text(encoding="utf-8")
    sdk = ToolUsingSdk(0.5)

    exit_code, out, err = live(
        capsys,
        sdk,
        *["--cases", tmp_path / "cases", "--runs", "3", "--max-calls", "3"],
        *["--max-usd", "0.90", "--max-cost-usd", "0.30"],
    )

    assert len(sdk.options) == 2
    assert old.read_text(encoding="utf-8") == old_text
    assert "keeps its old recording: calc." in err
    assert out.endswith("stopped: budget\nspent=$1.0 of --max-usd $0.90\n")
    assert exit_code == 1


def test_a_run_reporting_no_cost_counts_as_the_cap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    context_case(tmp_path / "cases")
    sdk = ToolUsingSdk(0.10, None)

    _, out, _ = context_live(capsys, sdk, tmp_path / "cases", "--runs", 2)

    assert (
        "  cost: $0.40, of which $0.1 reported by 1 of 2 runs, and 1 unreported counted at "
        "--max-cost-usd $0.30 each\n"
    ) in out
    assert out.endswith("spent=$0.40 of --max-usd $8.10\n")


def test_case_picks_only_those_cases_and_plans_only_their_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = tmp_path / "cases"
    picked, other = context_case(cases, "calc"), context_case(cases, "calc-2")
    sdk = ToolUsingSdk()

    exit_code, out, _ = live(
        capsys,
        sdk,
        *["--cases", cases, "--mode", "context", "--case", "calc", "--runs", "3"],
        *["--max-calls", "3", "--max-usd", "0.90", "--max-cost-usd", "0.30"],
    )

    assert (exit_code, len(sdk.options)) == (0, 3)
    assert context_path(picked).is_file()
    assert not context_path(other).exists()
    assert "case calc-2" not in out


def test_an_unknown_case_exits_2_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    context_case(tmp_path / "cases")
    sdk = ToolUsingSdk()

    exit_code, _, err = context_live(
        capsys, sdk, tmp_path / "cases", "--case", "calc", "--case", "nope"
    )

    assert (exit_code, sdk.options) == (2, [])
    assert "--case nope: no such case under --cases" in err


def test_each_call_has_one_attempt_and_the_cost_cap_as_its_budget_and_is_audited(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_case(tmp_path / "cases")
    monkeypatch.setenv("REVIEW_BOT_MAX_RETRIES", "3")
    sdk = ToolUsingSdk(0.0712)

    live(
        capsys,
        sdk,
        *["--cases", tmp_path / "cases", "--runs", "2", "--max-calls", "2"],
        *["--max-usd", "0.20", "--max-cost-usd", "0.10"],
    )

    assert [options.max_budget_usd for options in sdk.options] == [0.10, 0.10]
    assert [options.env["CLAUDE_CODE_MAX_RETRIES"] for options in sdk.options] == ["0", "0"]
    records = [json.loads(line) for line in LIVE_AUDIT.read_text(encoding="ascii").splitlines()]
    called = [record["data"] for record in records if record["event"] == "called"]
    assert [data["cost_usd"] for data in called] == ["0.0712", "0.0712"]


def test_diff_mode_with_amounts_writes_the_diff_recording_and_reports_spend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = write_case(tmp_path / "cases")
    sdk = ToolUsingSdk(0.05)

    exit_code, out, _ = live(
        capsys,
        sdk,
        *["--cases", tmp_path / "cases", "--runs", "2", "--max-calls", "2"],
        *["--max-usd", "0.20", "--max-cost-usd", "0.10"],
    )

    recorded = load_recording(case_file.parent / "recordings" / "live" / "recording.json")
    assert recorded.mode == "diff"
    assert [run.cost_usd for run in recorded.runs] == [Decimal("0.05"), Decimal("0.05")]
    assert "tool calls per run" not in out
    assert "  cost: $0.10, reported by every run\n  cap-hit runs: none\n" in out
    assert out.endswith("result: pass\nspent=$0.10 of --max-usd $0.20\n")
    assert exit_code == 0


def test_diff_mode_without_amounts_sets_no_budget_and_reports_no_spend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_case(tmp_path / "cases")
    monkeypatch.setenv("REVIEW_BOT_MAX_COST_USD", "0.50")
    sdk = ToolUsingSdk(0.05)

    exit_code, out, _ = live(
        capsys, sdk, "--cases", tmp_path / "cases", "--runs", 2, "--max-calls", 2
    )

    assert [options.max_budget_usd for options in sdk.options] == [None, None]
    assert "spent=" not in out
    assert "cost:" not in out
    assert out.endswith("result: pass\n")
    assert exit_code == 0


def test_context_report_shows_tool_calls_per_run_and_the_spend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    context_case(tmp_path / "cases")
    sdk = ToolUsingSdk(0.25, reads=[(NOTES_FILE,), (NOTES_FILE, NOTES_FILE, "app/calc.py")])

    exit_code, out, _ = context_live(capsys, sdk, tmp_path / "cases", "--runs", 2)

    assert exit_code == 0
    assert "  tool calls per run: mean 2.0, largest 3\n" in out
    assert "  cost: $0.50, reported by every run\n  cap-hit runs: none\n" in out
    assert out.endswith("result: pass\nspent=$0.50 of --max-usd $8.10\n")


def test_context_mode_still_refuses_in_ci_and_over_max_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    context_case(tmp_path / "cases")
    sdk = ToolUsingSdk()

    short_exit, _, short_err = context_live(
        capsys, sdk, tmp_path / "cases", "--max-calls", 1, "--runs", 2
    )
    monkeypatch.setenv("CI", "true")
    ci_exit, _, ci_err = context_live(capsys, sdk, tmp_path / "cases")

    assert (short_exit, ci_exit, sdk.options) == (2, 2, [])
    assert "would make 2 calls (1 case x 2 runs), more than --max-calls 1" in short_err
    assert "live runs in CI only from a workflow_dispatch run" in ci_err
