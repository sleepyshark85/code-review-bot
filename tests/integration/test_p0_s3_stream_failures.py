"""P0-S3 AC4 and AC5, in real processes: the exit code survives a broken stdout or stderr.

Only a process exit code shows these failures. CPython retries a failed flush at shutdown and turns
the code into 120, and `capsys` has neither real buffering nor a real file descriptor.
"""

import io
import sys

import pytest
from diff_support import SAMPLE_RULE_LINES
from stream_support import (
    CLOSED,
    DEV_FULL,
    MERGED,
    READER_GONE,
    needs_dev_full,
    run_process,
)

pytestmark = pytest.mark.integration

PREFIX = "review-bot: error:"
SAMPLE_DIFF = "Assignment/sample_diff.txt"
ABSENT_DIFF = "tests/fixtures/p0_s3_no_such_file.diff"
# How INJECTED_CHILD's FINDING renders.
FINDING_LINE = "billing/invoice.py:14 [high/logic] " + "x" * 100 + " (confidence 0.90)"

ANY_BROKEN_STREAM = [CLOSED, READER_GONE, pytest.param(DEV_FULL, marks=needs_dev_full)]

# A provider can only be injected through `main`, so these cases use a small child, not `-m`.
INJECTED_CHILD = """
import sys
from review_bot import cli
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.providers.mock import MockProvider

class Raises:
    def __init__(self, error):
        self.error = error

    async def review(self, request):
        raise self.error

class CustomBaseException(BaseException):
    pass

# Anchored on an added line of the sample diff, so it prints (P1-S6).
FINDING = Finding(
    file="billing/invoice.py", line=14, severity="high", category="logic",
    rationale="x" * 100, confidence=0.9,
)
PROVIDERS = {
    "raises": Raises(RuntimeError("SENTINEL")),
    "system_exit_0": Raises(SystemExit(0)),
    "keyboard_interrupt": Raises(KeyboardInterrupt()),
    "custom_base_exception": Raises(CustomBaseException("MODEL-SENTINEL\\x1b[31m")),
    "base_exception_group": Raises(
        BaseExceptionGroup("MODEL-SENTINEL\\x1b[31m", [CustomBaseException("MODEL-SENTINEL")])
    ),
    "rate_limited": MockProvider(error=ProviderError(ErrorCategory.RATE_LIMIT, "detail")),
    "one_finding": MockProvider(findings=[FINDING]),
    # Distinct rationales, so none is a duplicate of another (P1-S7 AC12).
    "many_findings": MockProvider(
        findings=[
            FINDING.model_copy(update={"rationale": f"{n:04d}" + "x" * 96}) for n in range(5000)
        ]
    ),
}
sys.exit(cli.main(sys.argv[2:], provider=PROVIDERS[sys.argv[1]]))
"""

# What a program taking no care gets: 3, the stream was gone from the start; 120, writing raised
# and it exits 4, but the unflushed text survives to the shutdown flush. That goes for stderr too.
PROBE_STREAM = """
import sys
stream = getattr(sys, sys.argv[1])
if stream is None:
    sys.exit(3)
try:
    stream.write("x\\n")
    stream.flush()
except OSError:
    sys.exit(4)
sys.exit(5)
"""


def bot_command(*flags, diff=SAMPLE_DIFF):
    return [sys.executable, "-m", "review_bot", diff, "--provider", "mock", *flags]


def injected_command(provider):
    return [sys.executable, "-c", INJECTED_CHILD, provider, SAMPLE_DIFF, "--provider", "mock"]


def assert_no_traceback(stderr):
    assert "Traceback" not in stderr
    assert "Exception ignored" not in stderr
    assert "SENTINEL" not in stderr


def assert_only_the_error_line(stderr):
    lines = stderr.splitlines()
    assert len(lines) == 1, repr(stderr)
    assert lines[0].startswith(PREFIX), repr(stderr)


@pytest.mark.parametrize(
    ("stream", "kind", "code"),
    [
        ("stdout", CLOSED, 3),
        ("stderr", CLOSED, 3),
        ("stdout", READER_GONE, 120),
        ("stderr", READER_GONE, 120),
        pytest.param("stdout", DEV_FULL, 120, marks=needs_dev_full),
        pytest.param("stderr", DEV_FULL, 120, marks=needs_dev_full),
    ],
)
def test_p0_s3_ac4_control_each_broken_stream_really_is_broken(stream, kind, code):
    completed = run_process([sys.executable, "-c", PROBE_STREAM, stream], **{stream: kind})

    assert completed.returncode == code


def test_p0_s3_ac4_control_many_findings_outgrow_the_stream_buffers():
    completed = run_process(injected_command("many_findings"))

    assert completed.returncode == 1, completed.stderr
    assert len(completed.stdout) > 10 * io.DEFAULT_BUFFER_SIZE


@pytest.mark.parametrize(
    ("kind", "command"),
    [
        pytest.param(CLOSED, bot_command("--threshold", "critical"), id="closed"),
        pytest.param(CLOSED, injected_command("one_finding"), id="closed_with_findings"),
        pytest.param(READER_GONE, bot_command("--threshold", "critical"), id="reader_gone"),
        pytest.param(
            DEV_FULL, bot_command("--threshold", "critical"), marks=needs_dev_full, id="dev_full"
        ),
        pytest.param(READER_GONE, bot_command("--help"), id="reader_gone_help"),
        pytest.param(READER_GONE, injected_command("many_findings"), id="reader_gone_on_write"),
        pytest.param(
            DEV_FULL,
            injected_command("many_findings"),
            marks=needs_dev_full,
            id="dev_full_on_write",
        ),
    ],
)
def test_p0_s3_ac4_ac5_stdout_failure_exits_14_and_leaves_only_the_error_line(kind, command):
    completed = run_process(command, stdout=kind)

    assert completed.returncode == 14, completed.stderr
    assert_no_traceback(completed.stderr)
    assert_only_the_error_line(completed.stderr)


@pytest.mark.parametrize(
    ("kind", "command", "code"),
    [
        pytest.param(CLOSED, bot_command(diff=ABSENT_DIFF), 3, id="closed_missing_diff_3"),
        pytest.param(
            READER_GONE, bot_command(diff=ABSENT_DIFF), 3, id="reader_gone_missing_diff_3"
        ),
        pytest.param(
            DEV_FULL,
            bot_command(diff=ABSENT_DIFF),
            3,
            marks=needs_dev_full,
            id="dev_full_missing_diff_3",
        ),
        pytest.param(CLOSED, injected_command("rate_limited"), 9, id="closed_provider_error_9"),
        pytest.param(
            READER_GONE, injected_command("rate_limited"), 9, id="reader_gone_provider_error_9"
        ),
        pytest.param(
            DEV_FULL,
            injected_command("rate_limited"),
            9,
            marks=needs_dev_full,
            id="dev_full_provider_error_9",
        ),
    ],
)
def test_p0_s3_ac4_stdout_nothing_writes_to_does_not_change_the_code(kind, command, code):
    completed = run_process(command, stdout=kind)

    assert completed.returncode == code, completed.stderr
    assert_only_the_error_line(completed.stderr)


@pytest.fixture(scope="module")
def provider_raising():
    """A provider raising `RuntimeError("SENTINEL")`. Its stderr is the fixed internal error."""
    return run_process(injected_command("raises"))


def test_p0_s3_ac4_provider_raising_makes_the_process_exit_14(provider_raising):
    assert provider_raising.returncode == 14, provider_raising.stderr
    assert provider_raising.stdout == ""
    assert_no_traceback(provider_raising.stderr)


# `SystemExit(1)` and `SystemExit(text)` are in `test_p0_s3_safe_stderr.py`, in process.
@pytest.mark.parametrize(
    "provider",
    ["system_exit_0", "keyboard_interrupt", "custom_base_exception", "base_exception_group"],
)
def test_p0_s3_ac4_provider_ending_the_process_exits_14_with_the_fixed_line(
    provider, provider_raising
):
    completed = run_process(injected_command(provider))

    assert completed.returncode == 14, completed.stderr
    assert completed.stdout == ""
    assert_only_the_error_line(completed.stderr)
    assert completed.stderr == provider_raising.stderr


def test_p0_s3_ac4_help_still_exits_0():
    completed = run_process(bot_command("--help"))

    assert completed.returncode == 0, completed.stderr
    assert "usage" in completed.stdout.lower()
    assert completed.stderr == ""


STDERR_OUTCOMES = {
    "no_findings_0": (bot_command("--threshold", "critical"), 0),
    "findings_1": (injected_command("one_finding"), 1),
    "missing_diff_3": (bot_command(diff=ABSENT_DIFF), 3),
    "unexpected_14": (injected_command("raises"), 14),
}
# A `None` stderr's other outcomes, including 2 and 9, are in `test_p0_s3_safe_stderr.py`.
STDERR_CASES = [
    *[(CLOSED, name) for name in ("findings_1", "missing_diff_3")],
    *[(READER_GONE, name) for name in STDERR_OUTCOMES],
    *[
        pytest.param(DEV_FULL, name, marks=needs_dev_full)
        for name in ("findings_1", "missing_diff_3", "unexpected_14")
    ],
]


@pytest.mark.parametrize(("kind", "outcome"), STDERR_CASES)
def test_p0_s3_ac4_unavailable_stderr_never_changes_the_exit_code(kind, outcome):
    command, code = STDERR_OUTCOMES[outcome]

    completed = run_process(command, stderr=kind)

    assert completed.returncode == code
    if code in {0, 1}:
        assert completed.stdout.strip(), "a delivered review reaches stdout"
    else:
        assert completed.stdout == ""


@pytest.mark.parametrize("kind", ANY_BROKEN_STREAM)
def test_p0_s3_ac4_stdout_and_stderr_both_failing_exits_14(kind):
    completed = run_process(bot_command(), stdout=kind, stderr=kind)

    assert completed.returncode == 14


@pytest.mark.parametrize(
    ("command", "code", "stdout_lines"),
    [
        # The sample's rule findings print before the model's (P1-S7 AC1, AC8; P1-S8 AC1).
        (injected_command("one_finding"), 1, [*SAMPLE_RULE_LINES, FINDING_LINE]),
        (bot_command("--threshold", "critical"), 0, ["No findings at or above critical."]),
    ],
    ids=["findings", "no_findings"],
)
def test_p0_s3_ac5_success_lines_come_after_stdout_is_flushed(command, code, stdout_lines):
    # Both streams share one pipe, so a summary written before stdout's flush would come first.
    completed = run_process(command, stderr=MERGED)

    assert completed.returncode == code, completed.stdout
    lines = completed.stdout.splitlines()
    assert lines[: len(stdout_lines)] == stdout_lines
    rest = lines[len(stdout_lines) :]
    assert any("canned" in line.lower() for line in rest), lines
    assert any("input_tokens=" in line for line in rest), lines
