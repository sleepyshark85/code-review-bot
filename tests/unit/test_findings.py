import json

import pytest
from pydantic import ValidationError

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import (
    Category,
    EvidenceSpan,
    Finding,
    ReviewedFinding,
    RuleId,
    Severity,
    Source,
    parse_findings,
)


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
        {"file": "a\ud800.py"},
        {"rationale": "lone \udcff surrogate"},
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


@pytest.mark.parametrize(
    ("name", "value"), [("source", "rule"), ("fingerprint", "0123456789abcdef")]
)
def test_parse_findings_rejects_a_field_only_code_may_set(name: str, value: str) -> None:
    raw = json.dumps({"findings": [valid_fields(**{name: value})]})

    with pytest.raises(ProviderError) as caught:
        parse_findings(raw)

    assert caught.value.category is ErrorCategory.INVALID_OUTPUT


@pytest.mark.parametrize("rule", list(RuleId))
def test_parse_findings_accepts_a_rule_id_as_the_models_tag(rule: RuleId) -> None:
    [finding] = parse_findings(json.dumps({"findings": [valid_fields(rule=rule.value)]}))

    assert finding.rule is rule


@pytest.mark.parametrize("raw_rule", [{"rule": None}, {}], ids=["null", "absent"])
def test_parse_findings_reads_a_null_or_absent_rule_as_untagged(raw_rule: dict[str, None]) -> None:
    [finding] = parse_findings(json.dumps({"findings": [valid_fields(**raw_rule)]}))

    assert finding.rule is None


@pytest.mark.parametrize("rule", ["made-up", "secret", "SWALLOWED-EXCEPTION", ""])
def test_parse_findings_rejects_a_rule_that_is_not_a_rule_id(rule: str) -> None:
    with pytest.raises(ProviderError) as caught:
        parse_findings(json.dumps({"findings": [valid_fields(rule=rule)]}))

    assert caught.value.category is ErrorCategory.INVALID_OUTPUT


def evidence_span(
    start: int = 12, end: int = 12, file: str = "billing/checkout.py"
) -> dict[str, object]:
    return {"file": file, "start_line": start, "end_line": end}


def test_parse_findings_reads_evidence_spans_in_the_order_given() -> None:
    spans = [evidence_span(10, 14), evidence_span(3, 3, file="a.py")]

    [finding] = parse_findings(json.dumps({"findings": [valid_fields(evidence=spans)]}))

    assert finding.evidence == (
        EvidenceSpan(file="billing/checkout.py", start_line=10, end_line=14),
        EvidenceSpan(file="a.py", start_line=3, end_line=3),
    )


def test_parse_findings_reads_absent_evidence_as_empty() -> None:
    [finding] = parse_findings(json.dumps({"findings": [valid_fields()]}))

    assert finding.evidence == ()


def test_parse_findings_accepts_five_evidence_spans() -> None:
    raw = json.dumps({"findings": [valid_fields(evidence=[evidence_span()] * 5)]})

    assert len(parse_findings(raw)[0].evidence) == 5


@pytest.mark.parametrize(
    "evidence",
    [
        [evidence_span()] * 6,
        [evidence_span(13, 12)],
        [evidence_span(0, 12)],
        [evidence_span(1, 0)],
        [evidence_span(file="")],
        [{**evidence_span(), "text": "x = 1"}],
        [{"file": "a.py", "start_line": 1}],
        [evidence_span(start="12")],  # type: ignore[arg-type]
        evidence_span(),
    ],
    ids=[
        "six-spans",
        "start-after-end",
        "start-below-one",
        "end-below-one",
        "empty-file",
        "extra-field",
        "missing-end",
        "line-as-string",
        "not-a-list",
    ],
)
def test_parse_findings_rejects_invalid_evidence_whole(evidence: object) -> None:
    raw = json.dumps({"findings": [valid_fields(), valid_fields(evidence=evidence)]})

    with pytest.raises(ProviderError) as caught:
        parse_findings(raw)

    assert caught.value.category is ErrorCategory.INVALID_OUTPUT


def test_severity_rank_follows_the_ascending_order() -> None:
    assert [severity.rank for severity in Severity] == [0, 1, 2, 3]


@pytest.mark.parametrize(
    ("rule", "expected"),
    [(RuleId.SWALLOWED_EXCEPTION, "swallowed-exception"), (None, "ai:security")],
)
def test_reviewed_finding_rule_part_is_its_rule_else_ai_and_its_category(
    rule: RuleId | None, expected: str
) -> None:
    finding = Finding(
        file="a.py",
        line=1,
        severity=Severity.LOW,
        category=Category.SECURITY,
        rationale="Why.",
        confidence=0.5,
    )

    assert ReviewedFinding(finding, Source.AI, rule, "0" * 16).rule_part == expected
