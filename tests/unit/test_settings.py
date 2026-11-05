import pytest

from review_bot.errors import ConfigError
from review_bot.findings import Severity
from review_bot.settings import Settings, load_settings


def test_load_settings_with_nothing_set_uses_defaults() -> None:
    assert load_settings({}) == Settings(
        provider="claude", model="claude-opus-5-5", threshold=Severity.LOW, max_diff_bytes=400_000
    )


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
