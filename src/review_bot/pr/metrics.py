"""Gate numbers, computed from data already read (Phase 2 design, How the gate is evidenced).

Pure: no I/O and no clock. P2-S11 adds the value baseline and its percentiles; P2-S10 the
statistics its run metrics share (``pr.run_metrics``): precision with its Wilson interval,
coverage and cost.
"""

import json
import math
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, getcontext
from typing import Final

from review_bot.pr.feedback import RejectReason
from review_bot.pr.github import MergedPullRequest, in_window

__all__ = [
    "BASELINE_SCHEMA",
    "Baseline",
    "Precision",
    "Prices",
    "Spread",
    "Verdicts",
    "median",
    "nearest_rank",
    "ratio",
    "render_baseline_json",
    "render_baseline_text",
    "summarise_baseline",
    "to_cents",
    "token_cost_usd",
    "wilson_interval",
]

BASELINE_SCHEMA: Final = "baseline/v1"
# The escaped-defect proxy counts merged PRs whose title's first word is "fix" (design, Q8).
_FIX_TITLE: Final = re.compile(r"\s*fix\b", re.IGNORECASE)
_SECONDS_PER_HOUR: Final = 3600
_HOURS_PLACES: Final = 2
# A median of counts is a whole or a half.
_COMMITS_PLACES: Final = 1
_RATIO_PLACES: Final = 3
# Prices are per million tokens.
_TOKENS_PER_PRICE: Final = Decimal(1_000_000)
_CENT: Final = Decimal("0.01")
# The normal quantile for a two-sided 95% interval, about 1.96.
_Z_95: Final = statistics.NormalDist().inv_cdf(0.975)


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


def ratio(part: int, whole: int) -> float | None:
    """``part / whole``, or None when ``whole`` is 0: a precision, a coverage or a share."""
    return part / whole if whole else None


def wilson_interval(successes: int, trials: int) -> tuple[float, float] | None:
    """The Wilson score 95% interval of ``successes / trials``, or None with no trials.

    Raises:
        ValueError: If ``successes`` isn't from 0 to ``trials``.
    """
    if not 0 <= successes <= trials:
        raise ValueError(f"{successes} successes don't fit in {trials} trials.")
    if not trials:
        return None
    observed, z_squared = successes / trials, _Z_95**2
    scale = 1 + z_squared / trials
    centre = (observed + z_squared / (2 * trials)) / scale
    spread = observed * (1 - observed) / trials + z_squared / (4 * trials**2)
    half_width = _Z_95 * math.sqrt(spread) / scale
    # Clamped, as float rounding can take an end a hair past 0 or 1.
    return max(0.0, centre - half_width), min(1.0, centre + half_width)


@dataclass(frozen=True, slots=True)
class Prices:
    """The user's token prices, in USD per million tokens; never stored in code (design)."""

    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal


def token_cost_usd(input_tokens: int, output_tokens: int, prices: Prices) -> Decimal:
    """What the tokens cost at ``prices``, before rounding to cents."""
    spent = input_tokens * prices.input_usd_per_mtok + output_tokens * prices.output_usd_per_mtok
    return spent / _TOKENS_PER_PRICE


def to_cents(amount: Decimal) -> Decimal:
    """``amount`` rounded half-even to cents."""
    # Enough digits for every whole dollar, so a large amount can't overflow the precision.
    digits = max(getcontext().prec, amount.adjusted() + 3)
    return amount.quantize(_CENT, rounding=ROUND_HALF_EVEN, context=Context(prec=digits))


@dataclass(frozen=True, slots=True)
class Verdicts:
    """Findings accepted, rejected for a reason other than ``duplicate``, and duplicates."""

    accepted: int
    rejected: int
    duplicates: int

    @property
    def judged(self) -> int:
        """Every finding with a verdict, duplicates included."""
        return self.accepted + self.rejected + self.duplicates

    @property
    def counted(self) -> int:
        """The findings precision counts: duplicates are left out."""
        return self.accepted + self.rejected

    @property
    def precision(self) -> float | None:
        """Accepted / (accepted + rejected other than duplicate); None with none of those."""
        return ratio(self.accepted, self.counted)

    @property
    def wilson_95(self) -> tuple[float, float] | None:
        """The Wilson 95% interval of ``precision``; optimistic, as a PR's comments correlate."""
        return wilson_interval(self.accepted, self.counted)

    @property
    def lower_bound(self) -> float | None:
        """Precision with duplicates counted as rejections."""
        return ratio(self.accepted, self.judged)


@dataclass(frozen=True, slots=True)
class Precision:
    """How one channel's findings were judged, by the latest label for each.

    ``units`` counts the findings there were to judge: would-be comments, or posted comments.
    ``pr_count`` counts the PRs with a finding in the precision, and ``largest_pr`` is how many
    of those findings the PR with the most has. ``splits`` maps each trait, such as ``source``,
    to the verdicts for each of its values, in value order. ``conflicts`` and ``resolved`` count
    the findings with those labels, which never enter precision; only comments have them.
    """

    units: int
    verdicts: Verdicts
    pr_count: int
    largest_pr: int
    splits: Mapping[str, Mapping[str, Verdicts]]
    reject_reasons: Mapping[RejectReason, int]
    conflicts: int
    resolved: int

    @property
    def coverage(self) -> float | None:
        """Judged / units, duplicates being judged too."""
        return ratio(self.verdicts.judged, self.units)

    @property
    def largest_pr_share(self) -> float | None:
        """The largest PR's part of the findings in the precision."""
        return ratio(self.largest_pr, self.verdicts.counted)
