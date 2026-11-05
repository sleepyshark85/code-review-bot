"""The Claude provider: the only module that imports the Claude Agent SDK (ADR 0001)."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterable, Callable, Mapping
from contextlib import aclosing
from typing import Final, cast

from claude_agent_sdk import ClaudeAgentOptions, Message, ResultMessage, query

from review_bot.errors import ReviewBotError
from review_bot.findings import parse_findings
from review_bot.providers.base import ReviewRequest, ReviewResponse, TokenUsage
from review_bot.settings import Settings

__all__ = ["ClaudeAgentProvider", "SdkCall"]

type SdkCall = Callable[[str, ClaudeAgentOptions], AsyncGenerator[object, None]]

# ``usage["input_tokens"]`` excludes tokens served from or written to the prompt cache.
_INPUT_TOKEN_FIELDS: Final = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class _ClaudeCallError(ReviewBotError):
    """A call failed. Uncategorised until P0-S4, so ``main`` reports it as an internal error."""


class ClaudeAgentProvider:
    """Reviews with a locked-down Claude: one turn, no tools, no settings, no MCP servers."""

    def __init__(self, settings: Settings, *, sdk_call: SdkCall | None = None) -> None:
        """Prepare the provider; nothing starts until ``review``.

        Args:
            settings: Supplies the CLI's retry and timeout limits, and the overall deadline.
            sdk_call: Replaces the real SDK call, so tests stay offline.
        """
        self._settings = settings
        self._sdk_call = _call_sdk if sdk_call is None else sdk_call

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        """Make one call, leaving retries to the CLI, and return its validated findings.

        Raises:
            TimeoutError: If the deadline passes first.
            _ClaudeCallError: If Claude reports a failed call, or the stream ends without a result.
            ProviderError: With category ``invalid_output``, if the model's text isn't valid
                findings.
        """
        stream = self._sdk_call(request.user_prompt, _build_options(request, self._settings))
        async with asyncio.timeout(self._settings.deadline_s), aclosing(stream):
            result = await _read_result(stream)

        if result.is_error:
            # Judged on ``is_error`` because a failed call's subtype can still be "success". Its
            # ``result`` holds untrusted CLI output, so it stays out of the message.
            raise _ClaudeCallError("Claude reported that the call failed.")
        return ReviewResponse(
            findings=parse_findings(result.result or ""), usage=_token_usage(result.usage)
        )


def _build_options(request: ReviewRequest, settings: Settings) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=request.system_prompt,
        model=request.model,
        tools=[],
        allowed_tools=[],
        setting_sources=[],
        mcp_servers={},
        strict_mcp_config=True,
        hooks=None,
        agents=None,
        max_turns=1,
        env={
            "CLAUDE_CODE_MAX_RETRIES": str(settings.max_retries),
            "API_TIMEOUT_MS": str(settings.api_timeout_ms),
        },
        stderr=_discard_cli_stderr,
    )


def _discard_cli_stderr(_line: str) -> None:
    """Drop the CLI's stderr: it is untrusted, and only our own lines may reach the user."""


async def _read_result(stream: AsyncIterable[object]) -> ResultMessage:
    async for message in stream:
        if isinstance(message, ResultMessage):
            return message
    raise _ClaudeCallError("The Claude stream ended without a result.")


def _token_usage(usage: object) -> TokenUsage:
    """Read the CLI's untrusted usage. Bad usage counts as 0: it never costs a valid review."""
    if not isinstance(usage, Mapping):
        return TokenUsage(0, 0)
    return TokenUsage(
        input_tokens=sum(_token_count(usage, field) for field in _INPUT_TOKEN_FIELDS),
        output_tokens=_token_count(usage, "output_tokens"),
    )


def _token_count(usage: Mapping[str, object], field: str) -> int:
    count = usage.get(field)
    # ``type``, not ``isinstance``, because a bool is an int.
    return count if type(count) is int and count >= 0 else 0


async def _call_sdk(prompt: str, options: ClaudeAgentOptions) -> AsyncGenerator[object, None]:
    # ``query`` is an async generator annotated only as AsyncIterator. An async ``for`` doesn't
    # close its iterator, so closing this wrapper closes ``query`` explicitly. That is as far as
    # it reaches: ``query`` doesn't close the inner generator owning the subprocess, so after an
    # early exit the CLI is reaped when ``asyncio.run`` shuts the loop down.
    messages = cast(AsyncGenerator[Message, None], query(prompt=prompt, options=options))
    async with aclosing(messages):
        async for message in messages:
            yield message
