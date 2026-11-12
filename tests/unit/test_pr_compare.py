from datetime import timedelta
from decimal import Decimal
from typing import Any

from compare_samples import HEAD_A, HEAD_B, HEAD_C, pr_report
from metrics_samples import PRICES, RECORDED, key, parsed, shadow_label, signal_label
from shadow_samples import entry

from review_bot.pr.compare import ComparedPr, Comparison, ExcludedPr, compare_runs
from review_bot.pr.feedback import FeedbackLabel, FeedbackRecord, RejectReason
from review_bot.price_flags import Prices

ACCEPT, REJECT = FeedbackLabel.ACCEPT, FeedbackLabel.REJECT


def compare(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    labels: list[FeedbackRecord] | None = None,
    prices: Prices | None = None,
) -> Comparison:
    return compare_runs(map(parsed, baseline), map(parsed, candidate), labels or [], prices)


def test_compare_pairs_by_head_sha_and_counts_only_the_compared_heads_units() -> None:
    baseline = [pr_report(2, HEAD_A, entry(key(1))), pr_report(2, HEAD_B, entry(key(2)))]
    candidate = [pr_report(2, HEAD_B, entry(key(3)))]

    comparison = compare(baseline, candidate)

    assert comparison.compared == (ComparedPr("owner/repo", 2, HEAD_B),)
    assert comparison.excluded == ()
    assert (comparison.baseline.units, comparison.candidate.units) == (1, 1)


def test_compare_excludes_candidates_without_a_same_head_baseline_and_baselines_alone() -> None:
    baseline = [pr_report(3, HEAD_A), pr_report(5, HEAD_A)]
    candidate = [pr_report(3, HEAD_C), pr_report(4, HEAD_C)]

    comparison = compare(baseline, candidate)

    assert comparison.compared == ()
    assert comparison.excluded == (
        ExcludedPr("owner/repo", 3, "no_same_head"),
        ExcludedPr("owner/repo", 4, "no_same_head"),
        ExcludedPr("owner/repo", 5, "no_candidate"),
    )


def test_compare_takes_the_latest_candidate_head_a_baseline_shares() -> None:
    baseline = [pr_report(2, HEAD_A), pr_report(2, HEAD_B)]
    candidate = [
        pr_report(2, HEAD_B, finished_minute=1),
        pr_report(2, HEAD_A, finished_minute=2),
        pr_report(2, HEAD_C, finished_minute=3),
    ]

    comparison = compare(baseline, candidate)

    assert comparison.compared == (ComparedPr("owner/repo", 2, HEAD_A),)


def test_compare_ignores_reports_not_reviewed_in_shadow_mode() -> None:
    baseline = [pr_report(2, HEAD_A, outcome="failed", reason="permanent", report=None)]
    candidate = [pr_report(2, HEAD_A, mode="comment"), pr_report(3, HEAD_A)]

    comparison = compare(baseline, candidate)

    assert comparison.compared == ()
    assert comparison.excluded == (ExcludedPr("owner/repo", 3, "no_same_head"),)


def test_compare_counts_each_sides_verdicts_by_reason_coverage_and_unjudged() -> None:
    entries = [entry(key(number)) for number in range(1, 6)]
    labels: list[FeedbackRecord] = [
        shadow_label(key(1), ACCEPT, pr=2),
        shadow_label(key(2), ACCEPT, pr=2),
        shadow_label(key(3), REJECT, RejectReason.WRONG, pr=2),
        shadow_label(key(4), REJECT, RejectReason.DUPLICATE, pr=2),
    ]

    side = compare([pr_report(2, HEAD_A, *entries)], [pr_report(2, HEAD_A)], labels).baseline

    assert (side.units, side.unjudged, side.coverage) == (5, 1, 0.8)
    assert side.reject_reasons == {
        RejectReason.WRONG: 1,
        RejectReason.NOT_WORTH_IT: 0,
        RejectReason.WRONG_LINE: 0,
        RejectReason.DUPLICATE: 1,
    }
    assert (side.verdicts.precision, side.verdicts.lower_bound) == (2 / 3, 0.5)
    assert side.verdicts.wilson_95 is not None


def test_compare_ignores_labels_of_prs_not_compared_and_keeps_the_latest_per_unit() -> None:
    labels: list[FeedbackRecord] = [
        shadow_label(key(1), REJECT, RejectReason.WRONG, pr=2, recorded_at=RECORDED),
        shadow_label(key(1), ACCEPT, pr=2, recorded_at=RECORDED + timedelta(hours=1)),
        shadow_label(key(1), ACCEPT, pr=9),
    ]

    side = compare([pr_report(2, HEAD_A, entry(key(1)))], [pr_report(2, HEAD_A)], labels).baseline

    assert (side.verdicts.accepted, side.verdicts.rejected, side.units) == (1, 0, 1)


def test_compare_ignores_reaction_and_resolution_labels() -> None:
    labels: list[FeedbackRecord] = [shadow_label(key(1), REJECT, RejectReason.WRONG, pr=2)]
    later = RECORDED + timedelta(hours=1)
    same_line = {"pr": 2, "anchor": key(1)[2]}
    signals: list[FeedbackRecord] = [
        signal_label("1" * 16, ACCEPT, recorded_at=later).model_copy(update=same_line),
        signal_label(
            "1" * 16, FeedbackLabel.RESOLVED, channel="resolution", recorded_at=later
        ).model_copy(update=same_line),
    ]
    reports = [pr_report(2, HEAD_A, entry(key(1)))], [pr_report(2, HEAD_A, entry(key(1)))]

    assert compare(*reports, labels + signals) == compare(*reports, labels)


def test_compare_pools_accepted_units_on_one_anchored_line_across_rule_parts() -> None:
    logic, reliability = key(1, "ai:logic"), key(1, "ai:reliability")
    labels: list[FeedbackRecord] = [
        shadow_label(logic, pr=2),
        shadow_label(reliability, pr=2),
        shadow_label(key(2), pr=2),
        shadow_label(key(3), pr=2),
    ]
    baseline = [pr_report(2, HEAD_A, entry(logic), entry(key(2)))]
    candidate = [pr_report(2, HEAD_A, entry(reliability), entry(key(3)))]

    comparison = compare(baseline, candidate, labels)

    pool = comparison.pool
    assert (pool.issues, pool.both, pool.baseline_only, pool.candidate_only) == (3, 1, 1, 1)
    assert comparison.recall(comparison.baseline) == 2 / 3
    assert comparison.recall(comparison.candidate) == 2 / 3


def test_compare_uses_reported_costs_when_every_report_has_one() -> None:
    labels: list[FeedbackRecord] = [shadow_label(key(1), pr=2)]
    candidate = [
        pr_report(2, HEAD_A, entry(key(1)), cost_usd=Decimal("0.30"), tool_calls=4),
        pr_report(3, HEAD_A, cost_usd=Decimal("0.10"), tool_calls=2),
    ]

    side = compare([pr_report(2, HEAD_A), pr_report(3, HEAD_A)], candidate, labels, PRICES)
    cost = side.candidate.cost

    assert cost is not None
    assert (cost.basis, cost.total_usd, cost.mean_per_pr_usd) == (
        "recorded",
        Decimal("0.40"),
        Decimal("0.20"),
    )
    assert cost.per_accepted_unit_usd == Decimal("0.40")
    assert (side.candidate.mean_tool_calls, side.candidate.most_tool_calls) == (3.0, 4)


def test_compare_estimates_a_side_with_an_unreported_cost_from_its_tokens() -> None:
    baseline = [
        pr_report(2, HEAD_A, cost_usd=Decimal("9"), input_tokens=1_000_000, output_tokens=0),
        pr_report(3, HEAD_A, input_tokens=0, output_tokens=1_000_000),
    ]
    candidate = [pr_report(2, HEAD_A), pr_report(3, HEAD_A)]

    cost = compare(baseline, candidate, prices=PRICES).baseline.cost

    assert cost is not None
    assert (cost.basis, cost.total_usd, cost.per_accepted_unit_usd) == (
        "estimated",
        Decimal(90),
        None,
    )


def test_compare_without_reported_costs_or_prices_has_no_cost() -> None:
    comparison = compare([pr_report(2, HEAD_A)], [pr_report(2, HEAD_A)])

    assert comparison.baseline.cost is None


def test_compare_shows_tokens_per_pr_and_review_time_percentiles() -> None:
    baseline = [
        pr_report(2, HEAD_A, input_tokens=100, output_tokens=10, review_s=5),
        pr_report(2, HEAD_A, input_tokens=300, output_tokens=30, review_s=20),
        pr_report(3, HEAD_A, input_tokens=200, output_tokens=20, review_s=60),
    ]

    side = compare(baseline, [pr_report(2, HEAD_A), pr_report(3, HEAD_A)]).baseline

    assert (side.mean_input_tokens, side.mean_output_tokens) == (300.0, 30.0)
    assert (side.review_s.runs, side.review_s.p50, side.review_s.p95) == (3, 20.0, 60.0)
    assert (side.mean_tool_calls, side.most_tool_calls) == (0.0, 0)


def judged_pair(
    baseline_labels: list[FeedbackLabel], candidate_labels: list[FeedbackLabel]
) -> Comparison:
    """PR 2 with one unit per label: the baseline's on lines 1.., the candidate's on 101.."""
    baseline = [entry(key(number)) for number in range(1, len(baseline_labels) + 1)]
    candidate = [entry(key(100 + number)) for number in range(1, len(candidate_labels) + 1)]
    labels: list[FeedbackRecord] = [
        shadow_label(key(number), label, None if label is ACCEPT else RejectReason.WRONG, pr=2)
        for number, label in [
            *enumerate(baseline_labels, start=1),
            *enumerate(candidate_labels, start=101),
        ]
    ]
    return compare([pr_report(2, HEAD_A, *baseline)], [pr_report(2, HEAD_A, *candidate)], labels)


def test_compare_verdict_states_precision_and_recall_changes_for_the_candidate() -> None:
    verdict = judged_pair([ACCEPT, REJECT], [ACCEPT, ACCEPT]).verdict

    assert (verdict.is_decidable, verdict.precision, verdict.recall) == (True, "higher", "higher")


def test_compare_verdict_reads_same_for_equal_shares() -> None:
    verdict = judged_pair([ACCEPT, REJECT], [ACCEPT, REJECT]).verdict

    assert (verdict.precision, verdict.recall) == ("same", "same")


def test_compare_verdict_reads_lower_and_not_measured_without_a_precision() -> None:
    verdict = judged_pair([ACCEPT], []).verdict

    assert (verdict.is_decidable, verdict.precision, verdict.recall) == (True, None, "lower")


def test_compare_verdict_is_not_decidable_below_90_percent_coverage() -> None:
    baseline = [pr_report(2, HEAD_A, *(entry(key(number)) for number in range(1, 11)))]
    labels: list[FeedbackRecord] = [shadow_label(key(number), pr=2) for number in range(1, 9)]

    verdict = compare(baseline, [pr_report(2, HEAD_A)], labels).verdict

    assert (verdict.is_decidable, verdict.precision, verdict.recall) == (False, None, None)


def test_compare_verdict_is_decidable_at_exactly_90_percent_coverage() -> None:
    baseline = [pr_report(2, HEAD_A, *(entry(key(number)) for number in range(1, 11)))]
    labels: list[FeedbackRecord] = [shadow_label(key(number), pr=2) for number in range(1, 10)]

    verdict = compare(baseline, [pr_report(2, HEAD_A)], labels).verdict

    assert verdict.is_decidable
