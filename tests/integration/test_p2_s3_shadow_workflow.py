r"""P2-S3: a shadow review on every same-repo PR, from an Action that posts nothing.

Written from the spec's criteria, design.md (Safety on the PR path, CI settings and caps) and ADRs
0008, 0010 and 0012. AC1 to AC11 read `.github/workflows/review.yml` with PyYAML. Two things are
executed rather than read:

- the shell scripts of the mode check (AC11) and the summary step (AC8), run with bash;
- the run step's command line and caps (AC5, AC7), passed to `review_bot.pr.cli.main` with the
  step's `env` and a broken event file, so a bad flag or cap exits 2 and a good one gets as far
  as the event (exit 19). No run reaches a model or GitHub.

Job `if:` expressions are evaluated by a small evaluator for the subset GitHub's expression
language they use: context names, string literals, `==`, `!=`, `&&`, `||`, `!` and parentheses.
Like GitHub, it compares strings case-insensitively and reads an unset variable as `''`.
"""

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from repo_paths import REPO_ROOT

from review_bot.pr import cli
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "review.yml"
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SHADOW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
LATER_ACTIONS = {"closed"}  # S8
READ_ONLY = {"contents": "read"}
REPO = "octo/review-bot"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
NUMBER = 7
EVENT_ERROR_EXIT = 19
CONFIG_ERROR_EXIT = 2
OUT_DIR = "$RUNNER_TEMP/review-bot"
REPORT_FILES = ("shadow.json", "shadow.md", "audit.jsonl")
STATUS_FUNCTIONS = re.compile(r"\b(always|failure|cancelled|success)\s*\(")
EVENT_FIELD = re.compile(r"github\.event\b[\w.\[\]'\"*-]*")
# The design's caps table, as the run step's environment (design.md, CI settings and caps).
CAPS = {
    "REVIEW_BOT_MAX_RETRIES": "0",
    "REVIEW_BOT_MAX_DIFF_BYTES": "400000",
    "REVIEW_BOT_MAX_REVIEW_BYTES": "120000",
    "REVIEW_BOT_MAX_OUTPUT_TOKENS": "16000",
    "REVIEW_BOT_DEADLINE_S": "240",
}


# --- reading the workflow ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(text: str) -> dict[Any, Any]:
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return loaded


def triggers_of(workflow: dict[Any, Any]) -> dict[str, Any]:
    """The `on:` section, which YAML 1.1 reads as the boolean key `True`."""
    keys = [key for key in ("on", True) if key in workflow]
    assert len(keys) == 1, f"expected one `on:` key, found {keys}"
    on = workflow[keys[0]]
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    assert isinstance(on, dict)
    return on


def jobs_of(workflow: dict[Any, Any]) -> dict[str, dict[str, Any]]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def review_job(workflow: dict[Any, Any], mode: str) -> dict[str, Any]:
    """The one job whose step runs `review_bot.pr run --mode <mode>` (P2-S6 adds `comment`)."""
    found = [
        job
        for job in jobs_of(workflow).values()
        if (step := run_step_in(job)) is not None and option_value(argv_of(step), "--mode") == mode
    ]
    assert len(found) == 1, f"expected one job running `--mode {mode}`, found {len(found)}"
    return found[0]


def shadow_job(workflow: dict[Any, Any]) -> dict[str, Any]:
    return review_job(workflow, "shadow")


def check_job_name(workflow: dict[Any, Any]) -> str:
    """The job whose step reads `vars.REVIEW_BOT_MODE` into its env, to check it."""
    found = [
        name
        for name, job in jobs_of(workflow).items()
        if any(
            "vars.REVIEW_BOT_MODE" in str(value)
            for step in job.get("steps", [])
            for value in (step.get("env") or {}).values()
        )
    ]
    assert len(found) == 1, f"expected one mode-check job, found {found}"
    return found[0]


def steps_of(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job["steps"]
    assert isinstance(steps, list)
    return steps


def run_step_in(job: dict[str, Any]) -> dict[str, Any] | None:
    found = [step for step in job.get("steps", []) if "review_bot.pr run" in step.get("run", "")]
    assert len(found) <= 1
    return found[0] if found else None


def collect_step_in(job: dict[str, Any]) -> dict[str, Any] | None:
    """The step running `python -m review_bot.pr collect` (P2-S8), if the job has one."""
    found = [
        step for step in job.get("steps", []) if "review_bot.pr collect" in step.get("run", "")
    ]
    assert len(found) <= 1
    return found[0] if found else None


def collect_job_name(workflow: dict[Any, Any]) -> str:
    """The one job with a `collect` step (P2-S8)."""
    found = [name for name, job in jobs_of(workflow).items() if collect_step_in(job) is not None]
    assert len(found) == 1, f"expected one job running `collect`, found {found}"
    return found[0]


def run_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    step = run_step_in(shadow_job(workflow))
    assert step is not None
    return step


def steps_using(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [step for step in steps_of(job) if step.get("uses", "").startswith(f"{action}@")]


def all_jobs_steps(workflow: dict[Any, Any]) -> Iterator[dict[str, Any]]:
    for job in jobs_of(workflow).values():
        yield from job.get("steps", [])


def walk(node: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], Any]]:
    """Every (key path, value) pair in the YAML tree, scalars included."""
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, (*path, index))


def same_dir(path: str) -> str:
    """A path with the runner's temp directory written one way, in shell or in an expression."""
    return re.sub(r"\$\{\{\s*runner\.temp\s*\}\}", "$RUNNER_TEMP", path).strip().strip('"')


def argv_of(step: dict[str, Any]) -> list[str]:
    """A run step's arguments after `python -m review_bot.pr`, as the shell would split them."""
    words = shlex.split(step["run"])
    start = words.index("review_bot.pr")
    return words[start + 1 :]


def run_command_argv(workflow: dict[Any, Any]) -> list[str]:
    return argv_of(run_step(workflow))


def option_value(argv: list[str], option: str) -> str:
    assert argv.count(option) == 1, f"expected one {option} in {argv}"
    return argv[argv.index(option) + 1]


# --- a small evaluator for GitHub's `if:` expressions ------------------------------------------

_TOKEN = re.compile(r"\s*(?:('(?:[^']|'')*')|(==|!=|&&|\|\||!|\(|\))|([A-Za-z_][\w.\-]*))")


def evaluate(expression: str, contexts: dict[str, Any]) -> bool:
    tokens: list[str] = []
    position, expression = 0, expression.strip()
    while position < len(expression):
        match = _TOKEN.match(expression, position)
        assert match is not None, f"can't read {expression[position:]!r}"
        tokens.append(match.group(match.lastindex or 0))
        position = match.end()
    parser = _Parser(tokens, contexts)
    value = parser.either()
    assert parser.done(), f"unread tokens in {expression!r}"
    return _truthy(value)


class _Parser:
    def __init__(self, tokens: list[str], contexts: dict[str, Any]) -> None:
        self._tokens, self._contexts, self._at = tokens, contexts, 0

    def done(self) -> bool:
        return self._at == len(self._tokens)

    def _peek(self) -> str | None:
        return self._tokens[self._at] if self._at < len(self._tokens) else None

    def _take(self) -> str:
        token = self._tokens[self._at]
        self._at += 1
        return token

    def either(self) -> Any:
        value = self.both()
        while self._peek() == "||":
            self._take()
            right = self.both()
            value = value if _truthy(value) else right
        return value

    def both(self) -> Any:
        value = self.negation()
        while self._peek() == "&&":
            self._take()
            right = self.negation()
            value = right if _truthy(value) else value
        return value

    def negation(self) -> Any:
        if self._peek() == "!":
            self._take()
            return not _truthy(self.negation())
        return self.comparison()

    def comparison(self) -> Any:
        left = self.primary()
        if self._peek() in {"==", "!="}:
            operator, right = self._take(), self.primary()
            equal = _loose_equal(left, right)
            return equal if operator == "==" else not equal
        return left

    def primary(self) -> Any:
        word = self._take()
        if word == "(":
            value = self.either()
            assert self._take() == ")"
            return value
        if word.startswith("'"):
            return word[1:-1].replace("''", "'")
        assert self._peek() != "(", f"function {word}() isn't supported by this evaluator"
        assert word in self._contexts, f"the expression reads {word}, which the test doesn't set"
        return self._contexts[word]


def _truthy(value: Any) -> bool:
    return value not in (None, "", False, 0)


def _loose_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return (left or "") == (right or "")
    return str(left).casefold() == str(right).casefold()


def shadow_runs(workflow: dict[Any, Any], *, head_repo: str | None, mode: str) -> bool:
    return job_runs(shadow_job(workflow), head_repo=head_repo, mode=mode)


def job_runs(
    job: dict[str, Any], *, head_repo: str | None, mode: str, action: str = "synchronize"
) -> bool:
    """Whether the job's `if:` holds for an event with this action, head repo and mode."""
    condition = job.get("if", True)
    if condition is True:
        return True
    contexts = {
        "github.event.pull_request.head.repo.full_name": head_repo,
        "github.repository": REPO,
        "github.event.action": action,
        "vars.REVIEW_BOT_MODE": mode,
    }
    return evaluate(re.sub(r"^\$\{\{|\}\}$", "", str(condition).strip()), contexts)


# --- running a step's script ---------------------------------------------------------------------


def bash_or_skip() -> str:
    bash = shutil.which("bash")
    if bash is None or "system32" in bash.casefold():
        pytest.skip("needs a POSIX bash on PATH (WSL's launcher doesn't count) to run the script")
    return bash


def run_script(script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a step's script as Actions does on Linux: `bash -e`, stderr folded into stdout."""
    environment = {"PATH": os.environ.get("PATH", ""), **env}
    return subprocess.run(  # noqa: S603 - a script from this repo's own workflow
        [bash_or_skip(), "-e", "-c", script],
        env=environment,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


# --- AC1: trigger --------------------------------------------------------------------------------


def test_p2_s3_ac1_only_event_is_pull_request(workflow):
    assert set(triggers_of(workflow)) == {"pull_request"}


@pytest.mark.parametrize("forbidden", ["pull_request_target", "workflow_run"])
def test_p2_s3_ac1_forbidden_event_names_appear_nowhere(text, forbidden):
    assert forbidden not in text


def test_p2_s3_ac1_shadow_job_runs_only_for_the_four_actions(workflow):
    """The trigger lists the four actions; any later type (S8's `closed`) must be filtered out."""
    types = set(triggers_of(workflow)["pull_request"]["types"])
    assert types >= SHADOW_ACTIONS
    assert types <= SHADOW_ACTIONS | LATER_ACTIONS
    for action in types:
        runs = job_runs(shadow_job(workflow), head_repo=REPO, mode="", action=action)
        assert runs == (action in SHADOW_ACTIONS), action


# --- AC2: permissions ----------------------------------------------------------------------------


def test_p2_s3_ac2_workflow_permissions_are_contents_read_only(workflow):
    assert workflow.get("permissions") == READ_ONLY


def test_p2_s3_ac2_shadow_job_sets_exactly_contents_read(workflow):
    assert shadow_job(workflow).get("permissions") == READ_ONLY


# --- AC3: who runs it, and the required check ---------------------------------------------------


@pytest.mark.parametrize("mode", ["", "shadow"])
def test_p2_s3_ac3_same_repo_pr_runs_when_mode_is_unset_or_shadow(workflow, mode):
    assert shadow_runs(workflow, head_repo=REPO, mode=mode)


@pytest.mark.parametrize("mode", ["off", "comment", "bogus"])
def test_p2_s3_ac3_other_mode_runs_no_shadow_job(workflow, mode):
    assert not shadow_runs(workflow, head_repo=REPO, mode=mode)


@pytest.mark.parametrize(
    "head_repo",
    ["someone/review-bot", "octo/review-bot-fork", None],
    ids=["fork", "similar-name", "deleted-fork"],
)
@pytest.mark.parametrize("mode", ["", "shadow"])
def test_p2_s3_ac3_fork_pr_runs_no_shadow_job(workflow, head_repo, mode):
    assert not shadow_runs(workflow, head_repo=head_repo, mode=mode)


def test_p2_s3_ac3_no_job_is_named_gate(workflow):
    for job_id, job in jobs_of(workflow).items():
        assert job_id != "gate"
        assert str(job.get("name", "")).strip().casefold() != "gate"


def test_p2_s3_ac3_ci_yml_still_has_the_gate_job():
    """The required check stays CI's `gate`; review.yml has none (the test above)."""
    ci = yaml.safe_load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert "gate" in ci["jobs"]
    assert ci["jobs"]["gate"].get("name", "gate") == "gate"


# --- AC4: code from the base --------------------------------------------------------------------


def test_p2_s3_ac4_base_sha_is_checked_out_with_full_history_and_no_credentials(workflow):
    checkouts = steps_using(shadow_job(workflow), "actions/checkout")
    assert len(checkouts) == 1
    with_ = checkouts[0]["with"]
    assert re.fullmatch(r"\$\{\{\s*github\.event\.pull_request\.base\.sha\s*\}\}", with_["ref"])
    assert with_["fetch-depth"] == 0
    assert with_["persist-credentials"] is False


def test_p2_s3_ac4_head_sha_is_only_fetched(workflow):
    """The head SHA reaches one step's env, and that step only fetches it."""
    readers = [
        step
        for step in steps_of(shadow_job(workflow))
        if any("head.sha" in str(value) for value in (step.get("env") or {}).values())
    ]
    assert len(readers) == 1
    script = readers[0]["run"].strip()
    assert re.fullmatch(r'git fetch [^;&|`$]*"\$\w+"', script), script


def test_p2_s3_ac4_head_sha_reaches_no_action_input_but_the_artifact_name(workflow):
    for step in all_jobs_steps(workflow):
        for key, value in (step.get("with") or {}).items():
            if "head" in str(value):
                assert step["uses"].startswith("actions/upload-artifact@")
                assert key == "name"


def test_p2_s3_ac4_no_step_moves_the_work_tree_off_the_base(workflow):
    moves = re.compile(r"\bgit\s+(checkout|switch|reset|worktree|merge|pull|rebase|cherry-pick)\b")
    for step in all_jobs_steps(workflow):
        assert not moves.search(step.get("run", "")), step["run"]


def test_p2_s3_ac4_dependencies_install_locked_without_dev_after_checkout(workflow):
    steps = steps_of(shadow_job(workflow))
    installs = [i for i, step in enumerate(steps) if "uv sync" in step.get("run", "")]
    assert len(installs) == 1
    assert steps[installs[0]]["run"].split() == ["uv", "sync", "--locked", "--no-dev"]
    checkout = steps.index(steps_using(shadow_job(workflow), "actions/checkout")[0])
    assert checkout < installs[0] < steps.index(run_step(workflow))


# --- AC5: the key, and only the key, reaches only the run step ----------------------------------


def test_p2_s3_ac5_api_key_is_only_in_the_run_steps_env(workflow):
    """Scoped to the shadow job since P2-S6, whose comment job has its own rule (its AC10)."""
    job = shadow_job(workflow)
    assert run_step(workflow)["env"]["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    holders = [path for path, value in walk(job) if isinstance(value, str) and "secrets." in value]
    run_index = steps_of(job).index(run_step(workflow))
    assert holders == [("steps", run_index, "env", "ANTHROPIC_API_KEY")]
    assert yaml.safe_dump(job).count("secrets.") == 1


@pytest.mark.parametrize("token", ["GITHUB_TOKEN", "github.token"])
def test_p2_s3_ac5_no_github_token_is_passed_anywhere_in_the_shadow_job(workflow, token):
    """Keys and values alike; scoped to the shadow job since P2-S6 (its AC10)."""
    assert token.casefold() not in yaml.safe_dump(shadow_job(workflow)).casefold()


def test_p2_s3_ac5_no_job_or_workflow_env_carries_values_to_every_step(workflow):
    assert "env" not in workflow
    assert "env" not in shadow_job(workflow)


def test_p2_s3_ac5_run_passes_mode_shadow_and_out(workflow):
    argv = run_command_argv(workflow)
    assert argv[0] == "run"
    assert option_value(argv, "--mode") == "shadow"
    assert same_dir(option_value(argv, "--out")) == OUT_DIR


def test_p2_s3_ac5_ac7_run_command_and_caps_are_accepted_by_the_cli(
    workflow, tmp_path, monkeypatch, capsys
):
    """The step's own argv and env, with a broken event, get past parsing to the event (19)."""
    event = tmp_path / "event.json"
    event.write_text("not json", encoding="utf-8")
    for name in [name for name in os.environ if name.startswith("REVIEW_BOT_")]:
        monkeypatch.delenv(name)
    for name, value in run_step(workflow)["env"].items():
        if not str(value).startswith("${{"):
            monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    argv = [os.path.expandvars(word) for word in run_command_argv(workflow)]

    exit_code = cli.main(argv, provider=MockProvider(findings=[]))

    assert exit_code == EVENT_ERROR_EXIT, capsys.readouterr().err
    assert exit_code != CONFIG_ERROR_EXIT


# --- AC6: no event text in shell -----------------------------------------------------------------


def test_p2_s3_ac6_no_run_block_interpolates_an_expression(workflow):
    for step in all_jobs_steps(workflow):
        assert "${{" not in step.get("run", ""), step.get("name")


def test_p2_s3_ac6_env_values_read_only_the_number_and_shas(workflow):
    allowed = {
        "github.event.pull_request.number",
        "github.event.pull_request.base.sha",
        "github.event.pull_request.head.sha",
    }
    for path, value in walk(workflow):
        if "env" in path and isinstance(value, str):
            assert set(EVENT_FIELD.findall(value)) <= allowed, (path, value)


def test_p2_s3_ac6_action_and_head_repo_are_read_only_in_if(workflow):
    if_only = {"github.event.action", "github.event.pull_request.head.repo.full_name"}
    for path, value in walk(workflow):
        if isinstance(value, str) and path and path[-1] != "if":
            assert not set(EVENT_FIELD.findall(value)) & if_only, (path, value)


def test_p2_s3_ac6_no_other_event_field_appears_anywhere(text):
    allowed = {
        "github.event.pull_request.number",
        "github.event.pull_request.base.sha",
        "github.event.pull_request.head.sha",
        "github.event.action",
        "github.event.pull_request.head.repo.full_name",
    }
    assert set(EVENT_FIELD.findall(text)) <= allowed


def test_p2_s3_ac6_branch_name_context_is_never_read(text):
    """`github.head_ref` is the PR's branch name: event text by another name."""
    assert "head_ref" not in text


# --- AC7: caps -----------------------------------------------------------------------------------


def test_p2_s3_ac7_run_step_env_sets_the_design_caps(workflow):
    env = {name: str(value) for name, value in run_step(workflow)["env"].items()}
    assert {name: env.get(name) for name in CAPS} == CAPS


def test_p2_s3_ac7_shadow_job_times_out_after_10_minutes(workflow):
    assert shadow_job(workflow)["timeout-minutes"] == 10


def test_p2_s3_ac7_concurrency_is_one_group_per_pr_cancelling_in_progress(workflow):
    concurrency = shadow_job(workflow).get("concurrency") or workflow.get("concurrency")
    assert isinstance(concurrency, dict)
    assert "github.event.pull_request.number" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is True


# --- AC8: summary and artifact -------------------------------------------------------------------


def summary_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    return summary_step_in(shadow_job(workflow))


def summary_step_in(job: dict[str, Any]) -> dict[str, Any]:
    found = [step for step in steps_of(job) if "GITHUB_STEP_SUMMARY" in step.get("run", "")]
    assert len(found) == 1
    return found[0]


def test_p2_s3_ac8_summary_step_runs_always(workflow):
    assert summary_step(workflow).get("if") == "always()"


def test_p2_s3_ac8_summary_step_writes_shadow_md_from_out(workflow, tmp_path):
    out = tmp_path / "review-bot"
    out.mkdir()
    report = "# Shadow review\n\n- would comment on `a.py:3`\n"
    (out / "shadow.md").write_text(report, encoding="utf-8")
    summary = tmp_path / "summary.md"

    done = run_script(
        summary_step(workflow)["run"],
        {"RUNNER_TEMP": tmp_path.as_posix(), "GITHUB_STEP_SUMMARY": summary.as_posix()},
        tmp_path,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert report in summary.read_text(encoding="utf-8")


def upload_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    return upload_step_in(shadow_job(workflow))


def upload_step_in(job: dict[str, Any]) -> dict[str, Any]:
    found = steps_using(job, "actions/upload-artifact")
    assert len(found) == 1
    return found[0]


def test_p2_s3_ac8_artifact_uploads_the_three_files_from_out(workflow):
    paths = [same_dir(line) for line in upload_step(workflow)["with"]["path"].splitlines()]
    assert sorted(path for path in paths if path) == sorted(f"{OUT_DIR}/{f}" for f in REPORT_FILES)


def test_p2_s3_ac8_artifact_name_holds_pr_number_head_sha_and_attempt(workflow):
    name = upload_step(workflow)["with"]["name"]
    for part in (
        "github.event.pull_request.number",
        "github.event.pull_request.head.sha",
        "github.run_attempt",
    ):
        assert re.search(r"\$\{\{\s*" + re.escape(part) + r"\s*\}\}", name), part


def test_p2_s3_ac8_artifact_is_kept_90_days_and_uploads_always(workflow):
    step = upload_step(workflow)
    assert step["with"]["retention-days"] == 90
    assert step.get("if") == "always()"


# --- AC9: pinned actions -------------------------------------------------------------------------


def test_p2_s3_ac9_every_uses_is_pinned_to_a_commit_sha(workflow):
    uses = [value for path, value in walk(workflow) if path and path[-1] == "uses"]
    assert uses
    for value in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", value), value


# --- AC10: failures stay visible -----------------------------------------------------------------


def test_p2_s3_ac10_nothing_continues_on_error(workflow, text):
    assert not [path for path, _ in walk(workflow) if path and path[-1] == "continue-on-error"]
    assert "continue-on-error" not in text


# --- AC11: an unknown mode fails a check job, and no review job runs ----------------------------


def check_script(workflow: dict[Any, Any]) -> str:
    steps = [step for step in jobs_of(workflow)[check_job_name(workflow)]["steps"] if "run" in step]
    assert len(steps) == 1
    assert re.fullmatch(r"\$\{\{\s*vars\.REVIEW_BOT_MODE\s*\}\}", steps[0]["env"]["MODE"])
    return str(steps[0]["run"])


@pytest.mark.parametrize("mode", ["", "off", "shadow", "comment"])
def test_p2_s3_ac11_known_mode_passes_the_check_quietly(workflow, tmp_path, mode):
    done = run_script(check_script(workflow), {"MODE": mode}, tmp_path)

    assert (done.returncode, done.stdout + done.stderr) == (0, "")


def test_p2_s3_ac11_mode_absent_from_env_passes_the_check(workflow, tmp_path):
    done = run_script(check_script(workflow), {}, tmp_path)

    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    "mode",
    [
        "bogus",
        "Shadow",
        "SHADOW",
        " shadow",
        "shadow ",
        "on",
        "*",
        "shadow|comment",
        "$(touch pwned)",
    ],
)
def test_p2_s3_ac11_unknown_mode_fails_with_one_line_naming_variable_and_values(
    workflow, tmp_path, mode
):
    done = run_script(check_script(workflow), {"MODE": mode}, tmp_path)

    output = done.stdout + done.stderr
    assert done.returncode != 0
    assert len(output.splitlines()) == 1, output
    for word in ("REVIEW_BOT_MODE", "unset", "off", "shadow", "comment"):
        assert word in output, word
    assert not (tmp_path / "pwned").exists()


def depends_on(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def test_p2_s3_ac11_every_other_job_needs_the_check_and_respects_its_failure(workflow):
    check = check_job_name(workflow)
    jobs = jobs_of(workflow)
    others = {name: job for name, job in jobs.items() if name != check}
    assert others
    for name, job in others.items():
        ancestors, frontier = set(), depends_on(job)
        while frontier:
            ancestors |= frontier
            frontier = set().union(*(depends_on(jobs[parent]) for parent in frontier)) - ancestors
        assert check in ancestors, f"job {name} doesn't need {check}"
        assert not STATUS_FUNCTIONS.search(str(job.get("if", ""))), job["if"]
