"""Gate numbers, computed from data already read (Phase 2 design, How the gate is evidenced).

Pure: no I/O and no clock. P2-S11 adds the value baseline, and the statistics it shares with
P2-S10's run metrics.
"""

import json
import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

from review_bot.pr.github import MergedPullRequest, in_window

__all__ = [
    "BASELINE_SCHEMA",
    "Baseline",
    "Spread",
    "median",
    "nearest_rank",
    "render_baseline_json",
    "render_baseline_text",
    "summarise_baseline",
]

BASELINE_SCHEMA: Final = "baseline/v1"
# The escaped-defect proxy counts merged PRs whose title's first word is "fix" (design, Q8).
_FIX_TITLE: Final = re.compile(r"\s*fix\b", re.IGNORECASE)
_SECONDS_PER_HOUR: Final = 3600
_HOURS_PLACES: Final = 2
# A median of counts is a whole or a half.
_COMMITS_PLACES: Final = 1
_RATIO_PLACES: Final = 3


def median(values: Sequence[float]) -> float | None:
    """The median of ``values``, the mean of the middle two for an even count; None if empty."""
    return statistics.median(values) if values else None


def nearest_rank(values: Sequence[float], percent: int) -> float | None:
    """The ``percent`` percentile of ``values`` by nearest rank, or None if there are none.

    It is the smallest value with at least ``percent``% of the values at or below it, so it is
    always one of ``values``.

    Raises:
        ValueError: If ``percent`` isn't from 1 to 100.
    """
    if not 1 <= percent <= 100:  # noqa: PLR2004 - a percentage's bounds
        raise ValueError(f"A percentile is from 1 to 100, not {percent}.")
    if not values:
        return None
    rank = -(-percent * len(values) // 100)  # ceil(percent / 100 * n), in integers
    return sorted(values)[rank - 1]


@dataclass(frozen=True, slots=True)
class Spread:
    """The median and nearest-rank p90 of some values; both None when there are none."""

    median: float | None
    p90: float | None


def _spread(values: Sequence[float], *, places: int) -> Spread:
    """The spread of ``values``, each statistic a float rounded to ``places`` decimals."""
    middle, p90 = median(values), nearest_rank(values, 90)
    return Spread(
        None if middle is None else round(float(middle), places),
        None if p90 is None else round(float(p90), places),
    )


@dataclass(frozen=True, slots=True)
class Baseline:
    """How a repository's merged pull requests went in a window (``baseline/v1``).

    ``fix_prs`` counts those whose title's first word is ``fix``, in any case, and
    ``escaped_defects_per_merged_pr`` is ``(fix_prs + bug_issues) / pr_count``, or None with no
    pull requests. Hours are rounded to 2 decimals and the ratio to 3. No title is kept.
    """

    repo: str
    since: date
    until: date
    captured_at: datetime
    pr_count: int
    open_to_merge_hours: Spread
    commits_after_open: Spread
    fix_prs: int
    bug_issues: int
    escaped_defects_per_merged_pr: float | None


def summarise_baseline(  # noqa: PLR0913 - keyword-only facts of one capture
    pulls: Iterable[MergedPullRequest],
    *,
    repo: str,
    since: date,
    until: date,
    bug_issues: int,
    captured_at: datetime,
) -> Baseline:
    """Summarise the pull requests merged from ``since`` to ``until`` (UTC days, both included).

    Pull requests merged outside the window are left out, whoever listed them. A pull request's
    commits after opening are those whose committer date is later than its ``created_at``.
    """
    merged = [pull for pull in pulls if in_window(pull.merged_at, since, until)]
    hours = [
        (pull.merged_at - pull.created_at).total_seconds() / _SECONDS_PER_HOUR for pull in merged
    ]
    commits = [sum(1 for at in pull.commit_dates if at > pull.created_at) for pull in merged]
    fix_prs = sum(1 for pull in merged if _FIX_TITLE.match(pull.title))
    escaped = round((fix_prs + bug_issues) / len(merged), _RATIO_PLACES) if merged else None
    return Baseline(
        repo=repo,
        since=since,
        until=until,
        captured_at=captured_at,
        pr_count=len(merged),
        open_to_merge_hours=_spread(hours, places=_HOURS_PLACES),
        commits_after_open=_spread(commits, places=_COMMITS_PLACES),
        fix_prs=fix_prs,
        bug_issues=bug_issues,
        escaped_defects_per_merged_pr=escaped,
    )


def render_baseline_json(baseline: Baseline) -> str:
    """The ``baseline/v1`` document, as one line of ASCII JSON."""
    document = {
        "schema": BASELINE_SCHEMA,
        "repo": baseline.repo,
        "since": baseline.since.isoformat(),
        "until": baseline.until.isoformat(),
        "captured_at": _utc(baseline.captured_at),
        "pr_count": baseline.pr_count,
        "open_to_merge_hours": _spread_document(baseline.open_to_merge_hours),
        "commits_after_open": _spread_document(baseline.commits_after_open),
        "fix_prs": baseline.fix_prs,
        "bug_issues": baseline.bug_issues,
        "escaped_defects_per_merged_pr": baseline.escaped_defects_per_merged_pr,
    }
    return json.dumps(document, ensure_ascii=True, allow_nan=False)


def render_baseline_text(baseline: Baseline) -> str:
    """The same numbers as ``render_baseline_json``, as ``name=value`` lines for a person.

    Each value is written as the JSON document writes it, so a missing number reads ``null``.
    """
    hours, commits = baseline.open_to_merge_hours, baseline.commits_after_open
    lines = [
        f"repo={_shown(baseline.repo)} since={_shown(baseline.since.isoformat())} "
        f"until={_shown(baseline.until.isoformat())} "
        f"captured_at={_shown(_utc(baseline.captured_at))}",
        f"pr_count={baseline.pr_count}",
        f"open_to_merge_hours median={_shown(hours.median)} p90={_shown(hours.p90)}",
        f"commits_after_open median={_shown(commits.median)} p90={_shown(commits.p90)}",
        f"fix_prs={baseline.fix_prs} bug_issues={baseline.bug_issues} "
        f"escaped_defects_per_merged_pr={_shown(baseline.escaped_defects_per_merged_pr)}",
    ]
    return "\n".join(f"baseline: {line}" for line in lines)


def _spread_document(spread: Spread) -> dict[str, float | None]:
    return {"median": spread.median, "p90": spread.p90}


def _shown(value: str | float | None) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()
