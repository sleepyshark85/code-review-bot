"""``feedback/v1`` records: a verdict on one of the bot's comments, and its channel (ADR 0011).

A ``shadow`` record is a person's judgement of a would-be comment (P2-S4). A ``reaction`` or
``resolution`` record is read from what people did to a posted comment (P2-S8). No record holds a
comment's body, its rationale or a username.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)

from review_bot.findings import Severity, Source
from review_bot.pr.github import CommentSignals, Resolver

__all__ = [
    "FeedbackLabel",
    "FeedbackRecord",
    "Hex16",
    "RejectReason",
    "RepoName",
    "ShadowFeedback",
    "SignalFeedback",
    "Text",
    "Unit",
    "labels_from_signals",
    "parse_feedback_line",
    "render_feedback_line",
]

_SCHEMA: Final = "feedback/v1"
_THUMBS_UP: Final = "+1"
_THUMBS_DOWN: Final = "-1"

# A fingerprint or an anchor: the first 16 hex digits of a SHA-256 (ADR 0003, ADR 0009).
type Hex16 = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
# ``owner/name``, as ``pr.event`` accepts it.
type RepoName = Annotated[str, Field(pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")]
type Text = Annotated[str, Field(min_length=1)]


class FeedbackLabel(StrEnum):
    """A verdict on a comment. Only ``accept`` and ``reject`` move precision (ADR 0011)."""

    ACCEPT = "accept"
    REJECT = "reject"
    CONFLICT = "conflict"
    RESOLVED = "resolved"


class RejectReason(StrEnum):
    """Why a would-be comment was rejected. Precision leaves ``duplicate`` out (design)."""

    WRONG = "wrong"
    NOT_WORTH_IT = "not_worth_it"
    WRONG_LINE = "wrong_line"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class Unit:
    """One would-be comment: a pull request's comments sharing a key, told apart by ``index``.

    The key is the path, the rule part and the anchor. ``index`` numbers one report's entries
    for a key in line order (Phase 2 design, How the gate is evidenced).
    """

    repo: str
    pr: int
    file: str
    rule_part: str
    anchor: str
    index: int


class _Record(BaseModel):
    """The keys every ``feedback/v1`` record has, in order (P2-S4 AC10).

    Each channel's record narrows the fields that differ, keeping their place.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Aliased because ``schema`` is a ``BaseModel`` attribute.
    schema_: Literal["feedback/v1"] = Field(default=_SCHEMA, alias="schema")
    channel: str
    repo: RepoName
    pr: Annotated[int, Field(ge=1)]
    fingerprint: Hex16
    file: Text
    rule_part: Text
    anchor: Hex16
    index: Annotated[int, Field(ge=0)] | None
    source: Source | None
    severity: Severity | None
    label: FeedbackLabel
    reason: RejectReason | None
    run_id: Text | None
    model: Text | None
    prompt_version: Text | None
    recorded_at: AwareDatetime

    @field_serializer("recorded_at")
    def _utc(self, recorded_at: datetime) -> str:
        return recorded_at.astimezone(UTC).isoformat()


class ShadowFeedback(_Record):
    """A person's judgement of a would-be comment from shadow reports (P2-S4).

    ``fingerprint``, ``source`` and ``severity`` are the latest report's, and ``run_id``,
    ``model`` and ``prompt_version`` are that report's run. ``reason`` is set exactly when the
    label is ``reject``.
    """

    channel: Literal["shadow"]
    index: Annotated[int, Field(ge=0)]
    source: Source
    severity: Severity
    run_id: Text
    model: Text
    prompt_version: Text

    @field_validator("label", mode="before")
    @classmethod
    def _accept_or_reject(cls, label: object) -> object:
        # Checked before parsing, so the error lists the values a person types, not enum members.
        if label not in (FeedbackLabel.ACCEPT, FeedbackLabel.REJECT):
            raise ValueError("a shadow record's label is 'accept' or 'reject'")
        return label

    @model_validator(mode="after")
    def _check_reason(self) -> Self:
        if (self.label is FeedbackLabel.REJECT) != (self.reason is not None):
            raise ValueError("reason is required on a reject, and must be null on an accept")
        return self

    @property
    def unit(self) -> Unit:
        """The would-be comment this record judges."""
        return Unit(self.repo, self.pr, self.file, self.rule_part, self.anchor, self.index)


type _SignalChannel = Literal["reaction", "resolution"]


class SignalFeedback(_Record):
    """What people did to a posted comment, keyed by its posted fingerprint (P2-S8).

    A ``reaction`` record is labelled ``accept``, ``reject`` or ``conflict``, and a
    ``resolution`` record ``resolved``. ``file``, ``rule_part`` and ``anchor`` are the comment's;
    the fields only its report holds are null, for P2-S10 to join.
    """

    channel: _SignalChannel
    index: None
    source: None
    severity: None
    reason: None
    run_id: None
    model: None
    prompt_version: None

    @model_validator(mode="after")
    def _check_label(self) -> Self:
        if (self.channel == "resolution") != (self.label is FeedbackLabel.RESOLVED):
            raise ValueError("label is resolved exactly on a resolution record")
        return self


# One line of a ``feedback/v1`` file.
type FeedbackRecord = ShadowFeedback | SignalFeedback


class _Channel(BaseModel):
    """A line's ``channel`` alone, read first to choose its record."""

    channel: Literal["shadow", "reaction", "resolution"]


_CHANNEL: Final = TypeAdapter(_Channel)


def parse_feedback_line(line: str | bytes) -> FeedbackRecord:
    """Parse one line of a ``feedback/v1`` file, strictly, as the record its ``channel`` names.

    Reading the channel first keeps each error's location a plain key, such as ``repo``.

    Raises:
        ValidationError: If it isn't a JSON object, its channel is missing or unknown, or it
            isn't a valid record of that channel.
    """
    channel = _CHANNEL.validate_json(line, strict=True).channel
    record = ShadowFeedback if channel == "shadow" else SignalFeedback
    return record.model_validate_json(line, strict=True)


def render_feedback_line(record: ShadowFeedback | SignalFeedback) -> str:
    """Render the record as one line of pure ASCII JSON, with no line break."""
    document = record.model_dump(mode="json", by_alias=True)
    return json.dumps(document, ensure_ascii=True, allow_nan=False)


def labels_from_signals(
    signals: Iterable[CommentSignals], *, repo: str, pr: int, recorded_at: datetime
) -> list[SignalFeedback]:
    """Reduce the bot's comments on one pull request, and what people did, to labels (ADR 0011).

    Comments sharing a fingerprint, such as a moved comment and its old thread, are one finding,
    with at most one record per channel. A finding's records name its newest comment's file,
    rule part and anchor. Findings come in order of their oldest comment, each reaction record
    before its resolution record.
    """
    findings: dict[str, list[CommentSignals]] = {}
    for signal in sorted(signals, key=lambda signal: signal.comment.comment_id):
        findings.setdefault(signal.comment.fingerprint, []).append(signal)
    records: list[SignalFeedback] = []
    for comments in findings.values():
        newest = comments[-1].comment
        for channel, label in _finding_labels(comments):
            record = SignalFeedback(
                channel=channel,
                repo=repo,
                pr=pr,
                fingerprint=newest.fingerprint,
                file=newest.path,
                rule_part=newest.rule_part,
                anchor=newest.anchor,
                index=None,
                source=None,
                severity=None,
                label=label,
                reason=None,
                run_id=None,
                model=None,
                prompt_version=None,
                recorded_at=recorded_at,
            )
            records.append(record)
    return records


def _finding_labels(
    comments: Sequence[CommentSignals],
) -> list[tuple[_SignalChannel, FeedbackLabel]]:
    """One finding's labels, from every comment that carries it.

    Its thumbs give a ``reaction`` label: thumbs-up alone is ``accept``, thumbs-down alone
    ``reject``, and both ``conflict``; other reactions count for nothing. A thread a person
    resolved gives a ``resolution`` label, ``resolved``.
    """
    reactions = frozenset[str]().union(*(signal.reactions for signal in comments))
    up, down = _THUMBS_UP in reactions, _THUMBS_DOWN in reactions
    labels: list[tuple[_SignalChannel, FeedbackLabel]] = []
    if up and down:
        labels.append(("reaction", FeedbackLabel.CONFLICT))
    elif up:
        labels.append(("reaction", FeedbackLabel.ACCEPT))
    elif down:
        labels.append(("reaction", FeedbackLabel.REJECT))
    if any(signal.comment.resolved_by is Resolver.PERSON for signal in comments):
        labels.append(("resolution", FeedbackLabel.RESOLVED))
    return labels
