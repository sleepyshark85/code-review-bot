"""The review finding schema, parsing raw model text into findings, and the kept-finding wrapper."""

from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.suggestion import SuggestionDropReason

__all__ = [
    "Category",
    "EvidenceSpan",
    "Finding",
    "ReviewedFinding",
    "RuleId",
    "Severity",
    "Source",
    "parse_findings",
]

# How many spans of context one finding may cite (P3-S4 Limits).
_MAX_EVIDENCE_SPANS: Final = 5

# Fixed, because pydantic's own message quotes the input and model text must never reach stderr.
_INVALID_OUTPUT_MESSAGE: Final = (
    'The model\'s output was rejected: it must be bare JSON {"findings": [...]} matching the '
    "findings schema. Run the review again."
)


class Severity(StrEnum):
    """How serious a finding is, in ascending order."""

    LOW = auto()
    MEDIUM = auto()
    HIGH = auto()
    CRITICAL = auto()

    @property
    def rank(self) -> int:
        """Its place in the ascending order, from 0 for ``low``, for comparing severities."""
        return list(Severity).index(self)


class Category(StrEnum):
    """What kind of problem a finding describes."""

    LOGIC = auto()
    SECURITY = auto()
    RELIABILITY = auto()
    MAINTAINABILITY = auto()
    TESTING = auto()


class RuleId(StrEnum):
    """A check that runs with no model."""

    SWALLOWED_EXCEPTION = "swallowed-exception"
    OPEN_WITHOUT_ENCODING = "open-without-encoding"


class EvidenceSpan(BaseModel):
    """Lines ``start_line`` to ``end_line`` of ``file``, both included, that a finding relies on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.start_line > self.end_line:
            raise ValueError("start_line must not be after end_line")
        return self


class Finding(BaseModel):
    """One problem reported against one line of a diff.

    ``rule`` is the model's tag, naming the rule that would catch the same problem, or None.
    ``evidence`` is the context the finding relies on, as the model cites it; once verified, only
    the spans the model was shown (``context.evidence``). ``suggestion`` is the model's
    replacement for the anchored line, or None; once verified, None unless verify kept it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(pattern=r"\S")
    line: int = Field(ge=1)
    severity: Severity
    category: Category
    rationale: str = Field(pattern=r"\S")
    confidence: float = Field(ge=0.0, le=1.0)
    rule: RuleId | None = None
    evidence: tuple[EvidenceSpan, ...] = Field(default=(), max_length=_MAX_EVIDENCE_SPANS)
    suggestion: str | None = None


class Source(StrEnum):
    """Where a finding came from."""

    RULE = auto()
    AI = auto()


@dataclass(frozen=True, slots=True)
class ReviewedFinding:
    """A finding the pipeline kept, with fields set by code, so the model can't forge them.

    ``rule`` is the rule that found it, or a model finding's tag (``Finding.rule``).
    ``fingerprint`` is its stable identity (``fingerprint.fingerprint_for``).
    ``evidence_dropped`` counts the cited spans verify dropped from ``finding.evidence``.
    ``suggestion_dropped`` is why verify dropped the model's suggestion, or None if it kept one
    or there was none.
    """

    finding: Finding
    source: Source
    rule: RuleId | None
    fingerprint: str
    evidence_dropped: int = 0
    suggestion_dropped: SuggestionDropReason | None = None

    @property
    def rule_part(self) -> str:
        """Its fingerprint's rule part: ``rule``, or ``ai:<category>`` if it has none (ADR 0003)."""
        return self.rule or f"ai:{self.finding.category}"


class _FindingsEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: tuple[Finding, ...]


def parse_findings(raw: str) -> tuple[Finding, ...]:
    """Parse model text that must be bare JSON of the form ``{"findings": [...]}``.

    Validation is strict: model output is rejected whole, never coerced into shape.

    Raises:
        ProviderError: With category ``invalid_output``, if the text isn't JSON or doesn't match
            the schema.
    """
    try:
        return _FindingsEnvelope.model_validate_json(raw, strict=True).findings
    except ValidationError as exc:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT, _INVALID_OUTPUT_MESSAGE) from exc
