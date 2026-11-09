import dataclasses
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, get_args

import pytest
from diff_samples import section

from review_bot.context.codeowners import (
    NO_CODEOWNERS,
    CodeOwners,
    CodeOwnersStatus,
    parse_codeowners,
)
from review_bot.diff_parser import parse_diff
from review_bot.errors import (
    DiffEmptyError,
    DiffNotParsableError,
    DiffNotUtf8Error,
    DiffTooLargeError,
    ErrorCategory,
    ProviderError,
    RepoConfigError,
    SecretDetectedError,
)
from review_bot.findings import Category, EvidenceSpan, Finding, ReviewedFinding, Severity, Source
from review_bot.fingerprint import anchor_for
from review_bot.policy import Decision
from review_bot.pr.config import DEFAULT_REPO_CONFIG, RepoConfig
from review_bot.pr.event import PullRequestRef
from review_bot.pr.github import BotComment
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.shadow import (
    REPORTED_ERRORS,
    Incremental,
    Mode,
    Origin,
    ReportedError,
    build_shadow_report,
    render_shadow_json,
    render_shadow_markdown,
)
from review_bot.prompt import PROMPT_VERSION
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
        prompt_version=PROMPT_VERSION,
    )


def build(
    review: PipelineResult | ReportedError, config: RepoConfig | None = DEFAULT_REPO_CONFIG
) -> dict[str, Any]:
    shadow = build_shadow_report(
        review,
        mode=Mode.SHADOW,
        existing=(),
        settings=SETTINGS,
        config=config,
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
        "scope",
        "since",
        "config",
        "codeowners",
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
    assert (document["scope"], document["since"]) == ("full", None)
    assert document["config"] == {
        "source": "default",
        "threshold": "low",
        "exclude": [],
        "max_open_comments": 10,
        "domain_rules": [],
    }


def test_render_shadow_json_shows_the_repository_config_with_the_threshold_used() -> None:
    config = RepoConfig(
        source=f".review-bot.toml@{'a' * 40}",
        threshold=Severity.MEDIUM,
        exclude=("*.snap",),
        max_open_comments=3,
    )

    # The settings' threshold is the one the review used, which a flag can set over the file's.
    document = build(result(reviewed(2)), config)

    assert document["config"] == {
        "source": f".review-bot.toml@{'a' * 40}",
        "threshold": "low",
        "exclude": ["*.snap"],
        "max_open_comments": 3,
        "domain_rules": [],
    }


def test_build_shadow_report_plans_up_to_the_repository_comment_limit() -> None:
    config = RepoConfig(source="default", max_open_comments=1)

    document = build(result(reviewed(1), reviewed(2)), config)

    assert [entry["line"] for entry in document["comments"]] == [1]
    assert [(entry["line"], entry["reason"]) for entry in document["withheld"]] == [
        (2, "over_limit")
    ]


def test_build_shadow_report_without_a_config_refuses_to_plan_comments() -> None:
    with pytest.raises(ValueError, match="config"):
        build(result(reviewed(1)), None)


def test_build_shadow_report_of_an_invalid_config_has_no_config_and_names_the_problem() -> None:
    error = RepoConfigError("unknown key 'max_retries'")

    document = build(error, None)

    assert (document["outcome"], document["reason"], document["config"]) == (
        "failed",
        "invalid_config",
        None,
    )
    assert r"max\_retries" in document["summary_body"]


def test_render_shadow_json_writes_utc_times_and_a_local_event_at_its_start() -> None:
    shadow = build_shadow_report(
        result(),
        mode=Mode.SHADOW,
        existing=(),
        settings=SETTINGS,
        config=DEFAULT_REPO_CONFIG,
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
        "applied": "not_attempted",
        "owners": [],
        "evidence": [],
        "evidence_dropped": 0,
        "suggestion": None,
    }
    assert comment["body"].endswith(f"anchor={comment['anchor']} -->")
    assert document["withheld"] == [
        {
            "fingerprint": secret.fingerprint,
            "file": "a.py",
            "line": 2,
            "reason": "possible_secret",
            "owners": [],
        }
    ]
    assert document["codeowners"] is None


def test_render_shadow_json_writes_each_comments_kept_evidence_and_dropped_count() -> None:
    plain = reviewed(1)
    evidence = (EvidenceSpan(file="a.py", start_line=1, end_line=2),)
    finding = dataclasses.replace(
        plain, finding=plain.finding.model_copy(update={"evidence": evidence}), evidence_dropped=1
    )

    (comment,) = build(result(finding))["comments"]

    assert comment["evidence"] == [{"file": "a.py", "start_line": 1, "end_line": 2}]
    assert comment["evidence_dropped"] == 1
    assert "Context: `a.py:1-2`" in comment["body"]


def test_render_shadow_json_redacts_a_secret_in_the_embedded_report() -> None:
    text = render_shadow_json(
        build_shadow_report(
            result(reviewed(1, rationale=f"Leaks {GITHUB_TOKEN}")),
            mode=Mode.SHADOW,
            existing=(),
            settings=SETTINGS,
            config=DEFAULT_REPO_CONFIG,
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
        (RepoConfigError("x"), Outcome.FAILED, Reason.INVALID_CONFIG),
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
        mode=Mode.SHADOW,
        existing=(),
        settings=SETTINGS,
        config=DEFAULT_REPO_CONFIG,
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


def with_suggestion(suggestion: str) -> ReviewedFinding:
    plain = reviewed(1)
    return dataclasses.replace(
        plain, finding=plain.finding.model_copy(update={"suggestion": suggestion})
    )


def test_render_shadow_json_writes_each_comments_kept_suggestion() -> None:
    (comment,) = build(result(with_suggestion("x = 2")))["comments"]

    assert comment["suggestion"] == "x = 2"
    assert "```suggestion\nx = 2\n```" in comment["body"]


def test_render_shadow_markdown_keeps_a_suggestion_block_inside_its_list_item() -> None:
    shadow = build_shadow_report(
        result(with_suggestion("    x = 2\n\n    y = 3")),
        mode=Mode.SHADOW,
        existing=(),
        settings=SETTINGS,
        config=DEFAULT_REPO_CONFIG,
        run_id="run-1",
        started_at=STARTED,
        finished_at=STARTED,
        origin=Origin.LOCAL,
        pr=None,
    )

    item = render_shadow_markdown(shadow).split("\n\n", 1)[1]

    assert "\n  ```suggestion\n      x = 2\n\n      y = 3\n  ```\n" in item


def test_render_shadow_markdown_with_no_comments_is_the_summary() -> None:
    shadow = build_shadow_report(
        result(),
        mode=Mode.SHADOW,
        existing=(),
        settings=SETTINGS,
        config=DEFAULT_REPO_CONFIG,
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
        mode=Mode.SHADOW,
        existing=(),
        settings=settings,
        config=DEFAULT_REPO_CONFIG,
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
            mode=Mode.SHADOW,
            existing=(),
            settings=SETTINGS,
            config=DEFAULT_REPO_CONFIG,
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


def bot_comment(comment_id: int, line: int | None, text: str) -> BotComment:
    return BotComment(
        comment_id=comment_id,
        thread_id=f"thread-{comment_id}",
        fingerprint="c" * 16,
        rule_part="ai:logic",
        anchor=anchor_for(text),
        path="a.py",
        line=line,
        severity=Severity.LOW,
        resolved_by=None,
    )


def build_in_comment_mode(
    *existing: BotComment,
    findings: tuple[ReviewedFinding, ...] = (reviewed(1),),
    codeowners: CodeOwners | None = None,
) -> dict[str, Any]:
    shadow = build_shadow_report(
        result(*findings),
        mode=Mode.COMMENT,
        existing=existing,
        settings=SETTINGS,
        config=DEFAULT_REPO_CONFIG,
        run_id="run-1",
        started_at=STARTED,
        finished_at=STARTED,
        origin=Origin.EVENT,
        pr=PR,
        codeowners=codeowners,
    )
    document: dict[str, Any] = json.loads(render_shadow_json(shadow))
    return document


def test_render_shadow_json_in_comment_mode_writes_paired_comments_then_resolves() -> None:
    document = build_in_comment_mode(bot_comment(4, 1, "first"), bot_comment(5, None, "gone"))

    kept, resolved = document["comments"]
    assert document["mode"] == "comment"
    assert (kept["action"], kept["fingerprint"], kept["applied"]) == (
        "update",
        "c" * 16,
        "not_attempted",
    )
    assert resolved == {
        "action": "resolve",
        "fingerprint": "c" * 16,
        "rule_part": "ai:logic",
        "anchor": anchor_for("gone"),
        "source": None,
        "severity": "low",
        "file": "a.py",
        "line": None,
        "body": None,
        "applied": "not_attempted",
        "owners": [],
        "evidence": None,
        "evidence_dropped": None,
        "suggestion": None,
    }


def test_build_shadow_report_in_shadow_mode_refuses_existing_comments() -> None:
    with pytest.raises(ValueError, match="reads no comments"):
        build_shadow_report(
            result(),
            mode=Mode.SHADOW,
            existing=(bot_comment(4, 1, "first"),),
            settings=SETTINGS,
            config=DEFAULT_REPO_CONFIG,
            run_id="run-1",
            started_at=STARTED,
            finished_at=STARTED,
            origin=Origin.EVENT,
            pr=PR,
        )


# Incremental runs (P2-S7).

SINCE = "c" * 40
# The whole PR diff; the reviewed diff, ``DIFF``, holds only its first two lines.
WHOLE_PR_DIFF = parse_diff(section("a.py", "+first\n+second\n+third\n"))


def changed_removing(text: str) -> Incremental:
    changed = parse_diff(
        f"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -5 +5 @@\n-{text}\n+x\n"
    )
    return Incremental(pr_diff=WHOLE_PR_DIFF, changed=changed)


def build_incremental(
    review: PipelineResult | Reason,
    *existing: BotComment,
    since: str | None = SINCE,
    incremental: Incremental | None = None,
    mode: Mode = Mode.COMMENT,
) -> dict[str, Any]:
    shadow = build_shadow_report(
        review,
        mode=mode,
        existing=existing,
        settings=SETTINGS,
        config=DEFAULT_REPO_CONFIG,
        run_id="run-1",
        started_at=STARTED,
        finished_at=STARTED,
        origin=Origin.EVENT,
        pr=PR,
        since=since,
        incremental=incremental,
    )
    document: dict[str, Any] = json.loads(render_shadow_json(shadow))
    return document


def test_an_incremental_review_plans_against_the_whole_pr_diff_and_records_its_start() -> None:
    document = build_incremental(result(reviewed(3)), incremental=changed_removing("gone"))

    (created,) = document["comments"]
    assert (created["action"], created["line"]) == ("create", 3)
    assert (document["scope"], document["since"]) == ("incremental", SINCE)


def test_no_new_changes_is_reviewed_and_only_resolves_comments_whose_lines_changed() -> None:
    changed = bot_comment(4, 9, "gone")
    unchanged = dataclasses.replace(bot_comment(5, 8, "untouched"), fingerprint="d" * 16)

    document = build_incremental(
        Reason.NO_NEW_CHANGES, changed, unchanged, incremental=changed_removing("gone")
    )

    assert (document["outcome"], document["reason"], document["report"]) == (
        "reviewed",
        "no_new_changes",
        None,
    )
    assert [(entry["action"], entry["line"]) for entry in document["comments"]] == [("resolve", 9)]
    assert document["summary_body"].endswith(
        "since `ccccccc`, so nothing was sent to the model. "
        "Open bot comments on this pull request: 1."
    )


def test_an_incremental_summary_names_its_start_and_counts_the_comments_left_open() -> None:
    kept = dataclasses.replace(bot_comment(5, 8, "untouched"), fingerprint="d" * 16)

    document = build_incremental(result(reviewed(3)), kept, incremental=changed_removing("gone"))

    assert document["summary_body"] == (
        "**Review bot:** `reviewed`. Changes since `ccccccc` reviewed. 1 comments, 0 withheld. "
        "Open bot comments on this pull request: 2."
    )


def test_a_full_comment_mode_summary_counts_comments_left_open_after_resolves() -> None:
    document = build_in_comment_mode(bot_comment(4, 1, "first"), bot_comment(5, None, "gone"))

    assert document["summary_body"] == (
        "**Review bot:** `reviewed`. 1 comments, 0 withheld. "
        "Open bot comments on this pull request: 1."
    )


def test_a_shadow_mode_summary_counts_no_open_comments() -> None:
    assert build(result(reviewed(2)))["summary_body"] == (
        "**Review bot:** `reviewed`. 1 comments, 0 withheld."
    )


def test_already_reviewed_is_a_skip_that_records_its_start() -> None:
    document = build_incremental(Reason.ALREADY_REVIEWED)

    assert (document["outcome"], document["reason"]) == ("skipped", "already_reviewed")
    assert (document["scope"], document["since"]) == ("incremental", SINCE)


@pytest.mark.parametrize(
    ("review", "since", "incremental", "mode", "message"),
    [
        (Reason.NO_NEW_CHANGES, SINCE, None, Mode.COMMENT, "Only an incremental run"),
        (result(), None, changed_removing("x"), Mode.COMMENT, "come with the commit"),
        (result(), SINCE, None, Mode.SHADOW, "reads no comments or summary"),
    ],
    ids=["no-new-changes-without-diffs", "diffs-without-since", "since-in-shadow-mode"],
)
def test_build_shadow_report_refuses_inconsistent_incremental_inputs(
    review: PipelineResult | Reason,
    since: str | None,
    incremental: Incremental | None,
    mode: Mode,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_incremental(review, since=since, incremental=incremental, mode=mode)


CODEOWNERS_SOURCE = f".github/CODEOWNERS@{'b' * 40}"
# A comment, and a finding withheld for the secret its rationale quotes.
COMMENTED_AND_WITHHELD = (reviewed(1), reviewed(2, rationale=f"Leaks {GITHUB_TOKEN}"))


def test_render_shadow_json_names_the_owners_of_every_entry_and_the_codeowners_read() -> None:
    codeowners = parse_codeowners(b"* @org/core\n!x @bad\n", source=CODEOWNERS_SOURCE)

    document = build_in_comment_mode(
        bot_comment(5, None, "gone"), findings=COMMENTED_AND_WITHHELD, codeowners=codeowners
    )

    assert [entry["owners"] for entry in document["comments"]] == [["@org/core"], ["@org/core"]]
    assert [entry["owners"] for entry in document["withheld"]] == [["@org/core"]]
    assert document["codeowners"] == {
        "source": CODEOWNERS_SOURCE,
        "status": "found",
        "rules": 1,
        "invalid_lines": 1,
    }
    assert document["summary_body"].endswith("Owners:\n\n- `@org/core`: 2")


@pytest.mark.parametrize(
    ("codeowners", "expected"),
    [
        (
            NO_CODEOWNERS,
            {"source": None, "status": "absent", "rules": 0, "invalid_lines": 0},
        ),
        (
            CodeOwners(source=CODEOWNERS_SOURCE, status=CodeOwnersStatus.NOT_A_FILE),
            {"source": CODEOWNERS_SOURCE, "status": "not_a_file", "rules": 0, "invalid_lines": 0},
        ),
    ],
    ids=["absent", "not-a-file"],
)
def test_render_shadow_json_without_usable_codeowners_names_no_owner(
    codeowners: CodeOwners, expected: dict[str, object]
) -> None:
    document = build_in_comment_mode(findings=COMMENTED_AND_WITHHELD, codeowners=codeowners)

    assert document["codeowners"] == expected
    assert [entry["owners"] for entry in (*document["comments"], *document["withheld"])] == [
        [],
        [],
    ]
    assert "Owners:" not in document["summary_body"]
