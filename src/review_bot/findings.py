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


class Finding(BaseModel):
    """One problem reported against one line of a diff.

    ``rule`` is the model's tag, naming the rule that would catch the same problem, or None.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(pattern=r"\S")
    line: int = Field(ge=1)
    severity: Severity
    category: Category
    rationale: str = Field(pattern=r"\S")
    confidence: float = Field(ge=0.0, le=1.0)
    rule: RuleId | None = None


class Source(StrEnum):
    """Where a finding came from."""

    RULE = auto()
    AI = auto()


@dataclass(frozen=True, slots=True)
class ReviewedFinding:
    """A finding the pipeline kept, with fields set by code, so the model can't forge them.

    ``rule`` is the rule that found it, or a model finding's tag (``Finding.rule``).
    ``fingerprint`` is its stable identity (``fingerprint.fingerprint_for``).
    """

    finding: Finding
    source: Source
    rule: RuleId | None
    fingerprint: str

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
