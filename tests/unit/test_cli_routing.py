"""Building the routed provider from the policy's fallback, and what is checked first (P4-S7)."""

import asyncio
from collections.abc import Mapping
from decimal import Decimal

import pytest
from openai_samples import API_KEY, completion, error_response, transport

from review_bot import cli
from review_bot.errors import ConfigError, ErrorCategory, ProviderError
from review_bot.governance.policy import BUILTIN_POLICY, Policy, parse_policy
from review_bot.providers import openai_compat
from review_bot.providers.base import LLMProvider, ReviewRequest, ReviewResponse
from review_bot.providers.mock import MockProvider
from review_bot.providers.router import Attempt, RoutingProvider
from review_bot.settings import Settings

LOOPBACK = "http://127.0.0.1:8000/v1"
FALLBACK_URL = "https://fallback.test/v1"
CI: Mapping[str, str] = {"CI": "true"}
NOT_CI: Mapping[str, str] = {}
REQUEST = ReviewRequest(system_prompt="system", user_prompt="user", model="m0")


def policy(*, fallback_modes: str = '["diff"]') -> Policy:
    text = f"""version = "v"
[[models]]
provider = "openai"
model = "m0"
modes = ["diff", "context"]
[[models]]
provider = "claude"
model = "claude-opus-5-5"
modes = ["diff", "context"]
[[models]]
provider = "openai"
model = "m1"
modes = {fallback_modes}
base_url = "{FALLBACK_URL}"
[routing]
fallback = {{ provider = "openai", model = "m1", base_url = "{FALLBACK_URL}" }}
"""
    return parse_policy(text.encode(), source="p.toml")


PRIMARY = Settings(provider="openai", model="m0", openai_base_url=LOOPBACK, max_retries=0)


def build(
    settings: Settings, *, key: str | None = API_KEY, environ: Mapping[str, str] = NOT_CI
) -> LLMProvider:
    return cli.build_routed_provider(settings, policy(), openai_api_key=key, environ=environ)


def review(provider: LLMProvider) -> ReviewResponse:
    assert isinstance(provider, RoutingProvider)
    return asyncio.run(provider.review(REQUEST))


def test_without_a_fallback_the_provider_is_built_as_before() -> None:
    provider = cli.build_routed_provider(
        Settings(provider="mock"), BUILTIN_POLICY, openai_api_key=None, environ={}
    )

    assert isinstance(provider, MockProvider)


def test_an_unavailable_primary_routes_to_the_fallback_at_its_base_url_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = transport(error_response(503), completion())
    monkeypatch.setattr(openai_compat, "UrllibTransport", lambda: fake)

    response = review(build(PRIMARY))

    assert [(sent.url, sent.body["model"]) for sent in fake.sent] == [
        (f"{LOOPBACK}/chat/completions", "m0"),
        (f"{FALLBACK_URL}/chat/completions", "m1"),
    ]
    assert [attempt.route for attempt in response.attempts] == ["openai:m0", "openai:m1"]
    assert response.attempts[1].category is None


def test_a_fallback_without_its_key_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = transport(error_response(503))
    monkeypatch.setattr(openai_compat, "UrllibTransport", lambda: fake)

    with pytest.raises(ProviderError) as raised:
        review(build(PRIMARY, key=None))

    assert raised.value.attempts[1] == Attempt("openai:m1", None, None, skipped="no_credential")
    assert len(fake.sent) == 1


def test_claude_in_ci_without_its_key_is_skipped_for_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openai_compat, "UrllibTransport", lambda: transport(completion()))

    response = review(build(Settings(provider="claude"), environ=CI))

    assert response.attempts == (
        Attempt("claude:claude-opus-5-5", None, None, skipped="no_credential"),
        Attempt("openai:m1", None, None),
    )


def test_fallback_settings_are_the_runs_with_the_fallbacks_provider_model_and_url() -> None:
    settings = Settings(provider="claude", max_cost_usd=Decimal("0.60"))

    fallback = cli.fallback_settings(settings, policy())

    assert fallback == settings.model_copy(
        update={"provider": "openai", "model": "m1", "openai_base_url": FALLBACK_URL}
    )


@pytest.mark.parametrize(
    "settings",
    [Settings(provider="claude", context="repo"), Settings(provider="openai", model="m1")],
    ids=["mode-not-listed", "is-the-primary"],
)
def test_fallback_settings_are_none_for_an_unlisted_mode_or_the_primary_itself(
    settings: Settings,
) -> None:
    assert cli.fallback_settings(settings, policy()) is None


@pytest.mark.parametrize(
    ("key", "expected"), [(API_KEY, True), (None, False)], ids=["with-key", "without-key"]
)
def test_can_fall_back_only_to_a_fallback_with_its_credential(
    key: str | None, *, expected: bool
) -> None:
    settings = Settings(provider="claude")

    assert cli.can_fall_back(settings, policy(), openai_api_key=key, environ=CI) is expected


def test_cannot_fall_back_without_a_fallback_for_the_mode() -> None:
    settings = Settings(provider="claude", context="repo")

    assert not cli.can_fall_back(settings, policy(), openai_api_key=API_KEY, environ=CI)


def test_check_provider_settings_refuses_an_openai_fallback_with_a_cap_and_no_prices() -> None:
    settings = Settings(provider="claude", max_cost_usd=Decimal("0.60"))

    with pytest.raises(ConfigError, match="--price-input-usd-per-mtok"):
        cli.check_provider_settings(settings, policy(), openai_api_key=API_KEY)


def test_check_provider_settings_lets_a_priced_fallback_run_with_or_without_its_key() -> None:
    settings = Settings(
        provider="claude",
        max_cost_usd=Decimal("0.60"),
        price_input_usd_per_mtok=Decimal(1),
        price_output_usd_per_mtok=Decimal(2),
    )

    for key in (API_KEY, None):
        cli.check_provider_settings(settings, policy(), openai_api_key=key)


def test_check_provider_settings_refuses_a_fallback_key_that_cant_be_sent() -> None:
    with pytest.raises(ConfigError, match="OPENAI_API_KEY holds"):
        cli.check_provider_settings(
            Settings(provider="claude"), policy(), openai_api_key="key with space"
        )


def test_routed_failure_keeps_the_last_routes_category(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = transport(error_response(503), error_response(401))
    monkeypatch.setattr(openai_compat, "UrllibTransport", lambda: fake)

    with pytest.raises(ProviderError) as raised:
        review(build(PRIMARY))

    assert raised.value.category is ErrorCategory.AUTH
    assert [attempt.category for attempt in raised.value.attempts] == [
        ErrorCategory.TRANSIENT,
        ErrorCategory.AUTH,
    ]
