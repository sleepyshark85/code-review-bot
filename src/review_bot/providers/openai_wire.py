"""The Chat Completions wire format, pure: request bodies, responses and errors (ADR 0020).

Everything a response holds is untrusted. Each reader here returns our own types or raises a
``ProviderError`` with fixed text, so an endpoint's prose never reaches the user's message.
"""

import email.utils
import ipaddress
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Final, Literal
from urllib.parse import urlsplit

from review_bot.audit import JsonValue
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding, parse_findings
from review_bot.providers.base import TokenUsage

__all__ = [
    "DEFAULT_BASE_URL",
    "MAX_RESPONSE_BYTES",
    "ChatRequest",
    "Completion",
    "check_base_url",
    "error_category",
    "error_text",
    "is_loopback_url",
    "read_completion",
    "retry_after_s",
]

DEFAULT_BASE_URL: Final = "https://api.openai.com/v1"
# The most of a response body that is read; one byte more fails the call (P4-S2 Limits).
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024

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
    """One diff-mode Chat Completions request: two messages, no tools.

    ``max_output_tokens`` None sends no output cap, so the endpoint's own applies.
    """

    model: str
    system_prompt: str
    user_prompt: str
    max_output_tokens: int | None
    base_url: str

    def body(self) -> dict[str, JsonValue]:
        """The JSON body, with the output cap under the field the base URL's host expects."""
        body: dict[str, JsonValue] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": self.user_prompt},
            ],
        }
        if self.max_output_tokens is not None:
            body[_output_cap_field(self.base_url)] = self.max_output_tokens
        return body


@dataclass(frozen=True, slots=True)
class Completion:
    """A successful completion's validated findings and token usage."""

    findings: tuple[Finding, ...]
    usage: TokenUsage


def check_base_url(url: str) -> str:
    """Return ``url`` if it may be a base URL, else raise ``ValueError`` saying why.

    It must be ``https``, or ``http`` on a loopback host, and hold no userinfo, query or
    fragment, so no credential or parameter hides in it.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("must be an http or https URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("must not hold a user name or password")
    if parts.query or parts.fragment or url.endswith(("?", "#")):
        raise ValueError("must not hold a query or fragment")
    if parts.scheme == "http" and not is_loopback_url(url):
        raise ValueError("must use https, except on a loopback host such as 127.0.0.1")
    return url


def is_loopback_url(url: str) -> bool:
    """Whether ``url``'s host is ``localhost`` or a loopback address, such as ``127.0.0.1``."""
    host = urlsplit(url).hostname or ""
    if host == _LOCALHOST:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def read_completion(body: bytes) -> Completion:
    """Read a 200 response's body: the first choice's findings, and the usage.

    Raises:
        ProviderError: ``refusal`` if the model refused or a content filter stopped it;
            ``invalid_output`` if the body is over 4 MB, isn't a completion, was cut short by
            the output cap, or its content isn't the findings JSON.
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
    findings = parse_findings(content if isinstance(content, str) else "")
    return Completion(findings, _token_usage(completion.get("usage")))


def error_category(status: int, body: bytes) -> ErrorCategory:
    """The category of a response whose status isn't 200.

    A redirect is never followed, so it is ``permanent`` like any 4xx without its own category.
    """
    if status in _AUTH_STATUSES:
        return ErrorCategory.AUTH
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        is_quota = _error_field(body, "code") == _QUOTA_EXHAUSTED
        return ErrorCategory.PERMANENT if is_quota else ErrorCategory.RATE_LIMIT
    if status in _SERVER_ERROR_STATUSES:
        return ErrorCategory.TRANSIENT
    return ErrorCategory.PERMANENT


def error_text(body: bytes) -> str | None:
    """An error response's ``error.message``, else its body as text, or None if it is empty.

    Untrusted: it may only become diagnostics, which are scrubbed before they are kept.
    """
    message = _error_field(body, "message")
    if message is not None:
        return message
    text = body[:MAX_RESPONSE_BYTES].decode("utf-8", "replace").strip()
    return text or None


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


def _error_field(body: bytes, name: str) -> str | None:
    """The string ``error.<name>`` of an error body, or None if it has none."""
    parsed = _json_object(body)
    error = None if parsed is None else parsed.get("error")
    value = error.get(name) if isinstance(error, dict) else None
    return value if isinstance(value, str) else None


def _token_usage(usage: object) -> TokenUsage:
    """Read the endpoint's untrusted usage. Bad usage counts as 0: it never costs a valid review."""
    if not isinstance(usage, Mapping):
        return TokenUsage(0, 0)
    return TokenUsage(
        input_tokens=_token_count(usage, "prompt_tokens"),
        output_tokens=_token_count(usage, "completion_tokens"),
    )


def _token_count(usage: Mapping[str, object], field: str) -> int:
    count = usage.get(field)
    # ``type``, not ``isinstance``, because a bool is an int.
    return count if type(count) is int and count >= 0 else 0
