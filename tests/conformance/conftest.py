"""`provider_factory`, one per provider: the only provider-specific code in this directory."""

from typing import Literal

import pytest
from claude_agent_sdk import CLINotFoundError
from fake_sdk import (
    HIGH_FINDING,
    FakeSdk,
    assistant_error,
    cli_stderr,
    error_result,
    findings_json,
    result_message,
)

from review_bot import cli
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.providers.base import LLMProvider
from review_bot.providers.claude_agent import ClaudeAgentProvider
from review_bot.providers.mock import MockProvider

type Scenario = Literal["valid", "empty", "invalid", "warned", "diagnosed"] | ErrorCategory

# P1-S5: "warned" succeeds with one warning; "diagnosed" fails with `auth` and one CLI line.
DIAGNOSTIC_LINE = "conformance diagnostic line"


def mock_provider(scenario: Scenario) -> LLMProvider:
    if scenario == "valid":
        return MockProvider()
    if scenario == "empty":
        return MockProvider(findings=[])
    if scenario == "invalid":
        return MockProvider(raw_output="not json")
    if scenario == "warned":
        return MockProvider(warnings=("conformance warning",))
    if scenario == "diagnosed":
        error = ProviderError(ErrorCategory.AUTH, "detail", diagnostics=(DIAGNOSTIC_LINE,))
        return MockProvider(error=error)
    return MockProvider(error=ProviderError(scenario, "detail"))


USAGE = {"input_tokens": 2, "cache_read_input_tokens": 50, "output_tokens": 7}
CLAUDE_SCRIPTS = {
    "valid": (result_message(findings_json(HIGH_FINDING), usage=USAGE),),
    "empty": (result_message(findings_json(), usage=USAGE),),
    "invalid": (result_message("not json", usage=USAGE),),
    "warned": (assistant_error("rate_limit"), result_message(findings_json(), usage=USAGE)),
    "diagnosed": (cli_stderr(DIAGNOSTIC_LINE), error_result(401, "api_error")),
    ErrorCategory.AUTH: (error_result(401, "api_error"),),
    ErrorCategory.RATE_LIMIT: (error_result(429, "api_error"),),
    ErrorCategory.REFUSAL: (result_message("I won't.", stop_reason="refusal"),),
    ErrorCategory.TRANSIENT: (error_result(503, "api_error"),),
    ErrorCategory.PERMANENT: (error_result(400, "api_error"),),
    ErrorCategory.UNAVAILABLE: (CLINotFoundError(),),
    ErrorCategory.INVALID_OUTPUT: (result_message("not json"),),
}


def claude_provider(scenario: Scenario) -> LLMProvider:
    _, settings = cli.parse_args(["change.diff", "--provider", "claude"])
    return ClaudeAgentProvider(settings, sdk_call=FakeSdk(*CLAUDE_SCRIPTS[scenario]))


PROVIDER_FACTORIES = {"mock": mock_provider, "claude": claude_provider}


@pytest.fixture(params=PROVIDER_FACTORIES.values(), ids=PROVIDER_FACTORIES.keys())
def provider_factory(request):
    return request.param


@pytest.fixture
def provider_factories():
    return PROVIDER_FACTORIES


@pytest.fixture
def diagnostic_line():
    """The CLI line the "diagnosed" scenario carries, for either provider."""
    return DIAGNOSTIC_LINE
