import pytest

from review_bot.errors import RepoConfigError
from review_bot.findings import Severity
from review_bot.pr.config import (
    DEFAULT_REPO_CONFIG,
    MAX_CONFIG_BYTES,
    RepoConfig,
    parse_repo_config,
    with_repo_threshold,
)
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
