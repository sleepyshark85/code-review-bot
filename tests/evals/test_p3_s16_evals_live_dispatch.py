"""P3-S16 AC7 for `evals live`: in CI it runs only for a `workflow_dispatch` given --allow-dispatch.

Written from the spec's criteria and ADR 0015. Every run goes through `review_bot.evals.main`
with P1-S16's scripted `sdk_call`, so no test makes a live call. Each test works on a `tmp_path`
copy of the `sample` case, from a temporary working directory, and sets `CI` and
`GITHUB_EVENT_NAME` only on purpose. The workflow's AC1 to AC6 and `pr backfill`'s AC7 are in
`tests/integration/test_p3_s16_live_workflow.py`.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from dispatch_support import REFUSED_EVENTS, set_ci
from test_p1_s16_eval_live_command import (
    FLOAT_MONEY,
    SAMPLE,
    Live,
    ScriptedSdk,
    always,
    answer,
    copy_cases,
    flags,
    live_path,
    read_recording,
)

from review_bot.evals import main

pytestmark = pytest.mark.integration

USAGE_EXIT = 2
DISPATCH = "workflow_dispatch"
ALLOW = "--allow-dispatch"


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary working directory, with neither CI nor GITHUB_EVENT_NAME set."""
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
        return Live(exit_code, captured.out + captured.err)

    return run


def assert_refused_before_any_call(live: Live, sdk: ScriptedSdk, root: Path) -> None:
    assert live.exit_code == USAGE_EXIT, live.output
    assert sdk.calls == []
    assert not live_path(root, SAMPLE).exists()
    assert not (Path.cwd() / ".review-bot").exists(), "a refused run writes no audit log"


@pytest.mark.parametrize("ci", ["true", "", "false"])
def test_p3_s16_ac7_evals_live_runs_in_ci_for_a_dispatch_with_the_flag(
    run_live, tmp_path, monkeypatch, ci
):
    set_ci(monkeypatch, DISPATCH, ci=ci)
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))

    live = run_live(*flags(root, runs=2, max_calls=2), ALLOW, sdk=sdk)

    assert live.exit_code != USAGE_EXIT, live.output
    assert len(sdk.calls) == 2
    assert live_path(root, SAMPLE).is_file()


@pytest.mark.parametrize("event", REFUSED_EVENTS)
def test_p3_s16_ac7_evals_live_with_the_flag_refuses_any_other_ci_event(
    run_live, tmp_path, monkeypatch, event
):
    set_ci(monkeypatch, event)
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))

    live = run_live(*flags(root, runs=1, max_calls=1), ALLOW, sdk=sdk)

    assert_refused_before_any_call(live, sdk, root)
    assert "CI" in live.output


@pytest.mark.parametrize("ci", ["true", "", "false"])
def test_p3_s16_ac7_evals_live_for_a_dispatch_without_the_flag_refuses(
    run_live, tmp_path, monkeypatch, ci
):
    set_ci(monkeypatch, DISPATCH, ci=ci)
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))

    live = run_live(*flags(root, runs=1, max_calls=1), sdk=sdk)

    assert_refused_before_any_call(live, sdk, root)
    assert "CI" in live.output


@pytest.mark.parametrize("event", [None, DISPATCH, "pull_request", "push"])
@pytest.mark.parametrize("flag", [[], [ALLOW]], ids=["no-flag", "flag"])
def test_p3_s16_ac7_evals_live_without_ci_runs_whatever_the_flag_and_event(
    run_live, tmp_path, monkeypatch, event, flag
):
    if event is not None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))

    live = run_live(*flags(root, runs=2, max_calls=2), *flag, sdk=sdk)

    assert live.exit_code != USAGE_EXIT, live.output
    assert len(sdk.calls) == 2


def test_p3_s16_ac7_evals_live_without_ci_the_flag_changes_no_result(run_live, tmp_path):
    """Same exit code and the same recorded findings, with and without the flag."""
    results = []
    for index, flag in enumerate(([], [ALLOW])):
        root = copy_cases(tmp_path / f"run-{index}", SAMPLE)
        sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))
        live = run_live(*flags(root, runs=1, max_calls=1), *flag, sdk=sdk)
        runs = read_recording(root, SAMPLE)["runs"]
        results.append((live.exit_code, [run["findings"] for run in runs]))
    assert results[0] == results[1]


def test_p3_s16_limits_dispatched_evals_live_still_refuses_over_its_budget(
    run_live, tmp_path, monkeypatch
):
    """The dispatch passes the CI guard only; the command's own pre-flight still applies."""
    set_ci(monkeypatch, DISPATCH)
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))

    live = run_live(*flags(root, runs=2, max_calls=1), ALLOW, sdk=sdk)

    assert live.exit_code == USAGE_EXIT, live.output
    assert sdk.calls == []


def test_p3_s16_interfaces_replay_has_no_allow_dispatch(run_live, tmp_path):
    """Interfaces: the flag is on `evals live` and `pr backfill` only."""
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer(FLOAT_MONEY)))

    replayed = run_live("--cases", root, ALLOW, sdk=sdk, command="replay")

    assert replayed.exit_code == USAGE_EXIT, replayed.output
    assert sdk.calls == []
