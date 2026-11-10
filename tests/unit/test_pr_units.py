import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from shadow_samples import K1, K2, K3, entry, report

from review_bot.pr.cli import main
from review_bot.pr.feedback import Unit
from review_bot.pr.units import (
    ShadowFile,
    WouldBeComment,
    distinct_runs,
    reviewed_shadow,
    would_be_comments,
)
from review_bot.providers.mock import MockProvider


def parsed(document: dict[str, Any]) -> ShadowFile:
    return ShadowFile.model_validate_json(json.dumps(document), strict=True)


def unit(key: tuple[str, str, str], index: int = 0, pr: int = 7) -> Unit:
    return Unit("owner/repo", pr, *key, index)


def units(comments: tuple[WouldBeComment, ...]) -> list[Unit]:
    return [comment.unit for comment in comments]


# --- would_be_comments --------------------------------------------------------------------------


def test_would_be_comments_groups_one_prs_reports_by_key_counting_the_reports_with_each() -> None:
    first = report(entry(K1), entry(K2), finished_at="2026-11-08T10:00:00+00:00")
    second = report(entry(K1), entry(K2), finished_at="2026-11-08T11:00:00+00:00")
    third = report(entry(K1), entry(K3), finished_at="2026-11-08T12:00:00+00:00")

    comments = would_be_comments(map(parsed, (first, second, third)))

    assert {comment.unit: comment.report_count for comment in comments} == {
        unit(K1): 3,
        unit(K2): 2,
        unit(K3): 1,
    }


def test_would_be_comments_shows_each_as_the_report_finished_last_has_it_whatever_the_order() -> (
    None
):
    later = report(
        entry(K1, line=12, fingerprint="2" * 16, body="reworded"),
        finished_at="2026-11-08T12:00:00+00:00",
        run_id="later",
        report={
            "model": "claude-new",
            "prompt_version": "p1-v2",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    earlier = report(entry(K1, line=10, fingerprint="1" * 16, body="first"), run_id="earlier")

    (comment,) = would_be_comments([parsed(later), parsed(earlier)])

    assert (comment.entry.line, comment.entry.fingerprint, comment.entry.body) == (
        12,
        "2" * 16,
        "reworded",
    )
    assert (comment.run_id, comment.model, comment.prompt_version) == (
        "later",
        "claude-new",
        "p1-v2",
    )
    assert comment.report_count == 2


def test_would_be_comments_indexes_one_reports_entries_for_a_key_in_line_order() -> None:
    two = report(entry(K1, line=30, body="lower"), entry(K1, line=5, body="upper"))
    one = report(entry(K1, line=5, body="only"), finished_at="2026-11-08T09:00:00+00:00")

    comments = would_be_comments([parsed(two), parsed(one)])

    assert [(c.unit.index, c.entry.body, c.report_count) for c in comments] == [
        (0, "upper", 2),
        (1, "lower", 1),
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"mode": "comment"},
        {"outcome": "skipped", "report": None},
        {"outcome": "failed"},
        {"origin": "local", "pr": None},
    ],
)
def test_would_be_comments_ignores_reports_that_are_not_reviewed_pr_shadow_runs(
    fields: dict[str, Any],
) -> None:
    assert would_be_comments([parsed(report(entry(K1), **fields))]) == ()


def test_reviewed_shadow_gives_the_pr_and_review_of_a_reviewed_pr_shadow_run() -> None:
    shadow = parsed(report(entry(K1)))

    assert reviewed_shadow(shadow) == (shadow.pr, shadow.report)


@pytest.mark.parametrize(
    "fields",
    [
        {"mode": "comment"},
        {"outcome": "skipped", "report": None},
        {"outcome": "failed"},
        {"origin": "local", "pr": None},
    ],
)
def test_reviewed_shadow_is_none_for_any_other_report(fields: dict[str, Any]) -> None:
    assert reviewed_shadow(parsed(report(entry(K1), **fields))) is None


def test_would_be_comments_ignores_entries_whose_action_is_not_create() -> None:
    document = report(entry(K1, action="update"), entry(K2))

    assert units(would_be_comments([parsed(document)])) == [unit(K2)]


def test_would_be_comments_counts_backfilled_reports_like_event_ones() -> None:
    reports = [report(entry(K1), origin="backfill"), report(entry(K1))]

    (comment,) = would_be_comments(map(parsed, reports))

    assert comment.report_count == 2


def test_would_be_comments_come_in_order_of_pr_file_and_line() -> None:
    document = report(entry(K3, line=1), entry(K2, line=9), entry(K1, line=20))
    other_pr = report(entry(K1, line=1), number=3)

    comments = would_be_comments([parsed(document), parsed(other_pr)])

    assert units(comments) == [unit(K1, pr=3), unit(K2), unit(K1), unit(K3)]


def test_shadow_file_reads_a_comment_mode_report_keeping_only_its_create_entries() -> None:
    resolve = entry(K3, action="resolve") | {"source": None, "body": None, "line": None}
    document = report(entry(K1), entry(K2, action="keep"), resolve, mode="comment")

    shadow = parsed(document)

    assert [item.action for item in shadow.comments] == ["create"]


def test_shadow_file_needs_the_report_object_of_a_reviewed_report() -> None:
    with pytest.raises(ValueError, match="report/v1"):
        parsed(report(entry(K1), report=None))


def test_shadow_file_parses_a_report_the_bot_writes(tmp_path: Path) -> None:
    diff = tmp_path / "change.diff"
    diff.write_text("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+x = 1\n")
    out = tmp_path / "out"
    assert main(["shadow", str(diff), "--out", str(out)], provider=MockProvider(findings=[])) == 0

    shadow = ShadowFile.model_validate_json((out / "shadow.json").read_bytes(), strict=True)

    assert (shadow.origin, shadow.outcome) == ("local", "reviewed")


def test_would_be_comments_counts_a_report_given_twice_once() -> None:
    document = report(entry(K1), run_id="same")

    (comment,) = would_be_comments([parsed(document), parsed(document)])

    assert (comment.unit.index, comment.report_count) == (0, 1)


def test_would_be_comments_keeps_the_same_key_on_two_prs_apart() -> None:
    reports = [report(entry(K1), number=7), report(entry(K1), number=8)]

    assert units(would_be_comments(map(parsed, reports))) == [unit(K1, pr=7), unit(K1, pr=8)]


def test_distinct_runs_keeps_the_first_report_of_each_run_in_the_order_given() -> None:
    first, second = parsed(report(run_id="a", number=1)), parsed(report(run_id="b"))
    again = parsed(report(run_id="a", number=2))

    assert distinct_runs([first, second, again]) == [first, second]


def test_shadow_file_reads_the_head_sha_reported_cost_and_tool_calls() -> None:
    review = {
        "model": "claude-x",
        "prompt_version": "p3-v1",
        "usage": {"input_tokens": 1, "output_tokens": 2, "cost_usd": "0.0123"},
        "context": {"commit": "2" * 40, "tool_calls": 4},
    }

    shadow = parsed(report(report=review))

    assert shadow.pr is not None
    assert shadow.report is not None
    assert shadow.pr.head_sha == "1" * 40
    assert shadow.report.usage.cost_usd == Decimal("0.0123")
    assert shadow.report.context is not None
    assert shadow.report.context.tool_calls == 4


def test_shadow_file_of_a_phase_2_report_has_no_cost_or_tool_calls() -> None:
    shadow = parsed(report())

    assert shadow.report is not None
    assert (shadow.report.usage.cost_usd, shadow.report.context) == (None, None)


@pytest.mark.parametrize("cost_usd", ["-0.01", "NaN", "1000001"])
def test_shadow_file_refuses_a_reported_cost_out_of_range(cost_usd: str) -> None:
    review = {
        "model": "claude-x",
        "prompt_version": "p3-v1",
        "usage": {"input_tokens": 1, "output_tokens": 2, "cost_usd": cost_usd},
    }

    with pytest.raises(ValueError, match="cost_usd"):
        parsed(report(report=review))


def test_shadow_file_needs_a_head_sha_of_40_hex_digits() -> None:
    document = report()
    document["pr"]["head_sha"] = "HEAD"

    with pytest.raises(ValueError, match="head_sha"):
        parsed(document)
