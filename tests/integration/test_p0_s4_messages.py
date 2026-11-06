"""P0-S4 AC9 to AC13, and the AC6 note on losing stderr: what the user reads, and the default."""

import io
import re
import subprocess
import sys

import pytest
from claude_agent_sdk import (
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
)
from claude_agent_sdk._errors import MessageParseError
from diff_support import FINDING_DIFF, MINIMAL_DIFF
from fake_sdk import (
    HIGH_FINDING,
    FakeSdk,
    assistant_error,
    error_result,
    findings_json,
    raised_result_error,
    result_message,
)
from p0_s4_support import MARK, PREFIX, assert_one_error_line, failure

from review_bot import cli
from review_bot.providers.claude_agent import ClaudeAgentProvider

pytestmark = pytest.mark.integration

API_KEY = "test-key-sentinel"
MODEL_TEXT = "MODEL-SENTINEL"


# --- AC9 ------------------------------------------------------------------------------------------


def test_p0_s4_ac9_cli_not_found_says_what_to_do(claude_review):
    run = claude_review(FakeSdk(CLINotFoundError()))

    assert run.exit_code == 7, run.stderr
    assert "install" in run.stderr
    assert "PATH" in run.stderr
    assert run.stdout == ""
    assert_one_error_line(run.stderr)


# --- AC10 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        (assistant_error("authentication_failed"),),
        (error_result(401),),
        (error_result(403),),
        (raised_result_error(401),),
    ],
    ids=["assistant_error", "result_401", "result_403", "raised_result_error"],
)
def test_p0_s4_ac10_auth_names_the_login_and_the_key(claude_review, script):
    run = claude_review(FakeSdk(*script))

    assert run.exit_code == 8, run.stderr
    assert "Claude Code" in run.stderr
    assert re.search(r"log ?in", run.stderr, re.IGNORECASE), run.stderr
    assert "ANTHROPIC_API_KEY" in run.stderr
    assert_one_error_line(run.stderr)


# --- AC11 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ProcessError("Command failed", exit_code=1, stderr=f"auth failed for {API_KEY}"),
        ProcessError(f"Command failed for {API_KEY}", exit_code=1),
        ProcessError(f"{API_KEY}", exit_code=1, stderr=API_KEY),
    ],
    ids=["in_stderr", "in_message", "in_both"],
)
def test_p0_s4_ac11_key_in_a_process_error_reaches_neither_stream(
    claude_review, monkeypatch, error
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)

    run = claude_review(FakeSdk(error))

    assert run.exit_code == 12, run.stderr
    assert API_KEY not in run.stdout
    assert API_KEY not in run.stderr


def test_p0_s4_ac11_control_the_fake_error_really_holds_the_key():
    error = ProcessError("Command failed", exit_code=1, stderr=f"auth failed for {API_KEY}")

    assert API_KEY in str(error)
    assert API_KEY in error.stderr


# --- AC12 -----------------------------------------------------------------------------------------


ROUTES = {
    "str_of_result_error": ((raised_result_error(500, text=MARK, result=None),), 11),
    "result_of_result_error": ((raised_result_error(400, result=MARK),), 12),
    "result_error_with_no_status": ((raised_result_error(text=MARK, result=MARK),), 12),
    "result_message_result": ((error_result(400, result=MARK),), 12),
    "result_message_result_with_no_status": ((error_result(result=MARK),), 12),
    "result_message_errors": ((result_message(None, is_error=True, errors=[MARK]),), 12),
    "cli_json_decode_error": ((CLIJSONDecodeError(MARK, ValueError(MARK)),), 12),
    "message_parse_error": ((MessageParseError(MARK, {"text": MARK}),), 12),
    "refusal_result": ((result_message(MARK, stop_reason="refusal"),), 10),
    "prose_result": ((result_message(MARK),), 13),
    "assistant_text": ((assistant_error("rate_limit", MARK),), 9),
    "assistant_text_then_result": (
        (assistant_error("rate_limit", MARK), error_result(500, result=MARK)),
        9,
    ),
    "process_error": ((ProcessError(MARK, exit_code=1, stderr=MARK),), 12),
}


@pytest.mark.parametrize(("script", "code"), ROUTES.values(), ids=ROUTES.keys())
def test_p0_s4_ac12_model_text_reaches_no_stream(claude_review, script, code):
    run = claude_review(FakeSdk(*script))

    assert run.exit_code == code, run.stderr
    assert MODEL_TEXT not in run.stderr
    assert "\x1b" not in run.stderr
    assert run.stdout == ""
    assert_one_error_line(run.stderr)


@pytest.mark.parametrize(
    "error",
    [CLIJSONDecodeError(MARK, ValueError(MARK)), MessageParseError(MARK, {"text": MARK})],
    ids=["json_decode", "message_parse"],
)
def test_p0_s4_ac12_message_does_not_use_the_text_of_either_class(claude_provider, error):
    assert MODEL_TEXT in str(error)

    caught = failure(claude_provider, (error,))

    assert MODEL_TEXT not in str(caught)
    assert "\x1b" not in str(caught)


@pytest.mark.parametrize(
    "error", [ClaudeSDKError(MARK), CLINotFoundError(MARK)], ids=["other_sdk_error", "not_found"]
)
def test_p0_s4_ac12_other_sdk_text_reaches_no_stream(claude_review, error):
    run = claude_review(FakeSdk(error))

    assert MODEL_TEXT not in run.stderr
    assert "\x1b" not in run.stderr
    assert_one_error_line(run.stderr)


def test_p0_s4_ac12_connection_error_text_is_escaped(claude_review):
    # AC1 keeps this one class's guidance, so its text may appear, but never raw.
    run = claude_review(FakeSdk(CLIConnectionError(MARK)))

    assert "\x1b" not in run.stderr
    assert_one_error_line(run.stderr)


def test_p0_s4_ac12_warning_on_a_successful_run_has_no_model_text(claude_review):
    fake = FakeSdk(assistant_error("rate_limit", MARK), result_message(findings_json()))

    run = claude_review(fake)

    assert run.exit_code == 0, run.stderr
    assert MODEL_TEXT not in run.stderr
    assert "\x1b" not in run.stderr


# --- AC13 -----------------------------------------------------------------------------------------


def test_p0_s4_ac13_parse_args_selects_claude(bot):
    _, settings = bot.parse_args("change.diff")

    assert settings.provider == "claude"


def test_p0_s4_ac13_build_provider_returns_claude_and_starts_no_subprocess(bot, monkeypatch):
    def refuse_process(*args, **kwargs):
        raise AssertionError("building the provider must not start a process")

    monkeypatch.setattr(subprocess.Popen, "__init__", refuse_process)
    _, settings = bot.parse_args("change.diff")

    provider = cli.build_provider(settings)

    assert isinstance(provider, ClaudeAgentProvider)


def test_p0_s4_ac13_a_run_with_no_flags_reaches_claude_not_canned_findings(
    bot, write_diff, real_query_calls
):
    run = bot.run(str(write_diff(MINIMAL_DIFF)))

    assert len(real_query_calls) == 1
    real_query_calls.clear()
    assert run.exit_code == 14, run.stderr
    assert run.stdout == ""
    assert "canned" not in run.stderr.lower()


def test_p0_s4_ac13_summary_of_a_default_run_names_claude(bot, write_diff):
    diff = write_diff(FINDING_DIFF)
    _, settings = bot.parse_args(str(diff))
    fake = FakeSdk(result_message(findings_json(HIGH_FINDING)))

    run = bot.run(str(diff), provider=ClaudeAgentProvider(settings, sdk_call=fake))

    assert run.exit_code == 1, run.stderr
    assert "provider=claude" in run.stderr
    assert "canned" not in run.stderr.lower()


def test_p0_s4_ac13_missing_diff_with_no_flags_stays_3(bot, tmp_path, real_query_calls):
    run = bot.run(str(tmp_path / "absent.diff"))

    assert run.exit_code == 3, run.stderr
    assert real_query_calls == []


def test_p0_s4_ac13_empty_diff_with_no_flags_stays_5(bot, write_diff, real_query_calls):
    run = bot.run(str(write_diff("   \n")))

    assert run.exit_code == 5, run.stderr
    assert real_query_calls == []


def test_p0_s4_ac13_provider_variable_still_beats_the_default(bot, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_PROVIDER", "mock")

    _, settings = bot.parse_args("change.diff")

    assert settings.provider == "mock"


def test_p0_s4_ac13_provider_flag_still_beats_the_default(bot):
    _, settings = bot.parse_args("change.diff", "--provider", "mock")

    assert settings.provider == "mock"


@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "whitespace"])
def test_p0_s4_ac13_blank_variable_exits_2_and_names_it(bot, write_diff, monkeypatch, value):
    monkeypatch.setenv("REVIEW_BOT_PROVIDER", value)
    provider = bot.mock()

    run = bot.run(str(write_diff(MINIMAL_DIFF)), provider=provider)

    assert run.exit_code == 2, run.stderr
    assert "REVIEW_BOT_PROVIDER" in run.stderr
    assert run.stdout == ""
    assert provider.requests == []


@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "whitespace"])
def test_p0_s4_ac13_blank_flag_exits_2_and_names_it(bot, write_diff, value):
    provider = bot.mock()

    run = bot.run(str(write_diff(MINIMAL_DIFF)), "--provider", value, provider=provider)

    assert run.exit_code == 2, run.stderr
    assert "--provider" in run.stderr
    assert run.stdout == ""
    assert provider.requests == []


# --- AC6's note: losing stderr, in process -------------------------------------------------------
# `test_p0_s4_real_sdk.py` has the real-process cases, the only ones that can see exit 120.


@pytest.fixture(params=["none", "closed", "write", "flush"])
def lose_stderr(request, monkeypatch, failing_stream):
    # Called from the test body: pytest reinstates `capsys`'s streams when the call phase starts.
    def lose():
        if request.param == "none":
            stream = None
        elif request.param == "closed":
            stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
            stream.close()
        else:
            stream = failing_stream(request.param)
        monkeypatch.setattr(sys, "stderr", stream)

    return lose


@pytest.mark.parametrize(
    ("result", "code"),
    [(findings_json(HIGH_FINDING), 1), (findings_json(), 0)],
    ids=["found", "none"],
)
def test_p0_s4_ac6_note_warning_with_stderr_lost_still_delivers(
    claude_review, lose_stderr, result, code
):
    fake = FakeSdk(assistant_error("rate_limit"), result_message(result))
    lose_stderr()

    run = claude_review(fake)

    assert run.exit_code == code
    assert ("src/app.py" in run.stdout) == (code == 1)
    assert run.stderr == ""


def test_p0_s4_ac6_warning_may_precede_the_error_line_when_stdout_fails(
    claude_review, monkeypatch, failing_stream
):
    # P0-S3 AC5 allows this one exception to its "exactly one line".
    fake = FakeSdk(assistant_error("rate_limit"), result_message(findings_json(HIGH_FINDING)))
    monkeypatch.setattr(sys, "stdout", failing_stream("write"))

    run = claude_review(fake)

    assert run.exit_code == 14, run.stderr
    warning, error = run.stderr.splitlines()
    assert "rate_limit" in warning
    assert error.startswith(PREFIX)


def test_p0_s4_ac6_note_provider_does_not_import_cli():
    probe = (
        "import sys, review_bot.providers.claude_agent;"
        "sys.exit(3 if 'review_bot.cli' in sys.modules else 0)"
    )

    completed = subprocess.run(  # noqa: S603 - fixed arguments, our own interpreter
        [sys.executable, "-c", probe],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
