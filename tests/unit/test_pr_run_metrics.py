import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from shadow_samples import entry, report

from review_bot.pr.feedback import (
    FeedbackLabel,
    FeedbackRecord,
    RejectReason,
    ShadowFeedback,
    SignalFeedback,
)
from review_bot.pr.metrics import Prices
from review_bot.pr.metrics_report import render_metrics_json, render_metrics_text
from review_bot.pr.outcome import Outcome
from review_bot.pr.run_metrics import (
    OutcomeCount,
    Percentiles,
    RunMetrics,
    TokenTotals,
    summarise_runs,
)
from review_bot.pr.units import ShadowFile

RECORDED = datetime(2026, 11, 8, 12, tzinfo=UTC)
PRICES = Prices(Decimal(15), Decimal(75))


def parsed(document: dict[str, Any]) -> ShadowFile:
    return ShadowFile.model_validate_json(json.dumps(document), strict=True)


def key(number: int, rule_part: str = "ai:logic") -> tuple[str, str, str]:
    return ("src/app.py", rule_part, f"{number:016x}")


def usage(input_tokens: int, output_tokens: int) -> dict[str, Any]:
    return {
        "model": "claude-x",
        "prompt_version": "p1-v1",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def posted(number: int, applied: str = "done") -> dict[str, Any]:
    """A comment mode ``create`` entry, fingerprinted by ``number``, applied as ``applied``."""
    return entry(key(number), fingerprint=str(number) * 16) | {"applied": applied}


def shadow_label(
    anchor_key: tuple[str, str, str],
    label: FeedbackLabel = FeedbackLabel.ACCEPT,
    reason: RejectReason | None = None,
    *,
    pr: int = 7,
    recorded_at: datetime = RECORDED,
) -> ShadowFeedback:
    file, rule_part, anchor = anchor_key
    return ShadowFeedback.model_validate(
        {
            "channel": "shadow",
            "repo": "owner/repo",
            "pr": pr,
            "fingerprint": "f" * 16,
            "file": file,
            "rule_part": rule_part,
            "anchor": anchor,
            "index": 0,
            "source": "ai",
            "severity": "medium",
            "label": label,
            "reason": reason,
            "run_id": "run",
            "model": "claude-x",
            "prompt_version": "p1-v1",
            "recorded_at": recorded_at,
        }
    )


def signal_label(
    fingerprint: str,
    label: FeedbackLabel = FeedbackLabel.ACCEPT,
    *,
    channel: str = "reaction",
    recorded_at: datetime = RECORDED,
) -> SignalFeedback:
    return SignalFeedback.model_validate(
        {
            "channel": channel,
            "repo": "owner/repo",
            "pr": 7,
            "fingerprint": fingerprint,
            "file": "src/app.py",
            "rule_part": "ai:logic",
            "anchor": "a" * 16,
            "index": None,
            "source": None,
            "severity": None,
            "label": label,
            "reason": None,
            "run_id": None,
            "model": None,
            "prompt_version": None,
            "recorded_at": recorded_at,
        }
    )


def summarise(
    reports: list[dict[str, Any]],
    labels: list[FeedbackRecord] | None = None,
    *,
    since: date | None = None,
    until: date | None = None,
    prices: Prices | None = None,
) -> RunMetrics:
    return summarise_runs(
        map(parsed, reports), labels or [], since=since, until=until, prices=prices
    )


def timed_report(end_to_end_s: int, review_s: int, **fields: Any) -> dict[str, Any]:
    finished = datetime(2026, 11, 8, 12, tzinfo=UTC).timestamp()
    return report(
        event_at=datetime.fromtimestamp(finished - end_to_end_s, UTC).isoformat(),
        started_at=datetime.fromtimestamp(finished - review_s, UTC).isoformat(),
        finished_at=datetime.fromtimestamp(finished, UTC).isoformat(),
        **fields,
    )


def gate_sample() -> tuple[list[dict[str, Any]], list[FeedbackRecord]]:
    """34 would-be comments on 4 PRs: 21 accepted, 9 rejected as wrong, 2 duplicates."""
    reports = [
        report(*(entry(key(number)) for number in range(first, first + 10)), number=pr)
        for pr, first in ((1, 0), (2, 10), (3, 20))
    ]
    reports.append(report(*(entry(key(number)) for number in range(30, 34)), number=4))
    labels: list[FeedbackRecord] = []
    for number in range(32):
        pr = 1 + number // 10
        if number < 21:
            labels.append(shadow_label(key(number), pr=pr))
        elif number < 30:
            labels.append(
                shadow_label(key(number), FeedbackLabel.REJECT, RejectReason.WRONG, pr=pr)
            )
        else:
            duplicate = shadow_label(
                key(number), FeedbackLabel.REJECT, RejectReason.DUPLICATE, pr=pr
            )
            labels.append(duplicate)
    return reports, labels


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


# --- Rendering ----------------------------------------------------------------------------------


def test_json_holds_the_gate_samples_numbers_as_metrics_v1() -> None:
    reports, labels = gate_sample()

    document = json.loads(render_metrics_json(summarise(reports, labels)))

    assert document["schema"] == "metrics/v1"
    shadow = document["shadow_precision"]
    assert (shadow["precision"], shadow["wilson_low"], shadow["wilson_high"]) == (
        0.7,
        0.521,
        0.833,
    )
    assert (shadow["lower_bound"], shadow["coverage"], shadow["duplicates"]) == (0.656, 0.941, 2)
    assert shadow["splits"]["reject_reason"]["duplicate"] == {"count": 2}
    assert shadow["splits"]["source"]["ai"]["accepted"] == 21
    assert document["cost"] is None


def test_json_shows_cost_as_cent_strings_and_runs_with_their_outcomes() -> None:
    reports = [timed_report(90, 30, report=usage(1_000_000, 100_000))]

    document = json.loads(render_metrics_json(summarise(reports, prices=PRICES)))

    assert document["cost"] == {"usd": "22.50", "per_accepted_finding_usd": None}
    assert document["runs"]["latency_s"]["end_to_end"] == {"runs": 1, "p50": 90.0, "p95": 90.0}
    assert document["runs"]["outcomes"] == [
        {"mode": "shadow", "origin": "event", "outcome": "reviewed", "reason": None, "count": 1}
    ]
    assert document["tokens"]["input"] == {
        "total": 1_000_000,
        "mean": 1_000_000.0,
        "max": 1_000_000,
    }


def test_json_is_one_ascii_line_even_with_unicode_from_a_report() -> None:
    reports = [report(entry(key(1, "régle")))]

    rendered = render_metrics_json(summarise(reports, [shadow_label(key(1, "régle"))]))

    assert rendered.isascii()
    assert "\n" not in rendered


def test_text_shows_percentages_cents_and_n_a() -> None:
    reports, labels = gate_sample()
    reports.append(report(report=usage(1_000_000, 100_000), number=9))

    text = render_metrics_text(summarise(reports, labels, prices=PRICES))

    assert "precision=70.0% (21/30)" in text
    assert "wilson_95=52.1% to 83.3%" in text
    assert "lower_bound=65.6% (21/32)" in text
    assert "coverage=94.1%" in text
    assert "cost total=" in text
    assert "comment_precision units=0 judged=0 coverage=n/a" in text
    assert all(line.startswith("metrics: ") for line in text.splitlines())


def test_text_without_prices_says_not_priced() -> None:
    assert "metrics: cost=not priced" in render_metrics_text(summarise([report()])).splitlines()


def test_text_quotes_report_text_so_it_cant_forge_a_line() -> None:
    reports = [report(outcome="failed", reason="bad\nmetrics: forged", report=None)]

    text = render_metrics_text(summarise(reports))

    assert 'reason="bad\\nmetrics: forged"' in text
    assert "metrics: forged" not in text.splitlines()


def test_text_with_only_since_shows_until_as_any() -> None:
    text = render_metrics_text(summarise([], since=date(2026, 11, 1)))

    assert text.splitlines()[0] == "metrics: runs count=0 since=2026-11-01 until=any"


def test_text_with_only_until_shows_since_as_any() -> None:
    text = render_metrics_text(summarise([], until=date(2026, 11, 8)))

    assert text.splitlines()[0] == "metrics: runs count=0 since=any until=2026-11-08"
