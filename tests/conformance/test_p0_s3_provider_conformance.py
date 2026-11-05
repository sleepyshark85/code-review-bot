"""P0-S3 AC8 and AC9: what every `LLMProvider` must do, whichever provider it is."""

import dataclasses
import os
import shutil
from decimal import Decimal

import pytest
from pydantic import BaseModel

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.providers.base import ReviewRequest, ReviewResponse, TokenUsage

pytestmark = pytest.mark.integration

REQUEST = ReviewRequest(system_prompt="Review.", user_prompt="+x", model="claude-opus-5-5")


def foreign_types(value):
    """Every type reachable from `value` that is neither built in nor from `review_bot`."""
    kind = type(value)
    if kind.__module__.partition(".")[0] not in {"builtins", "review_bot"}:
        return [kind]
    if dataclasses.is_dataclass(value):
        children = [getattr(value, field.name) for field in dataclasses.fields(value)]
    elif isinstance(value, BaseModel):
        children = [getattr(value, name) for name in kind.model_fields]
    elif isinstance(value, dict):
        children = [*value.keys(), *value.values()]
    elif isinstance(value, list | tuple | set | frozenset):
        children = list(value)
    else:
        children = []
    return [found for child in children for found in foreign_types(child)]


async def test_p0_s3_ac8_valid_returns_findings_and_non_negative_usage(provider_factory):
    response = await provider_factory("valid").review(REQUEST)

    assert isinstance(response, ReviewResponse)
    assert response.findings
    assert all(isinstance(finding, Finding) for finding in response.findings)
    assert response.usage.input_tokens >= 0
    assert response.usage.output_tokens >= 0


async def test_p0_s3_ac8_empty_returns_no_findings(provider_factory):
    response = await provider_factory("empty").review(REQUEST)

    assert response.findings == ()


async def test_p0_s3_ac8_invalid_raises_invalid_output(provider_factory):
    with pytest.raises(ProviderError) as caught:
        await provider_factory("invalid").review(REQUEST)

    assert caught.value.category is ErrorCategory.INVALID_OUTPUT


@pytest.mark.parametrize("category", list(ErrorCategory))
async def test_p0_s3_ac8_each_category_raises_that_category(provider_factory, category):
    with pytest.raises(ProviderError) as caught:
        await provider_factory(category).review(REQUEST)

    assert caught.value.category is category


@pytest.mark.parametrize("scenario", ["valid", "empty"])
async def test_p0_s3_ac8_response_holds_only_our_types_or_built_ins(provider_factory, scenario):
    response = await provider_factory(scenario).review(REQUEST)

    assert foreign_types(response) == []


@pytest.mark.parametrize(
    "response",
    [
        ReviewResponse(findings=(), usage=TokenUsage(Decimal(1), 0)),
        ReviewResponse(findings=(Decimal(1),), usage=TokenUsage(0, 0)),
    ],
    ids=["in_usage", "in_findings"],
)
def test_p0_s3_ac8_control_a_foreign_type_is_noticed(response):
    assert foreign_types(response) == [Decimal]


def test_p0_s3_ac9_conformance_runs_without_claude_or_a_key():
    assert shutil.which("claude") is None
    assert "ANTHROPIC_API_KEY" not in os.environ
