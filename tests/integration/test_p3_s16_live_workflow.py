"""P3-S16: live steps run only from a manual dispatch on `main`, with every amount a literal.

Written from the spec's criteria, design.md (Spend and caps) and ADR 0015, in the style of the
P2-S3 tests of `review.yml`. AC1 to AC6 read `.github/workflows/live.yml` with PyYAML; job `if:`
expressions go through P2-S3's small evaluator. Three things are executed rather than read:

- the `smoke` job's diff step, with bash and the real git, in this clone: as written, and with
  its `-- src` pathspec removed, so the 20 KB guard sees a diff over the limit. A clone without
  the two SHAs skips these tests, except in CI, whose full-history checkout must hold them;
- its review step, with bash and a fake `uv` on `PATH` that records each call's argv and
  environment and writes a line to stdout and stderr, so no run reaches a model;
- each recorded `review-bot` command line, through the real parser (`review_bot.cli.parse_args`)
  and, when the commits are here, through `review_bot.cli.main` with `MockProvider`.

AC7 for `pr backfill` is here, through `review_bot.pr.cli.main` with the P2-S13 fixtures; AC7 for
`evals live` is in `tests/evals/test_p3_s16_evals_live_dispatch.py`. AC8 and AC9 are records of
live runs, kept in the test report.
"""

import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from dispatch_support import REFUSED_EVENTS, set_ci
from repo_paths import REPO_ROOT
from test_p2_s3_shadow_workflow import (
    evaluate,
    jobs_of,
    run_script,
    steps_of,
    steps_using,
    triggers_of,
    walk,
)
from test_p2_s13_shadow_backfill import (
    USAGE_EXIT,
    Backfiller,
    History,
    PerPullProvider,
    assert_refused_before_any_call,
)

from review_bot import cli as review_cli
from review_bot.pr import cli as pr_cli
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "live.yml"
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
READ_ONLY = {"contents": "read"}
SMOKE = "smoke"
BACKFILL = "backfill"
# This slice's choice, then later slices': P3-S8 AC11 adds `backfill`; P3-S6 AC10 adds
# `evals-diff` and `evals-context`.
CHOICES = [SMOKE, BACKFILL, "evals-diff", "evals-context"]
# P3-S8 AC11: the backfill job also reads issues (for S9), so S16 AC3's `contents: read` alone
# holds for every other job.
BACKFILL_PERMISSIONS = {"contents": "read", "issues": "read"}
MAIN_REF = "refs/heads/main"
OTHER_REFS = [
    "refs/heads/feat/p3-s16",
    "refs/heads/main-old",
    "refs/tags/v0.4.0",
    "refs/pull/9/merge",
]
SHA = re.compile(r"[0-9a-f]{40}")
MAX_DIFF_BYTES = 20_000  # "under 20 KB"; 20,000 is the stricter reading of KB
PER_REVIEW_CAP = Decimal("0.30")
SMOKE_CAP = Decimal("0.60")  # design.md, Spend and caps: smoke, 2 calls x $0.30
SMOKE_CALLS = 2
MAX_TIMEOUT_MINUTES = 60
SECRET_REFERENCE = re.compile(r"secrets\.(\w+)")
EVENT_OR_INPUT = re.compile(r"\$\{\{[^}]*\b(github\.event\.|inputs\.)")
STEP_CHOICE = re.compile(r"inputs\.step\s*==\s*'([^']*)'")
FAKE_STDOUT = "FAKE-REVIEW-STDOUT"
FAKE_STDERR = "FAKE-REVIEW-STDERR"
# Read once, at import: the autouse fixture below removes CI from every test's environment.
IN_CI = "CI" in os.environ


# --- reading the workflow ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(text: str) -> dict[Any, Any]:
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return loaded


def all_steps(workflow: dict[Any, Any]) -> Iterator[dict[str, Any]]:
    for job in jobs_of(workflow).values():
        yield from job.get("steps", [])


def job_runs(job: dict[str, Any], *, ref: str, step: str) -> bool:
    """Whether the job's `if:` holds for a dispatch of `step` on `ref`."""
    condition = job.get("if", True)
    if condition is True:
        return True
    contexts = {"github.ref": ref, "inputs.step": step, "github.event.inputs.step": step}
    return evaluate(re.sub(r"^\$\{\{|\}\}$", "", str(condition).strip()), contexts)


def smoke_job(workflow: dict[Any, Any]) -> dict[str, Any]:
    """The one job a `smoke` dispatch on `main` runs."""
    found = [job for job in jobs_of(workflow).values() if job_runs(job, ref=MAIN_REF, step=SMOKE)]
    assert len(found) == 1, f"expected one job for the smoke step, found {len(found)}"
    return found[0]


def step_running(job: dict[str, Any], command: str) -> dict[str, Any]:
    found = [step for step in steps_of(job) if command in step.get("run", "")]
    assert len(found) == 1, f"expected one step running {command!r}, found {len(found)}"
    return found[0]


def diff_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    return step_running(smoke_job(workflow), "git diff")


def review_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    return step_running(smoke_job(workflow), "review-bot")


def literal_env(*envs: dict[str, Any] | None) -> dict[str, str]:
    """The env values that are literals, later envs winning, as Actions merges job and step."""
    merged: dict[str, str] = {}
    for env in envs:
        for name, value in (env or {}).items():
            if "${{" not in str(value):
                merged[name] = str(value)
    return merged


def smoke_shas(workflow: dict[Any, Any]) -> tuple[str, str]:
    """The base and head SHAs the diff step names, read from the env it runs with."""
    job, step = smoke_job(workflow), diff_step(workflow)
    env = literal_env(workflow.get("env"), job.get("env"), step.get("env"))
    words = re.findall(r'"?\$\{?(\w+)\}?"?|([0-9a-f]{40})', step["run"].split("git diff", 1)[1])
    shas = [env.get(name, "") if name else literal for name, literal in words]
    shas = [sha for sha in shas if SHA.fullmatch(sha)]
    assert len(shas) == 2, f"expected two literal SHAs in the diff command, found {shas}"
    return shas[0], shas[1]


def has_commits(*shas: str) -> bool:
    return all(
        subprocess.run(  # noqa: S603 - git, with SHAs read from this repo's own workflow
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        ).returncode
        == 0
        for sha in shas
    )


def smoke_commits(workflow: dict[Any, Any]) -> tuple[str, str]:
    """The smoke's two SHAs; in CI (full history) they must be here, elsewhere a skip suffices."""
    base, head = smoke_shas(workflow)
    if not has_commits(base, head):
        message = "this clone doesn't hold the smoke's two commits; fetch the full history"
        if IN_CI:
            pytest.fail(message)
        pytest.skip(message)
    return base, head


# --- running the smoke job's steps --------------------------------------------------------------

FAKE_UV = """#!{python}
import json, os, sys
with open(os.environ["FAKE_UV_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
print("{stdout}")
print("{stderr}", file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_UV_EXIT", "0")))
"""


@dataclass(frozen=True, slots=True)
class ReviewCall:
    argv: list[str]  # after `review-bot`
    env: dict[str, str]


@dataclass(frozen=True, slots=True)
class SmokeRun:
    returncode: int
    output: str
    runner_temp: Path
    calls: list[ReviewCall]


def run_review_step(workflow: dict[Any, Any], tmp_path: Path, exit_code: int = 0) -> SmokeRun:
    """Run the review step with bash, a fake `uv` on PATH and `RUNNER_TEMP` in `tmp_path`."""
    fake_bin, runner_temp, log = tmp_path / "bin", tmp_path / "runner-temp", tmp_path / "uv.jsonl"
    fake_bin.mkdir()
    runner_temp.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        FAKE_UV.format(python=sys.executable, stdout=FAKE_STDOUT, stderr=FAKE_STDERR),
        encoding="utf-8",
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    step = review_step(workflow)
    env = literal_env(workflow.get("env"), smoke_job(workflow).get("env"), step.get("env"))
    env |= {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "RUNNER_TEMP": str(runner_temp),
        "FAKE_UV_LOG": str(log),
        "FAKE_UV_EXIT": str(exit_code),
        # What `${{ secrets.ANTHROPIC_API_KEY }}` becomes on the runner.
        "ANTHROPIC_API_KEY": "test-placeholder-not-a-key",
    }
    (runner_temp / "live").mkdir()
    (runner_temp / "live" / "smoke.diff").write_text("", encoding="utf-8")
    done = run_script(step["run"], env, tmp_path)
    calls = []
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            argv = record["argv"]
            assert "review-bot" in argv, argv
            calls.append(ReviewCall(argv[argv.index("review-bot") + 1 :], record["env"]))
    return SmokeRun(done.returncode, done.stdout + done.stderr, runner_temp, calls)


@pytest.fixture
def smoke_run(workflow: dict[Any, Any], tmp_path: Path) -> SmokeRun:
    run = run_review_step(workflow, tmp_path)
    assert run.returncode == 0, run.output
    return run


def parsed(call: ReviewCall, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any]:
    """The call's diff path and settings, from the real parser with the call's REVIEW_BOT_* env."""
    for name in [name for name in os.environ if name.startswith("REVIEW_BOT_")]:
        monkeypatch.delenv(name)
    for name, value in call.env.items():
        if name.startswith("REVIEW_BOT_"):
            monkeypatch.setenv(name, value)
    return review_cli.parse_args(call.argv)


def under(path: Path, directory: Path) -> bool:
    return path.resolve().is_relative_to(directory.resolve())


# --- AC1: a manual dispatch with one choice, and nothing else ----------------------------------


def test_p3_s16_ac1_only_trigger_is_workflow_dispatch(workflow):
    assert set(triggers_of(workflow)) == {"workflow_dispatch"}


def test_p3_s16_ac1_exactly_one_input_step_a_required_choice(workflow):
    inputs = triggers_of(workflow)["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"step"}
    assert inputs["step"]["type"] == "choice"
    assert inputs["step"]["required"] is True


def test_p3_s16_ac1_options_are_smoke_and_the_later_slices_choices(workflow):
    assert triggers_of(workflow)["workflow_dispatch"]["inputs"]["step"]["options"] == CHOICES


def test_p3_s16_ac1_no_free_form_input_has_a_default_or_other_type(workflow):
    step_input = triggers_of(workflow)["workflow_dispatch"]["inputs"]["step"]
    assert set(step_input) <= {"description", "type", "required", "options", "default"}
    assert step_input.get("default", SMOKE) in CHOICES


def test_p3_s16_ac1_no_other_input_is_read_anywhere(text):
    assert set(re.findall(r"inputs\.(\w+)", text)) <= {"step"}


def test_p3_s16_ac1_every_choice_an_if_compares_is_an_option(workflow):
    compared = {
        choice
        for path, value in walk(workflow)
        if path and path[-1] == "if"
        for choice in STEP_CHOICE.findall(str(value))
    }
    assert compared
    assert compared <= set(CHOICES)


@pytest.mark.parametrize("step", ["", "evals", "backfill-x", "SMOKE-x", "smoke ", "evals-diff "])
def test_p3_s16_ac1_no_job_runs_for_a_value_that_isnt_an_option(workflow, step):
    for name, job in jobs_of(workflow).items():
        assert not job_runs(job, ref=MAIN_REF, step=step), name


@pytest.mark.parametrize("forbidden", ["pull_request", "push", "schedule", "workflow_run"])
def test_p3_s16_ac1_no_other_event_name_appears_in_the_triggers(text, forbidden):
    on_section = text.split("\non:", 1)[1].split("\njobs:", 1)[0]
    assert not re.search(rf"^\s*{forbidden}\s*:", on_section, re.MULTILINE)


# --- AC2: the smoke step --------------------------------------------------------------------------


def test_p3_s16_ac2_smoke_checks_out_main_with_full_history(workflow):
    checkouts = steps_using(smoke_job(workflow), "actions/checkout")
    assert len(checkouts) == 1
    with_ = checkouts[0]["with"]
    assert with_["ref"] in {"main", MAIN_REF}
    assert with_["fetch-depth"] == 0


def test_p3_s16_ac2_diff_is_written_with_git_diff_output(workflow):
    assert re.search(r"\bgit diff\b[^\n]*--output[= ]", diff_step(workflow)["run"])


def test_p3_s16_ac2_both_shas_are_literals_in_the_workflow(workflow):
    base, head = smoke_shas(workflow)
    assert SHA.fullmatch(base)
    assert SHA.fullmatch(head)
    assert base != head


def run_diff_step(
    workflow: dict[Any, Any], script: str, runner_temp: Path
) -> subprocess.CompletedProcess[str]:
    """Run `script` as the diff step, from the repository root, with RUNNER_TEMP `runner_temp`."""
    runner_temp.mkdir(exist_ok=True)
    env = literal_env(workflow.get("env"), smoke_job(workflow).get("env"))
    env |= literal_env(diff_step(workflow).get("env")) | {"RUNNER_TEMP": str(runner_temp)}
    return run_script(script, env, REPO_ROOT)


def test_p3_s16_ac2_diff_step_writes_a_diff_under_20_kb(workflow, tmp_path):
    smoke_commits(workflow)
    runner_temp = tmp_path / "runner-temp"

    done = run_diff_step(workflow, diff_step(workflow)["run"], runner_temp)

    assert done.returncode == 0, done.stdout + done.stderr
    written = [path for path in runner_temp.rglob("*") if path.is_file()]
    assert len(written) == 1, written
    size = written[0].stat().st_size
    assert 0 < size < MAX_DIFF_BYTES, size
    assert written[0].read_bytes().startswith(b"diff --git ")


def test_p3_s16_ac2_diff_holds_only_src(workflow, tmp_path):
    smoke_commits(workflow)
    runner_temp = tmp_path / "runner-temp"

    done = run_diff_step(workflow, diff_step(workflow)["run"], runner_temp)

    assert done.returncode == 0, done.stdout + done.stderr
    (written,) = [path for path in runner_temp.rglob("*") if path.is_file()]
    headers = re.findall(r"^diff --git a/(\S+) b/(\S+)$", written.read_text("utf-8"), re.MULTILINE)
    assert headers
    for old, new in headers:
        assert old.startswith("src/"), old
        assert new.startswith("src/"), new


def test_p3_s16_ac2_diff_of_20_kb_or_more_fails_the_step_naming_its_size(workflow, tmp_path):
    """The whole diff between the same SHAs (about 80 KB) must stop the job before any call."""
    smoke_commits(workflow)
    script = diff_step(workflow)["run"]
    assert " -- src" in script
    runner_temp = tmp_path / "runner-temp"

    done = run_diff_step(workflow, script.replace(" -- src", ""), runner_temp)

    (written,) = [path for path in runner_temp.rglob("*") if path.is_file()]
    size = written.stat().st_size
    assert size >= MAX_DIFF_BYTES, "the test needs a diff over the limit"
    assert done.returncode != 0
    output = done.stdout + done.stderr
    assert str(size) in output, output
    assert "20 KB" in output, output


def test_p3_s16_ac2_no_review_runs_after_a_failed_diff_step(workflow):
    """The review step comes after the diff step, and runs only if every step before succeeded."""
    steps = steps_of(smoke_job(workflow))
    assert steps.index(diff_step(workflow)) < steps.index(review_step(workflow))
    condition = str(review_step(workflow).get("if", "success()"))
    assert condition.strip() in {"success()", "${{ success() }}"}, condition


def test_p3_s16_ac2_head_sha_is_in_mains_history(workflow):
    """`--context-rev` must name a commit of the `main` the job checks out."""
    base, head = smoke_commits(workflow)
    for sha in (base, head):
        done = subprocess.run(  # noqa: S603 - git, with SHAs read from this repo's own workflow
            ["git", "merge-base", "--is-ancestor", sha, "HEAD"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        )
        assert done.returncode == 0, f"{sha} isn't in this branch's history"


def test_p3_s16_ac2_review_bot_runs_exactly_twice(smoke_run):
    assert len(smoke_run.calls) == SMOKE_CALLS


def test_p3_s16_ac2_each_review_reads_the_diff_step_output(workflow, smoke_run, monkeypatch):
    written = re.search(r'--output[= ]"?([^"\s]+)"?', diff_step(workflow)["run"])
    assert written is not None
    diff_path = Path(written.group(1).replace("$RUNNER_TEMP", str(smoke_run.runner_temp)))
    for call in smoke_run.calls:
        path, _ = parsed(call, monkeypatch)
        assert path == diff_path


def test_p3_s16_ac2_each_review_has_context_repo_dot_and_the_head_sha(
    workflow, smoke_run, monkeypatch
):
    _, head = smoke_shas(workflow)
    for call in smoke_run.calls:
        _, settings = parsed(call, monkeypatch)
        assert settings.context_repo == Path()
        assert settings.context_rev == head


def test_p3_s16_ac2_each_review_caps_cost_at_030_with_retries_0(smoke_run, monkeypatch):
    for call in smoke_run.calls:
        _, settings = parsed(call, monkeypatch)
        assert settings.max_cost_usd == PER_REVIEW_CAP
        assert settings.max_retries == 0
        assert settings.provider == "claude"


def test_p3_s16_ac2_flags_are_literals_on_the_command_line(workflow):
    run = review_step(workflow)["run"]
    assert re.search(r"--max-cost-usd\s+0\.30\b", run)
    assert re.search(r"--context-repo\s+\.(\s|$)", run)
    assert "REVIEW_BOT_MAX_COST_USD" not in run


def test_p3_s16_ac2_total_spend_is_at_most_the_design_cap(smoke_run, monkeypatch):
    caps = [parsed(call, monkeypatch)[1].max_cost_usd for call in smoke_run.calls]
    assert None not in caps
    assert sum(caps) <= SMOKE_CAP


def test_p3_s16_ac2_real_cli_runs_each_command_on_the_real_diff(
    workflow, smoke_run, tmp_path, monkeypatch, capsys
):
    """ADR 0015: the real CLI accepts the step's arguments (MockProvider, from the root)."""
    smoke_commits(workflow)
    done = run_diff_step(workflow, diff_step(workflow)["run"], smoke_run.runner_temp)
    assert done.returncode == 0, done.stdout + done.stderr
    monkeypatch.chdir(REPO_ROOT)

    for call in smoke_run.calls:
        parsed(call, monkeypatch)  # sets the call's REVIEW_BOT_* env
        exit_code = review_cli.main(call.argv, provider=MockProvider(findings=[]))

        assert exit_code == 0, capsys.readouterr().err


# --- AC3: main only, one queue, a time limit, read-only ------------------------------------------


@pytest.mark.parametrize("step", CHOICES)
@pytest.mark.parametrize("ref", OTHER_REFS)
def test_p3_s16_ac3_no_job_runs_off_main(workflow, ref, step):
    for name, job in jobs_of(workflow).items():
        assert not job_runs(job, ref=ref, step=step), name


def test_p3_s16_ac3_every_job_if_compares_the_ref_with_main(workflow):
    for name, job in jobs_of(workflow).items():
        condition = str(job.get("if", ""))
        assert re.search(r"github\.ref\s*==\s*'refs/heads/main'", condition), name


def test_p3_s16_ac3_every_job_shares_one_concurrency_group_that_never_cancels(workflow):
    groups = set()
    for name, job in jobs_of(workflow).items():
        concurrency = job.get("concurrency") or workflow.get("concurrency")
        assert isinstance(concurrency, dict), name
        assert concurrency["cancel-in-progress"] is False, name
        groups.add(str(concurrency["group"]))
    assert len(groups) == 1, groups
    workflow_group = (workflow.get("concurrency") or {}).get("group")
    for name, job in jobs_of(workflow).items():
        job_group = (job.get("concurrency") or {}).get("group")
        assert not (job_group and job_group == workflow_group), f"{name} would wait on itself"
    (group,) = groups
    assert "${{" not in group, "a group from an expression could differ between steps"


def test_p3_s16_ac3_every_job_has_a_timeout_of_at_most_60_minutes(workflow):
    for name, job in jobs_of(workflow).items():
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int), name
        assert 0 < timeout <= MAX_TIMEOUT_MINUTES, name


def test_p3_s16_ac3_every_job_has_exactly_contents_read(workflow):
    """As amended by P3-S8 AC11: the `backfill` job's permissions are exactly that AC's."""
    for name, job in jobs_of(workflow).items():
        is_backfill = job_runs(job, ref=MAIN_REF, step=BACKFILL)
        expected = BACKFILL_PERMISSIONS if is_backfill else READ_ONLY
        assert job.get("permissions") == expected, name
    assert workflow.get("permissions", READ_ONLY) == READ_ONLY


def test_p3_s16_ac3_every_checkout_persists_no_credentials(workflow):
    checkouts = [
        step for job in jobs_of(workflow).values() for step in steps_using(job, "actions/checkout")
    ]
    assert checkouts
    for step in checkouts:
        assert step["with"]["persist-credentials"] is False


# --- AC4: the key reaches one step; the choice is read only in `if:` ------------------------------


def test_p3_s16_ac4_the_only_secrets_are_the_api_key_and_backfills_github_token(text):
    """P3-S8 AC11 adds `GITHUB_TOKEN`, for the backfill step only (tested in P3-S8's suite)."""
    assert set(SECRET_REFERENCE.findall(text)) == {"ANTHROPIC_API_KEY", "GITHUB_TOKEN"}


def test_p3_s16_ac4_api_key_is_only_in_the_review_steps_env(workflow):
    """In the smoke job; P3-S8's suite checks the backfill job's one live step the same way."""
    job = smoke_job(workflow)
    job_name = next(name for name, other in jobs_of(workflow).items() if other is job)
    holders = [
        path
        for path, value in walk(workflow)
        if isinstance(value, str) and "secrets." in value and path[:2] == ("jobs", job_name)
    ]
    review_index = steps_of(job).index(review_step(workflow))
    assert holders == [("jobs", job_name, "steps", review_index, "env", "ANTHROPIC_API_KEY")]
    outside_jobs = [
        path
        for path, value in walk(workflow)
        if isinstance(value, str) and "secrets." in value and path[:1] != ("jobs",)
    ]
    assert outside_jobs == []
    assert review_step(workflow)["env"]["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"


def test_p3_s16_ac4_the_key_reaches_the_review_command(smoke_run):
    for call in smoke_run.calls:
        assert call.env.get("ANTHROPIC_API_KEY") == "test-placeholder-not-a-key"


def test_p3_s16_ac4_no_run_block_interpolates_event_or_inputs(workflow):
    for step in all_steps(workflow):
        assert not EVENT_OR_INPUT.search(step.get("run", "")), step.get("name")


def test_p3_s16_ac4_no_env_or_with_carries_event_or_inputs(workflow):
    """A value in `env:` or `with:` reaches the shell too; the choice belongs in `if:` only."""
    for path, value in walk(workflow):
        if isinstance(value, str) and path and path[-1] != "if" and path[0] == "jobs":
            assert not re.search(r"\b(github\.event\.|inputs\.)", value), (path, value)


def test_p3_s16_ac4_choice_is_read_only_in_if(workflow):
    for path, value in walk(workflow):
        if isinstance(value, str) and re.search(r"\binputs\.step\b", value):
            assert path[-1] == "if", path


# --- AC5: one artifact, always, for 90 days -----------------------------------------------------


def upload_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    found = steps_using(smoke_job(workflow), "actions/upload-artifact")
    assert len(found) == 1
    return found[0]


def upload_paths(workflow: dict[Any, Any], runner_temp: Path) -> list[Path]:
    lines = str(upload_step(workflow)["with"]["path"]).splitlines()
    shown = [
        # A function, so a Windows path's backslashes aren't read as escapes.
        re.sub(r"\$\{\{\s*runner\.temp\s*\}\}", lambda _: str(runner_temp), line).strip()
        for line in lines
    ]
    return [Path(line) for line in shown if line]


def covered(path: Path, uploads: list[Path]) -> bool:
    return any(path.resolve() == upload.resolve() or under(path, upload) for upload in uploads)


def test_p3_s16_ac5_one_upload_runs_always_and_is_kept_90_days(workflow):
    step = upload_step(workflow)
    assert step.get("if") == "always()"
    assert step["with"]["retention-days"] == 90


def test_p3_s16_ac5_every_review_captures_stdout_and_stderr_to_uploaded_files(workflow, smoke_run):
    uploads = upload_paths(workflow, smoke_run.runner_temp)
    files = [path for path in smoke_run.runner_temp.rglob("*") if path.is_file()]
    holding_stdout = [p for p in files if FAKE_STDOUT in p.read_text(encoding="utf-8")]
    holding_stderr = [p for p in files if FAKE_STDERR in p.read_text(encoding="utf-8")]
    assert len(holding_stdout) == SMOKE_CALLS, holding_stdout
    assert len(holding_stderr) == SMOKE_CALLS, holding_stderr
    for path in [*holding_stdout, *holding_stderr]:
        assert covered(path, uploads), path


def test_p3_s16_ac5_each_reviews_output_goes_to_its_own_files(smoke_run):
    files = [path for path in smoke_run.runner_temp.rglob("*") if path.is_file()]
    for marker in (FAKE_STDOUT, FAKE_STDERR):
        for path in files:
            assert path.read_text(encoding="utf-8").count(marker) <= 1, path


def test_p3_s16_ac5_every_audit_log_is_uploaded(workflow, smoke_run, monkeypatch):
    uploads = upload_paths(workflow, smoke_run.runner_temp)
    audit_logs = {parsed(call, monkeypatch)[1].audit_log for call in smoke_run.calls}
    for audit_log in audit_logs:
        assert audit_log.is_absolute(), "a relative audit log lands in the checkout, not the upload"
        assert covered(audit_log, uploads), audit_log


def test_p3_s16_ac5_a_failed_review_still_writes_its_output_and_fails_the_step(workflow, tmp_path):
    """An exit past 1 is a failed review: the job fails, and the always() upload still has it."""
    run = run_review_step(workflow, tmp_path, exit_code=12)

    assert run.returncode != 0
    files = [path for path in run.runner_temp.rglob("*") if path.is_file()]
    assert any(FAKE_STDERR in path.read_text(encoding="utf-8") for path in files)


# --- AC6: never the required check --------------------------------------------------------------


def test_p3_s16_ac6_no_job_is_named_gate(workflow):
    for job_id, job in jobs_of(workflow).items():
        assert job_id.casefold() != "gate"
        assert str(job.get("name", "")).strip().casefold() != "gate"


def test_p3_s16_ac6_ci_yml_still_holds_the_gate_job():
    ci = yaml.safe_load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert ci["jobs"]["gate"].get("name", "gate") == "gate"


def test_p3_s16_ac6_live_yml_can_never_report_on_a_pull_request(workflow):
    """A required check must report on every PR; a dispatch-only workflow never does."""
    assert "pull_request" not in triggers_of(workflow)
    assert "pull_request_target" not in triggers_of(workflow)


# --- AC7: pr backfill runs in CI only for a dispatch given --allow-dispatch ----------------------


@pytest.fixture
def history(tmp_path: Path) -> History:
    return History(tmp_path / "clone")


@pytest.fixture
def backfill(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> Backfiller:
    return Backfiller(capsys, tmp_path)


@pytest.fixture(autouse=True)
def no_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test sets CI and GITHUB_EVENT_NAME only on purpose."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)


def test_p3_s16_ac7_backfill_runs_in_ci_for_a_dispatch_with_the_flag(
    backfill, history, monkeypatch
):
    set_ci(monkeypatch, "workflow_dispatch")
    pulls = history.merged_many(81, 82)
    provider = PerPullProvider()

    run = backfill.run(pulls, history.repo.path, "--allow-dispatch", provider=provider)

    assert run.exit_code == 0, run.stderr
    assert len(provider.requests) == 2
    assert run.shadow(81)["outcome"] == "reviewed"


@pytest.mark.parametrize("ci", ["true", "", "false"])
def test_p3_s16_ac7_backfill_with_ci_set_to_any_value_and_the_flag_runs_for_a_dispatch(
    backfill, history, monkeypatch, ci
):
    set_ci(monkeypatch, "workflow_dispatch", ci=ci)
    provider = PerPullProvider()

    run = backfill.run(
        history.merged_many(83), history.repo.path, "--allow-dispatch", provider=provider
    )

    assert run.exit_code == 0, run.stderr
    assert len(provider.requests) == 1


@pytest.mark.parametrize("event", REFUSED_EVENTS)
def test_p3_s16_ac7_backfill_with_the_flag_refuses_any_other_ci_event(
    backfill, history, monkeypatch, event
):
    set_ci(monkeypatch, event)
    provider = PerPullProvider()

    run = backfill.run(
        history.merged_many(84), history.repo.path, "--allow-dispatch", provider=provider
    )

    assert_refused_before_any_call(run, provider, USAGE_EXIT)
    assert not run.out.exists(), "a refused backfill creates no --out"
    assert re.search(r"\bCI\b", run.stderr), run.stderr


@pytest.mark.parametrize("ci", ["true", "", "false"])
def test_p3_s16_ac7_backfill_for_a_dispatch_without_the_flag_refuses(
    backfill, history, monkeypatch, ci
):
    set_ci(monkeypatch, "workflow_dispatch", ci=ci)
    provider = PerPullProvider()

    run = backfill.run(history.merged_many(85), history.repo.path, provider=provider)

    assert_refused_before_any_call(run, provider, USAGE_EXIT)
    assert not run.out.exists()


@pytest.mark.parametrize("event", [None, "workflow_dispatch", "pull_request", "push"])
@pytest.mark.parametrize("flag", [[], ["--allow-dispatch"]], ids=["no-flag", "flag"])
def test_p3_s16_ac7_backfill_without_ci_runs_whatever_the_flag_and_event(
    backfill, history, monkeypatch, event, flag
):
    if event is not None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    provider = PerPullProvider()

    run = backfill.run(history.merged_many(86, 87), history.repo.path, *flag, provider=provider)

    assert run.exit_code == 0, run.stderr
    assert len(provider.requests) == 2
    assert [path.parent.name for path in run.reports()] == ["86", "87"]


def test_p3_s16_limits_dispatched_backfill_still_refuses_over_its_budget(
    backfill, history, monkeypatch
):
    """The dispatch passes the CI guard only; the command's own pre-flight still applies."""
    set_ci(monkeypatch, "workflow_dispatch")
    provider = PerPullProvider()

    run = backfill.run(
        history.merged_many(88, 89),
        history.repo.path,
        "--allow-dispatch",
        max_calls=1,
        provider=provider,
    )

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []


def test_p3_s16_interfaces_pr_run_rejects_allow_dispatch(capsys):
    """Interfaces: the flag is on `evals live` and `pr backfill`; on `pr run` it's a usage error."""
    argv = ["run", "--event", "e.json", "--repo", ".", "--out", "o", "--allow-dispatch"]

    assert pr_cli.main(argv, provider=MockProvider()) == USAGE_EXIT
    assert "--allow-dispatch" in capsys.readouterr().err
