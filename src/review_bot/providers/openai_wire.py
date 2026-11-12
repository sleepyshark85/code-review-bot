"""The Chat Completions wire format, pure: request bodies, responses and errors (ADR 0020).

Everything a response holds is untrusted. Each reader here returns our own types or raises a
``ProviderError`` with fixed text, so an endpoint's prose never reaches the user's message.
"""

import email.utils
import ipaddress
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Final, Literal, cast
from urllib.parse import urlsplit

from review_bot.audit import JsonValue
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding, parse_findings
from review_bot.providers.base import TokenUsage, ToolDefinition

__all__ = [
    "DEFAULT_BASE_URL",
    "MAX_RESPONSE_BYTES",
    "MAX_TOOL_ARGUMENTS_BYTES",
    "ChatRequest",
    "Completion",
    "ErrorReply",
    "ToolCall",
    "ToolTurn",
    "check_base_url",
    "is_loopback_url",
    "is_plain_text",
    "read_completion",
    "read_error",
    "retry_after_s",
    "tool_replies",
]

DEFAULT_BASE_URL: Final = "https://api.openai.com/v1"
# The most of a response body that is read; one byte more fails the call (P4-S2 Limits).
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
# The most of one tool call's arguments that is parsed; more is refused (P4-S6 Limits).
MAX_TOOL_ARGUMENTS_BYTES: Final = 64 * 1024

# OpenAI's newer models refuse ``max_tokens``; many self-hosted servers know only it.
_OPENAI_HOST: Final = "api.openai.com"
_LOCALHOST: Final = "localhost"
_QUOTA_EXHAUSTED: Final = "insufficient_quota"
_AUTH_STATUSES: Final = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN})
_SERVER_ERROR_STATUSES: Final = range(500, 600)

_NOT_A_COMPLETION: Final = (
    "The OpenAI-compatible endpoint's response isn't a chat completion. Check "
    "--openai-base-url, then try again."
)
_CUT_SHORT: Final = (
    "The model hit its output token cap before it finished the review. Raise "
    "--max-output-tokens, or review a smaller diff."
)
_REFUSED: Final = "The model refused to review this diff. Review it by hand."

type OutputCapField = Literal["max_completion_tokens", "max_tokens"]


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """A review's Chat Completions requests: the system and user prompts, then any replies.

    ``max_output_tokens`` None sends no output cap, so the endpoint's own applies. ``tools``
    are offered as functions, in their order; with none, no ``tools`` are sent, as in diff mode.
    """

    model: str
    system_prompt: str
    user_prompt: str
    max_output_tokens: int | None
    base_url: str
    tools: tuple[ToolDefinition, ...] = ()

    def body(self, replies: Sequence[JsonValue] = ()) -> dict[str, JsonValue]:
        """The JSON body of the request that follows ``replies`` (``tool_replies``), in order.

        The output cap goes under the field the base URL's host expects.
        """
        body: dict[str, JsonValue] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": self.user_prompt},
                *replies,
            ],
        }
        if self.tools:
            body["tools"] = [_function(tool) for tool in self.tools]
        if self.max_output_tokens is not None:
            body[_output_cap_field(self.base_url)] = self.max_output_tokens
        return body


@dataclass(frozen=True, slots=True)
class ErrorReply:
    """An error response's category, and its text, which is untrusted.

    ``text`` is ``error.message``, else the body as text, or None if it is empty. It may only
    become diagnostics, which are scrubbed before they are kept.
    """

    category: ErrorCategory
    is_quota_exhausted: bool
    text: str | None


@dataclass(frozen=True, slots=True)
class Completion:
    """A successful completion's validated findings, and its token usage, None if unusable."""

    findings: tuple[Finding, ...]
    usage: TokenUsage | None


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call the model asked for; all of it is untrusted.

    ``arguments`` are the call's arguments parsed, or empty if they couldn't be, so that the
    executor refuses the call.
    """

    call_id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ToolTurn:
    """A successful completion that asks for tools: its text, its calls, and its usage.

    ``content`` is None if it has none, and ``usage`` None if it is unusable.
    """

    content: str | None
    calls: tuple[ToolCall, ...]
    usage: TokenUsage | None


def check_base_url(url: str) -> str:
    """Return ``url`` if it may be a base URL, else raise ``ValueError`` saying why.

    It must be ``https``, or ``http`` on a loopback host, with a valid port if it has one, and
    hold no userinfo, query or fragment, so no credential or parameter hides in it. It must be
    plain ASCII with no space or control character, as a request line needs.
    """
    if not is_plain_text(url):
        raise ValueError("must hold only ASCII with no space or control character")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("must be an http or https URL with a host")
    try:
        parts.port  # noqa: B018 - read only for the ValueError a bad port raises
    except ValueError as exc:
        raise ValueError("must have a port from 0 to 65535, if it has one") from exc
    if parts.username is not None or parts.password is not None:
        raise ValueError("must not hold a user name or password")
    if parts.query or parts.fragment or url.endswith(("?", "#")):
        raise ValueError("must not hold a query or fragment")
    if parts.scheme == "http" and not is_loopback_url(url):
        raise ValueError("must use https, except on a loopback host such as 127.0.0.1")
    return url


def is_plain_text(text: str) -> bool:
    """Whether ``text`` is printable ASCII with no space, so it fits in a request's head."""
    return text.isascii() and text.isprintable() and " " not in text


def is_loopback_url(url: str) -> bool:
    """Whether ``url``'s host is ``localhost`` or a loopback address, such as ``127.0.0.1``."""
    host = urlsplit(url).hostname or ""
    if host == _LOCALHOST:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def read_completion(body: bytes) -> Completion | ToolTurn:
    """Read a 200 response's body: the first choice's findings or tool calls, and the usage.

    A message with at least one tool call is a ``ToolTurn``, whatever its content.

    Raises:
        ProviderError: ``refusal`` if the model refused or a content filter stopped it;
            ``invalid_output`` if the body is over 4 MB, isn't a completion, was cut short by
            the output cap, has malformed tool calls, or has none and its content isn't the
            findings JSON.
    """
    completion = _json_object(body)
    choices = None if completion is None else completion.get("choices")
    if completion is None or not isinstance(choices, list) or not choices:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT, _NOT_A_COMPLETION)
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT, _NOT_A_COMPLETION)
    finish_reason = choice.get("finish_reason")
    refusal = message.get("refusal")
    if finish_reason == "content_filter" or (isinstance(refusal, str) and refusal):
        raise ProviderError(ErrorCategory.REFUSAL, _REFUSED)
    if finish_reason == "length":
        raise ProviderError(ErrorCategory.INVALID_OUTPUT, _CUT_SHORT)
    content = message.get("content")
    text = content if isinstance(content, str) else None
    usage = _token_usage(completion.get("usage"))
    calls = _tool_calls(message.get("tool_calls"))
    if calls:
        return ToolTurn(text, calls, usage)
    return Completion(parse_findings(text or ""), usage)


def tool_replies(turn: ToolTurn, results: Sequence[str]) -> list[JsonValue]:
    """The messages that answer ``turn``: its assistant message, then each call's result.

    The assistant message is rebuilt from what was read, so each call's arguments go back as
    the executor got them: an empty object for arguments that couldn't be parsed.

    Raises:
        ValueError: If there isn't exactly one result per call.
    """
    calls: list[JsonValue] = [
        {
            "id": call.call_id,
            "type": "function",
            "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
        }
        for call in turn.calls
    ]
    answers: list[JsonValue] = [
        {"role": "tool", "tool_call_id": call.call_id, "content": result}
        for call, result in zip(turn.calls, results, strict=True)
    ]
    return [{"role": "assistant", "content": turn.content, "tool_calls": calls}, *answers]


def read_error(status: int, body: bytes) -> ErrorReply:
    """Read a response whose status isn't a success: its category, and its text.

    A redirect is never followed, so it is ``permanent`` like any 4xx without its own category.
    """
    error = _error_object(body)
    is_quota_exhausted = (
        status == HTTPStatus.TOO_MANY_REQUESTS and error.get("code") == _QUOTA_EXHAUSTED
    )
    if status in _AUTH_STATUSES:
        category = ErrorCategory.AUTH
    elif status == HTTPStatus.TOO_MANY_REQUESTS and not is_quota_exhausted:
        category = ErrorCategory.RATE_LIMIT
    elif status in _SERVER_ERROR_STATUSES:
        category = ErrorCategory.TRANSIENT
    else:
        category = ErrorCategory.PERMANENT
    message = error.get("message")
    text = message if isinstance(message, str) else _body_text(body)
    return ErrorReply(category, is_quota_exhausted=is_quota_exhausted, text=text)


def retry_after_s(value: str, *, now: datetime) -> float | None:
    """The wait a ``Retry-After`` value asks for, in seconds, or None if it can't be read.

    It is whole seconds, or an HTTP date, read against ``now``; a date already past asks for
    no wait. A date with no zone, which ``-0000`` gives, is read as UTC, as HTTP dates are.
    """
    text = value.strip()
    if text.isascii() and text.isdigit():
        return float(text)
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - now).total_seconds(), 0.0)


def _function(tool: ToolDefinition) -> JsonValue:
    """``tool`` as Chat Completions offers a function."""
    # A JSON Schema that pydantic generated, so it holds only JSON values.
    parameters = cast("JsonValue", tool.input_schema)
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description, "parameters": parameters},
    }


def _tool_calls(tool_calls: object) -> tuple[ToolCall, ...]:
    """A message's untrusted ``tool_calls``, in order; none if it has none.

    Raises:
        ProviderError: ``invalid_output`` unless they are a list of calls, each with a string
            ``id`` and a ``function`` with a string ``name``.
    """
    if tool_calls is None:
        return ()
    if not isinstance(tool_calls, list):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT, _NOT_A_COMPLETION)
    return tuple(_tool_call(item) for item in tool_calls)


def _tool_call(item: object) -> ToolCall:
    """See ``_tool_calls``."""
    call_id = item.get("id") if isinstance(item, dict) else None
    function = item.get("function") if isinstance(item, dict) else None
    if not isinstance(call_id, str) or not isinstance(function, dict):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT, _NOT_A_COMPLETION)
    name = function.get("name")
    if not isinstance(name, str):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT, _NOT_A_COMPLETION)
    return ToolCall(call_id, name, _tool_arguments(function.get("arguments")))


def _tool_arguments(arguments: object) -> Mapping[str, object]:
    """A call's untrusted ``arguments`` parsed strictly, or empty if they can't be.

    They must be a string of at most 64 KB holding one JSON object, with no repeated key and no
    ``NaN`` or ``Infinity``.
    """
    if not isinstance(arguments, str):
        return {}
    # ``surrogatepass``, because a JSON string can hold a lone surrogate, which UTF-8 can't.
    if len(arguments.encode("utf-8", "surrogatepass")) > MAX_TOOL_ARGUMENTS_BYTES:
        return {}
    try:
        parsed = json.loads(
            arguments, object_pairs_hook=_object_without_repeats, parse_constant=_refuse_constant
        )
    # Deep nesting exhausts the parser's recursion, well within 64 KB.
    except (ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _object_without_repeats(pairs: list[tuple[str, object]]) -> dict[str, object]:
    parsed = dict(pairs)
    if len(parsed) < len(pairs):
        raise ValueError("a key is repeated")
    return parsed


def _refuse_constant(name: str) -> object:
    raise ValueError(f"{name} isn't JSON")


def _output_cap_field(base_url: str) -> OutputCapField:
    is_openai = urlsplit(base_url).hostname == _OPENAI_HOST
    return "max_completion_tokens" if is_openai else "max_tokens"


def _json_object(body: bytes) -> dict[str, object] | None:
    """``body`` parsed, if it is a JSON object of at most 4 MB; else None."""
    if len(body) > MAX_RESPONSE_BYTES:
        return None
    try:
        parsed = json.loads(body)
    # Deep nesting exhausts the parser's recursion, well within 4 MB.
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _error_object(body: bytes) -> dict[str, object]:
    """The ``error`` object of an error body, or an empty one if it has none."""
    parsed = _json_object(body)
    error = None if parsed is None else parsed.get("error")
    return error if isinstance(error, dict) else {}


def _body_text(body: bytes) -> str | None:
    text = body[:MAX_RESPONSE_BYTES].decode("utf-8", "replace").strip()
    return text or None


def _token_usage(usage: object) -> TokenUsage | None:
    """Read the endpoint's untrusted usage: None unless both counts are integers of 0 or more."""
    if not isinstance(usage, Mapping):
        return None
    input_tokens = _token_count(usage.get("prompt_tokens"))
    output_tokens = _token_count(usage.get("completion_tokens"))
    if input_tokens is None or output_tokens is None:
        return None
    return TokenUsage(input_tokens, output_tokens)


def _token_count(count: object) -> int | None:
    # ``type``, not ``isinstance``, because a bool is an int.
    return count if type(count) is int and count >= 0 else None
