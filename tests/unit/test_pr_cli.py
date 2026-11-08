import json
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest
from audit_records import read_records
from diff_samples import section

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
