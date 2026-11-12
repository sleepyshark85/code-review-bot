"""Phase 3 against Phase 2 on the labelled pull requests (ADR 0016; design, Q3).

Pure. Each candidate (context) report is paired with the baseline (Phase 2) report of the same
repo, PR and head SHA. On the paired reports, each side's would-be comments (``pr.units``) are
judged by the latest label for each, as Phase 2 measures precision; recall is relative to the
pool of accepted issues either side found. ``pr.compare_report`` renders the result.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Final, Literal

from review_bot.pr.feedback import (
    FeedbackLabel,
    FeedbackRecord,
    RejectReason,
    ShadowFeedback,
    Unit,
)
from review_bot.pr.metrics import Verdicts, ratio
from review_bot.pr.run_metrics import Percentiles, duration_percentiles
from review_bot.pr.units import ShadowFile, distinct_runs, reviewed_shadow, would_be_comments
from review_bot.price_flags import Prices, token_cost_usd

__all__ = [
    "COVERAGE_NEEDED",
    "Change",
    "ComparedPr",
    "Comparison",
    "Cost",
    "CostBasis",
    "ExcludedPr",
    "Exclusion",
    "Pool",
    "Side",
    "Verdict",
    "compare_runs",
]

type Change = Literal["higher", "same", "lower"]
type CostBasis = Literal["recorded", "estimated"]
# ``no_same_head``: a candidate PR with no baseline report at any of its heads.
# ``no_candidate``: a baseline PR with no candidate report.
type Exclusion = Literal["no_same_head", "no_candidate"]
# A pull request: its repo and number.
type _PrKey = tuple[str, int]
# An issue: accepted units on one anchored line of a PR's file, whatever their rule part.
type _IssueKey = tuple[str, int, str, str]

# Both sides must be judged to at least this share of their units for a verdict (design, Q3).
COVERAGE_NEEDED: Final = Fraction(9, 10)


@dataclass(frozen=True, slots=True)
class ComparedPr:
    """A pull request compared at one head SHA."""

    repo: str
    number: int
    head_sha: str


@dataclass(frozen=True, slots=True)
class ExcludedPr:
    """A pull request left out of the comparison, and why."""

    repo: str
    number: int
    reason: Exclusion


@dataclass(frozen=True, slots=True)
class Cost:
    """A side's cost, unrounded, and where it comes from.

    ``per_accepted_unit_usd`` is None when the side has no accepted unit.
    """

    basis: CostBasis
    total_usd: Decimal
    mean_per_pr_usd: Decimal
    per_accepted_unit_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class Side:
    """One side's figures over its reports of the compared pull requests, at their heads.

    ``units`` counts its would-be comments, ``verdicts`` their latest labels and
    ``reject_reasons`` the rejections by reason, duplicates included. ``issues`` counts the
    issues it has an accepted unit in. Token and tool call means are per compared PR, None with
    none; ``cost`` is None when not available. ``review_s`` is each report's review time.
    """

    units: int
    verdicts: Verdicts
    reject_reasons: Mapping[RejectReason, int]
    issues: int
    cost: Cost | None
    mean_input_tokens: float | None
    mean_output_tokens: float | None
    mean_tool_calls: float | None
    most_tool_calls: int
    review_s: Percentiles

    @property
    def unjudged(self) -> int:
        """The units no label judges."""
        return self.units - self.verdicts.judged

    @property
    def coverage(self) -> float | None:
        """Judged / units, duplicates being judged too; None with no units."""
        return ratio(self.verdicts.judged, self.units)


@dataclass(frozen=True, slots=True)
class Pool:
    """The accepted issues either side found: how many, and how many each found alone."""

    issues: int
    baseline_only: int
    candidate_only: int

    @property
    def both(self) -> int:
        """The issues both sides found."""
        return self.issues - self.baseline_only - self.candidate_only


@dataclass(frozen=True, slots=True)
class Verdict:
    """Q3 on the labelled PRs: the candidate's precision and recall against the baseline's.

    It ``is_decidable`` only when both sides are judged to ``COVERAGE_NEEDED``; otherwise both
    changes are None. A decidable change is None when either side has nothing to measure it on.
    """

    is_decidable: bool
    precision: Change | None
    recall: Change | None


@dataclass(frozen=True, slots=True)
class Comparison:
    """The pull requests compared and excluded, each side's figures, the pool and the verdict.

    Compared and excluded pull requests come in order of repo and number.
    """

    compared: tuple[ComparedPr, ...]
    excluded: tuple[ExcludedPr, ...]
    baseline: Side
    candidate: Side
    pool: Pool
    verdict: Verdict

    def recall(self, side: Side) -> float | None:
        """The side's issues / the pool's, or None with an empty pool."""
        return ratio(side.issues, self.pool.issues)


@dataclass(frozen=True, slots=True)
class _Run:
    """A reviewed shadow mode report of a pull request, with the figures compare reads."""

    report: ShadowFile
    pr: _PrKey
    head_sha: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None
    tool_calls: int
    review_s: float


@dataclass(frozen=True, slots=True)
class _Judged:
    """A side's units, the latest label of each judged one, and its accepted issues."""

    units: frozenset[Unit]
    labels: tuple[ShadowFeedback, ...]
    issues: frozenset[_IssueKey]


def compare_runs(
    baseline: Iterable[ShadowFile],
    candidate: Iterable[ShadowFile],
    labels: Iterable[FeedbackRecord],
    prices: Prices | None,
) -> Comparison:
    """Pair the candidate reports with the baseline's by head SHA, and compare the two sides.

    Only ``shadow`` reports with outcome ``reviewed`` and a pull request count, each ``run_id``
    once. A candidate PR is compared at the head of its latest report that a baseline report
    shares. Of the shadow labels for one unit, the latest ``recorded_at`` wins, the later one
    given on a tie; other labels, and labels for units of no compared report, are ignored.
    ``prices`` estimate the cost of a side in which some report has no reported cost.
    """
    baseline_runs, candidate_runs = _reviewed(baseline), _reviewed(candidate)
    compared, excluded = _pair(baseline_runs, candidate_runs)
    heads = {(pr.repo, pr.number): pr.head_sha for pr in compared}
    baseline_runs = [run for run in baseline_runs if heads.get(run.pr) == run.head_sha]
    candidate_runs = [run for run in candidate_runs if heads.get(run.pr) == run.head_sha]
    latest = _latest_labels(labels)
    baseline_judged = _judged(baseline_runs, latest)
    candidate_judged = _judged(candidate_runs, latest)
    pool = Pool(
        issues=len(baseline_judged.issues | candidate_judged.issues),
        baseline_only=len(baseline_judged.issues - candidate_judged.issues),
        candidate_only=len(candidate_judged.issues - baseline_judged.issues),
    )
    baseline_side = _side(baseline_runs, baseline_judged, len(compared), prices)
    candidate_side = _side(candidate_runs, candidate_judged, len(compared), prices)
    return Comparison(
        compared=compared,
        excluded=excluded,
        baseline=baseline_side,
        candidate=candidate_side,
        pool=pool,
        verdict=_verdict(baseline_side, candidate_side, pool),
    )


def _reviewed(reports: Iterable[ShadowFile]) -> list[_Run]:
    """The reviewed shadow mode reports of a pull request, each run once."""
    runs: list[_Run] = []
    for report in distinct_runs(reports):
        reviewed = reviewed_shadow(report)
        if reviewed is None:
            continue
        pr, review = reviewed
        usage, context = review.usage, review.context
        runs.append(
            _Run(
                report=report,
                pr=(pr.repo, pr.number),
                head_sha=pr.head_sha,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
                tool_calls=0 if context is None else context.tool_calls,
                review_s=(report.finished_at - report.started_at).total_seconds(),
            )
        )
    return runs


def _pair(
    baseline: Sequence[_Run], candidate: Sequence[_Run]
) -> tuple[tuple[ComparedPr, ...], tuple[ExcludedPr, ...]]:
    """The pull requests compared, each at one head SHA, and those excluded, with why."""
    baseline_heads: defaultdict[_PrKey, set[str]] = defaultdict(set)
    for run in baseline:
        baseline_heads[run.pr].add(run.head_sha)
    candidate_by_pr: defaultdict[_PrKey, list[_Run]] = defaultdict(list)
    for run in candidate:
        candidate_by_pr[run.pr].append(run)
    compared: list[ComparedPr] = []
    excluded: list[ExcludedPr] = []
    for (repo, number), runs in candidate_by_pr.items():
        shared = [run for run in runs if run.head_sha in baseline_heads.get((repo, number), ())]
        if shared:
            latest = max(shared, key=lambda run: run.report.finished_at)
            compared.append(ComparedPr(repo, number, latest.head_sha))
        else:
            excluded.append(ExcludedPr(repo, number, "no_same_head"))
    excluded += [
        ExcludedPr(repo, number, "no_candidate")
        for repo, number in baseline_heads
        if (repo, number) not in candidate_by_pr
    ]
    return (
        tuple(sorted(compared, key=lambda pr: (pr.repo, pr.number))),
        tuple(sorted(excluded, key=lambda pr: (pr.repo, pr.number))),
    )


def _latest_labels(labels: Iterable[FeedbackRecord]) -> dict[Unit, ShadowFeedback]:
    """The shadow label with the latest ``recorded_at`` for each unit, the later on a tie."""
    latest: dict[Unit, ShadowFeedback] = {}
    for record in labels:
        if not isinstance(record, ShadowFeedback):
            continue
        kept = latest.get(record.unit)
        if kept is None or record.recorded_at >= kept.recorded_at:
            latest[record.unit] = record
    return latest


def _judged(runs: Sequence[_Run], latest: Mapping[Unit, ShadowFeedback]) -> _Judged:
    """The runs' units, their latest labels, and the issues of the accepted ones."""
    units = frozenset(comment.unit for comment in would_be_comments(run.report for run in runs))
    labels = tuple(latest[unit] for unit in units if unit in latest)
    issues = frozenset(
        (label.repo, label.pr, label.file, label.anchor)
        for label in labels
        if label.label is FeedbackLabel.ACCEPT
    )
    return _Judged(units, labels, issues)


def _side(runs: Sequence[_Run], judged: _Judged, pr_count: int, prices: Prices | None) -> Side:
    """One side's figures; ``pr_count`` is the compared pull requests, the means' divisor."""
    reasons = Counter(label.reason for label in judged.labels if label.reason is not None)
    duplicates = reasons[RejectReason.DUPLICATE]
    accepted = sum(1 for label in judged.labels if label.label is FeedbackLabel.ACCEPT)
    tool_calls: Counter[_PrKey] = Counter()
    for run in runs:
        tool_calls[run.pr] += run.tool_calls
    return Side(
        units=len(judged.units),
        verdicts=Verdicts(accepted, reasons.total() - duplicates, duplicates),
        reject_reasons={reason: reasons[reason] for reason in RejectReason},
        issues=len(judged.issues),
        cost=_cost(runs, pr_count, accepted, prices),
        mean_input_tokens=ratio(sum(run.input_tokens for run in runs), pr_count),
        mean_output_tokens=ratio(sum(run.output_tokens for run in runs), pr_count),
        mean_tool_calls=ratio(tool_calls.total(), pr_count),
        most_tool_calls=max(tool_calls.values(), default=0),
        review_s=duration_percentiles([run.review_s for run in runs]),
    )


def _cost(runs: Sequence[_Run], pr_count: int, accepted: int, prices: Prices | None) -> Cost | None:
    """Reported costs if every run has one, else the tokens at ``prices``, else None."""
    if not runs or not pr_count:
        return None
    reported = [run.cost_usd for run in runs if run.cost_usd is not None]
    basis: CostBasis
    if len(reported) == len(runs):
        basis, total = "recorded", sum(reported, Decimal(0))
    elif prices is not None:
        input_tokens = sum(run.input_tokens for run in runs)
        output_tokens = sum(run.output_tokens for run in runs)
        basis, total = "estimated", token_cost_usd(input_tokens, output_tokens, prices)
    else:
        return None
    return Cost(basis, total, total / pr_count, total / accepted if accepted else None)


def _verdict(baseline: Side, candidate: Side, pool: Pool) -> Verdict:
    """Q3: each change, once both sides are judged to ``COVERAGE_NEEDED``."""
    if not (_is_covered(baseline) and _is_covered(candidate)):
        return Verdict(is_decidable=False, precision=None, recall=None)
    return Verdict(
        is_decidable=True,
        precision=_change(_precision(baseline), _precision(candidate)),
        recall=_change(_share(baseline.issues, pool.issues), _share(candidate.issues, pool.issues)),
    )


def _is_covered(side: Side) -> bool:
    """Whether the side's judged units are at least ``COVERAGE_NEEDED`` of them; true with none."""
    return side.verdicts.judged >= COVERAGE_NEEDED * side.units


def _precision(side: Side) -> Fraction | None:
    return _share(side.verdicts.accepted, side.verdicts.counted)


def _share(part: int, whole: int) -> Fraction | None:
    """``part / whole`` exactly, so equal shares compare equal; None when ``whole`` is 0."""
    return Fraction(part, whole) if whole else None


def _change(baseline: Fraction | None, candidate: Fraction | None) -> Change | None:
    if baseline is None or candidate is None:
        return None
    if candidate > baseline:
        return "higher"
    return "same" if candidate == baseline else "lower"
