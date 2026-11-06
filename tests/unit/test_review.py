"""Unit tests for `run_pipeline`: the secret check, and what each stage records."""

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from audit_records import data_of, events
from diff_samples import MINIMAL_DIFF, section

from review_bot.audit import AuditLog
from review_bot.errors import ErrorCategory, ProviderError, SecretDetectedError
from review_bot.prompt import PROMPT_VERSION
from review_bot.providers.mock import MockProvider
from review_bot.review import PipelineResult, run_pipeline
from review_bot.settings import Settings

AWS_KEY = "AK" + "IA" + "ABCDEFGH" + "23456789"


def run(diff_path: Path, log_path: Path, provider: MockProvider) -> PipelineResult:
    audit = AuditLog(log_path, clock=lambda: datetime(2026, 11, 6, tzinfo=UTC))
    return asyncio.run(run_pipeline(diff_path, Settings(provider="mock"), provider, audit))


def write_diff(tmp_path: Path, text: str, name: str = "change.diff") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def review_expecting_secrets(diff_path: Path, log_path: Path) -> tuple[str, MockProvider]:
    provider = MockProvider()
    with pytest.raises(SecretDetectedError) as caught:
        run(diff_path, log_path, provider)
    return str(caught.value), provider


def test_run_pipeline_with_a_secret_names_it_without_the_path_and_calls_no_provider(
    tmp_path: Path,
) -> None:
    diff = write_diff(tmp_path, f"not a diff\n{AWS_KEY}\n", name=f"{AWS_KEY}.diff")

    message, provider = review_expecting_secrets(diff, tmp_path / "audit.jsonl")

    assert message == (
        "secret: 1 possible secret in the diff (aws-access-key at diff line 2), so it was not "
        "sent to the model. Remove it and rotate it if it is real, then run again."
    )
    assert provider.requests == []


def test_run_pipeline_with_seven_secrets_names_the_first_five_and_counts_the_rest(
    tmp_path: Path,
) -> None:
    diff = write_diff(tmp_path, f"+{AWS_KEY}\n" * 7)

    message, _ = review_expecting_secrets(diff, tmp_path / "audit.jsonl")

    named = "; ".join(f"aws-access-key at diff line {line}" for line in range(1, 6))
    assert message == (
        f"secret: 7 possible secrets in the diff ({named}; 2 more), so it was not sent to the "
        "model. Remove them and rotate any that are real, then run again."
    )


def test_run_pipeline_with_a_secret_records_kinds_and_lines_but_not_the_value(
    tmp_path: Path,
) -> None:
    log = tmp_path / "audit.jsonl"
    diff = write_diff(tmp_path, section(f"{AWS_KEY}.py", f"+{AWS_KEY}\n"), name=f"{AWS_KEY}.diff")

    review_expecting_secrets(diff, log)

    assert events(log) == ["run.start", "static.secrets", "agent.skipped"]
    assert data_of(log, "static.secrets") == {
        "hits": [
            {"kind": "aws-access-key", "diff_line": line_number} for line_number in (1, 1, 2, 3, 5)
        ]
    }
    assert data_of(log, "agent.skipped") == {"reason": "secret_detected"}
    assert AWS_KEY not in log.read_text(encoding="ascii")


def test_run_pipeline_records_the_start_with_the_diffs_hash_and_size(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    diff = write_diff(tmp_path, MINIMAL_DIFF.replace("\n", "\r\n"))

    run(diff, log, MockProvider(findings=[]))

    diff_bytes = diff.read_bytes()
    start = data_of(log, "run.start")
    assert start["provider"] == "mock"
    assert start["model"] == Settings().model
    assert start["prompt_version"] == PROMPT_VERSION
    assert start["threshold"] == "low"
    assert start["diff_sha256"] == hashlib.sha256(diff_bytes).hexdigest()
    assert start["diff_bytes"] == len(diff_bytes)


def test_run_pipeline_records_each_stage_of_a_review_in_order(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"

    run(write_diff(tmp_path, MINIMAL_DIFF), log, MockProvider(findings=[]))

    assert events(log) == ["run.start", "parse.parsed", "agent.called"]
    called = data_of(log, "agent.called")
    assert called["input_tokens"] == called["output_tokens"] == 0
    assert called["duration_s"] >= 0


def test_run_pipeline_lists_a_skipped_file_and_flagged_hunks_without_header_text(
    tmp_path: Path,
) -> None:
    log = tmp_path / "audit.jsonl"
    text = (
        section("README.md")
        + "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        + "@@ -1,1 +1,1 @@\n+fine\n-gone\n"
        + "@@ -5,1 +7,9 @@ HEADER-TEXT\n+short\n"
        + "@@ -9 +nine @@ HEADER-TEXT\n+unreadable\n"
    )

    run(write_diff(tmp_path, text), log, MockProvider(findings=[]))

    assert data_of(log, "parse.parsed") == {
        "files": 2,
        "hunks": 4,
        "skipped_files": [{"file": "README.md", "reason": "docs"}],
        "skipped_files_total": 1,
        "flagged_hunks": [
            {"file": "app.py", "index": 1, "new_start": 7},
            {"file": "app.py", "index": 2, "new_start": None},
        ],
        "flagged_hunks_total": 2,
    }
    assert "HEADER-TEXT" not in log.read_text(encoding="ascii")


def test_run_pipeline_lists_the_first_50_skipped_files_and_flagged_hunks_with_totals(
    tmp_path: Path,
) -> None:
    log = tmp_path / "audit.jsonl"
    docs = "".join(section(f"docs/{index:02}.md") for index in range(51))
    hunks = "".join("@@ -1,9 +1,9 @@\n+x\n" for _ in range(51))
    text = docs + f"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n{hunks}"

    run(write_diff(tmp_path, text), log, MockProvider(findings=[]))

    parsed = data_of(log, "parse.parsed")
    assert parsed["skipped_files"] == [
        {"file": f"docs/{index:02}.md", "reason": "docs"} for index in range(50)
    ]
    assert parsed["flagged_hunks"] == [
        {"file": "app.py", "index": index, "new_start": 1} for index in range(50)
    ]
    assert (parsed["skipped_files_total"], parsed["flagged_hunks_total"]) == (51, 51)


def test_run_pipeline_with_every_file_skipped_records_why_and_calls_no_provider(
    tmp_path: Path,
) -> None:
    log = tmp_path / "audit.jsonl"
    provider = MockProvider()

    result = run(write_diff(tmp_path, section("uv.lock")), log, provider)

    assert not result.model_called
    assert provider.requests == []
    assert events(log) == ["run.start", "parse.parsed", "agent.skipped"]
    assert data_of(log, "agent.skipped") == {"reason": "nothing_to_review"}


def test_run_pipeline_with_a_provider_error_records_its_category(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    provider = MockProvider(error=ProviderError(ErrorCategory.RATE_LIMIT, "SENTINEL"))

    with pytest.raises(ProviderError):
        run(write_diff(tmp_path, MINIMAL_DIFF), log, provider)

    assert events(log) == ["run.start", "parse.parsed", "agent.failed"]
    assert data_of(log, "agent.failed") == {"category": "rate_limit"}
    assert "SENTINEL" not in log.read_text(encoding="ascii")


def test_run_pipeline_writes_a_path_that_is_not_utf8_in_gits_quoted_form(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    text = (
        'diff --git "a/notes\\377.md" "b/notes\\377.md"\n'
        '--- "a/notes\\377.md"\n+++ "b/notes\\377.md"\n@@ -0,0 +1,9 @@\n+x\n'
    )

    run(write_diff(tmp_path, text), log, MockProvider())

    parsed = data_of(log, "parse.parsed")
    assert log.read_bytes().isascii()
    assert parsed["skipped_files"] == [
        {"file": '"notes\\377.md"', "reason": "docs", "path_quoted": True}
    ]
    assert parsed["flagged_hunks"] == [
        {"file": '"notes\\377.md"', "index": 0, "new_start": 1, "path_quoted": True}
    ]


def test_run_pipeline_names_a_deleted_file_by_its_old_path(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    text = (
        "diff --git a/docs/old.md b/docs/old.md\ndeleted file mode 100644\n"
        "--- a/docs/old.md\n+++ /dev/null\n@@ -1,9 +0,0 @@\n-x\n" + MINIMAL_DIFF
    )

    run(write_diff(tmp_path, text), log, MockProvider(findings=[]))

    parsed = data_of(log, "parse.parsed")
    assert parsed["skipped_files"] == [{"file": "docs/old.md", "reason": "docs"}]
    assert parsed["flagged_hunks"] == [{"file": "docs/old.md", "index": 0, "new_start": 0}]


def test_run_pipeline_names_a_section_with_no_old_or_new_path_dev_null(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    text = "--- /dev/null\n+++ /dev/null\n@@ -0,0 +1,9 @@\n+x\n"

    run(write_diff(tmp_path, text), log, MockProvider(findings=[]))

    assert data_of(log, "parse.parsed")["flagged_hunks"] == [
        {"file": "/dev/null", "index": 0, "new_start": 1}
    ]
