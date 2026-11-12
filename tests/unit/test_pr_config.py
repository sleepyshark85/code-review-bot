import dataclasses
from datetime import date

import pytest

from review_bot.errors import ConfigFileError, RepoConfigError
from review_bot.findings import RuleId, Severity
from review_bot.governance.gate import GateClass
from review_bot.governance.policy import BUILTIN_POLICY, Policy
from review_bot.pr.config import (
    DEFAULT_REPO_CONFIG,
    MAX_CONFIG_BYTES,
    RepoConfig,
    gate_classes,
    parse_config_file,
    parse_repo_config,
    with_repo_threshold,
)
from review_bot.rules import DomainRule
from review_bot.settings import Settings, load_settings

BASE = "a" * 40


def parse(text: str) -> RepoConfig:
    return parse_repo_config(text.encode("utf-8"), base_sha=BASE)


def test_parse_repo_config_reads_every_key_and_names_its_source() -> None:
    config = parse('threshold = "medium"\nexclude = ["generated/**", "*.snap"]\n')

    assert config == RepoConfig(
        source=f".review-bot.toml@{BASE}",
        threshold=Severity.MEDIUM,
        exclude=("generated/**", "*.snap"),
        max_open_comments=10,
    )


def test_parse_repo_config_of_an_empty_file_has_the_defaults() -> None:
    config = parse("")

    assert (config.threshold, config.exclude, config.max_open_comments) == (None, (), 10)
    assert DEFAULT_REPO_CONFIG.source == "default"


@pytest.mark.parametrize("limit", [1, 25])
def test_parse_repo_config_accepts_the_comment_limit_bounds(limit: int) -> None:
    assert parse(f"max_open_comments = {limit}").max_open_comments == limit


def test_parse_repo_config_accepts_50_patterns_of_200_characters() -> None:
    patterns = ", ".join(f'"{n:0200d}"' for n in range(50))

    assert len(parse(f"exclude = [{patterns}]").exclude) == 50


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("threshold = ", "isn't valid TOML"),
        ("max_retries = 0", "unknown key 'max_retries'"),
        ("mode = 'comment'", "unknown key 'mode'"),
        ("[tool]\nx = 1", "unknown key 'tool'"),
        ("threshold = 1", "'threshold'"),
        ('threshold = "urgent"', "'threshold'"),
        ('exclude = "*.snap"', "'exclude'"),
        ("exclude = [1]", "'exclude[0]'"),
        ('max_open_comments = "3"', "'max_open_comments'"),
        ("max_open_comments = 3.0", "'max_open_comments'"),
        ("max_open_comments = true", "'max_open_comments'"),
        ("max_open_comments = 0", "'max_open_comments'"),
        ("max_open_comments = 26", "'max_open_comments'"),
        ("exclude = [" + ", ".join('"x"' for _ in range(51)) + "]", "'exclude'"),
        (f'exclude = ["{"x" * 201}"]', "'exclude[0]'"),
    ],
)
def test_parse_repo_config_rejects_an_invalid_file_naming_the_key_or_problem(
    text: str, named: str
) -> None:
    with pytest.raises(RepoConfigError, match="is invalid") as caught:
        parse(text)

    assert named in str(caught.value)
    assert BASE in str(caught.value)


def test_parse_repo_config_rejects_a_file_over_64_kb() -> None:
    data = b"#" * (MAX_CONFIG_BYTES + 1)

    with pytest.raises(RepoConfigError, match=f"over the limit of {MAX_CONFIG_BYTES} bytes"):
        parse_repo_config(data, base_sha=BASE)


def test_parse_repo_config_accepts_a_file_of_exactly_64_kb() -> None:
    data = b"#" * MAX_CONFIG_BYTES

    assert parse_repo_config(data, base_sha=BASE).exclude == ()


def test_parse_repo_config_rejects_bytes_that_arent_utf8() -> None:
    with pytest.raises(RepoConfigError, match="isn't valid UTF-8"):
        parse_repo_config(b'threshold = "\xff"', base_sha=BASE)


def test_parse_repo_config_bounds_and_escapes_an_unknown_key_it_names() -> None:
    key = "k\x1b" + "x" * 1_000

    with pytest.raises(RepoConfigError) as caught:
        parse(f'"{key}" = 1')

    message = str(caught.value)
    assert "\x1b" not in message
    assert len(message) < 400


def test_with_repo_threshold_applies_the_files_threshold_over_the_default() -> None:
    config = RepoConfig(source="s", threshold=Severity.MEDIUM)

    assert with_repo_threshold(Settings(), config).threshold is Severity.MEDIUM


def test_with_repo_threshold_keeps_a_threshold_from_a_flag_or_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = RepoConfig(source="s", threshold=Severity.MEDIUM)
    monkeypatch.setenv("REVIEW_BOT_THRESHOLD", "high")

    from_variable = with_repo_threshold(load_settings({}), config)
    from_flag = with_repo_threshold(load_settings({"threshold": "critical"}), config)

    assert (from_variable.threshold, from_flag.threshold) == (Severity.HIGH, Severity.CRITICAL)


def test_with_repo_threshold_without_a_file_threshold_keeps_the_settings() -> None:
    settings = Settings(provider="mock")

    assert with_repo_threshold(settings, DEFAULT_REPO_CONFIG) is settings


@pytest.mark.parametrize(
    "document", ["x = " + "[" * 1000 + "]" * 1000, "x = " + "{a = " * 1000 + "}" * 1000]
)
def test_parse_repo_config_rejects_nesting_too_deep_to_parse(document: str) -> None:
    with pytest.raises(RepoConfigError, match="nested too deeply"):
        parse(document)


@pytest.mark.parametrize("pattern", ["/src/a.json", "generated/", "src//a.json", ""])
def test_parse_repo_config_rejects_a_pattern_with_an_empty_segment(pattern: str) -> None:
    with pytest.raises(RepoConfigError, match="relative paths") as caught:
        parse(f'exclude = ["{pattern}"]')

    assert f"'{pattern}'" in str(caught.value)
    assert "'exclude[0]'" in str(caught.value)


MONEY_ENTRY = '[[domain_rules]]\nrule = "decimal-money"\npaths = ["billing/**"]\n'


def test_parse_repo_config_reads_domain_rules_with_their_default_severity() -> None:
    config = parse(
        MONEY_ENTRY
        + '[[domain_rules]]\nrule = "decimal-money"\npaths = ["a", "b/*.py"]\nseverity = "medium"\n'
    )

    assert config.domain_rules == (
        DomainRule(RuleId.DECIMAL_MONEY, ("billing/**",), Severity.HIGH),
        DomainRule(RuleId.DECIMAL_MONEY, ("a", "b/*.py"), Severity.MEDIUM),
    )


def test_parse_repo_config_accepts_20_domain_rules_of_20_patterns() -> None:
    patterns = ", ".join(f'"p{n}"' for n in range(20))
    entry = f'[[domain_rules]]\nrule = "decimal-money"\npaths = [{patterns}]\n'

    config = parse(entry * 20)

    assert [len(rule.paths) for rule in config.domain_rules] == [20] * 20


def test_parse_repo_config_without_domain_rules_enables_none() -> None:
    assert parse('threshold = "low"').domain_rules == ()


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ('[[domain_rules]]\nrule = "float-money"\npaths = ["a"]', "'domain_rules[0].rule'"),
        ('[[domain_rules]]\nrule = "swallowed-exception"\npaths = ["a"]', "'domain_rules[0].rule'"),
        ('[[domain_rules]]\nrule = 1\npaths = ["a"]', "'domain_rules[0].rule'"),
        ('[[domain_rules]]\npaths = ["a"]', "'domain_rules[0].rule'"),
        ('[[domain_rules]]\nrule = "decimal-money"', "'domain_rules[0].paths'"),
        ('[[domain_rules]]\nrule = "decimal-money"\npaths = []', "'domain_rules[0].paths'"),
        (
            '[[domain_rules]]\nrule = "decimal-money"\npaths = [' + ", ".join(['"a"'] * 21) + "]",
            "'domain_rules[0].paths'",
        ),
        (
            MONEY_ENTRY + '[[domain_rules]]\nrule = "decimal-money"\npaths = ["/a"]',
            "'domain_rules[1].paths[0]'",
        ),
        (
            f'[[domain_rules]]\nrule = "decimal-money"\npaths = ["{"x" * 201}"]',
            "'domain_rules[0].paths[0]'",
        ),
        (MONEY_ENTRY + 'severity = "urgent"', "'domain_rules[0].severity'"),
        (MONEY_ENTRY * 21, "'domain_rules'"),
        ('domain_rules = "decimal-money"', "'domain_rules'"),
    ],
)
def test_parse_repo_config_rejects_an_invalid_domain_rule_naming_its_entry_and_key(
    text: str, named: str
) -> None:
    with pytest.raises(RepoConfigError, match="is invalid") as caught:
        parse(text)

    assert named in str(caught.value)


def test_parse_repo_config_names_the_domain_rules_when_a_rule_is_unknown() -> None:
    with pytest.raises(RepoConfigError, match="the domain rules are: decimal-money"):
        parse('[[domain_rules]]\nrule = "float-money"\npaths = ["a"]')


def test_parse_repo_config_names_an_unknown_key_in_a_domain_rule_and_the_keys_it_may_set() -> None:
    with pytest.raises(RepoConfigError) as caught:
        parse(MONEY_ENTRY + "colour = 1")

    assert "unknown key 'domain_rules[0].colour'" in str(caught.value)
    assert "may set only rule, paths and severity" in str(caught.value)


def test_parse_config_file_names_the_problem_but_not_where_the_file_is() -> None:
    with pytest.raises(ConfigFileError) as caught:
        parse_config_file(b"colour = 1", source="anywhere")

    assert str(caught.value).startswith("unknown key 'colour'")
    assert "anywhere" not in str(caught.value)


def test_parse_config_file_records_its_source() -> None:
    assert parse_config_file(b"", source="case repo").source == "case repo"


MONEY_RULE = '[[domain_rules]]\nrule = "decimal-money"\npaths = ["billing/**"]\n'
MONEY_CLASS = GateClass("decimal-money", Severity.HIGH, 0.9, "@owner", date(2026, 12, 1))
SECURITY_CLASS = GateClass("ai:security", Severity.CRITICAL, 0.9, "@owner", date(2026, 12, 1))
GATED_POLICY = dataclasses.replace(
    BUILTIN_POLICY, version="gated", gate_classes=(MONEY_CLASS, SECURITY_CLASS)
)


def test_parse_repo_config_reads_the_gate_classes_in_order() -> None:
    config = parse('gate = ["ai:security", "decimal-money"]\n')

    assert config.gate == ("ai:security", "decimal-money")
    assert DEFAULT_REPO_CONFIG.gate == ()


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ('gate = ["swallowed-exception"]', "its rule reports only medium"),
        ('gate = ["ai:style"]', "it isn't a finding class"),
        ("gate = [1]", "'gate[0]'"),
        ('gate = ["ai:logic", "ai:logic"]', "lists 'ai:logic' more than once"),
    ],
)
def test_parse_repo_config_refuses_a_gate_entry_that_isnt_a_class(text: str, named: str) -> None:
    with pytest.raises(RepoConfigError) as raised:
        parse(text)

    assert named in str(raised.value)


def test_gate_classes_are_the_policys_approved_classes_in_the_configs_order() -> None:
    config = parse(f'gate = ["ai:security", "decimal-money"]\n{MONEY_RULE}')

    classes = gate_classes(config, GATED_POLICY, base_sha=BASE)

    assert classes == (SECURITY_CLASS, MONEY_CLASS)
    assert gate_classes(parse(""), BUILTIN_POLICY, base_sha=BASE) == ()


@pytest.mark.parametrize(
    ("text", "policy", "reason"),
    [
        ('gate = ["ai:security"]', BUILTIN_POLICY, "the policy doesn't approve it"),
        ('gate = ["possible-secret"]', GATED_POLICY, "the policy doesn't approve it"),
        ('gate = ["decimal-money"]', GATED_POLICY, "no [[domain_rules]] entry enables that rule"),
        (
            f'gate = ["decimal-money"]\n{MONEY_RULE}severity = "medium"\n',
            GATED_POLICY,
            "every [[domain_rules]] entry for that rule is below the class's high",
        ),
    ],
)
def test_gate_classes_refuse_a_class_that_could_never_block(
    text: str, policy: Policy, reason: str
) -> None:
    with pytest.raises(RepoConfigError) as raised:
        gate_classes(parse(text), policy, base_sha=BASE)

    message = str(raised.value)
    assert f"at the base commit {BASE} is invalid: gate names the class " in message
    assert reason in message
    assert f"the policy `{policy.version}`" in message
