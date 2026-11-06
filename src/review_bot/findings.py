"""The review finding schema, parsing raw model text into findings, and the kept-finding wrapper."""

from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_bot.errors import ErrorCategory, ProviderError

__all__ = [
    "Category",
    "Finding",
    "ReviewedFinding",
    "RuleId",
    "Severity",
    "Source",
    "parse_findings",
]

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


class Category(StrEnum):
    """What kind of problem a finding describes."""

    LOGIC = auto()
    SECURITY = auto()
    RELIABILITY = auto()
    MAINTAINABILITY = auto()
    TESTING = auto()


class Finding(BaseModel):
    """One problem reported against one line of a diff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(pattern=r"\S")
    line: int = Field(ge=1)
    severity: Severity
    category: Category
    rationale: str = Field(pattern=r"\S")
    confidence: float = Field(ge=0.0, le=1.0)


class Source(StrEnum):
    """Where a finding came from."""

    RULE = auto()
    AI = auto()


class RuleId(StrEnum):
    """A check that runs with no model."""

    SWALLOWED_EXCEPTION = "swallowed-exception"
    OPEN_WITHOUT_ENCODING = "open-without-encoding"


@dataclass(frozen=True, slots=True)
class ReviewedFinding:
    """A finding the pipeline kept, with fields set by code, so the model can't forge them.

    ``rule`` is the rule that found it, or None for a model finding. ``fingerprint`` is its
    stable identity (``fingerprint.fingerprint_for``).
    """

    finding: Finding
    source: Source
    rule: RuleId | None
    fingerprint: str


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
