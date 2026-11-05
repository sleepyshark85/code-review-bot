import asyncio
import json
from collections.abc import AsyncGenerator
from typing import cast

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from review_bot.providers.base import ReviewRequest, TokenUsage
from review_bot.providers.claude_agent import ClaudeAgentProvider
from review_bot.settings import Settings

REQUEST = ReviewRequest(system_prompt="S", user_prompt="U", model="M")

VALID_RESULT = json.dumps(
    {
        "findings": [
            {
                "file": "a.py",
                "line": 1,
                "severity": "high",
                "category": "logic",
                "rationale": "Why.",
                "confidence": 0.5,
            }
        ]
    }
)


def result_message(
    usage: dict[str, object] | None = None, *, result: str = '{"findings": []}'
) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="session",
        result=result,
        usage=usage,
    )


class FakeQuery:
    """Stands in for the SDK's ``query``, optionally yielding a result, then never ending.

    It is a single generator, so tests using it show that ``query`` itself gets closed, not that
    the real SDK's subprocess stops: that happens at event loop shutdown.
    """

    def __init__(self, *messages: ResultMessage) -> None:
        self.messages = messages
        self.prompt: str | None = None
        self.options: ClaudeAgentOptions | None = None
        self.is_closed = False

    async def __call__(
        self, *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncGenerator[ResultMessage, None]:
        self.prompt, self.options = prompt, options
        try:
            for message in self.messages:
                yield message
            await asyncio.Event().wait()
        finally:
            self.is_closed = True


async def test_default_sdk_call_passes_prompt_and_options_to_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_query = FakeQuery(result_message())
    monkeypatch.setattr("review_bot.providers.claude_agent.query", fake_query)

    await ClaudeAgentProvider(Settings()).review(REQUEST)

    assert fake_query.prompt == "U"
    assert fake_query.options is not None
    assert fake_query.options.max_turns == 1


async def test_default_sdk_call_after_a_result_closes_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_query = FakeQuery(result_message())
    monkeypatch.setattr("review_bot.providers.claude_agent.query", fake_query)

    await ClaudeAgentProvider(Settings()).review(REQUEST)

    assert fake_query.is_closed


async def test_default_sdk_call_past_the_deadline_closes_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_query = FakeQuery()
    monkeypatch.setattr("review_bot.providers.claude_agent.query", fake_query)

    with pytest.raises(TimeoutError):
        await ClaudeAgentProvider(Settings(deadline_s=0.01)).review(REQUEST)

    assert fake_query.is_closed


async def test_review_with_null_usage_fields_counts_them_as_zero() -> None:
    usage: dict[str, object] = {
        "input_tokens": 2,
        "cache_creation_input_tokens": None,
        "output_tokens": None,
    }

    async def sdk_call(
        _prompt: str, _options: ClaudeAgentOptions
    ) -> AsyncGenerator[ResultMessage, None]:
        yield result_message(usage)

    response = await ClaudeAgentProvider(Settings(), sdk_call=sdk_call).review(REQUEST)

    assert response.usage == TokenUsage(2, 0)


@pytest.mark.parametrize("count", [-5, "7", "", 1.5, True, [1]])
@pytest.mark.parametrize("field", ["cache_read_input_tokens", "output_tokens"])
async def test_review_with_bad_token_count_keeps_the_findings_and_counts_zero(
    field: str, count: object
) -> None:
    async def sdk_call(
        _prompt: str, _options: ClaudeAgentOptions
    ) -> AsyncGenerator[ResultMessage, None]:
        yield result_message({field: count}, result=VALID_RESULT)

    response = await ClaudeAgentProvider(Settings(), sdk_call=sdk_call).review(REQUEST)

    assert len(response.findings) == 1
    assert response.usage == TokenUsage(0, 0)


@pytest.mark.parametrize("usage", ["junk", [1]])
async def test_review_with_usage_that_is_not_a_mapping_keeps_the_findings_and_counts_zero(
    usage: object,
) -> None:
    async def sdk_call(
        _prompt: str, _options: ClaudeAgentOptions
    ) -> AsyncGenerator[ResultMessage, None]:
        yield result_message(cast(dict[str, object], usage), result=VALID_RESULT)

    response = await ClaudeAgentProvider(Settings(), sdk_call=sdk_call).review(REQUEST)

    assert len(response.findings) == 1
    assert response.usage == TokenUsage(0, 0)
