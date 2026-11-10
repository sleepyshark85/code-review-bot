"""P3-S14: turn on context reviews in CI with one repository variable.

Written from the spec's criteria, Phase 3 design.md (Spend and caps) and ADR 0012, in the style
of the P2-S3 and P2-S6 suites of `review.yml`, whose helpers it uses. AC1 to AC6 read
`.github/workflows/review.yml` with PyYAML; each `${{ ... }}` env value is evaluated with P2-S3's
small evaluator (`resolved_env`), which, like GitHub, reads an unset variable as `''`. Three
things are executed rather than read:

- the `check-mode` job's context check, with bash, for every value tried (AC1);
- each review step's `run:` script, with bash and P3-S16's fake `uv` first on `PATH`, recording
  the argv and environment the bot would get, so the conditional cost-cap export runs (AC2, AC6);
- that recorded argv and environment, through `review_bot.pr.cli.main` in a P3-S8 pull request
  clone (head only under `refs/pull/7/head`), with a scripted provider: a real run in each mode.

AC7 is a record of a live run, kept in the test report.
"""

import json
import os
import re
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_p2_s2_review_pr_event import event
from test_p2_s3_shadow_workflow import (
    CAPS,
    STATUS_FUNCTIONS,
    WORKFLOW_PATH,
    argv_of,
    check_job_name,
    jobs_of,
    option_value,
    resolved_env,
    review_job,
    run_script,
    run_step_in,
    steps_of,
    steps_using,
    walk,
)
from test_p3_s8_pr_review_with_context import (
    CHECKOUT,
    CHECKOUT_AT_C,
    READ_AROUND_12,
    ContextProvider,
    PrRun,
    as_decimal,
    assert_phase_2_run,
    make_context_pull,
)
from test_p3_s16_live_workflow import FAKE_STDERR, FAKE_STDOUT, FAKE_UV

from review_bot.pr import cli as pr_cli
from review_bot.providers.base import ReviewRequest, ReviewResponse

pytestmark = pytest.mark.integration

REVIEW_MODES = ("shadow", "comment")
VARIABLE = "vars.REVIEW_BOT_CONTEXT"
COST_CAP = "REVIEW_BOT_MAX_COST_USD"
# AC2's literals.
TOOL_CAPS = {
    "REVIEW_BOT_MAX_TOOL_CALLS": "12",
    "REVIEW_BOT_MAX_TOOL_RESULT_BYTES": "16000",
    "REVIEW_BOT_MAX_TOOL_TOTAL_BYTES": "96000",
}
CONTEXT_COST_CAP = "0.60"
CONTEXT_DEADLINE_S = "300"
DIFF_DEADLINE_S = "240"
# AC3: every job's permissions; the check and collect jobs' are P2-S3's and P2-S8's, unchanged.
PERMISSIONS = {
    "check-mode": {"contents": "read"},
    "shadow": {"contents": "read", "issues": "read"},
    "comment": {"contents": "read", "pull-requests": "write", "issues": "read"},
    "collect": {"contents": "read", "pull-requests": "read"},
}
TOKEN = "ghs_s14_canary"  # noqa: S105 - a canary to look for, not a credential
API_KEY = "fake-key-for-the-test"
NUMBER = "7"
# Values the context check must refuse: case and spacing variants, other words, shell text.
UNKNOWN_VALUES = [
    "Repo",
    "REPO",
    "OFF",
    " repo",
    "repo ",
    "on",
    "true",
    "context",
    "shadow",
    "repo|off",
    "*",
    "$(touch pwned)",
    "`touch pwned`",
]
PWNED = "pwned"
CREDENTIAL = re.compile(r"secrets\.|github\.token|GITHUB_TOKEN|ANTHROPIC_API_KEY", re.IGNORECASE)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No REVIEW_BOT_*, GitHub variable or CI flag from the machine running the tests."""
    for name in list(os.environ):
        if name.startswith("REVIEW_BOT_") or name in {
            "CI",
            "GITHUB_EVENT_NAME",
            "GITHUB_TOKEN",
            "GITHUB_API_URL",
            "GITHUB_REPOSITORY",
        }:
            monkeypatch.delenv(name)


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def variables(value: str | None) -> dict[str, Any]:
    """The contexts a review step's env reads, with REVIEW_BOT_CONTEXT `value` (None: unset)."""
    return {
        VARIABLE: value,
        "secrets.ANTHROPIC_API_KEY": API_KEY,
        "secrets.GITHUB_TOKEN": TOKEN,
        "github.token": TOKEN,
        "github.event.pull_request.number": NUMBER,
    }


def review_step(workflow: dict[Any, Any], mode: str) -> dict[str, Any]:
    step = run_step_in(review_job(workflow, mode))
    assert step is not None
    return step


def step_env(workflow: dict[Any, Any], mode: str, value: str | None) -> dict[str, str]:
    """The review step's whole env as the runner would give it; every value must resolve."""
    step = review_step(workflow, mode)
    env = resolved_env(step, variables(value))
    assert set(env) == set(step["env"]), "an env value reads a context the test doesn't set"
    return env


def context_check_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    """The check-mode step whose env reads `vars.REVIEW_BOT_CONTEXT`."""
    found = [
        step
        for step in steps_of(jobs_of(workflow)[check_job_name(workflow)])
        if any(VARIABLE in str(value) for value in (step.get("env") or {}).values())
    ]
    assert len(found) == 1, f"expected one step checking {VARIABLE}, found {len(found)}"
    assert "run" in found[0]
    return found[0]


def run_context_check(workflow: dict[Any, Any], tmp_path: Path, value: str | None) -> Any:
    step = context_check_step(workflow)
    return run_script(step["run"], resolved_env(step, {VARIABLE: value}), tmp_path)


# --- running a review step's script with a fake `uv` ----------------------------------------------


@dataclass(frozen=True, slots=True)
class StepCall:
    argv: list[str]  # after `python -m review_bot.pr`
    env: dict[str, str]


def run_review_script(  # noqa: PLR0913 - the step, then where and what it runs with
    workflow: dict[Any, Any],
    mode: str,
    value: str | None,
    tmp_path: Path,
    *,
    event_path: Path | None = None,
    overrides: dict[str, str] | None = None,
) -> StepCall:
    """Run the step's `run:` with bash, its resolved env and a fake `uv` that records its call."""
    fake_bin, runner_temp, log = tmp_path / "bin", tmp_path / "runner-temp", tmp_path / "uv.jsonl"
    fake_bin.mkdir(parents=True)
    runner_temp.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        FAKE_UV.format(python=sys.executable, stdout=FAKE_STDOUT, stderr=FAKE_STDERR),
        encoding="utf-8",
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    env = step_env(workflow, mode, value) | (overrides or {})
    env |= {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_EVENT_PATH": str(event_path or tmp_path / "event.json"),
        "FAKE_UV_LOG": str(log),
    }
    done = run_script(review_step(workflow, mode)["run"], env, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1, f"expected one `uv` call, found {len(records)}"
    argv = records[0]["argv"]
    assert argv[:5] == ["run", "--no-sync", "python", "-m", "review_bot.pr"], argv
    return StepCall(argv[5:], records[0]["env"])


# --- AC1: an unknown REVIEW_BOT_CONTEXT fails the check job, naming it -------------------------


def test_p3_s14_ac1_check_job_reads_the_variable_into_its_env_not_its_script(workflow):
    step = context_check_step(workflow)
    (name,) = [n for n, v in step["env"].items() if VARIABLE in str(v)]
    assert re.fullmatch(r"\$\{\{\s*vars\.REVIEW_BOT_CONTEXT\s*\}\}", step["env"][name])
    assert "${{" not in step["run"]


def test_p3_s14_ac1_mode_check_is_still_there(workflow):
    """The job keeps P2-S3 AC11's mode check beside the new one."""
    steps = steps_of(jobs_of(workflow)[check_job_name(workflow)])
    readers = [
        step
        for step in steps
        if any("vars.REVIEW_BOT_MODE" in str(v) for v in (step.get("env") or {}).values())
    ]
    assert len(readers) == 1
    assert readers[0] is not context_check_step(workflow)


@pytest.mark.parametrize("value", [None, "", "off", "repo"], ids=["unset", "empty", "off", "repo"])
def test_p3_s14_ac1_known_value_passes_the_check_quietly(workflow, tmp_path, value):
    done = run_context_check(workflow, tmp_path, value)

    assert (done.returncode, done.stdout + done.stderr) == (0, "")


def test_p3_s14_ac1_variable_absent_from_env_passes_the_check(workflow, tmp_path):
    done = run_script(context_check_step(workflow)["run"], {}, tmp_path)

    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("value", UNKNOWN_VALUES)
def test_p3_s14_ac1_unknown_value_fails_with_one_error_line_naming_the_value(
    workflow, tmp_path, value
):
    done = run_context_check(workflow, tmp_path, value)

    output = done.stdout + done.stderr
    assert done.returncode != 0
    assert len(output.splitlines()) == 1, output
    assert output.startswith("::error::"), output
    assert value in output, output
    for word in ("REVIEW_BOT_CONTEXT", "unset", "off", "repo"):
        assert word in output, word
    assert not (tmp_path / PWNED).exists()


def test_p3_s14_ac1_both_review_jobs_need_the_check_and_respect_its_failure(workflow):
    check = check_job_name(workflow)
    for mode in REVIEW_MODES:
        job = review_job(workflow, mode)
        needs = job.get("needs", [])
        assert check in ({needs} if isinstance(needs, str) else set(needs)), mode
        assert not STATUS_FUNCTIONS.search(str(job.get("if", ""))), job["if"]


# --- AC2: the step's env, and the cost cap exported only in context mode ----------------------


@pytest.mark.parametrize("mode", REVIEW_MODES)
@pytest.mark.parametrize(
    ("value", "selected"),
    [(None, "off"), ("", "off"), ("off", "off"), ("repo", "repo")],
    ids=["unset", "empty", "off", "repo"],
)
def test_p3_s14_ac2_context_comes_from_the_variable_defaulting_to_off(
    workflow, mode, value, selected
):
    assert step_env(workflow, mode, value)["REVIEW_BOT_CONTEXT"] == selected


@pytest.mark.parametrize("mode", REVIEW_MODES)
def test_p3_s14_ac2_tool_caps_are_literals(workflow, mode):
    env = review_step(workflow, mode)["env"]
    assert {name: env.get(name) for name in TOOL_CAPS} == TOOL_CAPS


@pytest.mark.parametrize("mode", REVIEW_MODES)
@pytest.mark.parametrize(
    ("value", "deadline"),
    [(None, DIFF_DEADLINE_S), ("off", DIFF_DEADLINE_S), ("repo", CONTEXT_DEADLINE_S)],
    ids=["unset", "off", "repo"],
)
def test_p3_s14_ac2_deadline_is_300_for_repo_else_240(workflow, mode, value, deadline):
    assert step_env(workflow, mode, value)["REVIEW_BOT_DEADLINE_S"] == deadline


@pytest.mark.parametrize("mode", REVIEW_MODES)
@pytest.mark.parametrize("value", [None, "off", "repo"], ids=["unset", "off", "repo"])
def test_p3_s14_ac2_phase_2_caps_are_unchanged_but_the_deadline(workflow, mode, value):
    env = step_env(workflow, mode, value)
    expected = CAPS | ({"REVIEW_BOT_DEADLINE_S": CONTEXT_DEADLINE_S} if value == "repo" else {})
    assert {name: env.get(name) for name in CAPS} == expected


def test_p3_s14_ac2_cost_cap_is_no_env_key_anywhere(workflow):
    """An `env:` key can't be omitted, so the cap must come only from the run block."""
    keys = [path for path, node in walk(workflow) if path and path[-1] == COST_CAP]
    assert keys == []


@pytest.mark.parametrize("mode", REVIEW_MODES)
def test_p3_s14_ac2_repo_exports_the_cost_cap_060_to_the_bot(workflow, tmp_path, mode):
    call = run_review_script(workflow, mode, "repo", tmp_path)

    assert call.env[COST_CAP] == CONTEXT_COST_CAP
    assert call.env["REVIEW_BOT_CONTEXT"] == "repo"
    assert call.env["REVIEW_BOT_DEADLINE_S"] == CONTEXT_DEADLINE_S


@pytest.mark.parametrize("mode", REVIEW_MODES)
@pytest.mark.parametrize("value", ["off", ""], ids=["off", "empty"])
def test_p3_s14_ac2_diff_mode_never_sets_the_cost_cap(workflow, tmp_path, mode, value):
    call = run_review_script(workflow, mode, value, tmp_path)

    assert COST_CAP not in call.env
    assert call.env["REVIEW_BOT_CONTEXT"] == "off"


@pytest.mark.parametrize("mode", REVIEW_MODES)
def test_p3_s14_ac2_script_runs_the_same_command_in_both_modes(workflow, tmp_path, mode):
    """The cap is the only difference the script makes; the command is P2's, with this mode."""
    off = run_review_script(workflow, mode, "off", tmp_path / "off")
    repo = run_review_script(workflow, mode, "repo", tmp_path / "repo")

    for call, where in ((off, tmp_path / "off"), (repo, tmp_path / "repo")):
        assert call.argv[0] == "run"
        assert option_value(call.argv, "--mode") == mode
        assert option_value(call.argv, "--repo") == "."
        assert option_value(call.argv, "--event") == str(where / "event.json")
        assert option_value(call.argv, "--out") == f"{where / 'runner-temp'}/review-bot"
    assert off.argv[1:] == [
        word.replace(str(tmp_path / "repo"), str(tmp_path / "off")) for word in repo.argv[1:]
    ]
    assert argv_of(review_step(workflow, mode))[0] == "run"


@pytest.mark.parametrize("mode", REVIEW_MODES)
@pytest.mark.parametrize("value", [None, "", "off", "repo", "Repo", "REPO", "bogus"])
def test_p3_s14_ac2_cap_deadline_and_context_agree_for_every_value_the_check_passes(
    workflow, tmp_path, mode, value
):
    """GitHub's `==` ignores case and the shell's `=` doesn't; the check must close that gap."""
    if run_context_check(workflow, tmp_path, value).returncode != 0:
        assert value not in (None, "", "off", "repo")
        return
    call = run_review_script(workflow, mode, value, tmp_path)

    in_context = call.env["REVIEW_BOT_CONTEXT"] == "repo"
    assert (COST_CAP in call.env) is in_context
    assert (call.env["REVIEW_BOT_DEADLINE_S"] == CONTEXT_DEADLINE_S) is in_context


@pytest.mark.parametrize("mode", REVIEW_MODES)
@pytest.mark.parametrize("hostile", ["$(touch pwned)", "repo; touch pwned", "repo ] || [ x"])
def test_p3_s14_ac2_hostile_context_value_runs_nothing_and_sets_no_cap(
    workflow, tmp_path, mode, hostile
):
    """Defence in depth: the check refuses these, but the script must not act on them either."""
    call = run_review_script(
        workflow, mode, "off", tmp_path, overrides={"REVIEW_BOT_CONTEXT": hostile}
    )

    assert COST_CAP not in call.env
    assert not (tmp_path / PWNED).exists()


# --- AC2 and AC6, end to end: the recorded call through the real CLI ---------------------------


class EnvRecordingProvider(ContextProvider):
    """P3-S8's scripted provider, also recording the process environment when it is called:
    the environment the Claude CLI subprocess would inherit."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.environments: list[dict[str, str]] = []

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        self.environments.append(dict(os.environ))
        return await super().review(request)


@dataclass(frozen=True, slots=True)
class CiRun:
    run: PrRun
    call: StepCall
    provider: EnvRecordingProvider


@pytest.fixture
def ci_run(
    workflow: dict[Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Callable[[str, str | None, EnvRecordingProvider], CiRun]:
    """Run a review step's script, then its recorded argv and env through `pr_cli.main`, in a
    pull request clone at the base, with the head only fetched, as the step finds it."""

    def run(mode: str, value: str | None, provider: EnvRecordingProvider) -> CiRun:
        pull, _ = make_context_pull(tmp_path / "clone", {CHECKOUT: CHECKOUT_AT_C})
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps(event(pull.base, pull.head)), encoding="utf-8")
        call = run_review_script(workflow, mode, value, tmp_path / "step", event_path=event_path)
        for name, setting in call.env.items():
            if name.startswith("REVIEW_BOT_") or name in {"GITHUB_TOKEN", "ANTHROPIC_API_KEY"}:
                monkeypatch.setenv(name, setting)
        # Where context mode would read linked issues; nothing listens, and the event links none.
        monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:9")
        monkeypatch.chdir(pull.repo.path)  # `--repo .`: the step runs in the checkout
        capsys.readouterr()
        exit_code = pr_cli.main([*call.argv, "--provider", "mock"], provider=provider)
        captured = capsys.readouterr()
        out = Path(option_value(call.argv, "--out"))
        return CiRun(PrRun(exit_code, captured.out, captured.err, out), call, provider)

    return run


def test_p3_s14_ac2_repo_run_is_context_mode_with_the_workflow_caps(ci_run):
    provider = EnvRecordingProvider(tool_calls=[READ_AROUND_12], cost_usd=Decimal("0.42"))

    result = ci_run("shadow", "repo", provider)

    run = result.run
    assert run.exit_code == 0, run.stderr
    assert run.report["mode"] == "context"
    (request,) = provider.requests
    assert request.tools is not None
    caps = run.shadow["caps"]
    assert as_decimal(caps["max_cost_usd"]) == Decimal(CONTEXT_COST_CAP)
    assert caps["max_tool_calls"] == 12
    assert caps["max_tool_result_bytes"] == 16_000
    assert caps["max_tool_total_bytes"] == 96_000
    assert float(caps["deadline_s"]) == float(CONTEXT_DEADLINE_S)
    assert run.shadow["over_budget"] is False


def test_p3_s14_ac2_repo_run_over_the_cost_cap_is_over_budget(ci_run):
    """The exported 0.60 is the cap the run enforces, not just a value it reports."""
    provider = EnvRecordingProvider(tool_calls=[READ_AROUND_12], cost_usd=Decimal("0.61"))

    run = ci_run("shadow", "repo", provider).run

    assert run.shadow["over_budget"] is True


@pytest.mark.parametrize("value", [None, "off"], ids=["unset", "off"])
def test_p3_s14_ac6_unset_or_off_variable_is_phase_2s_run(ci_run, value):
    provider = EnvRecordingProvider(tool_calls=[READ_AROUND_12])

    result = ci_run("shadow", value, provider)

    assert COST_CAP not in result.call.env
    assert result.call.env["REVIEW_BOT_CONTEXT"] == "off"
    assert_phase_2_run(result.run, provider)
    caps = result.run.shadow["caps"]
    assert caps.get("max_cost_usd") is None
    assert float(caps["deadline_s"]) == float(DIFF_DEADLINE_S)


def test_p3_s14_ac6_unset_variable_run_writes_phase_2s_caps(ci_run, tmp_path, monkeypatch):
    """The caps a P2-era step env (CAPS alone) gives, against the S14 step env with it unset."""
    provider = EnvRecordingProvider()
    unset = ci_run("shadow", None, provider).run.shadow["caps"]
    for name in [name for name in os.environ if name.startswith("REVIEW_BOT_")]:
        monkeypatch.delenv(name)
    for name, value in CAPS.items():
        monkeypatch.setenv(name, value)
    pull, _ = make_context_pull(tmp_path / "phase-2", {CHECKOUT: CHECKOUT_AT_C})
    event_path = tmp_path / "phase-2-event.json"
    event_path.write_text(json.dumps(event(pull.base, pull.head)), encoding="utf-8")
    out = tmp_path / "phase-2-out"

    exit_code = pr_cli.main(
        ["run", "--event", str(event_path), "--repo", str(pull.repo.path), "--out", str(out),
         "--mode", "shadow", "--provider", "mock"],
        provider=EnvRecordingProvider(),
    )  # fmt: skip

    assert exit_code == 0
    phase_2 = json.loads((out / "shadow.json").read_bytes())["caps"]
    assert unset == phase_2


# --- AC3: permissions -------------------------------------------------------------------------


def test_p3_s14_ac3_every_jobs_permissions_are_exactly_these(workflow):
    jobs = jobs_of(workflow)
    assert set(jobs) == set(PERMISSIONS), "a job was added or removed: review its permissions"
    for name, job in jobs.items():
        assert job.get("permissions") == PERMISSIONS[name], name
    assert workflow.get("permissions") == {"contents": "read"}


@pytest.mark.parametrize("mode", REVIEW_MODES)
def test_p3_s14_ac3_review_step_gets_the_github_token(workflow, mode):
    token = review_step(workflow, mode)["env"]["GITHUB_TOKEN"]
    assert re.fullmatch(r"\$\{\{\s*(secrets\.GITHUB_TOKEN|github\.token)\s*\}\}", token)


def test_p3_s14_ac3_review_jobs_are_the_shadow_and_comment_jobs(workflow):
    """The table above names jobs by id; this ties the ids to the jobs running each mode."""
    assert review_job(workflow, "shadow") is jobs_of(workflow)["shadow"]
    assert review_job(workflow, "comment") is jobs_of(workflow)["comment"]


# --- AC4: head fetched not checked out, no event text in shell, no gate, bot from the base -----


@pytest.mark.parametrize("mode", REVIEW_MODES)
def test_p3_s14_ac4_base_is_checked_out_and_the_head_only_fetched(workflow, mode):
    job = review_job(workflow, mode)
    (checkout,) = steps_using(job, "actions/checkout")
    assert re.fullmatch(
        r"\$\{\{\s*github\.event\.pull_request\.base\.sha\s*\}\}", checkout["with"]["ref"]
    )
    assert checkout["with"]["persist-credentials"] is False
    readers = [
        step
        for step in steps_of(job)
        if any("head.sha" in str(value) for value in (step.get("env") or {}).values())
    ]
    assert len(readers) == 1
    assert re.fullmatch(r'git fetch [^;&|`$]*"\$\w+"', readers[0]["run"].strip())


def test_p3_s14_ac4_no_step_moves_the_work_tree_off_the_base(workflow):
    moves = re.compile(r"\bgit\s+(checkout|switch|reset|worktree|merge|pull|rebase|cherry-pick)\b")
    for job in jobs_of(workflow).values():
        for step in job.get("steps", []):
            assert not moves.search(step.get("run", "")), step.get("name")


def test_p3_s14_ac4_no_run_block_interpolates_an_expression(workflow):
    for job in jobs_of(workflow).values():
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", ""), step.get("name")


def test_p3_s14_ac4_no_job_is_named_gate(workflow):
    for job_id, job in jobs_of(workflow).items():
        assert job_id.casefold() != "gate"
        assert str(job.get("name", "")).strip().casefold() != "gate"


@pytest.mark.parametrize("mode", REVIEW_MODES)
def test_p3_s14_ac4_bot_installs_locked_from_the_base_and_runs_without_syncing(workflow, mode):
    job = review_job(workflow, mode)
    steps = steps_of(job)
    (install,) = [i for i, step in enumerate(steps) if "uv sync" in step.get("run", "")]
    assert steps[install]["run"].split() == ["uv", "sync", "--locked", "--no-dev"]
    (checkout,) = steps_using(job, "actions/checkout")
    review = review_step(workflow, mode)
    assert steps.index(checkout) < install < steps.index(review)
    assert "uv run --no-sync python -m review_bot.pr run" in review["run"]


# --- AC5: credentials reach only the review step; the token never the model -------------------


def test_p3_s14_ac5_credentials_appear_only_in_the_review_and_collect_steps(workflow):
    """Keys and values in every job. The collect step's token is P2-S8 AC9's, unchanged."""
    allowed = set()
    for name, job in jobs_of(workflow).items():
        for index, step in enumerate(steps_of(job)):
            if "review_bot.pr run" in step.get("run", ""):
                allowed |= {
                    (name, "steps", index, "env"),
                    (name, "steps", index, "env", "GITHUB_TOKEN"),
                    (name, "steps", index, "env", "ANTHROPIC_API_KEY"),
                }
            if "review_bot.pr collect" in step.get("run", ""):
                allowed |= {
                    (name, "steps", index, "env"),
                    (name, "steps", index, "env", "GITHUB_TOKEN"),
                }
    holders = set()
    for name, job in jobs_of(workflow).items():
        for path, node in walk(job):
            texts = [str(key) for key in node] if isinstance(node, dict) else []
            texts += [node] if isinstance(node, str) else []
            if any(CREDENTIAL.search(item) for item in texts):
                holders.add((name, *path))
    assert holders <= allowed, sorted(map(str, holders - allowed))
    assert "env" not in workflow
    for job in jobs_of(workflow).values():
        assert "env" not in job


@pytest.mark.parametrize("mode", REVIEW_MODES)
@pytest.mark.parametrize("value", ["off", "repo"])
def test_p3_s14_ac5_both_credentials_reach_the_bot_process(workflow, tmp_path, mode, value):
    call = run_review_script(workflow, mode, value, tmp_path)

    assert call.env["GITHUB_TOKEN"] == TOKEN
    assert call.env["ANTHROPIC_API_KEY"] == API_KEY


@pytest.mark.parametrize("value", [None, "repo"], ids=["unset", "repo"])
def test_p3_s14_ac5_github_token_never_reaches_the_model_process(ci_run, value):
    """The shadow step now holds the token; `take_github_token` keeps it from the model."""
    provider = EnvRecordingProvider(tool_calls=[READ_AROUND_12])

    result = ci_run("shadow", value, provider)

    assert result.run.exit_code == 0, result.run.stderr
    (environment,) = provider.environments
    assert "GITHUB_TOKEN" not in environment
    assert not [name for name, setting in environment.items() if TOKEN in setting]
