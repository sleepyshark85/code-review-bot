"""The second real provider: any OpenAI-compatible Chat Completions endpoint (ADR 0020).

It speaks HTTP through an injected ``HttpTransport``, so tests stay offline. The wire format,
and what each response means, are ``openai_wire``'s, and each POST with its retries is
``openai_calls``'s. This module checks the configuration, and runs the review's conversation:
the tool loop through the request's executor (ADR 0014), the caps and the deadline.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Final

from review_bot.audit import JsonValue, utc_now
from review_bot.errors import ConfigError, ErrorCategory, ProviderError
from review_bot.price_flags import Prices, token_cost_usd
from review_bot.providers.base import ReviewRequest, ReviewResponse, TokenUsage, ToolExecutor
from review_bot.providers.openai_calls import ChatEndpoint, HttpTransport
from review_bot.providers.openai_wire import (
    ChatRequest,
    Completion,
    check_base_url,
    is_loopback_url,
    is_plain_text,
    tool_replies,
)
from review_bot.providers.urllib_transport import HttpResponse, UrllibTransport
from review_bot.settings import OPENAI_API_KEY_VARIABLE, Settings

__all__ = [
    "HttpResponse",
    "HttpTransport",
    "OpenAICompatibleProvider",
    "UrllibTransport",
    "check_api_key",
    "token_prices",
]

_NO_KEY_MESSAGE: Final = (
    f"--provider openai needs {OPENAI_API_KEY_VARIABLE}, which is unset or empty, so nothing "
    "was sent. Set it, or point --openai-base-url at a loopback server that needs no key."
)
_UNSENDABLE_KEY_MESSAGE: Final = (
    f"{OPENAI_API_KEY_VARIABLE} holds a space, a control character such as a newline, or a "
    "character that isn't ASCII, so it can't be sent. Set it to the key alone."
)
_UNPRICED_CAP_MESSAGE: Final = (
    "A cost cap (--max-cost-usd, or the policy's [cost] ceiling) with --provider openai needs "
    "--price-input-usd-per-mtok and --price-output-usd-per-mtok, since its cost comes from "
    "them, so nothing was sent. Pass both prices."
)
_UNOFFERED_TOOLS_MESSAGE: Final = (
    "The model asked to call tools, but this review offered none, so it gave no review. Try again."
)
_UNREPORTED_COST_MESSAGE: Final = (
    "The OpenAI-compatible endpoint didn't report the tokens a turn used, so its cost can't be "
    "kept within --max-cost-usd and the review stopped. Use an endpoint that reports usage, or "
    "review without a cost cap."
)
_TOO_MANY_CALLS_MESSAGE: Final = (
    "The model asked for more tool calls in one message than --max-tool-calls allows, so none "
    "was run and the review stopped. Try again, or raise --max-tool-calls."
)
_COST_CAP_MESSAGE: Final = (
    "The model reached the review's cost cap before it finished the review. Raise "
    "--max-cost-usd, up to the policy's ceiling if it has one, or review a smaller diff."
)
_TURN_CAP_MESSAGE: Final = (
    "The model ran out of turns before it finished the review: its turn cap is --max-tool-calls "
    "plus 2. Raise --max-tool-calls, or review a smaller diff."
)
_DEADLINE_MESSAGE: Final = (
    "The OpenAI-compatible endpoint didn't finish in time. Try again, or raise --deadline-s."
)


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


def token_prices(settings: Settings) -> Prices | None:
    """The prices the review's cost comes from, or None if neither is set and nothing is capped.

    Raises:
        ConfigError: Naming the missing price flag, if only one price is set, or if a cost cap
            is set without both, since nothing could enforce it.
    """
    input_price = settings.price_input_usd_per_mtok
    output_price = settings.price_output_usd_per_mtok
    if input_price is not None and output_price is not None:
        return Prices(input_price, output_price)
    if input_price is not None or output_price is not None:
        missing = "input" if input_price is None else "output"
        raise ConfigError(
            f"--price-{missing}-usd-per-mtok (or REVIEW_BOT_PRICE_{missing.upper()}_USD_PER_MTOK) "
            "is missing, and --provider openai's cost comes from both prices, so nothing was "
            "sent. Pass both prices."
        )
    if settings.max_cost_usd is not None:
        raise ConfigError(_UNPRICED_CAP_MESSAGE)
    return None


class OpenAICompatibleProvider:
    """Reviews through an OpenAI-compatible Chat Completions endpoint.

    Diff mode offers no tools. Context mode offers the request's executor's tools as functions,
    and runs each call the model asks for through it, up to ``max_tool_calls`` per message.
    """

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
            settings: Supplies the output token cap, the retries, the API timeout, the overall
                deadline, the tool call cap, the cost cap and the token prices.
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
                character that can't be sent in a header (``check_api_key``); or if only one
                price is set, or a cost cap without both (``token_prices``).
        """
        check_base_url(base_url)
        check_api_key(base_url, api_key)
        self._prices = token_prices(settings)
        self._settings = settings
        self._base_url = base_url
        self._endpoint = ChatEndpoint(
            url=f"{base_url.rstrip('/')}/chat/completions",
            api_key=api_key or None,
            transport=UrllibTransport() if transport is None else transport,
            max_retries=settings.max_retries,
            api_timeout_s=settings.api_timeout_ms / 1000,
            sleep=sleep,
            clock=clock,
        )

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        """Converse until the model answers, by the deadline, which bounds the whole review.

        Each POST retries ``transient`` and ``rate_limit`` failures. ``cost_usd`` is the cost of
        all the turns at the token prices, or None without them.

        Raises:
            ProviderError: For every failure of a known kind; an error response's text is kept
                only as its diagnostics.
        """
        tools = request.tools
        chat = ChatRequest(
            model=request.model,
            system_prompt=request.system_prompt,
            user_prompt=request.user_prompt,
            max_output_tokens=self._settings.max_output_tokens,
            base_url=self._base_url,
            tools=() if tools is None else tools.definitions,
        )
        deadline = asyncio.get_running_loop().time() + self._settings.deadline_s
        try:
            async with asyncio.timeout_at(deadline):
                return await self._converse(chat, tools, deadline=deadline)
        except TimeoutError as exc:
            raise ProviderError(ErrorCategory.TRANSIENT, _DEADLINE_MESSAGE) from exc

    async def _converse(
        self, chat: ChatRequest, tools: ToolExecutor | None, *, deadline: float
    ) -> ReviewResponse:
        """Post the conversation, running each tool call the model asks for, until it answers.

        A message asking for more calls than ``max_tool_calls`` fails ``invalid_output``. The
        caps are checked after each turn that asks for tools, so its calls never run once one
        is reached, and the turn that answers is returned whatever it cost (ADR 0015). A
        turn whose usage is unusable counts as no tokens, or with a cost cap, as costing the
        whole cap.
        """
        replies: list[JsonValue] = []
        warnings: list[str] = []
        usage = TokenUsage(0, 0)
        turns = 0
        while True:
            reply = await self._endpoint.post_with_retries(
                chat.body(replies), deadline=deadline, warnings=warnings
            )
            turns += 1
            is_reported = reply.usage is not None
            usage = _added(usage, reply.usage or TokenUsage(0, 0))
            cost_usd = self._cost_usd(usage, is_reported=is_reported)
            if isinstance(reply, Completion):
                return ReviewResponse(
                    reply.findings, usage, warnings=tuple(warnings), cost_usd=cost_usd
                )
            if tools is None:
                raise ProviderError(ErrorCategory.INVALID_OUTPUT, _UNOFFERED_TOOLS_MESSAGE)
            # So no message can grow the conversation, the ledger or the audit log past the cap.
            if len(reply.calls) > self._settings.max_tool_calls:
                raise ProviderError(ErrorCategory.INVALID_OUTPUT, _TOO_MANY_CALLS_MESSAGE)
            self._check_caps(turns, cost_usd, is_reported=is_reported)
            results = [await tools.call(call.name, call.arguments) for call in reply.calls]
            replies.extend(tool_replies(reply, results))

    def _cost_usd(self, usage: TokenUsage, *, is_reported: bool) -> Decimal | None:
        """What ``usage`` costs at the token prices, or None without them.

        If the last turn's usage wasn't reported, that turn also costs the cost cap, if any.
        """
        if self._prices is None:
            return None
        cost_usd = token_cost_usd(usage.input_tokens, usage.output_tokens, self._prices)
        max_cost_usd = self._settings.max_cost_usd
        if is_reported or max_cost_usd is None:
            return cost_usd
        return cost_usd + max_cost_usd

    def _check_caps(self, turns: int, cost_usd: Decimal | None, *, is_reported: bool) -> None:
        """Raise a ``permanent`` ProviderError naming the cap if the review may go no further.

        ``cost_usd`` is None only without prices, so with no cost cap (``token_prices``). A turn
        whose usage wasn't reported reaches the cost cap, whatever came before it.
        """
        max_cost_usd = self._settings.max_cost_usd
        if max_cost_usd is not None and not is_reported:
            raise ProviderError(ErrorCategory.PERMANENT, _UNREPORTED_COST_MESSAGE, cap="cost")
        if max_cost_usd is not None and cost_usd is not None and cost_usd > max_cost_usd:
            raise ProviderError(ErrorCategory.PERMANENT, _COST_CAP_MESSAGE, cap="cost")
        if turns >= self._settings.max_tool_calls + 2:
            raise ProviderError(ErrorCategory.PERMANENT, _TURN_CAP_MESSAGE, cap="turns")


def _added(usage: TokenUsage, more: TokenUsage) -> TokenUsage:
    return TokenUsage(
        usage.input_tokens + more.input_tokens, usage.output_tokens + more.output_tokens
    )
