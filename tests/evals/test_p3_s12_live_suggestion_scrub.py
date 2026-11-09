"""P3-S12 AC11: a live recording scrubs a finding's `suggestion` as it does its `rationale`.

Written from the amended spec's AC11 before its code was read. The seam is P1-S16's: `python -m
review_bot.evals live` called in process through `main(..., sdk_call=ScriptedSdk(...))`, over a
copy of the committed `sample` case, so no model or network is reached. The scripted answer's
finding carries a `suggestion` holding an AWS-format key.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from test_p1_s16_eval_live_command import (
    AWS_KEY,
    CASE_FILES,
    SAMPLE,
    Live,
    ScriptedSdk,
    always,
    answer,
    copy_cases,
    finding,
    flags,
    live_path,
    read_recording,
    run_live,
    work_dir,
)

from review_bot.secret_scan import scan_secrets

__all__ = ["run_live", "work_dir"]  # P1-S16's fixtures, used here by name

REDACTED = "[REDACTED]"
SUGGESTION_PREFIX = "        key = "


def leaky_finding() -> dict[str, object]:
    return finding(
        CASE_FILES[SAMPLE],
        34,
        "Float arithmetic on money loses cents.",
        suggestion=f"{SUGGESTION_PREFIX}'{AWS_KEY}'",
    )


@pytest.fixture
def recorded(run_live: Callable[..., Live], tmp_path: Path) -> tuple[Live, Path]:
    root = copy_cases(tmp_path, SAMPLE)
    sdk = ScriptedSdk(always(answer(leaky_finding())))

    live = run_live(*flags(root, runs=1, max_calls=1), sdk=sdk)

    return live, root


def test_p3_s12_ac11_recording_is_written_not_refused(recorded):
    live, root = recorded

    assert live_path(root, SAMPLE).is_file(), live.output
    assert "wasn't written" not in live.output, live.output


def test_p3_s12_ac11_key_in_a_suggestion_is_written_redacted(recorded):
    _, root = recorded

    text = live_path(root, SAMPLE).read_text(encoding="utf-8")
    assert AWS_KEY not in text
    assert scan_secrets(text) == ()
    (recorded_finding,) = read_recording(root, SAMPLE)["runs"][0]["findings"]
    assert recorded_finding["suggestion"] == f"{SUGGESTION_PREFIX}'{REDACTED}'"


def test_p3_s12_ac11_key_reaches_no_command_output(recorded):
    live, _ = recorded

    assert AWS_KEY not in live.output
