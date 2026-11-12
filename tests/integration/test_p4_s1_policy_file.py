r"""P4-S1: run under a versioned policy file that every report records.

Written from the spec's criteria, Limits and Interfaces, Phase 4 design.md (Interfaces, `policy/v1`,
Policy as code) and ADR 0018, in parallel with the developer and before their code was read.

Every command runs in process through its public entry point, as the earlier suites drive it:
- `review-bot`: `review_bot.cli.main([DIFF, "--provider", "mock", ...], provider=MockProvider(...))`
  (the `bot` fixture), and P3-S1's `Harness` for context mode over a real git repository.
- `pr shadow|run|backfill`: `review_bot.pr.cli.main([...], provider=...)`, with P2-S13's
  `Backfiller` and P3-S8's `PrRunner` and pull request.
- `evals live|replay`: `review_bot.evals.main([...], sdk_call=fake)`, P1-S16's seam: the command
  builds its own Claude provider over the scripted `sdk_call`, so each call's options are visible.

Where a criterion is about the cap on each model call, the call is made by the real Claude
provider and stopped by the suite's guard (`real_query_calls`), which records the options the CLI
would get, so `max_budget_usd` is what a real call would carry. Nothing reaches a model or the
network.

Readings, each used in one place:
- "Exits 2 before reading the diff" is checked with a diff path that doesn't exist: reading it
  would exit 3 (P0-S1), so exit 2 naming the policy shows the diff was never read.
- "No provider is built" is checked by replacing `build_provider` (cli and pr.cli) with a
  recorder, and running with no provider injected.
- "64 KB" is read as either 64,000 or 65,536 bytes: 64,000 bytes must be accepted and 65,537
  refused; the bytes between are left to the developer.
- A message names "the file" if it holds the file's name, and "the key" if it holds the key.
"""

import hashlib
import json
import re
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from fake_sdk import findings_json, result_message
from repo_paths import REPO_ROOT
from test_p1_s4_audit_log import read_records
from test_p1_s10_json_output import SAMPLE_GOLDEN
from test_p2_s1_shadow_report_local import load_shadow
from test_p2_s2_review_pr_event import event
from test_p2_s13_shadow_backfill import Backfiller, History, PerPullProvider
from test_p3_s1_local_review_with_repo_tools import (
    CHECKOUT,
    GOLDEN,
    INVOICE,
    MAIN,
    READ_CHECKOUT,
    ContextRepo,
    Harness,
    git_processes,
    make_repo,
    numbered,
    refusal_reason,
    unwrap,
)
from test_p3_s6_live_evals_workflow import (
    CI_REFUSAL,
    STEP_IDS,
    STEPS,
    parse_in_ci,
    run_live_step,
    workflow,
)
from test_p3_s8_pr_review_with_context import (
    BACKFILL_ARGS,
    CHECKOUT_AT_C,
    ContextPull,
    PrRunner,
    make_context_pull,
)

from review_bot import cli
from review_bot.context.ledger import RefusalReason
from review_bot.context.tools import RepoRef, RepoTools, ToolCaps
from review_bot.evals import main as evals_main
from review_bot.git import GitRepo
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

__all__ = ["git_processes", "workflow"]  # fixtures, used by being imported

USAGE_EXIT = 2
FINDINGS_EXIT = 1
SHADOW_OK = 0  # `pr shadow` exits 0 once it writes its report, findings or not
DIFF_NOT_FOUND_EXIT = 3
INTERNAL_EXIT = 14
POLICY_VARIABLE = "REVIEW_BOT_POLICY"
AUDIT_VARIABLE = "REVIEW_BOT_AUDIT_LOG"
BUILTIN_VERSION = "builtin-v1"
POLICY_KEYS = ("version", "sha256", "source")
HEX64 = re.compile(r"[0-9a-f]{64}")
SAMPLE = REPO_ROOT / "Assignment" / "sample_diff.txt"
EVAL_CASES = REPO_ROOT / "tests" / "evals" / "cases"
ALL_TOOLS = ["glob", "grep", "read"]
NO_TOOLS_MESSAGE = (
    r"The policy `?{version}`? allows no tools, so context mode can't run; use diff mode\."
)
# Limits: 64 KB, read as 64,000 or 65,536 bytes (module docstring).
SIZE_ACCEPTED = 64_000
SIZE_REFUSED = 65_537
SHOWN_CHARS = 100
# Generous: it catches a hang or super-linear work, not a slow CI machine.
BOUND_S = 60.0


# --- The policy file ------------------------------------------------------------------------------


def policy_toml(
    version: str = "org-1",
    *,
    allowed: Sequence[str] | None = None,
    max_cost_usd: str | None = None,
) -> str:
    """A `policy/v1` file holding only S1's keys."""
    lines = [f'version = "{version}"']
    if allowed is not None:
        lines += ["[tools]", "allowed = [" + ", ".join(f'"{tool}"' for tool in allowed) + "]"]
    if max_cost_usd is not None:
        lines += ["[cost]", f'max_cost_usd = "{max_cost_usd}"']
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary working directory, outside CI, with no policy or cost variable set."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    for name in ("CI", "GITHUB_EVENT_NAME", POLICY_VARIABLE, "REVIEW_BOT_MAX_COST_USD"):
        monkeypatch.delenv(name, raising=False)
    return work


@pytest.fixture
def write_policy(work_dir: Path) -> Callable[..., Path]:
    """Write exact bytes into the working directory, so a relative name reaches the same file."""

    def write(content: str | bytes = policy_toml(), name: str = "p.toml") -> Path:
        path = work_dir / name
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return path

    return write


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def as_decimal(value: object) -> Decimal:
    """A dollar amount written as a string or a number, read exactly."""
    assert isinstance(value, str | int | float), value
    return Decimal(str(value))


# --- Running the commands -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Done:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def report(self) -> dict[str, Any]:
        document = json.loads(self.stdout)
        assert isinstance(document, dict), self.stdout
        assert document["schema"] == "report/v1"
        return document


def review(bot: Any, *flags: str, provider: Any = None, diff: Path = SAMPLE) -> Done:
    """`review-bot DIFF --provider mock --format json FLAGS`, with a mock answering no findings."""
    provider = MockProvider(findings=[]) if provider is None else provider
    run = bot.run(str(diff), "--provider", "mock", "--format", "json", *flags, provider=provider)
    return Done(run.exit_code, run.stdout, run.stderr)


@dataclass
class Shadow:
    """`pr shadow DIFF --out OUT --provider mock FLAGS`, with the audit log in OUT."""

    capsys: pytest.CaptureFixture[str]
    tmp_path: Path
    runs: int = 0

    def run(self, *flags: str, provider: Any = None, diff: Path = SAMPLE) -> tuple[Done, Path]:
        from review_bot.pr import cli as pr_cli  # noqa: PLC0415 - as the earlier suites import it

        self.runs += 1
        out = self.tmp_path / f"shadow-{self.runs}"
        provider = MockProvider(findings=[]) if provider is None else provider
        self.capsys.readouterr()
        exit_code = pr_cli.main(
            ["shadow", str(diff), "--out", str(out), "--provider", "mock", *flags],
            provider=provider,
        )
        captured = self.capsys.readouterr()
        return Done(exit_code, captured.out, captured.err), out


@pytest.fixture
def shadow(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Shadow:
    monkeypatch.delenv(AUDIT_VARIABLE, raising=False)
    return Shadow(capsys, tmp_path)


@pytest.fixture
def harness(bot: Any, tmp_path: Path) -> Harness:
    return Harness(bot, tmp_path)


@pytest.fixture
def repo(tmp_path: Path) -> ContextRepo:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def pr(capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv(AUDIT_VARIABLE, raising=False)
    return PrRunner(capsys, tmp_path)


@pytest.fixture
def pull(tmp_path: Path) -> ContextPull:
    made, _ = make_context_pull(tmp_path / "clone", {CHECKOUT: CHECKOUT_AT_C})
    return made


def run_pr(pr: Any, pull: ContextPull, *flags: str, provider: Any) -> Any:
    """`pr run` on the pull request's event, in diff mode unless `flags` say otherwise."""
    return pr.run(event(pull.base, pull.head), pull.repo.path, *flags, provider=provider)


@pytest.fixture
def history(tmp_path: Path) -> History:
    return History(tmp_path / "history")


@pytest.fixture
def backfill(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> Backfiller:
    return Backfiller(capsys, tmp_path)


@pytest.fixture
def built_providers(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every provider the commands try to build; building one fails the run loudly."""
    from review_bot.pr import cli as pr_cli  # noqa: PLC0415 - as the earlier suites import it

    built: list[Any] = []

    def build(settings: Any) -> Any:
        built.append(settings)
        raise AssertionError("a provider was built")

    monkeypatch.setattr(cli, "build_provider", build)
    monkeypatch.setattr(pr_cli, "build_provider", build)
    return built


def real_call_budgets(real_query_calls: list[dict[str, Any]]) -> list[float | None]:
    """Each stopped real call's `max_budget_usd`; clears the guard's record for the teardown."""
    calls = list(real_query_calls)
    real_query_calls.clear()
    assert calls, "no call reached the Claude CLI"
    return [call["options"].max_budget_usd for call in calls]


# --- evals live, over P1-S16's seam ---------------------------------------------------------------


@dataclass
class EvalSdk:
    """A fake `sdk_call` answering every call with no findings and a small reported cost."""

    calls: list[ClaudeAgentOptions] = field(default_factory=list)

    def __call__(self, prompt: str, options: ClaudeAgentOptions) -> Any:
        self.calls.append(options)
        return self._stream()

    @staticmethod
    async def _stream() -> Any:
        yield result_message(
            findings_json(),
            usage={"input_tokens": 100, "output_tokens": 10},
            total_cost_usd=0.01,
        )

    @property
    def budgets(self) -> list[float | None]:
        return [options.max_budget_usd for options in self.calls]


def copy_cases(tmp_path: Path, *case_ids: str) -> Path:
    """A `--cases` directory holding copies of committed cases, without their recordings."""
    root = tmp_path / "cases"
    for case_id in case_ids:
        (root / case_id).mkdir(parents=True)
        case = json.loads((EVAL_CASES / case_id / "case.json").read_text(encoding="utf-8"))
        for name in ("case.json", case["diff"]):
            shutil.copyfile(EVAL_CASES / case_id / name, root / case_id / name)
    return root


@dataclass(frozen=True, slots=True)
class Live:
    exit_code: int
    stdout: str
    stderr: str


def run_evals(capsys: pytest.CaptureFixture[str], *args: str | Path, sdk: EvalSdk) -> Live:
    capsys.readouterr()
    try:
        exit_code = evals_main([*map(str, args)], sdk_call=sdk)
    except SystemExit as exc:  # argparse reports usage errors this way
        exit_code = exc.code if isinstance(exc.code, int) else USAGE_EXIT
    captured = capsys.readouterr()
    return Live(exit_code, captured.out, captured.err)


def live_args(root: Path, *, runs: int, max_calls: int, mode: str = "diff") -> list[str | Path]:
    return [
        "live",
        "--cases",
        root,
        "--mode",
        mode,
        "--runs",
        str(runs),
        "--max-calls",
        str(max_calls),
    ]


def assert_nothing_recorded(root: Path, work_dir: Path) -> None:
    assert not list(root.rglob("recordings")), "a refused run wrote a recording"
    assert not (work_dir / ".review-bot").exists(), "a refused run wrote its audit log"


# --- AC1: no policy is the built-in policy, and changes nothing else ------------------------------


def assert_builtin(policy: Any) -> None:
    assert isinstance(policy, dict), policy
    assert tuple(policy) == POLICY_KEYS, policy
    assert policy["version"] == BUILTIN_VERSION
    assert policy["source"] == "builtin"
    assert HEX64.fullmatch(policy["sha256"]), policy


def test_p4_s1_ac1_report_without_a_policy_records_the_builtin_policy(bot):
    done = review(bot)

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert_builtin(done.report["policy"])


def test_p4_s1_ac1_report_policy_is_appended_after_phase_3s_keys(bot):
    """Design: report keys are appended only."""
    keys = tuple(review(bot).report)

    assert keys[-3:] == ("mode", "context", "policy"), keys


def test_p4_s1_ac1_builtin_sha256_is_fixed_across_runs_and_reports(bot, shadow):
    first, second = review(bot).report["policy"], review(bot).report["policy"]
    done, out = shadow.run()

    assert done.exit_code == SHADOW_OK, done.stderr
    document = load_shadow(out)
    assert first == second
    assert document["policy"] == first
    assert document["report"]["policy"] == first


def test_p4_s1_ac1_report_is_p1_s10s_golden_with_only_the_builtin_policy_added(bot):
    """The golden gained `policy`, with the built-in policy's fixed hash, and nothing else."""
    done = review(bot)

    golden = json.loads(SAMPLE_GOLDEN.read_text(encoding="utf-8"))
    assert_builtin(golden["policy"])
    assert {k: v for k, v in done.report.items() if k not in ("run_id", "usage")} == golden


def test_p4_s1_ac1_request_is_byte_for_byte_phase_1s_without_a_policy(bot):
    provider = MockProvider(findings=[])

    review(bot, provider=provider)

    [request] = provider.requests
    system = (GOLDEN / "p1_v1_system_prompt.golden.txt").read_bytes().decode("utf-8")
    user = (GOLDEN / "p1_v1_sample_user_prompt.golden.txt").read_bytes().decode("utf-8")
    assert request.system_prompt == system
    assert request.user_prompt + "\n" == user
    assert request.tools is None


def test_p4_s1_ac1_context_review_without_a_policy_offers_all_three_tools(harness, repo):
    review_run = harness.review(repo, [("grep", {"literal": "total("})])

    names = sorted(d.name for d in review_run.request.tools.definitions)
    assert names == ALL_TOOLS
    [body] = review_run.bodies
    assert refusal_reason(body) is None, body


def test_p4_s1_ac1_shadow_caps_hold_no_cost_cap_without_a_policy(shadow):
    done, out = shadow.run()

    assert done.exit_code == SHADOW_OK, done.stderr
    assert load_shadow(out)["caps"].get("max_cost_usd") is None


def test_p4_s1_ac1_audit_start_records_the_builtin_policy(bot, tmp_path):
    audit = tmp_path / "audit.jsonl"
    done = review(bot, "--audit-log", str(audit))

    [start] = [
        r["data"] for r in read_records(audit) if (r["stage"], r["event"]) == ("run", "start")
    ]
    assert start["policy_version"] == BUILTIN_VERSION
    assert start["policy_sha256"] == done.report["policy"]["sha256"]


def test_p4_s1_ac1_explicit_policy_like_the_builtin_changes_only_the_policy_field(
    bot, write_policy
):
    """All three tools and no ceiling: everything but `policy` matches the run without one."""
    path = write_policy(policy_toml("same-as-builtin", allowed=ALL_TOOLS))
    without_policy, with_policy = MockProvider(findings=[]), MockProvider(findings=[])

    plain = review(bot, provider=without_policy)
    chosen = review(bot, "--policy", str(path), provider=with_policy)

    assert chosen.exit_code == plain.exit_code
    volatile = ("run_id", "usage", "policy")
    assert {k: v for k, v in chosen.report.items() if k not in volatile} == {
        k: v for k, v in plain.report.items() if k not in volatile
    }
    [first], [second] = without_policy.requests, with_policy.requests
    assert (first.system_prompt, first.user_prompt) == (second.system_prompt, second.user_prompt)


# --- AC2: a policy file is recorded by version, hash and source -----------------------------------


def test_p4_s1_ac2_report_records_the_files_version_sha256_and_source_as_given(bot, write_policy):
    path = write_policy()

    done = review(bot, "--policy", "p.toml")

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert done.report["policy"] == {
        "version": "org-1",
        "sha256": sha256_of(path),
        "source": "p.toml",
    }


def test_p4_s1_ac2_source_is_an_absolute_path_exactly_as_given(bot, write_policy):
    path = write_policy()

    done = review(bot, "--policy", str(path))

    assert done.report["policy"]["source"] == str(path)


def test_p4_s1_ac2_sha256_is_of_the_files_raw_bytes_crlf_included(bot, write_policy):
    path = write_policy(policy_toml().replace("\n", "\r\n").encode("utf-8"))

    done = review(bot, "--policy", str(path))

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert done.report["policy"]["sha256"] == sha256_of(path)
    assert done.report["policy"]["sha256"] != hashlib.sha256(policy_toml().encode()).hexdigest()


def test_p4_s1_ac2_shadow_and_its_report_record_the_same_policy(shadow, write_policy):
    path = write_policy()

    done, out = shadow.run("--policy", "p.toml")

    assert done.exit_code == SHADOW_OK, done.stderr
    document = load_shadow(out)
    expected = {"version": "org-1", "sha256": sha256_of(path), "source": "p.toml"}
    assert document["policy"] == expected
    assert document["report"]["policy"] == expected


def test_p4_s1_ac2_audit_start_has_policy_version_and_sha256(bot, write_policy, tmp_path):
    path = write_policy()
    audit = tmp_path / "audit.jsonl"

    review(bot, "--policy", str(path), "--audit-log", str(audit))

    [start] = [
        r["data"] for r in read_records(audit) if (r["stage"], r["event"]) == ("run", "start")
    ]
    assert start["policy_version"] == "org-1"
    assert start["policy_sha256"] == sha256_of(path)


def test_p4_s1_ac2_shadow_audit_start_has_policy_version_and_sha256(shadow, write_policy):
    path = write_policy()

    _, out = shadow.run("--policy", str(path))

    records = read_records(out / "audit.jsonl")
    [start] = [r["data"] for r in records if (r["stage"], r["event"]) == ("run", "start")]
    assert (start["policy_version"], start["policy_sha256"]) == ("org-1", sha256_of(path))


def test_p4_s1_ac2_the_variable_alone_chooses_the_policy(bot, write_policy, monkeypatch):
    path = write_policy(policy_toml("env-1"), name="env.toml")
    monkeypatch.setenv(POLICY_VARIABLE, "env.toml")

    done = review(bot)

    assert done.report["policy"] == {
        "version": "env-1",
        "sha256": sha256_of(path),
        "source": "env.toml",
    }


def test_p4_s1_ac2_the_flag_wins_over_the_variable(bot, write_policy, monkeypatch):
    write_policy(policy_toml("env-1"), name="env.toml")
    path = write_policy()
    monkeypatch.setenv(POLICY_VARIABLE, "env.toml")

    done = review(bot, "--policy", "p.toml")

    assert done.report["policy"] == {
        "version": "org-1",
        "sha256": sha256_of(path),
        "source": "p.toml",
    }


def test_p4_s1_ac2_the_flag_wins_over_an_invalid_variable(bot, write_policy, monkeypatch):
    """The variable's file is never read when the flag names another."""
    write_policy("not toml at all [", name="env.toml")
    write_policy()
    monkeypatch.setenv(POLICY_VARIABLE, "env.toml")

    done = review(bot, "--policy", "p.toml")

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert done.report["policy"]["version"] == "org-1"


def test_p4_s1_ac2_pr_run_records_the_policy(pr, pull, write_policy):
    path = write_policy()

    run = run_pr(pr, pull, "--policy", str(path), provider=MockProvider(findings=[]))

    assert run.exit_code == 0, run.stderr
    assert run.shadow["policy"] == {
        "version": "org-1",
        "sha256": sha256_of(path),
        "source": str(path),
    }


def test_p4_s1_ac2_backfill_records_the_policy_in_every_report(backfill, history, write_policy):
    path = write_policy()
    pulls = history.merged_many(11, 12)

    run = backfill.run(
        pulls, history.repo.path, "--policy", str(path), max_calls=2, provider=PerPullProvider()
    )

    assert run.exit_code == 0, run.stderr
    expected = {"version": "org-1", "sha256": sha256_of(path), "source": str(path)}
    assert [run.shadow(p.number)["policy"] for p in pulls] == [expected, expected]


# --- AC3: an invalid policy file stops the command before the diff, naming the file and problem ---


@dataclass(frozen=True, slots=True)
class Invalid:
    content: str | bytes
    named: tuple[str, ...]  # at least one of these appears in the message


VERSION = 'version = "org-1"\n'
INVALID_CONTENT = {
    "unknown-top-level-key": Invalid(VERSION + "foo = 1\n", ("foo",)),
    "unknown-key-in-tools": Invalid(VERSION + '[tools]\nallowed = ["read"]\nfoo = 1\n', ("foo",)),
    "unknown-key-in-cost": Invalid(VERSION + '[cost]\nmax_cost_usd = "0.60"\nfoo = 1\n', ("foo",)),
    "a-later-slices-section": Invalid(VERSION + '[redaction]\nkinds = ["email"]\n', ("redaction",)),
    "a-later-slices-models": Invalid(VERSION + '[[models]]\nprovider = "claude"\n', ("models",)),
    "version-with-a-space": Invalid('version = "org 1"\n', ("version",)),
    "version-with-a-slash": Invalid('version = "org/1"\n', ("version",)),
    "version-empty": Invalid('version = ""\n', ("version",)),
    "version-65-characters": Invalid(f'version = "{"a" * 65}"\n', ("version",)),
    "version-not-ascii": Invalid('version = "org-é"\n', ("version",)),
    "version-not-a-string": Invalid("version = 1\n", ("version",)),
    "no-version": Invalid('[tools]\nallowed = ["read"]\n', ("version",)),
    "empty-file": Invalid("", ("version",)),
    "tool-unknown": Invalid(VERSION + '[tools]\nallowed = ["read", "bash"]\n', ("bash", "allowed")),
    "tool-in-capitals": Invalid(VERSION + '[tools]\nallowed = ["Read"]\n', ("Read", "allowed")),
    "tool-repeated": Invalid(
        VERSION + '[tools]\nallowed = ["read", "read"]\n',
        ("allowed", "repeat", "duplicate", "twice", "more than once"),
    ),
    "cost-below-0.01": Invalid(VERSION + '[cost]\nmax_cost_usd = "0.009"\n', ("max_cost_usd",)),
    "cost-zero": Invalid(VERSION + '[cost]\nmax_cost_usd = "0"\n', ("max_cost_usd",)),
    "cost-negative": Invalid(VERSION + '[cost]\nmax_cost_usd = "-1"\n', ("max_cost_usd",)),
    "cost-above-1000": Invalid(VERSION + '[cost]\nmax_cost_usd = "1000.01"\n', ("max_cost_usd",)),
    "cost-a-float": Invalid(VERSION + "[cost]\nmax_cost_usd = 0.6\n", ("max_cost_usd",)),
    "cost-an-integer": Invalid(VERSION + "[cost]\nmax_cost_usd = 1\n", ("max_cost_usd",)),
    "cost-a-word": Invalid(VERSION + '[cost]\nmax_cost_usd = "abc"\n', ("max_cost_usd",)),
    "cost-nan": Invalid(VERSION + '[cost]\nmax_cost_usd = "NaN"\n', ("max_cost_usd",)),
    "cost-infinity": Invalid(VERSION + '[cost]\nmax_cost_usd = "Infinity"\n', ("max_cost_usd",)),
    "cost-empty": Invalid(VERSION + '[cost]\nmax_cost_usd = ""\n', ("max_cost_usd",)),
    "not-utf-8": Invalid(b'version = "org-\xff"\n', ("UTF-8", "utf-8", "UTF8")),
    "not-toml": Invalid('version = "org-1\n', ("TOML", "toml")),
    "not-toml-no-value": Invalid("version =\n", ("TOML", "toml")),
    "over-64-kb": Invalid(
        VERSION + "#" * SIZE_REFUSED + "\n",
        ("64 KB", "65536", "65,536", "64,000", "limit", "large"),
    ),
}


def assert_policy_refused(
    done: Done, path: Path, named: Sequence[str], built: list[Any], provider: Any = None
) -> None:
    assert done.exit_code == USAGE_EXIT, done.stderr
    assert path.name in done.stderr, done.stderr
    assert any(text in done.stderr for text in named), (named, done.stderr)
    assert built == [], "a provider was built"
    if provider is not None:
        assert provider.requests == []


@pytest.mark.parametrize("case", INVALID_CONTENT.values(), ids=INVALID_CONTENT.keys())
def test_p4_s1_ac3_invalid_policy_exits_2_before_the_diff_naming_file_and_problem(
    bot, write_policy, built_providers, work_dir, case
):
    path = write_policy(case.content)

    run = bot.run(str(work_dir / "absent.diff"), "--provider", "claude", "--policy", str(path))

    assert_policy_refused(
        Done(run.exit_code, run.stdout, run.stderr), path, case.named, built_providers
    )
    assert run.exit_code != DIFF_NOT_FOUND_EXIT


def test_p4_s1_ac3_missing_policy_file_exits_2_naming_it(bot, built_providers, work_dir):
    path = work_dir / "absent-policy.toml"

    run = bot.run(str(work_dir / "absent.diff"), "--provider", "claude", "--policy", str(path))

    assert_policy_refused(
        Done(run.exit_code, run.stdout, run.stderr), path, (path.name,), built_providers
    )


def test_p4_s1_ac3_unreadable_policy_exits_2_naming_it(bot, built_providers, work_dir):
    """A directory can't be read as a file on any platform."""
    path = work_dir / "policy-dir.toml"
    path.mkdir()

    run = bot.run(str(work_dir / "absent.diff"), "--provider", "claude", "--policy", str(path))

    assert_policy_refused(
        Done(run.exit_code, run.stdout, run.stderr), path, (path.name,), built_providers
    )


def test_p4_s1_ac3_invalid_policy_from_the_variable_exits_2_naming_it(
    bot, write_policy, built_providers, work_dir, monkeypatch
):
    path = write_policy(VERSION + "foo = 1\n")
    monkeypatch.setenv(POLICY_VARIABLE, str(path))

    run = bot.run(str(work_dir / "absent.diff"), "--provider", "claude")

    assert_policy_refused(
        Done(run.exit_code, run.stdout, run.stderr), path, ("foo",), built_providers
    )


def test_p4_s1_ac3_file_of_64000_bytes_is_accepted(bot, write_policy):
    padding = SIZE_ACCEPTED - len(VERSION) - 1
    path = write_policy(VERSION + "#" * padding + "\n")
    assert path.stat().st_size == SIZE_ACCEPTED

    done = review(bot, "--policy", str(path))

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert done.report["policy"]["sha256"] == sha256_of(path)


@pytest.mark.parametrize("amount", ["0.01", "1000", "0.60", "999.99"])
def test_p4_s1_ac3_ceiling_at_or_inside_its_range_is_accepted(bot, write_policy, amount):
    path = write_policy(policy_toml(max_cost_usd=amount))

    done = review(bot, "--policy", str(path))

    assert done.exit_code == FINDINGS_EXIT, done.stderr


@pytest.mark.parametrize("version", ["a", "A" * 64, "rightship-2026-11", "v1.2_3-x"])
def test_p4_s1_ac3_version_matching_the_pattern_is_accepted(bot, write_policy, version):
    path = write_policy(policy_toml(version))

    done = review(bot, "--policy", str(path))

    assert done.report["policy"]["version"] == version


@pytest.mark.parametrize("allowed", [[], ["read"], ["glob", "read"], ["read", "grep", "glob"]])
def test_p4_s1_ac3_any_subset_of_the_three_tools_is_accepted(bot, write_policy, allowed):
    path = write_policy(policy_toml(allowed=allowed))

    done = review(bot, "--policy", str(path))

    assert done.exit_code == FINDINGS_EXIT, done.stderr


def test_p4_s1_ac3_shadow_refuses_an_invalid_policy_before_any_call(shadow, write_policy):
    path = write_policy(VERSION + "foo = 1\n")
    provider = MockProvider(findings=[])

    done, out = shadow.run("--policy", str(path), provider=provider)

    assert_policy_refused(done, path, ("foo",), [], provider)
    assert not (out / "shadow.json").exists()


def test_p4_s1_ac3_pr_run_refuses_an_invalid_policy_before_any_call(pr, pull, write_policy):
    path = write_policy(VERSION + "foo = 1\n")
    provider = MockProvider(findings=[])

    run = run_pr(pr, pull, "--policy", str(path), provider=provider)

    assert_policy_refused(Done(run.exit_code, run.stdout, run.stderr), path, ("foo",), [], provider)


def test_p4_s1_ac3_backfill_refuses_an_invalid_policy_before_any_call(
    backfill, history, write_policy
):
    path = write_policy(VERSION + "foo = 1\n")
    provider = PerPullProvider()

    run = backfill.run(
        history.merged_many(21), history.repo.path, "--policy", str(path), provider=provider
    )

    assert_policy_refused(Done(run.exit_code, run.stdout, run.stderr), path, ("foo",), [], provider)
    assert run.reports() == []


@pytest.mark.parametrize("command", ["live", "replay"])
def test_p4_s1_ac3_evals_refuse_an_invalid_policy_before_any_call(
    capsys, tmp_path, write_policy, work_dir, command
):
    path = write_policy(VERSION + "foo = 1\n")
    root = copy_cases(tmp_path, "sample")
    sdk = EvalSdk()
    args: list[str | Path] = (
        [*live_args(root, runs=1, max_calls=1), "--max-usd", "1", "--max-cost-usd", "0.10"]
        if command == "live"
        else ["replay", "--cases", root]
    )

    live = run_evals(capsys, *args, "--policy", path, sdk=sdk)

    assert live.exit_code == USAGE_EXIT, live.stderr
    assert path.name in live.stderr and "foo" in live.stderr, live.stderr
    assert sdk.calls == []
    assert_nothing_recorded(root, work_dir)


# Limits: hostile content is parsed in linear time, and a message shows at most 100 characters.


def test_p4_s1_limits_a_long_unknown_key_is_shown_cut_to_100_characters(
    bot, write_policy, work_dir
):
    path = write_policy(VERSION + "k" * 20_000 + " = 1\n")

    run = bot.run(str(work_dir / "absent.diff"), "--provider", "mock", "--policy", str(path))

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert "k" * (SHOWN_CHARS + 1) not in run.stderr, len(run.stderr)


def test_p4_s1_limits_a_long_version_is_shown_cut_to_100_characters(bot, write_policy, work_dir):
    path = write_policy(f'version = "{"v" * 20_000}"\n')

    run = bot.run(str(work_dir / "absent.diff"), "--provider", "mock", "--policy", str(path))

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert "v" * (SHOWN_CHARS + 1) not in run.stderr, len(run.stderr)


@pytest.mark.parametrize(
    ("content", "raw"),
    [
        pytest.param('version = "org\\u001b[31m-1"\n', "\x1b", id="escape-in-version"),
        pytest.param(VERSION + '"bell\\u0007key" = 1\n', "\x07", id="bell-in-a-quoted-key"),
        pytest.param(VERSION + '"line\\nfeed" = 1\n', "line\nfeed", id="newline-in-a-quoted-key"),
        pytest.param(VERSION + '"bidi\\u202ekey" = 1\n', chr(0x202E), id="bidi-override-in-a-key"),
        pytest.param(
            VERSION + '[tools]\nallowed = ["re\\u0000ad"]\n', "\x00", id="nul-in-a-tool-name"
        ),
    ],
)
def test_p4_s1_limits_unprintable_characters_in_a_message_are_escaped(
    bot, write_policy, work_dir, content, raw
):
    path = write_policy(content)

    run = bot.run(str(work_dir / "absent.diff"), "--provider", "mock", "--policy", str(path))

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert raw not in run.stderr, repr(run.stderr)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("[" * 30_000 + "]" * 30_000, id="nested-arrays"),
        pytest.param("{a=" * 15_000 + "1" + "}" * 15_000, id="nested-inline-tables"),
    ],
)
def test_p4_s1_limits_deep_nesting_within_64_kb_exits_2_in_bounded_time(
    bot, write_policy, work_dir, value
):
    """tomllib recurses on nesting: a parse that overflows must still be a policy error."""
    path = write_policy(VERSION + "foo = " + value + "\n")
    assert path.stat().st_size < SIZE_ACCEPTED

    started = time.monotonic()
    run = bot.run(str(work_dir / "absent.diff"), "--provider", "mock", "--policy", str(path))

    assert time.monotonic() - started < BOUND_S
    assert run.exit_code == USAGE_EXIT, run.stderr
    assert path.name in run.stderr, run.stderr


def test_p4_s1_limits_many_keys_within_64_kb_exit_2_in_bounded_time(bot, write_policy, work_dir):
    keys = "".join(f"k{n} = {n}\n" for n in range(4_000))
    path = write_policy(VERSION + keys)
    assert path.stat().st_size < SIZE_ACCEPTED

    started = time.monotonic()
    run = bot.run(str(work_dir / "absent.diff"), "--provider", "mock", "--policy", str(path))

    assert time.monotonic() - started < BOUND_S
    assert run.exit_code == USAGE_EXIT, run.stderr
    assert len(run.stderr) < 4_000, "the message lists every unknown key"


# --- AC4: the executor offers and runs only the allowed tools -------------------------------------


GREP_TOTAL = ("grep", {"literal": "total("})
GLOB_PY = ("glob", {"pattern": "billing/*.py"})


@pytest.fixture
def read_glob_policy(write_policy: Callable[..., Path]) -> Path:
    return write_policy(policy_toml(allowed=["read", "glob"]))


def test_p4_s1_ac4_request_tool_definitions_are_exactly_read_and_glob(
    harness, repo, read_glob_policy
):
    review_run = harness.review(repo, [GREP_TOTAL], "--policy", str(read_glob_policy))

    names = sorted(d.name for d in review_run.request.tools.definitions)
    assert names == ["glob", "read"]


def test_p4_s1_ac4_grep_call_returns_a_delimited_policy_denied_refusal(
    harness, repo, read_glob_policy
):
    review_run = harness.review(repo, [GREP_TOTAL], "--policy", str(read_glob_policy))

    [result] = review_run.provider.tool_results
    body = unwrap(result)  # the delimiters, with the body's hash
    assert "\n" not in body, body
    assert refusal_reason(body) == "policy_denied", body
    assert review_run.exit_code == FINDINGS_EXIT, review_run.stderr


def test_p4_s1_ac4_audit_tool_call_record_is_refused_policy_denied(harness, repo, read_glob_policy):
    harness.review(repo, [GREP_TOTAL], "--policy", str(read_glob_policy))

    [record] = harness.tool_call_records()
    assert (record["tool"], record["outcome"], record["reason"]) == (
        "grep",
        "refused",
        "policy_denied",
    )


def test_p4_s1_ac4_allowed_tools_still_run(harness, repo, read_glob_policy):
    review_run = harness.review(
        repo, [READ_CHECKOUT, GLOB_PY, GREP_TOTAL], "--policy", str(read_glob_policy)
    )

    read_body, glob_body, grep_body = review_run.bodies
    assert numbered(read_body), read_body
    assert glob_body.split("\n") == sorted([CHECKOUT, INVOICE]), glob_body
    assert refusal_reason(grep_body) == "policy_denied"
    assert harness.outcomes() == ["ok", "ok", "refused"]


def test_p4_s1_ac4_denied_call_counts_towards_max_tool_calls(harness, repo, read_glob_policy):
    review_run = harness.review(
        repo,
        [GREP_TOTAL, READ_CHECKOUT, ("read", {"path": MAIN})],
        "--policy",
        str(read_glob_policy),
        "--max-tool-calls",
        "2",
    )

    denied, read_body, third = review_run.bodies
    assert refusal_reason(denied) == "policy_denied"
    assert numbered(read_body), read_body
    assert refusal_reason(third) == "call_limit", third
    reasons = [(r["outcome"], r["reason"]) for r in harness.tool_call_records()]
    assert reasons == [("refused", "policy_denied"), ("ok", None), ("refused", "call_limit")]


def test_p4_s1_ac4_denied_call_runs_no_git(harness, repo, read_glob_policy, git_processes):
    harness.review(repo, [], "--policy", str(read_glob_policy))
    without_calls = list(git_processes)
    git_processes.clear()

    harness.review(repo, [GREP_TOTAL], "--policy", str(read_glob_policy))

    assert git_processes == without_calls


@pytest.mark.parametrize("call", [GREP_TOTAL, ("bash", {"command": "ls"})], ids=["grep", "unknown"])
def test_p4_s1_ac4_only_policy_denied_for_a_known_tool_not_allowed(
    harness, repo, read_glob_policy, call
):
    """A tool outside the three stays `unknown_tool`; only a known, disallowed one is denied."""
    review_run = harness.review(repo, [call], "--policy", str(read_glob_policy))

    expected = "policy_denied" if call[0] == "grep" else "unknown_tool"
    assert refusal_reason(review_run.bodies[0]) == expected


def test_p4_s1_ac4_order_of_allowed_does_not_matter(harness, repo, write_policy):
    path = write_policy(policy_toml(allowed=["glob", "read"]))

    review_run = harness.review(repo, [GREP_TOTAL], "--policy", str(path))

    assert sorted(d.name for d in review_run.request.tools.definitions) == ["glob", "read"]
    assert refusal_reason(review_run.bodies[0]) == "policy_denied"


def test_p4_s1_ac4_pr_run_context_offers_only_the_allowed_tools(pr, pull, read_glob_policy):
    provider = MockProvider(findings=[], tool_calls=[GREP_TOTAL])

    run = run_pr(
        pr, pull, "--context", "repo", "--policy", str(read_glob_policy), provider=provider
    )

    assert run.exit_code == 0, run.stderr
    [request] = provider.requests
    assert sorted(d.name for d in request.tools.definitions) == ["glob", "read"]
    assert refusal_reason(unwrap(provider.tool_results[0])) == "policy_denied"
    assert [(r["outcome"], r["reason"]) for r in run.tool_calls] == [("refused", "policy_denied")]


def test_p4_s1_interfaces_refusal_reason_policy_denied():
    assert RefusalReason.POLICY_DENIED == "policy_denied"


async def test_p4_s1_ac4_executor_ledger_entry_is_refused_policy_denied(repo):
    caps = ToolCaps(max_calls=2, max_result_bytes=16_000, max_total_bytes=96_000)
    tools = RepoTools(
        RepoRef(git=GitRepo(repo.path, timeout_s=30.0), commit=repo.commit),
        caps=caps,
        allowed=frozenset({"read", "glob"}),
    )

    assert sorted(d.name for d in tools.definitions) == ["glob", "read"]
    denied = await tools.call(*GREP_TOTAL)
    await tools.call(*READ_CHECKOUT)
    over = await tools.call(*READ_CHECKOUT)

    assert refusal_reason(unwrap(denied)) == "policy_denied"
    assert refusal_reason(unwrap(over)) == "call_limit"
    first = tools.ledger[0]
    assert (str(first.outcome), first.reason) == ("refused", RefusalReason.POLICY_DENIED)


# --- AC5: a policy allowing no tools refuses context mode before any call -------------------------


@pytest.fixture
def no_tools_policy(write_policy: Callable[..., Path]) -> Path:
    return write_policy(policy_toml("no-tools-1", allowed=[]))


def test_p4_s1_ac5_context_repo_with_no_tools_exits_2_before_any_call(
    harness, repo, no_tools_policy
):
    review_run = harness.review(repo, [READ_CHECKOUT], "--policy", str(no_tools_policy))

    assert review_run.exit_code == USAGE_EXIT, review_run.stderr
    assert review_run.provider.requests == []
    assert re.search(NO_TOOLS_MESSAGE.format(version="no-tools-1"), review_run.stderr), (
        review_run.stderr
    )


@pytest.mark.parametrize("given_by", ["flag", "variable"])
def test_p4_s1_ac5_pr_run_context_repo_with_no_tools_exits_2_before_any_call(
    pr, pull, no_tools_policy, monkeypatch, given_by
):
    flags = ("--context", "repo") if given_by == "flag" else ()
    if given_by == "variable":
        monkeypatch.setenv("REVIEW_BOT_CONTEXT", "repo")
    provider = MockProvider(findings=[])

    run = run_pr(pr, pull, *flags, "--policy", str(no_tools_policy), provider=provider)

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []
    assert re.search(NO_TOOLS_MESSAGE.format(version="no-tools-1"), run.stderr), run.stderr


def test_p4_s1_ac5_diff_mode_with_no_tools_runs_as_usual(bot, no_tools_policy):
    provider = MockProvider(findings=[])

    done = review(bot, "--policy", str(no_tools_policy), provider=provider)

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    [request] = provider.requests
    assert request.tools is None
    assert done.report["policy"]["version"] == "no-tools-1"


def test_p4_s1_ac5_pr_run_in_diff_mode_with_no_tools_runs_as_usual(pr, pull, no_tools_policy):
    provider = MockProvider(findings=[])

    run = run_pr(pr, pull, "--policy", str(no_tools_policy), provider=provider)

    assert run.exit_code == 0, run.stderr
    [request] = provider.requests
    assert request.tools is None


# --- AC6: the policy's ceiling caps every model call ----------------------------------------------


@pytest.fixture
def ceiling_policy(write_policy: Callable[..., Path]) -> Path:
    return write_policy(policy_toml("ceiling-1", max_cost_usd="0.60"))


def test_p4_s1_ac6_diff_mode_call_is_capped_at_the_ceiling(bot, ceiling_policy, real_query_calls):
    run = bot.run(str(SAMPLE), "--provider", "claude", "--policy", str(ceiling_policy))

    assert run.exit_code == INTERNAL_EXIT, run.stderr  # the guard's own exception
    assert real_call_budgets(real_query_calls) == [0.60]


def test_p4_s1_ac6_context_mode_call_is_capped_at_the_ceiling(
    bot, repo, ceiling_policy, real_query_calls
):
    bot.run(
        str(SAMPLE),
        "--provider",
        "claude",
        "--context-repo",
        str(repo.path),
        "--policy",
        str(ceiling_policy),
    )

    assert real_call_budgets(real_query_calls) == [0.60]


def test_p4_s1_ac6_control_no_policy_no_cap_in_diff_mode(bot, real_query_calls):
    bot.run(str(SAMPLE), "--provider", "claude")

    assert real_call_budgets(real_query_calls) == [None]


def test_p4_s1_ac6_lower_flag_caps_the_call_below_the_ceiling(
    bot, ceiling_policy, real_query_calls
):
    bot.run(
        str(SAMPLE),
        "--provider",
        "claude",
        "--policy",
        str(ceiling_policy),
        "--max-cost-usd",
        "0.30",
    )

    assert real_call_budgets(real_query_calls) == [0.30]


def test_p4_s1_ac6_flag_equal_to_the_ceiling_is_allowed(bot, ceiling_policy, real_query_calls):
    bot.run(
        str(SAMPLE),
        "--provider",
        "claude",
        "--policy",
        str(ceiling_policy),
        "--max-cost-usd",
        "0.60",
    )

    assert real_call_budgets(real_query_calls) == [0.60]


def test_p4_s1_ac6_shadow_caps_show_the_ceiling_in_diff_mode(shadow, ceiling_policy):
    done, out = shadow.run("--policy", str(ceiling_policy))

    assert done.exit_code == SHADOW_OK, done.stderr
    assert as_decimal(load_shadow(out)["caps"]["max_cost_usd"]) == Decimal("0.60")


def test_p4_s1_ac6_shadow_caps_show_the_lower_flag(shadow, ceiling_policy):
    _, out = shadow.run("--policy", str(ceiling_policy), "--max-cost-usd", "0.30")

    assert as_decimal(load_shadow(out)["caps"]["max_cost_usd"]) == Decimal("0.30")


def test_p4_s1_ac6_pr_run_context_caps_show_the_ceiling(pr, pull, ceiling_policy):
    provider = MockProvider(findings=[], tool_calls=[READ_CHECKOUT])

    run = run_pr(pr, pull, "--context", "repo", "--policy", str(ceiling_policy), provider=provider)

    assert run.exit_code == 0, run.stderr
    assert as_decimal(run.shadow["caps"]["max_cost_usd"]) == Decimal("0.60")


def assert_over_ceiling(stderr: str) -> None:
    assert "1.00" in stderr, stderr
    assert "0.60" in stderr, stderr
    assert "ceiling-1" in stderr or "p.toml" in stderr, stderr


def test_p4_s1_ac6_flag_above_the_ceiling_exits_2_before_any_call(bot, ceiling_policy):
    provider = MockProvider(findings=[])

    done = review(bot, "--policy", str(ceiling_policy), "--max-cost-usd", "1.00", provider=provider)

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert provider.requests == []
    assert_over_ceiling(done.stderr)


def test_p4_s1_ac6_variable_above_the_ceiling_exits_2_too(bot, ceiling_policy, monkeypatch):
    """Design, Policy as code: a cap set above the ceiling refuses, however it is set."""
    monkeypatch.setenv("REVIEW_BOT_MAX_COST_USD", "1.00")
    provider = MockProvider(findings=[])

    done = review(bot, "--policy", str(ceiling_policy), provider=provider)

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert provider.requests == []
    assert_over_ceiling(done.stderr)


def test_p4_s1_ac6_context_mode_above_the_ceiling_exits_2_before_any_call(
    harness, repo, ceiling_policy
):
    review_run = harness.review(
        repo, [READ_CHECKOUT], "--policy", str(ceiling_policy), "--max-cost-usd", "1.00"
    )

    assert review_run.exit_code == USAGE_EXIT, review_run.stderr
    assert review_run.provider.requests == []
    assert_over_ceiling(review_run.stderr)


def test_p4_s1_ac6_shadow_above_the_ceiling_exits_2_and_writes_no_report(shadow, ceiling_policy):
    provider = MockProvider(findings=[])

    done, out = shadow.run(
        "--policy", str(ceiling_policy), "--max-cost-usd", "1.00", provider=provider
    )

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert provider.requests == []
    assert not (out / "shadow.json").exists()
    assert_over_ceiling(done.stderr)


def test_p4_s1_ac6_pr_run_above_the_ceiling_exits_2_before_any_call(pr, pull, ceiling_policy):
    provider = MockProvider(findings=[])

    run = run_pr(
        pr, pull, "--context", "repo", "--policy", str(ceiling_policy), "--max-cost-usd", "1.00",
        provider=provider,
    )  # fmt: skip

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []
    assert_over_ceiling(run.stderr)


# --- AC7: evals live and pr backfill take --policy and refuse a cap above its ceiling -------------


def test_p4_s1_ac7_evals_live_caps_each_call_at_the_ceiling(capsys, tmp_path, ceiling_policy):
    root = copy_cases(tmp_path, "sample")
    sdk = EvalSdk()

    live = run_evals(
        capsys, *live_args(root, runs=3, max_calls=3), "--max-usd", "1.80",
        "--policy", ceiling_policy, sdk=sdk,
    )  # fmt: skip

    assert live.exit_code != USAGE_EXIT, live.stderr
    assert sdk.budgets == [0.60, 0.60, 0.60]


def test_p4_s1_ac7_evals_live_lower_flag_caps_each_call(capsys, tmp_path, ceiling_policy):
    root = copy_cases(tmp_path, "sample")
    sdk = EvalSdk()

    live = run_evals(
        capsys, *live_args(root, runs=3, max_calls=3), "--max-usd", "0.90",
        "--max-cost-usd", "0.30", "--policy", ceiling_policy, sdk=sdk,
    )  # fmt: skip

    assert live.exit_code != USAGE_EXIT, live.stderr
    assert sdk.budgets == [0.30, 0.30, 0.30]


def test_p4_s1_ac7_evals_live_flag_above_the_ceiling_exits_2_before_any_call(
    capsys, tmp_path, work_dir, ceiling_policy
):
    root = copy_cases(tmp_path, "sample")
    sdk = EvalSdk()

    live = run_evals(
        capsys, *live_args(root, runs=1, max_calls=1), "--max-usd", "100",
        "--max-cost-usd", "1.00", "--policy", ceiling_policy, sdk=sdk,
    )  # fmt: skip

    assert live.exit_code == USAGE_EXIT, live.stderr
    assert sdk.calls == []
    assert_over_ceiling(live.stderr)
    assert_nothing_recorded(root, work_dir)


def test_p4_s1_ac7_evals_live_allowance_uses_the_ceiling_as_the_cap(
    capsys, tmp_path, work_dir, ceiling_policy
):
    """3 calls x 0.60 = 1.80, over 1.79."""
    root = copy_cases(tmp_path, "sample")
    sdk = EvalSdk()

    live = run_evals(
        capsys, *live_args(root, runs=3, max_calls=3), "--max-usd", "1.79",
        "--policy", ceiling_policy, sdk=sdk,
    )  # fmt: skip

    assert live.exit_code == USAGE_EXIT, live.stderr
    assert sdk.calls == []
    assert "1.79" in live.stderr, live.stderr
    assert_nothing_recorded(root, work_dir)


def test_p4_s1_ac7_evals_live_from_the_variable_takes_its_ceiling(
    capsys, tmp_path, ceiling_policy, monkeypatch
):
    monkeypatch.setenv(POLICY_VARIABLE, str(ceiling_policy))
    root = copy_cases(tmp_path, "sample")
    sdk = EvalSdk()

    live = run_evals(capsys, *live_args(root, runs=1, max_calls=1), "--max-usd", "0.60", sdk=sdk)

    assert live.exit_code != USAGE_EXIT, live.stderr
    assert sdk.budgets == [0.60]


def test_p4_s1_ac7_evals_replay_takes_policy(capsys, tmp_path, write_policy):
    """Interfaces: `evals replay` takes `--policy` too; replay makes no call either way."""
    root = tmp_path / "replay-cases"
    shutil.copytree(EVAL_CASES / "sample", root / "sample")
    path = write_policy()

    plain = run_evals(capsys, "replay", "--cases", root, sdk=EvalSdk())
    chosen = run_evals(capsys, "replay", "--cases", root, "--policy", path, sdk=EvalSdk())

    assert "unrecognized arguments" not in chosen.stderr, chosen.stderr
    assert chosen.exit_code == plain.exit_code, chosen.stderr
    assert chosen.stdout == plain.stdout


def test_p4_s1_ac7_backfill_caps_each_review_at_the_ceiling(backfill, history, ceiling_policy):
    pulls = history.merged_many(31, 32, 33)
    provider = PerPullProvider()

    run = backfill.run(
        pulls, history.repo.path, "--policy", str(ceiling_policy), "--max-usd", "1.80",
        max_calls=3, provider=provider, dollar_flags=(),
    )  # fmt: skip

    assert run.exit_code == 0, run.stderr
    assert len(provider.requests) == 3
    caps = [as_decimal(run.shadow(p.number)["caps"]["max_cost_usd"]) for p in pulls]
    assert caps == [Decimal("0.60")] * 3


def test_p4_s1_ac7_backfill_flag_above_the_ceiling_exits_2_before_any_call(
    backfill, history, ceiling_policy
):
    provider = PerPullProvider()

    run = backfill.run(
        history.merged_many(34), history.repo.path, "--policy", str(ceiling_policy),
        "--max-usd", "100", "--max-cost-usd", "1.00", max_calls=1, provider=provider,
        dollar_flags=(),
    )  # fmt: skip

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []
    assert run.reports() == []
    assert_over_ceiling(run.stderr)


def test_p4_s1_ac7_backfill_allowance_uses_the_ceiling_as_the_cap(
    backfill, history, ceiling_policy
):
    """3 PRs x 0.60 = 1.80, over 1.79."""
    provider = PerPullProvider()

    run = backfill.run(
        history.merged_many(35, 36, 37), history.repo.path, "--policy", str(ceiling_policy),
        "--max-usd", "1.79", max_calls=3, provider=provider, dollar_flags=(),
    )  # fmt: skip

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []
    assert "1.79" in run.stderr, run.stderr


# --- AC8: evals live and pr backfill refuse in every mode without a dollar cap --------------------


def test_p4_s1_ac8_spec_example_diff_mode_without_either_flag_exits_2_naming_both(
    capsys, tmp_path, work_dir
):
    """`evals live --mode diff --runs 3 --max-calls 15` with the built-in policy."""
    root = copy_cases(tmp_path, "sample", "clean", "injection-canary")
    sdk = EvalSdk()

    live = run_evals(capsys, *live_args(root, runs=3, max_calls=15), sdk=sdk)

    assert live.exit_code == USAGE_EXIT, live.stderr
    assert sdk.calls == []
    assert "--max-usd" in live.stderr, live.stderr
    assert "--max-cost-usd" in live.stderr, live.stderr
    assert_nothing_recorded(root, work_dir)


def test_p4_s1_ac8_default_mode_is_refused_too(capsys, tmp_path, work_dir):
    root = copy_cases(tmp_path, "sample")
    sdk = EvalSdk()

    live = run_evals(capsys, "live", "--cases", root, "--runs", "1", "--max-calls", "1", sdk=sdk)

    assert live.exit_code == USAGE_EXIT, live.stderr
    assert sdk.calls == []
    assert "--max-usd" in live.stderr and "--max-cost-usd" in live.stderr, live.stderr


@pytest.mark.parametrize(
    ("given", "missing"),
    [
        pytest.param(["--max-usd", "1.00"], "--max-cost-usd", id="no-per-call-cap"),
        pytest.param(["--max-cost-usd", "0.10"], "--max-usd", id="no-max-usd"),
    ],
)
def test_p4_s1_ac8_evals_live_diff_mode_names_the_missing_flag(
    capsys, tmp_path, work_dir, given, missing
):
    root = copy_cases(tmp_path, "sample")
    sdk = EvalSdk()

    live = run_evals(capsys, *live_args(root, runs=3, max_calls=3), *given, sdk=sdk)

    assert live.exit_code == USAGE_EXIT, live.stderr
    assert sdk.calls == []
    assert missing in live.stderr, live.stderr
    assert_nothing_recorded(root, work_dir)


def test_p4_s1_ac8_evals_live_ceiling_without_max_usd_names_max_usd(
    capsys, tmp_path, work_dir, ceiling_policy
):
    root = copy_cases(tmp_path, "sample")
    sdk = EvalSdk()

    live = run_evals(
        capsys, *live_args(root, runs=1, max_calls=1), "--policy", ceiling_policy, sdk=sdk
    )

    assert live.exit_code == USAGE_EXIT, live.stderr
    assert sdk.calls == []
    assert "--max-usd" in live.stderr, live.stderr


def test_p4_s1_ac8_evals_live_diff_mode_over_its_allowance_exits_2(capsys, tmp_path, work_dir):
    """3 calls x 0.10 = 0.30, over 0.29."""
    root = copy_cases(tmp_path, "sample")
    sdk = EvalSdk()

    live = run_evals(
        capsys, *live_args(root, runs=3, max_calls=3), "--max-usd", "0.29",
        "--max-cost-usd", "0.10", sdk=sdk,
    )  # fmt: skip

    assert live.exit_code == USAGE_EXIT, live.stderr
    assert sdk.calls == []


def test_p4_s1_ac8_evals_live_diff_mode_exact_fit_runs_capped(capsys, tmp_path):
    root = copy_cases(tmp_path, "sample")
    sdk = EvalSdk()

    live = run_evals(
        capsys, *live_args(root, runs=3, max_calls=3), "--max-usd", "0.30",
        "--max-cost-usd", "0.10", sdk=sdk,
    )  # fmt: skip

    assert live.exit_code != USAGE_EXIT, live.stderr
    assert sdk.budgets == [0.10, 0.10, 0.10]


def test_p4_s1_ac8_backfill_diff_mode_without_either_flag_exits_2_naming_both(backfill, history):
    provider = PerPullProvider()

    run = backfill.run(
        history.merged_many(41, 42), history.repo.path, max_calls=2, provider=provider,
        dollar_flags=(),
    )  # fmt: skip

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []
    assert run.reports() == []
    assert "--max-usd" in run.stderr and "--max-cost-usd" in run.stderr, run.stderr


@pytest.mark.parametrize(
    ("given", "missing"),
    [
        pytest.param(["--max-usd", "1.00"], "--max-cost-usd", id="no-per-call-cap"),
        pytest.param(["--max-cost-usd", "0.10"], "--max-usd", id="no-max-usd"),
    ],
)
def test_p4_s1_ac8_backfill_diff_mode_names_the_missing_flag(backfill, history, given, missing):
    provider = PerPullProvider()

    run = backfill.run(
        history.merged_many(43), history.repo.path, *given, max_calls=1, provider=provider,
        dollar_flags=(),
    )  # fmt: skip

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []
    assert missing in run.stderr, run.stderr


def test_p4_s1_ac8_backfill_ceiling_and_max_usd_suffice(backfill, history, ceiling_policy):
    provider = PerPullProvider()

    run = backfill.run(
        history.merged_many(44), history.repo.path, "--policy", str(ceiling_policy),
        "--max-usd", "0.60", max_calls=1, provider=provider, dollar_flags=(),
    )  # fmt: skip

    assert run.exit_code == 0, run.stderr
    assert len(provider.requests) == 1


def test_p4_s1_ac8_backfill_diff_mode_over_its_allowance_exits_2(backfill, history):
    """2 PRs x 0.10 = 0.20, over 0.19."""
    provider = PerPullProvider()

    run = backfill.run(
        history.merged_many(45, 46), history.repo.path, "--max-usd", "0.19",
        "--max-cost-usd", "0.10", max_calls=2, provider=provider, dollar_flags=(),
    )  # fmt: skip

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []


def test_p4_s1_ac8_backfill_diff_mode_with_both_flags_runs(backfill, history):
    provider = PerPullProvider()

    run = backfill.run(
        history.merged_many(47, 48), history.repo.path, "--max-usd", "0.20",
        "--max-cost-usd", "0.10", max_calls=2, provider=provider, dollar_flags=(),
    )  # fmt: skip

    assert run.exit_code == 0, run.stderr
    assert len(provider.requests) == 2


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p4_s1_ac8_live_yml_evals_steps_pass_both_flags_within_their_allowance(
    workflow, tmp_path, step
):
    argv = run_live_step(workflow, step.choice, tmp_path / "step").argv

    assert_dollar_flags_fit(argv)


def test_p4_s1_ac8_live_yml_backfill_step_passes_both_flags_within_its_allowance():
    """P3-S8 AC11 checks the step runs exactly `BACKFILL_ARGS`."""
    assert_dollar_flags_fit(BACKFILL_ARGS)


def assert_dollar_flags_fit(argv: Sequence[str]) -> None:
    def value(flag: str) -> Decimal:
        assert argv.count(flag) == 1, (flag, argv)
        return Decimal(argv[argv.index(flag) + 1])

    assert value("--max-calls") * value("--max-cost-usd") <= value("--max-usd"), argv


@pytest.mark.parametrize("step", STEPS, ids=STEP_IDS)
def test_p4_s1_ac8_live_yml_evals_steps_still_parse_with_the_real_parser(
    workflow, tmp_path, monkeypatch, capsys, step
):
    """In CI for a `push`, a parsed command stops at the dispatch guard, nothing else."""
    argv = run_live_step(workflow, step.choice, tmp_path / "step").argv
    empty = tmp_path / "empty"
    empty.mkdir()
    capsys.readouterr()

    exit_code, sdk = parse_in_ci(argv, empty, monkeypatch)

    stderr = capsys.readouterr().err
    assert exit_code == USAGE_EXIT, stderr
    assert CI_REFUSAL.search(stderr), stderr
    assert sdk.calls == 0
