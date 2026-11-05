"""P0-S2 AC1 to AC8: the options Claude is called with, and the limits on the call."""

import threading
import time

import pytest
from claude_agent_sdk import CLIConnectionError, ProcessError, SystemMessage
from fake_sdk import FakeSdk, findings_json, hang, pause, result_message, succeeds

from review_bot import cli
from review_bot.providers.base import ReviewRequest

pytestmark = pytest.mark.integration

NOISE = "CLI-NOISE-SENTINEL"

SETTINGS = [
    ("--max-retries", "REVIEW_BOT_MAX_RETRIES", "max_retries", 2),
    ("--api-timeout-ms", "REVIEW_BOT_API_TIMEOUT_MS", "api_timeout_ms", 90000),
    ("--deadline-s", "REVIEW_BOT_DEADLINE_S", "deadline_s", 300),
]
SETTING_IDS = [row[0] for row in SETTINGS]


async def noise(options):
    options.stderr(NOISE)


def main_within_5s(claude_review, fake, *flags):
    """Run `main` on a thread, so one that never returns fails the test instead of hanging it."""
    outcome = []
    thread = threading.Thread(
        target=lambda: outcome.append(claude_review(fake, *flags)), daemon=True
    )
    started = time.monotonic()
    thread.start()
    thread.join(5)
    elapsed = time.monotonic() - started

    assert not thread.is_alive(), "main was still running after 5 seconds"
    assert outcome, "main raised instead of returning"
    return outcome[0], elapsed


@pytest.mark.parametrize(
    "script",
    [(result_message(findings_json()),), (RuntimeError("boom"),)],
    ids=["successful_review", "failed_review"],
)
def test_p0_s2_ac1_claude_is_locked_down_for_any_review(claude_review, script):
    fake = FakeSdk(*script)

    claude_review(fake)

    options = fake.options
    assert options.tools == []
    assert options.setting_sources == []
    assert options.max_turns == 1
    assert options.allowed_tools == []
    assert options.strict_mcp_config is True
    assert options.mcp_servers == {}
    assert not options.hooks
    assert not options.agents


async def test_p0_s2_ac2_options_carry_exactly_the_requests_prompts_and_model(claude_provider):
    fake = succeeds()

    await claude_provider(fake).review(
        ReviewRequest(system_prompt="S", user_prompt="the user prompt", model="M")
    )

    prompt, options = fake.calls[0]
    assert options.system_prompt == "S"
    assert options.model == "M"
    assert prompt == "the user prompt"


def test_p0_s2_ac2_model_flag_and_diff_reach_the_call_through_main(claude_review):
    fake = succeeds()

    claude_review(fake, "--model", "flag-model")

    prompt, options = fake.calls[0]
    assert options.model == "flag-model"
    assert "+x" in prompt
    assert "+x" not in options.system_prompt


def test_p0_s2_ac3_default_env_is_exact(claude_review):
    fake = succeeds()

    claude_review(fake)

    assert fake.options.env == {"CLAUDE_CODE_MAX_RETRIES": "2", "API_TIMEOUT_MS": "90000"}


def test_p0_s2_ac3_flags_set_the_env_values(claude_review):
    fake = succeeds()

    claude_review(fake, "--max-retries", "0", "--api-timeout-ms", "1000")

    assert fake.options.env == {"CLAUDE_CODE_MAX_RETRIES": "0", "API_TIMEOUT_MS": "1000"}


def test_p0_s2_ac3_env_stays_exact_with_a_key_in_the_environment(claude_review, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-sentinel")
    fake = succeeds()

    claude_review(fake)

    assert fake.options.env == {"CLAUDE_CODE_MAX_RETRIES": "2", "API_TIMEOUT_MS": "90000"}


@pytest.mark.parametrize("setting", SETTINGS, ids=SETTING_IDS)
def test_p0_s2_ac4_variable_alone_sets_the_value(bot, monkeypatch, setting):
    _, variable, field, _ = setting
    monkeypatch.setenv(variable, "7")

    _, settings = bot.parse_args("change.diff")

    assert getattr(settings, field) == 7


@pytest.mark.parametrize("setting", SETTINGS, ids=SETTING_IDS)
def test_p0_s2_ac4_flag_beats_variable(bot, monkeypatch, setting):
    flag, variable, field, _ = setting
    monkeypatch.setenv(variable, "7")

    _, settings = bot.parse_args("change.diff", flag, "5")

    assert getattr(settings, field) == 5


@pytest.mark.parametrize("setting", SETTINGS, ids=SETTING_IDS)
def test_p0_s2_ac4_neither_gives_the_default(bot, setting):
    _, _, field, default = setting

    _, settings = bot.parse_args("change.diff")

    assert getattr(settings, field) == default


@pytest.mark.parametrize("setting", SETTINGS, ids=SETTING_IDS)
def test_p0_s2_ac4_flag_wins_over_an_invalid_variable(bot, monkeypatch, setting):
    flag, variable, field, _ = setting
    monkeypatch.setenv(variable, "-1")

    _, settings = bot.parse_args("change.diff", flag, "5")

    assert getattr(settings, field) == 5


def test_p0_s2_ac4_variables_reach_the_options(claude_review, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_MAX_RETRIES", "4")
    monkeypatch.setenv("REVIEW_BOT_API_TIMEOUT_MS", "1234")
    fake = succeeds()

    claude_review(fake)

    assert fake.options.env == {"CLAUDE_CODE_MAX_RETRIES": "4", "API_TIMEOUT_MS": "1234"}


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--max-retries", "-1"),
        ("--api-timeout-ms", "0"),
        ("--api-timeout-ms", "-1"),
        ("--deadline-s", "0"),
        ("--deadline-s", "-1"),
        ("--max-retries", "abc"),
        ("--api-timeout-ms", "abc"),
        ("--deadline-s", "abc"),
        ("--deadline-s", "nan"),
        ("--deadline-s", "inf"),
    ],
)
def test_p0_s2_ac5_bad_flag_returns_2_and_names_it(bot, claude_argv, flag, value):
    # No fake: a bad setting has to stop the run first, and the guard fails any test that doesn't.
    run = bot.run(*claude_argv(flag, value))

    assert run.exit_code == 2, run.stderr
    assert flag in run.stderr


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("REVIEW_BOT_MAX_RETRIES", "-1"),
        ("REVIEW_BOT_DEADLINE_S", "0"),
        ("REVIEW_BOT_DEADLINE_S", "-1"),
        ("REVIEW_BOT_API_TIMEOUT_MS", "0"),
        ("REVIEW_BOT_API_TIMEOUT_MS", "-1"),
        ("REVIEW_BOT_MAX_RETRIES", "abc"),
        ("REVIEW_BOT_DEADLINE_S", "nan"),
    ],
)
def test_p0_s2_ac5_bad_variable_returns_2_and_names_it(
    bot, claude_argv, monkeypatch, variable, value
):
    monkeypatch.setenv(variable, value)

    run = bot.run(*claude_argv())

    assert run.exit_code == 2, run.stderr
    assert variable in run.stderr


@pytest.mark.parametrize(
    "error",
    [RuntimeError("boom"), ProcessError("boom", exit_code=1), CLIConnectionError("boom")],
    ids=["unexpected", "process_error", "connection_error"],
)
def test_p0_s2_ac6_a_failing_call_is_made_exactly_once(claude_review, error):
    fake = FakeSdk(error)

    run = claude_review(fake, "--max-retries", "5")

    assert run.exit_code == 14, run.stderr
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "fake",
    [
        FakeSdk(hang),
        FakeSdk(pause(0.01), SystemMessage(subtype="status", data={}), forever=True),
    ],
    ids=["silent", "never_stops_sending"],
)
def test_p0_s2_ac7_never_ending_stream_exits_14_within_5s(claude_review, fake):
    run, elapsed = main_within_5s(claude_review, fake, "--deadline-s", "0.1")

    assert run.exit_code == 14, run.stderr
    assert fake.started
    assert fake.closed
    assert elapsed >= 0.09, "main returned before the deadline could have passed"
    assert run.stdout == ""


def test_p0_s2_ac7_deadline_variable_bounds_the_stream(claude_review, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_DEADLINE_S", "0.1")
    fake = FakeSdk(hang)

    run, _ = main_within_5s(claude_review, fake)

    assert run.exit_code == 14, run.stderr
    assert fake.closed


def test_p0_s2_ac7_stream_slower_than_a_short_deadline_would_allow_still_succeeds(claude_review):
    fake = FakeSdk(pause(0.3), result_message(findings_json()))

    run, _ = main_within_5s(claude_review, fake, "--deadline-s", "4")

    assert run.exit_code == 0, run.stderr


def test_p0_s2_ac8_stderr_callback_is_set_for_any_review(claude_review):
    fake = succeeds()

    claude_review(fake)

    assert callable(fake.options.stderr)


@pytest.mark.parametrize(
    ("script", "exit_code"),
    [
        ((noise, result_message(findings_json())), 0),
        ((noise, RuntimeError("boom")), 14),
    ],
    ids=["on_success", "on_failure"],
)
def test_p0_s2_ac8_cli_noise_reaches_neither_stream(
    claude_argv, claude_provider, capfd, script, exit_code
):
    # `capfd`, not `capsys`: the noise mustn't reach the real descriptors by any route either.
    fake = FakeSdk(*script)

    code = cli.main(claude_argv(), provider=claude_provider(fake))

    captured = capfd.readouterr()
    assert code == exit_code, captured.err
    assert fake.started
    assert NOISE not in captured.out
    assert NOISE not in captured.err
