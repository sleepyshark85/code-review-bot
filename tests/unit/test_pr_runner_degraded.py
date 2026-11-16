"""A pull request run that degrades to the rules' findings when no model is available (P4-S8)."""

import asyncio
import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest
from git_samples import commit, init_repo

from review_bot.audit import AuditLog
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Source
from review_bot.git import GitRepo
from review_bot.governance.policy import BUILTIN_POLICY, Policy
from review_bot.pr.comments import read_reviewed_sha, with_summary_marker
from review_bot.pr.event import PullRequestRef
from review_bot.pr.fake_github import FakeGitHubClient
from review_bot.pr.outcome import Outcome
from review_bot.pr.runner import backfill_pull_request, run_pull_request
from review_bot.pr.shadow import Mode, ShadowReport
from review_bot.providers.mock import MockProvider
from review_bot.settings import Settings

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
DEGRADES = dataclasses.replace(BUILTIN_POLICY, degrade_to_rules=True)
SWALLOWING = "def run():\n    try:\n        return 2\n    except Exception:\n        pass\n"
DOWN = ProviderError(ErrorCategory.UNAVAILABLE, "Down.")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def pr_for(repo: Path) -> PullRequestRef:
    base = commit(repo, {"app.py": "def run():\n    return 1\n"})
    head = commit(repo, {"app.py": SWALLOWING})
    return PullRequestRef(
        repo="owner/repo",
        number=7,
        base_sha=base,
        head_sha=head,
        is_fork=False,
        is_draft=False,
        event_at=NOW,
        action="opened",
    )


def run(
    repo: Path, pr: PullRequestRef, *, policy: Policy, github: FakeGitHubClient | None = None
) -> ShadowReport:
    return asyncio.run(
        run_pull_request(
            pr,
            mode=Mode.SHADOW if github is None else Mode.COMMENT,
            git=GitRepo(repo, timeout_s=60),
            make_provider=lambda: MockProvider(error=DOWN),
            issue_reader=None,
            github=github,
            settings=Settings(provider="mock", threshold="low"),
            policy=policy,
            audit=AuditLog(repo.parent / "audit.jsonl", clock=lambda: NOW),
            environ={},
            clock=lambda: NOW,
        )
    )


def test_a_run_under_a_policy_that_degrades_reports_the_rules_findings(repo: Path) -> None:
    shadow = run(repo, pr_for(repo), policy=DEGRADES)

    assert (shadow.outcome, shadow.reason) == (Outcome.DEGRADED, ErrorCategory.UNAVAILABLE)
    assert [comment.planned.reviewed.source for comment in shadow.comments] == [Source.RULE]
    assert shadow.error is DOWN


def test_a_run_under_the_builtin_policy_fails_as_before(repo: Path) -> None:
    shadow = run(repo, pr_for(repo), policy=BUILTIN_POLICY)

    assert (shadow.outcome, shadow.comments) == (Outcome.FAILED, ())


def test_a_degraded_comment_mode_run_posts_the_rule_findings_and_keeps_the_reviewed_sha(
    repo: Path,
) -> None:
    pr = pr_for(repo)
    earlier = "a" * 40
    github = FakeGitHubClient()
    github.summary = with_summary_marker("**Review bot:** `reviewed`.", reviewed_sha=earlier)

    shadow = run(repo, pr, policy=DEGRADES, github=github)

    assert [call.name for call in github.calls][-2:] == ["post_review", "write_summary"]
    assert github.summary is not None
    assert read_reviewed_sha(github.summary) == earlier
    assert shadow.summary_body.startswith("Degraded: no model was available")


def test_a_backfill_never_degrades_whatever_the_policy(repo: Path) -> None:
    pr = dataclasses.replace(pr_for(repo), action="closed")

    shadow = asyncio.run(
        backfill_pull_request(
            pr,
            git=GitRepo(repo, timeout_s=60),
            make_provider=lambda: MockProvider(error=DOWN),
            issue_reader=None,
            settings=Settings(provider="mock"),
            policy=DEGRADES,
            audit=AuditLog(repo.parent / "audit.jsonl", clock=lambda: NOW),
            clock=lambda: NOW,
        )
    )

    assert (shadow.outcome, shadow.reason) == (Outcome.FAILED, ErrorCategory.UNAVAILABLE)
