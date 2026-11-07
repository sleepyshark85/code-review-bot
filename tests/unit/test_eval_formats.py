"""Unit tests for reading eval cases and recordings: every invalid file is named, exit 2's way."""

import json
from pathlib import Path
from typing import Any

import pytest
from eval_cases import AI_BUG, RULE_BUG, failed_run, finding, recording, run_of, write_case

from review_bot.evals.formats import (
    EvalInputError,
    find_case_files,
    load_case,
    load_recording,
)


def write_text(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def rejected_case(case_file: Path) -> str:
    with pytest.raises(EvalInputError) as caught:
        load_case(case_file)
    return str(caught.value)


def test_load_case_reads_a_valid_case_with_its_diff_and_hash(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)

    loaded = load_case(case_file)

    assert [bug.id for bug in loaded.case.bugs] == ["swallowed", "float-sum"]
    assert loaded.diff_path == case_file.parent / "change.diff"
    assert len(loaded.case_sha256) == 64
    assert loaded.case.min_passing_runs == 0


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"colour": "red"}, "colour: Extra inputs are not permitted"),
        ({"schema": "eval-case/v2"}, "schema: Input should be 'eval-case/v1'"),
        ({"bugs": []}, "bugs: "),
        ({"bugs": [RULE_BUG | {"colour": 1}]}, "bugs.0.colour: Extra inputs"),
        ({"bugs": [RULE_BUG, RULE_BUG]}, "bug ids must be unique"),
        ({"bugs": [RULE_BUG | {"lines": [[5, 4]]}]}, "line window [5, 4] ends before it starts"),
        ({"bugs": [RULE_BUG | {"lines": []}]}, "bugs.0.lines"),
        ({"bugs": [RULE_BUG | {"min_runs_caught": 3}]}, "min_runs_caught is required for an ai"),
        ({"bugs": [AI_BUG | {"min_runs_caught": None}]}, "min_runs_caught is required for an ai"),
        ({"bugs": [AI_BUG | {"keywords": [[]]}]}, "bugs.0.keywords.0"),
        ({"min_passing_runs": "5"}, "min_passing_runs: Input should be a valid integer"),
    ],
)
def test_load_case_names_the_file_and_the_problem(
    tmp_path: Path, fields: dict[str, Any], expected: str
) -> None:
    case_file = write_case(tmp_path, **fields)

    message = rejected_case(case_file)

    assert message.startswith(f"{case_file}: ")
    assert expected in message


def test_load_case_names_the_file_when_it_isnt_json(tmp_path: Path) -> None:
    case_file = write_text(write_case(tmp_path), "{not json")

    assert rejected_case(case_file).startswith(f"{case_file}: top level: Invalid JSON")


def test_load_case_names_a_missing_diff(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    (case_file.parent / "change.diff").unlink()

    message = rejected_case(case_file)

    assert message.startswith(f"{case_file}: its diff 'change.diff' isn't a file")


def test_load_case_rejects_an_id_that_isnt_its_directory(tmp_path: Path) -> None:
    case_file = write_case(tmp_path, id="other")

    assert "id 'other' differs from its directory's name 'calc'" in rejected_case(case_file)


def test_load_case_names_an_unreadable_file(tmp_path: Path) -> None:
    missing = tmp_path / "calc" / "case.json"

    assert rejected_case(missing).startswith(f"{missing} can't be read")


def test_find_case_files_lists_each_case_sorted(tmp_path: Path) -> None:
    second = write_case(tmp_path, "zeta")
    first = write_case(tmp_path, "alpha")

    assert find_case_files(tmp_path) == [first, second]


@pytest.mark.parametrize("make", [lambda root: root / "missing", lambda root: root])
def test_find_case_files_rejects_a_directory_with_no_case(tmp_path: Path, make: Any) -> None:
    cases_dir = make(tmp_path)

    with pytest.raises(EvalInputError, match="--cases"):
        find_case_files(cases_dir)


def test_load_recording_reads_runs_with_findings_or_an_error(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    data = recording(case_file, [run_of(finding()), failed_run("refusal")])
    path = write_text(tmp_path / "rec.json", json.dumps(data))

    loaded = load_recording(path)

    assert [run.error_category for run in loaded.runs] == [None, "refusal"]
    assert loaded.runs[0].findings is not None
    assert loaded.runs[0].findings[0].line == 3


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (run_of() | {"error": {"category": "refusal"}}, "exactly one of findings and error"),
        ({"usage": {"input_tokens": 0, "output_tokens": 0}, "warnings": []}, "exactly one"),
        (failed_run("melted"), "runs.0.error.category"),
        (run_of(finding(line=0)), "runs.0.findings.0.line"),
        ({"findings": []}, "runs.0.usage"),
    ],
)
def test_load_recording_names_the_file_and_the_problem(
    tmp_path: Path, run: dict[str, Any], expected: str
) -> None:
    case_file = write_case(tmp_path)
    path = write_text(tmp_path / "rec.json", json.dumps(recording(case_file, [run])))

    with pytest.raises(EvalInputError) as caught:
        load_recording(path)

    assert str(caught.value).startswith(f"{path}: ")
    assert expected in str(caught.value)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"schema": "eval-recording/v9"}, "schema"),
        ({"kind": "replayed"}, "kind"),
        ({"recorded_at": "2026-11-07T10:00:00"}, "recorded_at"),
        ({"runs": []}, "runs"),
        ({"extra": 1}, "extra"),
    ],
)
def test_load_recording_rejects_bad_top_level_fields(
    tmp_path: Path, fields: dict[str, Any], expected: str
) -> None:
    case_file = write_case(tmp_path)
    data = recording(case_file, [run_of()]) | fields
    path = write_text(tmp_path / "rec.json", json.dumps(data))

    with pytest.raises(EvalInputError, match=f": {expected}"):
        load_recording(path)
