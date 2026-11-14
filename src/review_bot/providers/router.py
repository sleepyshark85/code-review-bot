"""A provider that tries the policy's routes in order, falling back only when one is unavailable.

The fallback is tried once, only after ``unavailable``, ``transient`` or ``rate_limit``, so
routing never bypasses a spend limit, a refusal or a cap (ADR 0021). Every route gets the same
request, tools included, so the tool caps cover the whole review; the deadline does too, so a
fallback gets only the time left.
"""

import asyncio
import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.governance.policy import route_name
from review_bot.providers.base import (
    Attempt,
    DeniedTool,
    LLMProvider,
    ReviewRequest,
    ReviewResponse,
)

# ``Attempt`` is defined beside ``ReviewResponse``, which holds it, and named here too, with the
# router that makes it (Phase 4 design, Interfaces).
__all__ = ["Attempt", "Route", "RoutingProvider"]

_ROUTABLE: Final = frozenset(
    {ErrorCategory.UNAVAILABLE, ErrorCategory.TRANSIENT, ErrorCategory.RATE_LIMIT}
)
_DEADLINE_MESSAGE: Final = (
    "The review didn't finish in time across its routes. Try again, or raise --deadline-s."
)
_NO_CREDENTIAL_MESSAGE: Final = (
    "No route of the review has a credential, so nothing was sent. Set ANTHROPIC_API_KEY for "
    "Claude, or OPENAI_API_KEY for the policy's OpenAI-compatible model."
)


@dataclass(frozen=True, slots=True)
class Route:
    """One model a review may be routed to.

    ``make_provider`` builds its provider to finish within the seconds it is given, the time
    left of the review's deadline, since a provider may wait on a call that can't be cancelled.
    It is None when the route has no credential, so it is skipped.
    """

    provider_name: str
    model: str
    make_provider: Callable[[float], LLMProvider] | None

    @property
    def name(self) -> str:
        """Its name in reports (``route_name``)."""
        return route_name(self.provider_name, self.model)


class RoutingProvider:
    """Tries each route in order until one serves the review or fails in a way not to route."""

    def __init__(self, routes: Sequence[Route], *, deadline_s: float) -> None:
        """Hold the routes, primary first, and the deadline that bounds the whole review.

        Raises:
            ValueError: If there are no routes.
        """
        if not routes:
            raise ValueError("A routed review needs at least one route.")
        self._routes = tuple(routes)
        self._deadline_s = deadline_s

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        """Send ``request`` to each route in turn, with that route's model, until one answers.

        A route without a credential is skipped. Only ``unavailable``, ``transient`` and
        ``rate_limit`` failures with no cap are routed. After any other, or with no time left,
        no further route is tried; one skipped for time is recorded as such.

        Returns:
            The serving route's response, with every attempt in ``attempts`` and the tools
            denied across them all.

        Raises:
            ProviderError: The last failure, with every attempt in ``attempts``: ``transient``
                if the deadline passed during a route, or ``auth`` if no route had a
                credential.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._deadline_s
        attempts: list[Attempt] = []
        denied: list[DeniedTool] = []
        failure: ProviderError | None = None
        is_out_of_time = False
        for route in self._routes:
            if route.make_provider is None:
                attempts.append(Attempt(route.name, None, None, skipped="no_credential"))
                continue
            left_s = deadline - loop.time()
            # The flag too, since the loop may end a timeout a little before its deadline.
            if is_out_of_time or left_s <= 0:
                attempts.append(Attempt(route.name, None, None, skipped="deadline"))
                break
            provider = route.make_provider(left_s)
            routed = dataclasses.replace(request, model=route.model)
            try:
                async with asyncio.timeout_at(deadline):
                    response = await provider.review(routed)
            except TimeoutError as exc:
                is_out_of_time = True
                failure = ProviderError(ErrorCategory.TRANSIENT, _DEADLINE_MESSAGE)
                failure.__cause__ = exc
            except ProviderError as exc:
                failure = exc
            else:
                attempts.append(Attempt(route.name, None, response.cost_usd))
                return dataclasses.replace(
                    response,
                    attempts=tuple(attempts),
                    denied_tools=(*denied, *response.denied_tools),
                )
            attempts.append(Attempt(route.name, failure.category, None))
            denied.extend(failure.denied_tools)
            # A cap was the review's own limit, so another route would only spend it again.
            if failure.category not in _ROUTABLE or failure.cap is not None:
                break
        if failure is None:
            failure = ProviderError(ErrorCategory.AUTH, _NO_CREDENTIAL_MESSAGE)
        failure.attempts = tuple(attempts)
        failure.denied_tools = tuple(denied)
        raise failure
