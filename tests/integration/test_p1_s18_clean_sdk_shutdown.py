"""P1-S18: only the promised stderr from a Claude run, even when a CLI helper holds the pipes open.

Every run is a real child process through the real `query` and transport, with the command replaced
by a fake CLI, so no model or network is involved. The bot's stdout and stderr go to files, not
pipes: the helper may hold any pipe it inherited, and reading one would wait for the helper too.

The child releases an object after `main` returns, so it ends with `code = cli.main(argv)` and then
`sys.exit(code)`. With nothing to release, that is the spec's `sys.exit(cli.main(argv))`.
"""

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from diff_support import FINDING_DIFF, FINDING_LINE, SAMPLE_RULE_LINE
from fake_sdk import HIGH_FINDING, FakeSdk, findings_json, result_message
from p0_s4_support import PREFIX, assert_one_error_line
from real_sdk_support import FINDING_LINE_START
from repo_paths import REPO_ROOT
from stream_support import CLOSE_THEN_RUN

pytestmark = pytest.mark.integration

DEADLINE_TEXT = "didn't finish in time"
CONNECTION_TEXT = "Couldn't start or talk to the Claude Code CLI"
UNRAISABLE_TEXT = "Exception ignored in"
SUMMARY_START = "provider=claude"
HELPER_LIFETIME_S = 20  # The spec allows 30. Teardown stops it sooner.

# Writes nothing to any stream. It ends when the test's teardown asks, or after its lifetime.
HELPER = f"""
import pathlib, sys, time
alive, stop = map(pathlib.Path, sys.argv[1:])
alive.touch()
until = time.monotonic() + {HELPER_LIFETIME_S}
while not stop.exists() and time.monotonic() < until:
    time.sleep(0.05)
alive.unlink()
"""

# Modes starting `hold_` are pipe holders: the helper starts, and is known to be running, before
# anything is sent. `held_back` is a never-ending CLI that hasn't started answering.
FAKE_CLI = """
import json, os, pathlib, subprocess, sys, threading, time

workdir, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
alive = workdir / "helper_alive"
GOOD = GOOD_LITERAL

def send(message):
    print(json.dumps(message), flush=True)

if mode == "held_back":
    # Ends when its stdin closes, so it can't outlive the bot, and within 45 s whatever happens.
    threading.Thread(target=lambda: (sys.stdin.read(), os._exit(0)), daemon=True).start()
    time.sleep(20)
    (workdir / "marker").write_text("ran", encoding="utf-8")
    time.sleep(25)
    sys.exit(0)

if mode.startswith("hold_"):
    # Descriptors, not None: Windows passes the standard handles on only when they are given.
    subprocess.Popen(
        [sys.executable, "-c", HELPER_LITERAL, str(alive), str(workdir / "helper_stop")],
        stdin=0, stdout=1, stderr=2,
    )
    until = time.monotonic() + 10
    while not alive.exists() and time.monotonic() < until:
        time.sleep(0.01)

request = json.loads(sys.stdin.readline())
send({"type": "control_response", "response": {
    "subtype": "success", "request_id": request["request_id"], "response": {},
}})
sys.stdin.readline()
if mode == "hold_no_result":
    sys.exit(0)
if mode == "hold_signal_then_result":
    message = {"model": "m", "content": [{"type": "text", "text": "SENTINEL slow down"}]}
    send({"type": "assistant", "error": "rate_limit", "message": message, "session_id": "s"})
send({
    "type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1,
    "num_turns": 1, "session_id": "s", "usage": {"input_tokens": 2, "output_tokens": 7},
    "is_error": False, "result": GOOD,
})
sys.exit(0)
""".replace("GOOD_LITERAL", repr(findings_json(HIGH_FINDING))).replace(
    "HELPER_LITERAL", repr(HELPER)
)

# `extra` adds what a criterion needs: an object whose `__del__` raises, released during the review
# or after `main` returns, or a stdout whose `write` raises after the review.
BOT_CHILD = """
import io, sys
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport as Transport
from review_bot import cli

fake_cli, workdir, mode, extra, *argv = sys.argv[1:]

class Bomb:
    def __del__(self):
        raise RuntimeError("SENTINEL")

class RaisingStdout(io.TextIOBase):
    def writable(self):
        return True

    def write(self, text):
        raise RuntimeError("SENTINEL")

held = [Bomb()] if extra.startswith("release_") else []

def build_command(self):
    if extra == "release_during_review":
        held.clear()
    return [sys.executable, fake_cli, workdir, mode]

getattr(Transport, "_build_command")
Transport._build_command = build_command
Transport._find_cli = lambda self: sys.executable
if extra == "stdout_raises":
    sys.stdout = RaisingStdout()
code = cli.main(argv)
held.clear()
sys.exit(code)
"""


@dataclass(frozen=True, slots=True)
class Run:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class FakeCli:
    path: Path
    workdir: Path

    @property
    def helper_alive(self) -> Path:
        return self.workdir / "helper_alive"

    @property
    def marker(self) -> Path:
        return self.workdir / "marker"


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """The fake CLI. Afterwards, stop any helper it started and require that it has ended."""
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    path = tmp_path / "fake_cli.py"
    path.write_text(FAKE_CLI, encoding="utf-8")
    cli = FakeCli(path, tmp_path)
    yield cli
    (tmp_path / "helper_stop").touch()
    until = time.monotonic() + 10
    while cli.helper_alive.exists() and time.monotonic() < until:
        time.sleep(0.05)
    assert not cli.helper_alive.exists(), "a helper outlived its test"


@pytest.fixture
def run_bot(fake_cli, tmp_path):
    diff = tmp_path / "change.diff"
    diff.write_text(FINDING_DIFF, encoding="utf-8")

    def run(mode, *flags, extra="none", close_stdout=False, review=None):
        review = review or (str(diff), "--provider", "claude")
        command = [sys.executable, "-c", BOT_CHILD, str(fake_cli.path), str(tmp_path), mode, extra]
        command += [*review, *flags]
        if close_stdout:
            command = [sys.executable, "-c", CLOSE_THEN_RUN, "1", *command]
        out, err = tmp_path / "bot_stdout.txt", tmp_path / "bot_stderr.txt"
        with out.open("wb") as stdout, err.open("wb") as stderr:
            completed = subprocess.run(  # noqa: S603 - fixed arguments, our own interpreter
                command,
                cwd=REPO_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                timeout=90,
                check=False,
            )
        return Run(completed.returncode, read_text(out), read_text(err))

    return run


def read_text(path):
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def assert_clean_deadline_failure(run):
    assert run.exit_code == 11, run.stderr
    assert run.stdout == ""
    assert_one_error_line(run.stderr)
    assert DEADLINE_TEXT in run.stderr
    assert CONNECTION_TEXT not in run.stderr
    assert UNRAISABLE_TEXT not in run.stderr


def assert_one_summary_line(stderr):
    lines = stderr.splitlines()
    assert len(lines) == 1, repr(stderr)
    assert lines[0].startswith(SUMMARY_START), repr(stderr)


def assert_internal_error_line(run):
    assert run.exit_code == 14, run.stderr
    assert run.stdout == ""
    assert_one_error_line(run.stderr)
    assert "SENTINEL" not in run.stderr
    assert UNRAISABLE_TEXT not in run.stderr


# --- AC1, AC2, AC6: a pipe holder ----------------------------------------------------------------


def test_p1_s18_ac1_pipe_holder_without_a_result_hits_the_deadline_with_one_line(run_bot, fake_cli):
    run = run_bot("hold_no_result", "--deadline-s", "3")

    assert fake_cli.helper_alive.exists(), "the helper wasn't holding the pipes when the run ended"
    assert_clean_deadline_failure(run)


def test_p1_s18_ac2_pipe_holder_delivering_a_finding_gives_one_summary_line(run_bot):
    run = run_bot("hold_result")

    assert run.exit_code == 1, run.stderr
    assert run.stdout.startswith(FINDING_LINE_START)
    assert_one_summary_line(run.stderr)


def test_p1_s18_ac6_pipe_holder_signal_then_success_gives_the_warning_and_the_summary(run_bot):
    run = run_bot("hold_signal_then_result")

    assert run.exit_code == 1, run.stderr
    assert run.stdout.startswith(FINDING_LINE_START)
    lines = run.stderr.splitlines()
    assert len(lines) == 2, repr(run.stderr)
    assert sum("rate_limit" in line for line in lines) == 1, repr(run.stderr)
    assert sum(line.startswith(SUMMARY_START) for line in lines) == 1, repr(run.stderr)
    assert "SENTINEL" not in run.stderr
    assert PREFIX not in run.stderr


# --- AC3 and AC4: an object whose `__del__` raises ---------------------------------------------


def test_p1_s18_ac3_unraisable_after_main_is_discarded(run_bot):
    run = run_bot("result", extra="release_after_main")

    assert run.exit_code == 1, run.stderr
    assert run.stdout.startswith(FINDING_LINE_START)
    assert UNRAISABLE_TEXT not in run.stderr
    assert "SENTINEL" not in run.stderr


def test_p1_s18_ac4_unraisable_during_a_claude_review_still_reaches_stderr(run_bot):
    run = run_bot("result", extra="release_during_review")

    assert run.exit_code == 1, run.stderr
    assert run.stdout.startswith(FINDING_LINE_START)
    assert UNRAISABLE_TEXT in run.stderr
    assert "SENTINEL" in run.stderr


def test_p1_s18_ac4_unraisable_after_a_mock_run_still_reaches_stderr(run_bot):
    sample = str(REPO_ROOT / "Assignment" / "sample_diff.txt")

    run = run_bot("result", extra="release_after_main", review=(sample, "--provider", "mock"))

    # The mock's canned finding is dropped since P1-S6 (AC11), but the sample's swallowed exception
    # is a rule finding since P1-S7 (AC1), so the review exits 1.
    assert run.exit_code == 1, run.stderr
    assert run.stdout.strip()
    assert UNRAISABLE_TEXT in run.stderr
    assert "SENTINEL" in run.stderr


@pytest.mark.parametrize("route", ["injected_mock", "injected_claude", "mock_flag"])
def test_p1_s18_ac4_main_leaves_the_unraisable_hook_unless_it_built_claude(
    bot, claude_review, sample_diff, route
):
    hook = sys.unraisablehook

    if route == "injected_mock":
        run = bot.review(sample_diff, provider=bot.mock())
    elif route == "injected_claude":
        run = claude_review(FakeSdk(result_message(findings_json(HIGH_FINDING))))
    else:
        run = bot.review(sample_diff)

    # Only Claude's finding is anchored: the mock's canned one is dropped since P1-S6 (AC11). The
    # mock routes still exit 1, on the sample's swallowed exception, a rule finding since P1-S7.
    expected = FINDING_LINE if route == "injected_claude" else SAMPLE_RULE_LINE
    assert run.exit_code == 1, run.stderr
    assert run.stdout == expected + "\n"
    assert sys.unraisablehook is hook


# --- AC5: failures after the review still give one line ------------------------------------------


@pytest.mark.parametrize(
    ("extra", "close_stdout"),
    [("none", True), ("stdout_raises", False)],
    ids=["stdout_closed", "runtime_error_after_the_review"],
)
def test_p1_s18_ac5_failure_after_a_pipe_holders_review_exits_14_with_one_line(
    run_bot, extra, close_stdout
):
    run = run_bot("hold_result", extra=extra, close_stdout=close_stdout)

    assert_internal_error_line(run)


# --- AC7: a CLI that hasn't started answering by a 1 s deadline ----------------------------------


def test_p1_s18_ac7_held_back_cli_hits_the_deadline_not_a_connection_failure(run_bot, fake_cli):
    run = run_bot("held_back", "--deadline-s", "1")

    assert not fake_cli.marker.exists(), "the CLI wasn't held back"
    assert_clean_deadline_failure(run)
