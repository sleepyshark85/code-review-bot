import json
from pathlib import Path

import pytest
from shadow_samples import report, write_report

from review_bot.errors import ConfigError
from review_bot.pr.feedback import ShadowFeedback
from review_bot.pr.reports import (
    MAX_FILE_BYTES,
    BadLabelLine,
    parse_labels,
    read_labels,
    read_report,
    read_reports,
    skipped_line,
)
from review_bot.pr.units import ShadowFile

LABEL = {
    "schema": "feedback/v1",
    "channel": "shadow",
    "repo": "owner/repo",
    "pr": 7,
    "fingerprint": "f" * 16,
    "file": "src/app.py",
    "rule_part": "ai:logic",
    "anchor": "a" * 16,
    "index": 0,
    "source": "ai",
    "severity": "medium",
    "label": "accept",
    "reason": None,
    "run_id": "run-1",
    "model": "claude-x",
    "prompt_version": "p1-v1",
    "recorded_at": "2026-11-08T12:00:00+00:00",
}


def test_read_reports_reads_json_files_at_any_depth_and_says_why_others_are_skipped(
    tmp_path: Path,
) -> None:
    write_report(tmp_path, "a/b", report())
    (tmp_path / "broken.json").write_text("{", encoding="ascii")
    (tmp_path / "shadow.md").write_text("not read", encoding="ascii")

    reports, skipped = read_reports(tmp_path, flag="--reports")

    assert [type(item) for item in reports] == [ShadowFile]
    assert skipped == [(tmp_path / "broken.json", "not JSON")]


def test_read_reports_of_a_missing_directory_raises_naming_reports(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="--reports"):
        read_reports(tmp_path / "missing", flag="--reports")


def test_read_report_over_10_mb_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "big.json"
    path.write_bytes(b" " * (MAX_FILE_BYTES + 1))

    assert read_report(path) == f"over {MAX_FILE_BYTES} bytes"


def test_read_report_names_the_first_problem_of_another_document(tmp_path: Path) -> None:
    path = tmp_path / "other.json"
    path.write_text(json.dumps(report(schema="report/v1")), encoding="ascii")

    assert str(read_report(path)).startswith("not a shadow/v1 report (schema:")


def test_read_labels_parses_every_record_ignoring_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    path.write_text(f"{json.dumps(LABEL)}\n\n{json.dumps(LABEL)}\n", encoding="ascii")

    records = read_labels(path)

    assert isinstance(records, list)
    assert [type(record) for record in records] == [ShadowFeedback, ShadowFeedback]


def test_read_labels_names_the_first_line_that_isnt_a_record(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    path.write_text(f"{json.dumps(LABEL)}\n{json.dumps(LABEL | {'pr': 0})}\n", encoding="ascii")

    assert str(read_labels(path)).startswith("line 2 isn't a feedback/v1 record (pr:")


def test_read_labels_of_a_missing_file_says_it_cant_be_read(tmp_path: Path) -> None:
    assert str(read_labels(tmp_path / "missing.jsonl")).startswith("can't be read")


def test_read_labels_over_10_mb_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    path.write_bytes(b"\n" * (MAX_FILE_BYTES + 1))

    assert read_labels(path) == f"over {MAX_FILE_BYTES} bytes"


def test_skipped_line_names_the_file_and_the_problem(tmp_path: Path) -> None:
    path = tmp_path / "x.json"

    assert skipped_line(path, "not JSON") == f"review-bot pr: skipped {path}: not JSON"


def test_parse_labels_returns_the_first_bad_line_with_its_number_and_problem() -> None:
    lines = [json.dumps(LABEL), "", json.dumps(LABEL | {"label": "maybe"}), "not json"]
    raw = "\n".join(lines)

    assert parse_labels(raw.encode()) == BadLabelLine(
        3, "label: Value error, a shadow record's label is 'accept' or 'reject'"
    )
