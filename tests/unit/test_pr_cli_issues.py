"""Linked issues on ``python -m review_bot.pr run`` and ``backfill`` (P3-S9)."""

import json
from pathlib import Path
from typing import Any

import pytest
from git_samples import commit, init_repo

from review_bot.pr.cli import main
from review_bot.pr.fake_github import FakeGitHubClient
from review_bot.providers.mock import MockProvider

CLOSING_BODY = "Fixes #12"


@pytest.fixture(autouse=True)
def outside_ci_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CI", "REVIEW_BOT_CONTEXT", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def shas(tmp_path: Path) -> tuple[str, str]:
    repo = init_repo(tmp_path / "repo")
    return commit(repo, {"app.py": "value = 1\n"}), commit(repo, {"app.py": "value = 2\n"})


@pytest.fixture
def github() -> FakeGitHubClient:
    client = FakeGitHubClient()
    client.add_issue(number=12, title="Export totals", body="Include the discount.")
    return client


def run_pr(
    tmp_path: Path, shas: tuple[str, str], *flags: str, github: FakeGitHubClient | None = None
) -> tuple[int, MockProvider]:
    base, head = shas
    full_name = {"full_name": "owner/repo"}
    event = {
        "action": "opened",
        "repository": full_name,
        "pull_request": {
            "number": 7,
            "body": CLOSING_BODY,
            "draft": False,
            "updated_at": "2026-11-10T09:30:00Z",
            "base": {"sha": base, "repo": full_name},
            "head": {"sha": head, "repo": full_name},
        },
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    argv = ["run", "--event", str(path), "--repo", str(tmp_path / "repo")]
    provider = MockProvider(findings=[])
    out = ["--out", str(tmp_path / "out"), "--provider", "mock"]
    return main([*argv, *out, *flags], provider=provider, github=github), provider


def shadow_json(out_dir: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((out_dir / "shadow.json").read_text(encoding="ascii"))
    return document


def test_run_in_context_mode_reads_linked_issues_with_the_github_client_in_shadow_mode(
    tmp_path: Path, shas: tuple[str, str], github: FakeGitHubClient
) -> None:
    exit_code, provider = run_pr(tmp_path, shas, "--context", "repo", github=github)

    assert exit_code == 0
    assert "Include the discount." in provider.requests[0].user_prompt
    assert shadow_json(tmp_path / "out")["issues"] == [{"number": 12, "status": "included"}]
    assert [call.name for call in github.calls] == ["read_issue"]


def test_run_in_context_mode_with_no_token_reviews_without_the_issues(
    tmp_path: Path, shas: tuple[str, str]
) -> None:
    exit_code, provider = run_pr(tmp_path, shas, "--context", "repo")

    assert exit_code == 0
    assert "linked issue" not in provider.requests[0].user_prompt
    assert shadow_json(tmp_path / "out")["issues"] == [{"number": 12, "status": "no_token"}]


def test_run_in_context_mode_with_an_unusable_token_exits_2_before_any_review(
    tmp_path: Path,
    shas: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "bad token")

    exit_code, provider = run_pr(tmp_path, shas, "--context", "repo")

    assert exit_code == 2
    assert provider.requests == []
    assert "GITHUB_TOKEN holds characters a token can't have" in capsys.readouterr().err


def test_run_in_diff_mode_reads_no_issue(
    tmp_path: Path, shas: tuple[str, str], github: FakeGitHubClient
) -> None:
    exit_code, _provider = run_pr(tmp_path, shas, github=github)

    assert exit_code == 0
    assert github.calls == []
    assert shadow_json(tmp_path / "out")["issues"] == []


def test_backfill_in_context_mode_reads_the_issues_each_entrys_body_closes(
    tmp_path: Path, shas: tuple[str, str], github: FakeGitHubClient
) -> None:
    base, head = shas
    entry = {
        "number": 7,
        "baseRefOid": base,
        "headRefOid": head,
        "mergedAt": "2026-11-09T10:00:00Z",
        "isCrossRepository": False,
    }
    pulls = tmp_path / "pulls.json"
    pulls.write_text(json.dumps([{**entry, "body": CLOSING_BODY}]), encoding="utf-8")
    argv = ["backfill", "--pulls", str(pulls), "--github-repo", "owner/repo"]
    argv += ["--repo", str(tmp_path / "repo"), "--out", str(tmp_path / "out")]
    argv += ["--max-calls", "1", "--provider", "mock", "--context", "repo"]
    argv += ["--max-usd", "1", "--max-cost-usd", "1"]

    exit_code = main(argv, provider=MockProvider(findings=[]), github=github)

    assert exit_code == 0
    assert shadow_json(tmp_path / "out" / "7")["issues"] == [{"number": 12, "status": "included"}]
