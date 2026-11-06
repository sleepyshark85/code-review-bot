"""P1-S5 AC11: both providers fill `warnings` and `diagnostics` with `str` only, or leave them
empty.

The P0-S3 suite still runs over both providers unchanged; these cases add the two new fields.
"""

import pytest

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.providers.base import ReviewRequest, ReviewResponse, TokenUsage

pytestmark = pytest.mark.integration

REQUEST = ReviewRequest(system_prompt="Review.", user_prompt="+x", model="claude-opus-5-5")
FAILURES = list(ErrorCategory)


def only_str(values) -> bool:
    return all(type(value) is str for value in values)


async def failure(provider) -> ProviderError:
    with pytest.raises(ProviderError) as raised:
        await provider.review(REQUEST)
    return raised.value


@pytest.mark.parametrize("scenario", ["valid", "empty"])
async def test_p1_s5_ac11_a_plain_success_has_no_warnings(provider_factory, scenario):
    response = await provider_factory(scenario).review(REQUEST)

    assert tuple(response.warnings) == ()


async def test_p1_s5_ac11_a_warned_success_holds_only_str_warnings(provider_factory):
    response = await provider_factory("warned").review(REQUEST)

    assert len(response.warnings) == 1
    assert only_str(response.warnings)


@pytest.mark.parametrize("category", FAILURES, ids=str)
async def test_p1_s5_ac11_every_failure_holds_only_str_diagnostics(provider_factory, category):
    error = await failure(provider_factory(category))

    assert only_str(error.diagnostics)


async def test_p1_s5_ac11_a_failure_with_cli_output_carries_it_as_str(
    provider_factory, diagnostic_line
):
    error = await failure(provider_factory("diagnosed"))

    assert error.category is ErrorCategory.AUTH
    assert only_str(error.diagnostics)
    assert diagnostic_line in error.diagnostics


async def test_p1_s5_ac11_invalid_output_holds_only_str_diagnostics(provider_factory):
    error = await failure(provider_factory("invalid"))

    assert only_str(error.diagnostics)


def test_p1_s5_ac11_the_new_fields_default_to_empty():
    response = ReviewResponse(findings=(), usage=TokenUsage(0, 0))
    error = ProviderError(ErrorCategory.AUTH, "m")

    assert tuple(response.warnings) == ()
    assert tuple(error.diagnostics) == ()


def test_p1_s5_ac11_diagnostics_given_as_a_list_keep_their_order():
    error = ProviderError(ErrorCategory.AUTH, "m", diagnostics=["one", "two"])

    assert list(error.diagnostics) == ["one", "two"]
    assert only_str(error.diagnostics)
