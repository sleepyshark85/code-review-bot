"""Unit tests for replay: freshness, choosing recordings, and scoring through the real pipeline."""

import asyncio
import dataclasses
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from eval_cases import (
    CALC_DIFF,
    CALC_FILE,
    context_recording,
    failed_run,
    finding,
    recording,
    run_of,
    tool_call,
    write_case,
    write_json,
    write_live,
    write_live_context,
    write_repo,
    write_synthetic,
)

from review_bot import prompt
from review_bot.audit import AuditLog
from review_bot.context.tool_args import TOOL_DEFINITIONS
from review_bot.errors import GitError, GitTimeoutError
from review_bot.evals import freshness_fields
from review_bot.evals import replay as replay_module
from review_bot.evals.formats import EvalInputError, EvalMode, Recording
from review_bot.evals.replay import replay_cases, require_live
from review_bot.evals.score import CaseResult, CaseScore, RunOutcome
from review_bot.providers.mock import MockProvider
from review_bot.review import run_pipeline
from review_bot.settings import Settings

README_SECTION = (
    "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -0,0 +1 @@\n+x\n"
)


def replay(
    cases_dir: Path, control: Path | None = None, mode: EvalMode = "diff"
) -> list[CaseResult]:
    return asyncio.run(replay_cases(cases_dir, control, mode=mode))


def replay_one(cases_dir: Path, control: Path | None = None, mode: EvalMode = "diff") -> CaseResult:
    [result] = replay(cases_dir, control, mode)
    return result


def scored(result: CaseResult) -> CaseScore:
    assert result.score is not None
    return result.score


def rejected(cases_dir: Path, control: Path | None = None, mode: EvalMode = "diff") -> str:
    with pytest.raises(EvalInputError) as caught:
        replay(cases_dir, control, mode)
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
    assert [(credit.run, credit.finding.line) for credit in scored(result).dropped] == [(2, 42)]
    assert result.recording is not None
    assert result.recording.kind == "live"


def test_replay_cases_scores_the_final_column_after_dedup(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    # Tagged with the rule that caught line 4, one line away and no more severe: a duplicate.
    duplicate = finding(severity="low", rule="swallowed-exception")
    write_live(case_file, recording(case_file, [run_of(duplicate)], kind="live"))

    ai = scored(replay_one(tmp_path)).bugs[1]

    assert (ai.caught, ai.caught_final) == (1, 0)


def test_replay_cases_marks_a_failed_run_no_review_with_its_category(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    runs = [run_of(finding()), failed_run("refusal")]
    write_live(case_file, recording(case_file, runs, kind="live"))

    score = scored(replay_one(tmp_path))

    assert [(run.outcome, run.error) for run in score.runs] == [
        (RunOutcome.PASSED, None),
        (RunOutcome.NO_REVIEW, "refusal"),
    ]


def test_replay_cases_scores_injection_on_findings_as_returned_before_verify(
    tmp_path: Path,
) -> None:
    case_file = write_case(
        tmp_path,
        injection={"requested_file": "secrets/keys.py"},
        min_passing_runs=1,
    )
    obeyed = finding(file="secrets/keys.py", line=1, rationale="As asked.")
    write_live(case_file, recording(case_file, [run_of(finding(), obeyed)], kind="live"))

    score = scored(replay_one(tmp_path))

    (run,) = score.runs
    assert run.outcome is RunOutcome.FOLLOWED
    assert [credit.finding.file for credit in score.dropped] == ["secrets/keys.py"]


def test_replay_cases_counts_noise_the_same_whatever_the_threshold_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_file = write_case(
        tmp_path,
        bugs=[],
        noise_budget={"max_findings": 0, "max_severity": "low"},
        min_passing_runs=1,
    )
    write_live(case_file, recording(case_file, [run_of(finding(severity="low"))], kind="live"))
    monkeypatch.setenv("REVIEW_BOT_THRESHOLD", "critical")

    (run,) = scored(replay_one(tmp_path)).runs

    assert run.outcome is RunOutcome.OVER_BUDGET
    assert len(run.noise) == 1


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

    assert require_live(result, "claude-opus-5-5", mode="diff") is result


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (CaseResult("calc", None, None), "--require-live needs a live recording"),
        (live_result(kind="synthetic"), "--require-live needs a live recording"),
        (live_result(model="claude-other"), "default model claude-opus-5-5, but this one is of"),
    ],
)
def test_require_live_fails_anything_else(result: CaseResult, expected: str) -> None:
    [problem] = require_live(result, "claude-opus-5-5", mode="diff").problems

    assert expected in problem
    assert problem.endswith(
        "record one with python -m review_bot.evals live --cases DIR --runs N --max-calls M"
    )


@pytest.mark.parametrize(
    "result",
    [CaseResult("calc", None, None), live_result(model="claude-other")],
    ids=["no_recording", "other_model"],
)
def test_require_live_in_context_mode_says_where_a_live_context_recording_goes(
    result: CaseResult,
) -> None:
    [problem] = require_live(result, "claude-opus-5-5", mode="context").problems

    assert problem.endswith(
        "; no command records a live context run yet (P3-S6); one goes in "
        "recordings/live-context/recording.json"
    )
    assert "evals live" not in problem


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


# --- P3-S5: context mode ------------------------------------------------------------------------

UTIL = "app/util.py"
UTIL_TEXT = "".join(f"line_{number} = {number}\n" for number in range(1, 21))


def context_case(cases_dir: Path) -> Path:
    case_file = write_case(cases_dir, repo="repo")
    write_repo(case_file, {UTIL: UTIL_TEXT})
    return case_file


def cited(first: int, last: int, file: str = UTIL) -> dict[str, Any]:
    return finding(evidence=[{"file": file, "start_line": first, "end_line": last}])


def test_freshness_fields_in_context_mode_add_the_repo_tree_and_the_tool_definitions(
    tmp_path: Path,
) -> None:
    case_file = context_case(tmp_path)
    diff_fields = freshness_fields(case_file)

    fields = freshness_fields(case_file, "context")

    assert fields.keys() == {*diff_fields, "repo_tree"}
    assert fields["case_sha256"] == diff_fields["case_sha256"]
    assert fields["diff_sha256"] == diff_fields["diff_sha256"]
    assert fields["prompt_version"] == prompt.CONTEXT_PROMPT_VERSION
    assert fields["prompt_sha256"] != diff_fields["prompt_sha256"]
    assert len(fields["repo_tree"]) == 40


def test_freshness_fields_in_context_mode_follow_the_repo_but_not_diff_modes(
    tmp_path: Path,
) -> None:
    case_file = context_case(tmp_path)
    before = (freshness_fields(case_file), freshness_fields(case_file, "context"))

    (case_file.parent / "repo" / UTIL).write_text(UTIL_TEXT + "more = 1\n", encoding="utf-8")
    after = (freshness_fields(case_file), freshness_fields(case_file, "context"))

    assert after[0] == before[0]
    assert after[1] == before[1] | {"repo_tree": after[1]["repo_tree"]}
    assert after[1]["repo_tree"] != before[1]["repo_tree"]


def test_freshness_fields_in_context_mode_change_with_a_tool_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_file = context_case(tmp_path)
    before = freshness_fields(case_file, "context")
    changed = dataclasses.replace(TOOL_DEFINITIONS[0], description="Read anything.")

    monkeypatch.setattr(
        "review_bot.context.tools.TOOL_DEFINITIONS", (changed, *TOOL_DEFINITIONS[1:])
    )
    after = freshness_fields(case_file, "context")

    assert after == before | {"prompt_sha256": after["prompt_sha256"]}
    assert after["prompt_sha256"] != before["prompt_sha256"]


def test_freshness_fields_in_context_mode_use_an_empty_repo_without_one(tmp_path: Path) -> None:
    fields = freshness_fields(write_case(tmp_path), "context")

    assert fields["repo_tree"] == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def test_replay_cases_in_context_mode_without_a_recording_scores_the_rules(
    tmp_path: Path,
) -> None:
    context_case(tmp_path)

    result = replay_one(tmp_path, mode="context")

    rule, ai = scored(result).bugs
    assert (rule.caught, ai.is_measured, result.recording) == (1, False, None)


def test_replay_cases_in_context_mode_reads_the_live_context_recording_only(
    tmp_path: Path,
) -> None:
    case_file = context_case(tmp_path)
    write_live(case_file, recording(case_file, [run_of()], kind="live"))
    write_live_context(
        case_file, context_recording(case_file, [run_of(finding())] * 2, kind="live")
    )

    context, diff = replay_one(tmp_path, mode="context"), replay_one(tmp_path)

    assert scored(context).bugs[1].caught == 2
    assert scored(diff).bugs[1].caught == 0


@pytest.mark.parametrize(
    ("calls", "kept", "dropped"),
    [
        ([tool_call("read", [(UTIL, 1, 10)])], 1, 0),
        ([tool_call("grep", [(UTIL, 4, 4)])], 0, 1),
        ([tool_call("read", outcome="refused", reason="not_found")], 0, 1),
        ([], 0, 1),
    ],
    ids=["read_covers_it", "grep_line_only", "refused", "no_calls"],
)
def test_replay_cases_checks_evidence_against_the_runs_recorded_tool_calls(
    tmp_path: Path, calls: list[dict[str, Any]], kept: int, dropped: int
) -> None:
    case_file = context_case(tmp_path)
    run = run_of(cited(3, 5)) | {"tool_calls": calls}
    control = write_synthetic(case_file, context_recording(case_file, [run]))

    [replayed] = scored(replay_one(tmp_path, control, "context")).evidence.runs

    assert (replayed.evidence_kept, replayed.evidence_dropped) == (kept, dropped)
    assert [finding.line for finding in replayed.final] == [3], "the finding is kept either way"


def test_replay_cases_in_context_mode_counts_diff_lines_as_shown(tmp_path: Path) -> None:
    case_file = context_case(tmp_path)
    control = write_synthetic(
        case_file, context_recording(case_file, [run_of(cited(2, 5, "app/calc.py"))])
    )

    [replayed] = scored(replay_one(tmp_path, control, "context")).evidence.runs

    assert (replayed.evidence_kept, replayed.evidence_dropped) == (1, 0)


def test_replay_cases_checks_each_run_against_its_own_tool_calls(tmp_path: Path) -> None:
    case_file = context_case(tmp_path)
    runs = [
        run_of(cited(3, 3)) | {"tool_calls": [tool_call("read", [(UTIL, 1, 10)])]},
        run_of(cited(3, 3)),
    ]
    control = write_synthetic(case_file, context_recording(case_file, runs))

    first, second = scored(replay_one(tmp_path, control, "context")).evidence.runs

    assert (first.evidence_kept, second.evidence_kept) == (1, 0)


def test_replay_cases_fails_a_context_recording_with_a_stale_repo_tree(tmp_path: Path) -> None:
    case_file = context_case(tmp_path)
    data = context_recording(case_file, [run_of()], repo_tree="0" * 40)
    control = write_synthetic(case_file, data)

    [problem] = replay_one(tmp_path, control, "context").problems

    assert "its repo_tree differs from today's" in problem
    assert problem.endswith(
        "Set its five freshness fields to today's values, from "
        'review_bot.evals.freshness_fields(case.json, "context")'
    )


def test_replay_cases_tells_how_to_replace_a_stale_live_context_recording(tmp_path: Path) -> None:
    case_file = context_case(tmp_path)
    write_live_context(
        case_file, context_recording(case_file, [run_of()], kind="live", repo_tree="0" * 40)
    )

    [problem] = replay_one(tmp_path, mode="context").problems

    assert problem.endswith(
        "Record it again: no command records a live context run yet (P3-S6); one goes in "
        "recordings/live-context/recording.json"
    )


def test_replay_cases_rejects_a_context_recording_under_live(tmp_path: Path) -> None:
    case_file = context_case(tmp_path)
    path = write_live(case_file, context_recording(case_file, [run_of()], kind="live"))

    message = rejected(tmp_path)

    assert message.startswith(f"{path} is a context-mode recording under recordings/live/")
    assert "Move it to recordings/live-context/" in message


def test_replay_cases_rejects_a_diff_recording_under_live_context(tmp_path: Path) -> None:
    case_file = context_case(tmp_path)
    path = write_live_context(case_file, recording(case_file, [run_of()], kind="live"))

    message = rejected(tmp_path, mode="context")

    assert message.startswith(f"{path} is a diff-mode recording under recordings/live-context/")


def test_replay_cases_rejects_a_synthetic_recording_under_live_context(tmp_path: Path) -> None:
    case_file = context_case(tmp_path)
    path = write_live_context(case_file, context_recording(case_file, [run_of()]))

    assert rejected(tmp_path).startswith(
        f"{path} is a synthetic recording under recordings/live-context/"
    )


def test_replay_cases_tells_where_a_live_context_recording_under_synthetic_goes(
    tmp_path: Path,
) -> None:
    case_file = context_case(tmp_path)
    write_synthetic(case_file, context_recording(case_file, [run_of()], kind="live"))

    assert "Move it to recordings/live-context/recording.json" in rejected(tmp_path)


@pytest.mark.parametrize(("mode", "other"), [("diff", "context"), ("context", "diff")])
def test_replay_cases_rejects_a_control_of_the_other_mode(
    tmp_path: Path, mode: EvalMode, other: EvalMode
) -> None:
    case_file = context_case(tmp_path / "cases")
    make = recording if other == "diff" else context_recording
    control = write_json(tmp_path / "control.json", make(case_file, [run_of()]))

    message = rejected(tmp_path / "cases", control, mode)

    assert message.startswith(f"--recording {control} is a {other}-mode recording")
    assert f"Pass --mode {other}" in message


@pytest.mark.parametrize(
    "error",
    [
        GitError("git fast-import failed with exit code 128"),
        GitTimeoutError("git fast-import took longer than 60 seconds"),
    ],
    ids=["fails", "times_out"],
)
def test_replay_cases_names_a_case_whose_repo_cant_be_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: GitError
) -> None:
    case_file = context_case(tmp_path)

    def fail(*_args: Any) -> Any:
        raise error

    monkeypatch.setattr(replay_module, "build_fixture_repo", fail)

    assert rejected(tmp_path, mode="context") == f"{case_file}: its repo can't be built: {error}"


def test_replay_cases_in_context_mode_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases_dir = tmp_path / "cases"
    case_file = context_case(cases_dir)
    control = write_synthetic(case_file, context_recording(case_file, [run_of(finding())]))
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    before = sorted(cases_dir.rglob("*"))

    replay(cases_dir, control, "context")

    assert sorted(cases_dir.rglob("*")) == before
    assert list(temp_root.iterdir()) == []


def test_replay_cases_counts_only_the_diffs_lines_of_a_file_it_changes_as_the_repo_is_its_base(
    tmp_path: Path,
) -> None:
    case_file = write_case(tmp_path, repo="repo")
    write_repo(case_file, {"app/calc.py": "".join(f"x{n} = {n}\n" for n in range(1, 21))})
    read_all = [tool_call("read", [("app/calc.py", 1, 20)])]
    runs = [
        run_of(cited(8, 10, "app/calc.py")) | {"tool_calls": read_all},
        run_of(cited(2, 4, "app/calc.py")) | {"tool_calls": read_all},
    ]
    control = write_synthetic(case_file, context_recording(case_file, runs))

    outside, inside = scored(replay_one(tmp_path, control, "context")).evidence.runs

    assert (outside.evidence_kept, outside.evidence_dropped) == (0, 1)
    assert (inside.evidence_kept, inside.evidence_dropped) == (1, 0)


MONEY_CONFIG = '[[domain_rules]]\nrule = "decimal-money"\npaths = ["app/**"]\n'
MONEY_BUG = {"id": "float-money", "source": "rule", "file": CALC_FILE, "lines": [[3, 3]]}
MONEY_DIFF = CALC_DIFF.replace("+        return sum(items)\n", "+        return sum(items) * 1.2\n")


def money_case(cases_dir: Path, config: str = MONEY_CONFIG) -> Path:
    case_file = write_case(cases_dir, bugs=[MONEY_BUG], diff=MONEY_DIFF, repo="repo")
    write_repo(case_file, {".review-bot.toml": config})
    return case_file


@pytest.mark.parametrize("mode", ["diff", "context"])
def test_replay_cases_runs_the_domain_rules_of_the_case_repos_config(
    tmp_path: Path, mode: EvalMode
) -> None:
    money_case(tmp_path)

    result = replay_one(tmp_path, mode=mode)

    [money] = scored(result).bugs
    assert (money.caught, money.credits[0].finding.line) == (1, 3)


def test_replay_cases_without_a_config_in_the_case_repo_runs_no_domain_rule(
    tmp_path: Path,
) -> None:
    case_file = write_case(tmp_path, bugs=[MONEY_BUG], diff=MONEY_DIFF, repo="repo")
    write_repo(case_file, {"app/other.py": "x = 1\n"})

    [money] = scored(replay_one(tmp_path)).bugs

    assert money.caught == 0


def test_replay_cases_names_a_case_whose_repo_config_is_invalid(tmp_path: Path) -> None:
    case_file = money_case(tmp_path, config="colour = 1\n")

    message = rejected(tmp_path)

    assert message.startswith(f"{case_file}: its repo's .review-bot.toml is invalid: ")
    assert "unknown key 'colour'" in message
    assert message.endswith("Fix the case's repo.")


def test_freshness_fields_in_context_mode_hash_the_domain_rules_in_the_prompt(
    tmp_path: Path,
) -> None:
    with_rules = freshness_fields(money_case(tmp_path / "a"), "context")
    without = freshness_fields(money_case(tmp_path / "b", config=""), "context")

    assert with_rules["prompt_sha256"] != without["prompt_sha256"]


def test_freshness_fields_in_diff_mode_ignore_the_domain_rules(tmp_path: Path) -> None:
    with_rules = freshness_fields(money_case(tmp_path / "a"))
    without = freshness_fields(money_case(tmp_path / "b", config=""))

    assert with_rules["prompt_sha256"] == without["prompt_sha256"]
