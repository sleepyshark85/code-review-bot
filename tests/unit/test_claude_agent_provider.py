import asyncio
import json
import sys
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ProcessError,
    ResultError,
    ResultMessage,
)
from diff_samples import MINIMAL_DIFF

from review_bot.cli import main
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.providers.base import ReviewRequest, TokenUsage
from review_bot.providers.claude_agent import ClaudeAgentProvider, SdkCall
from review_bot.settings import Settings

REQUEST = ReviewRequest(system_prompt="S", user_prompt="U", model="M")

# Its file and line are in `MINIMAL_DIFF`, so `main` reports it rather than dropping it.
VALID_RESULT = json.dumps(
    {
        "findings": [
            {
                "file": "app.py",
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

    with pytest.raises(ProviderError) as caught:
        await ClaudeAgentProvider(Settings(deadline_s=0.01)).review(REQUEST)

    assert caught.value.category is ErrorCategory.TRANSIENT
    assert fake_query.is_closed


def scripted(*script: object) -> SdkCall:
    """An ``sdk_call`` playing ``script``: a step is raised if it is an exception, else yielded."""

    async def sdk_call(_prompt: str, _options: ClaudeAgentOptions) -> AsyncGenerator[object, None]:
        for step in script:
            if isinstance(step, Exception):
                raise step
            yield step

    return sdk_call


def raised_result_error(status: int, terminal_reason: str) -> ResultError:
    data = {"is_error": True, "api_error_status": status, "terminal_reason": terminal_reason}
    return ResultError("Claude Code returned an error result.", data=data, exit_code=1)


def assistant_error(error: Any) -> AssistantMessage:
    return AssistantMessage(content=[], model="M", error=error)


def failed_result(*, status: object = None, terminal_reason: str | None = None) -> ResultMessage:
    message = result_message()
    message.is_error = True
    message.api_error_status = cast(int | None, status)
    message.terminal_reason = terminal_reason
    return message


async def review_category(*script: object) -> ErrorCategory:
    with pytest.raises(ProviderError) as caught:
        await ClaudeAgentProvider(Settings(), sdk_call=scripted(*script)).review(REQUEST)
    return caught.value.category


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (499, ErrorCategory.PERMANENT),
        (500, ErrorCategory.TRANSIENT),
        (599, ErrorCategory.TRANSIENT),
        (600, ErrorCategory.PERMANENT),
        (200, ErrorCategory.PERMANENT),
    ],
)
async def test_review_with_status_at_a_range_edge_maps_it(
    status: int, category: ErrorCategory
) -> None:
    assert await review_category(failed_result(status=status)) is category


@pytest.mark.parametrize("status", ["401", True, 401.0, [401]])
async def test_review_with_status_that_is_not_an_integer_treats_it_as_absent(
    status: object,
) -> None:
    assert await review_category(failed_result(status=status)) is ErrorCategory.PERMANENT
    assert (
        await review_category(failed_result(status=status, terminal_reason="api_error"))
        is ErrorCategory.TRANSIENT
    )


async def test_review_with_assistant_error_that_is_not_hashable_names_it_as_permanent() -> None:
    provider = ClaudeAgentProvider(Settings(), sdk_call=scripted(assistant_error(["odd-value"])))

    with pytest.raises(ProviderError, match="odd-value") as caught:
        await provider.review(REQUEST)

    assert caught.value.category is ErrorCategory.PERMANENT


async def test_review_with_two_assistant_errors_reports_the_first() -> None:
    category = await review_category(assistant_error("rate_limit"), assistant_error("server_error"))

    assert category is ErrorCategory.RATE_LIMIT


async def test_review_with_failed_result_then_unexpected_exception_keeps_the_signal() -> None:
    category = await review_category(failed_result(status=429), ValueError("bug"))

    assert category is ErrorCategory.RATE_LIMIT


async def test_review_with_failed_result_then_success_returns_findings_with_a_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sdk_call = scripted(failed_result(status=503), result_message(result=VALID_RESULT))

    response = await ClaudeAgentProvider(Settings(), sdk_call=sdk_call).review(REQUEST)

    assert len(response.findings) == 1
    assert response.warnings == (
        "review-bot: warning: Claude signalled transient during the call, but a valid result "
        "followed, so that result stands.",
    )
    assert capsys.readouterr() == ("", "")


async def test_review_with_only_a_success_returns_no_warnings() -> None:
    sdk_call = scripted(result_message(result=VALID_RESULT))

    response = await ClaudeAgentProvider(Settings(), sdk_call=sdk_call).review(REQUEST)

    assert response.warnings == ()


def writing_cli_stderr(lines: Sequence[str], *script: object) -> SdkCall:
    """An ``sdk_call`` whose CLI writes ``lines`` to its stderr, then plays ``script``."""
    play = scripted(*script)

    async def sdk_call(prompt: str, options: ClaudeAgentOptions) -> AsyncGenerator[object, None]:
        assert options.stderr is not None
        for line in lines:
            options.stderr(line)
        async for message in play(prompt, options):
            yield message

    return sdk_call


async def review_diagnostics(
    sdk_call: SdkCall, settings: Settings | None = None
) -> tuple[str, ...]:
    provider = ClaudeAgentProvider(settings or Settings(), sdk_call=sdk_call)
    with pytest.raises(ProviderError) as caught:
        await provider.review(REQUEST)
    return caught.value.diagnostics


@pytest.mark.parametrize(
    "failure",
    [
        failed_result(status=401),
        raised_result_error(401, "api_error"),
        assistant_error("rate_limit"),
        result_message(result="not json"),
        ProcessError("Command failed"),
    ],
    ids=["error-result", "result-error", "assistant-error", "invalid-output", "sdk-error"],
)
async def test_review_that_fails_holds_the_cli_stderr_as_diagnostics(failure: object) -> None:
    diagnostics = await review_diagnostics(writing_cli_stderr(["one", "two"], failure))

    assert diagnostics == ("one", "two")


async def test_review_past_the_deadline_holds_the_cli_stderr_as_diagnostics() -> None:
    async def sdk_call(_prompt: str, options: ClaudeAgentOptions) -> AsyncGenerator[object, None]:
        assert options.stderr is not None
        options.stderr("before the hang")
        await asyncio.Event().wait()
        yield result_message()

    diagnostics = await review_diagnostics(sdk_call, Settings(deadline_s=0.01))

    assert diagnostics == ("before the hang",)


async def test_review_that_fails_holds_only_the_last_1000_cli_stderr_lines() -> None:
    lines = [f"line {number}" for number in range(1005)]

    diagnostics = await review_diagnostics(writing_cli_stderr(lines, failed_result(status=401)))

    assert diagnostics == tuple(lines[-1000:])


async def test_review_that_succeeds_with_cli_stderr_writes_it_nowhere(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sdk_call = writing_cli_stderr(["CLI-SENTINEL"], result_message(result=VALID_RESULT))

    response = await ClaudeAgentProvider(Settings(), sdk_call=sdk_call).review(REQUEST)

    assert "CLI-SENTINEL" not in repr(response)
    assert capsys.readouterr() == ("", "")


async def test_review_with_success_then_failure_closing_the_stream_keeps_the_findings() -> None:
    async def sdk_call(_prompt: str, _options: ClaudeAgentOptions) -> AsyncGenerator[object, None]:
        try:
            yield result_message(result=VALID_RESULT)
        finally:
            raise ProcessError("close failed")

    response = await ClaudeAgentProvider(Settings(), sdk_call=sdk_call).review(REQUEST)

    assert len(response.findings) == 1


@pytest.mark.parametrize(
    "stderr", [None, SimpleNamespace(encoding="no-such-codec")], ids=["closed", "failing"]
)
def test_main_with_overridden_signal_and_unavailable_stderr_exits_by_the_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: object
) -> None:
    diff_path = tmp_path / "change.diff"
    diff_path.write_text(MINIMAL_DIFF, encoding="utf-8")
    monkeypatch.setattr("sys.stderr", stderr)
    sdk_call = scripted(assistant_error("rate_limit"), result_message(result=VALID_RESULT))

    assert main([str(diff_path)], provider=ClaudeAgentProvider(Settings(), sdk_call=sdk_call)) == 1
    assert sys.stderr is None


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
