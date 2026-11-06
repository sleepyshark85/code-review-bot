"""A scripted `sdk_call` over the real SDK classes, the seam P0-S2 names for offline tests."""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultError,
    ResultMessage,
    TextBlock,
)
from unit.diff_samples import FINDING_FIELDS

# The shared default finding; `claude_argv`'s diff holds its anchor, so it prints (P1-S6).
HIGH_FINDING = dict(FINDING_FIELDS)


def findings_json(*findings: dict[str, Any]) -> str:
    return json.dumps({"findings": list(findings)})


def result_message(
    result: str | None, *, is_error: bool = False, subtype: str = "success", **fields: Any
) -> ResultMessage:
    """`fields` are any other `ResultMessage` fields, such as `usage` or `stop_reason`."""
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="session",
        result=result,
        **fields,
    )


def error_result(
    status: Any = None, terminal_reason: str | None = None, result: str | None = "API Error prose."
) -> ResultMessage:
    """The shape the CLI really sends for a failed API call: `success`, but `is_error=True`."""
    return result_message(
        result,
        is_error=True,
        stop_reason="stop_sequence",
        api_error_status=status,
        terminal_reason=terminal_reason,
    )


def raised_result_error(
    status: Any = None,
    terminal_reason: str | None = None,
    text: str = "Claude Code returned an error result: API Error prose.",
    result: str | None = "API Error prose.",
) -> ResultError:
    data = {
        "subtype": "success",
        "is_error": True,
        "api_error_status": status,
        "terminal_reason": terminal_reason,
        "result": result,
    }
    return ResultError(text, data=data, exit_code=1)


def assistant_error(error: Any, text: str = "Assistant text.") -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m", error=error)


def cli_stderr(*lines: str) -> Any:
    """A script step in which the CLI writes `lines` to its stderr, through `options.stderr`."""

    async def step(options: ClaudeAgentOptions) -> None:
        assert options.stderr is not None, "the provider set no stderr callback"
        for line in lines:
            options.stderr(line)

    return step


async def hang(_options: ClaudeAgentOptions) -> None:
    await asyncio.Event().wait()


def pause(seconds: float) -> Any:
    async def step(_options: ClaudeAgentOptions) -> None:
        await asyncio.sleep(seconds)

    return step


class FakeSdk:
    """Records `(prompt, options)`, then plays a script as an async generator.

    A script step is an exception to raise, an async callable to await with the options, or a
    message to yield. `forever` replays the script without end. `calls` is recorded when called
    and `started` when iterated, so a call that is never consumed shows up. `closed` is set only
    when the generator really finishes.
    """

    def __init__(self, *script: Any, forever: bool = False) -> None:
        self.calls: list[tuple[str, ClaudeAgentOptions]] = []
        self.started = False
        self.closed = False
        self._script = script
        self._forever = forever

    def __call__(self, prompt: str, options: ClaudeAgentOptions) -> AsyncGenerator[object, None]:
        self.calls.append((prompt, options))
        return self._stream(options)

    @property
    def options(self) -> ClaudeAgentOptions:
        assert len(self.calls) == 1
        return self.calls[0][1]

    async def _stream(self, options: ClaudeAgentOptions) -> AsyncGenerator[object, None]:
        self.started = True
        try:
            while True:
                for step in self._script:
                    if isinstance(step, BaseException):
                        raise step
                    if callable(step):
                        await step(options)
                    else:
                        yield step
                if not self._forever:
                    return
        finally:
            self.closed = True


def succeeds() -> FakeSdk:
    """A fake for criteria about what is passed in, whatever comes back."""
    return FakeSdk(result_message(findings_json()))
