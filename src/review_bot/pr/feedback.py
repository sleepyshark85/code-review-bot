"""``feedback/v1`` records: a person's verdict on one would-be comment (ADR 0011).

A record keeps the would-be comment's identity and metadata, never its body, its rationale or a
username. S8 adds the ``reaction`` and ``resolution`` channels and their labels.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer, model_validator

from review_bot.findings import Severity, Source

__all__ = [
    "FeedbackLabel",
    "FeedbackRecord",
    "Hex16",
    "RejectReason",
    "RepoName",
    "Text",
    "Unit",
    "render_feedback_line",
]

_SCHEMA: Final = "feedback/v1"

# A fingerprint or an anchor: the first 16 hex digits of a SHA-256 (ADR 0003, ADR 0009).
type Hex16 = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
# ``owner/name``, as ``pr.event`` accepts it.
type RepoName = Annotated[str, Field(pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")]
type Text = Annotated[str, Field(min_length=1)]


class FeedbackLabel(StrEnum):
    """A person's verdict on a would-be comment."""

    ACCEPT = "accept"
    REJECT = "reject"


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


class FeedbackRecord(BaseModel):
    """One ``feedback/v1`` record. Its fields are the whole record, in order (P2-S4 AC10).

    ``fingerprint``, ``source`` and ``severity`` are the latest report's, and ``run_id``,
    ``model`` and ``prompt_version`` are that report's run. ``reason`` is set exactly when the
    label is ``reject``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Aliased because ``schema`` is a ``BaseModel`` attribute.
    schema_: Literal["feedback/v1"] = Field(default=_SCHEMA, alias="schema")
    channel: Literal["shadow"]
    repo: RepoName
    pr: Annotated[int, Field(ge=1)]
    fingerprint: Hex16
    file: Text
    rule_part: Text
    anchor: Hex16
    index: Annotated[int, Field(ge=0)]
    source: Source
    severity: Severity
    label: FeedbackLabel
    reason: RejectReason | None
    run_id: Text
    model: Text
    prompt_version: Text
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _check_reason(self) -> Self:
        if (self.label is FeedbackLabel.REJECT) != (self.reason is not None):
            raise ValueError("reason is required on a reject, and must be null on an accept")
        return self

    @field_serializer("recorded_at")
    def _utc(self, recorded_at: datetime) -> str:
        return recorded_at.astimezone(UTC).isoformat()

    @property
    def unit(self) -> Unit:
        """The would-be comment this record judges."""
        return Unit(self.repo, self.pr, self.file, self.rule_part, self.anchor, self.index)


def render_feedback_line(record: FeedbackRecord) -> str:
    """Render the record as one line of pure ASCII JSON, with no line break."""
    document = record.model_dump(mode="json", by_alias=True)
    return json.dumps(document, ensure_ascii=True, allow_nan=False)
