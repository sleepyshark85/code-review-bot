"""A degraded run's report, summary and metrics (P4-S8)."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from diff_samples import section
from metrics_samples import key, parsed, posted, signal_label, summarise, timed_report, usage
from shadow_samples import entry, report

from review_bot.diff_parser import parse_diff
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Category, Finding, ReviewedFinding, RuleId, Severity, Source
from review_bot.governance.policy import BUILTIN_POLICY
from review_bot.pr.comments import reviewed_summary
from review_bot.pr.config import DEFAULT_REPO_CONFIG
from review_bot.pr.outcome import Outcome
from review_bot.pr.plan import CommentPlan
from review_bot.pr.run_metrics import OutcomeCount
from review_bot.pr.shadow import Mode, Origin, build_shadow_report, render_shadow_json
from review_bot.pr.units import would_be_comments
from review_bot.prompt import PROMPT_VERSION
from review_bot.providers.base import TokenUsage
from review_bot.report import PipelineResult
from review_bot.settings import Settings
from review_bot.threshold import Decision
from review_bot.verify import VerifyResult

DEGRADED_LINE = "Degraded: no model was available, so only the rules ran."
STARTED = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
DIFF = parse_diff(section("a.py", "+first\n+second\n"))
DOWN = ProviderError(ErrorCategory.UNAVAILABLE, "Down.")


def rule_finding() -> ReviewedFinding:
    finding = Finding(
        file="a.py",
        line=2,
        severity=Severity.MEDIUM,
        category=Category.LOGIC,
        rationale="Swallowed.",
        confidence=1.0,
        rule=RuleId.SWALLOWED_EXCEPTION,
    )
    return ReviewedFinding(finding, Source.RULE, RuleId.SWALLOWED_EXCEPTION, "0" * 16)


def degraded_result() -> PipelineResult:
    findings = (rule_finding(),)
    return PipelineResult(
        VerifyResult(kept=findings, checks=()),
        Decision(reported=findings, below_threshold=()),
        TokenUsage(0, 0),
        (),
        parsed=DIFF,
        model_called=True,
        prompt_version=PROMPT_VERSION,
        policy=BUILTIN_POLICY,
        degraded=DOWN,
    )


def build_degraded() -> tuple[Any, dict[str, Any]]:
    shadow = build_shadow_report(
        degraded_result(),
        mode=Mode.SHADOW,
        existing=(),
        settings=Settings(provider="mock"),
        config=DEFAULT_REPO_CONFIG,
        run_id="run-1",
        started_at=STARTED,
        finished_at=STARTED + timedelta(seconds=2),
        origin=Origin.LOCAL,
        pr=None,
    )
    document: dict[str, Any] = json.loads(render_shadow_json(shadow))
    return shadow, document


# --- The summary and the shadow report ------------------------------------------------------


def test_a_degraded_summary_opens_by_saying_only_the_rules_ran() -> None:
    summary = reviewed_summary(
        CommentPlan(), threshold=Severity.LOW, degraded=ErrorCategory.RATE_LIMIT
    )

    first, headline = summary.split("\n\n")
    assert first == DEGRADED_LINE
    assert headline == "**Review bot:** `degraded` (`rate_limit`). No findings at or above low."


def test_a_reviewed_summary_says_nothing_of_degrading() -> None:
    assert "Degraded" not in reviewed_summary(CommentPlan(), threshold=Severity.LOW)


def test_a_degraded_review_is_reported_degraded_with_its_rule_findings_planned() -> None:
    shadow, document = build_degraded()

    assert (document["outcome"], document["reason"]) == ("degraded", "unavailable")
    assert document["report"]["degraded"] == "rules_only"
    [comment] = document["comments"]
    assert (comment["source"], comment["rule_part"]) == ("rule", "swallowed-exception")
    assert document["summary_body"].startswith(DEGRADED_LINE)
    # Kept, so the run exits with the provider's code.
    assert shadow.error is DOWN


# --- Metrics -----------------------------------------------------------------------------------


def degraded_report(*entries: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return report(*entries, outcome="degraded", reason="unavailable", **fields)


def rule_entry(number: int) -> dict[str, Any]:
    return entry(key(number, "swallowed-exception")) | {"source": "rule"}


def test_a_degraded_report_must_hold_its_review() -> None:
    with pytest.raises(ValueError, match="a degraded report must hold its report/v1 object"):
        parsed(degraded_report(report=None))


def test_a_degraded_shadow_reports_rule_findings_are_would_be_comments() -> None:
    [comment] = would_be_comments([parsed(degraded_report(rule_entry(1)))])

    assert comment.entry.source is Source.RULE


def test_metrics_count_degraded_as_its_own_outcome_but_not_its_latency_or_tokens() -> None:
    reports = [
        timed_report(60, 10, report=usage(100, 10)),
        timed_report(900, 500, outcome="degraded", reason="unavailable", report=usage(0, 0)),
    ]

    metrics = summarise(reports)

    assert OutcomeCount("shadow", "event", Outcome.DEGRADED, "unavailable", 1) in metrics.outcomes
    assert (metrics.end_to_end_s.runs, metrics.review_s.runs, metrics.token_runs) == (1, 1, 1)
    assert metrics.end_to_end_s.p95 == 60.0


def test_a_degraded_comment_runs_posted_rule_findings_enter_precision_by_source() -> None:
    created = degraded_report(posted(1) | {"source": "rule"}, mode="comment")

    comment = summarise([created], [signal_label("1" * 16)]).comment

    assert (comment.units, comment.verdicts.accepted) == (1, 1)
    assert comment.splits["source"]["rule"].accepted == 1
