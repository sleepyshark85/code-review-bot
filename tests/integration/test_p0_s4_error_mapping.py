"""P0-S4 AC1 to AC8 and AC16: what each SDK failure means, and which signal wins.

Over a fake `sdk_call` using the real SDK classes. `test_p0_s4_real_sdk.py` runs the same paths
through the real `query`, which fakes can't stand in for: the SDK's own logging, for one.
AC15 is manual, run by the main session, and has no test here: see the test report.
"""

import asyncio

import claude_agent_sdk
import pytest
from claude_agent_sdk import (
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
    ResultError,
    SystemMessage,
)
from claude_agent_sdk._errors import MessageParseError
from fake_sdk import (
    HIGH_FINDING,
    FakeSdk,
    assistant_error,
    error_result,
    findings_json,
    hang,
    pause,
    raised_result_error,
    result_message,
)
from p0_s4_support import (
    EXIT_CODES,
    MARK,
    PREFIX,
    assert_maps_to,
    assert_one_error_line,
    failure,
    main_within_5s,
    review_within_5s,
)

from review_bot.errors import ErrorCategory, ProviderError

pytestmark = pytest.mark.integration

AUTH = ErrorCategory.AUTH
RATE_LIMIT = ErrorCategory.RATE_LIMIT
REFUSAL = ErrorCategory.REFUSAL
TRANSIENT = ErrorCategory.TRANSIENT
PERMANENT = ErrorCategory.PERMANENT
UNAVAILABLE = ErrorCategory.UNAVAILABLE

SENTINEL = "SENTINEL"
SUCCESSFUL = {
    "non_empty": (findings_json(HIGH_FINDING), 1),
    "empty": (findings_json(), 0),
}
PROSE = "I could not review this."

KNOWN_SIGNALS = [
    ("authentication_failed", AUTH),
    ("rate_limit", RATE_LIMIT),
    ("server_error", TRANSIENT),
    ("billing_error", PERMANENT),
    ("invalid_request", PERMANENT),
    ("unknown", PERMANENT),
    ("a_value_the_sdk_adds_later", PERMANENT),
]

STATUS_RULES = [
    (401, AUTH),
    (403, AUTH),
    (429, RATE_LIMIT),
    (408, TRANSIENT),
    (500, TRANSIENT),
    (502, TRANSIENT),
    (503, TRANSIENT),
    (529, TRANSIENT),
    (599, TRANSIENT),
    (400, PERMANENT),
    (402, PERMANENT),
    (404, PERMANENT),
    (413, PERMANENT),
    (418, PERMANENT),
    (499, PERMANENT),
    (600, PERMANENT),
]
RESULT_ROUTES = {"result_message": error_result, "raised_result_error": raised_result_error}
route_ids = list(RESULT_ROUTES)
routes = list(RESULT_ROUTES.values())


# --- AC1 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (CLINotFoundError(), UNAVAILABLE),
        (CLINotFoundError("Claude Code not found", cli_path="/no/such/claude"), UNAVAILABLE),
        (CLIConnectionError("Could not connect."), TRANSIENT),
        (ProcessError("Command failed", exit_code=1, stderr="details"), PERMANENT),
        (ProcessError("Command failed"), PERMANENT),
        (ClaudeSDKError("Something else."), PERMANENT),
        (CLIJSONDecodeError("not json", ValueError("bad")), PERMANENT),
        (MessageParseError("Missing a field", {"type": "assistant"}), PERMANENT),
    ],
    ids=[
        "cli_not_found",
        "cli_not_found_with_a_path",
        "connection_error",
        "process_error",
        "process_error_with_no_exit_code",
        "other_sdk_error",
        "json_decode_error",
        "message_parse_error",
    ],
)
def test_p0_s4_ac1_raised_sdk_error_maps_to_its_category(
    claude_provider, claude_review, error, category
):
    assert_maps_to(claude_provider, claude_review, (error,), category)


def test_p0_s4_ac1_connection_error_keeps_the_sdks_guidance(claude_provider, claude_review):
    guidance = "Working directory does not exist: /gone"
    script = (CLIConnectionError(guidance),)

    assert guidance in str(failure(claude_provider, script))
    assert guidance in claude_review(FakeSdk(*script)).stderr


def test_p0_s4_ac1_kept_guidance_is_still_one_clean_line(claude_review):
    run = claude_review(FakeSdk(CLIConnectionError("Guidance\x1b[31m\nreview-bot: error: forged")))

    assert run.exit_code == 11, run.stderr
    assert_one_error_line(run.stderr)


def test_p0_s4_ac1_sdk_version_is_the_audited_one():
    assert claude_agent_sdk.__version__ == "0.2.178", (
        "claude-agent-sdk moved. Before changing this pin, re-audit every CLIConnectionError raise "
        "site, because that text reaches stderr (the spec's AC1 note), and the error mapping in "
        "providers/claude_agent.py against the new SDK."
    )


@pytest.mark.parametrize(
    ("error", "parent_would_give", "category"),
    [
        (CLINotFoundError(), TRANSIENT, UNAVAILABLE),
        (raised_result_error(429), PERMANENT, RATE_LIMIT),
    ],
    ids=["cli_not_found_under_connection_error", "result_error_under_process_error"],
)
def test_p0_s4_ac1_subclass_maps_before_its_parent(
    claude_provider, error, parent_would_give, category
):
    caught = failure(claude_provider, (error,))

    assert caught.category is category
    assert caught.category is not parent_would_give


# --- AC2 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("route", routes, ids=route_ids)
@pytest.mark.parametrize(
    ("status", "category"), STATUS_RULES, ids=[str(status) for status, _ in STATUS_RULES]
)
def test_p0_s4_ac2_status_decides_the_category(
    claude_provider, claude_review, route, status, category
):
    assert_maps_to(claude_provider, claude_review, (route(status),), category)


@pytest.mark.parametrize("route", routes, ids=route_ids)
@pytest.mark.parametrize(
    ("terminal_reason", "category"),
    [("api_error", TRANSIENT), (None, PERMANENT), ("max_turns", PERMANENT), ("x", PERMANENT)],
    ids=["api_error", "no_reason", "max_turns", "other_reason"],
)
def test_p0_s4_ac2_no_status_falls_back_to_the_terminal_reason(
    claude_provider, claude_review, route, terminal_reason, category
):
    script = (route(None, terminal_reason),)

    assert_maps_to(claude_provider, claude_review, script, category)


@pytest.mark.parametrize("route", routes, ids=route_ids)
@pytest.mark.parametrize(
    ("status", "category"),
    [(400, PERMANENT), (401, AUTH), (429, RATE_LIMIT)],
    ids=["400", "401", "429"],
)
def test_p0_s4_ac2_a_status_beats_the_terminal_reason(claude_provider, route, status, category):
    assert failure(claude_provider, (route(status, "api_error"),)).category is category


@pytest.mark.parametrize("status", ["401", True], ids=["string", "boolean"])
def test_p0_s4_ac2_status_that_is_not_an_integer_counts_as_no_status(claude_provider, status):
    # Beyond the criterion's text: flagged for the designer. `ResultError` already drops a string.
    # With `api_error`, no status gives `transient`; a status counted by mistake, `permanent`.
    script = (error_result(status, "api_error"),)

    assert failure(claude_provider, script).category is TRANSIENT


@pytest.mark.parametrize(
    ("subtype", "result"),
    [
        ("success", findings_json(HIGH_FINDING | {"rationale": SENTINEL})),
        ("success", f"{SENTINEL} API error"),
        ("error_during_execution", findings_json(HIGH_FINDING | {"rationale": SENTINEL})),
        ("error_max_turns", f"{SENTINEL} API error"),
        ("success", findings_json()),
        ("success", None),
    ],
    ids=[
        "success_parseable",
        "success_prose",
        "error_parseable",
        "error_prose",
        "success_empty_findings",
        "success_no_result_text",
    ],
)
def test_p0_s4_ac2_error_result_is_never_parsed_as_findings(claude_review, subtype, result):
    # Replaces P0-S2 AC13's expectation of 14 for these inputs.
    fake = FakeSdk(result_message(result, is_error=True, subtype=subtype, errors=[SENTINEL]))

    run = claude_review(fake)

    assert run.exit_code == 12, run.stderr
    assert run.stdout == ""
    assert SENTINEL not in run.stderr
    assert_one_error_line(run.stderr)


# --- AC3 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("is_error", [True, False], ids=["is_error", "not_is_error"])
@pytest.mark.parametrize(
    "result",
    [None, PROSE, findings_json(), findings_json(HIGH_FINDING)],
    ids=["no_result", "prose", "empty_findings", "valid_findings"],
)
def test_p0_s4_ac3_refusal_stop_reason_is_a_refusal(
    claude_provider, claude_review, is_error, result
):
    script = (result_message(result, is_error=is_error, stop_reason="refusal"),)

    assert_maps_to(claude_provider, claude_review, script, REFUSAL)


@pytest.mark.parametrize("status", [400, 401, 429, 503])
def test_p0_s4_ac3_refusal_with_a_status_is_still_a_refusal(claude_provider, claude_review, status):
    script = (result_message(None, is_error=True, stop_reason="refusal", api_error_status=status),)

    assert_maps_to(claude_provider, claude_review, script, REFUSAL)


# --- AC4 ------------------------------------------------------------------------------------------


TAILS = {
    "stream_ends": (),
    "prose_result": (result_message(PROSE),),
    "error_result_with_no_status": (error_result(),),
    "process_error": (ProcessError("Command failed", exit_code=1),),
    "unexpected_exception": (ValueError("boom"),),
}


@pytest.mark.parametrize("tail", TAILS.values(), ids=TAILS.keys())
@pytest.mark.parametrize(
    ("signal", "category"), KNOWN_SIGNALS, ids=[signal for signal, _ in KNOWN_SIGNALS]
)
def test_p0_s4_ac4_assistant_error_then_no_successful_result_maps(
    claude_provider, claude_review, signal, category, tail
):
    script = (assistant_error(signal), *tail)

    assert_maps_to(claude_provider, claude_review, script, category)


def test_p0_s4_ac4_billing_error_message_names_billing(claude_provider, claude_review):
    script = (assistant_error("billing_error"),)

    assert "billing" in str(failure(claude_provider, script)).lower()
    assert "billing" in claude_review(FakeSdk(*script)).stderr.lower()


def test_p0_s4_ac4_unrecognised_error_message_names_the_value(claude_provider, claude_review):
    script = (assistant_error("a_value_the_sdk_adds_later"),)

    assert "a_value_the_sdk_adds_later" in str(failure(claude_provider, script))
    assert "a_value_the_sdk_adds_later" in claude_review(FakeSdk(*script)).stderr


def test_p0_s4_ac4_named_value_is_still_one_clean_line(claude_review):
    run = claude_review(FakeSdk(assistant_error("new\x1b[31m\nreview-bot: error: forged")))

    assert run.exit_code == 12, run.stderr
    assert_one_error_line(run.stderr)


# --- AC5 ------------------------------------------------------------------------------------------


def live_sequence(signal, status):
    """The spec's sequence: what the real CLI sends for a failed API call."""
    return (
        assistant_error(signal),
        result_message(
            "API Error prose.",
            subtype="success",
            is_error=True,
            stop_reason="stop_sequence",
            terminal_reason="api_error",
            api_error_status=status,
        ),
        raised_result_error(status, "api_error"),
    )


def test_p0_s4_ac5_auth_signal_wins_over_result_and_exception(claude_provider, claude_review):
    script = live_sequence("authentication_failed", 401)

    assert_maps_to(claude_provider, claude_review, script, AUTH)


def test_p0_s4_ac5_rate_limit_signal_wins_over_a_500_result_and_exception(
    claude_provider, claude_review
):
    script = live_sequence("rate_limit", 500)

    assert_maps_to(claude_provider, claude_review, script, RATE_LIMIT)


def test_p0_s4_ac5_result_status_wins_over_a_later_process_error(claude_provider, claude_review):
    script = (error_result(401, "api_error"), ProcessError("Command failed", exit_code=1))

    assert_maps_to(claude_provider, claude_review, script, AUTH)


@pytest.mark.parametrize(
    ("script", "category"),
    [
        ((error_result(429), raised_result_error(500)), RATE_LIMIT),
        ((error_result(401), CLIConnectionError("x")), AUTH),
        ((error_result(401), CLINotFoundError()), AUTH),
        ((error_result(401), ValueError("boom")), AUTH),
        ((result_message(None, stop_reason="refusal"), ProcessError("x")), REFUSAL),
    ],
    ids=[
        "result_error_says_otherwise",
        "connection_error_says_otherwise",
        "not_found_says_otherwise",
        "unexpected_exception",
        "refusal_then_process_error",
    ],
)
def test_p0_s4_ac5_result_message_wins_over_any_later_exception(claude_provider, script, category):
    assert failure(claude_provider, script).category is category


@pytest.mark.parametrize(
    ("script", "category"),
    [
        ((assistant_error("server_error"), error_result(401)), TRANSIENT),
        ((assistant_error("rate_limit"), result_message(None, stop_reason="refusal")), RATE_LIMIT),
        ((assistant_error("billing_error"), CLINotFoundError()), PERMANENT),
    ],
    ids=["over_a_result_status", "over_a_refusal", "over_an_exception"],
)
def test_p0_s4_ac5_assistant_error_wins_when_the_others_disagree(claude_provider, script, category):
    assert failure(claude_provider, script).category is category


def test_p0_s4_ac5_earliest_signal_wins_in_arrival_order(claude_provider, claude_review):
    script = (error_result(429), assistant_error("authentication_failed"))

    assert_maps_to(claude_provider, claude_review, script, RATE_LIMIT)


# --- AC6 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("result", "code"), SUCCESSFUL.values(), ids=SUCCESSFUL.keys())
@pytest.mark.parametrize("signal", [signal for signal, _ in KNOWN_SIGNALS])
def test_p0_s4_ac6_successful_result_overrides_the_signal_and_warns(
    claude_review, signal, result, code
):
    fake = FakeSdk(assistant_error(signal, MARK), result_message(result))

    run = claude_review(fake)

    assert run.exit_code == code, run.stderr
    assert bool(run.stdout.strip()) and ("src/app.py" in run.stdout) == (code == 1)
    assert PREFIX not in run.stderr
    assert sum(signal in line for line in run.stderr.splitlines()) == 1, repr(run.stderr)
    assert "MODEL-SENTINEL" not in run.stderr
    assert "\x1b" not in run.stderr
    assert "provider=claude" in run.stderr


def test_p0_s4_ac6_no_signal_gives_no_warning(claude_review):
    run = claude_review(FakeSdk(result_message(findings_json(HIGH_FINDING))))

    assert run.exit_code == 1, run.stderr
    assert len(run.stderr.splitlines()) == 1, repr(run.stderr)
    assert "provider=claude" in run.stderr


def test_p0_s4_ac6_named_signal_is_still_a_clean_warning(claude_review):
    # Beyond the criterion's text: flagged for the designer. The value comes from the CLI.
    hostile = "new\x1b[31m\nreview-bot: error: forged"
    fake = FakeSdk(assistant_error(hostile), result_message(findings_json(HIGH_FINDING)))

    run = claude_review(fake)

    assert run.exit_code == 1, run.stderr
    assert "\x1b" not in run.stderr
    assert not [line for line in run.stderr.splitlines() if line.startswith(PREFIX)]


@pytest.mark.parametrize(
    "tail",
    [
        (ProcessError("Command failed", exit_code=1),),
        (raised_result_error(401),),
        (ValueError("boom"),),
        (error_result(500),),
        (result_message(PROSE),),
    ],
    ids=["process_error", "result_error", "unexpected", "error_result", "prose_result"],
)
@pytest.mark.parametrize(("result", "code"), SUCCESSFUL.values(), ids=SUCCESSFUL.keys())
def test_p0_s4_ac6_nothing_after_a_successful_result_is_read(claude_review, result, code, tail):
    reached = []

    async def record(_options):
        reached.append(True)

    fake = FakeSdk(result_message(result), record, *tail)

    run = claude_review(fake)

    assert run.exit_code == code, run.stderr
    assert reached == []
    assert fake.closed
    assert PREFIX not in run.stderr


NEVER_ENDS = {
    "silent": {"tail": (hang,), "forever": False},
    "never_stops_sending": {
        "tail": (pause(0.01), SystemMessage(subtype="status", data={})),
        "forever": True,
    },
}


@pytest.mark.parametrize("stream", NEVER_ENDS.values(), ids=NEVER_ENDS.keys())
@pytest.mark.parametrize(("result", "code"), SUCCESSFUL.values(), ids=SUCCESSFUL.keys())
def test_p0_s4_ac6_review_returns_within_5s_when_the_stream_never_ends(
    claude_provider, stream, result, code
):
    fake = FakeSdk(result_message(result), *stream["tail"], forever=stream["forever"])

    review = review_within_5s(claude_provider(fake, "--deadline-s", "30"), fake)

    assert review.error is None
    assert len(review.response.findings) == code
    assert review.closed_on_return, "the stream was still open when review returned"


def test_p0_s4_ac6_main_exits_by_findings_when_the_stream_never_ends(claude_review):
    fake = FakeSdk(result_message(findings_json(HIGH_FINDING)), hang)

    run, _ = main_within_5s(claude_review, fake, "--deadline-s", "30")

    assert run.exit_code == 1, run.stderr
    assert "src/app.py" in run.stdout
    assert fake.closed


@pytest.mark.parametrize(("result", "code"), SUCCESSFUL.values(), ids=SUCCESSFUL.keys())
def test_p0_s4_ac6_deadline_during_close_still_returns_the_findings(claude_review, result, code):
    async def hangs_on_close(_prompt, _options):
        try:
            yield result_message(result)
        finally:
            await asyncio.Event().wait()

    run, _ = main_within_5s(claude_review, hangs_on_close, "--deadline-s", "0.1")

    assert run.exit_code == code, run.stderr
    assert ("src/app.py" in run.stdout) == (code == 1)
    assert PREFIX not in run.stderr


@pytest.mark.parametrize(
    ("signal", "code"),
    [("authentication_failed", 8), ("rate_limit", 9), ("server_error", 11), ("billing_error", 12)],
)
def test_p0_s4_ac6_earlier_signal_wins_over_prose(claude_review, signal, code):
    run = claude_review(FakeSdk(assistant_error(signal), result_message(PROSE, is_error=False)))

    assert run.exit_code == code, run.stderr
    assert run.stdout == ""
    assert_one_error_line(run.stderr)


def test_p0_s4_ac6_note_failed_result_then_a_successful_one_delivers_and_warns(claude_review):
    # From a note the developer added to the spec during this run: flagged for the designer.
    fake = FakeSdk(error_result(429, result=MARK), result_message(findings_json(HIGH_FINDING)))

    run = claude_review(fake)

    assert run.exit_code == 1, run.stderr
    assert "src/app.py" in run.stdout
    assert "rate_limit" in run.stderr
    assert "MODEL-SENTINEL" not in run.stderr
    assert PREFIX not in run.stderr


def test_p0_s4_ac6_prose_alone_returns_13(claude_review):
    run = claude_review(FakeSdk(result_message(PROSE, is_error=False)))

    assert run.exit_code == 13, run.stderr
    assert run.stdout == ""
    assert_one_error_line(run.stderr)


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": None, "output_tokens": None},
        {"input_tokens": "2", "output_tokens": "7"},
        {"input_tokens": True, "output_tokens": False},
        {"input_tokens": -2, "output_tokens": -7},
        {"input_tokens": 2.5, "output_tokens": [7]},
        {"cache_read_input_tokens": "x", "cache_creation_input_tokens": -1},
        ["not", "a", "mapping"],
    ],
    ids=[
        "null",
        "empty",
        "null_counts",
        "strings",
        "booleans",
        "negative",
        "other_types",
        "cache_fields",
        "not_a_mapping",
    ],
)
@pytest.mark.parametrize(("result", "code"), SUCCESSFUL.values(), ids=SUCCESSFUL.keys())
def test_p0_s4_ac6_note_bad_usage_still_returns_the_findings(
    claude_provider, claude_review, usage, result, code
):
    # The spec's Notes: usage never affects the verdict, and an invalid count counts as 0.
    message = result_message(result, usage=usage)
    fake = FakeSdk(message)

    review = review_within_5s(claude_provider(fake), fake)
    run = claude_review(FakeSdk(message))

    assert review.error is None
    assert len(review.response.findings) == code
    assert review.response.usage.input_tokens == 0
    assert review.response.usage.output_tokens == 0
    assert run.exit_code == code, run.stderr
    assert "input_tokens=0 output_tokens=0" in run.stderr


def test_p0_s4_ac6_note_one_bad_count_leaves_the_other_alone(claude_provider):
    message = result_message(findings_json(), usage={"input_tokens": 2, "output_tokens": "x"})
    fake = FakeSdk(message)

    review = review_within_5s(claude_provider(fake), fake)

    assert review.response.usage.input_tokens == 2
    assert review.response.usage.output_tokens == 0


# --- AC7 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        (),
        (SystemMessage(subtype="status", data={}),),
        (assistant_error(None, findings_json(HIGH_FINDING)),),
    ],
    ids=["empty_stream", "other_messages_only", "valid_json_only_in_an_assistant_message"],
)
def test_p0_s4_ac7_stream_with_no_result_message_is_permanent(
    claude_provider, claude_review, script
):
    # Replaces P0-S2 AC13's expectation of 14 for these inputs.
    assert_maps_to(claude_provider, claude_review, script, PERMANENT)


@pytest.mark.parametrize(
    "error",
    [
        ValueError(SENTINEL),
        RuntimeError(SENTINEL),
        Exception("Control request timeout: initialize"),
        Exception(SENTINEL),
    ],
    ids=["value_error", "runtime_error", "control_request_timeout", "bare_exception"],
)
def test_p0_s4_ac7_non_sdk_class_stays_uncategorised_and_exits_14(
    claude_provider, claude_review, error
):
    with pytest.raises(type(error)) as caught:
        failure(claude_provider, (error,))
    assert not isinstance(caught.value, ProviderError)

    run = claude_review(FakeSdk(error))

    assert run.exit_code == 14, run.stderr
    assert run.stdout == ""
    assert SENTINEL not in run.stderr
    assert "Control request timeout" not in run.stderr
    assert_one_error_line(run.stderr)


def test_p0_s4_ac7_non_sdk_class_after_other_messages_exits_14(claude_review):
    fake = FakeSdk(SystemMessage(subtype="status", data={}), ValueError(SENTINEL))

    run = claude_review(fake)

    assert run.exit_code == 14, run.stderr
    assert SENTINEL not in run.stderr


def test_p0_s4_ac7_non_sdk_class_after_an_error_signal_keeps_the_signal(claude_review):
    run = claude_review(FakeSdk(assistant_error("rate_limit"), ValueError(SENTINEL)))

    assert run.exit_code == 9, run.stderr


def test_p0_s4_ac7_timeout_error_raised_by_the_stream_exits_11(claude_provider, claude_review):
    # AC7's carve-out: any `TimeoutError` reaching `review` counts as the deadline passing.
    script = (TimeoutError(SENTINEL),)

    assert_maps_to(claude_provider, claude_review, script, TRANSIENT)
    assert SENTINEL not in claude_review(FakeSdk(*script)).stderr


# --- AC8 ------------------------------------------------------------------------------------------


NO_RESULT_STREAMS = {
    "silent": ((hang,), False),
    "never_stops_sending": ((pause(0.01), SystemMessage(subtype="status", data={})), True),
}


@pytest.mark.parametrize(("tail", "forever"), NO_RESULT_STREAMS.values(), ids=NO_RESULT_STREAMS)
def test_p0_s4_ac8_deadline_with_no_signal_is_transient(claude_provider, tail, forever):
    fake = FakeSdk(*tail, forever=forever)

    review = review_within_5s(claude_provider(fake, "--deadline-s", "0.1"), fake)

    assert review.response is None
    assert review.error.category is TRANSIENT
    assert review.closed_on_return, "the stream was still open when review raised"
    assert review.elapsed >= 0.09, "review raised before the deadline could have passed"


@pytest.mark.parametrize(("tail", "forever"), NO_RESULT_STREAMS.values(), ids=NO_RESULT_STREAMS)
def test_p0_s4_ac8_deadline_with_no_signal_exits_11(claude_review, tail, forever):
    fake = FakeSdk(*tail, forever=forever)

    run, elapsed = main_within_5s(claude_review, fake, "--deadline-s", "0.1")

    assert run.exit_code == 11, run.stderr
    assert fake.started
    assert fake.closed
    assert elapsed >= 0.09, "main returned before the deadline could have passed"
    assert run.stdout == ""
    assert_one_error_line(run.stderr)


@pytest.mark.parametrize(("tail", "forever"), NO_RESULT_STREAMS.values(), ids=NO_RESULT_STREAMS)
def test_p0_s4_ac8_deadline_after_an_auth_signal_reports_auth(claude_provider, tail, forever):
    fake = FakeSdk(assistant_error("authentication_failed"), *tail, forever=forever)

    review = review_within_5s(claude_provider(fake, "--deadline-s", "0.1"), fake)

    assert review.response is None
    assert review.error.category is AUTH
    assert review.closed_on_return, "the stream was still open when review raised"


@pytest.mark.parametrize(("tail", "forever"), NO_RESULT_STREAMS.values(), ids=NO_RESULT_STREAMS)
def test_p0_s4_ac8_deadline_after_an_auth_signal_exits_8(claude_review, tail, forever):
    fake = FakeSdk(assistant_error("authentication_failed"), *tail, forever=forever)

    run, _ = main_within_5s(claude_review, fake, "--deadline-s", "0.1")

    assert run.exit_code == 8, run.stderr
    assert fake.closed
    assert run.stdout == ""
    assert_one_error_line(run.stderr)


def test_p0_s4_ac8_deadline_after_an_error_result_reports_that_result(claude_review):
    # Beyond the criterion's text, which names `AssistantMessage.error`: flagged for the designer.
    fake = FakeSdk(error_result(429), hang)

    run, _ = main_within_5s(claude_review, fake, "--deadline-s", "0.1")

    assert run.exit_code == 9, run.stderr
    assert fake.closed


def test_p0_s4_ac8_deadline_variable_bounds_the_stream(claude_review, monkeypatch):
    # Carried over from P0-S2 AC7, with the new code.
    monkeypatch.setenv("REVIEW_BOT_DEADLINE_S", "0.1")
    fake = FakeSdk(hang)

    run, _ = main_within_5s(claude_review, fake)

    assert run.exit_code == 11, run.stderr
    assert fake.closed


def test_p0_s4_ac8_stream_slower_than_a_short_deadline_would_allow_still_succeeds(claude_review):
    # Carried over from P0-S2 AC7.
    fake = FakeSdk(pause(0.3), result_message(findings_json()))

    run, _ = main_within_5s(claude_review, fake, "--deadline-s", "4")

    assert run.exit_code == 0, run.stderr


# --- AC16 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize(
    "follow_up",
    [(), (ResultError("failed", data={}, exit_code=1),)],
    ids=["result_only", "then_result_error"],
)
def test_p0_s4_ac16_sdk_call_is_made_exactly_once(claude_review, status, follow_up):
    fake = FakeSdk(error_result(status), *follow_up)

    run = claude_review(fake, "--max-retries", "5")

    assert run.exit_code == EXIT_CODES[RATE_LIMIT if status == 429 else TRANSIENT], run.stderr
    assert len(fake.calls) == 1
