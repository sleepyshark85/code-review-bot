"""The second real provider: any OpenAI-compatible Chat Completions endpoint (ADR 0020).

It speaks HTTP through an injected ``HttpTransport``, so tests stay offline. The wire format,
and what each response means, are ``openai_wire``'s; this module adds the calls, the retries and
the deadline.
"""

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from http.client import HTTPException
from typing import Final, Protocol

from review_bot.audit import JsonValue, utc_now
from review_bot.errors import ConfigError, ErrorCategory, ProviderError
from review_bot.providers.base import ReviewRequest, ReviewResponse
from review_bot.providers.openai_wire import (
    ChatRequest,
    Completion,
    check_base_url,
    is_loopback_url,
    is_plain_text,
    read_completion,
    read_error,
    retry_after_s,
)
from review_bot.providers.urllib_transport import HttpResponse, UrllibTransport
from review_bot.settings import OPENAI_API_KEY_VARIABLE, Settings

__all__ = [
    "HttpResponse",
    "HttpTransport",
    "OpenAICompatibleProvider",
    "UrllibTransport",
    "check_api_key",
]

# A longer wait fails the call at once rather than holding the review (P4-S2 AC4).
_MAX_RETRY_AFTER_S: Final = 30
_RETRIED_CATEGORIES: Final = frozenset({ErrorCategory.TRANSIENT, ErrorCategory.RATE_LIMIT})
_REDACTED: Final = "[REDACTED]"
_SUCCESS_STATUSES: Final = range(200, 300)

_NO_KEY_MESSAGE: Final = (
    f"--provider openai needs {OPENAI_API_KEY_VARIABLE}, which is unset or empty, so nothing "
    "was sent. Set it, or point --openai-base-url at a loopback server that needs no key."
)
_UNSENDABLE_KEY_MESSAGE: Final = (
    f"{OPENAI_API_KEY_VARIABLE} holds a space, a control character such as a newline, or a "
    "character that isn't ASCII, so it can't be sent. Set it to the key alone."
)
_TOOLS_MESSAGE: Final = (
    "The OpenAI-compatible provider can't review with repository context yet. Review in diff mode."
)
_DEADLINE_MESSAGE: Final = (
    "The OpenAI-compatible endpoint didn't finish in time. Try again, or raise --deadline-s."
)
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


def check_api_key(base_url: str, api_key: str | None) -> None:
    """Check that a request to ``base_url`` can be authorised: only a loopback host needs no key.

    Raises:
        ConfigError: Naming ``OPENAI_API_KEY``, if it is unset or empty and the host isn't
            loopback, or if it can't be sent in a header; never quoting the key.
    """
    if not api_key:
        if not is_loopback_url(base_url):
            raise ConfigError(_NO_KEY_MESSAGE)
        return
    if not is_plain_text(api_key):
        raise ConfigError(_UNSENDABLE_KEY_MESSAGE)


@dataclasses.dataclass(frozen=True, slots=True)
class _Failure:
    """A failed attempt: its error, what the endpoint asked us to wait, and what went wrong."""

    error: ProviderError
    retry_after_s: float | None
    cause: str


class OpenAICompatibleProvider:
    """Reviews in diff mode through an OpenAI-compatible Chat Completions endpoint."""

    def __init__(  # noqa: PLR0913 - all but settings are keyword-only
        self,
        settings: Settings,
        *,
        base_url: str,
        api_key: str | None,
        transport: HttpTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Prepare the provider; nothing is sent until ``review``.

        Args:
            settings: Supplies the output token cap, the retries, the API timeout and the
                overall deadline.
            base_url: The endpoint, such as ``https://api.openai.com/v1``.
            api_key: Sent as a bearer token; None or empty sends none, which only a loopback
                host allows.
            transport: Sends the requests; None means a ``UrllibTransport``.
            sleep: Waits between retries, so tests needn't.
            clock: The current time, timezone-aware, against which a ``Retry-After`` date is
                read.

        Raises:
            ValueError: If ``base_url`` isn't allowed (``check_base_url``).
            ConfigError: If there is no key and the host isn't loopback, or the key holds a
                character that can't be sent in a header (``check_api_key``).
        """
        check_base_url(base_url)
        check_api_key(base_url, api_key)
        self._settings = settings
        self._base_url = base_url
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key or None
        self._transport = UrllibTransport() if transport is None else transport
        self._sleep = sleep
        self._clock = clock

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        """Send the request, retrying ``transient`` and ``rate_limit`` failures, by the deadline.

        Raises:
            ProviderError: For every failure of a known kind; an error response's text is kept
                only as its diagnostics.
        """
        if request.tools is not None:
            raise ProviderError(ErrorCategory.PERMANENT, _TOOLS_MESSAGE)
        chat = ChatRequest(
            model=request.model,
            system_prompt=request.system_prompt,
            user_prompt=request.user_prompt,
            max_output_tokens=self._settings.max_output_tokens,
            base_url=self._base_url,
        )
        deadline = asyncio.get_running_loop().time() + self._settings.deadline_s
        try:
            async with asyncio.timeout_at(deadline):
                return await self._call_with_retries(chat.body(), deadline=deadline)
        except TimeoutError as exc:
            raise ProviderError(ErrorCategory.TRANSIENT, _DEADLINE_MESSAGE) from exc

    async def _call_with_retries(
        self, body: Mapping[str, JsonValue], *, deadline: float
    ) -> ReviewResponse:
        """The first completion, after any retries; ``deadline`` is on the event loop's clock."""
        loop = asyncio.get_running_loop()
        warnings: list[str] = []
        retries = 0
        while True:
            outcome = await self._attempt(body, timeout_s=deadline - loop.time())
            if isinstance(outcome, Completion):
                return ReviewResponse(outcome.findings, outcome.usage, warnings=tuple(warnings))
            wait_s = self._wait_before_retry(outcome, retries, time_left_s=deadline - loop.time())
            if wait_s is None:
                raise outcome.error
            retries += 1
            warnings.append(
                f"review-bot: warning: the OpenAI-compatible endpoint failed ({outcome.cause}), "
                f"so the call was retried (retry {retries} of {self._settings.max_retries})."
            )
            await self._sleep(wait_s)

    def _wait_before_retry(
        self, failure: _Failure, retries: int, *, time_left_s: float
    ) -> float | None:
        """How long to wait before retrying ``failure``, or None if it mustn't be retried."""
        if failure.error.category not in _RETRIED_CATEGORIES:
            return None
        if retries >= self._settings.max_retries:
            return None
        wait_s = failure.retry_after_s
        if wait_s is None:
            wait_s = min(2**retries, _MAX_RETRY_AFTER_S)
        if wait_s > _MAX_RETRY_AFTER_S or wait_s >= time_left_s:
            return None
        return wait_s

    async def _attempt(
        self, body: Mapping[str, JsonValue], *, timeout_s: float
    ) -> Completion | _Failure:
        """One request: its completion, or a failure that may be retried.

        Raises:
            ProviderError: If a response came back that no retry can change, such as a refusal.
        """
        headers = {} if self._api_key is None else {"Authorization": f"Bearer {self._api_key}"}
        api_timeout_s = self._settings.api_timeout_ms / 1000
        try:
            response = await self._transport.post_json(
                self._url, body, headers=headers, timeout_s=min(api_timeout_s, timeout_s)
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
                return retry_after_s(value, now=self._clock())
        return None

    def _diagnostics(self, text: str | None) -> tuple[str, ...]:
        """An error response's text as diagnostics, with the key, should it be echoed, removed."""
        if text is None:
            return ()
        if self._api_key is not None:
            text = text.replace(self._api_key, _REDACTED)
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
