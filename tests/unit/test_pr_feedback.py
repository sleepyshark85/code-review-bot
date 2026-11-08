import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from review_bot.pr.feedback import FeedbackLabel, FeedbackRecord, RejectReason, Unit
from review_bot.pr.feedback import render_feedback_line as render

RECORD_KEYS = [
    "schema",
    "channel",
    "repo",
    "pr",
    "fingerprint",
    "file",
    "rule_part",
    "anchor",
    "index",
    "source",
    "severity",
    "label",
    "reason",
    "run_id",
    "model",
    "prompt_version",
    "recorded_at",
]


def record(**fields: Any) -> FeedbackRecord:
    values: dict[str, Any] = {
        "channel": "shadow",
        "repo": "owner/repo",
        "pr": 7,
        "fingerprint": "f" * 16,
        "file": "src/app.py",
        "rule_part": "ai:logic",
        "anchor": "a" * 16,
        "index": 1,
        "source": "ai",
        "severity": "high",
        "label": FeedbackLabel.ACCEPT,
        "reason": None,
        "run_id": "run-1",
        "model": "claude-x",
        "prompt_version": "p1-v1",
        "recorded_at": datetime(2026, 11, 8, 12, tzinfo=UTC),
    }
    return FeedbackRecord(**(values | fields))


def test_render_feedback_line_writes_exactly_the_record_keys_in_order_as_one_ascii_line() -> None:
    line = render(record(file="src/café.py"))

    assert line.isascii()
    assert "\n" not in line
    document = json.loads(line)
    assert list(document) == RECORD_KEYS
    assert document["schema"] == "feedback/v1"
    assert document["file"] == "src/café.py"
    assert document["reason"] is None


def test_render_feedback_line_writes_recorded_at_in_utc() -> None:
    eastern = timezone(timedelta(hours=-5))

    line = render(record(recorded_at=datetime(2026, 11, 8, 7, tzinfo=eastern)))

    assert json.loads(line)["recorded_at"] == "2026-11-08T12:00:00+00:00"


def test_a_rendered_record_parses_back_to_the_same_record() -> None:
    original = record(label=FeedbackLabel.REJECT, reason=RejectReason.WRONG_LINE)

    parsed = FeedbackRecord.model_validate_json(render(original), strict=True)

    assert parsed == original


@pytest.mark.parametrize(
    ("label", "reason"),
    [(FeedbackLabel.ACCEPT, RejectReason.WRONG), (FeedbackLabel.REJECT, None)],
)
def test_feedback_record_needs_a_reason_exactly_on_a_reject(
    label: FeedbackLabel, reason: RejectReason | None
) -> None:
    with pytest.raises(ValidationError, match="reason"):
        record(label=label, reason=reason)


@pytest.mark.parametrize(
    "fields",
    [
        {"fingerprint": "not-hex"},
        {"anchor": "A" * 16},
        {"repo": "no-slash"},
        {"pr": 0},
        {"index": -1},
        {"channel": "reaction"},
        {"recorded_at": datetime(2026, 11, 8)},  # noqa: DTZ001 - a naive time is the input under test
    ],
)
def test_feedback_record_rejects_an_invalid_field(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        record(**fields)


def test_feedback_record_with_an_extra_key_is_invalid() -> None:
    document = json.loads(render(record())) | {"body": "text"}

    with pytest.raises(ValidationError):
        FeedbackRecord.model_validate(document)


def test_feedback_record_unit_is_its_repo_pr_key_and_index() -> None:
    assert record().unit == Unit("owner/repo", 7, "src/app.py", "ai:logic", "a" * 16, 1)
