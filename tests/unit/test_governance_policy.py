import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from review_bot.context.ledger import ToolName
from review_bot.errors import ConfigError
from review_bot.findings import Severity
from review_bot.governance.gate import GateClass
from review_bot.governance.policy import (
    BUILTIN_POLICY,
    BUILTIN_POLICY_TEXT,
    MAX_POLICY_BYTES,
    Policy,
    PolicyError,
    check_context_allowed,
    check_settings,
    parse_policy,
)
from review_bot.governance.redaction import RedactionKind
from review_bot.settings import Settings

SOURCE = "p.toml"


def parse(text: str) -> Policy:
    return parse_policy(text.encode("utf-8"), source=SOURCE)


def test_parse_policy_reads_every_key_and_hashes_the_files_bytes() -> None:
    data = (
        b'version = "org-1"\n[tools]\nallowed = ["read", "glob"]\n[cost]\nmax_cost_usd = "0.60"\n'
    )

    policy = parse_policy(data, source=SOURCE)

    assert policy == Policy(
        version="org-1",
        sha256=hashlib.sha256(data).hexdigest(),
        source=SOURCE,
        tools=frozenset({ToolName.READ, ToolName.GLOB}),
        max_cost_usd=Decimal("0.60"),
    )


def test_parse_policy_with_only_a_version_allows_what_the_builtin_policy_does() -> None:
    policy = parse('version = "org-1"')

    assert (policy.tools, policy.max_cost_usd) == (BUILTIN_POLICY.tools, None)


def test_parse_policy_accepts_an_empty_tool_list() -> None:
    assert parse('version = "v"\n[tools]\nallowed = []').tools == frozenset()


def test_builtin_policy_is_its_text_parsed_with_source_builtin() -> None:
    parsed = parse_policy(BUILTIN_POLICY_TEXT, source="builtin")

    assert parsed == BUILTIN_POLICY
    assert BUILTIN_POLICY.version == "builtin-v1"
    assert BUILTIN_POLICY.tools == frozenset(ToolName)


@pytest.mark.parametrize("version", ["a", "A" * 64, "rightship-2026.11_b"])
def test_parse_policy_accepts_a_version_of_1_to_64_allowed_characters(version: str) -> None:
    assert parse(f'version = "{version}"').version == version


@pytest.mark.parametrize("amount", ["0.01", "1000", "1000.00", "7"])
def test_parse_policy_accepts_a_ceiling_from_a_cent_to_1000(amount: str) -> None:
    policy = parse(f'version = "v"\n[cost]\nmax_cost_usd = "{amount}"')

    assert policy.max_cost_usd == Decimal(amount)


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("version = ", "isn't valid TOML"),
        ('version = "v"\nfoo = 1', "unknown key 'foo'"),
        ('version = "v"\n[tools]\nallowed = []\nmore = 1', "unknown key 'tools.more'"),
        ('version = "v"\n[cost]\nmax_usd = "1"', "unknown key 'cost.max_usd'"),
        ("[tools]\nallowed = []", "'version' is missing"),
        ('version = ""', "'version'"),
        ('version = "has space"', "'version'"),
        (f'version = "{"a" * 65}"', "'version'"),
        ("version = 1", "'version'"),
        ('version = "v"\n[tools]\nallowed = ["bash"]', "'bash' isn't a tool"),
        ('version = "v"\n[tools]\nallowed = [1]', "'tools.allowed[0]'"),
        ('version = "v"\n[tools]\nallowed = "read"', "'tools.allowed'"),
        ('version = "v"\n[tools]\nallowed = ["read", "read"]', "lists 'read' more than once"),
        ('version = "v"\ntools = "x"', "'tools': it must be a table, such as [tools]"),
        ('version = "v"\ncost = 1', "'cost': it must be a table, such as [cost]"),
        ('version = "v"\n[cost]\nmax_cost_usd = 0.6', "must be a decimal string"),
        ('version = "v"\n[cost]\nmax_cost_usd = "-1"', "must be a decimal string"),
        ('version = "v"\n[cost]\nmax_cost_usd = "1e2"', "must be a decimal string"),
        ('version = "v"\n[cost]\nmax_cost_usd = "0.009"', "'0.009' is outside 0.01 to 1000"),
        ('version = "v"\n[cost]\nmax_cost_usd = "1000.01"', "is outside 0.01 to 1000"),
        (
            'version = "v"\n[redaction]\nkinds = ["name"]',
            "'name' isn't a redaction kind; the kinds are email, phone and ipv4",
        ),
        ('version = "v"\n[redaction]\nkinds = [1]', "'redaction.kinds[0]'"),
        ('version = "v"\n[redaction]\nkinds = ["ipv4", "ipv4"]', "lists 'ipv4' more than once"),
        ('version = "v"\n[redaction]\nmask = []', "unknown key 'redaction.mask'"),
    ],
)
def test_parse_policy_refuses_an_invalid_file_naming_it_and_the_problem(
    text: str, named: str
) -> None:
    with pytest.raises(PolicyError) as raised:
        parse(text)

    message = str(raised.value)
    assert f"The policy file {SOURCE} is invalid: " in message
    assert named in message


def test_parse_policy_refuses_a_file_over_64_kb() -> None:
    data = b'version = "v"\n' + b"#" * MAX_POLICY_BYTES

    with pytest.raises(PolicyError, match="over the limit of 65536 bytes"):
        parse_policy(data, source=SOURCE)


def test_parse_policy_shows_tomls_message_without_escaping_it_twice() -> None:
    with pytest.raises(PolicyError) as raised:
        parse('version = "\x1b"')

    message = str(raised.value)
    assert "isn't valid TOML: " in message
    assert "\x1b" not in message
    assert "\\\\" not in message


def test_parse_policy_shows_a_value_with_a_backslash_as_it_is() -> None:
    with pytest.raises(PolicyError) as raised:
        parse('version = "v"\n[tools]\nallowed = ["a\\\\b"]')

    assert "'a\\b' isn't a tool" in str(raised.value)


def test_parse_policy_refuses_bytes_that_arent_utf8() -> None:
    with pytest.raises(PolicyError, match="isn't valid UTF-8"):
        parse_policy(b'version = "\xff"', source=SOURCE)


def test_parse_policy_shows_at_most_100_characters_of_a_key_escaped() -> None:
    toml_key = "k\\u0007" + "x" * 200  # a BEL, escaped as TOML writes it

    with pytest.raises(PolicyError) as raised:
        parse(f'version = "v"\n"{toml_key}" = 1')

    shown = str(raised.value).split("unknown key '")[1].split("'")[0]
    assert shown == "k\\x07" + "x" * 98


def test_parse_policy_shows_a_long_value_cut_to_100_characters() -> None:
    value = "9" * 300

    with pytest.raises(PolicyError) as raised:
        parse(f'version = "v"\n[cost]\nmax_cost_usd = "{value}"')

    assert f"'{'9' * 100}' is outside" in str(raised.value)


def test_policy_error_is_a_config_error() -> None:
    assert issubclass(PolicyError, ConfigError)


def ceiling(amount: str | None, *, tools: frozenset[ToolName] = frozenset(ToolName)) -> Policy:
    return Policy(
        version="org-1",
        sha256="0" * 64,
        source=SOURCE,
        tools=tools,
        max_cost_usd=None if amount is None else Decimal(amount),
    )


def test_check_settings_with_no_cap_uses_the_ceiling() -> None:
    checked = check_settings(Settings(), ceiling("0.60"))

    assert checked.max_cost_usd == Decimal("0.60")


@pytest.mark.parametrize("cap", ["0.30", "0.60"])
def test_check_settings_keeps_a_cap_at_or_below_the_ceiling(cap: str) -> None:
    checked = check_settings(Settings(max_cost_usd=Decimal(cap)), ceiling("0.60"))

    assert checked.max_cost_usd == Decimal(cap)


def test_check_settings_refuses_a_cap_above_the_ceiling_naming_both_and_the_policy() -> None:
    with pytest.raises(ConfigError) as raised:
        check_settings(Settings(max_cost_usd=Decimal("1.00")), ceiling("0.60"))

    message = str(raised.value)
    assert "--max-cost-usd" in message
    assert "1.00" in message
    assert "0.60" in message
    assert "org-1" in message


def test_check_settings_without_a_ceiling_changes_nothing() -> None:
    settings = Settings(max_cost_usd=Decimal(5))

    assert check_settings(settings, BUILTIN_POLICY) == settings
    assert check_settings(Settings(), BUILTIN_POLICY).max_cost_usd is None


@pytest.mark.parametrize(
    "settings",
    [Settings(context="repo"), Settings(context_repo=Path("repo"))],
    ids=["context", "context_repo"],
)
def test_check_settings_refuses_context_mode_when_the_policy_allows_no_tool(
    settings: Settings,
) -> None:
    with pytest.raises(ConfigError, match=r"allows no tools, so context mode can't run"):
        check_settings(settings, ceiling(None, tools=frozenset()))


def test_check_settings_lets_diff_mode_run_when_the_policy_allows_no_tool() -> None:
    settings = Settings()

    assert check_settings(settings, ceiling(None, tools=frozenset())) == settings


def test_check_context_allowed_names_the_policy_and_says_to_use_diff_mode() -> None:
    with pytest.raises(ConfigError) as raised:
        check_context_allowed(ceiling(None, tools=frozenset()))

    assert str(raised.value) == (
        "The policy `org-1` allows no tools, so context mode can't run; use diff mode."
    )


def test_check_context_allowed_passes_with_one_tool() -> None:
    check_context_allowed(ceiling(None, tools=frozenset({ToolName.READ})))


def test_parse_policy_reads_the_redaction_kinds() -> None:
    policy = parse('version = "v"\n[redaction]\nkinds = ["email", "ipv4"]')

    assert policy.redaction == frozenset({RedactionKind.EMAIL, RedactionKind.IPV4})


def test_without_a_redaction_section_and_in_the_builtin_policy_nothing_is_masked() -> None:
    assert parse('version = "v"').redaction == frozenset()
    assert parse('version = "v"\n[redaction]').redaction == frozenset()
    assert BUILTIN_POLICY.redaction == frozenset()


GATE_CLASS = 'version = "v"\n[[gate.classes]]\napproved_by = "@owner"\napproved_on = 2026-12-01\n'


def test_parse_policy_reads_gate_classes_with_their_defaults() -> None:
    policy = parse(
        f'{GATE_CLASS}rule = "ai:security"\n'
        '[[gate.classes]]\nrule = "decimal-money"\nmin_severity = "high"\nmin_confidence = 1\n'
        'approved_by = "@owner"\napproved_on = 2026-12-02\n'
    )

    assert policy.gate_classes == (
        GateClass("ai:security", Severity.CRITICAL, 0.9, "@owner", date(2026, 12, 1)),
        GateClass("decimal-money", Severity.HIGH, 1.0, "@owner", date(2026, 12, 2)),
    )
    assert BUILTIN_POLICY.gate_classes == ()


@pytest.mark.parametrize(
    ("rule", "severity"), [("swallowed-exception", "medium"), ("open-without-encoding", "low")]
)
def test_parse_policy_refuses_a_class_whose_rule_can_never_block(rule: str, severity: str) -> None:
    with pytest.raises(PolicyError) as raised:
        parse(f'{GATE_CLASS}rule = "{rule}"\n')

    assert f"the class '{rule}' can't gate: its rule reports only {severity}" in str(raised.value)


@pytest.mark.parametrize(
    ("extra", "named"),
    [
        ('rule = "ai:style"', "'ai:style' can't gate: it isn't a finding class"),
        ('rule = "ai:security"\nmin_severity = "medium"', "it must be high or critical"),
        ('rule = "ai:security"\nmin_confidence = 0.4', "it must be a number from 0.5 to 1.0"),
        ('rule = "ai:security"\nmin_confidence = true', "it must be a number from 0.5 to 1.0"),
        ('rule = "ai:security"\nother = 1', "unknown key 'gate.classes[0].other'"),
    ],
)
def test_parse_policy_refuses_an_invalid_gate_class(extra: str, named: str) -> None:
    with pytest.raises(PolicyError) as raised:
        parse(f"{GATE_CLASS}{extra}\n")

    assert named in str(raised.value)


@pytest.mark.parametrize(
    ("text", "named"),
    [
        (
            'version = "v"\n[[gate.classes]]\nrule = "ai:security"\napproved_on = 2026-12-01\n',
            "'gate.classes[0].approved_by' is missing",
        ),
        (
            'version = "v"\n[[gate.classes]]\nrule = "ai:security"\napproved_by = "@o"\n'
            "approved_on = 2026-12-01T10:00:00\n",
            "it must be a TOML date",
        ),
        (
            'version = "v"\n[[gate.classes]]\nrule = "ai:security"\napproved_by = ""\n'
            "approved_on = 2026-12-01\n",
            "it must name who approved the class",
        ),
        (
            f'{GATE_CLASS}rule = "ai:security"\n[[gate.classes]]\nrule = "ai:security"\n'
            'approved_by = "@o"\napproved_on = 2026-12-01\n',
            "lists 'ai:security' more than once",
        ),
    ],
)
def test_parse_policy_refuses_a_gate_class_without_its_approval(text: str, named: str) -> None:
    with pytest.raises(PolicyError) as raised:
        parse(text)

    assert named in str(raised.value)
