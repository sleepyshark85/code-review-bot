"""Run metrics and precision from downloaded shadow reports and labels.

Pure: no I/O and no clock. The statistics come from ``pr.metrics``, and the unit rule and the
``run_id`` dedupe from ``pr.units`` (Phase 2 design, How the gate is evidenced);
``pr.metrics_report`` renders the result as ``metrics/v1``.
"""

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final

from review_bot.pr.feedback import (
    FeedbackLabel,
    FeedbackRecord,
    RejectReason,
    ShadowFeedback,
    SignalFeedback,
    Unit,
)
from review_bot.pr.github import in_window
from review_bot.pr.metrics import (
    Precision,
    Prices,
    Verdicts,
    nearest_rank,
    to_cents,
    token_cost_usd,
)
from review_bot.pr.outcome import Outcome
from review_bot.pr.units import ShadowEntry, ShadowFile, distinct_runs, would_be_comments

__all__ = ["OutcomeCount", "Percentiles", "RunMetrics", "TokenTotals", "summarise_runs"]

_SECONDS_PLACES: Final = 1
_MEAN_PLACES: Final = 1
# What a judged finding takes from the report that made it; precision is split by each.
_TRAITS: Final = ("origin", "source", "rule_part", "severity", "model", "prompt_version")

# A posted comment: its repo, PR and fingerprint, which reaction and resolution labels hold.
type _PostKey = tuple[str, int, str]
type _LabelKey = tuple[str, Unit] | tuple[str, str, int, str]
type _Traits = Mapping[str, str]


@dataclass(frozen=True, slots=True)
class OutcomeCount:
    """How many runs ended one way. ``reason`` is the report's text, so shown only escaped."""

    mode: str
    origin: str
    outcome: Outcome
    reason: str | None
    count: int


@dataclass(frozen=True, slots=True)
class Percentiles:
    """How many durations there are, and their nearest-rank p50 and p95 in seconds, or None."""

    runs: int
    p50: float | None
    p95: float | None


@dataclass(frozen=True, slots=True)
class TokenTotals:
    """The total, mean and largest of one kind of token count; mean and largest None if none."""

    total: int
    mean: float | None
    largest: int | None


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """What the runs and labels in a window show (``metrics/v1``).

    Runs count once per ``run_id``, and only if their ``event_at`` falls on a UTC day from
    ``since`` to ``until``, where None is no bound. End-to-end time covers the ``reviewed`` runs
    an event started, as a backfilled run's ``event_at`` is its merge time; review time, tokens
    and cost cover every ``reviewed`` run. ``cost_usd`` is None without prices, and
    ``cost_per_accepted_usd`` also when no shadow label accepts. ``shadow`` counts only
    ``shadow`` mode runs' would-be comments, and ``comment`` only the comments ``comment`` mode
    runs posted. ``orphans`` are the labels for a finding no report has, in or out of the
    window, left out of every number.
    """

    since: date | None
    until: date | None
    runs: int
    outcomes: tuple[OutcomeCount, ...]
    end_to_end_s: Percentiles
    review_s: Percentiles
    token_runs: int
    input_tokens: TokenTotals
    output_tokens: TokenTotals
    over_budget: int
    cost_usd: Decimal | None
    cost_per_accepted_usd: Decimal | None
    shadow: Precision
    comment: Precision
    orphans: tuple[FeedbackRecord, ...]


@dataclass(frozen=True, slots=True)
class _Judged:
    """One finding's latest accept or reject, with the traits of the report that made it."""

    pr: tuple[str, int]
    label: FeedbackLabel
    reason: RejectReason | None
    traits: _Traits


def summarise_runs(
    reports: Iterable[ShadowFile],
    labels: Iterable[FeedbackRecord],
    *,
    since: date | None,
    until: date | None,
    prices: Prices | None,
) -> RunMetrics:
    """Summarise the runs, and precision from the labels, as ``RunMetrics`` describes.

    Of the labels for one finding in one channel, the one with the latest ``recorded_at``
    counts, the later one given on a tie. A shadow label's finding is its would-be comment
    (``pr.units``), and a reaction or resolution label's the comment a ``comment`` mode run
    posted with its fingerprint. A finding's traits are the latest report's with it.
    """
    runs = distinct_runs(reports)
    window = [run for run in runs if in_window(run.event_at, since or date.min, until or date.max)]
    latest = _latest_labels(labels)
    reviewed = [run for run in window if run.outcome is Outcome.REVIEWED]
    from_events = [run for run in reviewed if run.origin == "event"]
    usages = [run.report.usage for run in reviewed if run.report is not None]
    input_tokens = [usage.input_tokens for usage in usages]
    output_tokens = [usage.output_tokens for usage in usages]
    would_be = _would_be(window)
    shadow = _judged_shadow(would_be, latest)
    posted = _posted(window)
    signals = Counter(
        record.label
        for record in latest
        if isinstance(record, SignalFeedback) and _post_key(record) in posted
    )
    cost = None if prices is None else token_cost_usd(sum(input_tokens), sum(output_tokens), prices)
    accepted = sum(1 for judged in shadow if judged.label is FeedbackLabel.ACCEPT)
    return RunMetrics(
        since=since,
        until=until,
        runs=len(window),
        outcomes=_outcomes(window),
        end_to_end_s=_percentiles([_seconds(run.event_at, run.finished_at) for run in from_events]),
        review_s=_percentiles([_seconds(run.started_at, run.finished_at) for run in reviewed]),
        token_runs=len(usages),
        input_tokens=_token_totals(input_tokens),
        output_tokens=_token_totals(output_tokens),
        over_budget=sum(1 for run in window if run.over_budget),
        cost_usd=None if cost is None else to_cents(cost),
        cost_per_accepted_usd=None if cost is None or not accepted else to_cents(cost / accepted),
        shadow=_precision(len(would_be), shadow, signals=Counter()),
        comment=_precision(len(posted), _judged_comments(posted, latest), signals=signals),
        orphans=_orphans(runs, latest),
    )


def _latest_labels(labels: Iterable[FeedbackRecord]) -> list[FeedbackRecord]:
    """The label with the latest ``recorded_at`` for each finding in each channel."""
    latest: dict[_LabelKey, FeedbackRecord] = {}
    for record in labels:
        key: _LabelKey
        if isinstance(record, ShadowFeedback):
            key = (record.channel, record.unit)
        else:
            key = (record.channel, *_post_key(record))
        kept = latest.get(key)
        if kept is None or record.recorded_at >= kept.recorded_at:
            latest[key] = record
    return list(latest.values())


def _would_be(runs: Sequence[ShadowFile]) -> dict[Unit, _Traits]:
    """Each would-be comment of the runs, with its traits."""
    origins = {run.run_id: run.origin for run in runs}
    return {
        comment.unit: _traits(
            comment.entry, origins[comment.run_id], comment.model, comment.prompt_version
        )
        for comment in would_be_comments(runs)
    }


def _posted(runs: Iterable[ShadowFile]) -> dict[_PostKey, _Traits]:
    """Each comment reviewed comment mode runs posted, with the latest such run's traits.

    A ``create`` entry whose ``applied`` isn't ``done`` never reached the pull request.
    """
    posted: dict[_PostKey, _Traits] = {}
    for run in sorted(runs, key=lambda run: run.finished_at):
        pr, review = run.pr, run.report
        if run.mode != "comment" or run.outcome is not Outcome.REVIEWED:
            continue
        if pr is None or review is None:
            continue
        for entry in run.comments:
            if entry.applied != "done":
                continue
            traits = _traits(entry, run.origin, review.model, review.prompt_version)
            posted[pr.repo, pr.number, entry.fingerprint] = traits
    return posted


def _judged_shadow(
    would_be: Mapping[Unit, _Traits], latest: Iterable[FeedbackRecord]
) -> list[_Judged]:
    """The would-be comments a shadow label judges."""
    return [
        _Judged((record.repo, record.pr), record.label, record.reason, would_be[record.unit])
        for record in latest
        if isinstance(record, ShadowFeedback) and record.unit in would_be
    ]


def _judged_comments(
    posted: Mapping[_PostKey, _Traits], latest: Iterable[FeedbackRecord]
) -> list[_Judged]:
    """The posted comments a reaction label accepts or rejects; ``conflict`` judges nothing."""
    return [
        _Judged((record.repo, record.pr), record.label, None, posted[_post_key(record)])
        for record in latest
        if record.channel == "reaction"
        and record.label in (FeedbackLabel.ACCEPT, FeedbackLabel.REJECT)
        and _post_key(record) in posted
    ]


def _orphans(
    runs: Sequence[ShadowFile], latest: Iterable[FeedbackRecord]
) -> tuple[FeedbackRecord, ...]:
    """The labels whose finding none of the runs has, whatever their window."""
    would_be, posted = _would_be(runs), _posted(runs)
    return tuple(
        record
        for record in latest
        if not (
            record.unit in would_be
            if isinstance(record, ShadowFeedback)
            else _post_key(record) in posted
        )
    )


def _post_key(record: FeedbackRecord) -> _PostKey:
    return record.repo, record.pr, record.fingerprint


def _traits(entry: ShadowEntry, origin: str, model: str, prompt_version: str) -> _Traits:
    return {
        "origin": origin,
        "source": entry.source,
        "rule_part": entry.rule_part,
        "severity": entry.severity,
        "model": model,
        "prompt_version": prompt_version,
    }


def _precision(
    units: int, judged: Sequence[_Judged], *, signals: Mapping[FeedbackLabel, int]
) -> Precision:
    """The precision of ``judged``, with ``signals`` counting the labels that never enter it."""
    per_pr = Counter(item.pr for item in judged if item.reason is not RejectReason.DUPLICATE)
    return Precision(
        units=units,
        verdicts=_verdicts(judged),
        pr_count=len(per_pr),
        largest_pr=max(per_pr.values(), default=0),
        splits={trait: _split(judged, trait) for trait in _TRAITS},
        reject_reasons=Counter(item.reason for item in judged if item.reason is not None),
        conflicts=signals.get(FeedbackLabel.CONFLICT, 0),
        resolved=signals.get(FeedbackLabel.RESOLVED, 0),
    )


def _split(judged: Iterable[_Judged], trait: str) -> dict[str, Verdicts]:
    groups: defaultdict[str, list[_Judged]] = defaultdict(list)
    for item in judged:
        groups[item.traits[trait]].append(item)
    return {value: _verdicts(groups[value]) for value in sorted(groups)}


def _verdicts(judged: Iterable[_Judged]) -> Verdicts:
    accepted = rejected = duplicates = 0
    for item in judged:
        if item.label is FeedbackLabel.ACCEPT:
            accepted += 1
        elif item.reason is RejectReason.DUPLICATE:
            duplicates += 1
        else:
            rejected += 1
    return Verdicts(accepted, rejected, duplicates)


def _outcomes(runs: Iterable[ShadowFile]) -> tuple[OutcomeCount, ...]:
    counts = Counter((run.mode, run.origin, run.outcome, run.reason) for run in runs)
    ordered = sorted(counts, key=lambda way: (*way[:3], way[3] or ""))
    return tuple(OutcomeCount(*way, count=counts[way]) for way in ordered)


def _seconds(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds()


def _percentiles(seconds: Sequence[float]) -> Percentiles:
    p50, p95 = nearest_rank(seconds, 50), nearest_rank(seconds, 95)
    return Percentiles(len(seconds), _rounded(p50, _SECONDS_PLACES), _rounded(p95, _SECONDS_PLACES))


def _token_totals(counts: Sequence[int]) -> TokenTotals:
    mean = statistics.fmean(counts) if counts else None
    return TokenTotals(sum(counts), _rounded(mean, _MEAN_PLACES), max(counts, default=None))


def _rounded(value: float | None, places: int) -> float | None:
    return None if value is None else round(value, places)
