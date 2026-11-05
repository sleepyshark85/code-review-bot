"""`provider_factory`, one per provider: the only provider-specific code in this directory."""

from typing import Literal

import pytest

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.providers.base import LLMProvider
from review_bot.providers.mock import MockProvider

type Scenario = Literal["valid", "empty", "invalid"] | ErrorCategory


def mock_provider(scenario: Scenario) -> LLMProvider:
    if scenario == "valid":
        return MockProvider()
    if scenario == "empty":
        return MockProvider(findings=[])
    if scenario == "invalid":
        return MockProvider(raw_output="not json")
    return MockProvider(error=ProviderError(scenario, "detail"))


PROVIDER_FACTORIES = {"mock": mock_provider}


@pytest.fixture(params=PROVIDER_FACTORIES.values(), ids=PROVIDER_FACTORIES.keys())
def provider_factory(request):
    return request.param
