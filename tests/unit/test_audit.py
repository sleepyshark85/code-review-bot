"""Unit tests for the audit log: records, appending, the default directory, failures, paths."""

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from audit_records import read_records
from hypothesis import given
from hypothesis import strategies as st

from review_bot.audit import DEFAULT_AUDIT_LOG_PATH, AuditLog, JsonValue, Stage, file_fields
from review_bot.diff_parser import parse_diff
from review_bot.errors import AuditError

AWS_KEY = "AK" + "IA" + "ABCDEFGH" + "23456789"
FIXED_TIME = datetime(2026, 11, 6, 12, 30, tzinfo=timezone(timedelta(hours=10)))


def fixed_clock() -> datetime:
    return FIXED_TIME


def open_log(path: Path) -> AuditLog:
    return AuditLog(path, clock=fixed_clock)


def test_record_writes_one_versioned_object_per_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = open_log(path)

    log.record(Stage.RUN, "start", {"model": "m"})
    log.record(Stage.PARSE, "parsed", {"files": 1})

    assert read_records(path) == [
        {
            "schema": "audit/v1",
            "run_id": log.run_id,
            "ts": "2026-11-06T02:30:00+00:00",
            "stage": "run",
            "event": "start",
            "data": {"model": "m"},
        },
        {
            "schema": "audit/v1",
            "run_id": log.run_id,
            "ts": "2026-11-06T02:30:00+00:00",
            "stage": "parse",
            "event": "parsed",
            "data": {"files": 1},
        },
    ]


def test_two_logs_on_one_path_append_with_different_run_ids(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    first, second = open_log(path), open_log(path)

    first.record(Stage.RUN, "end", {"exit_code": 0})
    second.record(Stage.RUN, "end", {"exit_code": 1})

    records = read_records(path)
    assert [record["run_id"] for record in records] == [first.run_id, second.run_id]
    assert first.run_id != second.run_id


def test_record_with_hostile_strings_stays_one_ascii_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    hostile = 'a\nb\r"c\x1b[31m\u2028d\u00e9'

    open_log(path).record(Stage.PARSE, "parsed", {"file": hostile})

    raw = path.read_bytes()
    assert raw.isascii()
    assert raw.count(b"\n") == 1
    assert json.loads(raw)["data"]["file"] == hostile


@pytest.mark.parametrize("data", [{"file": "x\udcff"}, {"list": [{"file": "\udcff"}]}])
def test_record_with_a_lone_surrogate_anywhere_raises_and_writes_nothing(
    tmp_path: Path, data: dict[str, JsonValue]
) -> None:
    path = tmp_path / "audit.jsonl"
    log = open_log(path)

    with pytest.raises(ValueError, match="lone surrogate"):
        log.record(Stage.PARSE, "parsed", data)

    assert path.read_bytes() == b""


def test_file_fields_with_a_valid_utf8_path_writes_it_plain_with_no_flag() -> None:
    assert file_fields('docs/caf\u00e9 "x" \\ \t.md') == {"file": 'docs/caf\u00e9 "x" \\ \t.md'}


def test_file_fields_with_a_byte_that_is_not_utf8_writes_gits_quoted_form() -> None:
    fields = file_fields(b"docs/caf\xff.md".decode("utf-8", "surrogateescape"))

    assert fields == {"file": r'"docs/caf\377.md"', "path_quoted": True}
    assert len(str(fields["file"])) == 17


def test_file_fields_quotes_every_byte_class_as_git_does() -> None:
    raw = b'\xff \x07\x08\t\n\x0b\x0c\r"\\ \x00\x01\x1b\x7f \xc3\xa9 ~'

    fields = file_fields(raw.decode("utf-8", "surrogateescape"))

    assert fields == {
        "file": r'"\377 \a\b\t\n\v\f\r\"\\ \000\001\033\177 \303\251 ~"',
        "path_quoted": True,
    }


def test_file_fields_with_a_path_that_is_already_quoted_escapes_it_again() -> None:
    # Nested: a path whose own name is a quoted, escaped path keeps every quote and backslash.
    raw = rb'"a\"b\\c\377"' + b"\xff"

    fields = file_fields(raw.decode("utf-8", "surrogateescape"))

    assert fields == {"file": r'"\"a\\\"b\\\\c\\377\"\377"', "path_quoted": True}


def test_file_fields_redacts_a_secret_before_quoting() -> None:
    path = f"{AWS_KEY}\udcff.py"

    assert file_fields(path) == {"file": r'"[REDACTED]\377.py"', "path_quoted": True}


@given(st.binary())
def test_file_fields_output_is_ascii_and_round_trips_through_the_parser(raw: bytes) -> None:
    path = raw.decode("utf-8", "surrogateescape")
    quoted = file_fields("b/" + path + "\udcff")["file"]
    assert isinstance(quoted, str)
    assert quoted.isascii()
    diff = f"diff --git a/x b/x\n--- a/x\n+++ {quoted}\n@@ -1 +1 @@\n-a\n+b\n"

    [parsed_file] = parse_diff(diff).files

    assert parsed_file.path == path + "\udcff"


def test_record_redacts_secrets_in_every_string_however_nested(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"

    open_log(path).record(
        Stage.PARSE, "parsed", {"file": f"{AWS_KEY}.py", "list": [{"file": f"x/{AWS_KEY}"}]}
    )

    assert AWS_KEY not in path.read_text(encoding="ascii")
    assert read_records(path)[0]["data"] == {
        "file": "[REDACTED].py",
        "list": [{"file": "x/[REDACTED]"}],
    }


def test_record_keeps_numbers_booleans_and_nulls(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    data = {"count": 3, "duration_s": 0.5, "flag": True, "start": None}

    open_log(path).record(Stage.AGENT, "called", data)

    assert read_records(path)[0]["data"] == data


def test_record_with_a_naive_clock_raises(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl", clock=lambda: datetime(2026, 11, 6))  # noqa: DTZ001

    with pytest.raises(ValueError, match="timezone-aware"):
        log.record(Stage.RUN, "end", {})


def test_run_id_is_a_uuid(tmp_path: Path) -> None:
    run_id = open_log(tmp_path / "audit.jsonl").run_id

    assert re.fullmatch(r"[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}", run_id)


def test_default_path_creates_its_directory_with_a_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    AuditLog(DEFAULT_AUDIT_LOG_PATH, clock=fixed_clock)

    assert (tmp_path / ".review-bot" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert (tmp_path / ".review-bot" / "audit.jsonl").exists()


def test_default_path_in_an_existing_directory_writes_no_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".review-bot").mkdir()

    AuditLog(DEFAULT_AUDIT_LOG_PATH, clock=fixed_clock)

    assert not (tmp_path / ".review-bot" / ".gitignore").exists()


def test_custom_path_creates_missing_directories_without_a_gitignore(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "deep" / "audit.jsonl"

    AuditLog(path, clock=fixed_clock)

    assert path.exists()
    assert not (path.parent / ".gitignore").exists()


def test_open_on_a_directory_raises_naming_the_path_and_the_flag(tmp_path: Path) -> None:
    with pytest.raises(AuditError) as caught:
        open_log(tmp_path)

    assert str(tmp_path) in str(caught.value)
    assert "--audit-log" in str(caught.value)


def test_open_under_a_file_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")

    with pytest.raises(AuditError, match="--audit-log"):
        open_log(blocker / "logs" / "audit.jsonl")


def test_record_when_the_file_became_unwritable_raises(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = open_log(path)
    path.unlink()
    path.mkdir()

    with pytest.raises(AuditError, match="--audit-log"):
        log.record(Stage.RUN, "end", {})


def test_timestamps_from_a_utc_clock_stay_utc(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"

    AuditLog(path, clock=lambda: datetime(2026, 1, 2, tzinfo=UTC)).record(Stage.RUN, "end", {})

    assert read_records(path)[0]["ts"] == "2026-01-02T00:00:00+00:00"


def test_open_failure_redacts_a_secret_in_the_path(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")

    with pytest.raises(AuditError) as caught:
        open_log(blocker / AWS_KEY / "audit.jsonl")

    assert AWS_KEY not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)
