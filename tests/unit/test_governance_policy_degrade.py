"""The policy's ``[routing] degrade_to_rules`` (P4-S8)."""

import pytest

from review_bot.governance.policy import BUILTIN_POLICY, Policy, PolicyError, parse_policy

SOURCE = "p.toml"


def parse(text: str) -> Policy:
    return parse_policy(f'version = "v"\n{text}'.encode(), source=SOURCE)


def test_degrade_to_rules_is_read_from_routing_without_needing_a_fallback() -> None:
    assert parse("[routing]\ndegrade_to_rules = true\n").degrade_to_rules is True
    assert parse("[routing]\ndegrade_to_rules = false\n").degrade_to_rules is False


def test_degrade_to_rules_is_off_by_default_and_in_the_builtin_policy() -> None:
    for policy in (parse(""), parse("[routing]\n"), BUILTIN_POLICY):
        assert policy.degrade_to_rules is False


@pytest.mark.parametrize("value", ['"true"', "1", "0"])
def test_parse_policy_refuses_a_degrade_to_rules_that_isnt_a_boolean(value: str) -> None:
    with pytest.raises(PolicyError, match=r"invalid value for 'routing\.degrade_to_rules'"):
        parse(f"[routing]\ndegrade_to_rules = {value}\n")
