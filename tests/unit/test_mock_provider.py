import pytest

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Category, Finding, Severity
from review_bot.providers.base import ReviewRequest, TokenUsage
from review_bot.providers.mock import MockProvider

REQUEST = ReviewRequest(system_prompt="S", user_prompt="U", model="M")

SCRIPTED_FINDING = Finding(
    file="b.py",
    line=2,
    severity=Severity.LOW,
    category=Category.TESTING,
    rationale="Scripted.",
    confidence=0.5,
)


async def test_review_records_every_request() -> None:
    provider = MockProvider()

    await provider.review(REQUEST)
    await provider.review(REQUEST)

    assert provider.requests == [REQUEST, REQUEST]


async def test_review_with_no_script_returns_one_canned_finding() -> None:
    response = await MockProvider().review(REQUEST)

    assert [finding.file for finding in response.findings] == ["mock/example.py"]
    assert response.usage == TokenUsage(0, 0)


async def test_review_with_findings_returns_them() -> None:
    response = await MockProvider(findings=[SCRIPTED_FINDING]).review(REQUEST)

    assert response.findings == (SCRIPTED_FINDING,)


async def test_review_with_empty_findings_returns_none_rather_than_canned() -> None:
    response = await MockProvider(findings=[]).review(REQUEST)

    assert response.findings == ()


async def test_review_with_raw_output_parses_it() -> None:
    raw = f'{{"findings": [{SCRIPTED_FINDING.model_dump_json()}]}}'

    response = await MockProvider(raw_output=raw).review(REQUEST)

    assert response.findings == (SCRIPTED_FINDING,)


async def test_review_with_invalid_raw_output_rejects_it() -> None:
    with pytest.raises(ProviderError) as caught:
        await MockProvider(raw_output="not json").review(REQUEST)

    assert caught.value.category is ErrorCategory.INVALID_OUTPUT


async def test_review_with_error_raises_it_and_still_records_the_request() -> None:
    provider = MockProvider(error=ProviderError(ErrorCategory.AUTH, "detail"))

    with pytest.raises(ProviderError, match="detail") as caught:
        await provider.review(REQUEST)

    assert caught.value.category is ErrorCategory.AUTH
    assert provider.requests == [REQUEST]


async def test_review_with_warnings_returns_them_with_the_findings() -> None:
    response = await MockProvider(findings=[], warnings=["W1", "W2"]).review(REQUEST)

    assert response.warnings == ("W1", "W2")


async def test_review_with_no_warnings_returns_none() -> None:
    response = await MockProvider().review(REQUEST)

    assert response.warnings == ()


def test_provider_error_holds_its_diagnostics_as_a_tuple() -> None:
    error = ProviderError(ErrorCategory.AUTH, "m", diagnostics=["d"])

    assert error.diagnostics == ("d",)
    assert str(error) == "m"
    assert ProviderError(ErrorCategory.AUTH, "m").diagnostics == ()
