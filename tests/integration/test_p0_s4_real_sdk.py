"""P0-S4 through the real `query` and transport, against a fake CLI that is a real child process.

Fakes at the `sdk_call` seam can't show what the SDK itself does: it logs at error level, turns a
non-zero exit into a `ResultError`, and keeps the subprocess until `asyncio.run` shuts down. No
model or network is involved, because the command is replaced. No test here selects a provider:
the default from AC13 is what reaches Claude.

Also here: AC6's note on losing stderr, in a real process, the only place exit 120 shows.
"""

import subprocess
import sys
import time

import pytest
from diff_support import FINDING_DIFF
from fake_sdk import HIGH_FINDING, findings_json
from p0_s4_support import PREFIX
from real_sdk_support import FAKE_CLI, FINDING_LINE_START, assert_failed_cleanly
from stream_support import (
    CLOSED,
    DEV_FULL,
    PIPE,
    READER_GONE,
    needs_dev_full,
    run_process,
)

pytestmark = pytest.mark.integration

API_KEY = "test-key-sentinel"

# `getattr` first: assigning to a renamed method would silently leave the real CLI in place.
REAL_QUERY_CHILD = """
import sys
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport as Transport
from review_bot import cli

fake_cli, marker, mode, diff, *flags = sys.argv[1:]
getattr(Transport, "_build_command")
if mode == "cli_missing":
    Transport._build_command = lambda self: [fake_cli]
else:
    Transport._build_command = lambda self: [sys.executable, fake_cli, marker, mode]
Transport._find_cli = lambda self: sys.executable
sys.exit(cli.main([diff, *flags]))
"""

# A fake `sdk_call` in a real process: the review, its warning and the exit code, with stderr lost.
OVERRIDDEN_SIGNAL_CHILD = """
import sys
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from review_bot import cli
from review_bot.providers.claude_agent import ClaudeAgentProvider

raw, argv = sys.argv[1], sys.argv[2:]

async def sdk_call(prompt, options):
    yield AssistantMessage(content=[TextBlock(text="SENTINEL")], model="m", error="rate_limit")
    yield ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
        session_id="s", result=raw,
    )

_, settings = cli.parse_args(argv)
sys.exit(cli.main(argv, provider=ClaudeAgentProvider(settings, sdk_call=sdk_call)))
"""


@pytest.fixture
def run_real_query(tmp_path, monkeypatch):
    def run(mode, *flags, api_key=None):
        monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
        if api_key is not None:
            monkeypatch.setenv("ANTHROPIC_API_KEY", api_key)
        fake_cli = tmp_path / "fake_cli.py"
        fake_cli.write_text(FAKE_CLI, encoding="utf-8")
        marker = tmp_path / "fake_cli_ran"
        diff = tmp_path / "change.diff"
        diff.write_text(FINDING_DIFF, encoding="utf-8")
        target = tmp_path / "no_such_claude.exe" if mode == "cli_missing" else fake_cli
        completed = subprocess.run(  # noqa: S603 - fixed arguments, our own interpreter
            [
                sys.executable,
                "-c",
                REAL_QUERY_CHILD,
                str(target),
                str(marker),
                mode,
                str(diff),
                *flags,
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=90,
            check=False,
        )
        # P1-S18 AC7: by a 1 s deadline, a CLI still starting and a hung one look the same to the
        # bot, so `never_ends` has no marker check. `success_then_never_ends` proves one runs.
        if mode not in {"cli_missing", "never_ends"}:
            assert marker.exists(), completed.stderr
        return completed

    return run


def test_p0_s4_ac13_ac5_live_401_sequence_exits_8_with_one_line(run_real_query):
    completed = run_real_query("live_401")

    assert_failed_cleanly(completed, 8)
    assert "ANTHROPIC_API_KEY" in completed.stderr


def test_p0_s4_ac5_result_401_then_the_clis_exit_exits_8_with_one_line(run_real_query):
    assert_failed_cleanly(run_real_query("result_401_then_crash"), 8)


@pytest.mark.parametrize(
    "mode", ["crash_on_the_first_request", "crash_after_the_prompt"], ids=["early", "late"]
)
def test_p0_s4_ac1_cli_crash_exits_12_with_one_line(run_real_query, mode):
    # Replaces P0-S2 AC13's expectation of 14 for a non-zero exit. The SDK logs this one at error
    # level, which a fake at the `sdk_call` seam never shows.
    assert_failed_cleanly(run_real_query(mode), 12)


@pytest.mark.parametrize(
    "mode", ["malformed_json", "malformed_after_the_prompt"], ids=["early", "late"]
)
def test_p0_s4_ac12_malformed_output_exits_12_without_its_text(run_real_query, mode):
    # Replaces P0-S2 AC13's expectation of 14. The text is in `CLIJSONDecodeError` and in the SDK's
    # own log line.
    assert_failed_cleanly(run_real_query(mode), 12)


def test_p0_s4_ac9_missing_cli_exits_7_with_one_line(run_real_query):
    completed = run_real_query("cli_missing")

    assert_failed_cleanly(completed, 7)
    assert "install" in completed.stderr
    assert "PATH" in completed.stderr


def test_p0_s4_ac11_key_written_by_the_cli_reaches_neither_stream(run_real_query):
    completed = run_real_query("crash_after_the_prompt", api_key=API_KEY)

    assert completed.returncode == 12, completed.stderr
    assert API_KEY not in completed.stdout
    assert API_KEY not in completed.stderr


def test_p0_s4_ac6_success_then_the_clis_exit_still_delivers(run_real_query):
    completed = run_real_query("success_then_crash")

    assert completed.returncode == 1, completed.stderr
    assert completed.stdout.startswith(FINDING_LINE_START)
    assert PREFIX not in completed.stderr
    assert "Fatal error" not in completed.stderr
    assert len(completed.stderr.splitlines()) == 1, completed.stderr
    assert "provider=claude" in completed.stderr


def test_p0_s4_ac6_signal_then_success_warns_and_delivers(run_real_query):
    completed = run_real_query("signal_then_success")

    assert completed.returncode == 1, completed.stderr
    assert completed.stdout.startswith(FINDING_LINE_START)
    assert "rate_limit" in completed.stderr
    assert "SENTINEL" not in completed.stderr
    assert PREFIX not in completed.stderr


def test_p0_s4_ac6_prose_alone_exits_13_with_one_line(run_real_query):
    assert_failed_cleanly(run_real_query("prose"), 13)


def test_p0_s4_ac6_success_from_a_cli_that_never_exits_is_delivered_before_the_deadline(
    run_real_query,
):
    # The 5 seconds in AC6 hold for the fake only: the SDK's own close of a child ignoring EOF adds
    # about 5 s (design.md section 4). What must hold here is that the deadline isn't waited out.
    started = time.monotonic()

    completed = run_real_query("success_then_never_ends", "--deadline-s", "30")

    elapsed = time.monotonic() - started
    assert completed.returncode == 1, completed.stderr
    assert completed.stdout.startswith(FINDING_LINE_START)
    assert elapsed < 30, f"took {elapsed:.1f} s, so the review waited for the deadline"


def test_p0_s4_ac8_cli_that_never_exits_hits_the_deadline_with_one_line(run_real_query):
    completed = run_real_query("never_ends", "--deadline-s", "1")

    assert_failed_cleanly(completed, 11)
    assert "didn't finish in time" in completed.stderr
    assert "Couldn't start or talk to the Claude Code CLI" not in completed.stderr


# --- AC6's note: stderr closed or failing, in a real process -------------------------------------


# The sample diff is reviewed here, so the finding sits on one of its added lines (P1-S6).
SAMPLE_FINDING = HIGH_FINDING | {"file": "billing/invoice.py", "line": 14}


def overridden_signal_command(result, *flags):
    return [
        sys.executable,
        "-c",
        OVERRIDDEN_SIGNAL_CHILD,
        result,
        "Assignment/sample_diff.txt",
        "--provider",
        "claude",
        *flags,
    ]


@pytest.mark.parametrize(
    ("result", "code"),
    [(findings_json(SAMPLE_FINDING), 1), (findings_json(), 0)],
    ids=["found", "none"],
)
def test_p0_s4_ac6_note_control_the_child_warns_when_stderr_works(result, code):
    completed = run_process(overridden_signal_command(result), stderr=PIPE)

    assert completed.returncode == code, completed.stderr
    assert "rate_limit" in completed.stderr
    assert "SENTINEL" not in completed.stderr


@pytest.mark.parametrize(
    "kind", [CLOSED, READER_GONE, pytest.param(DEV_FULL, marks=needs_dev_full)]
)
@pytest.mark.parametrize(
    ("result", "code"),
    [(findings_json(SAMPLE_FINDING), 1), (findings_json(), 0)],
    ids=["found", "none"],
)
def test_p0_s4_ac6_note_warning_with_stderr_lost_exits_by_findings_in_a_real_process(
    kind, result, code
):
    completed = run_process(overridden_signal_command(result), stderr=kind)

    assert completed.returncode == code, "not 14 or 120: losing the warning changes nothing"
    assert ("billing/invoice.py:14" in completed.stdout) == (code == 1)
