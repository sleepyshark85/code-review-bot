"""P3-S6: record live context evals within a dollar cap (AC1 to AC9).

Written from the spec's criteria, ADR 0015 and design.md's "Spend and caps", in parallel with the
developer and before their code was read. **No test here makes a live call.** Every run goes
through `review_bot.evals.main(["live", ...], sdk_call=fake)`, P1-S16's seam: the command builds
its own `ClaudeAgentProvider` over the scripted `sdk_call`, so each call's options can be read.

In context mode the fake plays the CLI as P3-S3's suite does: the init message listing the three
tools, then tool calls made through the provider's in-process `repo` server (`fake_mcp`), then a
result with `total_cost_usd`. The fake picks its script from the case whose diff the prompt holds.

Each test works on a `tmp_path` copy of committed context cases (P3-S5's `copy_cases`, which
copies a sibling diff named by a relative path), from a temporary working directory, outside CI.
AC10 and AC11 are in `tests/integration/test_p3_s6_live_evals_workflow.py`.

Readings, each used in one helper:
- The budget is checked with Decimal arithmetic on the flags and the reported costs, and a call
  starts while `--max-usd` less the spend so far is at least `--max-cost-usd` (AC3: "less than"
  stops). A cost the CLI reports as a float is read as its shortest decimal, as P3-S3 AC6 does.
- `spent=$S` sums the reported costs; S may print with or without a trailing zero (`0.8`/`0.80`).
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from fake_mcp import ToolCallLog, init_message, tool_call
from fake_sdk import error_result, findings_json, result_message
from test_p1_s16_eval_live_command import nothing_written_to_the_repository
from test_p3_s5_context_eval_cases import copy_cases

from review_bot.evals import freshness_fields, main

pytestmark = pytest.mark.integration

__all__ = ["nothing_written_to_the_repository"]  # an autouse fixture, used by being imported

USAGE_EXIT = 2
SAMPLE_CONTEXT = "sample-context"
CLEAN_CONTEXT = "clean-context"
TOOL_RESULT = "injection-tool-result"
# The one file each case's diff names, which the prompt then holds.
CASE_FILES = {
    SAMPLE_CONTEXT: "billing/invoice.py",
    CLEAN_CONTEXT: "billing/refunds.py",
    TOOL_RESULT: "billing/payouts.py",
}
# A file in each case's `repo`, for the fake to read.
REPO_FILES = {
    SAMPLE_CONTEXT: "billing/checkout.py",
    CLEAN_CONTEXT: "billing/orders.py",
    TOOL_RESULT: "billing/fees.py",
}
USAGE = {"input_tokens": 100, "output_tokens": 10}
LIVE_AUDIT = Path(".review-bot") / "eval-live-audit.jsonl"
FRESHNESS = ("case_sha256", "diff_sha256", "prompt_version", "prompt_sha256", "repo_tree")
# P3-S5's recorded tool call: P3-S1's ledger entry without `input_sha256`.
TOOL_CALL_KEYS = {"tool", "outcome", "reason", "spans", "listed", "result_bytes"}
SPAN_KEYS = {"path", "first_line", "last_line"}
NO_COST = object()

FLOAT_MONEY = {
    "file": CASE_FILES[SAMPLE_CONTEXT],
    "line": 34,
    "severity": "medium",
    "category": "logic",
    "rationale": "Float arithmetic on money loses cents.",
    "confidence": 0.8,
}


# --- The fake SDK ------------------------------------------------------------------------------


def case_of(prompt: str) -> str:
    named = [case_id for case_id, file in CASE_FILES.items() if file in prompt]
    assert len(named) == 1, f"the prompt names {named!r}, not exactly one case"
    return named[0]


# A step is a message to yield, an exception to raise, or an async callable given the options.
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
        self.calls.append((prompt, options))
        case_id = case_of(prompt)
        self.cases.append(case_id)
        return self._stream(self.respond(case_id, self.cases.count(case_id)), options)

    @staticmethod
    async def _stream(steps: Steps, options: ClaudeAgentOptions) -> Any:
        for step in steps:
            if isinstance(step, BaseException):
                raise step
            if callable(step):
                await step(options)
            else:
                yield step


def cost_fields(cost: object) -> dict[str, Any]:
    return {} if cost is NO_COST else {"total_cost_usd": cost}


def context_answer(
    *findings: Mapping[str, Any],
    cost: object = 0.05,
    tools: Sequence[object] = (),
) -> Steps:
    """A context review: the init, `tools` (tool-call steps), then the findings and the cost."""
    result = result_message(
        findings_json(*map(dict, findings)), usage=dict(USAGE), **cost_fields(cost)
    )
    return [init_message(), *tools, result]


def reads(case_id: str, count: int, log: ToolCallLog | None = None) -> list[object]:
    """`count` reads of the case's repository file."""
    return [tool_call("read", {"path": REPO_FILES[case_id]}, log or ToolCallLog())] * count


def cap_hit(subtype: str, cost: object = 0.30) -> Steps:
    """A query the CLI ended at its cost or turn cap (`error_max_budget_usd`, `error_max_turns`)."""
    result = result_message(None, is_error=True, subtype=subtype, **cost_fields(cost))
    return [init_message(), result]


def transient() -> Steps:
    return [init_message(), error_result(status=503)]


def diff_answer(*findings: Mapping[str, Any], cost: object = 0.05) -> Steps:
    """A diff-mode review: one result, no init or tools."""
    return [
        result_message(findings_json(*map(dict, findings)), usage=dict(USAGE), **cost_fields(cost))
    ]


def always(steps: Steps) -> Responder:
    return lambda _case_id, _number: steps


# --- The command -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Live:
    exit_code: int
    out: str
    err: str

    @property
    def output(self) -> str:
        return self.out + self.err

    def case_block(self, case_id: str) -> list[str]:
        """Stdout's lines for one case, from `case <id>:` up to the next blank line."""
        lines = self.out.splitlines()
        start = next(
            (i for i, line in enumerate(lines) if line.startswith(f"case {case_id}:")), None
        )
        assert start is not None, f"no case {case_id!r} in:\n{self.out}"
        block = []
        for line in lines[start:]:
            if not line:
                break
            block.append(line)
        return block


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary working directory, outside CI, for every test."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    return work


@pytest.fixture
def run_live(capsys: pytest.CaptureFixture[str]) -> Callable[..., Live]:
    def run(*args: str | Path, sdk: ScriptedSdk, command: str = "live") -> Live:
        capsys.readouterr()
        try:
            exit_code = main([command, *map(str, args)], sdk_call=sdk)
        except SystemExit as exc:  # argparse reports usage errors this way
            exit_code = exc.code if isinstance(exc.code, int) else USAGE_EXIT
        captured = capsys.readouterr()
        return Live(exit_code, captured.out, captured.err)

    return run


def budget(  # noqa: PLR0913 - one keyword per flag
    root: Path,
    *,
    runs: int,
    max_calls: int,
    max_usd: str | None,
    max_cost_usd: str | None,
    mode: str = "context",
) -> list[str | Path]:
    args: list[str | Path] = ["--cases", root, "--mode", mode, "--runs", str(runs)]
    args += ["--max-calls", str(max_calls)]
    if max_usd is not None:
        args += ["--max-usd", max_usd]
    if max_cost_usd is not None:
        args += ["--max-cost-usd", max_cost_usd]
    return args


def context_path(root: Path, case_id: str) -> Path:
    return root / case_id / "recordings" / "live-context" / "recording.json"


def diff_path(root: Path, case_id: str) -> Path:
    return root / case_id / "recordings" / "live" / "recording.json"


def read_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def plant_old_recordings(root: Path, *case_ids: str) -> dict[str, bytes]:
    old = {}
    for case_id in case_ids:
        path = context_path(root, case_id)
        path.parent.mkdir(parents=True)
        path.write_bytes(f'{{"old": "{case_id}"}}\n'.encode())
        old[case_id] = path.read_bytes()
    return old


def agent_records(log: Path, event: str) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in log.read_text(encoding="ascii").splitlines()]
    return [r["data"] for r in records if r.get("stage") == "agent" and r.get("event") == event]


def last_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, "nothing on stdout"
    return lines[-1]


# --- AC1: a context recording per case, with tool calls, cost and freshness ---------------------


def test_p3_s6_ac1_two_cases_three_runs_write_fresh_context_recordings(run_live, tmp_path):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT, CLEAN_CONTEXT)
    sdk = ScriptedSdk(
        lambda case_id, _n: context_answer(
            *([FLOAT_MONEY] if case_id == SAMPLE_CONTEXT else []), tools=reads(case_id, 1)
        )
    )

    live = run_live(
        *budget(root, runs=3, max_calls=6, max_usd="1.80", max_cost_usd="0.30"), sdk=sdk
    )

    assert live.exit_code != USAGE_EXIT, live.output
    assert sorted(sdk.cases) == [CLEAN_CONTEXT] * 3 + [SAMPLE_CONTEXT] * 3
    for case_id in (SAMPLE_CONTEXT, CLEAN_CONTEXT):
        recording = read_json(context_path(root, case_id))
        assert recording["mode"] == "context"
        assert recording["kind"] == "live"
        assert recording["case_id"] == case_id
        fresh = freshness_fields(root / case_id / "case.json", mode="context")
        assert {name: recording[name] for name in FRESHNESS} == {
            name: fresh[name] for name in FRESHNESS
        }
        assert len(recording["runs"]) == 3
        for run in recording["runs"]:
            assert "error" not in run, run
            [call] = run["tool_calls"]
            assert call["tool"] == "read"
            assert call["outcome"] == "ok"
            assert {span["path"] for span in call["spans"]} == {REPO_FILES[case_id]}
            assert run["usage"]["cost_usd"] == "0.05"
        assert not diff_path(root, case_id).exists(), "context mode writes no diff-mode recording"


def test_p3_s6_ac1_replay_in_context_mode_scores_the_recording_without_a_stale_problem(
    run_live, tmp_path
):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT)
    sdk = ScriptedSdk(always(context_answer(FLOAT_MONEY, tools=reads(SAMPLE_CONTEXT, 1))))
    run_live(*budget(root, runs=3, max_calls=3, max_usd="0.90", max_cost_usd="0.30"), sdk=sdk)

    replayed = run_live(
        "--cases", root, "--mode", "context", sdk=ScriptedSdk(always([])), command="replay"
    )

    assert replayed.exit_code != USAGE_EXIT, replayed.output
    assert "stale" not in replayed.output.lower(), replayed.output
    assert re.search(r"float-money\b.*caught 3/3", replayed.output), replayed.output


@pytest.mark.parametrize(
    ("subtype", "cap"),
    [("error_max_budget_usd", "cost"), ("error_max_turns", "turns")],
    ids=["cost", "turns"],
)
def test_p3_s6_ac1_cap_hit_run_is_recorded_with_its_cap(run_live, tmp_path, subtype, cap):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT)
    sdk = ScriptedSdk(lambda _c, n: cap_hit(subtype) if n == 2 else context_answer(FLOAT_MONEY))

    live = run_live(
        *budget(root, runs=3, max_calls=3, max_usd="0.90", max_cost_usd="0.30"), sdk=sdk
    )

    assert len(sdk.calls) == 3, live.output
    runs = read_json(context_path(root, SAMPLE_CONTEXT))["runs"]
    assert runs[1]["error"] == {"category": "permanent", "cap": cap}
    assert [len(runs[0]["findings"]), len(runs[2]["findings"])] == [1, 1]


def test_p3_s6_ac1_failed_run_without_a_cap_records_no_cap(run_live, tmp_path):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT)
    sdk = ScriptedSdk(lambda _c, n: transient() if n == 2 else context_answer(FLOAT_MONEY))

    run_live(*budget(root, runs=3, max_calls=3, max_usd="0.90", max_cost_usd="0.30"), sdk=sdk)

    runs = read_json(context_path(root, SAMPLE_CONTEXT))["runs"]
    assert runs[1]["error"] == {"category": "transient"}


# --- AC2: refused before any call unless the plan fits the allowance ----------------------------


@pytest.mark.parametrize(
    ("max_usd", "max_cost_usd", "named"),
    [
        pytest.param(None, "0.30", ["--max-usd"], id="no-max-usd"),
        pytest.param("1.80", None, ["--max-cost-usd"], id="no-max-cost-usd"),
        pytest.param("1.50", "0.30", ["6", "0.30", "1.50"], id="6-calls-x-0.30-over-1.50"),
    ],
)
def test_p3_s6_ac2_context_mode_refuses_before_any_call(  # noqa: PLR0913, PLR0917 - parametrized
    run_live, tmp_path, work_dir, max_usd, max_cost_usd, named
):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT, CLEAN_CONTEXT)
    sdk = ScriptedSdk(always(context_answer()))

    live = run_live(
        *budget(root, runs=3, max_calls=6, max_usd=max_usd, max_cost_usd=max_cost_usd), sdk=sdk
    )

    assert live.exit_code == USAGE_EXIT, live.output
    assert sdk.calls == []
    for text in named:
        assert re.search(rf"(?<![\d.]){re.escape(text)}(?![\d])", live.err), (text, live.err)
    assert not (root / SAMPLE_CONTEXT / "recordings").exists()
    assert not (work_dir / LIVE_AUDIT).exists()


def test_p3_s6_ac2_over_plan_refusal_names_planned_calls_cap_and_allowance(run_live, tmp_path):
    """The refusal names all three, each next to its flag or as the plan's count."""
    root = copy_cases(tmp_path, SAMPLE_CONTEXT, CLEAN_CONTEXT)
    sdk = ScriptedSdk(always(context_answer()))

    live = run_live(
        *budget(root, runs=3, max_calls=6, max_usd="1.50", max_cost_usd="0.30"), sdk=sdk
    )

    assert live.exit_code == USAGE_EXIT
    assert re.search(r"\b6 calls\b", live.err), live.err
    assert "--max-cost-usd 0.30" in live.err, live.err
    assert "--max-usd 1.50" in live.err, live.err


def test_p3_s6_ac2_plan_exactly_at_the_allowance_runs(run_live, tmp_path):
    """6 calls x 0.25 = 1.50: "over" refuses, equal doesn't (design: planned x cap <= --max-usd)."""
    root = copy_cases(tmp_path, SAMPLE_CONTEXT, CLEAN_CONTEXT)
    sdk = ScriptedSdk(always(context_answer(cost=0.01)))

    live = run_live(
        *budget(root, runs=3, max_calls=6, max_usd="1.50", max_cost_usd="0.25"), sdk=sdk
    )

    assert live.exit_code != USAGE_EXIT, live.output
    assert len(sdk.calls) == 6


@pytest.mark.parametrize("value", ["0", "-1", "abc", "nan"])
@pytest.mark.parametrize("flag", ["--max-usd", "--max-cost-usd"])
def test_p3_s6_ac2_interfaces_money_flags_must_be_decimals_above_0(run_live, tmp_path, flag, value):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT)
    sdk = ScriptedSdk(always(context_answer()))
    args = budget(root, runs=1, max_calls=1, max_usd="0.30", max_cost_usd="0.30")
    args[args.index(flag) + 1] = value

    live = run_live(*args, sdk=sdk)

    assert live.exit_code == USAGE_EXIT, live.output
    assert sdk.calls == []
    assert flag in live.err, live.err


def test_p3_s6_ac2_ci_set_still_refuses(run_live, tmp_path, monkeypatch):
    monkeypatch.setenv("CI", "true")
    root = copy_cases(tmp_path, SAMPLE_CONTEXT)
    sdk = ScriptedSdk(always(context_answer()))

    live = run_live(
        *budget(root, runs=1, max_calls=1, max_usd="0.30", max_cost_usd="0.30"), sdk=sdk
    )

    assert live.exit_code == USAGE_EXIT, live.output
    assert sdk.calls == []
    assert re.search(r"\bCI\b", live.err), live.err


def test_p3_s6_ac2_short_max_calls_still_refuses(run_live, tmp_path):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT, CLEAN_CONTEXT)
    sdk = ScriptedSdk(always(context_answer()))

    live = run_live(
        *budget(root, runs=3, max_calls=5, max_usd="1.80", max_cost_usd="0.30"), sdk=sdk
    )

    assert live.exit_code == USAGE_EXIT, live.output
    assert sdk.calls == []
    assert re.search(r"\b6 calls\b", live.err), live.err


# --- AC3: no call starts once less than one cap is left ----------------------------------------


def test_p3_s6_ac3_budget_stops_further_calls_and_keeps_the_cut_short_cases_old_file(
    run_live, tmp_path
):
    """Each call overshoots to $0.50 against a $0.30 cap and $1.80 allowance.

    After 3 calls $0.30 is left, which isn't less than the cap, so a 4th starts; after it $-0.20
    is left, so the 5th doesn't. The first case finished and is written; the second keeps its old
    file.
    """
    root = copy_cases(tmp_path, SAMPLE_CONTEXT, CLEAN_CONTEXT)
    old = plant_old_recordings(root, SAMPLE_CONTEXT, CLEAN_CONTEXT)
    sdk = ScriptedSdk(always(context_answer(cost=0.50)))

    live = run_live(
        *budget(root, runs=3, max_calls=6, max_usd="1.80", max_cost_usd="0.30"), sdk=sdk
    )

    assert len(sdk.calls) == 4, live.output
    first, second = sdk.cases[0], sdk.cases[-1]
    assert sdk.cases == [first] * 3 + [second]
    finished = read_json(context_path(root, first))
    assert len(finished["runs"]) == 3
    assert context_path(root, second).read_bytes() == old[second]
    assert "stopped: budget" in live.out.splitlines(), live.out
    assert live.exit_code == 1, live.output
    named = [line for line in live.err.splitlines() if second in line and "run 1/3" not in line]
    assert named, f"stderr doesn't name the cut-short case {second}:\n{live.err}"


def test_p3_s6_ac3_a_case_cut_short_without_an_old_file_writes_none(run_live, tmp_path):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT, CLEAN_CONTEXT)
    sdk = ScriptedSdk(always(context_answer(cost=0.50)))

    run_live(*budget(root, runs=3, max_calls=6, max_usd="1.80", max_cost_usd="0.30"), sdk=sdk)

    assert not context_path(root, sdk.cases[-1]).exists()


def test_p3_s6_ac3_costs_within_the_cap_never_stop_the_run(run_live, tmp_path):
    """Control: at exactly the cap each time, every planned call fits."""
    root = copy_cases(tmp_path, SAMPLE_CONTEXT, CLEAN_CONTEXT)
    sdk = ScriptedSdk(always(context_answer(cost=0.30)))

    live = run_live(
        *budget(root, runs=3, max_calls=6, max_usd="1.80", max_cost_usd="0.30"), sdk=sdk
    )

    assert len(sdk.calls) == 6, live.output
    assert "stopped: budget" not in live.out


# --- AC4: an unreported cost counts as the whole cap ------------------------------------------


@pytest.mark.parametrize(
    ("second_cost", "calls"),
    [
        # 0.40 + 0.30 (counted) = 0.70; 0.20 left is under the 0.30 cap, so no 3rd call.
        pytest.param(NO_COST, 2, id="unreported-counts-as-the-cap"),
        # Control: 0.40 + 0.10 = 0.50; 0.40 left, so the 3rd call starts.
        pytest.param(0.10, 3, id="reported-0.10"),
        # P3-S3 AC6: a value it can't read is no cost at all, so it counts as the cap too.
        pytest.param(-1.0, 2, id="negative-counts-as-the-cap"),
    ],
)
def test_p3_s6_ac4_unreported_cost_counts_as_max_cost_usd(run_live, tmp_path, second_cost, calls):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT)
    costs = {1: 0.40, 2: second_cost, 3: 0.10}
    sdk = ScriptedSdk(lambda _c, n: context_answer(cost=costs[n]))

    live = run_live(
        *budget(root, runs=3, max_calls=3, max_usd="0.90", max_cost_usd="0.30"), sdk=sdk
    )

    assert len(sdk.calls) == calls, live.output
    assert ("stopped: budget" in live.out) == (calls < 3), live.out


# --- AC5: --case picks the cases, and only their calls are planned ------------------------------


def test_p3_s6_ac5_only_the_named_cases_run_and_are_planned(run_live, tmp_path):
    """Three cases are under --cases; a plan counting all three would pass --max-calls 2."""
    root = copy_cases(tmp_path, SAMPLE_CONTEXT, CLEAN_CONTEXT, TOOL_RESULT)
    sdk = ScriptedSdk(always(context_answer()))

    live = run_live(
        *budget(root, runs=1, max_calls=2, max_usd="0.60", max_cost_usd="0.30"),
        "--case", SAMPLE_CONTEXT, "--case", CLEAN_CONTEXT,
        sdk=sdk,
    )  # fmt: skip

    assert live.exit_code != USAGE_EXIT, live.output
    assert sorted(sdk.cases) == [CLEAN_CONTEXT, SAMPLE_CONTEXT]
    assert context_path(root, SAMPLE_CONTEXT).is_file()
    assert context_path(root, CLEAN_CONTEXT).is_file()
    assert not (root / TOOL_RESULT / "recordings").exists()


def test_p3_s6_ac5_unknown_case_id_exits_2_naming_it(run_live, tmp_path, work_dir):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT)
    sdk = ScriptedSdk(always(context_answer()))

    live = run_live(
        *budget(root, runs=1, max_calls=2, max_usd="0.60", max_cost_usd="0.30"),
        "--case", SAMPLE_CONTEXT, "--case", "no-such-case",
        sdk=sdk,
    )  # fmt: skip

    assert live.exit_code == USAGE_EXIT, live.output
    assert sdk.calls == []
    assert "no-such-case" in live.err, live.err
    assert not context_path(root, SAMPLE_CONTEXT).exists()


# --- AC6: diff mode with and without the dollar flags -----------------------------------------


def test_p3_s6_ac6_diff_mode_over_its_allowance_refuses_before_any_call(run_live, tmp_path):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT, CLEAN_CONTEXT)
    sdk = ScriptedSdk(always(diff_answer()))

    live = run_live(
        *budget(root, runs=3, max_calls=6, max_usd="0.50", max_cost_usd="0.10", mode="diff"),
        sdk=sdk,
    )

    assert live.exit_code == USAGE_EXIT, live.output
    assert sdk.calls == []
    assert re.search(r"\b6 calls\b", live.err), live.err


def test_p3_s6_ac6_diff_mode_with_the_flags_writes_the_diff_recording_capped(run_live, tmp_path):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT)
    sdk = ScriptedSdk(always(diff_answer(FLOAT_MONEY)))

    live = run_live(
        *budget(root, runs=3, max_calls=3, max_usd="0.30", max_cost_usd="0.10", mode="diff"),
        sdk=sdk,
    )

    assert live.exit_code != USAGE_EXIT, live.output
    assert len(sdk.calls) == 3
    recording = read_json(diff_path(root, SAMPLE_CONTEXT))
    assert recording["kind"] == "live"
    assert recording.get("mode", "diff") == "diff"
    assert [len(run["findings"]) for run in recording["runs"]] == [1, 1, 1]
    assert not context_path(root, SAMPLE_CONTEXT).exists()
    assert [options.max_budget_usd for _, options in sdk.calls] == [0.10] * 3


def test_p3_s6_ac6_diff_mode_stops_on_budget_too(run_live, tmp_path):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT)
    sdk = ScriptedSdk(always(diff_answer(cost=0.25)))

    live = run_live(
        *budget(root, runs=3, max_calls=3, max_usd="0.30", max_cost_usd="0.10", mode="diff"),
        sdk=sdk,
    )

    # 0.25 spent leaves 0.05, under the 0.10 cap.
    assert len(sdk.calls) == 1, live.output
    assert "stopped: budget" in live.out.splitlines()
    assert live.exit_code == 1


# AC6's diff mode without the dollar flags, uncapped as P1-S16's command, is superseded: P4-S1
# AC8 refuses it (tests/integration/test_p4_s1_policy_file.py).


# --- AC7: retries 0, the per-call budget, and the cost in the audit log ------------------------


def test_p3_s6_ac7_every_call_has_retries_0_and_the_per_call_budget(
    run_live, tmp_path, monkeypatch
):
    monkeypatch.setenv("REVIEW_BOT_MAX_RETRIES", "5")
    monkeypatch.setenv("REVIEW_BOT_MAX_COST_USD", "9.99")
    root = copy_cases(tmp_path, SAMPLE_CONTEXT)
    sdk = ScriptedSdk(always(context_answer(FLOAT_MONEY)))

    live = run_live(
        *budget(root, runs=2, max_calls=2, max_usd="0.60", max_cost_usd="0.30"), sdk=sdk
    )

    assert len(sdk.calls) == 2, live.output
    assert [options.env["CLAUDE_CODE_MAX_RETRIES"] for _, options in sdk.calls] == ["0", "0"]
    assert [options.max_budget_usd for _, options in sdk.calls] == [0.30, 0.30]


def test_p3_s6_ac7_live_audit_log_records_each_calls_cost(run_live, tmp_path, work_dir):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT)
    costs = {1: 0.05, 2: 0.1234, 3: NO_COST}
    sdk = ScriptedSdk(lambda _c, n: context_answer(cost=costs[n]))

    run_live(*budget(root, runs=3, max_calls=3, max_usd="0.90", max_cost_usd="0.30"), sdk=sdk)

    called = agent_records(work_dir / LIVE_AUDIT, "called")
    assert [record["cost_usd"] for record in called] == ["0.05", "0.1234", None]


# --- AC8: the report adds tool calls, cost, cap-hit runs and the spend -------------------------


def test_p3_s6_ac8_report_shows_tool_calls_cost_cap_hits_and_ends_with_spent(run_live, tmp_path):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT, CLEAN_CONTEXT)

    def respond(case_id: str, number: int) -> Steps:
        if case_id == SAMPLE_CONTEXT:  # 1, 2 and 3 tool calls: mean 2, largest 3; $0.30
            return context_answer(FLOAT_MONEY, cost=0.10, tools=reads(case_id, number))
        if number == 2:  # one cost cap-hit run, reported at $0.30; the case costs $0.50
            return cap_hit("error_max_budget_usd", cost=0.30)
        return context_answer(cost=0.10, tools=reads(case_id, 1))

    sdk = ScriptedSdk(respond)

    live = run_live(
        *budget(root, runs=3, max_calls=6, max_usd="1.80", max_cost_usd="0.30"), sdk=sdk
    )

    assert len(sdk.calls) == 6, live.output
    sample = "\n".join(live.case_block(SAMPLE_CONTEXT))
    clean = "\n".join(live.case_block(CLEAN_CONTEXT))
    assert re.search(r"tool calls\b.*\bmean\b\D*2(\.0+)?\b.*\blargest\b\D*3\b", sample), sample
    assert re.search(r"\$0\.30?\b", sample), sample
    assert re.search(r"\$0\.50?\b", clean), clean
    cap_lines = [line for line in clean.splitlines() if re.search(r"cap.?hit", line)]
    assert cap_lines, clean
    assert any(re.search(r"\b1\b|run 2\b", line) for line in cap_lines), cap_lines
    assert re.fullmatch(r"spent=\$0\.80? of --max-usd \$1\.80", last_line(live.out)), live.out


def test_p3_s6_ac8_cap_hit_count_is_zero_when_no_run_hit_a_cap(run_live, tmp_path):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT)
    sdk = ScriptedSdk(always(context_answer(cost=0.10, tools=reads(SAMPLE_CONTEXT, 2))))

    live = run_live(
        *budget(root, runs=3, max_calls=3, max_usd="0.90", max_cost_usd="0.30"), sdk=sdk
    )

    block = live.case_block(SAMPLE_CONTEXT)
    cap_lines = [line for line in block if re.search(r"cap.?hit", line)]
    assert all(not re.search(r"\b[1-9]\d*\b", line.split("cap", 1)[1]) for line in cap_lines), (
        cap_lines
    )
    assert re.fullmatch(r"spent=\$0\.30? of --max-usd \$0\.90", last_line(live.out)), live.out


# --- AC9: no file text in the recording --------------------------------------------------------


FILE_SENTINEL = "FILE-TEXT-SENTINEL-QX81"
GREP_LITERAL = "SENTINEL-QX"


def test_p3_s6_ac9_file_text_the_model_read_is_not_in_the_recording(run_live, tmp_path):
    root = copy_cases(tmp_path, SAMPLE_CONTEXT)
    checkout = root / SAMPLE_CONTEXT / "repo" / REPO_FILES[SAMPLE_CONTEXT]
    with checkout.open("a", encoding="utf-8") as file:
        file.write(f"# {FILE_SENTINEL}\n")
    log = ToolCallLog()
    tools = [
        tool_call("read", {"path": REPO_FILES[SAMPLE_CONTEXT]}, log),
        tool_call("grep", {"literal": GREP_LITERAL}, log),
    ]
    sdk = ScriptedSdk(always(context_answer(FLOAT_MONEY, tools=tools)))

    run_live(*budget(root, runs=1, max_calls=1, max_usd="0.30", max_cost_usd="0.30"), sdk=sdk)

    assert all(FILE_SENTINEL in text for text in log.texts), "the model must have read the text"
    text = context_path(root, SAMPLE_CONTEXT).read_text(encoding="utf-8")
    assert FILE_SENTINEL not in text
    assert GREP_LITERAL not in text
    [run] = read_json(context_path(root, SAMPLE_CONTEXT))["runs"]
    assert [call["tool"] for call in run["tool_calls"]] == ["read", "grep"]
    for call in run["tool_calls"]:
        assert set(call) <= TOOL_CALL_KEYS, sorted(call)
        assert isinstance(call["result_bytes"], int) and call["result_bytes"] > 0
        for span in call["spans"]:
            assert set(span) == SPAN_KEYS
            assert span["path"] == REPO_FILES[SAMPLE_CONTEXT]
