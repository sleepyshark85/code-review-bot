r"""P2-S2: review a pull request from its event file and a clone, within the per-run caps.

Written from the spec's criteria, design.md (Interfaces, Safety, CI settings and caps) and ADRs
0008 and 0012, before the developer's code was read. Every run calls `review_bot.pr.cli.main` in
process with `["run", "--event", PATH, "--repo", DIR, "--out", OUT, ...]` and a scripted provider
passed as `provider=` (the one place to change: `PrRunner.run`). No run reaches a model or GitHub.

Each pull request is a real git repository in `tmp_path`: a base commit on `main`, a head commit
on `feature`, and `main` checked out, as CI's base checkout is. Event files are hand-written
`pull_request` payloads with GitHub's field names.

"No git call" is observed by pointing `--repo` at an empty directory that isn't a repository:
any git command there fails, so a run that reaches git can't end `skipped` with exit 0.
"""

import importlib
import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_p1_s4_audit_log import events, only, read_records
from test_p2_s1_shadow_report_local import anchor_of, everything_in, load_shadow

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.providers.base import TokenUsage
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

REPO_NAME = "octo/review-bot"
NUMBER = 7
UPDATED_AT = "2026-11-08T10:11:12Z"
# design.md: timestamps are UTC; the run writes the `+00:00` form.
EVENT_AT = "2026-11-08T10:11:12+00:00"
ABSENT_SHA = "0123456789abcdef0123456789abcdef01234567"
AUDIT_VARIABLE = "REVIEW_BOT_AUDIT_LOG"
REVIEW_BYTES_VARIABLE = "REVIEW_BOT_MAX_REVIEW_BYTES"
OUTPUT_TOKENS_VARIABLE = "REVIEW_BOT_MAX_OUTPUT_TOKENS"
ERROR_EXIT = 19
# Generous: it catches a hang or reading the whole file, not a slow CI machine.
BOUND_S = 60.0

APP = "src/app.py"
BASE_APP = "alpha = 1\nbeta = 2\ngamma = 3\n"
# Two lines inserted on top, so the head's line numbers differ from the base's.
HEAD_APP = "new_one = 1\nnew_two = 2\n" + BASE_APP


@pytest.fixture(autouse=True)
def outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub Actions sets `CI`; AC5 makes it matter, so each test sets it only on purpose."""
    monkeypatch.delenv("CI", raising=False)


# A real repository with a base and a head commit.


class GitRepo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q", "--initial-branch=main")
        for key, value in {
            "user.name": "Tester",
            "user.email": "tester@example.invalid",
            "commit.gpgsign": "false",
            "core.autocrlf": "false",
        }.items():
            self.git("config", key, value)

    def git(self, *args: str) -> str:
        result = subprocess.run(  # noqa: S603 - fixed git arguments, test-owned repository
            ["git", "-C", str(self.path), *args],  # noqa: S607 - git from PATH, as the bot uses
            capture_output=True,
            check=True,
        )
        return result.stdout.decode("utf-8", "replace")

    def commit(self, files: dict[str, str], message: str = "change") -> str:
        for name, content in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def branch(self, name: str) -> None:
        self.git("checkout", "-q", "-b", name)

    def checkout(self, name: str) -> None:
        self.git("checkout", "-q", name)

    def diff_bytes(self, base: str, head: str, *paths: str) -> int:
        return len(
            self.git(
                "diff", "--no-ext-diff", "--no-textconv", f"{base}...{head}", "--", *paths
            ).encode()
        )


@dataclass(frozen=True, slots=True)
class PullRequest:
    repo: GitRepo
    base: str
    head: str


def make_pull_request(
    path: Path, head_files: dict[str, str], base_files: dict[str, str] | None = None
) -> PullRequest:
    """Base commit `base_files` on `main`, head `head_files` on `feature`; `main` checked out."""
    repo = GitRepo(path)
    base = repo.commit(base_files or {APP: BASE_APP}, "base")
    repo.branch("feature")
    head = repo.commit(head_files, "head")
    repo.checkout("main")
    return PullRequest(repo, base, head)


@pytest.fixture
def pull_request(tmp_path: Path) -> PullRequest:
    return make_pull_request(tmp_path / "clone", {APP: HEAD_APP})


@pytest.fixture
def not_a_repo(tmp_path: Path) -> Path:
    path = tmp_path / "not-a-repo"
    path.mkdir()
    return path


# The event payload.


def event(
    base: str,
    head: str,
    *,
    action: str = "opened",
    head_repo: str | None = REPO_NAME,
) -> dict[str, Any]:
    """A `pull_request` event with GitHub's field names, for a same-repo PR by default."""
    return {
        "action": action,
        "number": NUMBER,
        "pull_request": {
            "number": NUMBER,
            "title": "Add new values",
            "body": "Adds two values.",
            "draft": False,
            "state": "open",
            "updated_at": UPDATED_AT,
            "base": {"ref": "main", "sha": base, "repo": {"full_name": REPO_NAME}},
            "head": {
                "ref": "feature",
                "sha": head,
                "repo": None if head_repo is None else {"full_name": head_repo},
            },
        },
        "repository": {"full_name": REPO_NAME},
    }


# Running the command and reading what it writes.


@dataclass(frozen=True, slots=True)
class Run:
    exit_code: int
    stdout: str
    stderr: str
    out: Path

    @property
    def shadow(self) -> dict[str, Any]:
        return load_shadow(self.out)

    @property
    def stderr_lines(self) -> list[str]:
        return [line for line in self.stderr.splitlines() if line.strip()]


class PrRunner:
    def __init__(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        self._capsys = capsys
        self._tmp_path = tmp_path

    def write_event(self, payload: dict[str, Any] | str | bytes) -> Path:
        path = self._tmp_path / "event.json"
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        path.write_bytes(payload.encode("utf-8") if isinstance(payload, str) else payload)
        return path

    def run(
        self,
        payload: dict[str, Any] | str | bytes,
        repo: Path,
        *flags: str,
        provider: Any = None,
        provider_name: str = "mock",
    ) -> Run:
        """`run --event PATH --repo DIR --out OUT --provider NAME`, in process."""
        pr_cli = importlib.import_module("review_bot.pr.cli")
        event_path = self.write_event(payload)
        out = self._tmp_path / "out"
        self._capsys.readouterr()
        exit_code = pr_cli.main(
            [
                "run", "--event", str(event_path), "--repo", str(repo), "--out", str(out),
                "--provider", provider_name, *flags,
            ],
            provider=provider,
        )  # fmt: skip
        captured = self._capsys.readouterr()
        return Run(exit_code, captured.out, captured.err, out)


@pytest.fixture
def pr(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> PrRunner:
    return PrRunner(capsys, tmp_path)


def finding_at(file: str, line: int) -> Finding:
    return Finding(
        file=file, line=line, severity="high", category="logic", rationale="R.", confidence=0.9
    )


def assert_event_error(run: Run, pattern: str) -> None:
    """Exit 19: one `review-bot: error:` line saying what is unusable, and no report."""
    assert run.exit_code == ERROR_EXIT, run.stderr
    assert len(run.stderr_lines) == 1, run.stderr
    (line,) = run.stderr_lines
    assert line.startswith("review-bot: error: "), line
    assert re.search(pattern, line), line
    assert not (run.out / "shadow.json").exists()
    assert not (run.out / "shadow.md").exists()


def assert_skipped(run: Run, reason: str) -> None:
    assert run.exit_code == 0, run.stderr
    document = run.shadow
    assert (document["outcome"], document["reason"]) == ("skipped", reason)
    assert document["report"] is None
    assert document["comments"] == []


def prompt_of(provider: MockProvider) -> str:
    (request,) = provider.requests
    return request.user_prompt


# AC1: an `opened` same-repo PR is reviewed as an event-origin shadow run on the head's lines.


def test_p2_s2_ac1_opened_same_repo_pr_is_an_event_shadow_run_with_the_events_pr(pr, pull_request):
    provider = MockProvider(findings=[])

    run = pr.run(
        event(pull_request.base, pull_request.head), pull_request.repo.path, provider=provider
    )

    assert run.exit_code == 0, run.stderr
    document = run.shadow
    assert (document["mode"], document["origin"], document["outcome"]) == (
        "shadow",
        "event",
        "reviewed",
    )
    assert document["pr"] == {
        "repo": REPO_NAME,
        "number": NUMBER,
        "base_sha": pull_request.base,
        "head_sha": pull_request.head,
    }
    assert document["event_at"] == EVENT_AT
    assert len(provider.requests) == 1


def test_p2_s2_ac1_finding_is_anchored_on_the_heads_line(pr, pull_request):
    """Head line 2 is `new_two = 2`; in the base, line 2 is `beta = 2`."""
    provider = MockProvider(findings=[finding_at(APP, 2)])

    run = pr.run(
        event(pull_request.base, pull_request.head), pull_request.repo.path, provider=provider
    )

    assert run.exit_code == 0, run.stderr
    comments = run.shadow["comments"]
    assert [(c["action"], c["file"], c["line"], c["anchor"]) for c in comments] == [
        ("create", APP, 2, anchor_of("new_two = 2"))
    ]
    assert "+new_two = 2" in prompt_of(provider)


# AC2: the base moved on after the PR branched; only merge-base...head is reviewed.


def test_p2_s2_ac2_only_the_prs_own_changes_are_reviewed_after_the_base_moves(pr, tmp_path):
    repo = GitRepo(tmp_path / "clone")
    repo.commit({APP: BASE_APP, "src/other.py": "kept = 1\n"}, "base")
    repo.branch("feature")
    head = repo.commit({APP: HEAD_APP}, "head")
    repo.checkout("main")
    moved_base = repo.commit({"src/other.py": "kept = 1\nbase_only_change = 2\n"}, "main moves")
    provider = MockProvider(findings=[])

    run = pr.run(event(moved_base, head), repo.path, provider=provider)

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    prompt = prompt_of(provider)
    assert "+new_two = 2" in prompt
    assert "base_only_change" not in prompt
    assert "src/other.py" not in prompt


# AC3: which actions are reviewed, and which are skipped without git or the provider.


@pytest.mark.parametrize("action", ["synchronize", "reopened", "ready_for_review"])
def test_p2_s2_ac3_reviewable_action_is_reviewed_as_opened(pr, pull_request, action):
    provider = MockProvider(findings=[])

    run = pr.run(
        event(pull_request.base, pull_request.head, action=action),
        pull_request.repo.path,
        provider=provider,
    )

    assert run.exit_code == 0, run.stderr
    document = run.shadow
    assert (document["mode"], document["origin"], document["outcome"]) == (
        "shadow",
        "event",
        "reviewed",
    )
    assert document["pr"]["head_sha"] == pull_request.head
    assert len(provider.requests) == 1


@pytest.mark.parametrize("action", ["closed", "edited", "labeled"])
def test_p2_s2_ac3_other_action_is_skipped_action_without_git_or_provider(pr, not_a_repo, action):
    provider = MockProvider(findings=[])

    run = pr.run(event("a" * 40, "b" * 40, action=action), not_a_repo, provider=provider)

    assert_skipped(run, "action")
    assert provider.requests == []


# AC4: a fork, or a deleted fork, is skipped before git and the provider.


@pytest.mark.parametrize("head_repo", ["someone/review-bot", None], ids=["fork", "deleted_fork"])
def test_p2_s2_ac4_fork_is_skipped_fork_without_git_or_provider(pr, not_a_repo, head_repo):
    provider = MockProvider(findings=[])

    run = pr.run(event("a" * 40, "b" * 40, head_repo=head_repo), not_a_repo, provider=provider)

    assert_skipped(run, "fork")
    assert provider.requests == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [({"action": "closed"}, "action"), ({"head_repo": None}, "fork")],
    ids=["action", "fork"],
)
def test_p2_s2_ac3_ac4_refusal_is_recorded_in_the_audit_log_under_out(
    pr, not_a_repo, monkeypatch, change, reason
):
    monkeypatch.delenv(AUDIT_VARIABLE)

    run = pr.run(event("a" * 40, "b" * 40, **change), not_a_repo, provider=MockProvider())

    assert_skipped(run, reason)
    assert only(read_records(run.out / "audit.jsonl"), "run.refused") == {"reason": reason}


# AC5: in CI, the Claude provider needs ANTHROPIC_API_KEY; locally it uses the login.


@pytest.mark.parametrize("key", [None, ""], ids=["unset", "empty"])
def test_p2_s2_ac5_ci_claude_without_a_key_is_skipped_no_credential(
    pr, pull_request, monkeypatch, key
):
    monkeypatch.setenv("CI", "true")
    if key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    provider = MockProvider(findings=[])

    run = pr.run(
        event(pull_request.base, pull_request.head),
        pull_request.repo.path,
        provider=provider,
        provider_name="claude",
    )

    assert_skipped(run, "no_credential")
    assert provider.requests == []


def test_p2_s2_ac5_ci_claude_without_a_key_builds_no_provider(pr, pull_request, monkeypatch):
    """No provider injected: building and calling Claude would reach the suite's SDK guard."""
    monkeypatch.setenv("CI", "true")

    run = pr.run(
        event(pull_request.base, pull_request.head), pull_request.repo.path, provider_name="claude"
    )

    assert_skipped(run, "no_credential")


def test_p2_s2_ac5_ci_claude_with_a_key_goes_ahead(pr, pull_request, monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-sentinel")
    provider = MockProvider(findings=[])

    run = pr.run(
        event(pull_request.base, pull_request.head),
        pull_request.repo.path,
        provider=provider,
        provider_name="claude",
    )

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    assert len(provider.requests) == 1


def test_p2_s2_ac5_outside_ci_claude_without_a_key_goes_ahead(pr, pull_request):
    provider = MockProvider(findings=[])

    run = pr.run(
        event(pull_request.base, pull_request.head),
        pull_request.repo.path,
        provider=provider,
        provider_name="claude",
    )

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    assert len(provider.requests) == 1


# AC6: the reviewable-bytes cap counts only what's sent to the model, after skipping.

REVIEW_CAP = "120000"


def python_lines(count: int) -> str:
    """`count` lines of 50 bytes each, with no rule hit and nothing secret-shaped."""
    return "".join(f'item_{n:05} = "the quick brown fox jumps over"\n' for n in range(count))


def docs_lines(count: int) -> str:
    return "".join(f"Line {n:06} of the guide: " + "words " * 13 + "end.\n" for n in range(count))


@pytest.fixture
def mostly_docs(tmp_path: Path) -> PullRequest:
    """About 300,000 diff bytes: 250,000 of a docs file and 50,000 of Python."""
    return make_pull_request(
        tmp_path / "docs-clone",
        {"docs/guide.md": docs_lines(2_300), "src/big.py": python_lines(980)},
    )


@pytest.fixture
def too_much_code(tmp_path: Path) -> PullRequest:
    """About 130,000 diff bytes of Python (47 bytes a line in the diff)."""
    return make_pull_request(tmp_path / "code-clone", {"src/big.py": python_lines(2_800)})


def test_p2_s2_ac6_precondition_diff_sizes(mostly_docs, too_much_code):
    total = mostly_docs.repo.diff_bytes(mostly_docs.base, mostly_docs.head)
    code = mostly_docs.repo.diff_bytes(mostly_docs.base, mostly_docs.head, "src/big.py")
    large = too_much_code.repo.diff_bytes(too_much_code.base, too_much_code.head)

    assert 290_000 <= total <= 310_000, total
    assert 45_000 <= code <= 55_000, code
    assert 125_000 <= large <= 135_000, large


def test_p2_s2_ac6_large_diff_of_mostly_docs_is_reviewed_under_the_cap(
    pr, mostly_docs, monkeypatch
):
    monkeypatch.setenv(REVIEW_BYTES_VARIABLE, REVIEW_CAP)
    provider = MockProvider(findings=[])

    run = pr.run(
        event(mostly_docs.base, mostly_docs.head), mostly_docs.repo.path, provider=provider
    )

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    assert len(provider.requests) == 1


def test_p2_s2_ac6_reviewable_bytes_over_the_cap_are_skipped_too_large_without_a_call(
    pr, too_much_code, monkeypatch
):
    monkeypatch.setenv(REVIEW_BYTES_VARIABLE, REVIEW_CAP)
    provider = MockProvider(findings=[])

    run = pr.run(
        event(too_much_code.base, too_much_code.head), too_much_code.repo.path, provider=provider
    )

    assert_skipped(run, "too_large")
    assert provider.requests == []


def write_pr_diff(pull: PullRequest, tmp_path: Path) -> Path:
    path = tmp_path / "pr.diff"
    path.write_bytes(
        pull.repo.git(
            "diff", "--no-ext-diff", "--no-textconv", f"{pull.base}...{pull.head}"
        ).encode()
    )
    return path


def test_p2_s2_ac6_review_bot_exits_6_over_the_reviewable_cap(
    bot, too_much_code, tmp_path, monkeypatch
):
    monkeypatch.setenv(REVIEW_BYTES_VARIABLE, REVIEW_CAP)
    provider = MockProvider(findings=[])

    run = bot.review(write_pr_diff(too_much_code, tmp_path), provider=provider)

    assert run.exit_code == 6, run.stderr
    assert provider.requests == []


# AC7: the output token cap reaches the Claude CLI; invalid caps are usage errors.


def test_p2_s2_ac7_output_token_cap_is_passed_to_the_claude_cli(claude_review, monkeypatch):
    """The Claude CLI reads its output cap from `CLAUDE_CODE_MAX_OUTPUT_TOKENS`."""
    from fake_sdk import succeeds  # noqa: PLC0415 - as Phase 0's lockdown tests import it

    monkeypatch.setenv(OUTPUT_TOKENS_VARIABLE, "16000")
    fake = succeeds()

    claude_review(fake)

    assert fake.options.env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == "16000", fake.options.env


def test_p2_s2_ac7_both_caps_unset_leave_the_claude_env_as_before(claude_review):
    from fake_sdk import succeeds  # noqa: PLC0415 - as Phase 0's lockdown tests import it

    fake = succeeds()

    claude_review(fake)

    assert fake.options.env == {"CLAUDE_CODE_MAX_RETRIES": "2", "API_TIMEOUT_MS": "90000"}


def test_p2_s2_ac7_both_caps_unset_review_bot_reviews_a_large_diff(bot, too_much_code, tmp_path):
    provider = MockProvider(findings=[])

    run = bot.review(write_pr_diff(too_much_code, tmp_path), provider=provider)

    assert run.exit_code == 0, run.stderr
    assert len(provider.requests) == 1


INVALID_CAPS = [
    (variable, value)
    for variable in (REVIEW_BYTES_VARIABLE, OUTPUT_TOKENS_VARIABLE)
    for value in ("0", "-1", "abc")
]


@pytest.mark.parametrize(("variable", "value"), INVALID_CAPS)
def test_p2_s2_ac7_invalid_cap_makes_review_bot_exit_2_naming_it(
    bot, write_diff, monkeypatch, variable, value
):
    from diff_support import MINIMAL_DIFF  # noqa: PLC0415 - only these cases need a diff

    monkeypatch.setenv(variable, value)
    provider = MockProvider(findings=[])

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 2, run.stderr
    assert variable in run.stderr
    assert provider.requests == []


@pytest.mark.parametrize(("variable", "value"), INVALID_CAPS)
def test_p2_s2_ac7_invalid_cap_makes_run_exit_2_naming_it(
    pr, pull_request, monkeypatch, variable, value
):
    monkeypatch.setenv(variable, value)
    provider = MockProvider(findings=[])

    run = pr.run(
        event(pull_request.base, pull_request.head), pull_request.repo.path, provider=provider
    )

    assert run.exit_code == 2, run.stderr
    assert variable in run.stderr
    assert provider.requests == []


# AC8: one call at most, and the effective caps in the report.


def test_p2_s2_ac8_caps_list_the_effective_values(pr, pull_request, monkeypatch):
    monkeypatch.setenv(REVIEW_BYTES_VARIABLE, REVIEW_CAP)
    monkeypatch.setenv(OUTPUT_TOKENS_VARIABLE, "16000")
    monkeypatch.setenv("REVIEW_BOT_MAX_RETRIES", "0")
    monkeypatch.setenv("REVIEW_BOT_DEADLINE_S", "240")

    run = pr.run(
        event(pull_request.base, pull_request.head),
        pull_request.repo.path,
        provider=MockProvider(findings=[]),
    )

    assert run.exit_code == 0, run.stderr
    assert list(run.shadow["caps"].items()) == [
        ("max_diff_bytes", 400_000),
        ("max_review_bytes", 120_000),
        ("max_output_tokens", 16_000),
        ("max_retries", 0),
        ("deadline_s", 240),
    ]


def test_p2_s2_ac8_unset_new_caps_are_null_and_the_rest_are_the_defaults(pr, pull_request):
    run = pr.run(
        event(pull_request.base, pull_request.head),
        pull_request.repo.path,
        provider=MockProvider(findings=[]),
    )

    assert run.exit_code == 0, run.stderr
    assert list(run.shadow["caps"].items()) == [
        ("max_diff_bytes", 400_000),
        ("max_review_bytes", None),
        ("max_output_tokens", None),
        ("max_retries", 2),
        ("deadline_s", 300),
    ]


@pytest.mark.parametrize(
    "provider_factory",
    [
        lambda: MockProvider(findings=[finding_at(APP, 2)]),
        lambda: MockProvider(error=ProviderError(ErrorCategory.TRANSIENT, "Try again.")),
    ],
    ids=["reviewed", "transient_failure"],
)
def test_p2_s2_ac8_provider_is_called_at_most_once(pr, pull_request, provider_factory):
    provider = provider_factory()

    pr.run(event(pull_request.base, pull_request.head), pull_request.repo.path, provider=provider)

    assert len(provider.requests) == 1


# AC9: reported usage over the output cap is flagged, not failed.


# "Exceeds": exactly the cap is not over it.
@pytest.mark.parametrize(("tokens", "over"), [(20_000, True), (16_001, True), (16_000, False)])
def test_p2_s2_ac9_output_tokens_over_the_cap_set_over_budget_and_stay_reviewed(
    pr, pull_request, monkeypatch, tokens, over
):
    monkeypatch.setenv(OUTPUT_TOKENS_VARIABLE, "16000")

    run = pr.run(
        event(pull_request.base, pull_request.head),
        pull_request.repo.path,
        provider=MockProvider(findings=[], usage=TokenUsage(1_000, tokens)),
    )

    assert run.exit_code == 0, run.stderr
    document = run.shadow
    assert document["outcome"] == "reviewed"
    assert document["over_budget"] is over


def test_p2_s2_ac9_no_output_cap_is_never_over_budget(pr, pull_request):
    run = pr.run(
        event(pull_request.base, pull_request.head),
        pull_request.repo.path,
        provider=MockProvider(findings=[], usage=TokenUsage(1_000, 20_000)),
    )

    assert run.exit_code == 0, run.stderr
    assert run.shadow["over_budget"] is False


# AC10: an unusable event exits 19 with one line naming the field, before git or the provider.


def set_in(payload: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    node = payload
    *parents, last = path.split(".")
    for key in parents:
        node = node[key]
    node[last] = value
    return payload


def bad_repo(payload: dict[str, Any], value: str) -> dict[str, Any]:
    """The same bad name everywhere, so the PR still reads as same-repo."""
    for path in ("repository.full_name", "pull_request.base.repo.full_name",
                 "pull_request.head.repo.full_name"):  # fmt: skip
        set_in(payload, path, value)
    return payload


def bad_number(payload: dict[str, Any], value: Any) -> dict[str, Any]:
    set_in(payload, "number", value)
    return set_in(payload, "pull_request.number", value)


BASE_SHA_ERROR = r"pull_request\.base\.sha isn't a 40-hex-digit SHA"
HEAD_SHA_ERROR = r"pull_request\.head\.sha isn't a 40-hex-digit SHA"
MALFORMED: dict[str, tuple[Callable[[dict[str, Any]], dict[str, Any]], str]] = {
    "no_pull_request": (lambda p: {k: v for k, v in p.items() if k != "pull_request"},
                        "lacks pull_request"),
    "base_sha_39_hex": (lambda p: set_in(p, "pull_request.base.sha", "a" * 39), BASE_SHA_ERROR),
    "base_sha_not_hex": (lambda p: set_in(p, "pull_request.base.sha", "g" * 40), BASE_SHA_ERROR),
    "head_sha_option": (lambda p: set_in(p, "pull_request.head.sha", "--output=/tmp/pwned"),
                        HEAD_SHA_ERROR),
    "head_sha_null": (lambda p: set_in(p, "pull_request.head.sha", None), HEAD_SHA_ERROR),
    "repo_no_slash": (lambda p: bad_repo(p, "review-bot"), r"repo"),
    "repo_extra_part": (lambda p: bad_repo(p, "octo/review-bot/x"), r"repo"),
    "number_zero": (lambda p: bad_number(p, 0), r"pull_request\.number"),
    "number_negative": (lambda p: bad_number(p, -3), r"pull_request\.number"),
    "number_string": (lambda p: bad_number(p, "7"), r"pull_request\.number"),
    "number_bool": (lambda p: bad_number(p, True), r"pull_request\.number"),
    "number_float": (lambda p: bad_number(p, 1.5), r"pull_request\.number"),
}  # fmt: skip


@pytest.mark.parametrize("case", MALFORMED)
def test_p2_s2_ac10_malformed_field_exits_19_with_one_line_naming_it(
    pr, pull_request, not_a_repo, case
):
    mutate, field = MALFORMED[case]
    payload = mutate(event(pull_request.base, pull_request.head))
    provider = MockProvider(findings=[])

    # `--repo` isn't a repository: reaching git first would name git, not the field.
    run = pr.run(payload, not_a_repo, provider=provider)

    assert_event_error(run, rf"--event .*{field}")
    assert provider.requests == []


NOT_JSON = {"broken": b"{not json", "empty": b"", "binary": b"\xff\xfe\x00"}


@pytest.mark.parametrize("content", NOT_JSON.values(), ids=NOT_JSON.keys())
def test_p2_s2_ac10_event_that_isnt_json_exits_19_with_one_line(pr, not_a_repo, content):
    provider = MockProvider(findings=[])

    run = pr.run(content, not_a_repo, provider=provider)

    assert_event_error(run, r"--event .*isn't JSON")
    assert provider.requests == []


def test_p2_s2_ac10_json_that_isnt_an_object_exits_19_with_one_line(pr, not_a_repo):
    provider = MockProvider(findings=[])

    run = pr.run(b"[1, 2]", not_a_repo, provider=provider)

    assert_event_error(run, r"--event .*(isn't a JSON object|lacks pull_request)")
    assert provider.requests == []


def test_p2_s2_limits_event_over_10_mb_exits_19(pr, pull_request):
    """A valid event padded past 10 MB, by either reading of MB, still isn't read."""
    provider = MockProvider(findings=[])
    valid = json.dumps(event(pull_request.base, pull_request.head))
    padded = valid[:-1] + " " * (11_000_000 - len(valid)) + "}"

    started = time.monotonic()
    run = pr.run(padded, pull_request.repo.path, provider=provider)
    elapsed = time.monotonic() - started

    assert_event_error(run, r"--event .*is over 10000000 bytes")
    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    assert provider.requests == []


# AC11: a head commit the clone lacks.


def test_p2_s2_ac11_missing_head_sha_exits_19_naming_it(pr, pull_request):
    provider = MockProvider(findings=[])

    run = pr.run(event(pull_request.base, ABSENT_SHA), pull_request.repo.path, provider=provider)

    assert_event_error(run, rf"Commit {ABSENT_SHA} isn't in --repo ")
    assert provider.requests == []


# AC12: no external diff and no textconv from the clone's attributes and config.

TEXTCONV_OUTPUT = "TEXTCONV-OUTPUT"


def plant_diff_driver(repo: GitRepo, traps: Path) -> tuple[Path, Path]:
    """`*.py` gets driver `trap`, whose textconv and external command each write a marker.

    The textconv prefixes every line, so its output is visible in a diff.
    """
    traps.mkdir()
    textconv_marker, command_marker = traps / "textconv.marker", traps / "command.marker"
    (repo.path / ".gitattributes").write_bytes(b"*.py diff=trap\n")
    repo.git(
        "config", "diff.trap.textconv",
        f"touch '{textconv_marker.as_posix()}' && sed 's/^/{TEXTCONV_OUTPUT} /'",
    )  # fmt: skip
    repo.git("config", "diff.trap.command", f"touch '{command_marker.as_posix()}' && true")
    return textconv_marker, command_marker


def test_p2_s2_ac12_control_planted_driver_runs_under_plain_git_diff(pull_request, tmp_path):
    """The trap works: git's own defaults run the external command, then the textconv."""
    textconv_marker, command_marker = plant_diff_driver(pull_request.repo, tmp_path / "traps")
    span = f"{pull_request.base}...{pull_request.head}"

    pull_request.repo.git("diff", span)
    output = pull_request.repo.git("diff", "--no-ext-diff", span)

    assert command_marker.exists()
    assert textconv_marker.exists()
    assert TEXTCONV_OUTPUT in output


def test_p2_s2_ac12_diff_driver_isnt_run_and_the_plain_change_is_reviewed(
    pr, pull_request, tmp_path
):
    textconv_marker, command_marker = plant_diff_driver(pull_request.repo, tmp_path / "traps")
    provider = MockProvider(findings=[])

    run = pr.run(
        event(pull_request.base, pull_request.head), pull_request.repo.path, provider=provider
    )

    assert run.exit_code == 0, run.stderr
    assert not textconv_marker.exists()
    assert not command_marker.exists()
    prompt = prompt_of(provider)
    assert "+new_one = 1\n+new_two = 2\n" in prompt
    assert TEXTCONV_OUTPUT not in prompt


# AC13: the PR's title and body are kept nowhere.

CANARY = "CANARY-TITLE-7f3a9c"


def test_p2_s2_ac13_title_and_body_text_is_in_neither_the_report_nor_the_audit_log(
    pr, pull_request, monkeypatch
):
    monkeypatch.delenv(AUDIT_VARIABLE)
    hostile = f"{CANARY}\x00\x1b[2J\u202e\r\n{CANARY}"
    payload = event(pull_request.base, pull_request.head)
    set_in(payload, "pull_request.title", hostile)
    set_in(payload, "pull_request.body", hostile)

    run = pr.run(
        payload, pull_request.repo.path, provider=MockProvider(findings=[finding_at(APP, 2)])
    )

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    assert CANARY.encode() not in (run.out / "shadow.json").read_bytes()
    assert CANARY.encode() not in (run.out / "audit.jsonl").read_bytes()
    assert CANARY.encode() not in everything_in(run.out)


# AC14: the audit log defaults to OUT/audit.jsonl, and the key is in no file under OUT.

# Built at run time so no key-shaped literal sits in the repository.
KEY_CANARY = "sk" + "-ant-canary-" + "Zq7Xw3Rt9Lp2Mn5Vb8Kd4Hs6"


def test_p2_s2_ac14_audit_log_defaults_to_out_audit_jsonl(pr, pull_request, monkeypatch):
    monkeypatch.delenv(AUDIT_VARIABLE)

    run = pr.run(
        event(pull_request.base, pull_request.head),
        pull_request.repo.path,
        provider=MockProvider(findings=[]),
    )

    assert run.exit_code == 0, run.stderr
    records = read_records(run.out / "audit.jsonl")
    assert {record["run_id"] for record in records} == {run.shadow["run_id"]}
    assert "run.pull_request" in events(records), events(records)


def test_p2_s2_ac14_key_echoed_by_a_failing_provider_is_in_no_file_under_out(
    pr, pull_request, monkeypatch
):
    monkeypatch.delenv(AUDIT_VARIABLE)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY_CANARY)
    error = ProviderError(
        ErrorCategory.AUTH,
        "Not logged in.",
        diagnostics=[f"Invalid API key: {KEY_CANARY}", f"ANTHROPIC_API_KEY={KEY_CANARY}"],
    )

    run = pr.run(
        event(pull_request.base, pull_request.head),
        pull_request.repo.path,
        provider=MockProvider(error=error),
    )

    assert run.exit_code == 8, run.stderr
    assert (run.shadow["outcome"], run.shadow["reason"]) == ("failed", "auth")
    assert (run.out / "audit.jsonl").is_file()
    assert KEY_CANARY.encode() not in everything_in(run.out)


# AC15: a draft is reviewed in shadow mode.


def test_p2_s2_ac15_draft_pr_is_reviewed_in_shadow_mode(pr, pull_request):
    provider = MockProvider(findings=[])

    run = pr.run(
        set_in(event(pull_request.base, pull_request.head), "pull_request.draft", True),
        pull_request.repo.path,
        provider=provider,
    )

    assert run.exit_code == 0, run.stderr
    assert (run.shadow["mode"], run.shadow["outcome"]) == ("shadow", "reviewed")
    assert len(provider.requests) == 1


# AC16: only docs, lock or generated files: nothing to review, no call.


def test_p2_s2_ac16_only_docs_lock_and_generated_files_are_nothing_to_review(pr, tmp_path):
    pull = make_pull_request(
        tmp_path / "clone",
        {
            "README.md": "# Title\n\nMore words.\n",
            "uv.lock": "version = 1\n",
            "web/app.min.js": "var a=1;\n",
            "proto/api_pb2.py": "x = 1\n",
        },
    )
    provider = MockProvider(findings=[])

    run = pr.run(event(pull.base, pull.head), pull.repo.path, provider=provider)

    assert_skipped(run, "nothing_to_review")
    assert provider.requests == []


# Limits: git output past max diff bytes is skipped `too_large`.


def test_p2_s2_limits_git_output_past_max_diff_bytes_is_skipped_too_large(
    pr, too_much_code, monkeypatch
):
    monkeypatch.setenv("REVIEW_BOT_MAX_DIFF_BYTES", "1000")
    provider = MockProvider(findings=[])

    run = pr.run(
        event(too_much_code.base, too_much_code.head), too_much_code.repo.path, provider=provider
    )

    assert_skipped(run, "too_large")
    assert provider.requests == []
