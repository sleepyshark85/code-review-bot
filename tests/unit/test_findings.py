import json

import pytest
from pydantic import ValidationError

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Category, Finding, Severity, parse_findings


def valid_fields(**overrides: object) -> dict[str, object]:
    return {
        "file": "a.py",
        "line": 3,
        "severity": "high",
        "category": "security",
        "rationale": "Why it matters.",
        "confidence": 0.5,
        **overrides,
    }


def test_severity_values_are_lower_case_in_ascending_order() -> None:
    assert list(Severity) == ["low", "medium", "high", "critical"]


def test_category_values_are_lower_case() -> None:
    assert list(Category) == ["logic", "security", "reliability", "maintainability", "testing"]


@pytest.mark.parametrize(
    "override",
    [
        {"file": ""},
        {"line": 0},
        {"severity": "urgent"},
        {"category": "style"},
        {"rationale": ""},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"extra": "field"},
    ],
)
def test_finding_with_invalid_field_is_rejected(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Finding.model_validate(valid_fields(**override))


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_finding_accepts_confidence_at_its_bounds(confidence: float) -> None:
    assert Finding.model_validate(valid_fields(confidence=confidence)).confidence == confidence


def test_finding_is_frozen() -> None:
    finding = Finding.model_validate(valid_fields())

    with pytest.raises(ValidationError):
        finding.line = 4


def test_parse_findings_with_valid_json_returns_findings() -> None:
    raw = json.dumps({"findings": [valid_fields(), valid_fields(line=9, confidence=1)]})

    findings = parse_findings(raw)

    assert [finding.line for finding in findings] == [3, 9]
    assert findings[0].severity is Severity.HIGH
    assert findings[0].category is Category.SECURITY


def test_parse_findings_with_empty_list_returns_nothing() -> None:
    assert parse_findings('{"findings": []}') == ()


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",
        "[]",
        '{"findings": [], "extra": 1}',
        json.dumps({"findings": [valid_fields(line="3")]}),
        json.dumps({"findings": [valid_fields(line=True)]}),
        json.dumps({"findings": [valid_fields(), valid_fields(line=0)]}),
        "```json\n" + '{"findings": []}' + "\n```",
        json.dumps({"findings": [valid_fields(file=" ")]}),
        json.dumps({"findings": [valid_fields(rationale="\n\t ")]}),
    ],
    ids=[
        "not-json",
        "no-findings-key",
        "bare-list",
        "extra-top-level-field",
        "line-as-string",
        "line-as-boolean",
        "one-valid-one-invalid",
        "code-fence",
        "blank-file",
        "blank-rationale",
    ],
)
def test_parse_findings_with_invalid_output_rejects_it_whole(raw: str) -> None:
    with pytest.raises(ProviderError) as caught:
        parse_findings(raw)

    assert caught.value.category is ErrorCategory.INVALID_OUTPUT
