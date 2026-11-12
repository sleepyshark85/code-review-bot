"""The pipeline masks personal data in everything a request sends, and counts it (P4-S4)."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from audit_records import data_of, events
from diff_samples import section
from git_samples import commit, init_repo

from review_bot.audit import AuditLog
from review_bot.context.issues import IssueStatus, LinkedIssue
from review_bot.context.ledger import ToolName
from review_bot.context.tools import RepoRef
from review_bot.diff_parser import parse_diff
from review_bot.errors import SecretDetectedError
from review_bot.findings import Category, Finding, Severity
from review_bot.git import GitRepo
from review_bot.governance.policy import BUILTIN_POLICY, Policy
from review_bot.governance.redaction import RedactionKind
from review_bot.prompt import build_request
from review_bot.providers.mock import MockProvider
from review_bot.report import PipelineResult
from review_bot.review import ReviewContext, run_pipeline
from review_bot.settings import Settings

ADDRESS = "jane.doe@rightship.com"
DIFF = section("app.py", f'+contact = "{ADDRESS}"\n')
REDACTING = Policy(
    version="pii",
    sha256="0" * 64,
    source="p.toml",
    tools=frozenset(ToolName),
    max_cost_usd=None,
    redaction=frozenset({RedactionKind.EMAIL}),
)
AWS_KEY = "AK" + "IA" + "ABCDEFGH" + "23456789"


def finding(suggestion: str | None = None) -> Finding:
    return Finding(
        file="app.py",
        line=1,
        severity=Severity.HIGH,
        category=Category.SECURITY,
        rationale="Personal data in code.",
        confidence=0.9,
        suggestion=suggestion,
    )


def review(
    tmp_path: Path,
    provider: MockProvider,
    *,
    policy: Policy = REDACTING,
    diff: str = DIFF,
    context: ReviewContext | None = None,
) -> PipelineResult:
    diff_path = tmp_path / "change.diff"
    diff_path.write_bytes(diff.encode("utf-8"))
    audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: datetime(2026, 11, 12, tzinfo=UTC))
    pipeline = run_pipeline(
        diff_path, Settings(provider="mock"), provider, audit, context=context, policy=policy
    )
    return asyncio.run(pipeline)


def context_with(tmp_path: Path, issues: tuple[LinkedIssue, ...] = ()) -> ReviewContext:
    repo = init_repo(tmp_path / "repo")
    sha = commit(repo, {"owners.txt": "bob@rightship.com\n"})
    return ReviewContext(
        repo=RepoRef(git=GitRepo(repo, timeout_s=60), commit=sha), read_issues=lambda: issues
    )


def test_the_diff_is_masked_and_a_finding_on_its_line_keeps_its_fingerprint(
    tmp_path: Path,
) -> None:
    provider = MockProvider(findings=[finding()])

    (tmp_path / "masked").mkdir()
    (tmp_path / "plain").mkdir()
    result = review(tmp_path / "masked", provider)
    unmasked = review(tmp_path / "plain", MockProvider(findings=[finding()]), policy=BUILTIN_POLICY)

    [request] = provider.requests
    assert 'contact = "[REDACTED:email]"' in request.user_prompt
    assert ADDRESS not in request.user_prompt
    [kept] = result.decision.reported
    assert kept.fingerprint == unmasked.decision.reported[0].fingerprint


def test_the_counts_go_in_the_result_and_one_audit_record_without_the_value(
    tmp_path: Path,
) -> None:
    result = review(tmp_path, MockProvider(findings=[]))

    log = tmp_path / "audit.jsonl"
    assert result.redaction == {"diff": {RedactionKind.EMAIL: 1}}
    assert events(log).count("static.redaction") == 1
    assert data_of(log, "static.redaction") == {"counts": {"diff": {"email": 1}}}
    assert ADDRESS not in log.read_text(encoding="ascii")


def test_under_a_policy_that_masks_nothing_found_the_record_has_no_counts(
    tmp_path: Path,
) -> None:
    result = review(tmp_path, MockProvider(findings=[]), diff=section("app.py"))

    assert result.redaction == {}
    assert data_of(tmp_path / "audit.jsonl", "static.redaction") == {"counts": {}}


def test_under_the_builtin_policy_the_prompt_is_unchanged_and_nothing_is_recorded(
    tmp_path: Path,
) -> None:
    provider = MockProvider(findings=[])

    result = review(tmp_path, provider, policy=BUILTIN_POLICY)

    [request] = provider.requests
    unmasked = build_request(parse_diff(DIFF).reviewable_text, model=request.model)
    assert request.user_prompt == unmasked.user_prompt
    assert ADDRESS in request.user_prompt
    assert result.redaction == {}
    assert "static.redaction" not in events(tmp_path / "audit.jsonl")


def test_tool_results_and_linked_issues_are_masked_and_counted_by_source(
    tmp_path: Path,
) -> None:
    issues = (
        LinkedIssue(7, IssueStatus.INCLUDED, "Ask ann@rightship.com\n\nor carl@rightship.com"),
    )
    provider = MockProvider(findings=[], tool_calls=[("read", {"path": "owners.txt"})])

    result = review(tmp_path, provider, context=context_with(tmp_path, issues))

    [request] = provider.requests
    assert "Ask [REDACTED:email]\n\nor [REDACTED:email]" in request.user_prompt
    assert "1\t[REDACTED:email]" in provider.tool_results[0]
    assert "bob@" not in provider.tool_results[0]
    assert result.redaction == {
        "diff": {RedactionKind.EMAIL: 1},
        "tools": {RedactionKind.EMAIL: 1},
        "issues": {RedactionKind.EMAIL: 2},
    }
    assert data_of(tmp_path / "audit.jsonl", "context.issue")["bytes_sent"] == len(
        b"Ask [REDACTED:email]\n\nor [REDACTED:email]"
    )


@pytest.mark.parametrize(("is_context", "dropped"), [(True, "redacted"), (False, "not_requested")])
def test_a_suggestion_holding_a_placeholder_is_dropped_and_its_finding_kept(
    tmp_path: Path, is_context: bool, dropped: str
) -> None:
    provider = MockProvider(findings=[finding(suggestion='contact = "[REDACTED:email]"')])
    context = context_with(tmp_path) if is_context else None

    result = review(tmp_path, provider, context=context)

    [kept] = result.decision.reported
    assert (kept.finding.suggestion, kept.suggestion_dropped) == (None, dropped)


def test_a_secret_still_stops_the_review_before_anything_is_masked(tmp_path: Path) -> None:
    provider = MockProvider(findings=[])

    with pytest.raises(SecretDetectedError):
        review(tmp_path, provider, diff=section("app.py", f'+key = "{AWS_KEY}" # {ADDRESS}\n'))

    assert provider.requests == []
    assert "static.redaction" not in events(tmp_path / "audit.jsonl")
