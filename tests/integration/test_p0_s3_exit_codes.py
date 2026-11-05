"""P0-S3 AC1 to AC3: each provider failure and each kind of invalid output has its exit code."""

import json

import pytest
from diff_support import MINIMAL_DIFF
from fake_sdk import HIGH_FINDING, FakeSdk, findings_json, result_message

from review_bot.errors import ErrorCategory, ProviderError

pytestmark = pytest.mark.integration

SENTINEL = "SENTINEL"
# Spelled out from design.md's table, not read from the code: the codes are a public contract.
EXIT_CODES = {
    ErrorCategory.UNAVAILABLE: 7,
    ErrorCategory.AUTH: 8,
    ErrorCategory.RATE_LIMIT: 9,
    ErrorCategory.REFUSAL: 10,
    ErrorCategory.TRANSIENT: 11,
    ErrorCategory.PERMANENT: 12,
    ErrorCategory.INVALID_OUTPUT: 13,
}
FINDING_WITHOUT_LINE = {name: value for name, value in HIGH_FINDING.items() if name != "line"}

AC2_INVALID_OUTPUT = {
    "not_json": "not json",
    "no_findings_key": "{}",
    "missing_line": findings_json(FINDING_WITHOUT_LINE),
    "line_0": findings_json(HIGH_FINDING | {"line": 0}),
    "severity_urgent": findings_json(HIGH_FINDING | {"severity": "urgent"}),
    "confidence_1_5": findings_json(HIGH_FINDING | {"confidence": 1.5}),
    "extra_field": findings_json(HIGH_FINDING | {"extra": "field"}),
    "valid_plus_invalid": findings_json(HIGH_FINDING, HIGH_FINDING | {"line": 0}),
    "invalid_plus_valid": findings_json(HIGH_FINDING | {"line": 0}, HIGH_FINDING),
    "code_fence": f"```json\n{findings_json(HIGH_FINDING)}\n```",
}
# Beyond AC2's list, from the design's "not bare JSON, or fails the `Finding` schema".
OTHER_INVALID_OUTPUT = {
    "empty": "",
    "whitespace": " \n",
    "null": "null",
    "bare_list": "[]",
    "findings_null": '{"findings": null}',
    "extra_top_level_key": '{"findings": [], "extra": 1}',
    "preamble": f"Here you go: {findings_json()}",
    "nan_confidence": json.dumps({"findings": [HIGH_FINDING | {"confidence": float("nan")}]}),
    "nesting_past_the_recursion_limit": "[" * 200_000,
    "integer_past_the_digit_limit": '{"findings": [{"line": ' + "1" * 5000 + "}]}",
}


def test_p0_s3_ac1_the_table_names_every_category():
    assert set(EXIT_CODES) == set(ErrorCategory)
    assert len(EXIT_CODES) == 7


@pytest.mark.parametrize(("category", "code"), EXIT_CODES.items(), ids=str)
def test_p0_s3_ac1_category_returns_its_code_and_names_itself(bot, write_diff, category, code):
    provider = bot.mock(error=ProviderError(category, "detail"))

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == code, run.stderr
    assert run.stdout == ""
    assert category.value in run.stderr
    assert "detail" in run.stderr


@pytest.mark.parametrize("raw", AC2_INVALID_OUTPUT.values(), ids=AC2_INVALID_OUTPUT.keys())
def test_p0_s3_ac2_invalid_output_exits_13_with_empty_stdout(bot, write_diff, raw):
    run = bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock(raw_output=raw))

    assert run.exit_code == 13, run.stderr
    assert run.stdout == ""


@pytest.mark.parametrize("raw", OTHER_INVALID_OUTPUT.values(), ids=OTHER_INVALID_OUTPUT.keys())
def test_p0_s3_ac2_other_malformed_output_exits_13(bot, write_diff, raw):
    run = bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock(raw_output=raw))

    assert run.exit_code == 13, run.stderr
    assert run.stdout == ""


def test_p0_s3_ac2_empty_findings_exit_0(bot, write_diff):
    run = bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock(raw_output='{"findings": []}'))

    assert run.exit_code == 0, run.stderr


def test_p0_s3_ac2_control_the_finding_the_cases_derive_from_is_valid(bot, write_diff):
    provider = bot.mock(raw_output=findings_json(HIGH_FINDING))

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 1, run.stderr
    assert run.stdout_body.startswith("src/app.py:7 ")


@pytest.mark.parametrize(
    "result",
    [
        None,
        "",
        " \n",
        f"{SENTINEL} not json",
        f"```json\n{findings_json()}\n```",
        f"Here you go: {findings_json()}",
        f'{{"findings": [{{"file": "{SENTINEL}"}}]}}',
        "[]",
    ],
    ids=["none", "empty", "whitespace", "prose", "fenced", "preamble", "fails_schema", "bare_list"],
)
def test_p0_s3_ac3_successful_result_that_is_not_findings_returns_13(claude_review, result):
    run = claude_review(FakeSdk(result_message(result, is_error=False)))

    assert run.exit_code == 13, run.stderr
    assert run.stdout == ""
    assert "Traceback" not in run.stderr
    assert SENTINEL not in run.stderr
