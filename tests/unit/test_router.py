"""The routing provider: the fallback only on availability failures, within one deadline."""

import asyncio
import dataclasses
from collections.abc import Callable, Mapping
from decimal import Decimal

import pytest

from review_bot.errors import ErrorCategory, ProviderCap, ProviderError
from review_bot.findings import Category, Finding, Severity
from review_bot.providers.base import (
    DeniedTool,
    LLMProvider,
    ReviewRequest,
    ReviewResponse,
    TokenUsage,
)
from review_bot.providers.mock import MockProvider
from review_bot.providers.router import Attempt, Route, RoutingProvider

REQUEST = ReviewRequest(system_prompt="system", user_prompt="user", model="claude-opus-5-5")
FALLBACK_FINDING = Finding(
    file="a.py",
    line=1,
    severity=Severity.HIGH,
    category=Category.LOGIC,
    rationale="Found by the fallback.",
    confidence=0.9,
)
ROUTABLE = [ErrorCategory.UNAVAILABLE, ErrorCategory.TRANSIENT, ErrorCategory.RATE_LIMIT]
NOT_ROUTABLE = [
    ErrorCategory.AUTH,
    ErrorCategory.REFUSAL,
    ErrorCategory.INVALID_OUTPUT,
    ErrorCategory.PERMANENT,
]
DEADLINE_S = 60.0


def failing(category: ErrorCategory, *, cap: ProviderCap | None = None) -> MockProvider:
    return MockProvider(error=ProviderError(category, f"{category} failure", cap=cap))


def fallback(cost_usd: str = "0.20") -> MockProvider:
    return MockProvider(findings=[FALLBACK_FINDING], cost_usd=Decimal(cost_usd))


def given(provider: LLMProvider | None) -> Callable[[float], LLMProvider] | None:
    """A route's maker of ``provider``, whatever time is left; None for no provider."""
    if provider is None:
        return None
    return lambda _left_s: provider


def router(primary: MockProvider | None, second: MockProvider | None) -> RoutingProvider:
    return RoutingProvider(
        [Route("claude", "claude-opus-5-5", given(primary)), Route("openai", "m1", given(second))],
        deadline_s=DEADLINE_S,
    )


def review(provider: RoutingProvider) -> ReviewResponse:
    return asyncio.run(provider.review(REQUEST))


def failure_of(provider: RoutingProvider) -> ProviderError:
    with pytest.raises(ProviderError) as raised:
        review(provider)
    return raised.value


def test_route_name_is_provider_colon_model() -> None:
    assert Route("openai", "m1", None).name == "openai:m1"


def test_a_primary_that_serves_is_the_only_attempt() -> None:
    primary, second = MockProvider(cost_usd=Decimal("0.10")), fallback()

    response = review(router(primary, second))

    assert response.attempts == (Attempt("claude:claude-opus-5-5", None, Decimal("0.10")),)
    assert (len(primary.requests), second.requests) == (1, [])


@pytest.mark.parametrize("category", ROUTABLE)
def test_an_availability_failure_sends_the_same_request_to_the_fallback_once(
    category: ErrorCategory,
) -> None:
    primary, second = failing(category), fallback()

    response = review(router(primary, second))

    assert primary.requests == [REQUEST]
    [sent] = second.requests
    assert sent.model == "m1"
    assert (sent.system_prompt, sent.user_prompt, sent.tools, sent.policy) == (
        REQUEST.system_prompt,
        REQUEST.user_prompt,
        REQUEST.tools,
        REQUEST.policy,
    )
    assert response.findings == (FALLBACK_FINDING,)
    assert response.attempts == (
        Attempt("claude:claude-opus-5-5", category, None),
        Attempt("openai:m1", None, Decimal("0.20")),
    )


@pytest.mark.parametrize("category", NOT_ROUTABLE)
def test_a_failure_that_isnt_about_availability_is_raised_without_a_fallback(
    category: ErrorCategory,
) -> None:
    second = fallback()

    error = failure_of(router(failing(category), second))

    assert error.category is category
    assert error.attempts == (Attempt("claude:claude-opus-5-5", category, None),)
    assert second.requests == []


@pytest.mark.parametrize("category", ROUTABLE)
def test_a_failure_at_a_cap_is_never_routed(category: ErrorCategory) -> None:
    second = fallback()

    error = failure_of(router(failing(category, cap="cost"), second))

    assert (error.cap, len(error.attempts), second.requests) == ("cost", 1, [])


def test_when_both_routes_fail_the_last_failure_is_raised_with_both_attempts() -> None:
    error = failure_of(
        router(failing(ErrorCategory.UNAVAILABLE), failing(ErrorCategory.RATE_LIMIT))
    )

    assert error.category is ErrorCategory.RATE_LIMIT
    assert error.attempts == (
        Attempt("claude:claude-opus-5-5", ErrorCategory.UNAVAILABLE, None),
        Attempt("openai:m1", ErrorCategory.RATE_LIMIT, None),
    )


def test_a_route_without_a_credential_is_skipped_for_the_next() -> None:
    response = review(router(None, fallback()))

    assert response.attempts == (
        Attempt("claude:claude-opus-5-5", None, None, skipped="no_credential"),
        Attempt("openai:m1", None, Decimal("0.20")),
    )


def test_a_fallback_without_a_credential_is_skipped_and_the_primarys_failure_raised() -> None:
    error = failure_of(router(failing(ErrorCategory.UNAVAILABLE), None))

    assert error.category is ErrorCategory.UNAVAILABLE
    assert error.attempts[1] == Attempt("openai:m1", None, None, skipped="no_credential")


def test_with_no_route_holding_a_credential_the_review_fails_auth_sending_nothing() -> None:
    error = failure_of(router(None, None))

    assert error.category is ErrorCategory.AUTH
    assert "ANTHROPIC_API_KEY" in str(error)
    assert [attempt.skipped for attempt in error.attempts] == ["no_credential"] * 2


def test_the_tools_denied_on_every_attempt_are_kept_in_order() -> None:
    error = ProviderError(ErrorCategory.TRANSIENT, "down")
    error.denied_tools = (DeniedTool("Bash", "hook"),)
    second = ReviewResponse(
        findings=(), usage=TokenUsage(0, 0), denied_tools=(DeniedTool("Write", "can_use_tool"),)
    )

    class Answers:
        async def review(self, request: ReviewRequest) -> ReviewResponse:
            return second

    provider = RoutingProvider(
        [
            Route("claude", "c", given(MockProvider(error=error))),
            Route("openai", "m1", given(Answers())),
        ],
        deadline_s=DEADLINE_S,
    )

    response = review(provider)

    assert response.denied_tools == (
        DeniedTool("Bash", "hook"),
        DeniedTool("Write", "can_use_tool"),
    )


class Sleeps:
    """Waits, then fails or answers, recording each request."""

    def __init__(self, wait_s: float, error: ProviderError | None = None) -> None:
        self.wait_s = wait_s
        self.error = error
        self.requests: list[ReviewRequest] = []

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        self.requests.append(request)
        await asyncio.sleep(self.wait_s)
        if self.error is not None:
            raise self.error
        return ReviewResponse(findings=(), usage=TokenUsage(0, 0))


def timed(primary: Sleeps, second: Sleeps, deadline_s: float) -> RoutingProvider:
    return RoutingProvider(
        [Route("claude", "c", given(primary)), Route("openai", "m1", given(second))],
        deadline_s=deadline_s,
    )


def test_the_fallback_gets_only_the_time_left_then_the_review_fails_transient() -> None:
    primary = Sleeps(0.05, ProviderError(ErrorCategory.TRANSIENT, "slow"))
    second = Sleeps(30)
    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        with pytest.raises(ProviderError) as raised:
            loop.run_until_complete(timed(primary, second, 0.3).review(REQUEST))
        elapsed = loop.time() - started
    finally:
        loop.close()

    assert raised.value.category is ErrorCategory.TRANSIENT
    assert "--deadline-s" in str(raised.value)
    assert raised.value.attempts == (
        Attempt("claude:c", ErrorCategory.TRANSIENT, None),
        Attempt("openai:m1", ErrorCategory.TRANSIENT, None),
    )
    # The loop may end a timeout a little early; the upper bound is far below the wait.
    assert 0.25 <= elapsed < 5


def test_with_no_time_left_the_fallback_is_skipped_for_the_deadline() -> None:
    primary = Sleeps(5, ProviderError(ErrorCategory.TRANSIENT, "slow"))
    second = Sleeps(0)

    with pytest.raises(ProviderError) as raised:
        asyncio.run(timed(primary, second, 0.05).review(REQUEST))

    assert raised.value.attempts[1] == Attempt("openai:m1", None, None, skipped="deadline")
    assert second.requests == []


def test_each_route_is_built_with_the_time_left_of_the_deadline() -> None:
    lefts: list[float] = []
    primary = Sleeps(0.2, ProviderError(ErrorCategory.UNAVAILABLE, "down"))

    def make(provider: Sleeps) -> Callable[[float], LLMProvider]:
        def make_provider(left_s: float) -> LLMProvider:
            lefts.append(left_s)
            return provider

        return make_provider

    routes = [Route("claude", "c", make(primary)), Route("openai", "m1", make(Sleeps(0)))]
    asyncio.run(RoutingProvider(routes, deadline_s=10).review(REQUEST))

    first, second = lefts
    assert 9.9 < first <= 10
    assert second <= first - 0.15


def test_a_router_needs_a_route() -> None:
    with pytest.raises(ValueError, match="at least one route"):
        RoutingProvider([], deadline_s=DEADLINE_S)


class CountingTools:
    """A tool executor that counts its calls, as one review's caps would."""

    definitions = ()

    def __init__(self) -> None:
        self.calls = 0

    async def call(self, name: str, arguments: Mapping[str, object]) -> str:
        self.calls += 1
        return f"{name} {self.calls}"


def test_both_routes_call_the_same_tool_executor_so_its_caps_cover_the_review() -> None:
    tools = CountingTools()
    request = dataclasses.replace(REQUEST, tools=tools)
    primary = MockProvider(
        tool_calls=[("read", {})] * 5, error=ProviderError(ErrorCategory.TRANSIENT, "down")
    )
    second = MockProvider(tool_calls=[("grep", {})] * 2, findings=[])

    asyncio.run(router(primary, second).review(request))

    assert second.requests[0].tools is tools
    assert tools.calls == 5 + 2
    assert second.tool_results == ["grep 6", "grep 7"]
