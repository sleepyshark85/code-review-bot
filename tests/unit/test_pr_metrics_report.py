import json
from datetime import date

from metrics_samples import PRICES, gate_sample, key, shadow_label, summarise, timed_report, usage
from shadow_samples import entry, report

from review_bot.pr.metrics_report import render_metrics_json, render_metrics_text


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
