"""The policy's ``[[models]]`` and ``[routing] fallback`` (P4-S7)."""

from pathlib import Path

import pytest

from review_bot.errors import ConfigError
from review_bot.governance.policy import (
    BUILTIN_POLICY,
    ModelEntry,
    Policy,
    PolicyError,
    check_settings,
    fallback_for,
    parse_policy,
    review_mode,
)
from review_bot.settings import Settings

SOURCE = "p.toml"
CLAUDE = '[[models]]\nprovider = "claude"\nmodel = "claude-opus-5-5"\nmodes = ["diff", "context"]\n'
OPENAI = (
    '[[models]]\nprovider = "openai"\nmodel = "m1"\nmodes = ["diff"]\n'
    'base_url = "https://llm.test/v1"\n'
)
FALLBACK = (
    '[routing]\nfallback = { provider = "openai", model = "m1", '
    'base_url = "https://llm.test/v1" }\n'
)
CLAUDE_ENTRY = ModelEntry("claude", "claude-opus-5-5", frozenset({"diff", "context"}))
OPENAI_ENTRY = ModelEntry("openai", "m1", frozenset({"diff"}), "https://llm.test/v1")


def parse(text: str) -> Policy:
    return parse_policy(f'version = "v"\n{text}'.encode(), source=SOURCE)


def test_parse_policy_reads_the_models_primary_first_and_the_fallback_entry() -> None:
    policy = parse(CLAUDE + OPENAI + FALLBACK)

    assert policy.models == (CLAUDE_ENTRY, OPENAI_ENTRY)
    assert policy.fallback == OPENAI_ENTRY


def test_without_models_or_routing_and_in_the_builtin_policy_any_model_and_no_fallback() -> None:
    for policy in (parse(""), BUILTIN_POLICY):
        assert (policy.models, policy.fallback) == ((), None)


def test_model_entry_route_is_provider_colon_model() -> None:
    assert OPENAI_ENTRY.route == "openai:m1"


@pytest.mark.parametrize(
    ("text", "named"),
    [
        (CLAUDE.replace('"claude"\nmodel', '"mock"\nmodel'), "it must be claude or openai"),
        (CLAUDE.replace('"claude-opus-5-5"', '"-bad"'), "it must be a model name"),
        (CLAUDE.replace('["diff", "context"]', "[]"), "it must list at least one"),
        (CLAUDE.replace('"context"]', '"review"]'), "each mode must be diff or context"),
        (CLAUDE.replace('"context"]', '"diff"]'), "lists 'diff' more than once"),
        (CLAUDE + 'base_url = "https://x.test/v1"\n', "only an openai model has a base_url"),
        (OPENAI.replace("https://llm", "http://llm"), "must use https"),
        (CLAUDE + CLAUDE, "lists 'claude:claude-opus-5-5' more than once"),
        (CLAUDE + "colour = 1\n", "unknown key 'models[0].colour'"),
        (CLAUDE + '[routing]\nfallback = { provider = "claude" }\n', "routing.fallback.model"),
        (CLAUDE + "[routing]\nretries = 2\n", "unknown key 'routing.retries'"),
    ],
)
def test_parse_policy_refuses_an_invalid_model_or_routing(text: str, named: str) -> None:
    with pytest.raises(PolicyError) as raised:
        parse(text)

    assert named in str(raised.value)


@pytest.mark.parametrize(
    "models",
    [CLAUDE, CLAUDE + OPENAI.replace('base_url = "https://llm.test/v1"\n', "")],
    ids=["not-listed", "other-base-url"],
)
def test_parse_policy_refuses_a_fallback_that_isnt_a_listed_entry(models: str) -> None:
    with pytest.raises(PolicyError) as raised:
        parse(models + FALLBACK)

    message = str(raised.value)
    assert f"The policy file {SOURCE} is invalid: the [routing] fallback openai:m1 " in message
    assert "List it there too" in message


def test_review_mode_is_context_with_a_repository_to_read() -> None:
    assert review_mode(Settings()) == "diff"
    assert review_mode(Settings(context="repo")) == "context"
    assert review_mode(Settings(context_repo=Path())) == "context"


def test_fallback_for_a_mode_the_fallback_doesnt_list_is_none() -> None:
    policy = parse(CLAUDE + OPENAI + FALLBACK)

    assert fallback_for(policy, "diff") == OPENAI_ENTRY
    assert fallback_for(policy, "context") is None
    assert fallback_for(BUILTIN_POLICY, "diff") is None


@pytest.mark.parametrize(
    "settings",
    [
        Settings(provider="claude", model="claude-opus-5-5", context="repo"),
        Settings(provider="openai", model="m1", openai_base_url="https://llm.test/v1"),
        Settings(provider="mock", model="anything", context="repo"),
    ],
    ids=["listed-context", "listed-diff", "mock-exempt"],
)
def test_check_settings_allows_a_listed_model_in_a_listed_mode(settings: Settings) -> None:
    assert check_settings(settings, parse(CLAUDE + OPENAI)) == settings


@pytest.mark.parametrize(
    ("settings", "asked"),
    [
        (Settings(provider="claude", model="other"), "--provider claude --model other in diff"),
        (
            Settings(provider="openai", model="m1", context="repo"),
            "openai --model m1 in context mode at https://api.openai.com/v1 isn't",
        ),
        (
            Settings(provider="openai", model="m1", openai_base_url="https://other.test/v1"),
            "openai --model m1 in diff mode at https://other.test/v1 isn't",
        ),
    ],
    ids=["unlisted-model", "unlisted-mode", "other-base-url"],
)
def test_check_settings_refuses_another_model_or_mode_naming_the_allowed_entries(
    settings: Settings, asked: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        check_settings(settings, parse(CLAUDE + OPENAI))

    message = str(raised.value)
    assert asked in message
    assert "policy `v` (p.toml)" in message
    assert (
        "It allows: claude:claude-opus-5-5 (diff, context), openai:m1 (diff) at "
        "https://llm.test/v1." in message
    )


def test_check_settings_without_models_allows_any_model() -> None:
    settings = Settings(provider="claude", model="other")

    assert check_settings(settings, parse("")) == settings
