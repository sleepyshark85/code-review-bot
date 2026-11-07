"""P1-S17: hold CI to the live measurement, offline.

Written from the spec's AC4, AC5 and AC7. Every run goes through `review_bot.evals.main` in
process, over the repository's committed cases or a temporary copy of them; nothing calls a model.

Report reading, from P1-S14's format: each case starts with a `case <id>: live recording of
<model>, ...` line, and every threshold the case misses is a `  FAIL: <reason>` line under it. A
bug below its `min_runs_caught` reads `FAIL: ai bug <id> was caught in N of 5 runs; ...`. A
missing, stale or other-model recording, or a case below `min_passing_runs`, has a FAIL line of
its own, so it can't read as a known miss.
"""

import json
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from review_bot.evals import freshness_fields, main
from review_bot.settings import Settings

pytestmark = pytest.mark.integration

CASES = Path(__file__).resolve().parent / "cases"
CASE_IDS = sorted(path.parent.name for path in CASES.glob("*/case.json"))
DEFAULT_MODEL = Settings.model_fields["model"].default
CREDENTIAL_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")

# Misses the user accepted, as (case, bug). The user decided on 2026-11-07 that `double-tax` stays
# a documented miss with no threshold change (P1-S17 AC3); see the P1-S17 section of
# docs/phases/phase-1/test-report.md. If the bug starts passing, remove it from this set.
KNOWN_MISSES = {("sample", "double-tax")}

_CASE_HEADER = re.compile(r"^case (?P<case>\S+): (?P<rest>.*)$")
_BUG_MISS = re.compile(r"^ai bug (?P<bug>\S+) was caught in \d+ of \d+ runs; it needs \d+$")


@dataclass(frozen=True, slots=True)
class Replay:
    exit_code: int
    output: str

    def headers(self) -> dict[str, str]:
        """Each case's header text after `case <id>: `."""
        return {
            match["case"]: match["rest"]
            for line in self.output.splitlines()
            if (match := _CASE_HEADER.match(line))
        }

    def failures(self) -> set[tuple[str, str]]:
        """Each FAIL line as (case, bug) for a bug below its threshold, else (case, the text)."""
        found: set[tuple[str, str]] = set()
        case = "<before any case>"
        for line in self.output.splitlines():
            if header := _CASE_HEADER.match(line):
                case = header["case"]
            elif line.strip().startswith("FAIL:"):
                reason = line.strip().removeprefix("FAIL:").strip()
                miss = _BUG_MISS.match(reason)
                found.add((case, miss["bug"] if miss else reason))
        return found


@pytest.fixture
def replay(capsys: pytest.CaptureFixture[str]) -> Callable[..., Replay]:
    def run(*args: str | Path) -> Replay:
        capsys.readouterr()
        exit_code = main(["replay", *map(str, args)])
        captured = capsys.readouterr()
        return Replay(exit_code, captured.out + captured.err)

    return run


def live_recording(case_id: str, cases: Path = CASES) -> dict[str, Any]:
    path = cases / case_id / "recordings" / "live" / "recording.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def assert_committed_recordings_hold(replay: Callable[..., Replay]) -> None:
    """AC4's check: a fresh live recording of the default model per case, missing only as known."""
    result = replay("--cases", CASES, "--require-live")

    assert set(result.headers()) == set(CASE_IDS), result.output
    for case_id, header in result.headers().items():
        assert header.startswith(f"live recording of {DEFAULT_MODEL},"), (case_id, header)
    assert result.failures() == KNOWN_MISSES, (
        "Only the documented misses may fail. If one now passes, update KNOWN_MISSES and the "
        f"test report:\n{result.output}"
    )
    assert result.exit_code == 1, result.output


# --- AC4: CI replays the committed live recordings --------------------------------------------


def test_p1_s17_ac4_every_case_has_a_committed_live_recording() -> None:
    assert CASE_IDS == [
        "clean",
        "injection-canary",
        "injection-delimiter",
        "injection-instruction",
        "sample",
    ]
    for case_id in CASE_IDS:
        data = live_recording(case_id)
        assert (data["case_id"], data["kind"]) == (case_id, "live")


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_p1_s17_ac4_committed_live_recording_is_fresh_and_of_the_default_model(
    case_id: str,
) -> None:
    data = live_recording(case_id)
    today = freshness_fields(CASES / case_id / "case.json")

    stale = {field: value for field, value in today.items() if data[field] != value}

    assert stale == {}, f"{case_id}'s live recording is stale; record it again: {stale}"
    assert data["model"] == DEFAULT_MODEL


def test_p1_s17_ac4_require_live_fails_only_on_the_documented_misses(
    replay: Callable[..., Replay],
) -> None:
    assert_committed_recordings_hold(replay)


def committed_copy(tmp_path: Path) -> Path:
    root = tmp_path / "cases"
    shutil.copytree(CASES, root)
    return root


def rewrite_live(root: Path, case_id: str, **fields: Any) -> None:
    path = root / case_id / "recordings" / "live" / "recording.json"
    data = json.loads(path.read_text(encoding="utf-8")) | fields
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_p1_s17_ac4_a_missing_live_recording_is_not_a_known_miss(
    replay: Callable[..., Replay], tmp_path: Path
) -> None:
    root = committed_copy(tmp_path)
    (root / "clean" / "recordings" / "live" / "recording.json").unlink()

    result = replay("--cases", root, "--require-live")

    assert result.exit_code == 1, result.output
    assert any(case == "clean" for case, _ in result.failures() - KNOWN_MISSES), result.output


def test_p1_s17_ac4_a_stale_live_recording_is_not_a_known_miss(
    replay: Callable[..., Replay], tmp_path: Path
) -> None:
    root = committed_copy(tmp_path)
    rewrite_live(root, "injection-delimiter", case_sha256="0" * 64)

    result = replay("--cases", root, "--require-live")

    assert result.exit_code == 1, result.output
    [(case, reason)] = result.failures() - KNOWN_MISSES
    assert case == "injection-delimiter"
    assert "stale" in reason


def test_p1_s17_ac4_another_models_live_recording_is_not_a_known_miss(
    replay: Callable[..., Replay], tmp_path: Path
) -> None:
    root = committed_copy(tmp_path)
    rewrite_live(root, "injection-canary", model="claude-other-model-9")

    result = replay("--cases", root, "--require-live")

    assert result.exit_code == 1, result.output
    assert any(case == "injection-canary" for case, _ in result.failures() - KNOWN_MISSES), (
        result.output
    )


def test_p1_s17_ac4_a_case_below_its_threshold_is_not_a_known_miss(
    replay: Callable[..., Replay], tmp_path: Path
) -> None:
    """Emptying one run's findings makes `rate-inverted` 4 of 5, below its `min_runs_caught`."""
    root = committed_copy(tmp_path)
    runs = live_recording("injection-delimiter", root)["runs"]
    runs[0]["findings"] = []
    rewrite_live(root, "injection-delimiter", runs=runs)

    result = replay("--cases", root, "--require-live")

    assert result.exit_code == 1, result.output
    assert ("injection-delimiter", "rate-inverted") in result.failures(), result.output


# --- AC5: a synthetic file in recordings/live/ can't satisfy AC4 -------------------------------
# P1-S14's `test_p1_s14_ac9_synthetic_file_under_live_exits_2_naming_it` covers this on the
# sample alone; this repeats it on a copy of the committed cases, with `--require-live`.


def test_p1_s17_ac5_synthetic_kind_under_live_exits_2_naming_it(
    replay: Callable[..., Replay], tmp_path: Path
) -> None:
    root = committed_copy(tmp_path)
    rewrite_live(root, "clean", kind="synthetic")

    result = replay("--cases", root, "--require-live")

    assert result.exit_code == 2, result.output
    assert "recording.json" in result.output
    assert "synthetic" in result.output


# --- AC7: offline, with no `claude` and no key ---------------------------------------------------


def test_p1_s17_ac7_ac4_holds_without_claude_on_path_or_a_key(
    replay: Callable[..., Replay],
) -> None:
    assert shutil.which("claude") is None, "tests/conftest.py must take `claude` off PATH"
    assert [name for name in CREDENTIAL_VARIABLES if name in os.environ] == []

    assert_committed_recordings_hold(replay)
