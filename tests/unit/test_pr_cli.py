import io
import json
import runpy
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from audit_records import read_records
from diff_samples import section
from git_samples import commit, init_repo
from shadow_samples import K1, K2, entry, report, write_report

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.pr.cli import main
from review_bot.providers.mock import MockProvider

# Assembled, so no key is written literally.
AWS_KEY = "AK" + "IA" + "ABCDEFGH" + "23456789"
RULE_DIFF = section("app.py", "+try:\n+    run()\n+except Exception:\n+    pass\n")
AI_FINDING = Finding(
    file="app.py", line=2, severity="medium", category="logic", rationale="Why.", confidence=0.9
)


@pytest.fixture
def diff_path(tmp_path: Path) -> Path:
    path = tmp_path / "change.diff"
    path.write_text(RULE_DIFF, encoding="utf-8")
    return path


def run(diff: Path, out: Path, *flags: str, provider: MockProvider | None = None) -> int:
    chosen = MockProvider(findings=[]) if provider is None else provider
    return main(["shadow", str(diff), "--out", str(out), *flags], provider=chosen)


def shadow_json(out: Path) -> dict[str, Any]:
    text = (out / "shadow.json").read_text(encoding="ascii")
    assert text.endswith("\n")
    assert text.count("\n") == 1
    document: dict[str, Any] = json.loads(text)
    return document


def test_shadow_writes_both_files_and_exits_0_with_findings(
    diff_path: Path, tmp_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"

    exit_code = run(diff_path, out, provider=MockProvider(findings=[AI_FINDING]))

    document = shadow_json(out)
    assert exit_code == 0
    assert document["outcome"] == "reviewed"
    assert [comment["line"] for comment in document["comments"]] == [2, 3]
    assert (out / "shadow.md").read_text(encoding="utf-8").startswith(document["summary_body"])
    assert {record["run_id"] for record in read_records(audit_log_in_tmp_path)} == {
        document["run_id"]
    }
    assert capsys.readouterr().err.endswith(
        f"outcome=reviewed comments=2 withheld=0 run_id={document['run_id']}\n"
    )


def test_shadow_writes_the_audit_log_in_out_when_no_variable_names_one(
    diff_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REVIEW_BOT_AUDIT_LOG")
    out = tmp_path / "out"

    run(diff_path, out)

    records = read_records(out / "audit.jsonl")
    assert records[-1]["data"] == {"exit_code": 0, "error": None}
    assert records[-1]["run_id"] == shadow_json(out)["run_id"]


def test_shadow_of_a_missing_diff_exits_3_and_writes_no_report(tmp_path: Path) -> None:
    out = tmp_path / "out"

    assert run(tmp_path / "missing.diff", out) == 3
    assert not (out / "shadow.json").exists()


def test_shadow_with_out_naming_a_file_exits_2_before_reading_the_diff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "taken"
    out.write_text("", encoding="utf-8")
    provider = MockProvider()

    exit_code = run(tmp_path / "missing.diff", out, provider=provider)

    assert exit_code == 2
    assert "--out" in capsys.readouterr().err
    assert provider.requests == []


def test_shadow_that_cannot_write_its_report_exits_2_naming_out(
    diff_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    (out / "shadow.json").mkdir(parents=True)

    exit_code = run(diff_path, out)

    assert exit_code == 2
    assert "Can't write the shadow report in --out" in capsys.readouterr().err


def test_shadow_with_a_provider_failure_writes_a_failed_report_and_exits_8(
    diff_path: Path,
    tmp_path: Path,
    audit_log_in_tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "out"
    provider = MockProvider(error=ProviderError(ErrorCategory.AUTH, "Log in."))

    exit_code = run(diff_path, out, provider=provider)

    document = shadow_json(out)
    assert exit_code == 8
    assert (document["outcome"], document["reason"]) == ("failed", "auth")
    assert read_records(audit_log_in_tmp_path)[-1]["data"] == {"exit_code": 8, "error": "auth"}
    written, error = capsys.readouterr().err.splitlines()[-2:]
    assert written.startswith("review-bot pr: wrote shadow.json and shadow.md in ")
    assert error == "review-bot: error: auth: Log in."


def test_shadow_of_a_diff_over_the_limit_is_skipped_with_no_call(
    diff_path: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    provider = MockProvider()

    exit_code = run(diff_path, out, "--max-diff-bytes", "10", provider=provider)

    assert exit_code == 0
    assert (shadow_json(out)["outcome"], shadow_json(out)["reason"]) == ("skipped", "too_large")
    assert provider.requests == []


def test_shadow_that_skips_a_diff_shows_the_errors_advice_before_the_written_line(
    diff_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(diff_path, tmp_path / "out", "--max-diff-bytes", "10")

    advice, written = capsys.readouterr().err.splitlines()[-2:]
    assert advice.startswith("review-bot pr: Diff ")
    assert advice.endswith("Split the diff or raise --max-diff-bytes.")
    assert written.startswith("review-bot pr: wrote shadow.json and shadow.md in ")


def test_shadow_of_a_diff_that_is_not_utf8_is_skipped_unreadable(tmp_path: Path) -> None:
    diff = tmp_path / "latin1.diff"
    diff.write_bytes(RULE_DIFF.encode("utf-8") + "+café\n".encode("latin-1"))
    out = tmp_path / "out"

    assert run(diff, out) == 0
    assert (shadow_json(out)["outcome"], shadow_json(out)["reason"]) == ("skipped", "unreadable")


def test_shadow_of_a_diff_the_system_cannot_read_exits_4_with_no_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "a-directory.diff"
    directory.mkdir()
    out = tmp_path / "out"

    exit_code = run(directory, out)

    assert exit_code == 4
    assert "Can't read diff" in capsys.readouterr().err
    assert not (out / "shadow.json").exists()


def test_shadow_of_a_diff_holding_a_secret_is_blocked_with_no_call(tmp_path: Path) -> None:
    diff = tmp_path / "secret.diff"
    diff.write_text(section("app.py", f'+key = "{AWS_KEY}"\n'), encoding="utf-8")
    out = tmp_path / "out"
    provider = MockProvider()

    exit_code = run(diff, out, provider=provider)

    document = shadow_json(out)
    assert exit_code == 0
    assert document["outcome"] == "blocked_secret"
    assert AWS_KEY not in (out / "shadow.json").read_text(encoding="ascii")
    assert provider.requests == []


@pytest.mark.parametrize(
    "argv", [[], ["shadow", "x.diff"], ["review", "x.diff", "--out", "o"], ["shadow", "--out", "o"]]
)
def test_main_with_bad_usage_exits_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(argv, provider=MockProvider()) == 2
    assert "Run with --help for usage." in capsys.readouterr().err


def test_main_with_an_invalid_setting_exits_2_naming_it(
    diff_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(diff_path, tmp_path / "out", "--threshold", "severe") == 2
    assert "--threshold" in capsys.readouterr().err


def test_python_m_review_bot_pr_exits_with_mains_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["review_bot.pr"])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("review_bot.pr", run_name="__main__")

    assert exit_info.value.code == 2
    assert "Run with --help for usage." in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [["run", "--out", "o", "--repo", "r"], ["run", "--event", "e", "--out", "o"]],
    ids=["no-event", "no-repo"],
)
def test_run_without_event_or_repo_exits_2(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv, provider=MockProvider()) == 2
    assert "Run with --help for usage." in capsys.readouterr().err


def pull_request_event(repo: Path, base: str, head: str) -> Path:
    path = repo.parent / "event.json"
    full_name = {"full_name": "owner/repo"}
    document = {
        "action": "opened",
        "repository": full_name,
        "pull_request": {
            "number": 7,
            "title": "Title canary-5d1a",
            "draft": False,
            "updated_at": "2026-11-08T09:30:00Z",
            "base": {"sha": base, "repo": full_name},
            "head": {"sha": head, "repo": full_name},
        },
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def run_pr(event: Path, repo: Path, out: Path, *flags: str, provider: MockProvider) -> int:
    argv = ["run", "--event", str(event), "--repo", str(repo), "--out", str(out), *flags]
    return main([*argv, "--provider", "mock"], provider=provider)


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, str, str]:
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"app.py": "def run():\n    return 1\n"})
    head = commit(repo, {"app.py": "def run():\n    return 2\n"})
    return repo, base, head


def test_run_writes_the_pull_requests_shadow_report_and_audit_log_in_out(
    clone: tuple[Path, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REVIEW_BOT_AUDIT_LOG")
    repo, base, head = clone
    out = tmp_path / "out"

    exit_code = run_pr(
        pull_request_event(repo, base, head), repo, out, provider=MockProvider(findings=[])
    )

    document = shadow_json(out)
    assert exit_code == 0
    assert (document["origin"], document["outcome"]) == ("event", "reviewed")
    assert document["pr"] == {"repo": "owner/repo", "number": 7, "base_sha": base, "head_sha": head}
    assert document["event_at"] == "2026-11-08T09:30:00+00:00"
    assert read_records(out / "audit.jsonl")[-1]["data"] == {"exit_code": 0, "error": None}
    assert not any("canary-5d1a" in path.read_text("utf-8") for path in out.iterdir())


@pytest.mark.parametrize("flags", [[], ["--mode", "shadow"]], ids=["default", "explicit"])
def test_run_in_shadow_mode_reviews_and_reports_mode_shadow(
    flags: list[str], clone: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, base, head = clone
    out = tmp_path / "out"
    event = pull_request_event(repo, base, head)

    exit_code = run_pr(event, repo, out, *flags, provider=MockProvider(findings=[]))

    document = shadow_json(out)
    assert exit_code == 0
    assert (document["mode"], document["outcome"]) == ("shadow", "reviewed")


@pytest.mark.parametrize("mode", ["comment", "off", ""])
def test_run_with_a_mode_other_than_shadow_exits_2_with_one_line_before_any_work(
    mode: str, clone: tuple[Path, str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = clone
    out = tmp_path / "out"
    provider = MockProvider()

    exit_code = run_pr(
        pull_request_event(repo, base, head), repo, out, "--mode", mode, provider=provider
    )

    err = capsys.readouterr().err
    assert exit_code == 2
    assert err.count("\n") == 1
    assert f"argument --mode: invalid choice: {mode!r}" in err
    assert "Run with --help for usage." in err
    assert not out.exists()
    assert provider.requests == []


def test_run_with_an_unusable_event_exits_19_with_one_line_and_no_report(
    clone: tuple[Path, str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, _ = clone
    event = pull_request_event(repo, base, "not-a-sha")
    out = tmp_path / "out"
    provider = MockProvider()

    exit_code = run_pr(event, repo, out, provider=provider)

    err = capsys.readouterr().err
    assert exit_code == 19
    assert err.count("\n") == 1
    assert "pull_request.head.sha isn't a 40-hex-digit SHA" in err
    assert not (out / "shadow.json").exists()
    assert provider.requests == []


def test_run_with_a_head_missing_from_the_clone_exits_19_naming_it(
    clone: tuple[Path, str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, _ = clone
    missing = "f" * 40
    provider = MockProvider()

    exit_code = run_pr(
        pull_request_event(repo, base, missing), repo, tmp_path / "out", provider=provider
    )

    assert exit_code == 19
    assert f"Commit {missing} isn't in --repo" in capsys.readouterr().err
    assert provider.requests == []


def test_run_with_claude_in_ci_without_a_key_skips_and_says_to_add_the_secret(
    clone: tuple[Path, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CI", "true")
    repo, base, head = clone
    out = tmp_path / "out"
    provider = MockProvider()
    argv = ["run", "--event", str(pull_request_event(repo, base, head)), "--repo", str(repo)]

    exit_code = main([*argv, "--out", str(out), "--provider", "claude"], provider=provider)

    assert exit_code == 0
    assert shadow_json(out)["reason"] == "no_credential"
    assert provider.requests == []
    assert "Add it as a repository secret" in capsys.readouterr().err


def test_run_with_a_provider_failure_keeps_the_key_out_of_every_file_in_out(
    clone: tuple[Path, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REVIEW_BOT_AUDIT_LOG")
    key = "sk-ant-canary-" + "0" * 20
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    repo, base, head = clone
    out = tmp_path / "out"
    error = ProviderError(ErrorCategory.AUTH, "Log in.", diagnostics=[f"key was {key}"])

    exit_code = run_pr(
        pull_request_event(repo, base, head), repo, out, provider=MockProvider(error=error)
    )

    assert exit_code == 8
    assert shadow_json(out)["outcome"] == "failed"
    assert (out / "audit.jsonl").exists()
    assert not any(key in path.read_text("utf-8") for path in out.iterdir())


def label(reports: Path, labels: Path) -> int:
    return main(["label", "--reports", str(reports), "--labels", str(labels)])


@pytest.mark.parametrize("stream_name", ["stdin", "stderr"])
def test_label_without_a_terminal_exits_2_naming_label_before_reading_anything(
    stream_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "review_bot.pr.cli.is_terminal", lambda stream: stream is not getattr(sys, stream_name)
    )
    labels = tmp_path / "labels.jsonl"
    labels.write_text("{not json\n", encoding="ascii")

    exit_code = label(tmp_path / "missing", labels)

    assert exit_code == 2
    assert capsys.readouterr().err == (
        "review-bot: error: label needs a terminal: stdin and stderr must both be attached to "
        "one. Run it from an interactive shell.\n"
    )


def test_label_in_a_terminal_appends_the_verdicts_and_ends_with_the_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("review_bot.pr.cli.is_terminal", lambda _stream: True)
    monkeypatch.setattr("sys.stdin", io.StringIO("a\n"))
    reports = tmp_path / "reports"
    write_report(reports, "one", report(entry(K1), entry(K2)))
    (reports / "broken.json").write_text("{", encoding="ascii")
    labels = tmp_path / "labels.jsonl"

    exit_code = label(reports, labels)

    assert exit_code == 0
    assert len(labels.read_text(encoding="ascii").splitlines()) == 1
    assert capsys.readouterr().err.endswith(
        "Labelling stopped.\nreview-bot pr: label: judged=1 skipped=0 remaining=1 files_skipped=1\n"
    )


def test_label_with_a_malformed_labels_file_exits_2_naming_the_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("review_bot.pr.cli.is_terminal", lambda _stream: True)
    labels = tmp_path / "labels.jsonl"
    labels.write_text("\n[]\n", encoding="ascii")

    exit_code = label(tmp_path, labels)

    assert exit_code == 2
    assert "Line 2 of the labels file" in capsys.readouterr().err


def test_label_interrupted_while_loading_reports_stops_cleanly_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("review_bot.pr.cli.is_terminal", lambda _stream: True)

    def interrupted(_directory: Path, _pattern: str) -> Iterator[Path]:
        raise KeyboardInterrupt
        yield  # pragma: no cover - makes this a generator, like rglob

    monkeypatch.setattr(Path, "rglob", interrupted)
    labels = tmp_path / "labels.jsonl"

    exit_code = label(tmp_path, labels)

    assert exit_code == 0
    assert capsys.readouterr().err == "Labelling stopped.\n"
    assert not labels.exists()
