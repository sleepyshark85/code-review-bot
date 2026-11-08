import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from review_bot.findings import Severity
from review_bot.pr.feedback import (
    FeedbackLabel,
    RejectReason,
    ShadowFeedback,
    SignalFeedback,
    Unit,
    labels_from_signals,
    parse_feedback_line,
)
from review_bot.pr.feedback import render_feedback_line as render
from review_bot.pr.github import BotComment, CommentSignals, Resolver

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
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
# What a reaction or resolution record leaves for P2-S10 to fill from the comment's report.
JOINED_KEYS = ["index", "source", "severity", "reason", "run_id", "model", "prompt_version"]
FP_A = "a" * 16
FP_B = "b" * 16


def record(**fields: Any) -> ShadowFeedback:
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
        "recorded_at": NOW,
    }
    return ShadowFeedback(**(values | fields))


def signal_record(**fields: Any) -> SignalFeedback:
    values: dict[str, Any] = {
        "channel": "reaction",
        "repo": "owner/repo",
        "pr": 7,
        "fingerprint": "f" * 16,
        "file": "src/app.py",
        "rule_part": "ai:logic",
        "anchor": "a" * 16,
        "label": FeedbackLabel.CONFLICT,
        "recorded_at": NOW,
        **dict.fromkeys(JOINED_KEYS),
    }
    return SignalFeedback(**(values | fields))


def signals(
    comment_id: int,
    *reactions: str,
    fingerprint: str = FP_A,
    path: str = "src/app.py",
    anchor: str = "c" * 16,
    resolved_by: Resolver | None = None,
) -> CommentSignals:
    comment = BotComment(
        comment_id=comment_id,
        thread_id=f"T{comment_id}",
        fingerprint=fingerprint,
        rule_part="ai:logic",
        anchor=anchor,
        path=path,
        line=3,
        severity=Severity.HIGH,
        resolved_by=resolved_by,
    )
    return CommentSignals(comment, frozenset(reactions))


def labels(*comments: CommentSignals) -> list[tuple[str, str, FeedbackLabel]]:
    records = labels_from_signals(comments, repo="owner/repo", pr=7, recorded_at=NOW)
    return [(r.fingerprint, r.channel, r.label) for r in records]


# --- records ------------------------------------------------------------------------------------


def test_render_feedback_line_writes_exactly_the_record_keys_in_order_as_one_ascii_line() -> None:
    line = render(record(file="src/café.py"))

    assert line.isascii()
    assert "\n" not in line
    document = json.loads(line)
    assert list(document) == RECORD_KEYS
    assert document["schema"] == "feedback/v1"
    assert document["file"] == "src/café.py"
    assert document["reason"] is None


def test_a_signal_record_has_the_same_keys_in_order_with_the_joined_ones_null() -> None:
    document = json.loads(render(signal_record()))

    assert list(document) == RECORD_KEYS
    assert [document[key] for key in JOINED_KEYS] == [None] * len(JOINED_KEYS)


def test_render_feedback_line_writes_recorded_at_in_utc() -> None:
    eastern = timezone(timedelta(hours=-5))

    line = render(record(recorded_at=datetime(2026, 11, 8, 7, tzinfo=eastern)))

    assert json.loads(line)["recorded_at"] == "2026-11-08T12:00:00+00:00"


@pytest.mark.parametrize(
    "original",
    [
        record(label=FeedbackLabel.REJECT, reason=RejectReason.WRONG_LINE),
        signal_record(),
        signal_record(channel="resolution", label=FeedbackLabel.RESOLVED),
    ],
    ids=["shadow", "reaction", "resolution"],
)
def test_a_rendered_record_parses_back_to_the_same_record(
    original: ShadowFeedback | SignalFeedback,
) -> None:
    assert parse_feedback_line(render(original)) == original


@pytest.mark.parametrize(
    ("label", "reason"),
    [(FeedbackLabel.ACCEPT, RejectReason.WRONG), (FeedbackLabel.REJECT, None)],
)
def test_a_shadow_record_needs_a_reason_exactly_on_a_reject(
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
        {"index": None},
        {"source": None},
        {"channel": "reaction"},
        {"label": FeedbackLabel.CONFLICT},
        {"label": FeedbackLabel.RESOLVED},
        {"recorded_at": datetime(2026, 11, 8)},  # noqa: DTZ001 - a naive time is the input under test
    ],
)
def test_a_shadow_record_rejects_an_invalid_field(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        record(**fields)


@pytest.mark.parametrize(
    "fields",
    [
        {"channel": "shadow"},
        {"index": 0},
        {"source": "ai"},
        {"run_id": "run-1"},
        {"reason": RejectReason.WRONG, "label": FeedbackLabel.REJECT},
        {"label": FeedbackLabel.RESOLVED},
        {"channel": "resolution", "label": FeedbackLabel.ACCEPT},
    ],
)
def test_a_signal_record_rejects_an_invalid_field(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        signal_record(**fields)


def test_a_record_with_an_extra_key_is_invalid() -> None:
    line = json.dumps(json.loads(render(record())) | {"body": "text"})

    with pytest.raises(ValidationError):
        parse_feedback_line(line)


@pytest.mark.parametrize("channel", [None, "replies", "\x1b[31m", [], {}])
def test_a_line_of_an_unknown_channel_is_invalid_naming_the_channels_not_the_value(
    channel: object,
) -> None:
    document = json.loads(render(signal_record())) | {"channel": channel}

    with pytest.raises(ValidationError) as caught:
        parse_feedback_line(json.dumps(document))

    error = caught.value.errors()[0]
    assert (error["loc"], error["msg"]) == (
        ("channel",),
        "Input should be 'shadow', 'reaction' or 'resolution'",
    )


@pytest.mark.parametrize("line", ['{"schema": "feedback/v1"}', "[]"])
def test_a_line_with_no_channel_or_that_is_not_an_object_is_invalid(line: str) -> None:
    with pytest.raises(ValidationError):
        parse_feedback_line(line)


@pytest.mark.parametrize("label", ["conflict", "resolved", "maybe", []])
def test_a_shadow_line_with_another_label_names_the_two_a_person_types(label: object) -> None:
    document = json.loads(render(record())) | {"label": label}

    with pytest.raises(ValidationError) as caught:
        parse_feedback_line(json.dumps(document))

    error = caught.value.errors()[0]
    assert error["loc"] == ("label",)
    assert error["msg"].endswith("a shadow record's label is 'accept' or 'reject'")


def test_an_invalid_line_names_the_plain_key_at_fault() -> None:
    document = json.loads(render(signal_record())) | {"repo": "no-slash"}

    with pytest.raises(ValidationError) as caught:
        parse_feedback_line(json.dumps(document))

    assert caught.value.errors()[0]["loc"] == ("repo",)


def test_a_shadow_record_unit_is_its_repo_pr_key_and_index() -> None:
    assert record().unit == Unit("owner/repo", 7, "src/app.py", "ai:logic", "a" * 16, 1)


# --- labels_from_signals ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reactions", "expected"),
    [
        (["+1"], FeedbackLabel.ACCEPT),
        (["-1"], FeedbackLabel.REJECT),
        (["+1", "-1"], FeedbackLabel.CONFLICT),
        (["+1", "heart", "eyes"], FeedbackLabel.ACCEPT),
    ],
)
def test_thumbs_on_a_comment_become_one_reaction_label(
    reactions: list[str], expected: FeedbackLabel
) -> None:
    assert labels(signals(1, *reactions)) == [(FP_A, "reaction", expected)]


def test_other_reactions_or_none_give_no_label() -> None:
    assert labels(signals(1, "heart", "laugh", "eyes"), signals(2, fingerprint=FP_B)) == []


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("+1", "-1", FeedbackLabel.CONFLICT),
        ("+1", "+1", FeedbackLabel.ACCEPT),
        ("-1", "heart", FeedbackLabel.REJECT),
    ],
)
def test_comments_sharing_a_fingerprint_combine_into_one_reaction_label(
    first: str, second: str, expected: FeedbackLabel
) -> None:
    assert labels(signals(1, first), signals(2, second)) == [(FP_A, "reaction", expected)]


def test_a_thread_a_person_resolved_gives_a_resolution_label_beside_any_reaction() -> None:
    assert labels(
        signals(1, "-1", resolved_by=Resolver.PERSON),
        signals(2, fingerprint=FP_B, resolved_by=Resolver.PERSON),
    ) == [
        (FP_A, "reaction", FeedbackLabel.REJECT),
        (FP_A, "resolution", FeedbackLabel.RESOLVED),
        (FP_B, "resolution", FeedbackLabel.RESOLVED),
    ]


def test_a_thread_the_bot_resolved_gives_no_resolution_label() -> None:
    assert labels(signals(1, resolved_by=Resolver.BOT)) == []


def test_a_moved_comment_and_its_person_resolved_old_thread_give_one_resolution_label() -> None:
    assert labels(
        signals(1, resolved_by=Resolver.PERSON), signals(2, resolved_by=Resolver.PERSON)
    ) == [(FP_A, "resolution", FeedbackLabel.RESOLVED)]


def test_findings_come_in_order_of_their_oldest_comment_whatever_the_input_order() -> None:
    assert labels(signals(3, "+1", fingerprint=FP_B), signals(2, "-1"), signals(1, "+1")) == [
        (FP_A, "reaction", FeedbackLabel.CONFLICT),
        (FP_B, "reaction", FeedbackLabel.ACCEPT),
    ]


def test_a_findings_records_name_its_newest_comments_file_and_anchor_and_null_the_rest() -> None:
    old = signals(1, "+1", path="old.py", anchor="1" * 16)
    new = signals(2, path="new.py", anchor="2" * 16, resolved_by=Resolver.PERSON)

    records = labels_from_signals([new, old], repo="owner/repo", pr=7, recorded_at=NOW)

    assert {(r.file, r.anchor, r.rule_part) for r in records} == {("new.py", "2" * 16, "ai:logic")}
    assert {(r.repo, r.pr, r.recorded_at) for r in records} == {("owner/repo", 7, NOW)}
    assert all(getattr(r, key) is None for r in records for key in JOINED_KEYS)
