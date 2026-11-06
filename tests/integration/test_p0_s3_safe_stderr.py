"""P0-S3 AC4 to AC7, in process: one safe error line, and `main` survives losing stderr."""

import io
import json
import re
import sys
import unicodedata

import pytest
from diff_support import FINDING_DIFF, MINIMAL_DIFF
from fake_sdk import HIGH_FINDING, FakeSdk, findings_json, result_message

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import parse_findings

pytestmark = pytest.mark.integration

PREFIX = "review-bot: error:"
# The forged line catches a sanitiser that handles escapes but lets a newline through.
HOSTILE = "before\x1b[31mmid\nreview-bot: error: forged"
MARK = "MODEL-SENTINEL\x1b[31m"


class RaisesSentinel:
    async def review(self, request):
        raise RuntimeError("SENTINEL")


class RaisesSystemExit:
    def __init__(self, code):
        self.code = code

    async def review(self, request):
        raise SystemExit(self.code)


def raw_controls(text):
    return [char for char in text if unicodedata.category(char) in {"Cc", "Zl", "Zp"}]


def assert_only_the_error_line(stderr):
    lines = stderr.splitlines()
    assert len(lines) == 1, repr(stderr)
    assert lines[0].startswith(PREFIX), repr(stderr)
    assert raw_controls(stderr.removesuffix("\n")) == [], repr(stderr)


def assert_error_line_first_usage_may_follow(stderr):
    lines = stderr.split("\n")
    assert lines[0].startswith(PREFIX), repr(stderr)
    assert [line for line in lines if PREFIX in line] == [lines[0]], repr(stderr)
    assert raw_controls(stderr.replace("\n", "")) == [], repr(stderr)
    assert "Traceback" not in stderr


# The test body makes these swaps: pytest reinstates `capsys`'s streams when the call phase starts,
# undoing any swap a fixture made. The stderr tests also assert that nothing reached the capture,
# which fails if a swap stops taking effect.
@pytest.fixture(params=["none", "closed", "write", "flush"])
def lose_stderr(request, monkeypatch, failing_stream):
    """Run the test once for each way stderr can be unavailable."""

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


@pytest.fixture(params=["write", "flush"])
def break_stdout(request, monkeypatch, failing_stream):
    return lambda: monkeypatch.setattr(sys, "stdout", failing_stream(request.param))


@pytest.mark.parametrize("fails_on", ["write", "flush"])
def test_p0_s3_ac4_control_the_failing_stream_fails_where_it_says(failing_stream, fails_on):
    stream = failing_stream(fails_on)

    if fails_on == "write":
        with pytest.raises(OSError, match="No space"):
            stream.write("line\n")
    else:
        stream.write("line\n")
        with pytest.raises(OSError, match="No space"):
            stream.flush()


@pytest.mark.parametrize(
    ("code", "flags"),
    [(1, ()), (0, ("--threshold", "critical")), (2, ("--nope",))],
    ids=["findings_1", "no_findings_0", "usage_2"],
)
def test_p0_s3_ac4_unavailable_stderr_review_returns_its_own_code(
    bot, write_diff, lose_stderr, code, flags
):
    lose_stderr()

    run = bot.review(write_diff(FINDING_DIFF), *flags, provider=bot.mock(findings=[bot.finding()]))

    assert run.exit_code == code
    assert bool(run.stdout) == (code != 2)
    assert run.stderr == ""


def test_p0_s3_ac4_unavailable_stderr_missing_diff_returns_3(bot, tmp_path, lose_stderr):
    lose_stderr()

    run = bot.review(tmp_path / "absent.diff")

    assert run.exit_code == 3
    assert run.stderr == ""


@pytest.mark.parametrize(
    ("code", "error"),
    [(9, ProviderError(ErrorCategory.RATE_LIMIT, "detail")), (14, None)],
    ids=["provider_error_9", "unexpected_14"],
)
def test_p0_s3_ac4_unavailable_stderr_failed_review_keeps_its_code(
    bot, write_diff, lose_stderr, code, error
):
    provider = bot.mock(error=error) if error else RaisesSentinel()
    lose_stderr()

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == code
    assert run.stderr == ""


def test_p0_s3_ac4_stdout_failing_with_stderr_unavailable_returns_14(
    bot, write_diff, break_stdout, lose_stderr
):
    break_stdout()
    lose_stderr()

    run = bot.review(write_diff(MINIMAL_DIFF))

    assert run.exit_code == 14
    assert run.stderr == ""


@pytest.mark.parametrize("code", [0, 1, MARK], ids=["zero", "one", "model_text"])
def test_p0_s3_ac4_system_exit_from_a_provider_returns_14_in_process(bot, write_diff, code):
    fixed_line = bot.review(write_diff(MINIMAL_DIFF), provider=RaisesSentinel()).stderr

    run = bot.review(write_diff(MINIMAL_DIFF), provider=RaisesSystemExit(code))

    assert run.exit_code == 14, run.stderr
    assert run.stdout == ""
    assert_only_the_error_line(run.stderr)
    assert "MODEL-SENTINEL" not in run.stderr
    assert run.stderr == fixed_line


INPUT_FAILURES = {
    "missing_3": (3, lambda write: write(MINIMAL_DIFF).with_name("absent.diff"), ()),
    "directory_4": (4, lambda write: write(MINIMAL_DIFF).parent, ()),
    "not_utf8_4": (4, lambda write: write(b"\xff\xfe+x\n"), ()),
    "empty_5": (5, lambda write: write(""), ()),
    "too_large_6": (6, lambda write: write("+xx\n"), ("--max-diff-bytes", "1")),
}


@pytest.mark.parametrize(
    ("code", "path_of", "flags"), INPUT_FAILURES.values(), ids=INPUT_FAILURES.keys()
)
def test_p0_s3_ac5_input_failure_is_one_error_line(bot, write_diff, code, path_of, flags):
    run = bot.review(path_of(write_diff), *flags)

    assert run.exit_code == code, run.stderr
    assert_only_the_error_line(run.stderr)


@pytest.mark.parametrize("category", list(ErrorCategory))
@pytest.mark.parametrize("message", ["detail", ""], ids=["detail", "empty_message"])
def test_p0_s3_ac5_provider_failure_is_one_error_line(bot, write_diff, category, message):
    provider = bot.mock(error=ProviderError(category, message))

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert 7 <= run.exit_code <= 13, run.stderr
    assert_only_the_error_line(run.stderr)


def test_p0_s3_ac5_internal_error_is_one_error_line(bot, write_diff):
    run = bot.review(write_diff(MINIMAL_DIFF), provider=RaisesSentinel())

    assert run.exit_code == 14, run.stderr
    assert_only_the_error_line(run.stderr)
    assert "SENTINEL" not in run.stderr


@pytest.mark.parametrize(
    "flags",
    [("--nope",), ("--threshold", "urgent"), ("--max-diff-bytes", "abc")],
    ids=["unknown_flag", "invalid_value", "invalid_number"],
)
def test_p0_s3_ac5_usage_error_starts_with_the_error_line(bot, write_diff, flags):
    run = bot.review(write_diff(MINIMAL_DIFF), *flags)

    assert run.exit_code == 2, run.stderr
    assert_error_line_first_usage_may_follow(run.stderr)


def test_p0_s3_ac5_missing_argument_starts_with_the_error_line(bot):
    run = bot.run()

    assert run.exit_code == 2, run.stderr
    assert_error_line_first_usage_may_follow(run.stderr)


def test_p0_s3_ac5_invalid_variable_starts_with_the_error_line(bot, write_diff, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_THRESHOLD", "urgent")

    run = bot.review(write_diff(MINIMAL_DIFF))

    assert run.exit_code == 2, run.stderr
    assert_error_line_first_usage_may_follow(run.stderr)


@pytest.mark.parametrize("flags", [(), ("--threshold", "critical")], ids=["findings", "none"])
def test_p0_s3_ac5_stdout_failure_leaves_only_the_error_line_in_process(
    bot, write_diff, break_stdout, flags
):
    break_stdout()

    run = bot.review(write_diff(FINDING_DIFF), *flags, provider=bot.mock(findings=[bot.finding()]))

    assert run.exit_code == 14, run.stderr
    assert_only_the_error_line(run.stderr)


@pytest.mark.parametrize("category", list(ErrorCategory))
def test_p0_s3_ac6_hostile_provider_message_is_one_clean_line(bot, write_diff, category):
    provider = bot.mock(error=ProviderError(category, HOSTILE))

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert 7 <= run.exit_code <= 13, run.stderr
    assert_only_the_error_line(run.stderr)


@pytest.mark.parametrize(
    "control",
    ["\r", "\x0b", "\x0c", "\x85", "\x9b", "\x00", "\x7f", "\N{LINE SEPARATOR}"],
    ids=["cr", "vt", "ff", "nel", "c1_csi", "nul", "del", "line_separator"],
)
def test_p0_s3_ac6_other_controls_in_a_provider_message_are_not_raw(bot, write_diff, control):
    provider = bot.mock(error=ProviderError(ErrorCategory.TRANSIENT, f"before{control}after"))

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 11, run.stderr
    assert_only_the_error_line(run.stderr)


def test_p0_s3_ac6_hostile_missing_path_is_one_clean_line(bot, tmp_path):
    run = bot.review(tmp_path / f"absent{HOSTILE}.diff")

    # Which of the two depends on the platform, as AC6 says.
    assert run.exit_code in {3, 4}, run.stderr
    assert_only_the_error_line(run.stderr)


@pytest.mark.parametrize(
    "flags",
    [
        (f"--nope{HOSTILE}", "--provider", "mock"),
        (f"--nope={HOSTILE}", "--provider", "mock"),
        (f"extra{HOSTILE}", "--provider", "mock"),
        ("--threshold", HOSTILE, "--provider", "mock"),
        (f"--threshold={HOSTILE}", "--provider", "mock"),
        ("--max-diff-bytes", HOSTILE, "--provider", "mock"),
        ("--deadline-s", HOSTILE, "--provider", "mock"),
        ("--provider", HOSTILE),
    ],
    ids=[
        "unknown_flag",
        "unknown_flag_with_value",
        "extra_positional",
        "threshold",
        "threshold_equals",
        "max_diff_bytes",
        "deadline",
        "provider",
    ],
)
def test_p0_s3_ac6_hostile_flag_gives_one_clean_error_line(bot, write_diff, flags):
    run = bot.run(str(write_diff(MINIMAL_DIFF)), *flags)

    assert run.exit_code == 2, run.stderr
    assert_error_line_first_usage_may_follow(run.stderr)


def test_p0_s3_ac6_hostile_variable_gives_one_clean_error_line(bot, write_diff, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_THRESHOLD", HOSTILE)

    run = bot.review(write_diff(MINIMAL_DIFF))

    assert run.exit_code == 2, run.stderr
    assert_error_line_first_usage_may_follow(run.stderr)


FORGED_COUNT = "\x1b[31mCLI\nreview-bot: error: forged"
BAD_USAGE = {
    "forged_input": ({"input_tokens": FORGED_COUNT, "output_tokens": 7}, 0, 7),
    "forged_output": ({"input_tokens": 7, "output_tokens": FORGED_COUNT}, 7, 0),
    "negative_input": ({"input_tokens": -5, "output_tokens": 7}, 0, 7),
    "negative_output": ({"input_tokens": 7, "output_tokens": -5}, 7, 0),
    "boolean_input": ({"input_tokens": True, "output_tokens": 7}, 0, 7),
    "junk": ("junk", 0, 0),
    "list": ([1], 0, 0),
}


@pytest.mark.parametrize(
    ("usage", "input_tokens", "output_tokens"), BAD_USAGE.values(), ids=BAD_USAGE.keys()
)
def test_p0_s3_ac6_bad_usage_counts_as_0_and_the_review_is_delivered(
    claude_review, usage, input_tokens, output_tokens
):
    fake = FakeSdk(result_message(findings_json(HIGH_FINDING), usage=usage))

    run = claude_review(fake)

    assert run.exit_code == 1, run.stderr
    assert run.stdout_body.startswith("src/app.py:7 ")
    assert len(run.stderr.splitlines()) == 1, repr(run.stderr)
    assert raw_controls(run.stderr.removesuffix("\n")) == [], repr(run.stderr)
    assert f"input_tokens={input_tokens} " in run.stderr
    # Phase 1 appends fields after this one (phase-1 design, Compatibility), so it ends a field.
    assert re.search(rf" output_tokens={output_tokens}( |\n$)", run.stderr), repr(run.stderr)
    assert "CLI" not in run.stderr
    assert "forged" not in run.stderr


AC7_INVALID_JSON = {
    "prose": f"{MARK} not json",
    "truncated": '{"findings": [{"file": "' + MARK,
    "fenced": f"```json\n{MARK}\n```",
}
AC7_FAILS_THE_SCHEMA = {
    "line": findings_json(HIGH_FINDING | {"line": MARK}),
    "severity": findings_json(HIGH_FINDING | {"severity": MARK}),
    "category": findings_json(HIGH_FINDING | {"category": MARK}),
    "confidence": findings_json(HIGH_FINDING | {"confidence": MARK}),
    "file_not_a_string": findings_json(HIGH_FINDING | {"file": [MARK]}),
    "rationale_not_a_string": findings_json(HIGH_FINDING | {"rationale": {MARK: MARK}}),
    "extra_field_name": findings_json(HIGH_FINDING | {MARK: 1}),
    "extra_top_level_name": json.dumps({"findings": [], MARK: 1}),
    "findings_not_a_list": json.dumps({"findings": MARK}),
    "finding_not_an_object": json.dumps({"findings": [MARK]}),
    "bare_string": json.dumps(MARK),
}
AC7_OUTPUT = AC7_INVALID_JSON | AC7_FAILS_THE_SCHEMA


@pytest.mark.parametrize("raw", AC7_OUTPUT.values(), ids=AC7_OUTPUT.keys())
def test_p0_s3_ac7_model_text_never_reaches_stderr(bot, write_diff, raw):
    run = bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock(raw_output=raw))

    assert run.exit_code == 13, run.stderr
    assert run.stdout == ""
    assert "MODEL-SENTINEL" not in run.stderr
    assert "\x1b" not in run.stderr
    assert "input_value" not in run.stderr
    assert_only_the_error_line(run.stderr)


@pytest.mark.parametrize("raw", AC7_OUTPUT.values(), ids=AC7_OUTPUT.keys())
def test_p0_s3_ac7_invalid_output_message_embeds_no_model_text(raw):
    with pytest.raises(ProviderError) as caught:
        parse_findings(raw)

    assert caught.value.category is ErrorCategory.INVALID_OUTPUT
    message = str(caught.value)
    assert "MODEL-SENTINEL" not in message
    assert "\x1b" not in message
    assert "input_value" not in message
