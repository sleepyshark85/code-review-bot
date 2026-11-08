"""Shared samples for the run metrics and metrics report unit tests."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from shadow_samples import entry, report

from review_bot.pr.feedback import (
    FeedbackLabel,
    FeedbackRecord,
    RejectReason,
    ShadowFeedback,
    SignalFeedback,
)
from review_bot.pr.metrics import Prices
from review_bot.pr.run_metrics import RunMetrics, summarise_runs
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
