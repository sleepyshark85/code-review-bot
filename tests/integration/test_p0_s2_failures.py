"""P0-S2 AC13, AC14 and AC17: every failure exits 14, cleanly, and never as 0 or 1."""

import asyncio
import io
import os
import subprocess
import sys

import pytest
from claude_agent_sdk import AssistantMessage, CLIConnectionError, ProcessError, TextBlock
from fake_sdk import HIGH_FINDING, FakeSdk, findings_json, result_message
from repo_paths import REPO_ROOT

from review_bot import cli
from review_bot.providers.base import ReviewResponse, TokenUsage

pytestmark = pytest.mark.integration

SENTINEL = "SENTINEL"
PARSEABLE_WITH_SENTINEL = findings_json(HIGH_FINDING | {"rationale": SENTINEL})

# Python and the CRT treat fd 1 closed by a parent like `>&-`, and `os.close` also clears the
# Windows standard handle. `cmd` has no such redirection, and this works on both platforms.
CLOSE_STDOUT_THEN_RUN = (
    "import os, subprocess, sys; os.close(1); "
    "sys.exit(subprocess.call(sys.argv[1:], close_fds=False))"
)
BOT_COMMAND = [sys.executable, "-m", "review_bot", "Assignment/sample_diff.txt", "--provider"]

# Only visible in a child process: under pytest, its own handlers keep `logging.lastResort` quiet.
LOGGING_PROVIDER_CHILD = """
import logging, sys
from review_bot import cli

class Provider:
    async def review(self, request):
        logging.getLogger("claude_agent_sdk._internal.query").error("SENTINEL")
        raise RuntimeError("boom")

sys.exit(cli.main(sys.argv[1:], provider=Provider()))
"""

# The real default `sdk_call`, `query` and transport, against a real child process. The command is
# replaced rather than set through `cli_path` because the SDK refuses a batch file on Windows, so
# a script couldn't be used there. `getattr` first: a plain assignment to a renamed method would
# silently leave the real CLI in place.
REAL_QUERY_CHILD = """
import sys
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport as Transport
from review_bot import cli

fake_cli, marker, mode, diff = sys.argv[1:]
getattr(Transport, "_build_command")
Transport._build_command = lambda self: [sys.executable, fake_cli, marker, mode]
Transport._find_cli = lambda self: sys.executable
sys.exit(cli.main([diff, "--provider", "claude"]))
"""

# Reads the SDK's first request before acting, so its write can't race the exit.
FAKE_CLI = """
import pathlib, sys

marker, mode = sys.argv[1:]
sys.stdin.readline()
pathlib.Path(marker).write_text("ran", encoding="utf-8")
sys.stderr.write("SENTINEL from the CLI\\n")
if mode == "malformed_json":
    print('{"type": SENTINEL is not JSON', flush=True)
    sys.exit(0)
sys.exit(3)
"""


def assert_failed_cleanly(run):
    assert run.exit_code == 14, run.stderr
    assert run.stdout == ""
    assert run.stderr.strip(), "a failure must say something"
    assert "Traceback" not in run.stderr
    assert SENTINEL not in run.stderr


def run_with_stdout_closed(command):
    return subprocess.run(  # noqa: S603 - fixed arguments, our own interpreter
        [sys.executable, "-c", CLOSE_STDOUT_THEN_RUN, *command],
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


class RaisingProvider:
    def __init__(self, error):
        self.error = error

    async def review(self, request):
        raise self.error


class ReturningProvider:
    def __init__(self, response):
        self.response = response

    async def review(self, request):
        return self.response


@pytest.mark.parametrize(
    "error",
    [
        Exception(SENTINEL),
        ValueError(SENTINEL),
        ProcessError(SENTINEL, exit_code=1, stderr=SENTINEL),
        CLIConnectionError(SENTINEL),
        ExceptionGroup(SENTINEL, [ValueError(SENTINEL)]),
    ],
    ids=["exception", "value_error", "process_error", "connection_error", "group"],
)
def test_p0_s2_ac13_raised_exception_exits_14_without_the_text(claude_review, error):
    run = claude_review(FakeSdk(error))

    assert_failed_cleanly(run)


def test_p0_s2_ac13_sdk_log_line_stays_off_stderr_in_a_real_process(tmp_path):
    diff = tmp_path / "change.diff"
    diff.write_text("+x\n", encoding="utf-8")

    completed = subprocess.run(  # noqa: S603 - fixed arguments, our own interpreter
        [sys.executable, "-c", LOGGING_PROVIDER_CHILD, str(diff), "--provider", "claude"],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )

    assert completed.returncode == 14, completed.stderr
    assert completed.stdout == ""
    assert SENTINEL not in completed.stderr
    assert len(completed.stderr.splitlines()) == 1, completed.stderr


@pytest.mark.parametrize("mode", ["malformed_json", "non_zero_exit"])
def test_p0_s2_ac13_real_query_failure_leaves_only_our_one_line(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    fake_cli = tmp_path / "fake_cli.py"
    fake_cli.write_text(FAKE_CLI, encoding="utf-8")
    marker = tmp_path / "fake_cli_ran"
    diff = tmp_path / "change.diff"
    diff.write_text("+x\n", encoding="utf-8")

    completed = subprocess.run(  # noqa: S603 - fixed arguments, our own interpreter
        [sys.executable, "-c", REAL_QUERY_CHILD, str(fake_cli), str(marker), mode, str(diff)],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )

    assert marker.exists(), completed.stderr
    assert completed.returncode == 14, completed.stderr
    assert completed.stdout == ""
    assert SENTINEL not in completed.stderr
    assert len(completed.stderr.splitlines()) == 1, completed.stderr


def test_p0_s2_ac13_exception_after_other_messages_exits_14(claude_review):
    fake = FakeSdk(
        AssistantMessage(content=[TextBlock(text=SENTINEL)], model="m"), RuntimeError(SENTINEL)
    )

    assert_failed_cleanly(claude_review(fake))


@pytest.mark.parametrize(
    ("subtype", "result"),
    [
        ("success", PARSEABLE_WITH_SENTINEL),
        ("success", f"{SENTINEL} API error"),
        ("error_during_execution", PARSEABLE_WITH_SENTINEL),
        ("error_max_turns", f"{SENTINEL} API error"),
    ],
    ids=["success_parseable", "success_prose", "error_parseable", "error_prose"],
)
def test_p0_s2_ac13_error_result_exits_14_without_the_text(claude_review, subtype, result):
    fake = FakeSdk(result_message(result, is_error=True, subtype=subtype))

    assert_failed_cleanly(claude_review(fake))


def test_p0_s2_ac13_error_result_with_empty_findings_is_not_a_clean_review(claude_review):
    fake = FakeSdk(result_message(findings_json(), is_error=True))

    assert_failed_cleanly(claude_review(fake))


def test_p0_s2_ac13_error_result_with_no_result_text_exits_14(claude_review):
    fake = FakeSdk(result_message(None, is_error=True, errors=[SENTINEL]))

    assert_failed_cleanly(claude_review(fake))


@pytest.mark.parametrize(
    "script",
    [
        (),
        (AssistantMessage(content=[TextBlock(text=PARSEABLE_WITH_SENTINEL)], model="m"),),
    ],
    ids=["empty_stream", "valid_json_only_in_an_assistant_message"],
)
def test_p0_s2_ac13_stream_with_no_result_message_exits_14(claude_review, script):
    assert_failed_cleanly(claude_review(FakeSdk(*script)))


@pytest.mark.parametrize(
    "result",
    [
        f"{SENTINEL} not json",
        "",
        None,
        f"```json\n{findings_json()}\n```",
        f"Here you go: {findings_json()}",
        f'{{"findings": [{{"file": "{SENTINEL}"}}]}}',
        "[]",
    ],
    ids=["prose", "empty", "none", "fenced", "preamble", "fails_the_schema", "wrong_shape"],
)
def test_p0_s2_ac14_result_that_is_not_valid_findings_exits_14(claude_review, result):
    run = claude_review(FakeSdk(result_message(result)))

    assert_failed_cleanly(run)


@pytest.mark.parametrize(
    "provider",
    [
        RaisingProvider(RuntimeError(SENTINEL)),
        RaisingProvider(asyncio.CancelledError(SENTINEL)),
        ReturningProvider(None),
        ReturningProvider(ReviewResponse(findings=(SENTINEL,), usage=TokenUsage(0, 0))),
    ],
    ids=["raises", "raises_cancelled", "returns_none", "returns_a_non_finding"],
)
def test_p0_s2_ac17_misbehaving_provider_exits_14(bot, write_diff, provider):
    run = bot.review(write_diff("+x\n"), provider=provider)

    assert_failed_cleanly(run)


@pytest.mark.parametrize("function", ["parse_args", "build_provider"])
def test_p0_s2_ac17_exception_from_a_step_of_main_exits_14(bot, write_diff, monkeypatch, function):
    def explode(*args, **kwargs):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(cli, function, explode)

    assert_failed_cleanly(bot.review(write_diff("+x\n")))


def test_p0_s2_ac17_closed_stream_exits_14_in_process(bot, write_diff, monkeypatch):
    closed = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    closed.close()
    monkeypatch.setattr(sys, "stdout", closed)

    run = bot.review(write_diff("+x\n"), provider=bot.mock())

    assert run.exit_code == 14, run.stderr
    assert "Traceback" not in run.stderr


def test_p0_s2_ac17_control_the_child_really_starts_with_stdout_closed():
    probe = "import sys; sys.exit(3 if sys.stdout is None else 4)"

    completed = run_with_stdout_closed([sys.executable, "-c", probe])

    assert completed.returncode == 3, completed.stderr


@pytest.mark.parametrize(
    "threshold", ["critical", "low"], ids=["would_exit_0_otherwise", "would_exit_1_otherwise"]
)
def test_p0_s2_ac17_stdout_closed_exits_14(threshold):
    completed = run_with_stdout_closed([*BOT_COMMAND, "mock", "--threshold", threshold])

    assert completed.returncode == 14, completed.stderr
    assert "Traceback" not in completed.stderr
    assert "Exception ignored" not in completed.stderr


def test_p0_s2_ac17_stdout_reader_gone_exits_14():
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed arguments, our own interpreter
            [*BOT_COMMAND, "mock", "--threshold", "critical"],
            cwd=REPO_ROOT,
            stdout=write_end,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
    finally:
        os.close(write_end)

    assert completed.returncode == 14, completed.stderr
    assert "Traceback" not in completed.stderr
    assert "Exception ignored" not in completed.stderr
