r"""P2-S13: backfill shadow reports over recent merged PRs, in one bounded run.

Written from the spec's criteria, design.md (Commands and exit codes, CI settings and caps) and
P2-S2's spec, before the developer's code was read. Every run calls `review_bot.pr.cli.main` in
process with `["backfill", "--pulls", FILE, "--github-repo", OWNER/NAME, "--repo", DIR, "--out",
OUT, "--max-calls", N, ...]` and a scripted provider passed as `provider=`, shared by every PR of
the run. No run reaches a model, the network or GitHub. `--github-repo` is the main session's
decision: `FILE` holds no repository, so the run is told it and writes it as `pr.repo`. The exact
stdout lines and the missing-commit hint are also the main session's.

The merged-PR list is `FILE`, the JSON `gh pr list --state merged --json
number,baseRefOid,headRefOid,mergedAt,isCrossRepository` writes: the only seam the spec names.

`History` is one real git repository in `tmp_path`: each PR is a branch off `main` adding its own
file, after which `main` moves on, so a PR's base SHA is not its merge base (P2-S2 AC2).
"""

import importlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_p1_s4_audit_log import aws_key
from test_p2_s1_shadow_report_local import anchor_of, load_shadow
from test_p2_s2_review_pr_event import APP, BASE_APP, GitRepo, docs_lines, finding_at, python_lines

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.pr.units import ShadowFile, would_be_comments
from review_bot.providers.base import ReviewRequest, ReviewResponse, TokenUsage

pytestmark = pytest.mark.integration

ABSENT_SHA = "0123456789abcdef0123456789abcdef01234567"
GITHUB_REPO = "octo/review-bot"
MAIN_ONLY = "src/main_only.py"
USAGE_EXIT = 2
INPUT_EXIT = 19
# cli.py's provider exit codes (Phase 0 design): rate limit 9, auth 8.
RATE_LIMIT_EXIT = 9
REVIEW_BYTES_VARIABLE = "REVIEW_BOT_MAX_REVIEW_BYTES"
# P4-S1 AC8: backfill refuses in every mode without `--max-usd` and a per-call cap. These are
# generous enough that no run here is refused or stopped by them (an unreported cost counts as
# the cap), so each test keeps its intent; a test of the dollar flags passes its own.
WITHIN_BUDGET = ("--max-usd", "1000", "--max-cost-usd", "0.10")
# With a dollar budget, the entries line adds the spend (P3-S8 AC8).
SPEND = re.compile(r" spent_usd=\d+(?:\.\d+)?")
# Generous: it catches a hang or reading the whole file, not a slow CI machine.
BOUND_S = 60.0


@pytest.fixture(autouse=True)
def outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub Actions sets `CI`; AC6 makes it matter, so each test sets it only on purpose."""
    monkeypatch.delenv("CI", raising=False)


# The repository's history of merged PRs, and the FILE `gh pr list` writes for it.


def pr_file(number: int) -> str:
    return f"src/pr_{number}.py"


def pr_content(number: int) -> str:
    """Line 2 is the line a scripted finding lands on."""
    return f"first_{number} = 1\nsecond_{number} = 2\n"


def merged_at(number: int) -> str:
    return f"2026-11-0{number % 9 + 1}T10:11:12Z"


def merged_at_utc(number: int) -> str:
    """design.md: timestamps are UTC, in the `+00:00` form (as P2-S2 writes `event_at`)."""
    return merged_at(number).replace("Z", "+00:00")


@dataclass(frozen=True, slots=True)
class MergedPull:
    number: int
    base: str
    head: str
    cross_repository: bool = False

    def entry(self) -> dict[str, Any]:
        """One element of `gh pr list --json ...`'s array, with gh's field names."""
        return {
            "number": self.number,
            "baseRefOid": self.base,
            "headRefOid": self.head,
            "mergedAt": merged_at(self.number),
            "isCrossRepository": self.cross_repository,
        }


class History:
    def __init__(self, path: Path) -> None:
        self.repo = GitRepo(path)
        self.repo.commit({APP: BASE_APP, MAIN_ONLY: "main_start = 0\n"}, "base")

    def merged(
        self, number: int, files: Mapping[str, str] | None = None, *, cross_repository: bool = False
    ) -> MergedPull:
        """Branch off `main`, commit `files` (by default the PR's own file), then move `main`."""
        self.repo.branch(f"pr-{number}")
        head = self.repo.commit(dict(files or {pr_file(number): pr_content(number)}), "head")
        self.repo.checkout("main")
        base = self.repo.commit({MAIN_ONLY: f"main_start = 0\nmain_after_{number} = 1\n"}, "moves")
        return MergedPull(number, base, head, cross_repository)

    def merged_many(self, *numbers: int) -> list[MergedPull]:
        return [self.merged(number) for number in numbers]


@pytest.fixture
def history(tmp_path: Path) -> History:
    return History(tmp_path / "clone")


# A provider answering each PR by the file its prompt holds, so the order of calls doesn't matter.


class PerPullProvider:
    """Records each request; answers by the first scripted path the prompt contains.

    An answer is a list of findings or a `ProviderError` to raise; no match answers no findings.
    """

    def __init__(self, answers: Mapping[str, Sequence[Finding] | ProviderError] | None = None):
        self.requests: list[ReviewRequest] = []
        self._answers = dict(answers or {})

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        self.requests.append(request)
        for path, answer in self._answers.items():
            if path in request.user_prompt:
                if isinstance(answer, ProviderError):
                    raise answer
                return ReviewResponse(findings=tuple(answer), usage=TokenUsage(0, 0))
        return ReviewResponse(findings=(), usage=TokenUsage(0, 0))

    def prompts_holding(self, path: str) -> list[str]:
        return [r.user_prompt for r in self.requests if path in r.user_prompt]


# Running the command and reading what it writes.


@dataclass(frozen=True, slots=True)
class Run:
    exit_code: int
    stdout: str
    stderr: str
    out: Path

    def shadow(self, number: int) -> dict[str, Any]:
        return load_shadow(self.out / str(number))

    def reports(self) -> list[Path]:
        return sorted(self.out.rglob("shadow.json"))


class Backfiller:
    def __init__(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        self._capsys = capsys
        self._tmp_path = tmp_path
        self.out = tmp_path / "out"
        self.github_repo = GITHUB_REPO

    def write_pulls(self, pulls: Sequence[MergedPull | dict[str, Any]] | str | bytes) -> Path:
        path = self._tmp_path / "pulls.json"
        if isinstance(pulls, str):
            pulls = pulls.encode("utf-8")
        if not isinstance(pulls, bytes):
            entries = [p.entry() if isinstance(p, MergedPull) else p for p in pulls]
            pulls = json.dumps(entries).encode("utf-8")
        path.write_bytes(pulls)
        return path

    def run(  # noqa: PLR0913 - one keyword per thing a test varies
        self,
        pulls: Sequence[MergedPull | dict[str, Any]] | str | bytes,
        repo: Path,
        *flags: str,
        max_calls: int = 10,
        provider: Any = None,
        provider_name: str = "mock",
        dollar_flags: Sequence[str] = WITHIN_BUDGET,
    ) -> Run:
        """`backfill --pulls F --github-repo R --repo DIR --out OUT --max-calls N --provider P`.

        `dollar_flags` come before `flags`, so a test's own value of either flag wins; `()` leaves
        both out.
        """
        pr_cli = importlib.import_module("review_bot.pr.cli")
        pulls_path = self.write_pulls(pulls)
        self._capsys.readouterr()
        exit_code = pr_cli.main(
            [
                "backfill", "--pulls", str(pulls_path), "--github-repo", self.github_repo,
                "--repo", str(repo),
                "--out", str(self.out), "--max-calls", str(max_calls),
                "--provider", provider_name, *dollar_flags, *flags,
            ],
            provider=provider,
        )  # fmt: skip
        captured = self._capsys.readouterr()
        return Run(exit_code, captured.out, captured.err, self.out)


@pytest.fixture
def backfill(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> Backfiller:
    return Backfiller(capsys, tmp_path)


def assert_skipped(document: dict[str, Any], reason: str) -> None:
    assert (document["outcome"], document["reason"]) == ("skipped", reason)
    assert document["report"] is None
    assert document["comments"] == []


def assert_refused_before_any_call(run: Run, provider: PerPullProvider, exit_code: int) -> None:
    assert run.exit_code == exit_code, run.stderr
    assert provider.requests == []
    assert run.reports() == []


def closing_lines(stdout: str) -> list[str]:
    """stdout's last two lines: the entries line, then the outcome and reason counts."""
    return stdout.rstrip("\n").splitlines()[-2:]


def counts_without_spend(stdout: str) -> list[str]:
    """`closing_lines` without the spend the dollar budget adds to the entries line."""
    return [SPEND.sub("", line) for line in closing_lines(stdout)]


def assert_missing_commit_hint(stderr: str, number: int) -> None:
    """One line names the PR, says it wasn't reviewed, and gives the fetch of its pull ref."""
    prefix = f"review-bot pr: pull request {number}'s base or head commit isn't in --repo, "
    (line,) = [line for line in stderr.splitlines() if line.startswith(prefix)] or [stderr]
    assert "wasn't reviewed" in line, stderr
    assert f"git fetch origin refs/pull/{number}/head:refs/pull/{number}/head" in line, stderr


# AC1: each PR whose commits are in DIR gets OUT/<number>/shadow.json, reviewed as P2-S2 does.


def test_p2_s13_ac1_each_pr_gets_a_backfill_shadow_report_with_its_number_shas_and_merged_at(
    backfill, history
):
    pulls = history.merged_many(11, 12, 13)
    provider = PerPullProvider()

    run = backfill.run(pulls, history.repo.path, max_calls=3, provider=provider)

    assert run.exit_code == 0, run.stderr
    assert len(provider.requests) == 3
    for pull in pulls:
        document = run.shadow(pull.number)
        assert (document["mode"], document["origin"], document["outcome"]) == (
            "shadow",
            "backfill",
            "reviewed",
        )
        assert document["pr"] == {
            "repo": GITHUB_REPO,
            "number": pull.number,
            "base_sha": pull.base,
            "head_sha": pull.head,
        }
        assert document["event_at"] == merged_at_utc(pull.number)
        assert (run.out / str(pull.number) / "shadow.md").is_file()


def test_p2_s13_ac1_each_pr_reviews_only_its_own_changes_anchored_on_its_heads_lines(
    backfill, history
):
    """P2-S2 AC2: merge base to head, so neither `main`'s later commits nor other PRs leak in."""
    pulls = history.merged_many(11, 12, 13)
    provider = PerPullProvider(
        {pr_file(p.number): [finding_at(pr_file(p.number), 2)] for p in pulls}
    )

    run = backfill.run(pulls, history.repo.path, provider=provider)

    assert run.exit_code == 0, run.stderr
    for pull in pulls:
        (prompt,) = provider.prompts_holding(pr_file(pull.number))
        assert f"+second_{pull.number} = 2" in prompt
        assert MAIN_ONLY not in prompt
        others = [p for p in pulls if p is not pull]
        assert not any(pr_file(other.number) in prompt for other in others)
        comments = run.shadow(pull.number)["comments"]
        assert [(c["action"], c["file"], c["line"], c["anchor"]) for c in comments] == [
            ("create", pr_file(pull.number), 2, anchor_of(f"second_{pull.number} = 2"))
        ]


def test_p2_s13_ac1_every_report_has_a_distinct_run_id_so_label_keeps_each(backfill, history):
    """`label` and `metrics` count a report once per `run_id` (P2-S4); a shared one loses PRs."""
    pulls = history.merged_many(11, 12, 13)
    provider = PerPullProvider(
        {pr_file(p.number): [finding_at(pr_file(p.number), 2)] for p in pulls}
    )

    run = backfill.run(pulls, history.repo.path, provider=provider)

    assert run.exit_code == 0, run.stderr
    run_ids = [run.shadow(pull.number)["run_id"] for pull in pulls]
    assert len(set(run_ids)) == 3, run_ids
    reports = [ShadowFile.model_validate_json(path.read_bytes()) for path in run.reports()]
    comments = would_be_comments(reports)
    assert sorted((c.unit.repo, c.unit.pr) for c in comments) == [
        (GITHUB_REPO, n) for n in (11, 12, 13)
    ]


@pytest.mark.parametrize("github_repo", ["review-bot", "octo/review-bot/extra", "", "octo/"])
def test_p2_s13_ac1_invalid_github_repo_exits_2_before_any_call(backfill, history, github_repo):
    """The main session's decision: `--github-repo` is required and must be `owner/name`."""
    pulls = history.merged_many(14)
    provider = PerPullProvider()
    backfill.github_repo = github_repo

    run = backfill.run(pulls, history.repo.path, provider=provider)

    assert_refused_before_any_call(run, provider, USAGE_EXIT)
    assert "--github-repo" in run.stderr, run.stderr


# AC2: a cross-repository PR is skipped as a fork, with no call.


def test_p2_s13_ac2_cross_repository_pr_is_skipped_fork_without_a_call(backfill, history):
    same_repo = history.merged(21)
    fork = history.merged(22, cross_repository=True)
    provider = PerPullProvider()

    run = backfill.run([same_repo, fork], history.repo.path, provider=provider)

    assert run.exit_code == 0, run.stderr
    assert_skipped(run.shadow(22), "fork")
    assert run.shadow(21)["outcome"] == "reviewed"
    assert len(provider.requests) == 1
    assert provider.prompts_holding(pr_file(22)) == []


# AC3: more planned calls than --max-calls refuses before any call, exit 2.


def test_p2_s13_ac3_more_planned_calls_than_allowed_refuses_naming_both(backfill, history):
    pulls = history.merged_many(31, 32, 33, 34, 35)
    provider = PerPullProvider()

    run = backfill.run(pulls, history.repo.path, max_calls=3, provider=provider)

    assert_refused_before_any_call(run, provider, USAGE_EXIT)
    assert re.search(r"(?<!\d)5(?!\d)", run.stderr), run.stderr
    assert re.search(r"(?<!\d)3(?!\d)", run.stderr), run.stderr


def test_p2_s13_ac3_planned_calls_count_entries_that_would_later_be_skipped(backfill, history):
    """A fork and a missing head still count: they're planned before any is skipped."""
    pulls = [
        *history.merged_many(31, 32, 33),
        history.merged(34, cross_repository=True),
        MergedPull(35, history.merged(36).base, ABSENT_SHA),
    ]
    provider = PerPullProvider()

    run = backfill.run(pulls, history.repo.path, max_calls=4, provider=provider)

    assert_refused_before_any_call(run, provider, USAGE_EXIT)
    assert re.search(r"(?<!\d)5(?!\d)", run.stderr), run.stderr
    assert re.search(r"(?<!\d)4(?!\d)", run.stderr), run.stderr


# AC4: a head SHA missing from DIR skips that PR as missing_commit; the others still run.


def test_p2_s13_ac4_missing_head_is_skipped_and_stderr_says_to_fetch_its_pull_ref(
    backfill, history
):
    before, after = history.merged_many(41, 43)
    missing = MergedPull(42, before.base, ABSENT_SHA)
    provider = PerPullProvider()

    run = backfill.run([before, missing, after], history.repo.path, provider=provider)

    assert run.exit_code == 0, run.stderr
    assert_skipped(run.shadow(42), "missing_commit")
    assert run.shadow(41)["outcome"] == "reviewed"
    assert run.shadow(43)["outcome"] == "reviewed"
    assert len(provider.requests) == 2
    assert_missing_commit_hint(run.stderr, 42)


def test_p2_s13_ac4_missing_base_is_also_skipped_missing_commit(backfill, history):
    """The main session's decision: a missing base commit is `missing_commit` too."""
    (present,) = history.merged_many(44)
    missing = MergedPull(45, ABSENT_SHA, present.head)
    provider = PerPullProvider()

    run = backfill.run([present, missing], history.repo.path, provider=provider)

    assert run.exit_code == 0, run.stderr
    assert_skipped(run.shadow(45), "missing_commit")
    assert run.shadow(44)["outcome"] == "reviewed"
    assert_missing_commit_hint(run.stderr, 45)


# AC5: over the reviewable-bytes cap, or docs only, is skipped with no call.


@pytest.fixture
def sized_pulls(history: History, monkeypatch: pytest.MonkeyPatch) -> list[MergedPull]:
    """51 is small Python, 52 is 20,000 bytes of Python over a 5,000-byte cap, 53 is docs only."""
    monkeypatch.setenv(REVIEW_BYTES_VARIABLE, "5000")
    return [
        history.merged(51),
        history.merged(52, {"src/big.py": python_lines(400)}),
        history.merged(53, {"docs/guide.md": docs_lines(40)}),
    ]


def test_p2_s13_ac5_too_large_and_docs_only_prs_are_skipped_without_a_call(
    backfill, history, sized_pulls
):
    provider = PerPullProvider()

    run = backfill.run(sized_pulls, history.repo.path, provider=provider)

    assert run.exit_code == 0, run.stderr
    assert run.shadow(51)["outcome"] == "reviewed"
    assert_skipped(run.shadow(52), "too_large")
    assert_skipped(run.shadow(53), "nothing_to_review")
    assert len(provider.requests) == 1
    assert provider.prompts_holding("src/big.py") == []
    assert provider.prompts_holding("docs/guide.md") == []


# AC6: CI set refuses before any call, exit 2.


def test_p2_s13_ac6_ci_set_refuses_before_any_call(backfill, history, monkeypatch):
    monkeypatch.setenv("CI", "true")
    pulls = history.merged_many(61, 62)
    provider = PerPullProvider()

    run = backfill.run(pulls, history.repo.path, provider=provider)

    assert_refused_before_any_call(run, provider, USAGE_EXIT)
    assert re.search(r"\bCI\b", run.stderr), run.stderr


# AC7: a provider failure fails that report only; the exit code is the first failure's.


def test_p2_s13_ac7_failed_pr_does_not_stop_the_rest_and_the_first_failure_sets_the_exit(
    backfill, history
):
    pulls = history.merged_many(71, 72, 73, 74)
    provider = PerPullProvider(
        {
            pr_file(72): ProviderError(ErrorCategory.RATE_LIMIT, "Wait, then try again."),
            pr_file(73): ProviderError(ErrorCategory.AUTH, "Log in again."),
        }
    )

    run = backfill.run(pulls, history.repo.path, provider=provider)

    assert run.exit_code == RATE_LIMIT_EXIT, run.stderr
    assert len(provider.requests) == 4
    assert (run.shadow(72)["outcome"], run.shadow(72)["reason"]) == ("failed", "rate_limit")
    assert (run.shadow(73)["outcome"], run.shadow(73)["reason"]) == ("failed", "auth")
    assert run.shadow(71)["outcome"] == "reviewed"
    assert run.shadow(74)["outcome"] == "reviewed"


# AC8: a malformed FILE, or a SHA that isn't 40 hex digits, exits 19 before any call.


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"[{not json", id="not-json"),
        pytest.param(b'{"number": 81}', id="object-not-array"),
        pytest.param(b"\xff\xfe[]", id="not-utf8"),
    ],
)
def test_p2_s13_ac8_malformed_file_exits_19_before_any_call(backfill, history, content):
    history.merged(81)
    provider = PerPullProvider()

    run = backfill.run(content, history.repo.path, provider=provider)

    assert_refused_before_any_call(run, provider, INPUT_EXIT)


def test_p2_s13_ac8_entry_missing_a_field_exits_19_naming_it_after_a_valid_entry(backfill, history):
    valid, broken = history.merged_many(81, 4242)
    entry = broken.entry()
    del entry["headRefOid"]
    provider = PerPullProvider()

    run = backfill.run([valid, entry], history.repo.path, provider=provider)

    assert_refused_before_any_call(run, provider, INPUT_EXIT)
    assert re.search(r"\bentry 2\b", run.stderr), run.stderr


@pytest.mark.parametrize("field", ["baseRefOid", "headRefOid"])
@pytest.mark.parametrize(
    "sha",
    [
        pytest.param("0123456789abcdef0123456789abcdef0123456", id="39-hex"),
        pytest.param("0123456789abcdef0123456789abcdef012345678", id="41-hex"),
        pytest.param("g" * 40, id="40-not-hex"),
        pytest.param("HEAD", id="ref-name"),
    ],
)
def test_p2_s13_ac8_sha_that_isnt_40_hex_digits_exits_19_naming_the_entry(
    backfill, history, field, sha
):
    valid, broken = history.merged_many(81, 4242)
    entry = broken.entry() | {field: sha}
    provider = PerPullProvider()

    run = backfill.run([valid, entry], history.repo.path, provider=provider)

    assert_refused_before_any_call(run, provider, INPUT_EXIT)
    assert re.search(r"\bentry 2\b", run.stderr), run.stderr


def test_p2_s13_ac8_repeated_pr_number_exits_19_naming_it(backfill, history):
    """The main session's decision: two entries for one PR would share `OUT/<number>`."""
    pull = history.merged(4242)
    provider = PerPullProvider()

    run = backfill.run([pull, pull], history.repo.path, provider=provider)

    assert_refused_before_any_call(run, provider, INPUT_EXIT)
    assert re.search(r"\bentry 2\b", run.stderr), run.stderr


def test_p2_s13_ac8_more_than_500_entries_exits_19_before_any_call(backfill, history):
    """Limits: at most 500 entries. The spec gives no code for a Limit; 19 is AC8's for FILE."""
    pull = history.merged(81)
    entries = [MergedPull(n, pull.base, pull.head).entry() for n in range(1, 502)]
    provider = PerPullProvider()

    run = backfill.run(entries, history.repo.path, max_calls=1000, provider=provider)

    assert_refused_before_any_call(run, provider, INPUT_EXIT)


def test_p2_s13_ac8_file_over_10_mb_exits_19_quickly_before_any_call(backfill, history):
    """Limits: at most 10 MB (10,000,000 bytes, as P2-S2 reads its event file)."""
    pull = history.merged(81)
    padding = b" " * (10_000_001 - len(json.dumps([pull.entry()])))
    provider = PerPullProvider()

    started = time.monotonic()
    run = backfill.run(json.dumps([pull.entry()]).encode() + padding, history.repo.path,
                       provider=provider)  # fmt: skip
    elapsed = time.monotonic() - started

    assert_refused_before_any_call(run, provider, INPUT_EXIT)
    assert elapsed < BOUND_S, elapsed


# AC9: stdout ends with counts per outcome and reason, including too_large.


def test_p2_s13_ac9_stdout_ends_with_counts_per_outcome_and_reason(backfill, history, sized_pulls):
    pulls = [
        *sized_pulls,
        history.merged(54, cross_repository=True),
        MergedPull(55, sized_pulls[0].base, ABSENT_SHA),
        history.merged(56),
    ]
    provider = PerPullProvider({pr_file(56): ProviderError(ErrorCategory.RATE_LIMIT, "Wait.")})

    run = backfill.run(pulls, history.repo.path, provider=provider)

    assert run.exit_code == RATE_LIMIT_EXIT, run.stderr
    assert counts_without_spend(run.stdout) == [
        "backfill: entries=6 already_in_out=0 run=6",
        "backfill: reviewed=1 blocked_secret=0 skipped=4 failed=1 fork=1 missing_commit=1 "
        "nothing_to_review=1 rate_limit=1 too_large=1",
    ], run.stdout


def test_p2_s13_ac9_counts_say_how_many_were_too_large_even_when_none_were(backfill, history):
    pulls = history.merged_many(57, 58)

    run = backfill.run(pulls, history.repo.path, provider=PerPullProvider())

    assert run.exit_code == 0, run.stderr
    assert counts_without_spend(run.stdout) == [
        "backfill: entries=2 already_in_out=0 run=2",
        "backfill: reviewed=2 blocked_secret=0 skipped=0 failed=0 too_large=0",
    ], run.stdout


# AC10: a PR whose report already exists is not reviewed again nor counted towards --max-calls.


def report_bytes(out: Path, number: int) -> bytes:
    return (out / str(number) / "shadow.json").read_bytes()


def test_p2_s13_ac10_existing_reports_are_kept_and_not_counted_towards_max_calls(backfill, history):
    """The earlier run leaves 102 `reviewed`, 104 `skipped` (fork) and 106 `blocked_secret`."""
    pulls = [
        *history.merged_many(101, 102, 103),
        history.merged(104, cross_repository=True),
        history.merged(105),
        history.merged(106, {"src/keys.py": f'key = "{aws_key(seed=5)}"\n'}),
    ]
    earlier = [pulls[1], pulls[3], pulls[5]]
    first = backfill.run(earlier, history.repo.path, max_calls=3, provider=PerPullProvider())
    assert first.exit_code == 0, first.stderr
    assert [first.shadow(p.number)["outcome"] for p in earlier] == [
        "reviewed",
        "skipped",
        "blocked_secret",
    ]
    kept = {p.number: report_bytes(backfill.out, p.number) for p in earlier}
    provider = PerPullProvider()

    run = backfill.run(pulls, history.repo.path, max_calls=3, provider=provider)

    assert run.exit_code == 0, run.stderr
    assert len(provider.requests) == 3
    for number, content in kept.items():
        assert provider.prompts_holding(pr_file(number)) == []
        assert report_bytes(run.out, number) == content
    for number in (101, 103, 105):
        assert run.shadow(number)["outcome"] == "reviewed"
    assert counts_without_spend(run.stdout)[0] == "backfill: entries=6 already_in_out=3 run=3"


def test_p2_s13_ac10_an_unreadable_existing_report_counts_as_done(backfill, history):
    """The main session's decision: it is left untouched and not counted."""
    unreadable, new = history.merged_many(111, 112)
    content = b"\xff not json {"
    (backfill.out / "111").mkdir(parents=True)
    (backfill.out / "111" / "shadow.json").write_bytes(content)
    provider = PerPullProvider()

    run = backfill.run([unreadable, new], history.repo.path, max_calls=1, provider=provider)

    assert run.exit_code == 0, run.stderr
    assert provider.prompts_holding(pr_file(111)) == []
    assert len(provider.requests) == 1
    assert report_bytes(run.out, 111) == content
    assert counts_without_spend(run.stdout)[0] == "backfill: entries=2 already_in_out=1 run=1"


def test_p2_s13_ac10_a_failed_report_is_reviewed_again_and_replaced(backfill, history):
    """The main session's decision (review round 1): `failed` isn't done, so it runs again."""
    done, failed = history.merged_many(108, 109)
    first = backfill.run(
        [done, failed],
        history.repo.path,
        provider=PerPullProvider({pr_file(109): ProviderError(ErrorCategory.TRANSIENT, "Retry.")}),
    )
    assert first.shadow(109)["outcome"] == "failed", first.stderr
    failed_run_id = first.shadow(109)["run_id"]
    done_bytes = report_bytes(backfill.out, 108)
    provider = PerPullProvider()

    run = backfill.run([done, failed], history.repo.path, max_calls=1, provider=provider)

    assert run.exit_code == 0, run.stderr
    assert len(provider.prompts_holding(pr_file(109))) == 1
    assert len(provider.requests) == 1
    replaced = run.shadow(109)
    assert replaced["outcome"] == "reviewed"
    assert replaced["run_id"] != failed_run_id
    assert report_bytes(run.out, 108) == done_bytes
    assert counts_without_spend(run.stdout)[0] == "backfill: entries=2 already_in_out=1 run=1"


def test_p2_s13_ac10_a_failed_report_counts_towards_max_calls(backfill, history):
    """108 is done, 109 failed and 110 is new: 2 planned calls, over `--max-calls 1`."""
    done, failed, new = history.merged_many(108, 109, 110)
    first = backfill.run(
        [done, failed],
        history.repo.path,
        provider=PerPullProvider({pr_file(109): ProviderError(ErrorCategory.TRANSIENT, "Retry.")}),
    )
    assert first.shadow(109)["outcome"] == "failed", first.stderr
    before = {number: report_bytes(backfill.out, number) for number in (108, 109)}
    provider = PerPullProvider()

    run = backfill.run([done, failed, new], history.repo.path, max_calls=1, provider=provider)

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []
    assert re.search(r"(?<!\d)2(?!\d)", run.stderr), run.stderr
    assert {number: report_bytes(run.out, number) for number in (108, 109)} == before
    assert not (run.out / "110" / "shadow.json").exists()


def test_p2_s13_ac10_running_again_reviews_nothing_and_rewrites_no_report(backfill, history):
    pulls = history.merged_many(106, 107)
    first = backfill.run(pulls, history.repo.path, max_calls=2, provider=PerPullProvider())
    assert first.exit_code == 0, first.stderr
    written = {path: path.read_bytes() for path in first.reports()}
    provider = PerPullProvider()

    again = backfill.run(pulls, history.repo.path, max_calls=1, provider=provider)

    assert again.exit_code == 0, again.stderr
    assert provider.requests == []
    assert {path: path.read_bytes() for path in again.reports()} == written


# AC11: REVIEW_BOT_MAX_RETRIES is overridden: every call is made with retries 0.


@pytest.mark.parametrize("given_by", ["variable", "flag"])
def test_p2_s13_ac11_caps_record_zero_retries_in_every_report(
    backfill, history, monkeypatch, given_by
):
    """The variable is AC11's; the flag is the main session's decision (`--max-retries` too)."""
    flags = ("--max-retries", "2") if given_by == "flag" else ()
    if given_by == "variable":
        monkeypatch.setenv("REVIEW_BOT_MAX_RETRIES", "2")
    pulls = [*history.merged_many(111, 112), history.merged(113, cross_repository=True)]

    run = backfill.run(pulls, history.repo.path, *flags, provider=PerPullProvider())

    assert run.exit_code == 0, run.stderr
    assert [run.shadow(p.number)["caps"]["max_retries"] for p in pulls] == [0, 0, 0]


def test_p2_s13_ac11_the_claude_cli_is_called_with_zero_retries(
    backfill, history, monkeypatch, real_query_calls
):
    """No injected provider: the Claude provider the run builds itself reaches the stopped `query`.

    The suite's guard stops the real call and records its options, so the retries the CLI would
    get are visible; the guard's exception fails the call, which is expected here.
    """
    monkeypatch.setenv("REVIEW_BOT_MAX_RETRIES", "2")
    pulls = history.merged_many(114, 115)

    backfill.run(pulls, history.repo.path, provider_name="claude")

    calls = list(real_query_calls)
    real_query_calls.clear()
    assert calls, "no call reached the Claude CLI"
    assert [call["options"].env.get("CLAUDE_CODE_MAX_RETRIES") for call in calls] == ["0"] * len(
        calls
    )
