"""Unit tests for replay: freshness, choosing recordings, and scoring through the real pipeline."""

import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from eval_cases import (
    CALC_DIFF,
    failed_run,
    finding,
    recording,
    run_of,
    write_case,
    write_json,
    write_live,
    write_synthetic,
)

from review_bot import prompt
from review_bot.audit import AuditLog
from review_bot.evals import freshness_fields
from review_bot.evals.formats import EvalInputError, Recording
from review_bot.evals.replay import replay_cases, require_live
from review_bot.evals.score import CaseResult, CaseScore
from review_bot.providers.mock import MockProvider
from review_bot.review import run_pipeline
from review_bot.settings import Settings

README_SECTION = (
    "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -0,0 +1 @@\n+x\n"
)


def replay(cases_dir: Path, control: Path | None = None) -> list[CaseResult]:
    return asyncio.run(replay_cases(cases_dir, control))


def replay_one(cases_dir: Path, control: Path | None = None) -> CaseResult:
    [result] = replay(cases_dir, control)
    return result


def scored(result: CaseResult) -> CaseScore:
    assert result.score is not None
    return result.score


def rejected(cases_dir: Path, control: Path | None = None) -> str:
    with pytest.raises(EvalInputError) as caught:
        replay(cases_dir, control)
    return str(caught.value)


def test_freshness_fields_hash_the_prompts_the_pipeline_sends(tmp_path: Path) -> None:
    case_file = write_case(tmp_path / "cases")
    provider = MockProvider(findings=())
    audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: datetime(2026, 11, 7, tzinfo=UTC))
    asyncio.run(run_pipeline(case_file.parent / "change.diff", Settings(), provider, audit))
    [request] = provider.requests
    sent = json.dumps([request.system_prompt, request.user_prompt]).encode("utf-8")

    fields = freshness_fields(case_file)

    assert fields == {
        "case_sha256": hashlib.sha256(case_file.read_bytes()).hexdigest(),
        "diff_sha256": hashlib.sha256(CALC_DIFF.encode("utf-8")).hexdigest(),
        "prompt_version": prompt.PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(sent).hexdigest(),
    }


def test_freshness_fields_change_with_the_system_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_file = write_case(tmp_path)
    before = freshness_fields(case_file)

    monkeypatch.setattr(prompt, "_SYSTEM_PROMPT", "Another prompt.")
    after = freshness_fields(case_file)

    assert after["prompt_sha256"] != before["prompt_sha256"]
    assert after | {"prompt_sha256": ""} == before | {"prompt_sha256": ""}


def test_freshness_fields_follow_what_the_model_sees_not_skipped_files(tmp_path: Path) -> None:
    before = freshness_fields(write_case(tmp_path / "a"))

    after = freshness_fields(write_case(tmp_path / "b", diff=CALC_DIFF + README_SECTION))

    assert after["prompt_sha256"] == before["prompt_sha256"]
    assert after["diff_sha256"] != before["diff_sha256"]


def test_replay_cases_with_no_recording_scores_the_rules_through_the_pipeline(
    tmp_path: Path,
) -> None:
    write_case(tmp_path)

    result = replay_one(tmp_path)

    rule, ai = scored(result).bugs
    assert (rule.caught, rule.credits[0].finding.line) == (1, 4)
    assert not ai.is_measured
    assert result.passed


def test_replay_cases_scores_a_live_recording_through_verify(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    off_the_diff = finding(line=42)
    write_live(
        case_file,
        recording(case_file, [run_of(finding()), run_of(off_the_diff)], kind="live"),
    )

    result = replay_one(tmp_path)

    ai = scored(result).bugs[1]
    assert (ai.caught, ai.out_of, ai.passed) == (1, 2, False)
    assert scored(result).unmatched == ()
    assert result.recording is not None
    assert result.recording.kind == "live"


def test_replay_cases_scores_the_final_column_after_dedup(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    # Tagged with the rule that caught line 4, one line away and no more severe: a duplicate.
    duplicate = finding(severity="low", rule="swallowed-exception")
    write_live(case_file, recording(case_file, [run_of(duplicate)], kind="live"))

    ai = scored(replay_one(tmp_path)).bugs[1]

    assert (ai.caught, ai.caught_final) == (1, 0)


def test_replay_cases_lists_a_failed_run_by_category(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    runs = [run_of(finding()), failed_run("refusal")]
    write_live(case_file, recording(case_file, runs, kind="live"))

    score = scored(replay_one(tmp_path))

    assert [(number, str(category)) for number, category in score.failed_runs] == [(2, "refusal")]


@pytest.mark.parametrize("field", ["case_sha256", "diff_sha256", "prompt_version", "prompt_sha256"])
def test_replay_cases_fails_a_stale_recording_without_scoring_it(
    tmp_path: Path, field: str
) -> None:
    case_file = write_case(tmp_path)
    write_live(case_file, recording(case_file, [run_of()], kind="live", **{field: "old"}))

    result = replay_one(tmp_path)

    assert result.score is None
    [problem] = result.problems
    assert f"the recording is stale: its {field} differs from today's" in problem
    assert problem.endswith(
        "Record it again: record a live run with python -m review_bot.evals live "
        "--cases DIR --runs N --max-calls M"
    )


def test_replay_cases_scores_a_control_in_place_of_the_live_recording(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    write_live(case_file, recording(case_file, [run_of()], kind="live"))
    control = write_synthetic(case_file, recording(case_file, [run_of(finding())] * 2))

    result = replay_one(tmp_path, control)

    assert result.recording is not None
    assert result.recording.kind == "synthetic"
    assert scored(result).bugs[1].passed


def test_replay_cases_accepts_a_control_outside_the_cases_directory(tmp_path: Path) -> None:
    case_file = write_case(tmp_path / "cases")
    control = write_json(tmp_path / "control.json", recording(case_file, [run_of(finding())]))

    assert scored(replay_one(tmp_path / "cases", control)).bugs[1].caught == 1


def test_replay_cases_rejects_a_synthetic_recording_under_live(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    path = write_live(case_file, recording(case_file, [run_of()]))

    assert rejected(tmp_path).startswith(f"{path} is a synthetic recording under recordings/live/")


def test_replay_cases_rejects_a_live_recording_under_synthetic(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    path = write_synthetic(case_file, recording(case_file, [run_of()], kind="live"))

    assert rejected(tmp_path).startswith(f"{path} is a live recording under recordings/synthetic/")


def test_replay_cases_rejects_a_live_control(tmp_path: Path) -> None:
    case_file = write_case(tmp_path / "cases")
    control = write_json(tmp_path / "live.json", recording(case_file, [run_of()], kind="live"))

    message = rejected(tmp_path / "cases", control)

    assert message.startswith(f"--recording {control} is a live recording")


def test_replay_cases_rejects_a_control_naming_no_case(tmp_path: Path) -> None:
    case_file = write_case(tmp_path / "cases")
    data = recording(case_file, [run_of()], case_id="elsewhere")
    control = write_json(tmp_path / "control.json", data)

    assert "case_id 'elsewhere' names no case" in rejected(tmp_path / "cases", control)


def test_replay_cases_rejects_a_live_recording_of_another_case(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    path = write_live(case_file, recording(case_file, [run_of()], kind="live", case_id="other"))

    assert rejected(tmp_path).startswith(f"{path}: case_id 'other' isn't its case's id 'calc'")


def test_replay_cases_names_a_case_whose_diff_cant_be_reviewed(tmp_path: Path) -> None:
    case_file = write_case(tmp_path, diff="no diff here\n")

    message = rejected(tmp_path)

    assert message.startswith(f"{case_file}: its diff can't be reviewed: ")


def test_replay_cases_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases_dir = tmp_path / "cases"
    case_file = write_case(cases_dir)
    write_live(case_file, recording(case_file, [run_of(finding())], kind="live"))
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    before = sorted(cases_dir.rglob("*"))

    replay(cases_dir)

    assert sorted(cases_dir.rglob("*")) == before
    assert list(temp_root.iterdir()) == []


def live_result(**fields: Any) -> CaseResult:
    data: dict[str, Any] = {"kind": "live", "model": "claude-opus-5-5"} | fields
    base = {
        "schema": "eval-recording/v1",
        "case_id": "calc",
        "case_sha256": "",
        "diff_sha256": "",
        "prompt_version": "",
        "prompt_sha256": "",
        "sdk_version": "",
        "recorded_at": "2026-11-07T10:00:00+00:00",
        "runs": [run_of()],
    }
    return CaseResult("calc", Recording.model_validate_json(json.dumps(base | data)), None)


def test_require_live_passes_a_live_recording_of_the_default_model() -> None:
    result = live_result()

    assert require_live(result, "claude-opus-5-5") is result


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (CaseResult("calc", None, None), "--require-live needs a live recording"),
        (live_result(kind="synthetic"), "--require-live needs a live recording"),
        (live_result(model="claude-other"), "default model claude-opus-5-5, but this one is of"),
    ],
)
def test_require_live_fails_anything_else(result: CaseResult, expected: str) -> None:
    [problem] = require_live(result, "claude-opus-5-5").problems

    assert expected in problem


def test_freshness_fields_name_a_case_whose_diff_cant_be_reviewed(tmp_path: Path) -> None:
    case_file = write_case(tmp_path, diff="")

    with pytest.raises(EvalInputError, match="its diff can't be reviewed: "):
        freshness_fields(case_file)


def test_replay_cases_tells_how_to_refresh_a_stale_synthetic_control(tmp_path: Path) -> None:
    case_file = write_case(tmp_path / "cases")
    data = recording(case_file, [run_of()], diff_sha256="old")
    control = write_json(tmp_path / "control.json", data)

    [problem] = replay_one(tmp_path / "cases", control).problems

    assert "its diff_sha256 differs from today's" in problem
    assert "Set its four freshness fields to today's values" in problem
    assert "case_sha256" not in problem
    assert "review_bot.evals.freshness_fields(case.json), or record a live run" in problem


SAMPLE_CASE = Path(__file__).parents[1] / "evals" / "cases" / "sample" / "case.json"


@pytest.mark.parametrize(
    "control",
    sorted((SAMPLE_CASE.parent / "recordings" / "synthetic").glob("*.json")),
    ids=lambda path: path.name,
)
def test_each_committed_sample_control_is_fresh(control: Path) -> None:
    data = json.loads(control.read_text(encoding="utf-8"))
    today = freshness_fields(SAMPLE_CASE)

    stale = {name: value for name, value in today.items() if data[name] != value}

    assert stale == {}, f"Set these fields in {control} to today's values: {stale}"
