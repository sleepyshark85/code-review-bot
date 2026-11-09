"""``--context`` and ``REVIEW_BOT_CONTEXT`` on ``python -m review_bot.pr`` (P3-S8)."""

import json
from pathlib import Path
from typing import Any

import pytest
from audit_records import read_records
from diff_samples import section
from git_samples import commit, init_repo

from review_bot.pr.cli import main
from review_bot.providers.mock import MockProvider

CONTEXT_VARIABLE = "REVIEW_BOT_CONTEXT"
READ_APP = ("read", {"path": "app.py"})


@pytest.fixture(autouse=True)
def without_context_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONTEXT_VARIABLE, raising=False)
    monkeypatch.delenv("CI", raising=False)


@pytest.fixture
def event(tmp_path: Path) -> Path:
    """An ``opened`` event for a pull request in ``tmp_path/repo``, from base to head."""
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"app.py": "value = 1\n"})
    head = commit(repo, {"app.py": "value = 2\n"})
    full_name = {"full_name": "owner/repo"}
    document = {
        "action": "opened",
        "repository": full_name,
        "pull_request": {
            "number": 7,
            "draft": False,
            "updated_at": "2026-11-10T09:30:00Z",
            "base": {"sha": base, "repo": full_name},
            "head": {"sha": head, "repo": full_name},
        },
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def run_pr(tmp_path: Path, event: Path, *flags: str, provider: MockProvider) -> int:
    argv = ["run", "--event", str(event), "--repo", str(tmp_path / "repo")]
    return main(
        [*argv, "--out", str(tmp_path / "out"), "--provider", "mock", *flags], provider=provider
    )


def shadow_json(tmp_path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (tmp_path / "out" / "shadow.json").read_text(encoding="ascii")
    )
    return document


def test_run_with_context_repo_offers_the_tools_and_reports_a_context_review(
    tmp_path: Path, event: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REVIEW_BOT_AUDIT_LOG")
    provider = MockProvider(findings=[], tool_calls=[READ_APP])

    exit_code = run_pr(tmp_path, event, "--context", "repo", provider=provider)

    assert exit_code == 0
    assert "value = 2" in provider.tool_results[0]
    document = shadow_json(tmp_path)
    assert document["report"]["mode"] == "context"
    assert document["caps"]["max_tool_calls"] == 12
    records = read_records(tmp_path / "out" / "audit.jsonl")
    assert [record["event"] for record in records if record["stage"] == "context"] == ["tool_call"]


def test_run_with_review_bot_context_repo_behaves_as_the_flag(
    tmp_path: Path, event: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONTEXT_VARIABLE, "repo")
    provider = MockProvider(findings=[], tool_calls=[READ_APP])

    exit_code = run_pr(tmp_path, event, provider=provider)

    assert exit_code == 0
    assert provider.requests[0].tools is not None
    assert shadow_json(tmp_path)["report"]["mode"] == "context"


@pytest.mark.parametrize("value", [None, "off"], ids=["unset", "off"])
def test_run_with_context_unset_or_off_reviews_in_diff_mode(
    value: str | None, tmp_path: Path, event: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if value is not None:
        monkeypatch.setenv(CONTEXT_VARIABLE, value)
    provider = MockProvider(findings=[])

    exit_code = run_pr(tmp_path, event, provider=provider)

    assert exit_code == 0
    assert provider.requests[0].tools is None
    document = shadow_json(tmp_path)
    assert (document["report"]["mode"], document["context"]) == ("diff", None)
    assert "max_tool_calls" not in document["caps"]


def test_run_with_an_unknown_context_variable_exits_2_naming_it(
    tmp_path: Path, event: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(CONTEXT_VARIABLE, "on")
    provider = MockProvider()

    exit_code = run_pr(tmp_path, event, provider=provider)

    assert exit_code == 2
    assert provider.requests == []
    assert "REVIEW_BOT_CONTEXT" in capsys.readouterr().err


def test_run_with_an_unknown_context_flag_exits_2_naming_it(
    tmp_path: Path, event: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = MockProvider()

    exit_code = run_pr(tmp_path, event, "--context", "head", provider=provider)

    assert exit_code == 2
    assert provider.requests == []
    assert "--context" in capsys.readouterr().err


def test_run_passes_the_tool_cap_flags_to_the_review(tmp_path: Path, event: Path) -> None:
    provider = MockProvider(findings=[], tool_calls=[READ_APP, READ_APP])

    run_pr(tmp_path, event, "--context", "repo", "--max-tool-calls", "1", provider=provider)

    assert "call_limit" in provider.tool_results[1]
    assert shadow_json(tmp_path)["caps"]["max_tool_calls"] == 1


def test_shadow_of_a_local_diff_reviews_in_diff_mode_whatever_review_bot_context_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONTEXT_VARIABLE, "repo")
    diff = tmp_path / "change.diff"
    diff.write_text(section("app.py", "+value = 1\n"), encoding="utf-8")
    provider = MockProvider(findings=[])

    exit_code = main(["shadow", str(diff), "--out", str(tmp_path / "out")], provider=provider)

    assert exit_code == 0
    assert provider.requests[0].tools is None
    assert "max_tool_calls" not in shadow_json(tmp_path)["caps"]


def help_text(command: str, capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit):
        main([command, "--help"])
    return " ".join(capsys.readouterr().out.split())


def test_shadow_help_doesnt_offer_context(capsys: pytest.CaptureFixture[str]) -> None:
    text = help_text("shadow", capsys)

    assert "such as --provider or --max-cost-usd, can also be set with REVIEW_BOT_" in text
    assert "--context" not in text


@pytest.mark.parametrize("command", ["run", "backfill"])
def test_run_and_backfill_help_say_context_can_be_set_from_the_environment(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    text = help_text(command, capsys)

    assert "such as --provider, --max-cost-usd or --context, can also be set with REVIEW_BOT_" in (
        text
    )
