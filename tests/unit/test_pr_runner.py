import asyncio
import dataclasses
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from audit_records import read_records
from git_samples import commit, git, init_repo

from review_bot.audit import AuditLog
from review_bot.context.codeowners import MAX_CODEOWNERS_BYTES
from review_bot.errors import ErrorCategory, GitError, GitHubError, ProviderError
from review_bot.findings import Finding
from review_bot.git import GitRepo
from review_bot.pr.comments import with_summary_marker
from review_bot.pr.event import PullRequestRef
from review_bot.pr.fake_github import FakeGitHubClient
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.runner import backfill_pull_request, run_pull_request
from review_bot.pr.shadow import Mode, Origin, Scope, ShadowReport, render_shadow_json
from review_bot.providers.base import LLMProvider, TokenUsage
from review_bot.providers.mock import MockProvider
from review_bot.settings import Settings

NOW = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
CLAUDE = Settings(provider="claude")
MOCK = Settings(provider="mock")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def pr_for(repo: Path, **changes: object) -> PullRequestRef:
    base = commit(repo, {"app.py": "def run():\n    return 1\n"})
    head = commit(repo, {"app.py": "def run():\n    return 2\n"})
    pr = PullRequestRef(
        repo="owner/repo",
        number=7,
        base_sha=base,
        head_sha=head,
        is_fork=False,
        is_draft=False,
        event_at=NOW,
        action="opened",
    )
    return dataclasses.replace(pr, **changes)  # type: ignore[arg-type]


class CountingFactory:
    """Makes ``provider`` and counts how often it was asked to."""

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider
        self.calls = 0

    def __call__(self) -> LLMProvider:
        self.calls += 1
        return self.provider


class Runner:
    """Runs ``run_pull_request`` on one clone, with one audit log and a fixed clock."""

    def __init__(self, repo: Path, audit: AuditLog) -> None:
        self.repo = repo
        self.audit = audit

    def __call__(
        self,
        pr: PullRequestRef,
        *,
        factory: CountingFactory,
        settings: Settings = MOCK,
        environ: Mapping[str, str] | None = None,
        github: FakeGitHubClient | None = None,
    ) -> ShadowReport:
        return asyncio.run(
            run_pull_request(
                pr,
                mode=Mode.SHADOW if github is None else Mode.COMMENT,
                git=GitRepo(self.repo, timeout_s=60),
                make_provider=factory,
                github=github,
                settings=settings,
                audit=self.audit,
                environ={} if environ is None else environ,
                clock=lambda: NOW,
            )
        )

    def records(self) -> list[dict[str, object]]:
        return read_records(self.audit.path)


@pytest.fixture
def run(repo: Path, tmp_path: Path) -> Runner:
    return Runner(repo, AuditLog(tmp_path / "audit.jsonl", clock=lambda: NOW))


def test_run_pull_request_reviews_the_head_and_reports_the_pull_request(
    repo: Path, run: Runner
) -> None:
    finding = Finding(
        file="app.py", line=2, severity="high", category="logic", rationale="Why.", confidence=1.0
    )
    provider = MockProvider(findings=[finding])
    pr = pr_for(repo)

    shadow = run(pr, factory=CountingFactory(provider))

    assert (shadow.outcome, shadow.reason, shadow.pr) == (Outcome.REVIEWED, None, pr)
    assert [comment.planned.reviewed.finding.line for comment in shadow.comments] == [2]
    assert len(provider.requests) == 1
    assert "+    return 2" in provider.requests[0].user_prompt
    records = run.records()
    assert records[0]["event"] == "pull_request"
    assert records[0]["data"] == {
        "repo": "owner/repo",
        "number": 7,
        "base_sha": pr.base_sha,
        "head_sha": pr.head_sha,
    }


def assert_refused(
    shadow: ShadowReport, run: Runner, factory: CountingFactory, reason: Reason
) -> None:
    assert (shadow.outcome, shadow.reason) == (Outcome.SKIPPED, reason)
    assert shadow.report is None
    assert factory.calls == 0
    assert run.records()[-1]["data"] == {"reason": reason}


def unknown_commits(pr: PullRequestRef) -> PullRequestRef:
    """``pr`` with commits git doesn't have, so any git call would fail the run."""
    return dataclasses.replace(pr, base_sha="c" * 40, head_sha="d" * 40)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"is_fork": True}, Reason.FORK),
        ({"action": "closed"}, Reason.ACTION),
        ({"action": "edited"}, Reason.ACTION),
        ({"action": "labeled"}, Reason.ACTION),
    ],
)
def test_run_pull_request_refuses_a_fork_or_action_without_git_or_a_provider(
    repo: Path, run: Runner, changes: dict[str, object], reason: Reason
) -> None:
    factory = CountingFactory(MockProvider())

    shadow = run(unknown_commits(pr_for(repo, **changes)), factory=factory)

    assert_refused(shadow, run, factory, reason)


@pytest.mark.parametrize("environ", [{"CI": "true"}, {"CI": "true", "ANTHROPIC_API_KEY": ""}])
def test_run_pull_request_refuses_claude_in_ci_without_a_key(
    repo: Path, run: Runner, environ: dict[str, str]
) -> None:
    factory = CountingFactory(MockProvider())

    shadow = run(unknown_commits(pr_for(repo)), factory=factory, settings=CLAUDE, environ=environ)

    assert_refused(shadow, run, factory, Reason.NO_CREDENTIAL)


@pytest.mark.parametrize(
    ("settings", "environ"),
    [
        (CLAUDE, {}),
        (CLAUDE, {"CI": "true", "ANTHROPIC_API_KEY": "key"}),
        (MOCK, {"CI": "true"}),
    ],
    ids=["local-login", "ci-with-key", "ci-mock"],
)
def test_run_pull_request_goes_ahead_with_a_credential_or_outside_ci(
    repo: Path, run: Runner, settings: Settings, environ: dict[str, str]
) -> None:
    factory = CountingFactory(MockProvider(findings=[]))

    shadow = run(pr_for(repo), factory=factory, settings=settings, environ=environ)

    assert shadow.outcome is Outcome.REVIEWED
    assert factory.calls == 1


@pytest.mark.parametrize("action", ["synchronize", "reopened", "ready_for_review"])
def test_run_pull_request_reviews_each_action_that_changes_the_review(
    repo: Path, run: Runner, action: str
) -> None:
    shadow = run(pr_for(repo, action=action), factory=CountingFactory(MockProvider(findings=[])))

    assert shadow.outcome is Outcome.REVIEWED


def test_run_pull_request_reviews_a_draft(repo: Path, run: Runner) -> None:
    shadow = run(pr_for(repo, is_draft=True), factory=CountingFactory(MockProvider(findings=[])))

    assert shadow.outcome is Outcome.REVIEWED


def test_run_pull_request_over_the_review_byte_cap_is_skipped_without_a_call(
    repo: Path, run: Runner
) -> None:
    provider = MockProvider()

    shadow = run(
        pr_for(repo),
        factory=CountingFactory(provider),
        settings=Settings(provider="mock", max_review_bytes=10),
    )

    assert (shadow.outcome, shadow.reason) == (Outcome.SKIPPED, Reason.TOO_LARGE)
    assert provider.requests == []


def test_run_pull_request_with_only_skipped_files_has_nothing_to_review(
    repo: Path, run: Runner
) -> None:
    base = commit(repo, {"README.md": "one\n"})
    head = commit(repo, {"README.md": "two\n"})
    pr = dataclasses.replace(pr_for(repo), base_sha=base, head_sha=head)
    provider = MockProvider()

    shadow = run(pr, factory=CountingFactory(provider))

    assert (shadow.outcome, shadow.reason) == (Outcome.SKIPPED, Reason.NOTHING_TO_REVIEW)
    assert provider.requests == []


def test_run_pull_request_with_a_provider_failure_reports_it_as_failed(
    repo: Path, run: Runner
) -> None:
    error = ProviderError(ErrorCategory.AUTH, "Log in.")

    shadow = run(pr_for(repo), factory=CountingFactory(MockProvider(error=error)))

    assert (shadow.outcome, shadow.reason, shadow.error) == (
        Outcome.FAILED,
        ErrorCategory.AUTH,
        error,
    )


def test_run_pull_request_over_the_output_cap_is_over_budget_but_reviewed(
    repo: Path, run: Runner
) -> None:
    provider = MockProvider(findings=[], usage=TokenUsage(100, 20_000))

    shadow = run(
        pr_for(repo),
        factory=CountingFactory(provider),
        settings=Settings(provider="mock", max_output_tokens=16_000),
    )

    assert (shadow.outcome, shadow.over_budget) == (Outcome.REVIEWED, True)


def test_run_pull_request_with_a_missing_head_raises_without_a_provider(
    repo: Path, run: Runner
) -> None:
    pr = dataclasses.replace(pr_for(repo), head_sha="e" * 40)
    factory = CountingFactory(MockProvider())

    with pytest.raises(GitError, match="e" * 40):
        run(pr, factory=factory)

    assert factory.calls == 0


def backfill(
    pr: PullRequestRef, run: Runner, factory: CountingFactory, settings: Settings = MOCK
) -> ShadowReport:
    return asyncio.run(
        backfill_pull_request(
            pr,
            git=GitRepo(run.repo, timeout_s=60),
            make_provider=factory,
            settings=settings,
            audit=run.audit,
            clock=lambda: NOW,
        )
    )


def test_backfill_pull_request_reviews_a_merged_pull_request_as_a_backfill(
    repo: Path, run: Runner
) -> None:
    provider = MockProvider(findings=[])
    pr = pr_for(repo, action="closed")

    shadow = backfill(pr, run, CountingFactory(provider))

    assert (shadow.origin, shadow.outcome, shadow.pr) == (Origin.BACKFILL, Outcome.REVIEWED, pr)
    assert "+    return 2" in provider.requests[0].user_prompt
    assert run.records()[0]["event"] == "pull_request"


def test_backfill_pull_request_refuses_a_fork_without_git_or_a_provider(
    repo: Path, run: Runner
) -> None:
    factory = CountingFactory(MockProvider())

    shadow = backfill(unknown_commits(pr_for(repo, is_fork=True)), run, factory)

    assert_refused(shadow, run, factory, Reason.FORK)


@pytest.mark.parametrize("missing", ["base_sha", "head_sha"])
def test_backfill_pull_request_with_a_missing_commit_is_skipped_without_a_provider(
    repo: Path, run: Runner, missing: str
) -> None:
    factory = CountingFactory(MockProvider())

    shadow = backfill(pr_for(repo, **{missing: "e" * 40}), run, factory)

    assert_refused(shadow, run, factory, Reason.MISSING_COMMIT)
    assert shadow.origin is Origin.BACKFILL


def test_backfill_pull_request_with_a_provider_failure_reports_it_as_failed(
    repo: Path, run: Runner
) -> None:
    error = ProviderError(ErrorCategory.RATE_LIMIT, "Wait.")

    shadow = backfill(pr_for(repo), run, CountingFactory(MockProvider(error=error)))

    assert (shadow.outcome, shadow.reason) == (Outcome.FAILED, ErrorCategory.RATE_LIMIT)


# Comment mode, against FakeGitHubClient.


def finding_on(line: int, severity: str = "high") -> Finding:
    return Finding(
        file="app.py",
        line=line,
        severity=severity,
        category="logic",
        rationale="Why.",
        confidence=1.0,
    )


def names(github: FakeGitHubClient) -> list[str]:
    return [call.name for call in github.calls]


def posted_summary(shadow: ShadowReport, reviewed_sha: str | None) -> str:
    return with_summary_marker(shadow.summary_body, reviewed_sha=reviewed_sha)


def review_again(github: FakeGitHubClient) -> None:
    """Make the next run on the same head review it whole, as with no summary yet."""
    github.summary = None
    github.calls.clear()


def applied(shadow: ShadowReport) -> list[tuple[str, str]]:
    entries = [(comment.planned.action, comment.applied) for comment in shadow.comments]
    return entries + [("resolve", resolve.applied) for resolve in shadow.resolves]


def scripted(*findings: Finding) -> CountingFactory:
    return CountingFactory(MockProvider(findings=list(findings)))


def test_run_pull_request_in_comment_mode_posts_one_review_on_the_head_then_the_summary(
    repo: Path, run: Runner
) -> None:
    github = FakeGitHubClient()
    pr = pr_for(repo)

    shadow = run(pr, factory=scripted(finding_on(2)), github=github)

    assert names(github) == ["read_summary", "list_bot_comments", "post_review", "write_summary"]
    (review,) = github.reviews
    (posted,) = review.comments
    assert (review.commit_sha, posted.path, posted.line) == (pr.head_sha, "app.py", 2)
    assert posted.body == shadow.comments[0].body
    assert github.summary == posted_summary(shadow, pr.head_sha)
    assert shadow.mode is Mode.COMMENT
    assert applied(shadow) == [("create", "done")]
    assert shadow.error is None


def test_run_pull_request_in_comment_mode_finding_the_same_again_posts_nothing(
    repo: Path, run: Runner
) -> None:
    github = FakeGitHubClient()
    pr = pr_for(repo)
    run(pr, factory=scripted(finding_on(2)), github=github)
    review_again(github)

    shadow = run(pr, factory=scripted(finding_on(2)), github=github)

    assert names(github) == ["read_summary", "list_bot_comments", "write_summary"]
    assert applied(shadow) == [("keep", "done")]
    assert len(github.comments) == 1


@pytest.mark.parametrize(
    ("changes", "reason"),
    [({"is_draft": True}, Reason.DRAFT), ({"is_fork": True}, Reason.FORK)],
)
def test_run_pull_request_in_comment_mode_refuses_without_any_call(
    repo: Path, run: Runner, changes: dict[str, object], reason: Reason
) -> None:
    github = FakeGitHubClient()
    factory = CountingFactory(MockProvider())

    shadow = run(unknown_commits(pr_for(repo, **changes)), factory=factory, github=github)

    assert_refused(shadow, run, factory, reason)
    assert github.calls == []


def test_run_pull_request_in_comment_mode_with_nothing_to_review_makes_no_call(
    repo: Path, run: Runner
) -> None:
    base = commit(repo, {"README.md": "one\n"})
    head = commit(repo, {"README.md": "two\n"})
    pr = dataclasses.replace(pr_for(repo), base_sha=base, head_sha=head)
    github = FakeGitHubClient()

    shadow = run(pr, factory=CountingFactory(MockProvider()), github=github)

    assert shadow.reason is Reason.NOTHING_TO_REVIEW
    assert github.calls == []


def test_run_pull_request_in_comment_mode_with_a_failed_review_only_writes_the_summary(
    repo: Path, run: Runner
) -> None:
    github = FakeGitHubClient()
    error = ProviderError(ErrorCategory.TRANSIENT, "Try again.")

    shadow = run(pr_for(repo), factory=CountingFactory(MockProvider(error=error)), github=github)

    assert names(github) == ["read_summary", "write_summary"]
    assert github.summary == posted_summary(shadow, None)
    assert "unavailable" in shadow.summary_body
    assert shadow.error is error


def test_run_pull_request_in_comment_mode_with_a_secret_only_writes_the_summary(
    repo: Path, run: Runner
) -> None:
    aws_key = "AKIA" + "ABCDEFGHIJKLMNOP"
    base = commit(repo, {"keys.py": "x = 1\n"})
    head = commit(repo, {"keys.py": f"x = {aws_key!r}\n"})
    pr = dataclasses.replace(pr_for(repo), base_sha=base, head_sha=head)
    github = FakeGitHubClient()

    shadow = run(pr, factory=CountingFactory(MockProvider()), github=github)

    assert shadow.outcome is Outcome.BLOCKED_SECRET
    assert names(github) == ["read_summary", "write_summary"]
    assert aws_key not in str(github.summary)


def failing_run_setup(repo: Path, run: Runner) -> tuple[PullRequestRef, FakeGitHubClient]:
    """A pull request whose next run creates, updates and resolves one comment each."""
    github = FakeGitHubClient()
    pr = pr_for(repo)
    run(pr, factory=scripted(finding_on(2)), github=github)
    first = github.comments[0]
    fingerprint = first.body.split("fingerprint=")[1][:16]
    github.add_comment(path="app.py", line=9, body=first.body.replace(fingerprint, "e" * 16))
    review_again(github)
    return pr, github


def test_run_pull_request_stops_at_a_failed_call_but_still_writes_the_summary(
    repo: Path, run: Runner
) -> None:
    pr, github = failing_run_setup(repo, run)
    github.fail_on("update_comment")

    shadow = run(pr, factory=scripted(finding_on(1), finding_on(2, "low")), github=github)

    assert names(github) == [
        "read_summary",
        "list_bot_comments",
        "post_review",
        "update_comment",
        "write_summary",
    ]
    assert sorted(applied(shadow)) == [
        ("create", "done"),
        ("resolve", "not_attempted"),
        ("update", "failed"),
    ]
    assert isinstance(shadow.error, GitHubError)
    assert "update_comment" in str(shadow.error)
    assert github.summary == posted_summary(shadow, None)


def test_run_pull_request_with_only_the_summary_failing_reports_its_error(
    repo: Path, run: Runner
) -> None:
    pr, github = failing_run_setup(repo, run)
    github.fail_on("write_summary")

    shadow = run(pr, factory=scripted(finding_on(1), finding_on(2, "low")), github=github)

    assert {status for _, status in applied(shadow)} == {"done"}
    assert "write_summary" in str(shadow.error)


def test_run_pull_request_posts_a_move_before_resolving_its_old_thread(
    repo: Path, run: Runner
) -> None:
    github = FakeGitHubClient()
    pr = pr_for(repo)
    run(pr, factory=scripted(finding_on(2)), github=github)
    github.mark_outdated(1)
    review_again(github)
    github.fail_on("resolve_thread")

    shadow = run(pr, factory=scripted(finding_on(2)), github=github)

    assert names(github) == [
        "read_summary",
        "list_bot_comments",
        "post_review",
        "resolve_thread",
        "write_summary",
    ]
    assert applied(shadow) == [("move", "failed")]
    assert len(github.comments) == 2


def test_run_pull_request_raises_when_the_bots_comments_cant_be_listed(
    repo: Path, run: Runner
) -> None:
    github = FakeGitHubClient()
    github.fail_on("list_bot_comments")

    with pytest.raises(GitHubError, match="list_bot_comments"):
        run(pr_for(repo), factory=scripted(finding_on(2)), github=github)

    assert names(github) == ["read_summary", "list_bot_comments"]


@pytest.mark.parametrize("mode", [Mode.SHADOW, Mode.COMMENT])
def test_run_pull_request_needs_a_github_client_exactly_in_comment_mode(
    repo: Path, tmp_path: Path, mode: Mode
) -> None:
    github = FakeGitHubClient() if mode is Mode.SHADOW else None

    with pytest.raises(ValueError, match="exactly in comment mode"):
        asyncio.run(
            run_pull_request(
                pr_for(repo),
                mode=mode,
                git=GitRepo(repo, timeout_s=60),
                make_provider=MockProvider,
                github=github,
                settings=MOCK,
                audit=AuditLog(tmp_path / "audit.jsonl", clock=lambda: NOW),
                environ={},
                clock=lambda: NOW,
            )
        )


# Incremental review (P2-S7).


def numbered(changes: Mapping[int, str]) -> str:
    """Thirty lines, ``a<n> = <n>``, with ``changes`` replacing some right-hand sides."""
    return "".join(f"a{n} = {changes.get(n, str(n))}\n" for n in range(1, 31))


def summary_for(sha: str) -> str:
    return with_summary_marker("**Review bot:** `reviewed`.", reviewed_sha=sha)


class History:
    """A pull request reviewed at ``reviewed``, then pushed to ``head``, changing x.py again."""

    def __init__(self, repo: Path) -> None:
        self.base = commit(repo, {"x.py": numbered({}), "y.py": numbered({})})
        self.reviewed = commit(
            repo, {"x.py": numbered({3: "'reviewed'"}), "y.py": numbered({3: "'reviewed'"})}
        )
        self.head = commit(repo, {"x.py": numbered({3: "'reviewed'", 25: "'head'"})})
        self.pr = PullRequestRef(
            repo="owner/repo",
            number=7,
            base_sha=self.base,
            head_sha=self.head,
            is_fork=False,
            is_draft=False,
            event_at=NOW,
            action="synchronize",
        )


@pytest.fixture
def history(repo: Path) -> History:
    return History(repo)


def x_finding(line: int) -> Finding:
    return finding_on(line).model_copy(update={"file": "x.py"})


def test_run_pull_request_reviews_only_the_hunks_changed_since_the_reviewed_commit(
    run: Runner, history: History
) -> None:
    github = FakeGitHubClient()
    github.summary = summary_for(history.reviewed)
    provider = MockProvider(findings=[x_finding(25)])

    shadow = run(history.pr, factory=CountingFactory(provider), github=github)

    (request,) = provider.requests
    assert "+a25 = 'head'" in request.user_prompt
    assert "a3 = 'reviewed'" not in request.user_prompt
    assert "y.py" not in request.user_prompt
    assert (shadow.outcome, shadow.scope, shadow.since) == (
        Outcome.REVIEWED,
        Scope.INCREMENTAL,
        history.reviewed,
    )
    assert applied(shadow) == [("create", "done")]
    assert github.summary == posted_summary(shadow, history.head)


def test_run_pull_request_on_the_reviewed_head_is_skipped_without_a_change(
    run: Runner, history: History
) -> None:
    github = FakeGitHubClient()
    github.summary = summary_for(history.head)
    factory = CountingFactory(MockProvider())

    shadow = run(history.pr, factory=factory, github=github)

    assert (shadow.outcome, shadow.reason) == (Outcome.SKIPPED, Reason.ALREADY_REVIEWED)
    assert factory.calls == 0
    assert names(github) == ["read_summary"]
    assert github.summary == summary_for(history.head)


def test_run_pull_request_after_merging_the_base_has_no_new_changes(
    repo: Path, run: Runner
) -> None:
    commit(repo, {"x.py": numbered({})})
    git(repo, "checkout", "-q", "-b", "feature")
    reviewed = commit(repo, {"x.py": numbered({3: "'feature'"})})
    git(repo, "checkout", "-q", "main")
    base = commit(repo, {"other.py": "main = 1\n"})
    git(repo, "checkout", "-q", "feature")
    git(repo, "merge", "-q", "--no-edit", "main")
    head = git(repo, "rev-parse", "HEAD")
    pr = dataclasses.replace(pr_for(repo), base_sha=base, head_sha=head)
    github = FakeGitHubClient()
    github.summary = summary_for(reviewed)
    factory = CountingFactory(MockProvider())

    shadow = run(pr, factory=factory, github=github)

    assert (shadow.outcome, shadow.reason) == (Outcome.REVIEWED, Reason.NO_NEW_CHANGES)
    assert (shadow.scope, shadow.since, shadow.report) == (Scope.INCREMENTAL, reviewed, None)
    assert factory.calls == 0
    assert names(github) == ["read_summary", "list_bot_comments", "write_summary"]
    assert github.summary == posted_summary(shadow, head)


def test_run_pull_request_after_an_empty_commit_has_no_new_changes(
    repo: Path, run: Runner, history: History
) -> None:
    git(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    head = git(repo, "rev-parse", "HEAD")
    github = FakeGitHubClient()
    github.summary = summary_for(history.head)
    factory = CountingFactory(MockProvider())

    shadow = run(dataclasses.replace(history.pr, head_sha=head), factory=factory, github=github)

    assert shadow.reason is Reason.NO_NEW_CHANGES
    assert factory.calls == 0


@pytest.mark.parametrize(
    "summary",
    [
        None,
        "**Review bot:** `reviewed`.",
        f"**Review bot:** `reviewed`.\n\n<!-- review-bot:v1 summary reviewed={'a' * 39} -->",
        summary_for("c" * 40),
    ],
    ids=["no-summary", "no-marker", "39-hex", "not-in-the-clone"],
)
def test_run_pull_request_without_a_usable_reviewed_commit_reviews_the_whole_pull_request(
    run: Runner, history: History, summary: str | None
) -> None:
    github = FakeGitHubClient()
    github.summary = summary
    provider = MockProvider()

    shadow = run(history.pr, factory=CountingFactory(provider), github=github)

    assert "y.py" in provider.requests[0].user_prompt
    assert (shadow.scope, shadow.since) == (Scope.FULL, None)
    assert github.summary == posted_summary(shadow, history.head)


def test_run_pull_request_after_a_force_push_reviews_the_whole_pull_request(
    repo: Path, run: Runner, history: History
) -> None:
    git(repo, "checkout", "-q", "--detach", history.base)
    rewritten = commit(repo, {"x.py": numbered({25: "'rewritten'"}), "y.py": numbered({1: "0"})})
    pr = dataclasses.replace(history.pr, head_sha=rewritten)
    github = FakeGitHubClient()
    github.summary = summary_for(history.reviewed)
    provider = MockProvider()

    shadow = run(pr, factory=CountingFactory(provider), github=github)

    assert "y.py" in provider.requests[0].user_prompt
    assert (shadow.scope, shadow.since) == (Scope.FULL, None)


def test_run_pull_request_with_a_provider_failure_keeps_the_reviewed_commit(
    run: Runner, history: History
) -> None:
    github = FakeGitHubClient()
    github.summary = summary_for(history.reviewed)
    error = ProviderError(ErrorCategory.TRANSIENT, "Try again.")

    shadow = run(history.pr, factory=CountingFactory(MockProvider(error=error)), github=github)

    assert (shadow.outcome, shadow.scope) == (Outcome.FAILED, Scope.INCREMENTAL)
    assert github.summary == posted_summary(shadow, history.reviewed)


def test_run_pull_request_with_a_failed_github_call_keeps_the_reviewed_commit(
    run: Runner, history: History
) -> None:
    github = FakeGitHubClient()
    github.summary = summary_for(history.reviewed)
    github.fail_on("post_review")

    shadow = run(history.pr, factory=scripted(x_finding(25)), github=github)

    assert applied(shadow) == [("create", "failed")]
    assert github.summary == posted_summary(shadow, history.reviewed)


def test_run_pull_request_raises_when_the_summary_cant_be_read(
    run: Runner, history: History
) -> None:
    github = FakeGitHubClient()
    github.fail_on("read_summary")
    factory = CountingFactory(MockProvider())

    with pytest.raises(GitHubError, match="read_summary"):
        run(history.pr, factory=factory, github=github)

    assert factory.calls == 0


def test_run_pull_request_in_shadow_mode_always_reviews_the_whole_pull_request(
    run: Runner, history: History
) -> None:
    shadow = run(history.pr, factory=CountingFactory(MockProvider()))

    assert (shadow.scope, shadow.since) == (Scope.FULL, None)


def merge_main_into_a_reviewed_branch(
    repo: Path, main_files: Mapping[str, str | bytes]
) -> tuple[PullRequestRef, str]:
    """A branch reviewed at its own commit, then merged with main, which added ``main_files``."""
    commit(repo, {"x.py": numbered({})})
    git(repo, "checkout", "-q", "-b", "feature")
    reviewed = commit(repo, {"x.py": numbered({3: "'feature'"})})
    git(repo, "checkout", "-q", "main")
    base = commit(repo, main_files)
    git(repo, "checkout", "-q", "feature")
    git(repo, "merge", "-q", "--no-edit", "main")
    head = git(repo, "rev-parse", "HEAD")
    return dataclasses.replace(pr_for(repo), base_sha=base, head_sha=head), reviewed


@pytest.mark.parametrize(
    "main_files",
    [{"big.py": "big = 1\n" * 1000}, {"latin.py": b"name = '\xe9'\n"}],
    ids=["over-the-byte-cap", "not-utf-8"],
)
def test_run_pull_request_reviews_the_whole_pull_request_when_the_diff_since_cant_be_used(
    repo: Path, run: Runner, main_files: Mapping[str, str | bytes]
) -> None:
    pr, reviewed = merge_main_into_a_reviewed_branch(repo, main_files)
    github = FakeGitHubClient()
    github.summary = summary_for(reviewed)
    provider = MockProvider()
    settings = Settings(provider="mock", max_diff_bytes=4000)

    shadow = run(pr, factory=CountingFactory(provider), settings=settings, github=github)

    assert "+a3 = 'feature'" in provider.requests[0].user_prompt
    assert (shadow.outcome, shadow.scope, shadow.since) == (Outcome.REVIEWED, Scope.FULL, None)
    assert github.summary == posted_summary(shadow, pr.head_sha)


def test_run_pull_request_blocked_by_a_secret_keeps_the_reviewed_commit_and_sends_its_hunk_again(
    repo: Path, run: Runner
) -> None:
    aws_key = "AKIA" + "ABCDEFGHIJKLMNOP"
    base = commit(repo, {"x.py": numbered({})})
    reviewed = commit(repo, {"x.py": numbered({3: "'reviewed'"})})
    blocked = commit(repo, {"x.py": numbered({3: "'reviewed'", 10: "'b'", 25: repr(aws_key)})})
    fixed = commit(repo, {"x.py": numbered({3: "'reviewed'", 10: "'b'"})})
    pr = dataclasses.replace(pr_for(repo), base_sha=base, head_sha=blocked)
    github = FakeGitHubClient()
    github.summary = summary_for(reviewed)

    shadow = run(pr, factory=CountingFactory(MockProvider()), github=github)

    assert (shadow.outcome, shadow.scope) == (Outcome.BLOCKED_SECRET, Scope.INCREMENTAL)
    assert github.summary == posted_summary(shadow, reviewed)

    provider = MockProvider()
    again = run(
        dataclasses.replace(pr, head_sha=fixed), factory=CountingFactory(provider), github=github
    )

    assert (again.scope, again.since) == (Scope.INCREMENTAL, reviewed)
    assert "+a10 = 'b'" in provider.requests[0].user_prompt
    assert aws_key not in provider.requests[0].user_prompt


def config_pr(repo: Path, config: str | None, head_files: Mapping[str, str]) -> PullRequestRef:
    """A pull request whose base holds ``config`` as ``.review-bot.toml``, if given."""
    base_files = {"app.py": "def run():\n    return 1\n"}
    if config is not None:
        base_files[".review-bot.toml"] = config
    base = commit(repo, base_files)
    head = commit(repo, head_files)
    return dataclasses.replace(pr_for(repo), base_sha=base, head_sha=head)


def finding_at(line: int, severity: str, file: str = "app.py") -> Finding:
    return Finding(
        file=file, line=line, severity=severity, category="logic", rationale="Why.", confidence=1.0
    )


CHANGED_APP = {"app.py": "def run():\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n"}


def test_run_pull_request_without_a_config_file_uses_the_defaults(repo: Path, run: Runner) -> None:
    pr = config_pr(repo, None, CHANGED_APP)

    shadow = run(pr, factory=CountingFactory(MockProvider(findings=[])))

    assert shadow.config == {
        "source": "default",
        "threshold": "low",
        "exclude": [],
        "max_open_comments": 10,
        "domain_rules": [],
    }


def test_run_pull_request_applies_the_base_threshold_not_the_heads(repo: Path, run: Runner) -> None:
    pr = config_pr(
        repo,
        'threshold = "medium"\n',
        {**CHANGED_APP, ".review-bot.toml": 'threshold = "critical"\n'},
    )
    provider = MockProvider(findings=[finding_at(2, "low"), finding_at(3, "medium")])

    shadow = run(pr, factory=CountingFactory(provider))

    assert [comment.planned.reviewed.finding.line for comment in shadow.comments] == [3]
    assert shadow.config is not None
    assert shadow.config["source"] == f".review-bot.toml@{pr.base_sha}"
    assert shadow.config["threshold"] == "medium"


def test_run_pull_request_runs_the_base_configs_domain_rules_and_lists_them(
    repo: Path, run: Runner
) -> None:
    config = '[[domain_rules]]\nrule = "decimal-money"\npaths = ["*.py"]\nseverity = "medium"\n'
    pr = config_pr(repo, config, {"app.py": "def run():\n    return 1.5\n"})

    shadow = run(pr, factory=CountingFactory(MockProvider(findings=[])))

    [comment] = shadow.comments
    reviewed = comment.planned.reviewed
    assert (reviewed.rule, reviewed.finding.line, reviewed.finding.severity) == (
        "decimal-money",
        2,
        "medium",
    )
    assert shadow.config is not None
    assert shadow.config["domain_rules"] == [
        {"rule": "decimal-money", "paths": ["*.py"], "severity": "medium"}
    ]


def test_run_pull_request_keeps_a_threshold_the_settings_were_given(
    repo: Path, run: Runner
) -> None:
    pr = config_pr(repo, 'threshold = "medium"\n', CHANGED_APP)
    provider = MockProvider(findings=[finding_at(3, "medium"), finding_at(4, "high")])
    settings = Settings(provider="mock", threshold="high")

    shadow = run(pr, factory=CountingFactory(provider), settings=settings)

    assert [comment.planned.reviewed.finding.line for comment in shadow.comments] == [4]


def test_run_pull_request_doesnt_send_an_excluded_file_to_the_model(
    repo: Path, run: Runner
) -> None:
    pr = config_pr(
        repo, 'exclude = ["*.snap"]\n', {**CHANGED_APP, "tests/x/a.snap": "snapshot text\n"}
    )
    provider = MockProvider(findings=[])

    shadow = run(pr, factory=CountingFactory(provider))

    assert "snapshot text" not in provider.requests[0].user_prompt
    assert shadow.report is not None
    assert shadow.report["skipped_files"] == [
        {"file": "tests/x/a.snap", "reason": "excluded_by_config"}
    ]


def test_run_pull_request_creates_comments_up_to_the_repository_limit(
    repo: Path, run: Runner
) -> None:
    pr = config_pr(repo, "max_open_comments = 3\n", CHANGED_APP)
    provider = MockProvider(findings=[finding_at(line, "high") for line in range(2, 7)])

    shadow = run(pr, factory=CountingFactory(provider))

    assert len(shadow.comments) == 3
    assert [item.reason for item in shadow.withheld] == ["over_limit", "over_limit"]


@pytest.mark.parametrize(
    "files",
    [
        {".review-bot.toml": "max_diff_bytes = 1\n"},
        {".review-bot.toml/inner.toml": "x = 1\n"},
    ],
    ids=["unknown-key", "directory"],
)
def test_run_pull_request_with_an_invalid_config_fails_without_a_provider(
    repo: Path, run: Runner, files: dict[str, str]
) -> None:
    commit(repo, files)
    pr = config_pr(repo, None, CHANGED_APP)
    factory = CountingFactory(MockProvider(findings=[]))

    shadow = run(pr, factory=factory)

    assert (shadow.outcome, shadow.reason, shadow.config) == (
        Outcome.FAILED,
        Reason.INVALID_CONFIG,
        None,
    )
    assert factory.calls == 0
    assert "invalid" in shadow.summary_body


def test_run_pull_request_with_an_invalid_config_in_comment_mode_writes_only_the_summary(
    repo: Path, run: Runner
) -> None:
    pr = config_pr(repo, 'threshold = "urgent"\n', CHANGED_APP)
    github = FakeGitHubClient()

    shadow = run(pr, factory=CountingFactory(MockProvider(findings=[])), github=github)

    assert shadow.outcome is Outcome.FAILED
    assert github.summary is not None
    assert "(`invalid_config`)" in github.summary
    assert "threshold" in github.summary
    assert github.comments == []


def test_a_refused_run_reads_no_config(repo: Path, run: Runner) -> None:
    shadow = run(
        unknown_commits(pr_for(repo, is_fork=True)), factory=CountingFactory(MockProvider())
    )

    assert shadow.config is None


def test_backfill_pull_request_applies_the_base_config(repo: Path, tmp_path: Path) -> None:
    pr = config_pr(repo, 'threshold = "high"\n', CHANGED_APP)
    provider = MockProvider(findings=[finding_at(2, "medium")])

    shadow = asyncio.run(
        backfill_pull_request(
            pr,
            git=GitRepo(repo, timeout_s=60),
            make_provider=lambda: provider,
            settings=MOCK,
            audit=AuditLog(tmp_path / "backfill.jsonl", clock=lambda: NOW),
            clock=lambda: NOW,
        )
    )

    assert shadow.comments == ()
    assert shadow.config is not None
    assert shadow.config["threshold"] == "high"


def test_an_invalid_config_run_keeps_the_last_reviewed_commit_in_the_summary(
    repo: Path, run: Runner
) -> None:
    pr = config_pr(repo, "max_retries = 0\n", CHANGED_APP)
    github = FakeGitHubClient()
    reviewed = "e" * 40
    github.summary = summary_for(reviewed)

    run(pr, factory=CountingFactory(MockProvider(findings=[])), github=github)

    assert github.summary is not None
    assert "(`invalid_config`)" in github.summary
    assert github.summary.endswith(f"<!-- review-bot:v1 summary reviewed={reviewed} -->")


def codeowners_pr(
    repo: Path, base_files: Mapping[str, str | bytes], head_files: Mapping[str, str] = CHANGED_APP
) -> PullRequestRef:
    """A pull request changing ``app.py``, whose base also holds ``base_files``."""
    base = commit(repo, {"app.py": "def run():\n    return 1\n", **base_files})
    head = commit(repo, head_files)
    return dataclasses.replace(pr_for(repo), base_sha=base, head_sha=head)


def shadow_json(shadow: ShadowReport) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(render_shadow_json(shadow))
    return document


def test_run_pull_request_names_owners_from_the_base_codeowners_not_the_heads(
    repo: Path, run: Runner
) -> None:
    pr = codeowners_pr(
        repo,
        {".github/CODEOWNERS": "* @org/core\n"},
        {**CHANGED_APP, ".github/CODEOWNERS": "* @org/other\n"},
    )

    shadow = run(pr, factory=CountingFactory(MockProvider(findings=[finding_at(2, "high")])))

    document = shadow_json(shadow)
    assert [entry["owners"] for entry in document["comments"]] == [["@org/core"]]
    assert document["codeowners"] == {
        "source": f".github/CODEOWNERS@{pr.base_sha}",
        "status": "found",
        "rules": 1,
        "invalid_lines": 0,
    }
    assert "- `@org/core`: 1" in shadow.summary_body


@pytest.mark.parametrize(
    ("paths", "used"),
    [
        ((".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"), ".github/CODEOWNERS"),
        (("CODEOWNERS", "docs/CODEOWNERS"), "CODEOWNERS"),
        (("docs/CODEOWNERS",), "docs/CODEOWNERS"),
    ],
)
def test_run_pull_request_reads_only_the_first_codeowners_that_exists(
    repo: Path, run: Runner, paths: tuple[str, ...], used: str
) -> None:
    pr = codeowners_pr(repo, {path: f"* @owner{index}\n" for index, path in enumerate(paths)})

    shadow = run(pr, factory=CountingFactory(MockProvider(findings=[finding_at(2, "high")])))

    document = shadow_json(shadow)
    assert document["codeowners"]["source"] == f"{used}@{pr.base_sha}"
    assert [entry["owners"] for entry in document["comments"]] == [["@owner0"]]


def test_run_pull_request_without_codeowners_names_no_owner(repo: Path, run: Runner) -> None:
    pr = codeowners_pr(repo, {})

    shadow = run(pr, factory=CountingFactory(MockProvider(findings=[finding_at(2, "high")])))

    document = shadow_json(shadow)
    assert document["codeowners"] == {
        "source": None,
        "status": "absent",
        "rules": 0,
        "invalid_lines": 0,
    }
    assert [entry["owners"] for entry in document["comments"]] == [[]]


def test_run_pull_request_with_codeowners_too_large_goes_on_with_no_owner(
    repo: Path, run: Runner
) -> None:
    pr = codeowners_pr(repo, {"CODEOWNERS": "* @org/core\n" + "#" * MAX_CODEOWNERS_BYTES})

    shadow = run(pr, factory=CountingFactory(MockProvider(findings=[finding_at(2, "high")])))

    document = shadow_json(shadow)
    assert (shadow.outcome, document["codeowners"]["status"]) == (Outcome.REVIEWED, "too_large")
    assert [entry["owners"] for entry in document["comments"]] == [[]]


def symlink_base(repo: Path, files: Mapping[str, str]) -> str:
    """A base commit holding ``files`` and a symbolic link at ``.github/CODEOWNERS``.

    The link's blob is staged directly, so the test needs no symlink support on disk; the next
    commit unstages it, since no file on disk backs it, so only this commit holds it.
    """
    commit(repo, {"app.py": "def run():\n    return 1\n", **files})
    target = repo.parent / "link-target"
    target.write_text("../CODEOWNERS", encoding="utf-8")
    link = git(repo, "hash-object", "-w", str(target))
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{link},.github/CODEOWNERS")
    git(repo, "commit", "-q", "-m", "link")
    return git(repo, "rev-parse", "HEAD")


def test_run_pull_request_passes_over_a_codeowners_symlink_to_the_next_path(
    repo: Path, run: Runner
) -> None:
    base = symlink_base(repo, {"CODEOWNERS": "* @org/core\n"})
    pr = dataclasses.replace(pr_for(repo), base_sha=base, head_sha=commit(repo, CHANGED_APP))

    shadow = run(pr, factory=CountingFactory(MockProvider(findings=[finding_at(2, "high")])))

    document = shadow_json(shadow)
    assert document["codeowners"]["source"] == f"CODEOWNERS@{base}"
    assert [entry["owners"] for entry in document["comments"]] == [["@org/core"]]


def test_run_pull_request_with_only_non_files_for_codeowners_names_no_owner(
    repo: Path, run: Runner
) -> None:
    base = symlink_base(repo, {"CODEOWNERS/inner": "x\n"})
    pr = dataclasses.replace(pr_for(repo), base_sha=base, head_sha=commit(repo, CHANGED_APP))

    shadow = run(pr, factory=CountingFactory(MockProvider(findings=[finding_at(2, "high")])))

    document = shadow_json(shadow)
    assert (shadow.outcome, document["codeowners"]) == (
        Outcome.REVIEWED,
        {
            "source": f".github/CODEOWNERS@{base}",
            "status": "not_a_file",
            "rules": 0,
            "invalid_lines": 0,
        },
    )
    assert [entry["owners"] for entry in document["comments"]] == [[]]


def test_a_refused_run_reads_no_codeowners(repo: Path, run: Runner) -> None:
    shadow = run(
        unknown_commits(pr_for(repo, is_fork=True)), factory=CountingFactory(MockProvider())
    )

    assert shadow.codeowners is None


def test_a_run_planning_no_comments_reads_no_codeowners(repo: Path, run: Runner) -> None:
    pr = codeowners_pr(repo, {"CODEOWNERS": "* @org/core\n"})
    provider = MockProvider(error=ProviderError(ErrorCategory.TRANSIENT, "Try again."))

    shadow = run(pr, factory=CountingFactory(provider))

    assert (shadow.outcome, shadow.codeowners) == (Outcome.FAILED, None)


def test_run_pull_request_in_comment_mode_posts_owners_only_in_the_summary(
    repo: Path, run: Runner
) -> None:
    github = FakeGitHubClient()
    pr = codeowners_pr(repo, {".github/CODEOWNERS": "* @org/core\n"})

    run(pr, factory=scripted(finding_at(2, "high")), github=github)

    (posted,) = github.reviews[0].comments
    assert "@" not in posted.body
    assert github.summary is not None
    assert "Owners:\n\n- `@org/core`: 1" in github.summary
