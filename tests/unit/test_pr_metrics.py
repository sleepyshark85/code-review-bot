import json
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from review_bot.pr.github import MergedPullRequest
from review_bot.pr.metrics import (
    Baseline,
    Spread,
    median,
    nearest_rank,
    render_baseline_json,
    render_baseline_text,
    summarise_baseline,
)

SINCE = date(2026, 10, 1)
UNTIL = date(2026, 10, 31)
OPENED = datetime(2026, 10, 10, 9, tzinfo=UTC)
CAPTURED = datetime(2026, 11, 8, 12, tzinfo=UTC)


def pull(
    title: str = "Add totals",
    *,
    hours: float = 1.0,
    opened: datetime = OPENED,
    commit_dates: tuple[datetime, ...] = (),
) -> MergedPullRequest:
    return MergedPullRequest(7, title, opened, opened + timedelta(hours=hours), commit_dates)


def summarise(pulls: list[MergedPullRequest], *, bug_issues: int = 0) -> Baseline:
    return summarise_baseline(
        pulls, repo="o/r", since=SINCE, until=UNTIL, bug_issues=bug_issues, captured_at=CAPTURED
    )


@pytest.mark.parametrize(
    ("values", "expected"), [([], None), ([3], 3), ([2, 5, 10, 30], 7.5), ([4, 1, 9], 4)]
)
def test_median_is_the_middle_value_or_the_mean_of_the_middle_two(
    values: list[float], expected: float | None
) -> None:
    assert median(values) == expected


@pytest.mark.parametrize(
    ("values", "percent", "expected"),
    [
        ([], 90, None),
        ([30, 2, 10, 5], 90, 30),
        ([60, 90, 120, 400], 50, 90),
        ([60, 90, 120, 400], 95, 400),
        ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90, 9),
        ([7], 1, 7),
        ([1, 2], 100, 2),
    ],
)
def test_nearest_rank_is_the_smallest_value_covering_the_percentile(
    values: list[float], percent: int, expected: float | None
) -> None:
    assert nearest_rank(values, percent) == expected


@pytest.mark.parametrize("percent", [0, 101, -5])
def test_nearest_rank_refuses_a_percentile_outside_1_to_100(percent: int) -> None:
    with pytest.raises(ValueError, match="from 1 to 100"):
        nearest_rank([1.0], percent)


def test_baseline_open_to_merge_hours_has_median_and_nearest_rank_p90() -> None:
    baseline = summarise([pull(hours=hours) for hours in (2, 5, 10, 30)])

    assert baseline.pr_count == 4
    assert baseline.open_to_merge_hours == Spread(7.5, 30.0)


def test_baseline_counts_only_commits_made_after_the_pr_opened() -> None:
    before, after = OPENED - timedelta(minutes=1), OPENED + timedelta(minutes=1)
    pulls = [
        pull(commit_dates=(before, after, after)),
        pull(commit_dates=(OPENED,)),
        pull(commit_dates=(after,) * 5),
    ]

    baseline = summarise(pulls)

    assert baseline.commits_after_open == Spread(2, 5)


def test_baseline_compares_commit_dates_across_time_zones() -> None:
    plus_two = timezone(timedelta(hours=2))
    # 10:30 at +02:00 is 08:30 UTC, before the 09:00 UTC opening.
    earlier = datetime(2026, 10, 10, 10, 30, tzinfo=plus_two)

    baseline = summarise([pull(commit_dates=(earlier,))])

    assert baseline.commits_after_open == Spread(0, 0)


@pytest.mark.parametrize(
    "title", ["Fix rounding", "fix: totals", "FIX the build", "  fix-up docs", "fix"]
)
def test_baseline_counts_a_title_whose_first_word_is_fix(title: str) -> None:
    assert summarise([pull(title)]).fix_prs == 1


@pytest.mark.parametrize(
    "title", ["Fixture cleanup", "Prefix tidy", "Fixes rounding", "fix_totals", "Hotfix", ""]
)
def test_baseline_ignores_fix_inside_or_after_the_first_word(title: str) -> None:
    assert summarise([pull(title)]).fix_prs == 0


def test_baseline_escaped_defects_per_merged_pr_adds_fix_prs_and_bug_issues() -> None:
    titles = ["Fix rounding", "fix: totals", "Fixture cleanup", "Prefix tidy"]

    baseline = summarise([pull(title) for title in titles], bug_issues=2)

    assert (baseline.fix_prs, baseline.bug_issues) == (2, 2)
    assert baseline.escaped_defects_per_merged_pr == 1.0


@pytest.mark.parametrize(
    "merged_at",
    [
        datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC),
    ],
)
def test_baseline_leaves_out_prs_merged_outside_the_window(merged_at: datetime) -> None:
    outside = MergedPullRequest(8, "Fix", merged_at - timedelta(hours=1), merged_at, ())

    baseline = summarise([pull(), outside])

    assert baseline.pr_count == 1
    assert baseline.fix_prs == 0


@pytest.mark.parametrize(
    "merged_at",
    [
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC),
        # 00:30 on 1 November at +02:00 is still 31 October in UTC.
        datetime(2026, 11, 1, 0, 30, tzinfo=timezone(timedelta(hours=2))),
    ],
)
def test_baseline_window_covers_both_end_days_in_utc(merged_at: datetime) -> None:
    edge = MergedPullRequest(8, "Edge", merged_at - timedelta(hours=1), merged_at, ())

    assert summarise([edge]).pr_count == 1


def test_baseline_of_an_empty_window_has_null_statistics() -> None:
    baseline = summarise([], bug_issues=3)

    assert baseline.pr_count == 0
    assert baseline.open_to_merge_hours == Spread(None, None)
    assert baseline.commits_after_open == Spread(None, None)
    assert baseline.escaped_defects_per_merged_pr is None
    assert baseline.bug_issues == 3


def test_baseline_rounds_hours_to_two_places_and_the_ratio_to_three() -> None:
    baseline = summarise([pull(hours=1 / 3)] * 3, bug_issues=1)

    assert baseline.open_to_merge_hours == Spread(0.33, 0.33)
    assert baseline.escaped_defects_per_merged_pr == 0.333


def test_baseline_json_is_one_ascii_line_with_every_field() -> None:
    baseline = summarise([pull("Fix rounding", hours=hours) for hours in (2, 5, 10, 30)])

    text = render_baseline_json(baseline)

    assert "\n" not in text
    assert text.isascii()
    assert json.loads(text) == {
        "schema": "baseline/v1",
        "repo": "o/r",
        "since": "2026-10-01",
        "until": "2026-10-31",
        "captured_at": "2026-11-08T12:00:00+00:00",
        "pr_count": 4,
        "open_to_merge_hours": {"median": 7.5, "p90": 30.0},
        "commits_after_open": {"median": 0.0, "p90": 0.0},
        "fix_prs": 4,
        "bug_issues": 0,
        "escaped_defects_per_merged_pr": 1.0,
    }


def test_baseline_json_writes_captured_at_in_utc() -> None:
    baseline = summarise_baseline(
        [],
        repo="o/r",
        since=SINCE,
        until=UNTIL,
        bug_issues=0,
        captured_at=datetime(2026, 11, 8, 14, tzinfo=timezone(timedelta(hours=2))),
    )

    assert json.loads(render_baseline_json(baseline))["captured_at"] == "2026-11-08T12:00:00+00:00"


@pytest.mark.parametrize("title", ["fix \x1b[31mCANARY-1\x07 " + chr(0x202E), "Fix CANARY-2"])
def test_baseline_json_and_text_never_hold_a_title(title: str) -> None:
    baseline = summarise([pull(title)])

    for rendered in (render_baseline_json(baseline), render_baseline_text(baseline)):
        assert "CANARY" not in rendered
        assert all(char.isprintable() or char == "\n" for char in rendered)
    assert baseline.fix_prs == 1


def test_baseline_text_shows_the_json_numbers_one_measure_per_line() -> None:
    baseline = summarise([pull(hours=hours) for hours in (2, 5, 10, 30)], bug_issues=2)

    assert render_baseline_text(baseline).splitlines() == [
        'baseline: repo="o/r" since="2026-10-01" until="2026-10-31" '
        'captured_at="2026-11-08T12:00:00+00:00"',
        "baseline: pr_count=4",
        "baseline: open_to_merge_hours median=7.5 p90=30.0",
        "baseline: commits_after_open median=0.0 p90=0.0",
        "baseline: fix_prs=0 bug_issues=2 escaped_defects_per_merged_pr=0.5",
    ]


def test_baseline_text_of_an_empty_window_shows_null() -> None:
    lines = render_baseline_text(summarise([])).splitlines()

    assert lines[2:] == [
        "baseline: open_to_merge_hours median=null p90=null",
        "baseline: commits_after_open median=null p90=null",
        "baseline: fix_prs=0 bug_issues=0 escaped_defects_per_merged_pr=null",
    ]
