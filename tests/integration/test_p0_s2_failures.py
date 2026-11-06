"""P0-S2 AC13 and AC17: these failures exit 14, cleanly, and never as 0 or 1.

P0-S3 AC3 replaced AC14: see `test_p0_s3_exit_codes.py`. P0-S4 AC1 to AC7 replaced AC13 for every
failure it now classifies: see `test_p0_s4_error_mapping.py` and `test_p0_s4_real_sdk.py`. What
stays here is what still exits 14.
"""

import asyncio
import io
import os
import subprocess
import sys

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock
from diff_support import MINIMAL_DIFF
from fake_sdk import FakeSdk
from repo_paths import REPO_ROOT

from review_bot import cli
from review_bot.providers.base import ReviewResponse, TokenUsage

pytestmark = pytest.mark.integration

SENTINEL = "SENTINEL"

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
        ExceptionGroup(SENTINEL, [ValueError(SENTINEL)]),
    ],
    ids=["exception", "value_error", "group"],
)
def test_p0_s2_ac13_raised_exception_exits_14_without_the_text(claude_review, error):
    run = claude_review(FakeSdk(error))

    assert_failed_cleanly(run)


def test_p0_s2_ac13_sdk_log_line_stays_off_stderr_in_a_real_process(tmp_path):
    diff = tmp_path / "change.diff"
    diff.write_text(MINIMAL_DIFF, encoding="utf-8")

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


def test_p0_s2_ac13_exception_after_other_messages_exits_14(claude_review):
    fake = FakeSdk(
        AssistantMessage(content=[TextBlock(text=SENTINEL)], model="m"), RuntimeError(SENTINEL)
    )

    assert_failed_cleanly(claude_review(fake))


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
    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert_failed_cleanly(run)


@pytest.mark.parametrize("function", ["parse_args", "build_provider"])
def test_p0_s2_ac17_exception_from_a_step_of_main_exits_14(bot, write_diff, monkeypatch, function):
    def explode(*args, **kwargs):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(cli, function, explode)

    assert_failed_cleanly(bot.review(write_diff(MINIMAL_DIFF)))


def test_p0_s2_ac17_closed_stream_exits_14_in_process(bot, write_diff, monkeypatch):
    closed = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    closed.close()
    monkeypatch.setattr(sys, "stdout", closed)

    run = bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock())

    assert run.exit_code == 14, run.stderr
    assert "Traceback" not in run.stderr


def test_p0_s2_ac17_control_the_child_really_starts_with_stdout_closed():
    probe = "import sys; sys.exit(3 if sys.stdout is None else 4)"

    completed = run_with_stdout_closed([sys.executable, "-c", probe])

    assert completed.returncode == 3, completed.stderr


# Since P1-S6 the mock's canned finding is dropped, so both write the no-findings line. A closed
# stdout with a finding to print is `closed_with_findings` in `test_p0_s3_stream_failures.py`.
@pytest.mark.parametrize(
    "threshold", ["critical", "low"], ids=["threshold_critical", "threshold_low"]
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
