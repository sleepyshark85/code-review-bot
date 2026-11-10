"""``run_pull_request`` and ``backfill_pull_request`` in context mode (P3-S8)."""

import asyncio
import dataclasses
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from audit_records import read_records
from git_samples import commit, git, init_repo

from review_bot.audit import AuditLog
from review_bot.errors import ErrorCategory, ProviderCap, ProviderError
from review_bot.findings import EvidenceSpan, Finding
from review_bot.git import GitRepo
from review_bot.pr.comments import with_summary_marker
from review_bot.pr.event import PullRequestRef
from review_bot.pr.fake_github import FakeGitHubClient
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.runner import backfill_pull_request, run_pull_request
from review_bot.pr.shadow import Mode, Scope, ShadowReport
from review_bot.prompt import CONTEXT_PROMPT_VERSION, PROMPT_VERSION
from review_bot.providers.mock import MockProvider
from review_bot.settings import Settings

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
CONTEXT = Settings(provider="mock", context="repo")
DIFF_MODE = Settings(provider="mock")
# Twenty lines, so the diff of a change to the last one doesn't show the first.
BASE_TEXT = "".join(f"line{n} = {n}\n" for n in range(1, 21))
HEAD_TEXT = BASE_TEXT.replace("line1 = 1\n", "line1 = 'head'\n").replace(
    "line20 = 20\n", "line20 = 'changed'\n"
)
READ_APP = ("read", {"path": "app.py", "start_line": 1, "line_count": 1})


class PullRequest:
    """A same-repo pull request whose head is committed but not checked out: the tree is base."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.base = commit(repo, {"app.py": BASE_TEXT})
        self.head = commit(repo, {"app.py": HEAD_TEXT})
        git(repo, "checkout", "-q", "--detach", self.base)
        self.ref = PullRequestRef(
            repo="owner/repo",
            number=8,
            base_sha=self.base,
            head_sha=self.head,
            is_fork=False,
            is_draft=False,
            event_at=NOW,
            action="opened",
        )


@pytest.fixture
def pull(tmp_path: Path) -> PullRequest:
    return PullRequest(init_repo(tmp_path / "repo"))


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl", clock=lambda: NOW)


def run(  # noqa: PLR0913 - keyword-only, each a separate input of the run
    pr: PullRequestRef,
    repo: Path,
    audit: AuditLog,
    *,
    provider: MockProvider,
    settings: Settings = CONTEXT,
    github: FakeGitHubClient | None = None,
) -> ShadowReport:
    return asyncio.run(
        run_pull_request(
            pr,
            mode=Mode.SHADOW if github is None else Mode.COMMENT,
            git=GitRepo(repo, timeout_s=60),
            make_provider=lambda: provider,
            github=github,
            settings=settings,
            audit=audit,
            environ={},
            clock=lambda: NOW,
        )
    )


def finding(line: int, *evidence: EvidenceSpan) -> Finding:
    return Finding(
        file="app.py",
        line=line,
        severity="high",
        category="logic",
        rationale="Why.",
        confidence=0.9,
        evidence=evidence,
    )


def test_run_pull_request_in_context_mode_reads_the_head_commit_not_the_working_tree(
    pull: PullRequest, audit: AuditLog
) -> None:
    provider = MockProvider(findings=[], tool_calls=[READ_APP])

    shadow = run(pull.ref, pull.repo, audit, provider=provider)

    (result,) = provider.tool_results
    assert "line1 = 'head'" in result
    assert "line1 = 1" not in result
    assert shadow.outcome is Outcome.REVIEWED
    report: Any = shadow.report
    assert (report["mode"], report["prompt_version"]) == ("context", CONTEXT_PROMPT_VERSION)
    assert report["context"]["commit"] == pull.head


def test_run_pull_request_in_context_mode_keeps_evidence_the_tools_returned_in_a_changed_file(
    pull: PullRequest, audit: AuditLog
) -> None:
    # Line 1 isn't in the diff, which shows only lines 17 to 20; the tool read it at the head.
    cited = EvidenceSpan(file="app.py", start_line=1, end_line=1)
    provider = MockProvider(findings=[finding(20, cited)], tool_calls=[READ_APP])

    shadow = run(pull.ref, pull.repo, audit, provider=provider)

    (comment,) = shadow.comments
    assert comment.planned.reviewed.finding.evidence == (cited,)
    assert comment.planned.reviewed.evidence_dropped == 0


def test_run_pull_request_in_diff_mode_offers_no_tools_and_uses_phase_1s_prompt(
    pull: PullRequest, audit: AuditLog
) -> None:
    provider = MockProvider(findings=[], tool_calls=[READ_APP])

    shadow = run(pull.ref, pull.repo, audit, provider=provider, settings=DIFF_MODE)

    (request,) = provider.requests
    assert request.tools is None
    assert provider.tool_results == []
    report: Any = shadow.report
    assert (report["mode"], report["prompt_version"], report["context"]) == (
        "diff",
        PROMPT_VERSION,
        None,
    )


def test_run_pull_request_in_context_mode_records_each_tool_call_in_the_audit_log(
    pull: PullRequest, audit: AuditLog
) -> None:
    provider = MockProvider(findings=[], tool_calls=[READ_APP, ("glob", {"pattern": "*.py"})])

    run(pull.ref, pull.repo, audit, provider=provider)

    tool_calls = [record for record in read_records(audit.path) if record["stage"] == "context"]
    assert [(record["event"], record["data"]["tool"]) for record in tool_calls] == [
        ("tool_call", "read"),
        ("tool_call", "glob"),
    ]


@pytest.mark.parametrize("cap", ["cost", "turns"])
def test_run_pull_request_stopped_by_a_cap_reports_a_permanent_failure(
    pull: PullRequest, audit: AuditLog, cap: ProviderCap
) -> None:
    error = ProviderError(ErrorCategory.PERMANENT, "The review hit its cap.", cap=cap)

    shadow = run(pull.ref, pull.repo, audit, provider=MockProvider(error=error))

    assert (shadow.outcome, shadow.reason, shadow.error) == (
        Outcome.FAILED,
        ErrorCategory.PERMANENT,
        error,
    )


def test_run_pull_request_in_context_mode_refused_as_a_fork_runs_no_tool_or_provider(
    pull: PullRequest, audit: AuditLog
) -> None:
    provider = MockProvider(findings=[], tool_calls=[READ_APP])

    shadow = run(dataclasses.replace(pull.ref, is_fork=True), pull.repo, audit, provider=provider)

    assert (shadow.outcome, shadow.reason) == (Outcome.SKIPPED, Reason.FORK)
    assert provider.requests == []
    assert provider.tool_results == []


def test_run_pull_request_reviewing_only_new_hunks_still_reads_the_head_commit(
    pull: PullRequest, audit: AuditLog
) -> None:
    newer_text = HEAD_TEXT.replace("line19 = 19\n", "line19 = 'newer'\n")
    git(pull.repo, "checkout", "-q", pull.head)
    newer = commit(pull.repo, {"app.py": newer_text})
    git(pull.repo, "checkout", "-q", "--detach", pull.base)
    github = FakeGitHubClient()
    github.summary = with_summary_marker("**Review bot:** `reviewed`.", reviewed_sha=pull.head)
    read_line_19 = ("read", {"path": "app.py", "start_line": 19, "line_count": 1})
    provider = MockProvider(findings=[], tool_calls=[read_line_19])
    pr = dataclasses.replace(pull.ref, head_sha=newer, action="synchronize")

    shadow = run(pr, pull.repo, audit, provider=provider, github=github)

    assert (shadow.scope, shadow.since) == (Scope.INCREMENTAL, pull.head)
    (request,) = provider.requests
    assert "line19 = 'newer'" in request.user_prompt
    assert "line19 = 'newer'" in provider.tool_results[0]
    report: Any = shadow.report
    assert report["context"]["commit"] == newer


def test_backfill_pull_request_in_context_mode_reads_the_head_commit(
    pull: PullRequest, audit: AuditLog
) -> None:
    provider = MockProvider(findings=[], tool_calls=[READ_APP], cost_usd=Decimal("0.25"))

    shadow = asyncio.run(
        backfill_pull_request(
            pull.ref,
            git=GitRepo(pull.repo, timeout_s=60),
            make_provider=lambda: provider,
            settings=CONTEXT,
            audit=audit,
            clock=lambda: NOW,
        )
    )

    assert "line1 = 'head'" in provider.tool_results[0]
    assert shadow.cost_usd == Decimal("0.25")
    report: Any = shadow.report
    assert report["context"]["commit"] == pull.head
