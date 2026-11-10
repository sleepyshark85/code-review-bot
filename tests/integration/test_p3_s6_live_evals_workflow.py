"""P3-S6 AC10 and AC11: the `evals-diff` and `evals-context` steps of `live.yml`.

Written from the spec's literals and design.md's "Spend and caps" table, as P3-S16's and P3-S8's
suites read the workflow: PyYAML, P2-S3's helpers and `if:` evaluator, and each job's live step run
with bash and a fake `uv` on `PATH` that records its argv and environment, so the arguments checked
are exactly what the step would pass and no run reaches a model.

AC11 hands each step's argv to the real `evals` CLI (`review_bot.evals.main`) in an empty working
directory with CI set for a `push` event: a parse that succeeds reaches the CI refusal (exit 2)
before any case is read, so `tests/evals/cases`, which doesn't exist there, is never loaded and the
case IDs S9 hasn't added yet don't matter. A control shows a parse failure looks different.

P3-S16's AC3, AC4 (no interpolation), and AC6 checks run over every job in its own suite, these two
included; the per-job parts of AC4 and AC5 are checked here for each step.
"""

import fnmatch
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml
from dispatch_support import set_ci
from repo_paths import REPO_ROOT
from test_p2_s3_shadow_workflow import (
    jobs_of,
    run_script,
    steps_of,
    steps_using,
    triggers_of,
    walk,
)
from test_p3_s16_live_workflow import (
    FAKE_STDERR,
    FAKE_STDOUT,
    FAKE_UV,
    MAIN_REF,
    READ_ONLY,
    job_runs,
)

from review_bot.evals import main as evals_main

pytestmark = pytest.mark.integration

WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "live.yml"
USAGE_EXIT = 2
EVALS_DIFF = "evals-diff"
EVALS_CONTEXT = "evals-context"
# design.md, Spend and caps: the 9 new context cases, in the table's order.
CASE_IDS = [
    "sample-context",
    "clean-context",
    "injection-tool-result",
    "injection-instruction-context",
    "injection-canary-context",
    "injection-delimiter-context",
    "ticket-criteria",
    "injection-issue",
    "domain-money",
]


@dataclass(frozen=True, slots=True)
class EvalStep:
    choice: str
    mode: str
    max_usd: str
    max_cost_usd: str
    recordings: str  # each case's recording directory, under recordings/

    @property
    def argv(self) -> list[str]:
        """AC10's arguments after `python -m review_bot.evals`."""
        cases = [arg for case_id in CASE_IDS for arg in ("--case", case_id)]
        return [
            "live", "--cases", "tests/evals/cases", "--mode", self.mode, "--runs", "3",
            "--max-calls", "27", "--max-usd", self.max_usd, "--max-cost-usd", self.max_cost_usd,
            *cases, "--allow-dispatch",
        ]  # fmt: skip


STEPS = [
    EvalStep(EVALS_DIFF, "diff", "2.70", "0.10", "live"),
    EvalStep(EVALS_CONTEXT, "context", "8.10", "0.30", "live-context"),
]
STEP_IDS = [step.choice for step in STEPS]
CI_REFUSAL = re.compile(r"workflow_dispatch.*--allow-dispatch|--allow-dispatch.*workflow_dispatch")


@pytest.fixture(autouse=True)
def no_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test sets CI and GITHUB_EVENT_NAME only on purpose."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def eval_job(workflow: dict[Any, Any], choice: str) -> tuple[str, dict[str, Any]]:
    """The one job a dispatch of `choice` on `main` runs."""
    found = [
        (name, job)
        for name, job in jobs_of(workflow).items()
        if job_runs(job, ref=MAIN_REF, step=choice)
    ]
    assert len(found) == 1, f"expected one job for {choice}, found {[n for n, _ in found]}"
    return found[0]


def live_step(workflow: dict[Any, Any], choice: str) -> dict[str, Any]:
    _, job = eval_job(workflow, choice)
    found = [step for step in steps_of(job) if "review_bot.evals" in step.get("run", "")]
    assert len(found) == 1, found
    return found[0]


@dataclass(frozen=True, slots=True)
class StepRun:
    returncode: int
    output: str
    runner_temp: Path
    argv: list[str]  # after `review_bot.evals`
    env: dict[str, str]


def run_live_step(
    workflow: dict[Any, Any], choice: str, tmp_path: Path, exit_code: int = 0
) -> StepRun:
    """Run the step with bash, a fake `uv` on PATH and `RUNNER_TEMP` in `tmp_path`."""
    fake_bin, runner_temp, log = tmp_path / "bin", tmp_path / "runner-temp", tmp_path / "uv.jsonl"
    workspace = tmp_path / "workspace"
    for directory in (fake_bin, runner_temp, workspace):
        directory.mkdir(parents=True)
    uv = fake_bin / "uv"
    uv.write_text(
        FAKE_UV.format(python=sys.executable, stdout=FAKE_STDOUT, stderr=FAKE_STDERR),
        encoding="utf-8",
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    step = live_step(workflow, choice)
    env = {k: str(v) for k, v in (step.get("env") or {}).items() if "${{" not in str(v)}
    env |= {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "RUNNER_TEMP": str(runner_temp),
        "FAKE_UV_LOG": str(log),
        "FAKE_UV_EXIT": str(exit_code),
        "ANTHROPIC_API_KEY": "fake-key-for-the-test",
    }
    done = run_script(step["run"], env, workspace)
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1, records
    argv = records[0]["argv"]
    assert "review_bot.evals" in argv, argv
    return StepRun(
        done.returncode,
        done.stdout + done.stderr,
        runner_temp,
        argv[argv.index("review_bot.evals") + 1 :],
        records[0]["env"],
    )


def upload_step(workflow: dict[Any, Any], choice: str) -> dict[str, Any]:
    _, job = eval_job(workflow, choice)
    (upload,) = steps_using(job, "actions/upload-artifact")
    return upload


def upload_lines(workflow: dict[Any, Any], choice: str) -> list[str]:
    lines = str(upload_step(workflow, choice)["with"]["path"]).splitlines()
    return [line.strip() for line in lines if line.strip()]


def covers(pattern: str, path: str) -> bool:
    """Whether the upload line `pattern` (a path or glob, relative to the workspace) holds `path`.

    A line is a file or a directory; each segment is matched by itself, so `*` stays in one.
    """
    wanted = PurePosixPath(pattern.rstrip("/")).parts
    parts = PurePosixPath(path).parts
    return len(parts) >= len(wanted) and all(
        fnmatch.fnmatchcase(part, glob) for part, glob in zip(parts, wanted, strict=False)
    )


def covered_in_runner_temp(path: Path, lines: list[str], runner_temp: Path) -> bool:
    for line in lines:
        shown = re.sub(r"\$\{\{\s*runner\.temp\s*\}\}", lambda _: str(runner_temp), line)
        if shown != line:
            upload = Path(shown).resolve()
            resolved = path.resolve()
            if resolved == upload or resolved.is_relative_to(upload):
                return True
    return False


# --- AC10: the two choices and their jobs -------------------------------------------------------


def test_p3_s6_ac10_step_choice_adds_evals_diff_and_evals_context(workflow):
    options = triggers_of(workflow)["workflow_dispatch"]["inputs"]["step"]["options"]
    assert EVALS_DIFF in options
    assert EVALS_CONTEXT in options


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac10_live_step_runs_exactly_the_spec_arguments(workflow, tmp_path, step):
    run = run_live_step(workflow, step.choice, tmp_path)

    assert run.returncode == 0, run.output
    assert run.argv == step.argv


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac10_planned_calls_times_the_cap_is_the_design_tables_allowance(step):
    """27 calls (9 cases x 3 runs) x the per-call cap is exactly the step's line of the $20."""
    assert len(CASE_IDS) * 3 == 27
    assert Decimal(27) * Decimal(step.max_cost_usd) == Decimal(step.max_usd)


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac10_live_step_runs_with_retries_0(workflow, tmp_path, step):
    run = run_live_step(workflow, step.choice, tmp_path)

    assert "--max-retries" not in run.argv, "evals live refuses --max-retries (P1-S16)"
    assert run.env.get("REVIEW_BOT_MAX_RETRIES") == "0"


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac10_amounts_are_literals_on_the_command_line(workflow, step):
    run = live_step(workflow, step.choice)["run"]
    assert re.search(rf"--max-usd\s+{re.escape(step.max_usd)}(\s|$)", run)
    assert re.search(rf"--max-cost-usd\s+{re.escape(step.max_cost_usd)}(\s|$)", run)
    assert re.search(r"--max-calls\s+27(\s|$)", run)
    assert "${{" not in run


# P3-S16 AC3 for these jobs.


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac10_s16_ac3_job_runs_on_main_only_read_only_and_bounded(workflow, step):
    name, job = eval_job(workflow, step.choice)
    assert re.search(r"github\.ref\s*==\s*'refs/heads/main'", str(job.get("if", ""))), name
    assert not job_runs(job, ref="refs/heads/feat/p3-s6", step=step.choice)
    assert job.get("permissions") == READ_ONLY
    assert 0 < job["timeout-minutes"] <= 60
    concurrency = job.get("concurrency") or workflow.get("concurrency")
    assert concurrency["cancel-in-progress"] is False
    (checkout,) = steps_using(job, "actions/checkout")
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["ref"] in {"main", MAIN_REF}


def test_p3_s6_ac10_s16_ac3_evals_jobs_share_the_other_jobs_concurrency_group(workflow):
    groups = {
        str((job.get("concurrency") or workflow.get("concurrency") or {}).get("group"))
        for job in jobs_of(workflow).values()
    }
    assert len(groups) == 1, groups


# P3-S16 AC4 for these jobs.


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac10_s16_ac4_api_key_reaches_only_the_live_step(workflow, step):
    name, job = eval_job(workflow, step.choice)
    index = steps_of(job).index(live_step(workflow, step.choice))
    holders = [
        path
        for path, value in walk(workflow)
        if isinstance(value, str) and "secrets." in value and path[:2] == ("jobs", name)
    ]
    assert holders == [("jobs", name, "steps", index, "env", "ANTHROPIC_API_KEY")]


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac10_s16_ac4_choice_is_read_only_in_the_jobs_if(workflow, step):
    name, job = eval_job(workflow, step.choice)
    for path, value in walk(job):
        if isinstance(value, str) and re.search(r"\b(inputs|github\.event)\.", value):
            assert path == ("if",), (name, path)


# P3-S16 AC5 for these jobs: the artifact holds the output, the audit log and the recordings.


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac10_s16_ac5_upload_runs_always_and_is_kept_90_days(workflow, step):
    upload = upload_step(workflow, step.choice)
    assert upload.get("if") == "always()"
    assert upload["with"]["retention-days"] == 90


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac10_s16_ac5_artifact_holds_the_captured_stdout_and_stderr(workflow, tmp_path, step):
    run = run_live_step(workflow, step.choice, tmp_path)
    lines = upload_lines(workflow, step.choice)

    files = [path for path in run.runner_temp.rglob("*") if path.is_file()]
    for marker in (FAKE_STDOUT, FAKE_STDERR):
        holding = [p for p in files if marker in p.read_text(encoding="utf-8")]
        assert len(holding) == 1, (marker, holding)
        assert covered_in_runner_temp(holding[0], lines, run.runner_temp), (holding, lines)


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac10_s16_ac5_artifact_holds_every_recording_written(workflow, step):
    """The command runs in the workspace, so each case's recording lands under it."""
    lines = upload_lines(workflow, step.choice)
    for case_id in CASE_IDS:
        path = f"tests/evals/cases/{case_id}/recordings/{step.recordings}/recording.json"
        assert any(covers(line, path) for line in lines), (path, lines)


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac10_s16_ac5_artifact_holds_the_live_audit_log(workflow, step):
    """P1-S16 AC12: `evals live` appends to `.review-bot/eval-live-audit.jsonl` under its cwd."""
    lines = upload_lines(workflow, step.choice)
    assert any(covers(line, ".review-bot/eval-live-audit.jsonl") for line in lines), lines


def test_p3_s6_ac10_s16_ac5_each_mode_uploads_its_own_recordings_only(workflow):
    """Diff mode's artifact holds `live/` recordings, context mode's `live-context/`."""
    diff_lines = upload_lines(workflow, EVALS_DIFF)
    context_lines = upload_lines(workflow, EVALS_CONTEXT)
    context_file = "tests/evals/cases/sample-context/recordings/live-context/recording.json"
    diff_file = "tests/evals/cases/sample-context/recordings/live/recording.json"
    assert not any(covers(line, context_file) for line in diff_lines), diff_lines
    assert not any(covers(line, diff_file) for line in context_lines), context_lines


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac10_s16_ac5_a_failed_run_fails_the_step_and_keeps_its_output(
    workflow, tmp_path, step
):
    run = run_live_step(workflow, step.choice, tmp_path, exit_code=1)

    assert run.returncode != 0
    files = [path for path in run.runner_temp.rglob("*") if path.is_file()]
    assert any(FAKE_STDERR in path.read_text(encoding="utf-8") for path in files)


# P3-S16 AC6 for these jobs.


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac10_s16_ac6_job_isnt_named_gate(workflow, step):
    name, job = eval_job(workflow, step.choice)
    assert name.casefold() != "gate"
    assert str(job.get("name", "")).strip().casefold() != "gate"


# --- AC11: the real parser accepts each step's arguments, with no call and no case loaded -------


class NoCallSdk:
    """An `sdk_call` that fails the test if anything calls it."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str, options: object) -> Any:
        self.calls += 1
        raise AssertionError("a call was made")


def parse_in_ci(
    argv: list[str], cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, NoCallSdk]:
    """`evals` main in an empty `cwd`, in CI for a `push`: a parsed command stops at the guard."""
    set_ci(monkeypatch, "push")
    monkeypatch.chdir(cwd)
    sdk = NoCallSdk()
    try:
        exit_code = evals_main(argv, sdk_call=sdk)
    except SystemExit as exc:  # argparse reports usage errors this way
        exit_code = exc.code if isinstance(exc.code, int) else USAGE_EXIT
    return exit_code, sdk


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac11_real_parser_accepts_the_steps_arguments_with_no_call_or_case_loaded(
    workflow, tmp_path, monkeypatch, capsys, step
):
    argv = run_live_step(workflow, step.choice, tmp_path / "step").argv
    empty = tmp_path / "empty"
    empty.mkdir()
    capsys.readouterr()

    exit_code, sdk = parse_in_ci(argv, empty, monkeypatch)

    stderr = capsys.readouterr().err
    assert exit_code == USAGE_EXIT, stderr
    assert CI_REFUSAL.search(stderr), stderr
    assert sdk.calls == 0
    # `tests/evals/cases` doesn't exist here: loading it would fail with a different error.
    assert "tests" not in stderr.replace("--", ""), stderr
    assert list(empty.iterdir()) == [], "nothing is read or written before the guard"


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p3_s6_ac11_control_an_argument_the_parser_rejects_isnt_the_ci_refusal(
    workflow, tmp_path, monkeypatch, capsys, step
):
    argv = run_live_step(workflow, step.choice, tmp_path / "step").argv
    empty = tmp_path / "empty"
    empty.mkdir()
    capsys.readouterr()

    exit_code, _ = parse_in_ci([*argv, "--no-such-flag"], empty, monkeypatch)

    stderr = capsys.readouterr().err
    assert exit_code == USAGE_EXIT
    assert "--no-such-flag" in stderr
    assert not CI_REFUSAL.search(stderr), stderr
