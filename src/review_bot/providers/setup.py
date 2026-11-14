"""Building a run's provider, routed to the policy's fallback, and checking it can run first.

The entry points call this, so ``review-bot`` and ``python -m review_bot.pr`` build and check
providers the same way (ADR 0002, ADR 0021).
"""

from collections.abc import Callable, Mapping
from typing import Final

from review_bot.errors import ConfigError
from review_bot.governance.policy import Policy, fallback_for, review_mode, route_name
from review_bot.providers.base import LLMProvider
from review_bot.providers.mock import MockProvider
from review_bot.providers.openai_compat import (
    OpenAICompatibleProvider,
    check_api_key,
    token_prices,
)
from review_bot.providers.openai_wire import is_loopback_url
from review_bot.providers.router import Route, RoutingProvider
from review_bot.settings import Settings, has_claude_credential

__all__ = [
    "build_provider",
    "build_routed_provider",
    "can_fall_back",
    "check_provider_settings",
    "fallback_settings",
]

_OPENAI_MODEL_MISSING: Final = (
    "--provider openai needs --model (or REVIEW_BOT_MODEL), since the default model is "
    "Claude's, so nothing was sent. Pass the model the endpoint serves."
)


def build_provider(settings: Settings, *, openai_api_key: str | None = None) -> LLMProvider:
    """Create the selected provider. Nothing starts until its ``review`` is called.

    ``openai_api_key`` is the key the entry point took from ``OPENAI_API_KEY``; only the
    ``openai`` provider uses it.

    Raises:
        ConfigError: For ``openai``, if there is no key and its base URL's host isn't loopback,
            or if only one price is set, or a cost cap without both.
    """
    if settings.provider == "claude":
        # Deferred so only a Claude run pays to load the SDK. If it can't be imported, the error
        # reaches ``main``'s catch-all, like any other failure.
        from review_bot.providers.claude_agent import ClaudeAgentProvider  # noqa: PLC0415 - see above

        return ClaudeAgentProvider(settings)
    if settings.provider == "openai":
        return OpenAICompatibleProvider(
            settings, base_url=settings.openai_base_url, api_key=openai_api_key
        )
    return MockProvider()


def build_routed_provider(
    settings: Settings,
    policy: Policy,
    *,
    make_provider: Callable[[Settings], LLMProvider],
    openai_api_key: str | None,
    environ: Mapping[str, str],
) -> LLMProvider:
    """The settings' provider, routed to the policy's fallback if it has one for the review.

    ``make_provider`` builds a provider from its settings, as ``build_provider`` does with the
    run's key; the entry point passes its own, so a test can replace it there. Without a
    fallback (``fallback_settings``), it builds the settings' provider, as before. With one,
    each route's provider is built with the time left as its deadline, except that a route
    without a credential (``_has_credential``) has none, so the review skips it.
    """
    fallback = fallback_settings(settings, policy)
    if fallback is None:
        return make_provider(settings)
    routes = [
        _route(each, make_provider, openai_api_key=openai_api_key, environ=environ)
        for each in (settings, fallback)
    ]
    return RoutingProvider(routes, deadline_s=settings.deadline_s)


def _route(
    settings: Settings,
    make_provider: Callable[[Settings], LLMProvider],
    *,
    openai_api_key: str | None,
    environ: Mapping[str, str],
) -> Route:
    """The route to the provider ``settings`` select, with no provider if it has no credential."""
    if not _has_credential(settings, openai_api_key=openai_api_key, environ=environ):
        return Route(settings.provider, settings.model, None)

    def within(left_s: float) -> LLMProvider:
        return make_provider(settings.model_copy(update={"deadline_s": left_s}))

    return Route(settings.provider, settings.model, within)


def fallback_settings(settings: Settings, policy: Policy) -> Settings | None:
    """The settings the policy's fallback reviews with: the run's, with its provider and model.

    An ``openai`` fallback with a ``base_url`` is reached there. None for the mock provider,
    which is never routed; if the policy has no fallback for the review's mode; or if the
    fallback is the run's own provider, model and base URL.
    """
    if settings.provider == "mock":
        return None
    entry = fallback_for(policy, review_mode(settings))
    if entry is None:
        return None
    update = {"provider": entry.provider, "model": entry.model}
    if entry.base_url is not None:
        update["openai_base_url"] = entry.base_url
    fallback = settings.model_copy(update=update)
    if _endpoint(fallback) == _endpoint(settings):
        return None
    return fallback


def _endpoint(settings: Settings) -> tuple[str, str, str]:
    return settings.provider, settings.model, settings.openai_base_url


def can_fall_back(
    settings: Settings, policy: Policy, *, openai_api_key: str | None, environ: Mapping[str, str]
) -> bool:
    """Whether the review has a fallback (``fallback_settings``) with its credential."""
    fallback = fallback_settings(settings, policy)
    return fallback is not None and _has_credential(
        fallback, openai_api_key=openai_api_key, environ=environ
    )


def _has_credential(
    settings: Settings, *, openai_api_key: str | None, environ: Mapping[str, str]
) -> bool:
    """Whether the provider ``settings`` select can authenticate.

    Claude needs ``ANTHROPIC_API_KEY`` only in CI (``has_claude_credential``); ``openai`` needs
    its key unless its host is loopback; the mock needs nothing.
    """
    if settings.provider == "claude":
        return has_claude_credential(environ)
    if settings.provider == "openai":
        return bool(openai_api_key) or is_loopback_url(settings.openai_base_url)
    return True


def check_provider_settings(
    settings: Settings, policy: Policy, *, openai_api_key: str | None
) -> None:
    """Refuse, before anything is sent, settings the selected provider can't run with.

    Called once ``settings`` are within ``policy``, so a cost cap from its ceiling counts. The
    policy's fallback for the review (``fallback_settings``) is checked too, if it is
    ``openai`` (``_check_openai_fallback``).

    Raises:
        ConfigError: For ``openai``, if no model was given, since the default is Claude's; if
            only one price is set, or a cost cap without both (``token_prices``); or if there
            is no key and its base URL's host isn't loopback. For an ``openai`` fallback, as
            ``_check_openai_fallback`` says.
    """
    fallback = fallback_settings(settings, policy)
    if fallback is not None and fallback.provider == "openai":
        _check_openai_fallback(fallback, openai_api_key=openai_api_key)
    if settings.provider != "openai":
        return
    # Given by a flag or variable, whatever its value: the default names a Claude model.
    if "model" not in settings.model_fields_set:
        raise ConfigError(_OPENAI_MODEL_MISSING)
    token_prices(settings)
    check_api_key(settings.openai_base_url, openai_api_key)


def _check_openai_fallback(fallback: Settings, *, openai_api_key: str | None) -> None:
    """Check that an ``openai`` fallback can run: it has its prices, and any key can be sent.

    Without a key it is only skipped, so a review the primary serves still runs.

    Raises:
        ConfigError: Naming the fallback's route, if only one price is set, or a cost cap
            without both; or if its key can't be sent in a header.
    """
    try:
        token_prices(fallback)
    except ConfigError as exc:
        route = route_name(fallback.provider, fallback.model)
        raise ConfigError(
            f"The policy's fallback {route} needs both --price-input-usd-per-mtok and "
            "--price-output-usd-per-mtok, since its cost comes from them, when either is set "
            "or under a cost cap (--max-cost-usd, or the policy's [cost] ceiling), so nothing "
            "was sent. Pass both prices."
        ) from exc
    if openai_api_key:
        check_api_key(fallback.openai_base_url, openai_api_key)
