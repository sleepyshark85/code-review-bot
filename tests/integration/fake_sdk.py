"""A scripted `sdk_call` over the real SDK classes, the seam P0-S2 names for offline tests."""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

HIGH_FINDING = {
    "file": "src/app.py",
    "line": 7,
    "severity": "high",
    "category": "logic",
    "rationale": "Rationale text.",
    "confidence": 0.9,
}


def findings_json(*findings: dict[str, Any]) -> str:
    return json.dumps({"findings": list(findings)})


def result_message(
    result: str | None,
    *,
    is_error: bool = False,
    subtype: str = "success",
    usage: dict[str, Any] | None = None,
    errors: list[str] | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="session",
        usage=usage,
        result=result,
        errors=errors,
    )


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
