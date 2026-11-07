"""Unit tests for the eval command: its report and its own exit codes, 0 pass, 1 fail, 2 usage."""

from pathlib import Path

import pytest
from eval_cases import failed_run, finding, recording, run_of, write_case, write_json, write_live

from review_bot.evals import main
from review_bot.evals.render import REPLAY_DISCLAIMER


def replay(capsys: pytest.CaptureFixture[str], *args: str | Path) -> tuple[int, str, str]:
    exit_code = main(["replay", *map(str, args)])
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def test_main_with_no_recording_passes_on_rules_and_marks_ai_bugs_not_measured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_case(tmp_path)

    exit_code, out, err = replay(capsys, "--cases", tmp_path)

    assert exit_code == 0
    assert out.splitlines()[0] == REPLAY_DISCLAIMER
    assert "  swallowed: rule, caught 1/1, final 1/1, needs 1, pass, evidence=rules" in out
    assert "  float-sum: ai, not measured" in out
    assert "  recall: rules 1/1, ai not measured" in out
    assert out.endswith("result: pass\n")
    assert err == ""


def test_main_shows_each_ai_row_with_its_evidence_and_credits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = write_case(tmp_path / "cases")
    runs = [run_of(finding()), run_of(finding(line=2, rationale="Odd\nname.")), failed_run()]
    control = write_json(tmp_path / "control.json", recording(case_file, runs, model="m-2"))

    exit_code, out, _ = replay(capsys, "--cases", tmp_path / "cases", "--recording", control)

    assert exit_code == 1
    assert "case calc: synthetic recording of m-2, recorded 2026-11-07, 3 runs" in out
    assert (
        "  float-sum: ai, caught 1/3, final 1/3, needs 2, fail, evidence=synthetic, model=m-2, "
        "recorded=2026-11-07\n    run 1: app/calc.py:3 [logic] Summing floats loses cents.\n"
    ) in out
    assert "  failed runs: run 3 (invalid_output)" in out
    assert "  unmatched findings:\n    run 2: app/calc.py:2 [logic] Odd\\nname." in out
    assert "  FAIL: ai bug float-sum was caught in 1 of 3 runs; it needs 2" in out
    assert out.endswith("result: fail\n")


def test_main_with_require_live_fails_a_case_with_no_live_recording(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_case(tmp_path)

    exit_code, out, _ = replay(capsys, "--cases", tmp_path, "--require-live")

    assert exit_code == 1
    assert "  FAIL: --require-live needs a live recording" in out


def test_main_with_require_live_passes_a_live_recording_of_the_default_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = write_case(tmp_path)
    write_live(case_file, recording(case_file, [run_of(finding())] * 2, kind="live"))

    exit_code, out, _ = replay(capsys, "--cases", tmp_path, "--require-live")

    assert exit_code == 0
    assert "evidence=live" in out


def test_main_shows_a_stale_recording_as_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = write_case(tmp_path)
    write_live(case_file, recording(case_file, [run_of()], kind="live", prompt_version="p0"))

    exit_code, out, _ = replay(capsys, "--cases", tmp_path)

    assert exit_code == 1
    assert "  FAIL: the recording is stale: its prompt_version differs" in out
    assert "recall:" not in out


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], "the following arguments are required: --cases"),
        (["--cases"], "expected one argument"),
        (["--cases", "missing"], "--cases missing isn't a directory"),
        (["--cases", ".", "--recording", "r.json", "--require-live"], "can't be combined"),
    ],
)
def test_main_exits_2_on_a_usage_error_with_one_line_on_stderr(
    capsys: pytest.CaptureFixture[str], args: list[str], expected: str
) -> None:
    exit_code, out, err = replay(capsys, *args)

    assert exit_code == 2
    assert out == ""
    assert err.startswith("review-bot evals: error: ")
    assert expected in err
    assert err.count("\n") == 1


def test_main_exits_2_on_an_invalid_case_naming_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = write_case(tmp_path, colour="red")

    exit_code, _, err = replay(capsys, "--cases", tmp_path)

    assert exit_code == 2
    assert str(case_file) in err


def test_main_exits_2_without_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "required: command" in capsys.readouterr().err


@pytest.mark.parametrize("prefix", ["b/", "./"])
def test_main_credits_a_finding_naming_a_prefixed_path_and_shows_the_diff_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], prefix: str
) -> None:
    case_file = write_case(tmp_path)
    prefixed = finding(file=f"{prefix}app/calc.py")
    write_live(case_file, recording(case_file, [run_of(prefixed)] * 2, kind="live"))

    exit_code, out, _ = replay(capsys, "--cases", tmp_path)

    assert exit_code == 0
    assert "  float-sum: ai, caught 2/2" in out
    assert "    run 1: app/calc.py:3 [logic] Summing floats loses cents." in out
