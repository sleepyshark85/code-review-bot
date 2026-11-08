import asyncio
import dataclasses
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from audit_records import read_records
from git_samples import commit, init_repo

from review_bot.audit import AuditLog
from review_bot.errors import ErrorCategory, GitError, ProviderError
from review_bot.findings import Finding
from review_bot.pr.event import PullRequestRef
from review_bot.pr.git import GitRepo
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.runner import run_pull_request
from review_bot.pr.shadow import ShadowReport
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
    ) -> ShadowReport:
        return asyncio.run(
            run_pull_request(
                pr,
                git=GitRepo(self.repo, timeout_s=60),
                make_provider=factory,
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
