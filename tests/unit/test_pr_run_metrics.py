from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from metrics_samples import (
    PRICES,
    gate_sample,
    key,
    posted,
    shadow_label,
    signal_label,
    summarise,
    timed_report,
    usage,
)
from shadow_samples import entry, report

from review_bot.pr.feedback import FeedbackLabel, FeedbackRecord, RejectReason
from review_bot.pr.metrics import Prices
from review_bot.pr.outcome import Outcome
from review_bot.pr.run_metrics import OutcomeCount, Percentiles, TokenTotals

# --- Runs: outcomes, latency, tokens and cost ---------------------------------------------------


def test_outcomes_are_counted_per_mode_origin_outcome_and_reason() -> None:
    reports = [
        *(report() for _ in range(4)),
        report(outcome="skipped", reason="no_credential", report=None),
        report(outcome="blocked_secret", report=None),
        report(outcome="failed", reason="timeout", report=None, origin="backfill"),
    ]

    metrics = summarise(reports)

    assert metrics.runs == 7
    assert metrics.outcomes == (
        OutcomeCount("shadow", "backfill", Outcome.FAILED, "timeout", 1),
        OutcomeCount("shadow", "event", Outcome.BLOCKED_SECRET, None, 1),
        OutcomeCount("shadow", "event", Outcome.REVIEWED, None, 4),
        OutcomeCount("shadow", "event", Outcome.SKIPPED, "no_credential", 1),
    )


def test_end_to_end_latency_is_nearest_rank_over_reviewed_event_runs_only() -> None:
    reports = [timed_report(seconds, 30) for seconds in (60, 90, 120, 400)]
    reports.append(timed_report(9_000, 50, origin="backfill"))
    reports.append(timed_report(5_000, 70, outcome="skipped", reason="empty", report=None))

    metrics = summarise(reports)

    assert metrics.end_to_end_s == Percentiles(4, 90.0, 400.0)


def test_review_time_covers_reviewed_runs_of_every_origin() -> None:
    reports = [timed_report(100, seconds) for seconds in (10, 20, 30)]
    reports.append(timed_report(100, 50, origin="backfill"))

    assert summarise(reports).review_s == Percentiles(4, 20.0, 50.0)


def test_latency_with_no_reviewed_run_is_none() -> None:
    metrics = summarise([report(outcome="skipped", reason="empty", report=None)])

    assert (metrics.end_to_end_s, metrics.review_s) == (
        Percentiles(0, None, None),
        Percentiles(0, None, None),
    )


def test_a_run_given_twice_is_counted_once() -> None:
    twice = report()

    metrics = summarise([twice, twice])

    assert (metrics.runs, metrics.token_runs) == (1, 1)


def test_tokens_have_total_mean_and_maximum_with_over_budget_runs_counted() -> None:
    reports = [
        report(report=usage(1000, 100)),
        report(report=usage(2000, 900), over_budget=True),
        report(report=usage(4000, 200)),
    ]

    metrics = summarise(reports)

    assert metrics.input_tokens == TokenTotals(7000, 2333.3, 4000)
    assert metrics.output_tokens == TokenTotals(1200, 400.0, 900)
    assert metrics.over_budget == 1


def test_tokens_with_no_reviewed_run_have_no_mean_or_maximum() -> None:
    metrics = summarise([])

    assert metrics.input_tokens == TokenTotals(0, None, None)


def test_cost_at_15_and_75_usd_is_22_50_and_2_50_per_each_of_9_accepted_findings() -> None:
    reports = [report(*(entry(key(number)) for number in range(9)), report=usage(1_000_000, 0))]
    reports.append(report(report=usage(0, 100_000), number=8))
    labels: list[FeedbackRecord] = [shadow_label(key(number)) for number in range(9)]

    metrics = summarise(reports, labels, prices=PRICES)

    assert (metrics.cost_usd, metrics.cost_per_accepted_usd) == (Decimal("22.50"), Decimal("2.50"))


def test_cost_without_prices_is_none() -> None:
    metrics = summarise([report()])

    assert (metrics.cost_usd, metrics.cost_per_accepted_usd) == (None, None)


def test_cost_per_accepted_finding_divides_the_unrounded_cost_then_rounds() -> None:
    # 0.0549 USD / 2 is 0.02745, so 0.03; rounding the total to 0.05 first would give 0.02.
    reports = [report(entry(key(1)), entry(key(2)), report=usage(54_900, 0))]
    labels: list[FeedbackRecord] = [shadow_label(key(1)), shadow_label(key(2))]

    metrics = summarise(reports, labels, prices=Prices(Decimal(1), Decimal(1)))

    assert (metrics.cost_usd, metrics.cost_per_accepted_usd) == (Decimal("0.05"), Decimal("0.03"))


def test_cost_per_accepted_finding_with_none_accepted_is_none() -> None:
    metrics = summarise([report()], prices=PRICES)

    assert metrics.cost_usd == Decimal("0.02")
    assert metrics.cost_per_accepted_usd is None


# --- Shadow precision ---------------------------------------------------------------------------


def test_shadow_precision_of_the_gate_sample() -> None:
    reports, labels = gate_sample()

    shadow = summarise(reports, labels).shadow

    assert (shadow.units, shadow.verdicts.judged) == (34, 32)
    assert (shadow.verdicts.accepted, shadow.verdicts.rejected) == (21, 9)
    assert shadow.verdicts.duplicates == 2
    assert shadow.verdicts.precision == 0.7
    assert shadow.verdicts.lower_bound == 21 / 32
    assert shadow.coverage == 32 / 34


def test_shadow_precision_counts_prs_and_the_largest_prs_share_without_duplicates() -> None:
    reports, labels = gate_sample()

    shadow = summarise(reports, labels).shadow

    # PRs 1, 2 and 3 have 10 each in the precision; PR 4 has only the 2 duplicates.
    assert (shadow.pr_count, shadow.largest_pr) == (3, 10)
    assert shadow.largest_pr_share == 10 / 30


def test_shadow_precision_splits_by_trait_and_reject_reason() -> None:
    reports = [
        report(entry(key(1)), entry(key(2, "swallowed-exception")), origin="backfill"),
    ]
    labels: list[FeedbackRecord] = [
        shadow_label(key(1)),
        shadow_label(key(2, "swallowed-exception"), FeedbackLabel.REJECT, RejectReason.WRONG_LINE),
    ]

    shadow = summarise(reports, labels).shadow

    assert shadow.splits["origin"]["backfill"].judged == 2
    assert shadow.splits["rule_part"]["ai:logic"].accepted == 1
    assert shadow.splits["rule_part"]["swallowed-exception"].rejected == 1
    assert shadow.reject_reasons == {RejectReason.WRONG_LINE: 1}


def test_comment_mode_reports_add_no_would_be_comments() -> None:
    reports = [report(entry(key(1)), mode="comment")]

    shadow = summarise(reports, [shadow_label(key(1))]).shadow

    assert (shadow.units, shadow.verdicts.judged) == (0, 0)


def test_the_latest_of_two_shadow_labels_for_one_would_be_comment_counts() -> None:
    later = shadow_label(key(1), recorded_at=datetime(2026, 11, 9, tzinfo=UTC))
    earlier = shadow_label(key(1), FeedbackLabel.REJECT, RejectReason.WRONG)

    shadow = summarise([report(entry(key(1)))], [later, earlier]).shadow

    assert (shadow.verdicts.accepted, shadow.verdicts.rejected) == (1, 0)


def test_with_no_judged_labels_precision_is_none() -> None:
    shadow = summarise([report(entry(key(1)))]).shadow

    assert shadow.verdicts.precision is None
    assert shadow.coverage == 0.0
    assert shadow.largest_pr_share is None


def test_the_window_keeps_runs_by_event_day_and_only_their_would_be_comments() -> None:
    inside = report(entry(key(1)), finished_at="2026-11-05T23:59:59+00:00", number=1)
    outside = report(entry(key(2)), finished_at="2026-11-06T00:00:00+00:00", number=2)
    labels: list[FeedbackRecord] = [shadow_label(key(1), pr=1), shadow_label(key(2), pr=2)]

    metrics = summarise([inside, outside], labels, since=date(2026, 11, 1), until=date(2026, 11, 5))

    assert (metrics.runs, metrics.shadow.units, metrics.shadow.verdicts.judged) == (1, 1, 1)
    assert metrics.orphans == ()


def test_the_window_compares_event_days_in_utc() -> None:
    # 00:30 at +02:00 on 1 November is 22:30 UTC on 31 October.
    before = report(entry(key(1)), event_at="2026-11-01T00:30:00+02:00")

    metrics = summarise([before], since=date(2026, 11, 1))

    assert (metrics.runs, metrics.shadow.units) == (0, 0)


def test_a_label_no_report_has_is_an_orphan_and_counts_nowhere() -> None:
    orphan = shadow_label(key(9))
    reaction = signal_label("e" * 16)

    metrics = summarise([report(entry(key(1)))], [orphan, reaction])

    assert metrics.orphans == (orphan, reaction)
    assert metrics.shadow.verdicts.judged == metrics.comment.verdicts.judged == 0


# --- Comment precision --------------------------------------------------------------------------


def test_a_reaction_takes_its_traits_from_the_comment_mode_report_that_created_it() -> None:
    created = report(
        posted(1),
        posted(2),
        mode="comment",
        report={**usage(1, 1), "model": "claude-new", "prompt_version": "p1-v2"},
    )
    labels: list[FeedbackRecord] = [
        signal_label("1" * 16),
        signal_label("2" * 16, FeedbackLabel.CONFLICT),
        signal_label("2" * 16, FeedbackLabel.RESOLVED, channel="resolution"),
    ]

    comment = summarise([created], labels).comment

    assert (comment.units, comment.verdicts.accepted, comment.verdicts.judged) == (2, 1, 1)
    assert comment.splits["model"] == {"claude-new": comment.verdicts}
    assert comment.splits["prompt_version"]["p1-v2"].accepted == 1
    assert comment.splits["severity"]["medium"].accepted == 1
    assert (comment.conflicts, comment.resolved) == (1, 1)
    assert comment.coverage == 0.5


@pytest.mark.parametrize("applied", ["failed", "not_attempted"])
def test_a_create_that_wasnt_posted_is_no_comment_and_its_reaction_an_orphan(applied: str) -> None:
    reports = [report(posted(1), posted(2, applied), mode="comment")]
    reaction = signal_label("2" * 16)

    metrics = summarise(reports, [signal_label("1" * 16), reaction])

    assert (metrics.comment.units, metrics.comment.verdicts.judged) == (1, 1)
    assert metrics.orphans == (reaction,)


def test_the_latest_of_two_reaction_labels_for_one_fingerprint_counts() -> None:
    created = report(posted(1), mode="comment")
    labels: list[FeedbackRecord] = [
        signal_label("1" * 16, FeedbackLabel.REJECT, recorded_at=datetime(2026, 11, 9, tzinfo=UTC)),
        signal_label("1" * 16),
    ]

    comment = summarise([created], labels).comment

    assert (comment.verdicts.accepted, comment.verdicts.rejected) == (0, 1)


def test_shadow_labels_never_enter_comment_precision() -> None:
    reports = [report(entry(key(1))), report(posted(2), mode="comment")]
    labels: list[FeedbackRecord] = [shadow_label(key(1)), signal_label("2" * 16)]

    metrics = summarise(reports, labels)

    assert metrics.shadow.verdicts.judged == metrics.comment.verdicts.judged == 1
