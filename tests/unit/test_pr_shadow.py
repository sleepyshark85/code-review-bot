import dataclasses
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, get_args

import pytest
from diff_samples import section

from review_bot.diff_parser import parse_diff
from review_bot.errors import (
    DiffEmptyError,
    DiffNotParsableError,
    DiffNotUtf8Error,
    DiffTooLargeError,
    ErrorCategory,
    ProviderError,
    SecretDetectedError,
)
from review_bot.findings import Category, Finding, ReviewedFinding, Severity, Source
from review_bot.policy import Decision
from review_bot.pr.event import PullRequestRef
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.shadow import (
    REPORTED_ERRORS,
    Origin,
    ReportedError,
    build_shadow_report,
    render_shadow_json,
    render_shadow_markdown,
)
from review_bot.providers.base import TokenUsage
from review_bot.render import report_document
from review_bot.report import PipelineResult
from review_bot.secret_scan import SecretHit, SecretKind
from review_bot.settings import Settings
from review_bot.verify import VerifyResult

SETTINGS = Settings(provider="mock")
STARTED = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
# Assembled, so no token is written literally.
GITHUB_TOKEN = "gh" + "p_" + "A1b2C3d4" * 4 + "E5f6"
DIFF = parse_diff(section("a.py", "+first\n+second\n"))


def reviewed(line: int, *, rationale: str = "Why.") -> ReviewedFinding:
    finding = Finding(
        file="a.py",
        line=line,
        severity=Severity.HIGH,
        category=Category.LOGIC,
        rationale=rationale,
        confidence=0.5,
    )
    return ReviewedFinding(finding, Source.AI, None, f"{line:016x}")


def result(*findings: ReviewedFinding, model_called: bool = True) -> PipelineResult:
    return PipelineResult(
        VerifyResult(kept=findings, checks=()),
        Decision(reported=findings, below_threshold=()),
        TokenUsage(3, 4),
        (),
        parsed=DIFF,
        model_called=model_called,
    )


def build(review: PipelineResult | ReportedError) -> dict[str, Any]:
    shadow = build_shadow_report(
        review,
        settings=SETTINGS,
        run_id="run-1",
        started_at=STARTED,
        finished_at=STARTED + timedelta(seconds=2),
        origin=Origin.LOCAL,
        pr=None,
    )
    document: dict[str, Any] = json.loads(render_shadow_json(shadow))
    return document


def test_render_shadow_json_writes_every_key_in_order_for_a_review() -> None:
    review = result(reviewed(2))

    document = build(review)

    assert list(document) == [
        "schema",
        "run_id",
        "mode",
        "origin",
        "outcome",
        "reason",
        "pr",
        "event_at",
        "started_at",
        "finished_at",
        "report",
        "summary_body",
        "comments",
        "withheld",
        "caps",
        "over_budget",
    ]
    assert document["schema"] == "shadow/v1"
    assert (document["mode"], document["origin"], document["pr"]) == ("shadow", "local", None)
    assert (document["outcome"], document["reason"]) == ("reviewed", None)
    assert document["report"] == report_document(review, SETTINGS, "run-1")
    assert document["caps"] == {
        "max_diff_bytes": 400_000,
        "max_review_bytes": None,
        "max_output_tokens": None,
        "max_retries": 2,
        "deadline_s": 300,
    }
    assert document["over_budget"] is False


def test_render_shadow_json_writes_utc_times_and_a_local_event_at_its_start() -> None:
    shadow = build_shadow_report(
        result(),
        settings=SETTINGS,
        run_id="run-1",
        started_at=STARTED.astimezone(timezone(timedelta(hours=2))),
        finished_at=STARTED + timedelta(seconds=2),
        origin=Origin.LOCAL,
        pr=None,
    )

    document = json.loads(render_shadow_json(shadow))

    assert document["event_at"] == document["started_at"] == "2026-11-08T10:00:00+00:00"
    assert document["finished_at"] == "2026-11-08T10:00:02+00:00"


def test_render_shadow_json_writes_each_comment_and_withheld_finding() -> None:
    finding = reviewed(1)
    secret = reviewed(2, rationale=f"Leaks {GITHUB_TOKEN}")

    document = build(result(finding, secret))

    (comment,) = document["comments"]
    assert {key: value for key, value in comment.items() if key != "body"} == {
        "action": "create",
        "fingerprint": finding.fingerprint,
        "rule_part": "ai:logic",
        "anchor": comment["anchor"],
        "source": "ai",
        "severity": "high",
        "file": "a.py",
        "line": 1,
    }
    assert comment["body"].endswith(f"anchor={comment['anchor']} -->")
    assert document["withheld"] == [
        {"fingerprint": secret.fingerprint, "file": "a.py", "line": 2, "reason": "possible_secret"}
    ]


def test_render_shadow_json_redacts_a_secret_in_the_embedded_report() -> None:
    text = render_shadow_json(
        build_shadow_report(
            result(reviewed(1, rationale=f"Leaks {GITHUB_TOKEN}")),
            settings=SETTINGS,
            run_id="run-1",
            started_at=STARTED,
            finished_at=STARTED,
            origin=Origin.LOCAL,
            pr=None,
        )
    )

    assert GITHUB_TOKEN not in text
    assert "Leaks [REDACTED]" in text


def test_build_shadow_report_skips_a_run_that_reviewed_nothing() -> None:
    document = build(result(model_called=False))

    assert (document["outcome"], document["reason"]) == ("skipped", "nothing_to_review")
    assert document["report"] is None


@pytest.mark.parametrize(
    ("error", "outcome", "reason"),
    [
        (DiffTooLargeError("x"), Outcome.SKIPPED, Reason.TOO_LARGE),
        (DiffEmptyError("x"), Outcome.SKIPPED, Reason.EMPTY),
        (DiffNotParsableError("x"), Outcome.SKIPPED, Reason.NOT_A_DIFF),
        (DiffNotUtf8Error("x"), Outcome.SKIPPED, Reason.UNREADABLE),
        (ProviderError(ErrorCategory.AUTH, "x"), Outcome.FAILED, ErrorCategory.AUTH),
    ],
)
def test_build_shadow_report_of_an_error_has_its_outcome_and_reason_and_no_review(
    error: ReportedError, outcome: Outcome, reason: Reason | ErrorCategory
) -> None:
    document = build(error)

    assert (document["outcome"], document["reason"]) == (outcome, reason)
    assert (document["report"], document["comments"], document["withheld"]) == (None, [], [])
    assert f"`{outcome}` (`{reason}`)" in document["summary_body"]


def test_build_shadow_report_of_a_secret_is_blocked_and_names_where_it_is() -> None:
    error = SecretDetectedError("x", hits=[SecretHit(SecretKind.AWS_ACCESS_KEY, 4)])

    document = build(error)

    assert (document["outcome"], document["reason"], document["report"]) == (
        "blocked_secret",
        None,
        None,
    )
    assert "- aws-access-key at diff line 4" in document["summary_body"]


def test_render_shadow_markdown_is_the_summary_then_each_comment_as_a_list_item() -> None:
    shadow = build_shadow_report(
        result(reviewed(1)),
        settings=SETTINGS,
        run_id="run-1",
        started_at=STARTED,
        finished_at=STARTED,
        origin=Origin.LOCAL,
        pr=None,
    )

    markdown = render_shadow_markdown(shadow)

    summary, item = markdown.split("\n\n", 1)
    assert summary == shadow.summary_body
    assert item.startswith("- `a.py:1`\n\n  **high** logic (source ai, confidence 0.50)\n\n")
    assert item.endswith(" -->")
    assert all(line.startswith("  ") for line in item.split("\n")[1:] if line)


def test_render_shadow_markdown_with_no_comments_is_the_summary() -> None:
    shadow = build_shadow_report(
        result(),
        settings=SETTINGS,
        run_id="run-1",
        started_at=STARTED,
        finished_at=STARTED,
        origin=Origin.LOCAL,
        pr=None,
    )

    assert render_shadow_markdown(shadow) == shadow.summary_body


def test_reported_errors_tuple_holds_exactly_the_alias_members() -> None:
    assert set(REPORTED_ERRORS) == set(get_args(ReportedError.__value__))
    assert len(REPORTED_ERRORS) == len(set(REPORTED_ERRORS))


PR = PullRequestRef(
    repo="owner/repo",
    number=7,
    base_sha="a" * 40,
    head_sha="b" * 40,
    is_fork=False,
    is_draft=True,
    event_at=datetime(2026, 11, 8, 9, 30, tzinfo=timezone(timedelta(hours=1))),
    action="opened",
)


def build_for_pr(
    review: PipelineResult | Reason, settings: Settings = SETTINGS, origin: Origin = Origin.EVENT
) -> dict[str, Any]:
    shadow = build_shadow_report(
        review,
        settings=settings,
        run_id="run-1",
        started_at=STARTED,
        finished_at=STARTED,
        origin=origin,
        pr=PR,
    )
    document: dict[str, Any] = json.loads(render_shadow_json(shadow))
    return document


def test_render_shadow_json_for_a_pull_request_has_its_origin_fields_and_event_time() -> None:
    document = build_for_pr(result())

    assert (document["mode"], document["origin"]) == ("shadow", "event")
    assert document["pr"] == {
        "repo": "owner/repo",
        "number": 7,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    assert document["event_at"] == "2026-11-08T08:30:00+00:00"
    assert document["started_at"] == "2026-11-08T10:00:00+00:00"


def test_render_shadow_json_for_a_backfill_has_its_origin_and_the_pull_requests_time() -> None:
    document = build_for_pr(result(), origin=Origin.BACKFILL)

    assert (document["origin"], document["event_at"]) == ("backfill", "2026-11-08T08:30:00+00:00")


@pytest.mark.parametrize(
    ("origin", "pr"), [(Origin.LOCAL, PR), (Origin.EVENT, None), (Origin.BACKFILL, None)]
)
def test_build_shadow_report_refuses_a_pull_request_that_doesnt_match_the_origin(
    origin: Origin, pr: PullRequestRef | None
) -> None:
    with pytest.raises(ValueError, match="exactly when its origin isn't local"):
        build_shadow_report(
            result(),
            settings=SETTINGS,
            run_id="run-1",
            started_at=STARTED,
            finished_at=STARTED,
            origin=origin,
            pr=pr,
        )


@pytest.mark.parametrize(
    "reason", [Reason.FORK, Reason.ACTION, Reason.NO_CREDENTIAL, Reason.MISSING_COMMIT]
)
def test_build_shadow_report_for_a_refused_run_is_skipped_with_its_reason(reason: Reason) -> None:
    document = build_for_pr(reason)

    assert (document["outcome"], document["reason"], document["report"]) == (
        "skipped",
        reason,
        None,
    )
    assert f"(`{reason}`)" in document["summary_body"]


@pytest.mark.parametrize(
    ("output_tokens", "cap", "over_budget"),
    [(16_001, 16_000, True), (16_000, 16_000, False), (20_000, None, False)],
)
def test_build_shadow_report_is_over_budget_only_past_the_output_token_cap(
    output_tokens: int, cap: int | None, over_budget: bool
) -> None:
    review = dataclasses.replace(result(), usage=TokenUsage(1, output_tokens))
    settings = Settings(provider="mock", max_output_tokens=cap)

    document = build_for_pr(review, settings)

    assert (document["outcome"], document["over_budget"]) == ("reviewed", over_budget)
    assert document["caps"]["max_output_tokens"] == cap
