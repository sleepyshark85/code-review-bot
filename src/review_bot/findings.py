"""The review finding schema, and parsing of raw model text into findings."""

from enum import StrEnum, auto

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Category", "Finding", "Severity", "parse_findings"]


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


class _FindingsEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: tuple[Finding, ...]


def parse_findings(raw: str) -> tuple[Finding, ...]:
    """Parse model text that must be bare JSON of the form ``{"findings": [...]}``.

    Validation is strict: model output is rejected whole, never coerced into shape.

    Raises:
        pydantic.ValidationError: If the text isn't JSON or doesn't match the schema.
    """
    return _FindingsEnvelope.model_validate_json(raw, strict=True).findings
