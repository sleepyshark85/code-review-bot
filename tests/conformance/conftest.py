"""`provider_factory`, one per provider: the only provider-specific code in this directory."""

from collections.abc import Callable
from typing import Literal

import pytest
from claude_agent_sdk import CLINotFoundError
from fake_mcp import CHECKOUT, ToolCallLog, init_message, tool_call
from fake_openai import (
    KEY,
    api_error,
    completion,
    connection_failure,
    fake,
    findings_content,
    http,
    tool_calls_completion,
)
from fake_openai import tool_call as function_call
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
from review_bot.findings import Finding
from review_bot.providers.base import LLMProvider
from review_bot.providers.claude_agent import ClaudeAgentProvider
from review_bot.providers.mock import MockProvider
from review_bot.providers.openai_compat import OpenAICompatibleProvider

type Scenario = (
    Literal["valid", "empty", "invalid", "warned", "diagnosed", "tool_loop"] | ErrorCategory
)

# P1-S5: "warned" succeeds with one warning; "diagnosed" fails with `auth` and one CLI line.
DIAGNOSTIC_LINE = "conformance diagnostic line"
# P3-S3 AC7: "tool_loop" makes this one read through the request's tools, then returns
# `HIGH_FINDING`.
TOOL_LOOP_CALL = ("read", {"path": CHECKOUT})


# Built per call, so no test shares an error or finding object with another.
MOCK_SCRIPTS: dict[str, Callable[[], dict[str, object]]] = {
    "valid": lambda: {},
    "empty": lambda: {"findings": []},
    "invalid": lambda: {"raw_output": "not json"},
    "warned": lambda: {"warnings": ("conformance warning",)},
    "diagnosed": lambda: {
        "error": ProviderError(ErrorCategory.AUTH, "detail", diagnostics=(DIAGNOSTIC_LINE,))
    },
    "tool_loop": lambda: {"tool_calls": [TOOL_LOOP_CALL], "findings": [Finding(**HIGH_FINDING)]},
}


def mock_provider(scenario: Scenario) -> LLMProvider:
    if scenario in MOCK_SCRIPTS:
        return MockProvider(**MOCK_SCRIPTS[scenario]())
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
    if scenario == "tool_loop":
        name, arguments = TOOL_LOOP_CALL
        script: tuple[object, ...] = (
            init_message(),
            tool_call(name, arguments, ToolCallLog()),
            result_message(findings_json(HIGH_FINDING), usage=USAGE),
        )
    else:
        script = CLAUDE_SCRIPTS[scenario]
    return ClaudeAgentProvider(settings, sdk_call=FakeSdk(*script))


# P4-S2 AC5: `openai` over a scripted `HttpTransport`. Error scenarios make no retry, so each
# fails on its one response; "warned" is a 503 retried once, then a success. P4-S6 AC9:
# "tool_loop" asks for the read by function calling, then answers with the finding.
OPENAI_SCRIPTS: dict[str, Callable[[], tuple[object, ...]]] = {
    "valid": lambda: (completion(findings_content(HIGH_FINDING)),),
    "empty": lambda: (completion(findings_content()),),
    "invalid": lambda: (completion("not json"),),
    "warned": lambda: (http(503, retry_after="0"), completion(findings_content())),
    "diagnosed": lambda: (http(401, DIAGNOSTIC_LINE),),
    ErrorCategory.AUTH: lambda: (api_error(401),),
    ErrorCategory.RATE_LIMIT: lambda: (api_error(429),),
    ErrorCategory.REFUSAL: lambda: (completion(None, finish_reason="content_filter"),),
    ErrorCategory.TRANSIENT: lambda: (api_error(503),),
    ErrorCategory.PERMANENT: lambda: (api_error(400),),
    ErrorCategory.UNAVAILABLE: lambda: (connection_failure("refused"),),
    ErrorCategory.INVALID_OUTPUT: lambda: (completion("not json"),),
    "tool_loop": lambda: (
        tool_calls_completion(function_call("call_1", *TOOL_LOOP_CALL)),
        completion(findings_content(HIGH_FINDING)),
    ),
}


def openai_provider(scenario: Scenario) -> LLMProvider:
    retries = "1" if scenario == "warned" else "0"
    _, settings = cli.parse_args(
        ["change.diff", "--provider", "openai", "--model", "m1", "--max-retries", retries]
    )
    transport = fake(*OPENAI_SCRIPTS[scenario]())
    return OpenAICompatibleProvider(
        settings, base_url=settings.openai_base_url, api_key=KEY, transport=transport
    )


PROVIDER_FACTORIES = {"mock": mock_provider, "claude": claude_provider, "openai": openai_provider}


@pytest.fixture(params=PROVIDER_FACTORIES.values(), ids=PROVIDER_FACTORIES.keys())
def provider_factory(request):
    return request.param


@pytest.fixture
def provider_factories():
    return PROVIDER_FACTORIES


@pytest.fixture
def diagnostic_line():
    """The CLI line the "diagnosed" scenario carries, for every provider."""
    return DIAGNOSTIC_LINE
