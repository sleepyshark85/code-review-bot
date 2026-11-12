"""One Chat Completions POST at a time, with its retries and its failures mapped (ADR 0020).

Part of the OpenAI-compatible provider: ``openai_compat`` runs the review's conversation, and
this module sends each of its requests through an ``HttpTransport``. What a response means is
``openai_wire``'s.
"""

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from http.client import HTTPException
from typing import Final, Protocol

from review_bot.audit import JsonValue
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.providers.openai_wire import (
    Completion,
    ToolTurn,
    read_completion,
    read_error,
    retry_after_s,
)
from review_bot.providers.urllib_transport import HttpResponse
from review_bot.settings import OPENAI_API_KEY_VARIABLE

__all__ = ["ChatEndpoint", "HttpTransport"]

# A longer wait fails the call at once rather than holding the review (P4-S2 AC4).
_MAX_RETRY_AFTER_S: Final = 30
_RETRIED_CATEGORIES: Final = frozenset({ErrorCategory.TRANSIENT, ErrorCategory.RATE_LIMIT})
_REDACTED: Final = "[REDACTED]"
_SUCCESS_STATUSES: Final = range(200, 300)

_TIMEOUT_MESSAGE: Final = (
    "The OpenAI-compatible endpoint didn't answer within the API timeout. Try again shortly, or "
    "raise --api-timeout-ms."
)
_DROPPED_MESSAGE: Final = (
    "The connection to the OpenAI-compatible endpoint was dropped. Try again shortly."
)
_UNREACHABLE_MESSAGE: Final = (
    "Couldn't connect to the OpenAI-compatible endpoint. Check --openai-base-url and the "
    "network, then try again."
)
_QUOTA_MESSAGE: Final = (
    "The OpenAI account's quota is used up (HTTP 429, insufficient_quota). Check its plan and "
    "billing."
)


class HttpTransport(Protocol):
    """Sends one JSON POST. One real implementation, ``UrllibTransport``, and fakes for tests."""

    async def post_json(
        self,
        url: str,
        body: Mapping[str, JsonValue],
        *,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> HttpResponse:
        """Send ``body`` and return the response, whatever its status; never follow a redirect.

        ``timeout_s`` bounds the whole request, and the body read is at most 4 MB and a byte.

        Raises:
            OSError: If no response came back, as the standard library raises it: such as
                ``TimeoutError``, ``ConnectionRefusedError`` or ``socket.gaierror``.
            HTTPException: If the response was malformed or cut off.
        """
        ...


@dataclasses.dataclass(frozen=True, slots=True)
class _Failure:
    """A failed attempt: its error, what the endpoint asked us to wait, and what went wrong."""

    error: ProviderError
    retry_after_s: float | None
    cause: str


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class ChatEndpoint:
    """A Chat Completions URL, and how to send to it.

    ``api_key`` None sends no ``Authorization`` header, and no ``repr`` shows it, so no
    traceback or log line can. ``sleep`` waits between retries, and ``clock`` gives the current
    time, timezone-aware, against which a ``Retry-After`` date is read.
    """

    url: str
    api_key: str | None = dataclasses.field(repr=False)
    transport: HttpTransport
    max_retries: int
    api_timeout_s: float
    sleep: Callable[[float], Awaitable[None]]
    clock: Callable[[], datetime]

    async def post_with_retries(
        self, body: Mapping[str, JsonValue], *, deadline: float, warnings: list[str]
    ) -> Completion | ToolTurn:
        """POST ``body`` until it succeeds, retrying ``transient`` and ``rate_limit`` failures.

        Each retry adds a line to ``warnings``. ``deadline`` is on the event loop's clock: no
        attempt runs, and no wait lasts, past it.

        Raises:
            ProviderError: For every failure of a known kind; an error response's text is kept
                only as its diagnostics.
        """
        loop = asyncio.get_running_loop()
        retries = 0
        while True:
            outcome = await self._attempt(body, timeout_s=deadline - loop.time())
            if not isinstance(outcome, _Failure):
                return outcome
            wait_s = self._wait_before_retry(outcome, retries, time_left_s=deadline - loop.time())
            if wait_s is None:
                raise outcome.error
            retries += 1
            warnings.append(
                f"review-bot: warning: the OpenAI-compatible endpoint failed ({outcome.cause}), "
                f"so the call was retried (retry {retries} of {self.max_retries})."
            )
            await self.sleep(wait_s)

    def _wait_before_retry(
        self, failure: _Failure, retries: int, *, time_left_s: float
    ) -> float | None:
        """How long to wait before retrying ``failure``, or None if it mustn't be retried."""
        if failure.error.category not in _RETRIED_CATEGORIES:
            return None
        if retries >= self.max_retries:
            return None
        wait_s = failure.retry_after_s
        if wait_s is None:
            wait_s = min(2**retries, _MAX_RETRY_AFTER_S)
        if wait_s > _MAX_RETRY_AFTER_S or wait_s >= time_left_s:
            return None
        return wait_s

    async def _attempt(
        self, body: Mapping[str, JsonValue], *, timeout_s: float
    ) -> Completion | ToolTurn | _Failure:
        """One request: its completion, or a failure that may be retried.

        Raises:
            ProviderError: If a response came back that no retry can change, such as a refusal.
        """
        headers = {} if self.api_key is None else {"Authorization": f"Bearer {self.api_key}"}
        try:
            response = await self.transport.post_json(
                self.url, body, headers=headers, timeout_s=min(self.api_timeout_s, timeout_s)
            )
        except (OSError, HTTPException) as exc:
            return _failure_without_response(exc)
        if response.status in _SUCCESS_STATUSES:
            return read_completion(response.body)
        return self._failure_from_status(response)

    def _failure_from_status(self, response: HttpResponse) -> _Failure:
        status = response.status
        reply = read_error(status, response.body)
        if reply.is_quota_exhausted:
            message = _QUOTA_MESSAGE
        else:
            message = _status_message(reply.category, status)
        diagnostics = self._diagnostics(reply.text)
        error = ProviderError(reply.category, message, diagnostics=diagnostics)
        return _Failure(error, self._retry_after_s(response.headers), f"HTTP {status}")

    def _retry_after_s(self, headers: Mapping[str, str]) -> float | None:
        """The wait the ``Retry-After`` header asks for, or None if it is absent or unparsable."""
        for name, value in headers.items():
            if name.lower() == "retry-after":
                return retry_after_s(value, now=self.clock())
        return None

    def _diagnostics(self, text: str | None) -> tuple[str, ...]:
        """An error response's text as diagnostics, with the key, should it be echoed, removed."""
        if text is None:
            return ()
        if self.api_key is not None:
            text = text.replace(self.api_key, _REDACTED)
        return tuple(text.splitlines())


def _failure_without_response(exc: OSError | HTTPException) -> _Failure:
    """A request that got no usable response.

    A timeout, or a connection dropped or answered badly, is ``transient``. Anything else,
    such as a refused connection or a failed DNS lookup, means the endpoint couldn't be
    reached, which is ``unavailable``.
    """
    if isinstance(exc, TimeoutError):
        error = ProviderError(ErrorCategory.TRANSIENT, _TIMEOUT_MESSAGE)
    elif isinstance(exc, HTTPException) or (
        isinstance(exc, ConnectionError) and not isinstance(exc, ConnectionRefusedError)
    ):
        error = ProviderError(ErrorCategory.TRANSIENT, _DROPPED_MESSAGE)
    else:
        error = ProviderError(ErrorCategory.UNAVAILABLE, _UNREACHABLE_MESSAGE)
    error.__cause__ = exc
    return _Failure(error, None, type(exc).__name__)


def _status_message(category: ErrorCategory, status: int) -> str:
    match category:
        case ErrorCategory.AUTH:
            return (
                f"The OpenAI-compatible endpoint rejected the credentials (HTTP {status}). "
                f"Check {OPENAI_API_KEY_VARIABLE}, then try again."
            )
        case ErrorCategory.RATE_LIMIT:
            return (
                f"The OpenAI-compatible endpoint's rate limit was exceeded (HTTP {status}). "
                "Wait, then try again."
            )
        case ErrorCategory.TRANSIENT:
            return f"The OpenAI-compatible endpoint was failing (HTTP {status}). Try again shortly."
        case _:
            return (
                f"The OpenAI-compatible endpoint refused the request (HTTP {status}), and trying "
                "again is unlikely to help. Check --model and --openai-base-url."
            )
