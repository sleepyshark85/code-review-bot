import json
from decimal import Decimal

from compare_samples import HEAD_A, HEAD_B, pr_report
from metrics_samples import PRICES, key, parsed, shadow_label
from shadow_samples import entry

from review_bot.pr.compare import Comparison, compare_runs
from review_bot.pr.compare_report import render_compare_json, render_compare_text
from review_bot.pr.feedback import FeedbackLabel, FeedbackRecord, RejectReason


def comparison(*, judged: int = 2) -> Comparison:
    """PR 2 compared, PR 3 excluded; the baseline has two units, ``judged`` of them labelled."""
    baseline = [
        pr_report(2, HEAD_A, entry(key(1)), entry(key(2)), review_s=12),
        pr_report(3, HEAD_A),
    ]
    candidate = [
        pr_report(2, HEAD_A, entry(key(1)), cost_usd=Decimal("0.123456"), tool_calls=3),
        pr_report(3, HEAD_B),
    ]
    labels: list[FeedbackRecord] = [
        shadow_label(key(1), pr=2),
        shadow_label(key(2), FeedbackLabel.REJECT, RejectReason.WRONG, pr=2),
    ][:judged]
    return compare_runs(map(parsed, baseline), map(parsed, candidate), labels, PRICES)


def test_render_compare_json_is_one_ascii_compare_v1_document() -> None:
    document = json.loads(render_compare_json(comparison()))

    assert document["schema"] == "compare/v1"
    assert document["prs"] == {
        "compared": [{"repo": "owner/repo", "number": 2, "head_sha": HEAD_A}],
        "excluded": [{"repo": "owner/repo", "number": 3, "reason": "no_same_head"}],
    }
    assert list(document) == ["schema", "prs", "baseline", "candidate", "pool", "verdict"]
    assert list(document["baseline"]) == [
        "units",
        "precision",
        "recall",
        "cost",
        "tokens",
        "tool_calls",
        "latency",
    ]


def test_render_compare_json_holds_each_sides_figures() -> None:
    document = json.loads(render_compare_json(comparison()))

    baseline, candidate = document["baseline"], document["candidate"]
    assert baseline["units"] == {
        "total": 2,
        "judged": 2,
        "accepted": 1,
        "rejected": {"wrong": 1, "not_worth_it": 0, "wrong_line": 0, "duplicate": 0},
        "unjudged": 0,
        "coverage": 1.0,
    }
    assert baseline["precision"]["share"] == 0.5
    assert baseline["recall"] == {"issues": 1, "pool": 1, "share": 1.0}
    assert baseline["cost"] == {
        "basis": "estimated",
        "total_usd": "0.0225",
        "mean_per_pr_usd": "0.0225",
        "per_accepted_unit_usd": "0.0225",
    }
    assert candidate["cost"]["total_usd"] == "0.1235"
    assert candidate["tool_calls"] == {"mean_per_pr": 3.0, "max_per_pr": 3}
    assert baseline["latency"] == {"review_s": {"runs": 1, "p50": 12.0, "p95": 12.0}}
    assert document["pool"] == {"issues": 1, "both": 1, "baseline_only": 0, "candidate_only": 0}
    assert document["verdict"] == {
        "decidable": True,
        "reason": None,
        "precision": "higher",
        "recall": "same",
        "coverage": {"baseline": 1.0, "candidate": 1.0},
    }


def test_render_compare_json_marks_a_cost_with_no_basis_not_available() -> None:
    result = compare_runs(
        [parsed(pr_report(2, HEAD_A))], [parsed(pr_report(2, HEAD_A))], [], prices=None
    )

    cost = json.loads(render_compare_json(result))["baseline"]["cost"]

    assert cost == {
        "basis": "not available",
        "total_usd": None,
        "mean_per_pr_usd": None,
        "per_accepted_unit_usd": None,
    }


def test_render_compare_text_shows_the_same_figures() -> None:
    text = render_compare_text(comparison())

    assert text.splitlines() == [
        "compare: prs compared=1 excluded=1",
        f"compare: compared repo=owner/repo number=2 head_sha={HEAD_A}",
        "compare: excluded repo=owner/repo number=3 reason=no_same_head",
        "compare: baseline units=2 judged=2 accepted=1 rejected wrong=1 not_worth_it=0 "
        "wrong_line=0 duplicate=0 unjudged=0 coverage=100.0%",
        "compare: baseline precision=50.0% (1/2) wilson_95=9.5% to 90.5% lower_bound=50.0% (1/2)",
        "compare: baseline recall=100.0% (1/1)",
        "compare: baseline cost total=0.0225 USD mean_per_pr=0.0225 USD "
        "per_accepted_unit=0.0225 USD basis=estimated",
        "compare: baseline tokens_per_pr mean_input=1000.0 mean_output=100.0",
        "compare: baseline tool_calls_per_pr mean=0.0 max=0",
        "compare: baseline latency_s review runs=1 p50=12.0 p95=12.0",
        "compare: candidate units=1 judged=1 accepted=1 rejected wrong=0 not_worth_it=0 "
        "wrong_line=0 duplicate=0 unjudged=0 coverage=100.0%",
        "compare: candidate precision=100.0% (1/1) wilson_95=20.7% to 100.0% "
        "lower_bound=100.0% (1/1)",
        "compare: candidate recall=100.0% (1/1)",
        "compare: candidate cost total=0.1235 USD mean_per_pr=0.1235 USD "
        "per_accepted_unit=0.1235 USD basis=recorded",
        "compare: candidate tokens_per_pr mean_input=1000.0 mean_output=100.0",
        "compare: candidate tool_calls_per_pr mean=3.0 max=3",
        "compare: candidate latency_s review runs=1 p50=10.0 p95=10.0",
        "compare: pool issues=1 both=1 baseline_only=0 candidate_only=0",
        "compare: verdict precision=higher recall=same",
    ]


def test_render_compare_text_says_why_a_verdict_is_not_decidable() -> None:
    text = render_compare_text(comparison(judged=1))

    assert text.splitlines()[-1] == (
        "compare: verdict=not decidable: coverage baseline_coverage=50.0% candidate_coverage=100.0%"
    )


def test_render_compare_text_marks_unmeasured_changes_and_missing_numbers() -> None:
    result = compare_runs(
        [parsed(pr_report(2, HEAD_A))], [parsed(pr_report(2, HEAD_A))], [], prices=None
    )

    lines = render_compare_text(result).splitlines()

    assert "compare: baseline cost=not available" in lines
    assert "compare: baseline recall=n/a (0/0)" in lines
    assert lines[-1] == "compare: verdict precision=not measured recall=not measured"
