"""P1-S4: trace every run in a JSONL audit log.

Written from the spec's criteria, Limits and design.md's `AuditLog` interface, before the
developer's code existed. The spec fixes the record envelope (`schema`, `run_id`, `ts`, `stage`,
`event`, `data`) but not the key names inside `data`, so these tests look for the values a
criterion names, wherever they sit in `data`, instead of guessing key names.

Every run here writes to a log under `tmp_path`, and `tests/conftest.py` points
`REVIEW_BOT_AUDIT_LOG` at a temporary file for the rest of the suite (spec Notes). The few tests
of the default path unset it and change into `tmp_path` first.

No secret appears in this file: each value is assembled at run time, as in P1-S3's tests.
`review_bot.audit` is imported per test, so a missing module fails its own tests only.
"""

import hashlib
import importlib
import json
import re
import shutil
import string
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from diff_support import MINIMAL_DIFF, section
from repo_paths import REPO_ROOT

from review_bot import cli
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

SCHEMA = "audit/v1"
ENVELOPE = ("schema", "run_id", "ts", "stage", "event", "data")
AUDIT_EXIT = 17
SECRET_EXIT = 16
ERROR = "review-bot: error: "
AUDIT_FLAG = "--audit-log"
AUDIT_VARIABLE = "REVIEW_BOT_AUDIT_LOG"
DEFAULT_LOG = Path(".review-bot") / "audit.jsonl"
SKIP_REASONS = frozenset({"docs", "lock", "generated", "binary"})
REDACTED = "[REDACTED]"
LISTED_AT_MOST = 50
GOLDEN = REPO_ROOT / "tests" / "fixtures" / "p1_s4"

# Spelled out from the exit-code tables, not read from the code: the codes are a public contract.
PROVIDER_EXITS = {
    ErrorCategory.UNAVAILABLE: 7,
    ErrorCategory.AUTH: 8,
    ErrorCategory.RATE_LIMIT: 9,
    ErrorCategory.REFUSAL: 10,
    ErrorCategory.TRANSIENT: 11,
    ErrorCategory.PERMANENT: 12,
    ErrorCategory.INVALID_OUTPUT: 13,
}

UPPER_DIGITS = string.ascii_uppercase + string.digits
# A decoded JSON string holds a surrogate code point only if its escape had no partner.
LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def aws_key(seed: int = 0) -> str:
    """An AWS access key ID, assembled so the source holds none."""
    return "AK" + "IA" + "".join(UPPER_DIGITS[(seed + 7 * i) % 36] for i in range(16))


def github_token(seed: int = 0) -> str:
    alnum = string.ascii_letters + string.digits
    return "gh" + "p_" + "".join(alnum[(seed + 7 * i) % len(alnum)] for i in range(36))


def octal(text: str) -> str:
    """Every byte of `text` as a git-style octal escape, for a quoted path."""
    return "".join(f"\\{byte:03o}" for byte in text.encode("utf-8"))


def quoted_section(escaped_path: str, body: str = "+x\n") -> str:
    """A file section whose paths are git-quoted, holding `escaped_path` as written by git."""
    count = body.count("\n")
    return (
        f'diff --git "a/{escaped_path}" "b/{escaped_path}"\n'
        f'--- "a/{escaped_path}"\n+++ "b/{escaped_path}"\n'
        f"@@ -0,0 +1,{count} @@\n{body}"
    )


def hunks_section(path: str, headers_and_bodies: list[tuple[str, str]]) -> str:
    """One file section holding each hunk, given its full `@@` header and body."""
    hunks = "".join(f"{header}\n{body}" for header, body in headers_and_bodies)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{hunks}"


# Reading the log.


def read_records(path: Path) -> list[dict[str, Any]]:
    """Parse the log, checking the format every criterion relies on (AC1, Limits).

    Each record is one line ending in `\\n`, pure ASCII, and a JSON object holding the envelope.
    Lines are split on `\\n` only, so a record holding U+2028 or `\\x85` raw would show up here
    as a record that `str.splitlines` readers would break.
    """
    raw = path.read_bytes()
    assert raw, f"the audit log {path} is empty"
    assert raw.endswith(b"\n"), "the last record isn't terminated"
    assert raw.isascii(), "the audit log isn't pure ASCII"
    text = raw.decode("ascii")
    lines = text[:-1].split("\n")
    assert len(text.splitlines()) == len(lines), "a record holds a raw line separator"
    records = []
    for number, line in enumerate(lines, start=1):
        record = json.loads(line)
        assert isinstance(record, dict), f"line {number} isn't a JSON object"
        missing = [name for name in ENVELOPE if name not in record]
        assert not missing, f"line {number} lacks {missing}"
        lone = [s for s in scalars(record) if isinstance(s, str) and LONE_SURROGATE.search(s)]
        assert not lone, f"line {number} holds a lone surrogate: {lone!r}"
        records.append(record)
    return records


def events(records: list[dict[str, Any]]) -> list[str]:
    return [f"{record['stage']}.{record['event']}" for record in records]


def only(records: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """The one record of `stage.event` name, returning its `data`."""
    matches = [record for record in records if f"{record['stage']}.{record['event']}" == name]
    assert len(matches) == 1, f"{name}: {len(matches)} records in {events(records)}"
    data = matches[0]["data"]
    assert isinstance(data, dict)
    return data


def scalars(value: Any) -> list[Any]:
    """Every scalar inside `value`, through dicts and lists."""
    if isinstance(value, Mapping):
        return [item for nested in value.values() for item in scalars(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in scalars(nested)]
    return [value]


def scalars_outside_lists(value: Any) -> list[Any]:
    """The scalars reachable through dicts only: totals and counts, not list entries."""
    if isinstance(value, Mapping):
        return [item for nested in value.values() for item in scalars_outside_lists(nested)]
    if isinstance(value, list):
        return []
    return [value]


def entry_lists(value: Any) -> list[list[Any]]:
    """Every list inside `value`."""
    found: list[list[Any]] = []
    if isinstance(value, Mapping):
        for nested in value.values():
            found.extend(entry_lists(nested))
    elif isinstance(value, list):
        found.append(value)
        for nested in value:
            found.extend(entry_lists(nested))
    return found


def list_holding(data: Mapping[str, Any], matches: Callable[[Any], bool]) -> list[Any]:
    """The one list in `data` with an entry that `matches`."""
    candidates = [items for items in entry_lists(data) if any(matches(item) for item in items)]
    assert len(candidates) == 1, f"{len(candidates)} lists match in {data}"
    return candidates[0]


def holds(entry: Any, *values: Any) -> bool:
    """True if `entry` holds each of `values` among its scalars, with `None` matched exactly."""
    found = scalars(entry)
    for value in values:
        if value is None:
            if not any(item is None for item in found):
                return False
        elif not any(item == value and type(item) is type(value) for item in found):
            return False
    return True


def run_end_code(records: list[dict[str, Any]]) -> Any:
    last = records[-1]
    assert (last["stage"], last["event"]) == ("run", "end"), events(records)
    return [value for value in scalars(last["data"]) if type(value) is int]


def summary_line(stderr: str) -> str:
    return stderr.splitlines()[-1]


def summary_run_id(stderr: str) -> str:
    line = summary_line(stderr)
    field = line.rsplit(" ", 1)[-1]
    assert field.startswith("run_id="), line
    return field.removeprefix("run_id=")


# Fixtures and providers.


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    """A log in a directory that doesn't exist yet, so creating parents is exercised too."""
    return tmp_path / "logs" / "audit.jsonl"


@pytest.fixture
def logged(bot: Any, log_path: Path) -> Callable[..., Any]:
    """Review with `--provider mock` and `--audit-log log_path`."""

    def review(diff: Path, *flags: str, provider: Any = None) -> Any:
        return bot.review(diff, AUDIT_FLAG, str(log_path), *flags, provider=provider)

    return review


@pytest.fixture
def in_empty_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run with no audit variable, from an empty working directory, as a user would."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.delenv(AUDIT_VARIABLE, raising=False)
    monkeypatch.chdir(work)
    return work


class RaisingProvider:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.requests: list[Any] = []

    async def review(self, request: Any) -> Any:
        self.requests.append(request)
        raise self.error


def finding(**overrides: Any) -> Finding:
    fields = {
        "file": "app.py",
        "line": 1,
        "severity": "high",
        "category": "logic",
        "rationale": "Rationale text.",
        "confidence": 0.9,
    }
    return Finding(**(fields | overrides))


def audit_error() -> Exception:
    """A genuine error from `AuditLog`, made the public way: opening it on a directory."""
    audit = importlib.import_module("review_bot.audit")
    directory = Path(__file__).parent
    try:
        audit.AuditLog(directory, clock=lambda: datetime.now(UTC))
    except Exception as exc:  # noqa: BLE001 - whatever it raises is what a failing log raises
        return exc
    raise AssertionError("AuditLog opened a directory as its log")


@pytest.fixture
def fail_record(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], list[str]]:
    """Make `AuditLog.record` fail, as a full disk would, for one `stage.event` and after it.

    Returns the events that were attempted, so a test can see the failure was reached.
    """

    def install(failing: str) -> list[str]:
        audit = importlib.import_module("review_bot.audit")
        error = audit_error()
        real_record = audit.AuditLog.record
        attempted: list[str] = []
        failed = False

        def record(self: Any, stage: Any, event: str, data: Any) -> None:
            nonlocal failed
            name = f"{getattr(stage, 'value', stage)}.{event}"
            attempted.append(name)
            if failed or name == failing:
                failed = True
                raise error
            real_record(self, stage, event, data)

        monkeypatch.setattr(audit.AuditLog, "record", record)
        return attempted

    return install


# AC1: one JSON object per line, one run_id, UTC ts, and the four events in order.


def test_p1_s4_ac1_every_line_is_an_audit_v1_record_of_one_run(logged, write_diff, log_path):
    before = datetime.now(UTC)

    run = logged(write_diff(MINIMAL_DIFF))

    after = datetime.now(UTC)
    assert run.exit_code == 1, run.stderr
    records = read_records(log_path)
    assert {record["schema"] for record in records} == {SCHEMA}
    run_ids = {record["run_id"] for record in records}
    assert len(run_ids) == 1
    (run_id,) = run_ids
    assert isinstance(run_id, str) and run_id.strip() == run_id and run_id
    for record in records:
        assert isinstance(record["stage"], str) and record["stage"]
        assert isinstance(record["event"], str) and record["event"]
        assert isinstance(record["data"], dict)
        stamp = datetime.fromisoformat(record["ts"])
        assert stamp.utcoffset() == timedelta(0), record["ts"]
        assert before - timedelta(seconds=5) <= stamp <= after + timedelta(seconds=5)


def test_p1_s4_ac1_start_parsed_called_end_appear_in_that_order(logged, write_diff, log_path):
    run = logged(write_diff(MINIMAL_DIFF))

    assert run.exit_code == 1, run.stderr
    names = events(read_records(log_path))
    required = ["run.start", "parse.parsed", "agent.called", "run.end"]
    positions = [names.index(name) for name in required]
    assert positions == sorted(positions), names
    assert names[0] == "run.start"
    assert names[-1] == "run.end"
    for name in required:
        assert names.count(name) == 1, names


def test_p1_s4_ac1_timestamps_never_go_backwards(logged, write_diff, log_path):
    logged(write_diff(MINIMAL_DIFF))

    stamps = [datetime.fromisoformat(record["ts"]) for record in read_records(log_path)]
    assert stamps == sorted(stamps)


# AC2: run.start holds provider, model, prompt version, threshold, diff SHA-256 and byte size.


def test_p1_s4_ac2_run_start_holds_the_settings_and_the_diffs_hash_and_size(
    logged, write_diff, log_path
):
    # CRLF and non-ASCII text, so the byte size and the hash differ from any count of characters.
    text = section("app.py", "+name = 'caf\u00e9'\r\n+total = 1\r\n")
    raw = text.encode("utf-8")
    assert len(raw) != len(text)

    run = logged(
        write_diff(raw),
        "--model",
        "audit-model",
        "--threshold",
        "medium",
        provider=None,
    )

    assert run.exit_code in (0, 1), run.stderr
    prompt_version = next(
        field.removeprefix("prompt=")
        for field in summary_line(run.stderr).split()
        if field.startswith("prompt=")
    )
    start = only(read_records(log_path), "run.start")
    found = scalars(start)
    assert "mock" in found
    assert "audit-model" in found
    assert prompt_version in found
    assert "medium" in found
    assert hashlib.sha256(raw).hexdigest() in [str(value).lower() for value in found]
    assert len(raw) in [value for value in found if type(value) is int]


def test_p1_s4_ac2_run_start_hash_changes_with_one_byte_of_the_diff(logged, write_diff, log_path):
    first = section("app.py", "+x = 1\n")
    second = section("app.py", "+x = 2\n")

    logged(write_diff(first, "one.diff"))
    logged(write_diff(second, "two.diff"))

    starts = [
        record["data"] for record in read_records(log_path) if events([record]) == ["run.start"]
    ]
    assert len(starts) == 2
    hashes = [hashlib.sha256(text.encode()).hexdigest() for text in (first, second)]
    for data, expected in zip(starts, hashes, strict=True):
        assert expected in [str(value).lower() for value in scalars(data)]


# AC3: two runs with one path are both in the file, appended, with different run_ids.


def test_p1_s4_ac3_second_run_appends_with_its_own_run_id(logged, write_diff, log_path):
    diff = write_diff(MINIMAL_DIFF)

    logged(diff)
    first_bytes = log_path.read_bytes()
    logged(diff)

    assert log_path.read_bytes().startswith(first_bytes)
    records = read_records(log_path)
    run_ids = [record["run_id"] for record in records]
    first_id = run_ids[0]
    split = run_ids.index(next(rid for rid in run_ids if rid != first_id))
    assert set(run_ids[:split]) == {first_id}
    assert len(set(run_ids[split:])) == 1
    assert run_ids[split] != first_id
    assert events(records).count("run.start") == 2
    assert events(records).count("run.end") == 2


def test_p1_s4_ac3_appends_to_a_log_that_already_holds_records(logged, write_diff, log_path):
    log_path.parent.mkdir(parents=True)
    log_path.write_bytes(b"")
    logged(write_diff(MINIMAL_DIFF))
    existing = log_path.read_bytes()

    for _ in range(3):
        logged(write_diff(MINIMAL_DIFF))

    assert log_path.read_bytes().startswith(existing)
    records = read_records(log_path)
    assert len({record["run_id"] for record in records}) == 4


# AC4: the default path, and the settings precedence of the flag and the variable.


def test_p1_s4_ac4_neither_flag_nor_variable_writes_under_the_working_directory(
    bot, write_diff, in_empty_directory
):
    run = bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock())

    assert run.exit_code == 1, run.stderr
    records = read_records(in_empty_directory / DEFAULT_LOG)
    assert events(records)[-1] == "run.end"


def test_p1_s4_ac4_variable_sets_the_path(bot, write_diff, in_empty_directory, monkeypatch):
    target = in_empty_directory.parent / "from-variable" / "audit.jsonl"
    monkeypatch.setenv(AUDIT_VARIABLE, str(target))

    run = bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock())

    assert run.exit_code == 1, run.stderr
    assert events(read_records(target))[-1] == "run.end"
    assert not (in_empty_directory / DEFAULT_LOG).exists()


def test_p1_s4_ac4_flag_wins_over_the_variable(bot, write_diff, in_empty_directory, monkeypatch):
    from_variable = in_empty_directory.parent / "from-variable" / "audit.jsonl"
    from_flag = in_empty_directory.parent / "from-flag" / "audit.jsonl"
    monkeypatch.setenv(AUDIT_VARIABLE, str(from_variable))

    run = bot.review(write_diff(MINIMAL_DIFF), AUDIT_FLAG, str(from_flag), provider=bot.mock())

    assert run.exit_code == 1, run.stderr
    assert events(read_records(from_flag))[-1] == "run.end"
    assert not from_variable.exists()
    assert not (in_empty_directory / DEFAULT_LOG).exists()


def test_p1_s4_ac4_an_overridden_unwritable_variable_doesnt_stop_the_run(
    bot, write_diff, in_empty_directory, monkeypatch, tmp_path
):
    """Phase 0's precedence: an overridden variable isn't validated, or used."""
    monkeypatch.setenv(AUDIT_VARIABLE, str(tmp_path))
    from_flag = tmp_path / "from-flag.jsonl"

    run = bot.review(write_diff(MINIMAL_DIFF), AUDIT_FLAG, str(from_flag), provider=bot.mock())

    assert run.exit_code == 1, run.stderr
    assert events(read_records(from_flag))[-1] == "run.end"


def test_p1_s4_ac4_relative_flag_is_under_the_working_directory(
    bot, write_diff, in_empty_directory
):
    run = bot.review(write_diff(MINIMAL_DIFF), AUDIT_FLAG, "nested/run.jsonl", provider=bot.mock())

    assert run.exit_code == 1, run.stderr
    assert events(read_records(in_empty_directory / "nested" / "run.jsonl"))[-1] == "run.end"


def test_p1_s4_ac4_child_process_default_is_redirected_by_the_suite():
    """Spec Notes: the suite's own runs, including child processes, must not write the repo."""
    completed = subprocess.run(  # noqa: S603 - fixed argument list, our own interpreter
        [
            sys.executable,
            "-c",
            f"import os; print(os.environ.get({AUDIT_VARIABLE!r}, ''))",
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )

    redirected = Path(completed.stdout.strip())
    assert redirected.is_absolute()
    assert REPO_ROOT not in redirected.parents


# AC5: parse.parsed lists skipped files and flagged hunks; nothing_to_review; hostile paths.


def test_p1_s4_ac5_parsed_lists_a_skipped_file_with_its_reason(logged, write_diff, log_path):
    text = section("README.md") + section("uv.lock") + MINIMAL_DIFF

    run = logged(write_diff(text), provider=None)

    assert run.exit_code == 1, run.stderr
    parsed = only(read_records(log_path), "parse.parsed")
    skipped = list_holding(parsed, lambda entry: holds(entry, "README.md"))
    assert any(holds(entry, "README.md", "docs") for entry in skipped)
    assert any(holds(entry, "uv.lock", "lock") for entry in skipped)
    assert not any(holds(entry, "app.py") for entry in skipped)


def test_p1_s4_ac5_parsed_lists_a_mismatched_hunk_by_file_index_and_declared_start(
    logged, write_diff, log_path
):
    text = hunks_section(
        "app.py",
        [
            ("@@ -0,0 +1,1 @@", "+a\n"),  # counts match: not listed
            ("@@ -20,1 +23,9 @@", "+b\n"),  # declares 9 new lines over a body of 1
        ],
    )

    run = logged(write_diff(text), provider=None)

    assert run.exit_code == 1, run.stderr
    parsed = only(read_records(log_path), "parse.parsed")
    hunks = list_holding(parsed, lambda entry: holds(entry, "app.py", 23))
    assert len(hunks) == 1
    assert holds(hunks[0], "app.py", 1, 23)


def test_p1_s4_ac5_hunk_with_an_unreadable_header_has_null_start_and_no_header_text(
    logged, write_diff, log_path
):
    marker = "HEADER-TEXT-SENTINEL"
    text = hunks_section("app.py", [("@@ -0,0 +1 @@", "+a\n"), (f"@@ {marker} @@", "+b\n")])

    run = logged(write_diff(text), provider=None)

    assert run.exit_code == 1, run.stderr
    assert marker not in log_path.read_text(encoding="ascii")
    assert "@@" not in log_path.read_text(encoding="ascii")
    parsed = only(read_records(log_path), "parse.parsed")
    hunks = list_holding(parsed, lambda entry: holds(entry, "app.py"))
    assert len(hunks) == 1
    assert holds(hunks[0], "app.py", 1, None)


def test_p1_s4_ac5_hunk_index_counts_within_its_own_file(logged, write_diff, log_path):
    text = hunks_section("one.py", [("@@ -0,0 +1 @@", "+a\n"), ("@@ -5,1 +7,4 @@", "+b\n")])
    text += hunks_section("two.py", [("@@ -3,1 +4,6 @@", "+c\n")])

    logged(write_diff(text), provider=None)

    parsed = only(read_records(log_path), "parse.parsed")
    hunks = list_holding(parsed, lambda entry: holds(entry, "one.py") or holds(entry, "two.py"))
    assert len(hunks) == 2
    assert holds(hunks[0], "one.py", 1, 7)
    assert holds(hunks[1], "two.py", 0, 4)


def fifty_one_of_each() -> tuple[str, list[str], list[int]]:
    """51 skipped docs files, then a code file with 51 mismatched hunks; starts avoid 0 to 51."""
    docs = [f"docs/page{index:03d}.md" for index in range(51)]
    starts = [100 + 10 * index for index in range(51)]
    text = "".join(section(path) for path in docs)
    text += hunks_section("app.py", [(f"@@ -{start},1 +{start},5 @@", "+x\n") for start in starts])
    return text, docs, starts


def test_p1_s4_ac5_51_skipped_files_and_51_flagged_hunks_list_the_first_50_plus_totals(
    logged, write_diff, log_path
):
    text, docs, starts = fifty_one_of_each()

    run = logged(write_diff(text), provider=None)

    assert run.exit_code == 1, run.stderr
    parsed = only(read_records(log_path), "parse.parsed")
    skipped = list_holding(parsed, lambda entry: holds(entry, docs[0]))
    assert len(skipped) == LISTED_AT_MOST
    for entry, path in zip(skipped, docs[:LISTED_AT_MOST], strict=True):
        assert holds(entry, path, "docs"), entry
    assert docs[-1] not in scalars(parsed)
    hunks = list_holding(parsed, lambda entry: holds(entry, "app.py", starts[0]))
    assert len(hunks) == LISTED_AT_MOST
    for index, (entry, start) in enumerate(zip(hunks, starts[:LISTED_AT_MOST], strict=True)):
        assert holds(entry, "app.py", index, start), entry
    assert starts[-1] not in scalars(parsed)
    totals = [value for value in scalars_outside_lists(parsed) if value == 51]
    assert len(totals) >= 2, parsed


def test_p1_s4_ac5_exactly_50_of_each_lists_all_of_them(logged, write_diff, log_path):
    docs = [f"docs/page{index:03d}.md" for index in range(50)]
    text = "".join(section(path) for path in docs) + MINIMAL_DIFF

    logged(write_diff(text), provider=None)

    parsed = only(read_records(log_path), "parse.parsed")
    skipped = list_holding(parsed, lambda entry: holds(entry, docs[0]))
    assert len(skipped) == 50
    assert 50 in scalars_outside_lists(parsed)


def test_p1_s4_ac5_all_files_skipped_gives_nothing_to_review_and_no_call(
    logged, write_diff, log_path
):
    provider_text = section("README.md") + section("uv.lock")
    provider = MockProvider()

    run = logged(write_diff(provider_text), provider=provider)

    assert run.exit_code == 0, run.stderr
    assert provider.requests == []
    records = read_records(log_path)
    names = events(records)
    assert "agent.called" not in names
    assert "nothing_to_review" in scalars(only(records, "agent.skipped"))
    assert names[-1] == "run.end"
    assert 0 in run_end_code(records)


# Paths in audit/v1 (design.md#audit-log): the keys `skipped_files` and `flagged_hunks`, and the
# C-quoted form of a path that isn't valid UTF-8, are pinned exactly.

SURROGATE_ESCAPE = re.compile(rb"\\ud[89a-f][0-9a-f]{2}", re.IGNORECASE)


def assert_no_surrogate_escape(log_path: Path) -> None:
    raw = log_path.read_bytes()
    assert not SURROGATE_ESCAPE.search(raw), SURROGATE_ESCAPE.findall(raw)


def skipped_files(log_path: Path) -> list[Any]:
    entries = only(read_records(log_path), "parse.parsed")["skipped_files"]
    assert isinstance(entries, list)
    return entries


def flagged_hunks(log_path: Path) -> list[Any]:
    entries = only(read_records(log_path), "parse.parsed")["flagged_hunks"]
    assert isinstance(entries, list)
    return entries


def test_p1_s4_ac5_skipped_path_decoding_byte_377_is_written_in_git_quoted_form(
    logged, write_diff, log_path
):
    text = quoted_section("docs/caf\\377.md") + MINIMAL_DIFF

    run = logged(write_diff(text), provider=None)

    assert run.exit_code == 1, f"expected exit 1 (not audit exit 17): {run.stderr}"
    assert log_path.read_bytes().isascii()
    assert_no_surrogate_escape(log_path)
    expected = json.loads(
        '{"file": "\\"docs/caf\\\\377.md\\"", "reason": "docs", "path_quoted": true}'
    )
    assert expected == {"file": '"docs/caf\\377.md"', "reason": "docs", "path_quoted": True}
    assert len(expected["file"]) == 17
    assert skipped_files(log_path) == [expected]


@pytest.mark.parametrize(
    "written",
    ["docs/caf\u00e9.md", '"docs/caf\\303\\251.md"'],
    ids=["plain", "git_quoted_octal"],
)
def test_p1_s4_ac5_valid_utf8_path_is_plain_with_no_path_quoted_key(
    logged, write_diff, log_path, written
):
    if written.startswith('"'):
        text = quoted_section(written.strip('"')) + MINIMAL_DIFF
    else:
        text = section(written) + MINIMAL_DIFF

    run = logged(write_diff(text), provider=None)

    assert run.exit_code == 1, run.stderr
    assert_no_surrogate_escape(log_path)
    assert skipped_files(log_path) == [{"file": "docs/caf\u00e9.md", "reason": "docs"}]


def test_p1_s4_ac5_mismatched_hunk_in_a_path_decoding_byte_377_is_written_in_git_quoted_form(
    logged, write_diff, log_path
):
    path = "src/caf\\377.py"
    text = (
        f'diff --git "a/{path}" "b/{path}"\n--- "a/{path}"\n+++ "b/{path}"\n@@ -1,1 +4,9 @@\n+x\n'
    )

    run = logged(write_diff(text), provider=None)

    assert run.exit_code == 1, run.stderr
    assert_no_surrogate_escape(log_path)
    assert flagged_hunks(log_path) == [
        {"file": '"src/caf\\377.py"', "index": 0, "new_start": 4, "path_quoted": True}
    ]


def test_p1_s4_ac5_only_file_skipped_with_byte_377_path_doesnt_exit_17(
    logged, write_diff, log_path
):
    """The `nothing was reviewed` path: every skipped path goes to stderr and the log."""
    run = logged(write_diff(quoted_section("docs/\\377\\376.md")), provider=None)

    assert run.exit_code == 0, run.stderr
    assert_no_surrogate_escape(log_path)
    records = read_records(log_path)
    assert "nothing_to_review" in scalars(only(records, "agent.skipped"))
    assert skipped_files(log_path) == [
        {"file": '"docs/\\377\\376.md"', "reason": "docs", "path_quoted": True}
    ]


# Each case: the path as git writes it in the diff, then its exact audit/v1 `file`. Every input
# byte is octal so the parser's own escape support isn't under test; the output uses the
# design's named escapes for \a \b \t \n \v \f \r, and octal for every other byte below 0x20,
# 0x7F, and above 0x7F, including the bytes of a valid UTF-8 character in an invalid path.
NON_UTF8_ESCAPING = {
    "named_controls": (
        "docs/\\007\\010\\011\\012\\013\\014\\015\\377.md",
        '"docs/\\a\\b\\t\\n\\v\\f\\r\\377.md"',
    ),
    "quote_backslash_control": (
        'docs/a\\"b\\\\c\\001\\037\\177\\377.md',
        '"docs/a\\"b\\\\c\\001\\037\\177\\377.md"',
    ),
    "valid_char_beside_invalid_byte": (
        "docs/caf\\303\\251\\377.md",
        '"docs/caf\\303\\251\\377.md"',
    ),
    "truncated_sequence": ("docs/caf\\303.md", '"docs/caf\\303.md"'),
    "encoded_surrogate": ("docs/\\355\\240\\200.md", '"docs/\\355\\240\\200.md"'),
    "overlong_slash": ("docs/a\\300\\257b.md", '"docs/a\\300\\257b.md"'),
}


@pytest.mark.parametrize(
    ("escaped", "expected"), NON_UTF8_ESCAPING.values(), ids=NON_UTF8_ESCAPING.keys()
)
def test_p1_s4_ac5_limits_non_utf8_path_uses_the_designs_exact_escaping(
    logged, write_diff, log_path, escaped, expected
):
    text = quoted_section(escaped) + MINIMAL_DIFF

    run = logged(write_diff(text), provider=None)

    assert run.exit_code == 1, run.stderr
    assert_no_surrogate_escape(log_path)
    assert skipped_files(log_path) == [{"file": expected, "reason": "docs", "path_quoted": True}]


# Deleted files (`+++ /dev/null`) are logged by their old path, never null.


def deleted_section(path: str, header: str, body: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\ndeleted file mode 100644\nindex 1111111..0000000\n"
        f"--- a/{path}\n+++ /dev/null\n{header}\n{body}"
    )


def test_p1_s4_ac5_skipped_deleted_file_is_logged_by_its_old_path(logged, write_diff, log_path):
    text = deleted_section("docs/old.md", "@@ -1,2 +0,0 @@", "-a\n-b\n") + MINIMAL_DIFF

    run = logged(write_diff(text), provider=None)

    assert run.exit_code == 1, run.stderr
    assert skipped_files(log_path) == [{"file": "docs/old.md", "reason": "docs"}]


def test_p1_s4_ac5_flagged_hunk_in_a_deleted_file_is_logged_by_its_old_path(
    logged, write_diff, log_path
):
    # Declares 3 old lines over a body of 1.
    text = deleted_section("src/gone.py", "@@ -1,3 +0,0 @@", "-a\n") + MINIMAL_DIFF

    run = logged(write_diff(text), provider=None)

    assert run.exit_code == 1, run.stderr
    assert flagged_hunks(log_path) == [{"file": "src/gone.py", "index": 0, "new_start": 0}]


def test_p1_s4_ac5_no_file_in_parse_parsed_is_null_for_deleted_files(logged, write_diff, log_path):
    text = (
        deleted_section("docs/old.md", "@@ -1,1 +0,0 @@", "-a\n")
        + deleted_section("src/gone.py", "@@ -1,4 +0,0 @@", "-a\n")
        + MINIMAL_DIFF
    )

    logged(write_diff(text), provider=None)

    entries = skipped_files(log_path) + flagged_hunks(log_path)
    assert len(entries) == 2
    assert all(isinstance(entry["file"], str) and entry["file"] for entry in entries), entries


def test_p1_s4_ac5_skipped_deleted_file_with_a_non_utf8_old_path_is_quoted(
    logged, write_diff, log_path
):
    path = "docs/caf\\377.md"
    text = (
        f'diff --git "a/{path}" "b/{path}"\ndeleted file mode 100644\n'
        f'--- "a/{path}"\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-a\n' + MINIMAL_DIFF
    )

    run = logged(write_diff(text), provider=None)

    assert run.exit_code == 1, run.stderr
    assert skipped_files(log_path) == [
        {"file": '"docs/caf\\377.md"', "reason": "docs", "path_quoted": True}
    ]


# Limits, hostile paths: control characters, newlines and quotes still give one record per line.

HOSTILE_PATHS = {
    "newline": "docs/a\\nb.md",
    "carriage_return": "docs/a\\rb.md",
    "escape_and_bell": "docs/\\033[31m\\007.md",
    "double_quote": 'docs/say \\"hi\\".md',
    "backslash": "docs/a\\\\b.md",
    "nul_octal": "docs/a\\000b.md",
    "line_separator_u2028": "docs/a\\342\\200\\250b.md",
    "next_line_x85": "docs/a\\302\\205b.md",
    "file_separator_x1c": "docs/a\\034b.md",
    "fake_record": 'docs/x\\n{\\"schema\\": \\"audit/v1\\"}.md',
}


@pytest.mark.parametrize("escaped", HOSTILE_PATHS.values(), ids=HOSTILE_PATHS.keys())
def test_p1_s4_ac1_limits_hostile_skipped_path_keeps_one_record_per_line(
    logged, write_diff, log_path, escaped
):
    text = quoted_section(escaped) + MINIMAL_DIFF

    run = logged(write_diff(text), provider=None)

    assert run.exit_code == 1, run.stderr
    records = read_records(log_path)
    assert {record["schema"] for record in records} == {SCHEMA}
    assert len({record["run_id"] for record in records}) == 1
    assert events(records)[0] == "run.start"
    assert events(records)[-1] == "run.end"
    parsed = only(records, "parse.parsed")
    skipped = list_holding(parsed, lambda entry: holds(entry, "docs"))
    assert len(skipped) == 1


def test_p1_s4_ac1_limits_hostile_mismatched_hunk_path_keeps_one_record_per_line(
    logged, write_diff, log_path
):
    path = 'src/a\\nb\\"c\\033.py'
    text = (
        f'diff --git "a/{path}" "b/{path}"\n--- "a/{path}"\n+++ "b/{path}"\n@@ -1,1 +8,3 @@\n+x\n'
    )

    run = logged(write_diff(text), provider=None)

    assert run.exit_code == 1, run.stderr
    records = read_records(log_path)
    hunks = list_holding(only(records, "parse.parsed"), lambda entry: holds(entry, 8))
    assert len(hunks) == 1


SKIPPED_BY_REASON = {
    "docs": "README.md",
    "lock": "uv.lock",
    "generated": "dist/app.js",
    "binary": "x.bin",
}


@pytest.mark.parametrize(("reason", "path"), SKIPPED_BY_REASON.items(), ids=SKIPPED_BY_REASON)
def test_p1_s4_ac5_each_skip_reason_is_recorded_from_the_fixed_set(
    logged, write_diff, log_path, reason, path
):
    if reason == "binary":
        text = f"diff --git a/{path} b/{path}\nBinary files a/{path} and b/{path} differ\n"
    else:
        text = section(path)

    logged(write_diff(text + MINIMAL_DIFF), provider=None)

    parsed = only(read_records(log_path), "parse.parsed")
    skipped = list_holding(parsed, lambda entry: holds(entry, path))
    assert len(skipped) == 1
    assert holds(skipped[0], path, reason), skipped
    others = SKIP_REASONS - {reason}
    assert not any(holds(skipped[0], other) for other in others), skipped


# AC6: a secret hit is recorded by kind and line, the agent is skipped, and V is never written.


def line_of(text: str, needle: str) -> int:
    return text[: text.index(needle)].count("\n") + 1


def test_p1_s4_ac6_secret_hit_records_kinds_lines_skip_and_exit_16(
    bot, logged, write_diff, log_path
):
    aws, github = aws_key(), github_token()
    text = section("app.py", f'+key = "{aws}"\n+ok = 1\n+token = "{github}"\n')
    provider = bot.mock()

    run = logged(write_diff(text), provider=provider)

    assert run.exit_code == SECRET_EXIT, run.stderr
    assert provider.requests == []
    raw = log_path.read_text(encoding="ascii")
    assert aws not in raw
    assert github not in raw
    records = read_records(log_path)
    secrets = scalars(only(records, "static.secrets"))
    assert "aws-access-key" in secrets
    assert "github-token" in secrets
    assert line_of(text, aws) in secrets
    assert line_of(text, github) in secrets
    assert "secret_detected" in scalars(only(records, "agent.skipped"))
    assert "agent.called" not in events(records)
    assert SECRET_EXIT in run_end_code(records)


def test_p1_s4_ac6_secret_in_the_plus_plus_plus_path_is_not_written(
    bot, logged, write_diff, log_path
):
    value = aws_key(seed=3)
    path = f"keys/{value}.py"
    text = f"diff --git a/app.py b/{path}\n--- a/app.py\n+++ b/{path}\n@@ -0,0 +1,1 @@\n+x\n"

    run = logged(write_diff(text), provider=bot.mock())

    assert run.exit_code == SECRET_EXIT, run.stderr
    assert value not in log_path.read_text(encoding="ascii")
    assert SECRET_EXIT in run_end_code(read_records(log_path))


def test_p1_s4_ac6_secret_in_the_diffs_own_path_is_not_written(bot, logged, write_diff, log_path):
    value = aws_key(seed=5)
    text = section("app.py", f'+key = "{aws_key(seed=9)}"\n')

    run = logged(write_diff(text, f"{value}.diff"), provider=bot.mock())

    assert run.exit_code == SECRET_EXIT, run.stderr
    log = log_path.read_text(encoding="ascii")
    assert value not in log
    assert aws_key(seed=9) not in log


def test_p1_s4_ac6_secret_in_a_clean_diffs_own_path_is_not_written(
    bot, logged, write_diff, log_path
):
    """No hit in the text, so the run goes on, but the diff file's name holds V."""
    value = aws_key(seed=6)

    run = logged(write_diff(MINIMAL_DIFF, f"{value}.diff"), provider=bot.mock())

    assert run.exit_code == 1, run.stderr
    assert value not in log_path.read_text(encoding="ascii")


# Since P1-S19, a path whose escapes decode to V exits 16 before any parse record (its AC2, AC4,
# AC7), so these inputs check that the log still holds no V. The `redact` step for a path that
# reaches `parse.parsed` is covered below with an `END` marker, which `redact` replaces but the
# scan doesn't flag (P1-S19 AC8).
OCTAL_SECRET_PATHS = {
    "skipped": lambda value: quoted_section(f"docs/{octal(value)}.md") + MINIMAL_DIFF,
    "flagged_hunk": lambda value: (
        f'diff --git "a/src/{octal(value)}.py" "b/src/{octal(value)}.py"\n'
        f'--- "a/src/{octal(value)}.py"\n+++ "b/src/{octal(value)}.py"\n@@ -1,1 +6,4 @@\n+x\n'
    ),
    # `\377` right before V puts the digit 7 next to it in the quoted form, so a word-boundary
    # pattern applied after quoting would miss it.
    "non_utf8_byte_after": lambda value: quoted_section(f"docs/{octal(value)}\\377.md"),
    "non_utf8_byte_before": lambda value: quoted_section(f"docs/\\377{octal(value)}.md"),
    "quote_and_backslash_beside": lambda value: quoted_section(
        f'docs/\\"\\\\{octal(value)}\\377.md'
    ),
    "deleted_old_path": lambda value: (
        f'diff --git "a/docs/{octal(value)}\\377.md" "b/docs/{octal(value)}\\377.md"\n'
        f'deleted file mode 100644\n--- "a/docs/{octal(value)}\\377.md"\n+++ /dev/null\n'
        "@@ -1,1 +0,0 @@\n-a\n" + MINIMAL_DIFF
    ),
}
OCTAL_SECRET_CASES = {
    f"{kind}-{shape_name}": (make, shape)
    for kind, make in (("aws", aws_key), ("github", github_token))
    for shape_name, shape in OCTAL_SECRET_PATHS.items()
}


@pytest.mark.parametrize("case", OCTAL_SECRET_CASES.values(), ids=OCTAL_SECRET_CASES.keys())
def test_p1_s4_ac6_limits_octal_quoted_secret_path_is_blocked_and_not_written(
    bot, logged, write_diff, log_path, case
):
    """The raw text holds only octal escapes, so the raw-text scan can't see V; the decoded
    path holds it, so the run stops before `parse.parsed` and V is never written."""
    make, shape = case
    value = make(seed=7)
    text = shape(value)
    assert value not in text
    provider = bot.mock(findings=[])

    run = logged(write_diff(text), provider=provider)

    assert run.exit_code == SECRET_EXIT, run.stderr
    assert provider.requests == []
    assert value not in log_path.read_text(encoding="ascii")
    records = read_records(log_path)
    assert "parse.parsed" not in events(records)
    assert SECRET_EXIT in run_end_code(records)


def end_marker() -> str:
    return "-----END RSA " + "PRIVATE" + " KEY-----"


# Each path holds an `END` marker alone: unflagged, so the run reaches `parse.parsed`, and
# `redact` replaces it from the path's start. A `\377` after it keeps the path non-UTF-8.
END_MARKER_PATHS = {
    "utf8_skipped": (
        lambda: quoted_section(f"docs/{octal(end_marker())}.md") + MINIMAL_DIFF,
        {"file": f"{REDACTED}.md", "reason": "docs"},
    ),
    "non_utf8_skipped": (
        lambda: quoted_section(f"docs/{octal(end_marker())}\\377.md") + MINIMAL_DIFF,
        {"file": f'"{REDACTED}\\377.md"', "reason": "docs", "path_quoted": True},
    ),
}


@pytest.mark.parametrize("case", END_MARKER_PATHS.values(), ids=END_MARKER_PATHS.keys())
def test_p1_s4_ac6_limits_end_marker_in_a_skipped_path_is_redacted(
    bot, logged, write_diff, log_path, case
):
    build, expected = case

    run = logged(write_diff(build()), provider=bot.mock(findings=[]))

    assert run.exit_code == 0, run.stderr
    assert "END RSA" not in log_path.read_text(encoding="ascii")
    assert_no_surrogate_escape(log_path)
    assert skipped_files(log_path) == [expected]


def test_p1_s4_ac6_limits_end_marker_in_a_flagged_hunk_path_is_redacted(
    bot, logged, write_diff, log_path
):
    path = f"src/{octal(end_marker())}\\377.py"
    text = (
        f'diff --git "a/{path}" "b/{path}"\n--- "a/{path}"\n+++ "b/{path}"\n@@ -1,1 +6,4 @@\n+x\n'
    )

    run = logged(write_diff(text), provider=bot.mock(findings=[]))

    assert run.exit_code == 0, run.stderr
    assert "END RSA" not in log_path.read_text(encoding="ascii")
    [entry] = flagged_hunks(log_path)
    assert entry["path_quoted"] is True, entry
    assert REDACTED in entry["file"], entry


# AC7: every exit from 3 to 15 ends the log with run.end and its code.


def input_failures(tmp_path: Path) -> dict[str, tuple[int, Path, tuple[str, ...]]]:
    directory = tmp_path / "a-directory.diff"
    directory.mkdir()
    not_utf8 = tmp_path / "latin1.diff"
    not_utf8.write_bytes(MINIMAL_DIFF.encode() + b"+caf\xe9\n")
    empty = tmp_path / "empty.diff"
    empty.write_bytes(b"  \n")
    large = tmp_path / "large.diff"
    large.write_bytes(MINIMAL_DIFF.encode())
    prose = tmp_path / "prose.diff"
    prose.write_bytes(b"+x\n")
    return {
        "3_missing": (3, tmp_path / "missing.diff", ()),
        "4_directory": (4, directory, ()),
        "4_not_utf8": (4, not_utf8, ()),
        "5_empty": (5, empty, ()),
        "6_too_large": (6, large, ("--max-diff-bytes", "10")),
        "15_not_a_diff": (15, prose, ()),
    }


INPUT_CASES = ["3_missing", "4_directory", "4_not_utf8", "5_empty", "6_too_large", "15_not_a_diff"]


@pytest.mark.parametrize("case", INPUT_CASES)
def test_p1_s4_ac7_input_failure_ends_with_run_end_and_its_code(
    bot, logged, tmp_path, log_path, case
):
    code, diff, flags = input_failures(tmp_path)[case]
    provider = bot.mock()

    run = logged(diff, *flags, provider=provider)

    assert run.exit_code == code, run.stderr
    assert provider.requests == []
    records = read_records(log_path)
    assert code in run_end_code(records)
    assert events(records).count("run.end") == 1


@pytest.mark.parametrize(("category", "code"), PROVIDER_EXITS.items(), ids=str)
def test_p1_s4_ac7_provider_failure_records_agent_failed_and_run_end(
    logged, write_diff, log_path, category, code
):
    message = "SENTINEL-PROVIDER-MESSAGE"
    provider = MockProvider(error=ProviderError(category, message))

    run = logged(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == code, run.stderr
    records = read_records(log_path)
    assert category.value in scalars(only(records, "agent.failed"))
    assert "agent.called" not in events(records)
    assert code in run_end_code(records)
    assert message not in log_path.read_text(encoding="ascii")


def test_p1_s4_ac7_invalid_model_output_records_its_category_and_13(
    bot, logged, write_diff, log_path
):
    raw_output = "SENTINEL-MODEL-TEXT is not json"

    run = logged(write_diff(MINIMAL_DIFF), provider=bot.mock(raw_output=raw_output))

    assert run.exit_code == 13, run.stderr
    records = read_records(log_path)
    assert 13 in run_end_code(records)
    assert "SENTINEL-MODEL-TEXT" not in log_path.read_text(encoding="ascii")


def test_p1_s4_ac7_unexpected_error_records_14_and_its_class_but_not_its_text(
    logged, write_diff, log_path
):
    run = logged(write_diff(MINIMAL_DIFF), provider=RaisingProvider(RuntimeError("SENTINEL")))

    assert run.exit_code == 14, run.stderr
    records = read_records(log_path)
    end = records[-1]
    assert 14 in run_end_code(records)
    assert "RuntimeError" in scalars(end["data"])
    assert "SENTINEL" not in log_path.read_text(encoding="ascii")


def test_p1_s4_ac7_unexpected_error_subclass_names_its_own_class(logged, write_diff, log_path):
    class SentinelBugError(LookupError):
        pass

    run = logged(write_diff(MINIMAL_DIFF), provider=RaisingProvider(SentinelBugError("SENTINEL")))

    assert run.exit_code == 14, run.stderr
    records = read_records(log_path)
    assert "SentinelBugError" in scalars(records[-1]["data"])
    assert "SENTINEL" not in log_path.read_text(encoding="ascii").replace("SentinelBugError", "")


def test_p1_s4_ac7_secret_value_in_a_provider_message_is_not_written(
    bot, logged, write_diff, log_path
):
    value = aws_key(seed=12)
    provider = bot.mock(error=ProviderError(ErrorCategory.PERMANENT, f"bad key {value}"))

    run = logged(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 12, run.stderr
    assert value not in log_path.read_text(encoding="ascii")


# Notes: a usage error and --help write no record.


def test_p1_s4_notes_usage_error_writes_no_record(bot, write_diff, log_path):
    run = bot.review(write_diff(MINIMAL_DIFF), AUDIT_FLAG, str(log_path), "--threshold", "urgent")

    assert run.exit_code == 2, run.stderr
    assert not log_path.exists() or log_path.read_bytes() == b""


def test_p1_s4_notes_help_writes_no_record(bot, log_path, capsys):
    try:
        code = cli.main(["--help", AUDIT_FLAG, str(log_path)])
    except SystemExit as exc:
        code = exc.code
    capsys.readouterr()

    assert code in (0, None)
    assert not log_path.exists() or log_path.read_bytes() == b""


# AC8: no rationale or diff text in the log, and output unchanged apart from run_id=.


def golden(name: str) -> str:
    return (GOLDEN / name).read_bytes().decode("utf-8")


SENTINEL_DIFF_TEXT = section("app.py", "+print('SENTINEL-DIFF')\n")
SENTINEL_FINDING = {
    "file": "app.py",
    "line": 1,
    "severity": "high",
    "category": "logic",
    "rationale": "SENTINEL-RATIONALE: the print is wrong.",
    "confidence": 0.9,
}


def assert_output_as_without_the_log(run: Any, case: str, run_id: str) -> None:
    assert run.stdout == golden(f"{case}.stdout.golden.txt")
    lines = golden(f"{case}.stderr.golden.txt").splitlines()
    lines[-1] = f"{lines[-1]} run_id={run_id}"
    assert run.stderr.splitlines() == lines


def test_p1_s4_ac8_findings_run_writes_no_rationale_or_diff_text(bot, logged, write_diff, log_path):
    provider = bot.mock(findings=[finding(**SENTINEL_FINDING)])

    run = logged(write_diff(SENTINEL_DIFF_TEXT), provider=provider)

    assert run.exit_code == 1, run.stderr
    log = log_path.read_text(encoding="ascii")
    assert "SENTINEL-RATIONALE" not in log
    assert "SENTINEL-DIFF" not in log
    assert "print(" not in log
    run_id = read_records(log_path)[0]["run_id"]
    assert_output_as_without_the_log(run, "findings", run_id)


def test_p1_s4_ac8_no_findings_output_is_as_without_the_log(bot, logged, write_diff, log_path):
    run = logged(write_diff(SENTINEL_DIFF_TEXT), provider=bot.mock(findings=[]))

    assert run.exit_code == 0, run.stderr
    assert "SENTINEL-DIFF" not in log_path.read_text(encoding="ascii")
    assert_output_as_without_the_log(run, "no_findings", read_records(log_path)[0]["run_id"])


def test_p1_s4_ac8_all_skipped_output_is_as_without_the_log(bot, logged, write_diff, log_path):
    run = logged(write_diff(section("README.md", "+SENTINEL-DIFF\n")), provider=bot.mock())

    assert run.exit_code == 0, run.stderr
    assert "SENTINEL-DIFF" not in log_path.read_text(encoding="ascii")
    assert_output_as_without_the_log(run, "all_skipped", read_records(log_path)[0]["run_id"])


def test_p1_s4_ac8_below_threshold_rationale_is_not_written(bot, logged, write_diff, log_path):
    low = finding(**(SENTINEL_FINDING | {"severity": "low"}))

    run = logged(
        write_diff(SENTINEL_DIFF_TEXT), "--threshold", "high", provider=bot.mock(findings=[low])
    )

    assert run.exit_code == 0, run.stderr
    log = log_path.read_text(encoding="ascii")
    assert "SENTINEL-RATIONALE" not in log
    assert "SENTINEL-DIFF" not in log


def test_p1_s4_ac8_no_added_or_context_line_of_the_sample_diff_is_written(
    logged, sample_diff, log_path, bot
):
    run = logged(sample_diff, provider=bot.mock(findings=[]))

    assert run.exit_code == 0, run.stderr
    log = log_path.read_text(encoding="ascii")
    body_lines = [
        line[1:].strip()
        for line in sample_diff.read_text(encoding="utf-8").splitlines()
        if line[:1] in {"+", " ", "-"} and not line.startswith(("+++", "---"))
    ]
    leaked = [line for line in body_lines if len(line) >= 8 and line in log]
    assert leaked == []


# AC9: an unwritable --audit-log exits 17 before the diff is read or the provider is called.


def unwritable_paths(tmp_path: Path) -> dict[str, Path]:
    directory = tmp_path / "is-a-directory"
    directory.mkdir()
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"a file, not a directory")
    return {
        "directory": directory,
        "parent_is_a_file": blocker / "audit.jsonl",
        "grandparent_is_a_file": blocker / "sub" / "audit.jsonl",
    }


@pytest.fixture(params=["directory", "parent_is_a_file", "grandparent_is_a_file"])
def unwritable(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    return unwritable_paths(tmp_path)[request.param]


def assert_audit_failure(run: Any, provider: Any, named: str) -> None:
    assert run.exit_code == AUDIT_EXIT, run.stderr
    assert run.stdout == ""
    assert provider.requests == []
    lines = run.stderr.splitlines()
    assert len(lines) == 1, run.stderr
    assert lines[0].startswith(ERROR), lines[0]
    assert named in lines[0], lines[0]


def test_p1_s4_ac9_unwritable_flag_path_exits_17_naming_path_and_flag(bot, write_diff, unwritable):
    provider = bot.mock()

    run = bot.review(write_diff(MINIMAL_DIFF), AUDIT_FLAG, str(unwritable), provider=provider)

    assert_audit_failure(run, provider, str(unwritable))
    assert AUDIT_FLAG in run.stderr


@pytest.mark.parametrize("diff_case", ["missing", "directory", "too_large", "secret", "not_utf8"])
def test_p1_s4_ac9_unwritable_log_is_checked_before_the_diff_is_read(
    bot, tmp_path, unwritable, diff_case
):
    diffs = {
        "missing": tmp_path / "missing.diff",
        "directory": tmp_path / "dir.diff",
        "too_large": tmp_path / "large.diff",
        "secret": tmp_path / "secret.diff",
        "not_utf8": tmp_path / "latin1.diff",
    }
    diffs["directory"].mkdir()
    diffs["too_large"].write_bytes(MINIMAL_DIFF.encode() * 100)
    diffs["secret"].write_bytes(section("app.py", f'+k = "{aws_key(seed=13)}"\n').encode())
    diffs["not_utf8"].write_bytes(b"\xff\xfe")
    provider = bot.mock()

    run = bot.review(
        diffs[diff_case],
        AUDIT_FLAG,
        str(unwritable),
        "--max-diff-bytes",
        "100",
        provider=provider,
    )

    assert_audit_failure(run, provider, str(unwritable))


def test_p1_s4_ac9_unwritable_path_holding_a_newline_still_gives_one_error_line(
    bot, write_diff, tmp_path
):
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"")
    hostile = f"{blocker}/a\nreview-bot: error: forged\rline/audit.jsonl"
    provider = bot.mock()

    run = bot.review(write_diff(MINIMAL_DIFF), AUDIT_FLAG, hostile, provider=provider)

    assert run.exit_code == AUDIT_EXIT, run.stderr
    assert run.stdout == ""
    assert provider.requests == []
    assert len(run.stderr.splitlines()) == 1, run.stderr
    assert AUDIT_FLAG in run.stderr


def test_p1_s4_ac9_unwritable_variable_path_exits_17_naming_the_path(
    bot, write_diff, unwritable, monkeypatch
):
    monkeypatch.setenv(AUDIT_VARIABLE, str(unwritable))
    provider = bot.mock()

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert_audit_failure(run, provider, str(unwritable))


def test_p1_s4_ac9_default_path_blocked_by_a_file_exits_17(bot, write_diff, in_empty_directory):
    (in_empty_directory / ".review-bot").write_bytes(b"not a directory")
    provider = bot.mock()

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert_audit_failure(run, provider, "audit.jsonl")


def test_p1_s4_ac9_unwritable_log_with_a_secret_in_its_path_doesnt_echo_it(
    bot, write_diff, tmp_path
):
    value = aws_key(seed=14)
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"")
    provider = bot.mock()

    run = bot.review(
        write_diff(MINIMAL_DIFF), AUDIT_FLAG, str(blocker / value / "a.jsonl"), provider=provider
    )

    assert run.exit_code == AUDIT_EXIT, run.stderr
    assert value not in run.stderr


# AC10 and AC11: a log failing mid-run.


@pytest.mark.parametrize("failing", ["run.start", "parse.parsed", "agent.called"])
def test_p1_s4_ac10_log_failing_before_stdout_exits_17_with_empty_stdout(
    bot, logged, write_diff, fail_record, failing
):
    attempted = fail_record(failing)

    run = logged(write_diff(MINIMAL_DIFF), provider=bot.mock())

    assert failing in attempted
    assert run.exit_code == AUDIT_EXIT, run.stderr
    assert run.stdout == ""
    assert run.stderr.splitlines()[-1].startswith(ERROR), run.stderr


def test_p1_s4_ac10_log_failing_before_the_call_doesnt_call_the_provider(
    bot, logged, write_diff, fail_record
):
    fail_record("parse.parsed")
    provider = bot.mock()

    run = logged(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == AUDIT_EXIT, run.stderr
    assert provider.requests == []


@pytest.mark.parametrize(("category", "code"), PROVIDER_EXITS.items(), ids=str)
def test_p1_s4_ac10_failed_review_whose_run_end_fails_keeps_its_own_code(
    logged, write_diff, fail_record, category, code
):
    attempted = fail_record("run.end")

    run = logged(
        write_diff(MINIMAL_DIFF), provider=MockProvider(error=ProviderError(category, "x"))
    )

    assert "run.end" in attempted
    assert run.exit_code == code, run.stderr
    assert run.stdout == ""


def test_p1_s4_ac10_unexpected_error_whose_run_end_fails_keeps_14(logged, write_diff, fail_record):
    fail_record("run.end")

    run = logged(write_diff(MINIMAL_DIFF), provider=RaisingProvider(RuntimeError("SENTINEL")))

    assert run.exit_code == 14, run.stderr
    assert "SENTINEL" not in run.stderr


@pytest.mark.parametrize("case", ["3_missing", "6_too_large", "15_not_a_diff"])
def test_p1_s4_ac10_input_failure_whose_run_end_fails_keeps_its_own_code(
    bot, logged, tmp_path, fail_record, case
):
    code, diff, flags = input_failures(tmp_path)[case]
    fail_record("run.end")

    run = logged(diff, *flags, provider=bot.mock())

    assert run.exit_code == code, run.stderr


def test_p1_s4_ac10_secret_hit_whose_run_end_fails_keeps_16(bot, logged, write_diff, fail_record):
    fail_record("run.end")
    text = section("app.py", f'+k = "{aws_key(seed=15)}"\n')

    run = logged(write_diff(text), provider=bot.mock())

    assert run.exit_code == SECRET_EXIT, run.stderr


@pytest.mark.parametrize(
    ("findings", "case"), [([SENTINEL_FINDING], "findings"), ([], "no_findings")]
)
def test_p1_s4_ac11_run_end_failing_after_stdout_exits_17_with_stdout_intact(
    logged, write_diff, fail_record, findings, case
):
    attempted = fail_record("run.end")
    provider = MockProvider(findings=[finding(**item) for item in findings])

    run = logged(write_diff(SENTINEL_DIFF_TEXT), provider=provider)

    assert "run.end" in attempted
    assert run.exit_code == AUDIT_EXIT, run.stderr
    assert run.stdout == golden(f"{case}.stdout.golden.txt")
    assert any(line.startswith(ERROR) for line in run.stderr.splitlines()), run.stderr


def test_p1_s4_ac11_all_skipped_run_end_failing_exits_17_with_stdout_intact(
    bot, logged, write_diff, fail_record
):
    fail_record("run.end")

    run = logged(write_diff(section("README.md")), provider=bot.mock())

    assert run.exit_code == AUDIT_EXIT, run.stderr
    assert run.stdout == golden("all_skipped.stdout.golden.txt")


# AC12: the summary's run_id is the log's.


@pytest.mark.parametrize(
    "text", [MINIMAL_DIFF, section("README.md")], ids=["reviewed", "all_skipped"]
)
def test_p1_s4_ac12_summary_ends_with_the_logs_run_id(bot, logged, write_diff, log_path, text):
    run = logged(write_diff(text), provider=bot.mock())

    assert run.exit_code in (0, 1), run.stderr
    records = read_records(log_path)
    assert summary_run_id(run.stderr) == records[0]["run_id"]


def test_p1_s4_ac12_each_runs_summary_names_its_own_run_id(bot, logged, write_diff, log_path):
    diff = write_diff(MINIMAL_DIFF)

    ids = [summary_run_id(logged(diff, provider=bot.mock()).stderr) for _ in range(3)]

    assert len(set(ids)) == 3
    logged_ids = list(dict.fromkeys(record["run_id"] for record in read_records(log_path)))
    assert logged_ids == ids


# AC13: the default directory ignores itself; an existing one or a custom path gets no .gitignore.


def test_p1_s4_ac13_default_path_creates_the_directory_with_a_gitignore_of_star(
    bot, write_diff, in_empty_directory
):
    run = bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock())

    assert run.exit_code == 1, run.stderr
    ignore = in_empty_directory / ".review-bot" / ".gitignore"
    assert "*" in ignore.read_text(encoding="utf-8").splitlines()


@pytest.mark.skipif(shutil.which("git") is None, reason="git isn't installed")
def test_p1_s4_ac13_git_ignores_the_default_directory(bot, write_diff, in_empty_directory):
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q"], cwd=in_empty_directory, check=True, timeout=60)  # noqa: S603

    run = bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock())

    assert run.exit_code == 1, run.stderr
    status = subprocess.run(  # noqa: S603 - fixed arguments
        [git, "status", "--porcelain", "--untracked-files=all"],
        cwd=in_empty_directory,
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    assert status.stdout == ""


def test_p1_s4_ac13_existing_default_directory_gets_no_gitignore(
    bot, write_diff, in_empty_directory
):
    (in_empty_directory / ".review-bot").mkdir()

    run = bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock())

    assert run.exit_code == 1, run.stderr
    assert not (in_empty_directory / ".review-bot" / ".gitignore").exists()
    assert (in_empty_directory / DEFAULT_LOG).exists()


def test_p1_s4_ac13_existing_gitignore_in_the_default_directory_is_left_alone(
    bot, write_diff, in_empty_directory
):
    directory = in_empty_directory / ".review-bot"
    directory.mkdir()
    (directory / ".gitignore").write_bytes(b"# mine\n")

    bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock())

    assert (directory / ".gitignore").read_bytes() == b"# mine\n"


def test_p1_s4_ac13_custom_flag_path_gets_no_gitignore(bot, write_diff, in_empty_directory):
    run = bot.review(
        write_diff(MINIMAL_DIFF), AUDIT_FLAG, "custom/audit.jsonl", provider=bot.mock()
    )

    assert run.exit_code == 1, run.stderr
    assert (in_empty_directory / "custom" / "audit.jsonl").exists()
    assert not (in_empty_directory / "custom" / ".gitignore").exists()
    assert not (in_empty_directory / ".review-bot").exists()


def test_p1_s4_ac13_custom_path_inside_review_bot_gets_no_gitignore(
    bot, write_diff, in_empty_directory
):
    """A custom path that happens to sit in `.review-bot/` is still a custom path."""
    run = bot.review(
        write_diff(MINIMAL_DIFF), AUDIT_FLAG, ".review-bot/other.jsonl", provider=bot.mock()
    )

    assert run.exit_code == 1, run.stderr
    assert not (in_empty_directory / ".review-bot" / ".gitignore").exists()


def test_p1_s4_ac13_custom_variable_path_gets_no_gitignore(
    bot, write_diff, in_empty_directory, monkeypatch
):
    monkeypatch.setenv(AUDIT_VARIABLE, str(in_empty_directory / "env-logs" / "a.jsonl"))

    run = bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock())

    assert run.exit_code == 1, run.stderr
    assert not (in_empty_directory / "env-logs" / ".gitignore").exists()
    assert not (in_empty_directory / ".review-bot").exists()


# Limits: size and cost.


def hostile_diff(target_bytes: int) -> str:
    """Many small files: half skipped docs, every hunk declaring the wrong count."""
    parts: list[str] = []
    size = 0
    index = 0
    while True:
        path = f"docs/f{index:06d}.md" if index % 2 else f"src/m{index:06d}.py"
        part = section(path, f"+x = {index}\n").replace("+1,1 @@", "+1,9 @@")
        if size + len(part) > target_bytes:
            return "".join(parts)
        parts.append(part)
        size += len(part)
        index += 1


def assert_bounded_parse(log_path: Path) -> None:
    records = read_records(log_path)
    parsed = only(records, "parse.parsed")
    for items in entry_lists(parsed):
        assert len(items) <= LISTED_AT_MOST, len(items)
    assert len(log_path.read_bytes()) < 64_000


def test_p1_s4_limits_diff_at_the_default_limit_is_fast_and_its_record_bounded(
    bot, logged, write_diff, log_path
):
    text = hostile_diff(400_000)
    diff = write_diff(text)
    assert 390_000 < diff.stat().st_size <= 400_000

    start = time.perf_counter()
    run = logged(diff, provider=bot.mock(findings=[]))
    elapsed = time.perf_counter() - start

    assert run.exit_code == 0, run.stderr
    assert_bounded_parse(log_path)
    assert elapsed < 30, f"{elapsed:.1f} s for 400 kB"


def test_p1_s4_limits_large_raised_limit_diff_is_fast_and_its_record_bounded(
    bot, logged, write_diff, log_path
):
    diff = write_diff(hostile_diff(5_000_000))

    start = time.perf_counter()
    run = logged(diff, "--max-diff-bytes", "6000000", provider=bot.mock(findings=[]))
    elapsed = time.perf_counter() - start

    assert run.exit_code == 0, run.stderr
    assert_bounded_parse(log_path)
    assert elapsed < 120, f"{elapsed:.1f} s for 5 MB"


def test_p1_s4_limits_over_the_limit_exits_6_unparsed(bot, logged, write_diff, log_path):
    diff = write_diff(hostile_diff(4_000))

    run = logged(diff, "--max-diff-bytes", "1000", provider=bot.mock())

    assert run.exit_code == 6, run.stderr
    records = read_records(log_path)
    assert "parse.parsed" not in events(records)
    assert 6 in run_end_code(records)
