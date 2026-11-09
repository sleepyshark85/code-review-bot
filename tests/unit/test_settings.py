from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from review_bot.errors import ConfigError
from review_bot.findings import Severity
from review_bot.settings import Settings, in_ci, live_run_refused, load_settings


def test_load_settings_with_nothing_set_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_BOT_AUDIT_LOG")

    assert load_settings({}) == Settings(
        provider="claude",
        model="claude-opus-5-5",
        threshold=Severity.LOW,
        max_diff_bytes=400_000,
        audit_log=Path(".review-bot/audit.jsonl"),
    )


def test_load_settings_reads_the_audit_log_from_the_environment_unless_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_BOT_AUDIT_LOG", "from-env.jsonl")

    assert load_settings({"audit_log": None}).audit_log == Path("from-env.jsonl")
    assert load_settings({"audit_log": "from-flag.jsonl"}).audit_log == Path("from-flag.jsonl")


def test_load_settings_with_flag_set_to_none_falls_back_to_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_BOT_THRESHOLD", "high")

    assert load_settings({"threshold": None}).threshold is Severity.HIGH


def test_load_settings_with_flag_and_environment_prefers_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_BOT_MAX_DIFF_BYTES", "5")

    assert load_settings({"max_diff_bytes": "7"}).max_diff_bytes == 7


def test_load_settings_with_valid_flag_ignores_invalid_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_BOT_THRESHOLD", "urgent")

    assert load_settings({"threshold": "high"}).threshold is Severity.HIGH


@pytest.mark.parametrize(
    ("field", "value", "flag"),
    [
        ("provider", "nope", "--provider"),
        ("model", "", "--model"),
        ("threshold", "urgent", "--threshold"),
        ("max_diff_bytes", "abc", "--max-diff-bytes"),
        ("max_diff_bytes", "0", "--max-diff-bytes"),
        ("max_diff_bytes", "-1", "--max-diff-bytes"),
        ("max_diff_bytes", "100000001", "--max-diff-bytes"),
        ("max_diff_bytes", "1" + "0" * 30, "--max-diff-bytes"),
        ("max_review_bytes", "0", "--max-review-bytes"),
        ("max_output_tokens", "abc", "--max-output-tokens"),
        ("max_tool_calls", "0", "--max-tool-calls"),
        ("max_tool_calls", "31", "--max-tool-calls"),
        ("max_tool_result_bytes", "999", "--max-tool-result-bytes"),
        ("max_tool_result_bytes", "64001", "--max-tool-result-bytes"),
        ("max_tool_total_bytes", "0", "--max-tool-total-bytes"),
        ("max_tool_total_bytes", "400001", "--max-tool-total-bytes"),
        ("context_rev", "", "--context-rev"),
        *[
            ("max_cost_usd", value, "--max-cost-usd")
            for value in ("0", "0.009", "-1", "abc", "1000.01", "nan", "NaN", "inf", "Infinity")
        ],
    ],
)
def test_load_settings_with_invalid_flag_names_the_flag(field: str, value: str, flag: str) -> None:
    with pytest.raises(ConfigError, match=flag):
        load_settings({field: value})


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("REVIEW_BOT_PROVIDER", "nope"),
        ("REVIEW_BOT_MODEL", ""),
        ("REVIEW_BOT_THRESHOLD", ""),
        ("REVIEW_BOT_MAX_DIFF_BYTES", "abc"),
        ("REVIEW_BOT_MAX_DIFF_BYTES", "100000001"),
        *[
            (variable, value)
            for variable in ("REVIEW_BOT_MAX_REVIEW_BYTES", "REVIEW_BOT_MAX_OUTPUT_TOKENS")
            for value in ("0", "-1", "abc")
        ],
    ],
)
def test_load_settings_with_invalid_environment_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ConfigError, match=variable):
        load_settings({})


@pytest.mark.parametrize(
    "model",
    ["a\x1b[31m", "a\nprovider=forged", "a\n", "a b", "a\tb", "--dangerous", "-x"],
    ids=["escape", "forged-line", "trailing-newline", "space", "tab", "double-hyphen", "hyphen"],
)
def test_load_settings_with_unsafe_model_flag_names_the_flag(model: str) -> None:
    with pytest.raises(ConfigError, match="--model"):
        load_settings({"model": model})


def test_load_settings_with_unsafe_model_variable_names_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_BOT_MODEL", "a\x1b[31m")

    with pytest.raises(ConfigError, match="REVIEW_BOT_MODEL"):
        load_settings({})


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5-5", "us.anthropic.claude:0", "models/claude@v1", "claude-opus-5-5[1m]"],
)
def test_load_settings_accepts_real_model_name_shapes(model: str) -> None:
    assert load_settings({"model": model}).model == model


@pytest.mark.parametrize("value", ["x", None])
def test_load_settings_with_unknown_flag_key_rejects_it(value: str | None) -> None:
    with pytest.raises(ConfigError, match="--max-retry"):
        load_settings({"max_retry": value})


def test_load_settings_accepts_max_diff_bytes_at_its_upper_bound() -> None:
    assert load_settings({"max_diff_bytes": "100000000"}).max_diff_bytes == 100_000_000


@pytest.mark.parametrize("value", ["inf", "nan"])
def test_load_settings_with_non_finite_deadline_names_the_flag(value: str) -> None:
    with pytest.raises(ConfigError, match="--deadline-s"):
        load_settings({"deadline_s": value})


def test_load_settings_error_lists_allowed_values() -> None:
    with pytest.raises(ConfigError, match="critical"):
        load_settings({"threshold": "urgent"})


def test_load_settings_with_no_format_set_uses_text() -> None:
    assert load_settings({"format": None}).format == "text"


def test_load_settings_accepts_the_markdown_format() -> None:
    assert load_settings({"format": "markdown"}).format == "markdown"


def test_load_settings_reads_the_format_from_the_environment_unless_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_BOT_FORMAT", "json")

    assert load_settings({"format": None}).format == "json"
    assert load_settings({"format": "text"}).format == "text"


@pytest.mark.parametrize(
    ("flags", "source"),
    [({"format": "yaml"}, "--format"), ({"format": None}, "REVIEW_BOT_FORMAT")],
)
def test_load_settings_with_an_unknown_format_names_its_source(
    flags: dict[str, str | None], source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEW_BOT_FORMAT", "yaml")

    with pytest.raises(ConfigError, match=f"{source}: .*'json'"):
        load_settings(flags)


def test_load_settings_with_no_triage_flag_leaves_triage_off() -> None:
    assert load_settings({}).triage is False


def test_load_settings_takes_triage_from_its_flag() -> None:
    assert load_settings({"triage": True}).triage is True


def test_load_settings_never_reads_triage_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_BOT_TRIAGE", "true")

    assert load_settings({}).triage is False


def test_load_settings_with_no_caps_set_leaves_both_new_caps_off() -> None:
    settings = load_settings({})

    assert (settings.max_review_bytes, settings.max_output_tokens) == (None, None)


def test_load_settings_reads_both_new_caps_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_BOT_MAX_REVIEW_BYTES", "120000")
    monkeypatch.setenv("REVIEW_BOT_MAX_OUTPUT_TOKENS", "16000")

    settings = load_settings({})

    assert (settings.max_review_bytes, settings.max_output_tokens) == (120_000, 16_000)


def test_load_settings_with_no_cost_cap_leaves_it_off() -> None:
    assert load_settings({}).max_cost_usd is None


@pytest.mark.parametrize("value", ["0.01", "0.309", "1000"])
def test_load_settings_keeps_a_cost_cap_from_a_cent_to_1000_exactly(value: str) -> None:
    assert load_settings({"max_cost_usd": value}).max_cost_usd == Decimal(value)


def test_load_settings_reads_the_cost_cap_from_the_environment_unless_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_BOT_MAX_COST_USD", "0.60")

    assert load_settings({}).max_cost_usd == Decimal("0.60")
    assert load_settings({"max_cost_usd": "0.30"}).max_cost_usd == Decimal("0.30")


def test_load_settings_with_an_invalid_cost_cap_variable_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_BOT_MAX_COST_USD", "0")

    with pytest.raises(ConfigError, match="REVIEW_BOT_MAX_COST_USD"):
        load_settings({})


def test_load_settings_with_no_context_set_reviews_in_diff_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REVIEW_BOT_CONTEXT", raising=False)

    assert load_settings({}).context == "off"


def test_load_settings_reads_the_context_from_the_environment_unless_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_BOT_CONTEXT", "repo")

    assert load_settings({}).context == "repo"
    assert load_settings({"context": "off"}).context == "off"


@pytest.mark.parametrize("value", ["on", "REPO", ""])
def test_load_settings_with_an_unknown_context_variable_names_it(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEW_BOT_CONTEXT", value)

    with pytest.raises(ConfigError, match="REVIEW_BOT_CONTEXT"):
        load_settings({})


@pytest.mark.parametrize(
    ("environ", "expected"), [({"CI": ""}, True), ({"CI": "false"}, True), ({}, False)]
)
def test_in_ci_is_true_when_ci_is_set_whatever_its_value(
    environ: dict[str, str], expected: bool
) -> None:
    assert in_ci(environ) is expected


DISPATCH: Final = {"CI": "true", "GITHUB_EVENT_NAME": "workflow_dispatch"}


def test_live_run_refused_lets_a_ci_workflow_dispatch_run_given_allow_dispatch_through() -> None:
    assert live_run_refused(DISPATCH, allow_dispatch=True) is False


@pytest.mark.parametrize(
    ("environ", "allow_dispatch"),
    [
        ({"CI": "true", "GITHUB_EVENT_NAME": "pull_request"}, True),
        ({"CI": "true", "GITHUB_EVENT_NAME": "push"}, True),
        ({"CI": "true", "GITHUB_EVENT_NAME": "schedule"}, True),
        ({"CI": "true", "GITHUB_EVENT_NAME": "WORKFLOW_DISPATCH"}, True),
        ({"CI": "true"}, True),
        (DISPATCH, False),
        ({"CI": "true"}, False),
    ],
)
def test_live_run_refused_in_ci_unless_both_workflow_dispatch_and_allow_dispatch(
    environ: dict[str, str], *, allow_dispatch: bool
) -> None:
    assert live_run_refused(environ, allow_dispatch=allow_dispatch) is True


@pytest.mark.parametrize("allow_dispatch", [True, False])
@pytest.mark.parametrize("environ", [{}, {"GITHUB_EVENT_NAME": "pull_request"}])
def test_live_run_refused_never_outside_ci_whatever_the_flag(
    environ: dict[str, str], *, allow_dispatch: bool
) -> None:
    assert live_run_refused(environ, allow_dispatch=allow_dispatch) is False
