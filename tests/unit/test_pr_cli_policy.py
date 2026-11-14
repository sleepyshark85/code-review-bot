import json
from pathlib import Path
from typing import Any

import pytest
from diff_samples import section
from openai_samples import API_KEY, completion, transport

from review_bot.pr.cli import main
from review_bot.providers import openai_compat
from review_bot.providers.mock import MockProvider

DIFF = section("app.py", "+value = 1\n")
NO_TOOLS = 'version = "locked"\n[tools]\nallowed = []\n'
CEILING = 'version = "org-1"\n[cost]\nmax_cost_usd = "0.60"\n'


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def shadow(tmp_path: Path, *flags: str, provider: MockProvider) -> int:
    diff = write(tmp_path / "change.diff", DIFF)
    return main(["shadow", str(diff), "--out", str(tmp_path / "out"), *flags], provider=provider)


def shadow_json(tmp_path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((tmp_path / "out" / "shadow.json").read_text("ascii"))
    return document


def test_shadow_records_the_policy_and_its_ceiling_as_the_diff_mode_cost_cap(
    tmp_path: Path, audit_log_in_tmp_path: Path
) -> None:
    policy = write(tmp_path / "policy.toml", CEILING)

    exit_code = shadow(tmp_path, "--policy", str(policy), provider=MockProvider(findings=[]))

    document = shadow_json(tmp_path)
    assert exit_code == 0
    assert document["policy"]["version"] == "org-1"
    assert document["report"]["policy"] == document["policy"]
    assert document["caps"]["max_cost_usd"] == "0.60"


def test_shadow_ignores_the_context_variable_under_a_policy_with_no_tools(
    tmp_path: Path, audit_log_in_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEW_BOT_CONTEXT", "repo")
    policy = write(tmp_path / "policy.toml", NO_TOOLS)

    exit_code = shadow(tmp_path, "--policy", str(policy), provider=MockProvider(findings=[]))

    assert exit_code == 0
    assert shadow_json(tmp_path)["policy"]["version"] == "locked"


def test_run_with_context_under_a_policy_with_no_tools_exits_2_before_reading_the_event(
    tmp_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = write(tmp_path / "policy.toml", NO_TOOLS)
    provider = MockProvider()

    exit_code = main(
        [
            "run",
            *("--event", str(tmp_path / "absent.json"), "--repo", str(tmp_path)),
            *("--out", str(tmp_path / "out"), "--context", "repo", "--policy", str(policy)),
        ],
        provider=provider,
    )

    assert exit_code == 2
    assert provider.requests == []
    assert "The policy `locked` allows no tools, so context mode can't run; use diff mode." in (
        capsys.readouterr().err
    )
    assert not (tmp_path / "out").exists()


def test_shadow_with_a_missing_policy_file_exits_2_naming_it(
    tmp_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = MockProvider()

    exit_code = shadow(tmp_path, "--policy", str(tmp_path / "absent.toml"), provider=provider)

    assert exit_code == 2
    assert provider.requests == []
    assert "absent.toml can't be used: it doesn't exist" in capsys.readouterr().err


ROUTED = """version = "routed"
[[models]]
provider = "claude"
model = "claude-opus-5-5"
modes = ["diff", "context"]
[[models]]
provider = "openai"
model = "m1"
modes = ["diff"]
[routing]
fallback = { provider = "openai", model = "m1" }
"""


def test_shadow_in_ci_without_claudes_key_is_served_by_the_fallback_and_reports_the_route(
    tmp_path: Path, audit_log_in_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    fake = transport(completion())
    monkeypatch.setattr(openai_compat, "UrllibTransport", lambda: fake)
    diff = write(tmp_path / "change.diff", DIFF)
    policy = write(tmp_path / "policy.toml", ROUTED)

    exit_code = main(["shadow", str(diff), "--out", str(tmp_path / "out"), "--policy", str(policy)])

    assert exit_code == 0
    assert [sent.body["model"] for sent in fake.sent] == ["m1"]
    assert shadow_json(tmp_path)["report"]["route"] == [
        {"route": "claude:claude-opus-5-5", "skipped": "no_credential"},
        {"route": "openai:m1", "category": None, "cost_usd": None},
    ]


def test_shadow_with_a_model_the_policy_doesnt_list_exits_2_naming_the_entries(
    tmp_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = write(tmp_path / "policy.toml", ROUTED)
    provider = MockProvider()

    exit_code = shadow(
        tmp_path, "--policy", str(policy), "--provider", "claude", "--model", "x", provider=provider
    )

    assert exit_code == 2
    assert "It allows: claude:claude-opus-5-5 (diff, context), openai:m1 (diff)." in (
        capsys.readouterr().err
    )
    assert provider.requests == []
