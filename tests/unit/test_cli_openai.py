"""The entry points with `--provider openai` (P4-S2): flags, the key, and what is refused."""

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
from diff_samples import FINDING_DIFF, FINDING_FIELDS
from openai_samples import API_KEY, FakeTransport, completion, transport

from review_bot import cli
from review_bot.errors import ConfigError
from review_bot.evals import cli as evals_cli
from review_bot.pr import cli as pr_cli
from review_bot.providers import openai_compat
from review_bot.providers.openai_compat import OpenAICompatibleProvider
from review_bot.settings import Settings, load_settings

OPENAI_M1 = ("--provider", "openai", "--model", "m1")
COST_POLICY = b'version = "org-1"\n[cost]\nmax_cost_usd = "0.60"\n'


@pytest.fixture(autouse=True)
def no_model_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run here gets a model from a flag or from a variable the test sets, never the caller's."""


@pytest.fixture
def diff_path(tmp_path: Path) -> Path:
    path = tmp_path / "change.diff"
    path.write_text(FINDING_DIFF, encoding="utf-8")
    return path


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    """The transport every provider ``main`` builds sends through, answering one finding."""
    scripted = transport(completion(json.dumps({"findings": [FINDING_FIELDS]})))
    monkeypatch.setattr(openai_compat, "UrllibTransport", lambda: scripted)
    return scripted


def test_main_reviews_with_openai_and_reports_its_provider_and_model(
    diff_path: Path,
    fake: FakeTransport,
    audit_log_in_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)

    exit_code = cli.main(
        [str(diff_path), "--provider", "openai", "--model", "m1", "--format", "json"]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert (report["provider"], report["model"]) == ("openai", "m1")
    (sent,) = fake.sent
    assert sent.headers["Authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in json.dumps(report)
    assert API_KEY not in audit_log_in_tmp_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "argv",
    [
        ["--provider", "openai", "--model", "m1"],
        ["--provider", "openai", "--model", "m1", "--openai-base-url", "https://x.example/v1"],
    ],
)
def test_main_without_the_key_for_a_remote_host_exits_2_naming_it_before_any_request(
    diff_path: Path,
    fake: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    exit_code = cli.main([str(diff_path), *argv])

    assert exit_code == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err
    assert fake.sent == []


def test_main_without_the_key_on_loopback_sends_no_authorization(
    diff_path: Path, fake: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    argv = [
        "--provider",
        "openai",
        "--model",
        "m1",
        "--openai-base-url",
        "http://127.0.0.1:8000/v1",
    ]

    cli.main([str(diff_path), *argv])

    (sent,) = fake.sent
    assert sent.url == "http://127.0.0.1:8000/v1/chat/completions"
    assert "Authorization" not in sent.headers


@pytest.mark.parametrize(
    "url",
    [
        "http://models.example.org/v1",
        "https://user:pass@models.example.org/v1",
        "https://models.example.org/v1?key=1",
        "https://models.example.org/v1#part",
    ],
)
def test_main_with_an_unsafe_base_url_exits_2_naming_its_source(
    diff_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str,
) -> None:
    monkeypatch.setenv("REVIEW_BOT_OPENAI_BASE_URL", url)

    exit_code = cli.main([str(diff_path), "--provider", "openai"])

    assert exit_code == 2
    assert "REVIEW_BOT_OPENAI_BASE_URL" in capsys.readouterr().err


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8000/v1", "http://localhost/v1", "http://[::1]/v1"]
)
def test_load_settings_accepts_a_loopback_http_base_url(url: str) -> None:
    assert load_settings({"openai_base_url": url}).openai_base_url == url


def test_main_with_a_cost_cap_and_openai_exits_2_before_any_request(
    diff_path: Path,
    fake: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)

    exit_code = cli.main([str(diff_path), *OPENAI_M1, "--max-cost-usd", "0.50"])

    assert exit_code == 2
    assert "--max-cost-usd isn't supported with --provider openai yet" in capsys.readouterr().err
    assert fake.sent == []


def test_main_under_a_policy_cost_ceiling_with_openai_exits_2_before_any_request(
    diff_path: Path,
    tmp_path: Path,
    fake: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    policy = tmp_path / "policy.toml"
    policy.write_bytes(COST_POLICY)

    exit_code = cli.main([str(diff_path), *OPENAI_M1, "--policy", str(policy)])

    assert exit_code == 2
    assert "--max-cost-usd isn't supported with --provider openai yet" in capsys.readouterr().err
    assert fake.sent == []


def test_check_provider_settings_lets_another_provider_keep_its_cost_cap() -> None:
    settings = Settings(provider="claude", max_cost_usd=Decimal("0.50"))

    cli.check_provider_settings(settings, openai_api_key=None)


def test_check_provider_settings_refuses_openai_without_a_key_for_a_remote_host() -> None:
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        cli.check_provider_settings(Settings(provider="openai", model="m1"), openai_api_key=None)


def test_build_provider_with_openai_selected_returns_the_openai_provider() -> None:
    provider = cli.build_provider(Settings(provider="openai"), openai_api_key=API_KEY)

    assert isinstance(provider, OpenAICompatibleProvider)


def test_review_bot_main_removes_the_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)

    cli.main(["--no-such-flag"])

    assert "OPENAI_API_KEY" not in os.environ


def test_pr_main_removes_the_key_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)

    pr_cli.main(["--no-such-flag"])

    assert "OPENAI_API_KEY" not in os.environ


def test_evals_main_removes_the_key_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)

    evals_cli.main(["--no-such-flag"])

    assert "OPENAI_API_KEY" not in os.environ


def test_pr_shadow_with_openai_and_a_cost_cap_exits_2(
    diff_path: Path,
    tmp_path: Path,
    fake: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    argv = ["shadow", str(diff_path), "--out", str(tmp_path / "out"), *OPENAI_M1]

    exit_code = pr_cli.main([*argv, "--max-cost-usd", "0.50"])

    assert exit_code == 2
    assert "--provider openai" in capsys.readouterr().err
    assert fake.sent == []


def test_pr_shadow_reviews_with_openai_using_the_key_main_took(
    diff_path: Path, tmp_path: Path, fake: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    out = tmp_path / "out"

    exit_code = pr_cli.main(
        ["shadow", str(diff_path), "--out", str(out), "--provider", "openai", "--model", "m1"]
    )

    assert exit_code == 0
    assert fake.sent[0].headers["Authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in (out / "shadow.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("argv", [[], ["--max-cost-usd", "0.50"]], ids=["plain", "with_cap"])
def test_main_with_openai_and_no_model_exits_2_naming_model_before_any_request(
    diff_path: Path,
    fake: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)

    exit_code = cli.main([str(diff_path), "--provider", "openai", *argv])

    assert exit_code == 2
    assert "--provider openai needs --model" in capsys.readouterr().err
    assert fake.sent == []


def test_main_with_openai_and_the_model_variable_reviews(
    diff_path: Path, fake: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    monkeypatch.setenv("REVIEW_BOT_MODEL", "m2")

    exit_code = cli.main([str(diff_path), "--provider", "openai"])

    assert exit_code == 1
    assert fake.sent[0].body["model"] == "m2"


def test_main_with_openai_and_claudes_model_name_given_sends_it(
    diff_path: Path, fake: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "Given" is what counts, not the value: a server may serve a model under that name.
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    default_model = Settings().model

    cli.main([str(diff_path), "--provider", "openai", "--model", default_model])

    assert fake.sent[0].body["model"] == default_model


@pytest.mark.parametrize("subcommand", ["shadow", "backfill"])
def test_pr_with_openai_and_no_model_exits_2_naming_model(
    diff_path: Path,
    tmp_path: Path,
    fake: FakeTransport,
    capsys: pytest.CaptureFixture[str],
    subcommand: str,
) -> None:
    inputs = (
        [str(diff_path)]
        if subcommand == "shadow"
        else [
            *("--pulls", str(tmp_path / "pulls.json"), "--github-repo", "owner/name"),
            *("--repo", str(tmp_path), "--max-calls", "1"),
        ]
    )

    exit_code = pr_cli.main(
        [subcommand, *inputs, "--out", str(tmp_path / "out"), "--provider", "openai"]
    )

    assert exit_code == 2
    assert "--provider openai needs --model" in capsys.readouterr().err
    assert fake.sent == []


def test_check_provider_settings_refuses_openai_without_a_model() -> None:
    with pytest.raises(ConfigError, match="--model"):
        cli.check_provider_settings(Settings(provider="openai"), openai_api_key=API_KEY)


def test_check_provider_settings_keeps_a_given_model_through_a_policy_ceiling() -> None:
    settings = load_settings({"provider": "openai", "model": "m1"})
    capped = settings.model_copy(update={"max_cost_usd": Decimal("0.60")})

    with pytest.raises(ConfigError, match="--max-cost-usd"):
        cli.check_provider_settings(capped, openai_api_key=API_KEY)


def test_main_with_a_key_ending_in_a_newline_exits_2_naming_the_variable(
    diff_path: Path,
    fake: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", f"{API_KEY}\n")

    exit_code = cli.main([str(diff_path), *OPENAI_M1])

    error = capsys.readouterr().err
    assert exit_code == 2
    assert "OPENAI_API_KEY" in error
    assert API_KEY not in error
    assert fake.sent == []


@pytest.mark.parametrize("url", ["http://[::1]:x/v1", "https://x.example/v 1"])
def test_main_with_a_base_url_urllib_cant_send_exits_2(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)

    assert cli.main([str(diff_path), *OPENAI_M1, "--openai-base-url", url]) == 2
