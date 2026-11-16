"""A review that degrades to the rules' findings when no model is available (P4-S8)."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from audit_records import data_of, events
from eval_cases import CALC_DIFF, CALC_FILE

from review_bot.audit import AuditLog
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import RuleId, Severity, Source
from review_bot.providers.base import Attempt, TokenUsage
from review_bot.providers.mock import MockProvider
from review_bot.providers.router import is_routable
from review_bot.report import PipelineResult
from review_bot.review import run_pipeline
from review_bot.settings import Settings

ROUTE = "mock:claude-opus-5-5"
ROUTABLE = [ErrorCategory.UNAVAILABLE, ErrorCategory.TRANSIENT, ErrorCategory.RATE_LIMIT]
NOT_ROUTABLE = [
    ErrorCategory.AUTH,
    ErrorCategory.REFUSAL,
    ErrorCategory.INVALID_OUTPUT,
    ErrorCategory.PERMANENT,
]


def review(
    tmp_path: Path, error: ProviderError, *, degrade_to_rules: bool = True
) -> PipelineResult:
    diff_path = tmp_path / "change.diff"
    diff_path.write_text(CALC_DIFF, encoding="utf-8")
    audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: datetime(2026, 11, 14, tzinfo=UTC))
    settings = Settings(provider="mock", threshold=Severity.LOW)
    provider = MockProvider(error=error)
    return asyncio.run(
        run_pipeline(diff_path, settings, provider, audit, degrade_to_rules=degrade_to_rules)
    )


@pytest.mark.parametrize("category", ROUTABLE)
def test_is_routable_for_an_availability_failure_without_a_cap(category: ErrorCategory) -> None:
    assert is_routable(ProviderError(category, "Down."))
    assert not is_routable(ProviderError(category, "Capped.", cap="cost"))


@pytest.mark.parametrize("category", NOT_ROUTABLE)
def test_is_routable_never_for_any_other_failure(category: ErrorCategory) -> None:
    assert not is_routable(ProviderError(category, "No."))


def test_a_degraded_review_reports_exactly_the_rules_findings(tmp_path: Path) -> None:
    error = ProviderError(ErrorCategory.UNAVAILABLE, "Down.")

    result = review(tmp_path, error)

    assert result.degraded is error
    assert result.model_called
    [reported] = result.decision.reported
    assert (reported.source, reported.rule) == (Source.RULE, RuleId.SWALLOWED_EXCEPTION)
    assert (reported.finding.file, reported.finding.line) == (CALC_FILE, 4)
    # The model's part, which a mock would have answered with its canned finding, is empty.
    assert result.verification.checks == ()
    assert (result.usage, result.cost_usd) == (TokenUsage(0, 0), None)


def test_a_degraded_review_records_its_category_after_the_failure(tmp_path: Path) -> None:
    review(tmp_path, ProviderError(ErrorCategory.TRANSIENT, "Timed out."))

    log = tmp_path / "audit.jsonl"
    recorded = events(log)
    assert recorded.index("agent.failed") < recorded.index("agent.degraded")
    assert data_of(log, "agent.degraded") == {"category": "transient"}
    assert "static.rules" in recorded
    assert recorded.count("policy.decided") == 1


def test_a_degraded_review_without_routing_has_one_failed_attempt(tmp_path: Path) -> None:
    result = review(tmp_path, ProviderError(ErrorCategory.UNAVAILABLE, "Down."))

    assert result.route == (Attempt(ROUTE, ErrorCategory.UNAVAILABLE, None),)


def test_a_degraded_review_keeps_every_routed_attempt(tmp_path: Path) -> None:
    error = ProviderError(ErrorCategory.UNAVAILABLE, "Down.")
    error.attempts = (
        Attempt("claude:claude-opus-5-5", ErrorCategory.UNAVAILABLE, None),
        Attempt("openai:m1", ErrorCategory.UNAVAILABLE, None),
    )

    result = review(tmp_path, error)

    assert result.route == error.attempts


def test_without_degrade_to_rules_a_routable_failure_still_fails(tmp_path: Path) -> None:
    with pytest.raises(ProviderError):
        review(tmp_path, ProviderError(ErrorCategory.UNAVAILABLE, "Down."), degrade_to_rules=False)

    assert "agent.degraded" not in events(tmp_path / "audit.jsonl")


@pytest.mark.parametrize("category", NOT_ROUTABLE)
def test_a_failure_that_isnt_routable_never_degrades(
    tmp_path: Path, category: ErrorCategory
) -> None:
    with pytest.raises(ProviderError):
        review(tmp_path, ProviderError(category, "No."))


def test_a_capped_failure_never_degrades(tmp_path: Path) -> None:
    with pytest.raises(ProviderError):
        review(tmp_path, ProviderError(ErrorCategory.TRANSIENT, "Capped.", cap="turns"))
