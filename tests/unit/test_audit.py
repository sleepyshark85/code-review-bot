"""Unit tests for the audit log: records, appending, the default directory, failures, paths."""

import json
import re
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from audit_records import read_records
from hypothesis import given
from hypothesis import strategies as st

from review_bot.audit import (
    DEFAULT_AUDIT_LOG_PATH,
    AuditLog,
    JsonValue,
    Stage,
    file_fields,
    scrub_diagnostics,
)
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


# `scrub_diagnostics`. Every secret is assembled here, so no literal reaches the repository.
PEM_BEGIN = "-----BEGIN " + "RSA PRIVATE KEY-----"
PEM_END = "-----END " + "RSA PRIVATE KEY-----"
PEM_BODY = "MIIEowIBAAKCAQEA" + "q7Lm" * 15
CREDENTIAL_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
DIFF_LINE = 'message = "say \\"hi\\""'
QUOTES_THE_DIFF = "[removed: quotes the diff]"


def scrub(lines: list[str], *, diff_text: str = "", **environ: str) -> tuple[str, ...]:
    return scrub_diagnostics(lines, diff_text=diff_text, environ=environ)


def test_scrub_diagnostics_with_no_lines_keeps_none() -> None:
    assert scrub([]) == ()


def test_scrub_diagnostics_keeps_ordinary_lines_unchanged() -> None:
    assert scrub(["CLI-SENTINEL", "", "exit 1"]) == ("CLI-SENTINEL", "", "exit 1")


def test_scrub_diagnostics_keeps_only_the_last_20_lines() -> None:
    lines = [f"line {number}" for number in range(50)]

    assert scrub(lines) == tuple(lines[-20:])


def test_scrub_diagnostics_keeps_only_the_last_2000_characters() -> None:
    lines = ["first", "x" * 9_000 + "y" * 1_000, "last"]

    kept = scrub(lines)

    assert kept == ("x" * 996 + "y" * 1_000, "last")


@given(st.lists(st.text(alphabet="ab \n", max_size=300), max_size=60))
def test_scrub_diagnostics_never_keeps_more_than_its_caps(lines: list[str]) -> None:
    kept = scrub(lines)

    assert len(kept) <= 20
    assert sum(len(line) for line in kept) <= 2_000


def test_scrub_diagnostics_redacts_a_secret_spanning_the_cap_before_cutting() -> None:
    # The 20-character key starts 2,005 characters from the end, so cutting first would keep its
    # last 15, which no pattern matches.
    line = " " * 50 + AWS_KEY + "y" * 1_985

    assert scrub([line]) == (" " * 5 + "[REDACTED]" + "y" * 1_985,)


def test_scrub_diagnostics_redacts_a_private_key_block_longer_than_the_caps() -> None:
    lines = ["before", PEM_BEGIN, *[PEM_BODY] * 40, PEM_END, "after"]

    assert scrub(lines) == ("before", "[REDACTED]", "after")


def test_scrub_diagnostics_redacts_a_private_key_block_with_no_end() -> None:
    lines = [PEM_BEGIN, *[PEM_BODY] * 30]

    assert scrub(lines) == ("[REDACTED]",)


def test_scrub_diagnostics_redacts_a_tail_holding_only_a_private_keys_end_and_body() -> None:
    assert scrub([PEM_BODY, PEM_BODY, PEM_END, "after"]) == ("[REDACTED]", "after")


@pytest.mark.parametrize("name", CREDENTIAL_VARIABLES)
def test_scrub_diagnostics_redacts_a_credential_variables_value(name: str) -> None:
    kept = scrub(["auth failed for test-key-sentinel."], **{name: "test-key-sentinel"})

    assert kept == ("auth failed for [REDACTED].",)


@pytest.mark.parametrize("name", CREDENTIAL_VARIABLES)
def test_scrub_diagnostics_with_an_empty_credential_variable_changes_nothing(name: str) -> None:
    assert scrub(["unchanged"], **{name: ""}) == ("unchanged",)


def test_scrub_diagnostics_ignores_other_variables() -> None:
    assert scrub(["value sentinel-of-home"], HOME="sentinel-of-home") == ("value sentinel-of-home",)


def test_scrub_diagnostics_redacts_a_secret_redact_finds() -> None:
    assert scrub([f"key {AWS_KEY}"]) == ("key [REDACTED]",)


@pytest.mark.parametrize(
    ("diff_line", "cli_line"),
    [
        (f"+{DIFF_LINE}", f"echo: {DIFF_LINE} (no marker)"),
        (f"-{DIFF_LINE}", f"echo: {DIFF_LINE} (no marker)"),
        (f" {DIFF_LINE}", f"echo: {DIFF_LINE} (no marker)"),
        (f"+        {DIFF_LINE}", f"echo:{DIFF_LINE}"),
        (f"+    {DIFF_LINE}  ", f'{{"prompt": "{json.dumps(DIFF_LINE)[1:-1]}"}}'),
        ("+path = 'C:\\temp'", r"""{"text": "path = 'C:\\temp'\n+next"}"""),
        ("+12345678", "count 12345678"),
    ],
    ids=["added", "removed", "context", "indented", "json-quotes", "json-backslash", "8-chars"],
)
def test_scrub_diagnostics_removes_a_line_quoting_a_diff_line(
    diff_line: str, cli_line: str
) -> None:
    diff_text = f"diff --git a/a.py b/a.py\n@@ -1 +1 @@\n{diff_line}\n"

    assert scrub(["kept", cli_line], diff_text=diff_text) == ("kept", QUOTES_THE_DIFF)


def test_scrub_diagnostics_keeps_a_line_holding_a_diff_line_under_8_characters() -> None:
    diff_text = "@@ -1 +1 @@\n+1234567\n-  else:\n"

    assert scrub(["got 1234567 else:"], diff_text=diff_text) == ("got 1234567 else:",)


def test_scrub_diagnostics_escapes_a_lone_surrogate_so_the_line_can_be_recorded(
    tmp_path: Path,
) -> None:
    [kept] = scrub(["bad" + chr(0xDCFF) + "byte"])

    assert kept == r"bad\udcffbyte"
    open_log(tmp_path / "audit.jsonl").record(Stage.AGENT, "failed", {"diagnostics": [kept]})


@pytest.mark.parametrize("quote_starts", [100, 995], ids=["before-the-cut", "across-the-cut"])
def test_scrub_diagnostics_removes_a_quoting_line_before_cutting_it(quote_starts: int) -> None:
    # The kept tail is the line's last 2,000 characters, which starts at 1,000.
    quote = "total = compute(items)"
    line = "q" * quote_starts + quote + "q" * (3_000 - quote_starts - len(quote))

    kept = scrub([line], diff_text=f"+{quote}\n")

    assert kept == (QUOTES_THE_DIFF,)


def test_scrub_diagnostics_with_a_large_diff_and_long_lines_stays_fast() -> None:
    # A plain substring search per diff line took about 10 s here.
    diff_text = "".join(f"+line_{number:08d}_of_the_diff\n" for number in range(15_000))
    lines = ["z" * 20_000] * 20

    started = time.perf_counter()
    kept = scrub(lines, diff_text=diff_text)

    assert time.perf_counter() - started < 5
    assert kept == ("z" * 2_000,)


@pytest.mark.parametrize(
    ("cli_line", "expected"),
    [
        ("value_0002 = 9, then value_0001 = 1", QUOTES_THE_DIFF),
        ("value_0002 = 9, then value_0001 = 2", "value_0002 = 9, then value_0001 = 2"),
    ],
    ids=["quote-after-a-near-miss", "only-near-misses"],
)
def test_scrub_diagnostics_checks_every_place_a_diff_lines_start_occurs(
    cli_line: str, expected: str
) -> None:
    assert scrub([cli_line], diff_text="+value_0001 = 1\n") == (expected,)


@pytest.mark.parametrize("quote_starts", [65_528, 65_530, 65_535, 65_536, 65_537])
def test_scrub_diagnostics_finds_a_quote_at_any_window_near_a_chunk_boundary(
    quote_starts: int,
) -> None:
    # Windows are read in chunks of 65,536 starting positions.
    quote = "total = compute(items)"
    line = "q" * quote_starts + quote + "q" * 10

    assert scrub([line], diff_text=f"+{quote}\n") == (QUOTES_THE_DIFF,)
