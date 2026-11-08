"""The Claude provider: the only module that imports the Claude Agent SDK (ADR 0001)."""

import asyncio
import dataclasses
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import aclosing
from typing import Final, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLIConnectionError,
    CLINotFoundError,
    Message,
    ResultError,
    ResultMessage,
    query,
)

from review_bot.errors import ErrorCategory, ProviderError
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

# Bounds the memory a chatty CLI can use; ``audit`` keeps far fewer once it has scrubbed them.
_MAX_HELD_STDERR_LINES: Final = 1000

_ASSISTANT_ERROR_CATEGORIES: Final[Mapping[str, ErrorCategory]] = {
    "authentication_failed": ErrorCategory.AUTH,
    "rate_limit": ErrorCategory.RATE_LIMIT,
    "server_error": ErrorCategory.TRANSIENT,
    "invalid_request": ErrorCategory.PERMANENT,
    "unknown": ErrorCategory.PERMANENT,
}

_STATUS_CATEGORIES: Final[Mapping[int, ErrorCategory]] = {
    401: ErrorCategory.AUTH,
    403: ErrorCategory.AUTH,
    408: ErrorCategory.TRANSIENT,
    429: ErrorCategory.RATE_LIMIT,
    **dict.fromkeys(range(500, 600), ErrorCategory.TRANSIENT),
}

# Fixed text: the SDK's exception text and the CLI's result can embed its stderr, raw output or
# model prose, none of which may reach the user.
_CATEGORY_MESSAGES: Final[Mapping[ErrorCategory, str]] = {
    ErrorCategory.AUTH: (
        "Claude rejected the credentials. Check the Claude Code login or ANTHROPIC_API_KEY, "
        "then try again."
    ),
    ErrorCategory.RATE_LIMIT: (
        "Claude's rate limit was still exceeded after the CLI's retries. Wait, then try again."
    ),
    ErrorCategory.REFUSAL: "Claude refused to review this diff. Review it by hand.",
    ErrorCategory.TRANSIENT: (
        "The Claude API was still failing after the CLI's retries. Try again shortly."
    ),
    ErrorCategory.PERMANENT: (
        "The Claude call failed, and trying again is unlikely to help. Check --model and that "
        "the Claude Code CLI runs."
    ),
    ErrorCategory.UNAVAILABLE: (
        "The Claude Code CLI wasn't found. To fix this, install Claude Code and check that "
        "claude is on your PATH."
    ),
}
_CONNECTION_MESSAGE: Final = (
    "Couldn't start or talk to the Claude Code CLI. Check that it runs, then try again."
)
_DEADLINE_MESSAGE: Final = "Claude didn't finish in time. Try again, or raise --deadline-s."
_BILLING_MESSAGE: Final = "Claude reported a billing problem. Check the account's billing."
_NO_RESULT_MESSAGE: Final = (
    "The Claude stream ended without a result. Check that the Claude Code CLI runs, then try again."
)


@dataclasses.dataclass(frozen=True, slots=True)
class _Signal:
    """A sign that the call failed, which only a later successful result can override."""

    name: str
    error: ProviderError


class ClaudeAgentProvider:
    """Reviews with a locked-down Claude: one turn, no tools, no settings, no MCP servers."""

    def __init__(self, settings: Settings, *, sdk_call: SdkCall | None = None) -> None:
        """Prepare the provider; nothing starts until ``review``.

        Args:
            settings: Supplies the CLI's retry, timeout and output token limits, and the overall
                deadline.
            sdk_call: Replaces the real SDK call, so tests stay offline.
        """
        self._settings = settings
        self._sdk_call = _call_sdk if sdk_call is None else sdk_call

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        """Make one call, leaving retries to the CLI, and return its validated findings.

        Reading stops at the first successful result, which outranks anything else in the stream.
        Without one, the earliest signal decides, then whatever ended the stream.

        Raises:
            ProviderError: For every failure of a known kind, with the tail of the CLI's stderr
                as its diagnostics.
        """
        cli_stderr: deque[str] = deque(maxlen=_MAX_HELD_STDERR_LINES)
        options = _build_options(request, self._settings, on_stderr=cli_stderr.append)
        stream = self._sdk_call(request.user_prompt, options)
        response: ReviewResponse | None = None
        signal: _Signal | None = None
        raised: Exception | None = None
        try:
            async with asyncio.timeout(self._settings.deadline_s), aclosing(stream):
                async for message in stream:
                    outcome = _read_message(message)
                    if isinstance(outcome, ReviewResponse):
                        response = outcome
                        break
                    signal = signal or outcome
        # Held, not handled: an unexpected exception still propagates from ``_settle`` unless the
        # stream had already given a better answer.
        except Exception as exc:  # noqa: BLE001 - see above
            raised = exc
        try:
            return _settle(response, signal, raised)
        except ProviderError as exc:
            # Only on failure: a successful call's CLI text is never kept (P1-S5).
            exc.diagnostics = tuple(cli_stderr)
            raise


def _build_options(
    request: ReviewRequest, settings: Settings, *, on_stderr: Callable[[str], None]
) -> ClaudeAgentOptions:
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
        env=_cli_env(settings),
        # Held as diagnostics, never written: it is untrusted, and only our own lines may reach
        # the user.
        stderr=on_stderr,
    )


def _cli_env(settings: Settings) -> dict[str, str]:
    env = {
        "CLAUDE_CODE_MAX_RETRIES": str(settings.max_retries),
        "API_TIMEOUT_MS": str(settings.api_timeout_ms),
    }
    # Only when set, so without the cap the CLI runs exactly as before (ADR 0012).
    if settings.max_output_tokens is not None:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(settings.max_output_tokens)
    return env


def _read_message(message: object) -> ReviewResponse | _Signal | None:
    if isinstance(message, AssistantMessage) and message.error is not None:
        return _read_assistant_error(message.error)
    if isinstance(message, ResultMessage):
        return _read_result(message)
    return None


def _read_assistant_error(error: object) -> _Signal:
    # ``str``, because the SDK passes this field of the CLI's JSON through unchecked, and a value
    # that isn't hashable would break the lookup.
    name = str(error)
    if name == "billing_error":
        return _Signal(name, ProviderError(ErrorCategory.PERMANENT, _BILLING_MESSAGE))
    category = _ASSISTANT_ERROR_CATEGORIES.get(name)
    if category is None:
        message = f"Claude reported an unrecognised error: {name}. Check the Claude Code CLI."
        return _Signal(name, ProviderError(ErrorCategory.PERMANENT, message))
    return _Signal(name, _category_error(category))


def _read_result(result: ResultMessage) -> ReviewResponse | _Signal:
    if result.stop_reason == "refusal":
        return _Signal(ErrorCategory.REFUSAL, _category_error(ErrorCategory.REFUSAL))
    # Judged on ``is_error`` because a failed call's subtype can still be "success".
    if result.is_error:
        category = _category_from_status(result.api_error_status, result.terminal_reason)
        return _Signal(category, _category_error(category))
    try:
        findings = parse_findings(result.result or "")
    except ProviderError as exc:
        return _Signal(exc.category, exc)
    return ReviewResponse(findings=findings, usage=_token_usage(result.usage))


def _settle(
    response: ReviewResponse | None, signal: _Signal | None, raised: Exception | None
) -> ReviewResponse:
    if response is not None:
        if signal is None:
            return response
        warning = (
            f"review-bot: warning: Claude signalled {signal.name} during the call, but a valid "
            "result followed, so that result stands."
        )
        return dataclasses.replace(response, warnings=(warning,))
    if signal is not None:
        raise signal.error from raised
    if raised is None:
        raise ProviderError(ErrorCategory.PERMANENT, _NO_RESULT_MESSAGE)
    if isinstance(raised, TimeoutError):
        raise ProviderError(ErrorCategory.TRANSIENT, _DEADLINE_MESSAGE) from raised
    if isinstance(raised, ClaudeSDKError):
        raise _translate_sdk_error(raised) from raised
    raise raised


def _translate_sdk_error(error: ClaudeSDKError) -> ProviderError:
    # Subclasses first: CLINotFoundError is a CLIConnectionError, ResultError a ProcessError.
    if isinstance(error, CLINotFoundError):
        return _category_error(ErrorCategory.UNAVAILABLE)
    if isinstance(error, CLIConnectionError):
        # The one SDK text shown: in SDK 0.2.178 it is the SDK's own guidance plus OS errors, never
        # the CLI's output. ``write_diagnostic`` escapes it. Recheck this on an SDK upgrade.
        message = f"{_CONNECTION_MESSAGE} The SDK said: {error}"
        return ProviderError(ErrorCategory.TRANSIENT, message)
    if isinstance(error, ResultError):
        return _category_error(_category_from_status(error.api_error_status, error.terminal_reason))
    return _category_error(ErrorCategory.PERMANENT)


def _category_from_status(status: object, terminal_reason: object) -> ErrorCategory:
    # ``type``, not ``isinstance``, because a bool is an int. The SDK doesn't check these fields.
    if type(status) is int:
        return _STATUS_CATEGORIES.get(status, ErrorCategory.PERMANENT)
    return ErrorCategory.TRANSIENT if terminal_reason == "api_error" else ErrorCategory.PERMANENT


def _category_error(category: ErrorCategory) -> ProviderError:
    return ProviderError(category, _CATEGORY_MESSAGES[category])


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
