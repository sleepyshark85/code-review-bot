"""P1-S5: see provider warnings and failure diagnostics in the audit log.

Written from the spec's criteria, ADR 0004 and design.md's Interfaces, before the developer's code
existed. The CLI's stderr is played through `options.stderr` by `fake_sdk.cli_stderr`, as the
spec's preamble says. The spec fixes what `agent.failed.data` holds but not its key names, so the
kept diagnostics are found as the one list of strings in that record's `data`.

`scrub_diagnostics` has no module named in design.md. Only `audit` reads diagnostics (ADR 0004),
so it's imported from `review_bot.audit`, per test, so a missing name fails its own tests only.

No secret appears in this file: each value is assembled at run time, as in P1-S3's tests.
"""

import ast
import asyncio
import contextlib
import importlib
import io
import json
import re
import string
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from diff_support import MINIMAL_DIFF
from fake_sdk import (
    HIGH_FINDING,
    FakeSdk,
    assistant_error,
    cli_stderr,
    error_result,
    findings_json,
    hang,
    raised_result_error,
    result_message,
)
from p0_s4_support import REQUEST, assert_one_error_line, raw_controls
from repo_paths import REPO_ROOT
from test_p1_s4_audit_log import entry_lists, events, only, read_records, scalars

from review_bot import cli
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.providers.claude_agent import ClaudeAgentProvider
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

AUDIT_FLAG = "--audit-log"
ERROR = "review-bot: error:"
REDACTED = "[REDACTED]"
QUOTES_THE_DIFF = "[removed: quotes the diff]"
CLI_SENTINEL = "CLI-SENTINEL: the CLI wrote this line"
CANNED_NOTICE = "review-bot: warning: MockProvider output is canned, not a real review."
KEPT_LINES = 20
KEPT_CHARACTERS = 2_000
HELD_LINES = 1_000
CREDENTIAL_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
KEY_SENTINEL = "test-key-sentinel"
PROVIDERS_DIR = REPO_ROOT / "src" / "review_bot" / "providers"
GOLDEN = REPO_ROOT / "tests" / "fixtures" / "p1_s5"
# Generous bounds: they catch a hang or quadratic work, not a slow CI machine.
BOUND_S = 30.0
# Tighter, for the one case the reviewer measured at about 10 s before the fix.
SCRUB_BOUND_S = 5.0

ALNUM = string.ascii_letters + string.digits
UPPER_DIGITS = string.ascii_uppercase + string.digits


def rate_limit_warning() -> str:
    """P0-S4 AC6's warning for `rate_limit`, as Phase 0 wrote it (AC1: unchanged)."""
    return (GOLDEN / "rate_limit_warning.stderr.golden.txt").read_text("utf-8").removesuffix("\n")


def scrub_diagnostics(lines: Sequence[str], *, diff_text: str, environ: dict[str, str]) -> Any:
    audit = importlib.import_module("review_bot.audit")
    return audit.scrub_diagnostics(lines, diff_text=diff_text, environ=environ)


# Secrets, assembled so the source holds none.


def chars(alphabet: str, count: int, seed: int = 0) -> str:
    return "".join(alphabet[(seed + 7 * index) % len(alphabet)] for index in range(count))


def aws_key(seed: int = 0) -> str:
    return "AK" + "IA" + chars(UPPER_DIGITS, 16, seed)


def github_token(seed: int = 0) -> str:
    return "gh" + "p_" + chars(ALNUM, 36, seed)


def anthropic_key(seed: int = 0) -> str:
    return "sk" + "-ant-" + chars(ALNUM + "_-", 24, seed)


PEM_LABEL = "RSA " + "PRIVATE" + " KEY"


def pem_block(body_lines: int, seed: int = 0) -> list[str]:
    """A private-key block, one CLI line per PEM line: its BEGIN line, body, and END line."""
    body = [chars(ALNUM + "+/", 64, seed + 11 * index) for index in range(body_lines)]
    return ["-----BEGIN " + PEM_LABEL + "-----", *body, "-----END " + PEM_LABEL + "-----"]


def fragments(value: str, width: int = 8) -> list[str]:
    """Every `width`-character piece of `value`: none may survive, not just the whole."""
    return [value[start : start + width] for start in range(len(value) - width + 1)]


# Reading the log.


def kept_diagnostics(log_path: Path) -> list[str]:
    """The one list of strings in `agent.failed.data`: the scrubbed tail the log kept."""
    data = only(read_records(log_path), "agent.failed")
    lists = [items for items in entry_lists(data) if all(type(item) is str for item in items)]
    assert len(lists) == 1, f"{len(lists)} lists of strings in agent.failed: {data}"
    return lists[0]


def warning_texts(log_path: Path) -> list[Any]:
    records = read_records(log_path)
    return [
        value
        for record in records
        if (record["stage"], record["event"]) == ("agent", "warning")
        for value in scalars(record["data"])
    ]


def log_text(log_path: Path) -> str:
    """The log as written, and once more with its JSON escapes decoded."""
    raw = log_path.read_text(encoding="ascii")
    decoded = "\n".join(
        str(value) for record in read_records(log_path) for value in scalars(record)
    )
    return raw + "\n" + decoded


# Running the bot.


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "audit.jsonl"


@pytest.fixture
def claude_logged(bot: Any, write_diff: Callable[..., Path], log_path: Path) -> Callable[..., Any]:
    """Run `main` with `--provider claude` over a fake `sdk_call`, logging to `log_path`."""

    def review(sdk_call: Any, *flags: str, diff: str = MINIMAL_DIFF) -> Any:
        argv = [str(write_diff(diff)), "--provider", "claude", AUDIT_FLAG, str(log_path), *flags]
        _, settings = cli.parse_args(argv)
        return bot.run(*argv, provider=ClaudeAgentProvider(settings, sdk_call=sdk_call))

    return review


@pytest.fixture
def mock_logged(bot: Any, write_diff: Callable[..., Path], log_path: Path) -> Callable[..., Any]:
    def review(provider: Any, diff: str = MINIMAL_DIFF) -> Any:
        return bot.review(write_diff(diff), AUDIT_FLAG, str(log_path), provider=provider)

    return review


def within(seconds: float, action: Callable[[], Any]) -> Any:
    """Run `action` on a thread, so one that never returns fails the test instead of hanging."""
    outcome: list[Any] = []
    thread = threading.Thread(target=lambda: outcome.append(action()), daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"still running after {seconds} seconds"
    assert outcome, "raised instead of returning"
    return outcome[0]


def provider_failure(claude_provider: Any, *script: Any) -> ProviderError:
    provider = claude_provider(FakeSdk(*script))
    try:
        asyncio.run(provider.review(REQUEST))
    except ProviderError as error:
        return error
    raise AssertionError("the review returned instead of raising a ProviderError")


# --- AC1 ------------------------------------------------------------------------------------------

FOUND_OR_NONE = pytest.mark.parametrize(
    ("result", "code"),
    [(findings_json(HIGH_FINDING), 1), (findings_json(), 0)],
    ids=["found", "none"],
)


@FOUND_OR_NONE
def test_p1_s5_ac1_overridden_signal_warns_on_stderr_as_in_phase_0(claude_logged, result, code):
    run = claude_logged(FakeSdk(assistant_error("rate_limit"), result_message(result)))

    assert run.exit_code == code, run.stderr
    assert ("src/app.py" in run.stdout) == (code == 1)
    assert run.stderr.splitlines().count(rate_limit_warning()) == 1, run.stderr


@FOUND_OR_NONE
def test_p1_s5_ac1_overridden_signal_is_logged_as_agent_warning(
    claude_logged, log_path, result, code
):
    run = claude_logged(FakeSdk(assistant_error("rate_limit"), result_message(result)))

    assert run.exit_code == code, run.stderr
    assert rate_limit_warning() in warning_texts(log_path)


def test_p1_s5_ac1_response_warnings_hold_the_warning(claude_provider):
    fake = FakeSdk(assistant_error("rate_limit"), result_message(findings_json(HIGH_FINDING)))

    response = asyncio.run(claude_provider(fake).review(REQUEST))

    assert tuple(response.warnings) == (rate_limit_warning(),)
    assert len(response.findings) == 1


def test_p1_s5_ac1_no_signal_gives_no_response_warning(claude_provider):
    fake = FakeSdk(result_message(findings_json(HIGH_FINDING)))

    response = asyncio.run(claude_provider(fake).review(REQUEST))

    assert tuple(response.warnings) == ()


def provider_lines(stderr: str) -> list[str]:
    """Stderr without Phase 0's own lines: the mock's canned notice and the summary."""
    return [line for line in stderr.splitlines()[:-1] if line != CANNED_NOTICE]


def test_p1_s5_ac1_limits_signal_with_a_lone_surrogate_still_exits_by_the_findings(claude_logged):
    # The signal's name is the CLI's JSON, which can spell a lone surrogate as an escape.
    signal = "rate_limit" + chr(0xDCFF)
    fake = FakeSdk(assistant_error(signal), result_message(findings_json(HIGH_FINDING)))

    run = claude_logged(fake)

    assert run.exit_code == 1, run.stderr
    assert "src/app.py" in run.stdout


# --- AC2 ------------------------------------------------------------------------------------------


def test_p1_s5_ac2_mock_warning_is_a_stderr_line_and_an_agent_warning(mock_logged, log_path):
    run = mock_logged(MockProvider(findings=[], warnings=("W",)))

    assert run.exit_code == 0, run.stderr
    assert "W" in run.stderr.splitlines(), run.stderr
    assert "W" in warning_texts(log_path)


def test_p1_s5_ac2_each_mock_warning_is_written_in_order(mock_logged, log_path):
    run = mock_logged(MockProvider(findings=[], warnings=("W-ONE", "W-TWO")))

    lines = run.stderr.splitlines()
    assert lines.index("W-ONE") < lines.index("W-TWO"), run.stderr
    logged = warning_texts(log_path)
    assert "W-ONE" in logged
    assert "W-TWO" in logged


def test_p1_s5_ac2_no_warnings_means_no_line_and_no_record(mock_logged, log_path):
    run = mock_logged(MockProvider(findings=[]))

    assert run.exit_code == 0, run.stderr
    assert provider_lines(run.stderr) == [], run.stderr
    assert "agent.warning" not in events(read_records(log_path))


def test_p1_s5_ac2_warning_goes_to_stderr_not_stdout(mock_logged):
    run = mock_logged(MockProvider(findings=[], warnings=("W-SENTINEL",)))

    assert "W-SENTINEL" not in run.stdout


def test_p1_s5_ac2_hostile_warning_stays_one_safe_line(mock_logged):
    # Beyond the criterion: stderr stays as safe as today (Value), whatever a provider returns.
    hostile = "W\x1b[31m\nreview-bot: error: forged\u2028tail"

    run = mock_logged(MockProvider(findings=[], warnings=(hostile,)))

    assert run.exit_code == 0, run.stderr
    assert raw_controls(run.stderr.replace("\n", "")) == [], repr(run.stderr)
    assert not [line for line in run.stderr.splitlines() if line.startswith(ERROR)], run.stderr
    assert len(provider_lines(run.stderr)) == 1, repr(run.stderr)


def test_p1_s5_ac2_logged_warning_is_scrubbed(mock_logged, log_path):
    # design.md's audit table: `warning` holds scrubbed text.
    value = aws_key(seed=4)

    mock_logged(MockProvider(findings=[], warnings=(f"W {value}",)))

    assert value not in log_text(log_path)
    assert "agent.warning" in events(read_records(log_path))


# --- AC3 ------------------------------------------------------------------------------------------


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


@FOUND_OR_NONE
def test_p1_s5_ac3_claude_warning_with_stderr_lost_stays_in_the_log(
    claude_logged, log_path, lose_stderr, result, code
):
    fake = FakeSdk(assistant_error("rate_limit"), result_message(result))
    lose_stderr()

    run = claude_logged(fake)

    assert run.exit_code == code
    assert ("src/app.py" in run.stdout) == (code == 1)
    assert run.stderr == ""
    assert rate_limit_warning() in warning_texts(log_path)


def test_p1_s5_ac3_mock_warning_with_stderr_lost_stays_in_the_log(
    mock_logged, log_path, lose_stderr
):
    lose_stderr()

    run = mock_logged(MockProvider(warnings=("W-SENTINEL",)))

    assert run.exit_code == 1
    assert "mock/example.py" in run.stdout
    assert "W-SENTINEL" in warning_texts(log_path)


# --- AC4 ------------------------------------------------------------------------------------------

STREAMS = frozenset({"stdout", "stderr", "__stdout__", "__stderr__"})


def provider_sources() -> list[Path]:
    sources = sorted(PROVIDERS_DIR.glob("*.py"))
    assert sources, f"no sources under {PROVIDERS_DIR}"
    return sources


def stream_uses(source: Path) -> list[str]:
    """Each import of `console`, `print` call, or `sys` stream reference in `source`."""
    found = []
    for node in ast.walk(ast.parse(source.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if "console" in alias.name]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [alias.name for alias in node.names]
            if "console" in module or "console" in names:
                found.append(f"from {module} import {names}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            found += ["print()"] if node.func.id == "print" else []
        elif isinstance(node, ast.Attribute) and node.attr in STREAMS:
            found += [f"sys.{node.attr}"] if ast.unparse(node.value) == "sys" else []
    return found


@pytest.mark.parametrize("source", provider_sources(), ids=lambda path: path.name)
def test_p1_s5_ac4_no_provider_source_imports_console_or_touches_a_stream(source):
    assert stream_uses(source) == []


def test_p1_s5_ac4_importing_the_providers_doesnt_load_console():
    probe = (
        "import sys, review_bot.providers.base, review_bot.providers.mock,"
        " review_bot.providers.claude_agent;"
        "sys.exit(3 if 'review_bot.console' in sys.modules else 0)"
    )

    completed = subprocess.run(  # noqa: S603 - fixed arguments, our own interpreter
        [sys.executable, "-c", probe],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "script",
    [
        (cli_stderr(CLI_SENTINEL), assistant_error("rate_limit"), result_message(findings_json())),
        (cli_stderr(CLI_SENTINEL), error_result(401, "api_error")),
        (cli_stderr(CLI_SENTINEL), raised_result_error(401)),
    ],
    ids=["warned_success", "error_result", "raised_result_error"],
)
def test_p1_s5_ac4_claude_review_writes_to_neither_stream(claude_provider, capfd, script):
    provider = claude_provider(FakeSdk(*script))
    capfd.readouterr()

    with contextlib.suppress(ProviderError):
        asyncio.run(provider.review(REQUEST))

    assert capfd.readouterr() == ("", "")


def test_p1_s5_ac4_mock_review_with_warnings_writes_to_neither_stream(capfd):
    provider = MockProvider(warnings=("W-SENTINEL",))
    capfd.readouterr()

    asyncio.run(provider.review(REQUEST))

    assert capfd.readouterr() == ("", "")


# --- AC5 ------------------------------------------------------------------------------------------

FAILED_CALLS = {
    "result_error": ((raised_result_error(401),), (), 8),
    "error_result": ((error_result(401, "api_error"),), (), 8),
    "deadline": ((hang,), ("--deadline-s", "0.1"), 11),
}


@pytest.mark.parametrize(("tail", "flags", "code"), FAILED_CALLS.values(), ids=FAILED_CALLS.keys())
def test_p1_s5_ac5_failed_calls_cli_line_is_in_agent_failed(
    claude_logged, log_path, tail, flags, code
):
    fake = FakeSdk(cli_stderr("first CLI line", CLI_SENTINEL), *tail)

    run = within(BOUND_S, lambda: claude_logged(fake, *flags))

    assert run.exit_code == code, run.stderr
    assert CLI_SENTINEL in kept_diagnostics(log_path)


@pytest.mark.parametrize(("tail", "flags", "code"), FAILED_CALLS.values(), ids=FAILED_CALLS.keys())
def test_p1_s5_ac5_failed_calls_cli_line_reaches_neither_stream(claude_logged, tail, flags, code):
    fake = FakeSdk(cli_stderr(CLI_SENTINEL), *tail)

    run = within(BOUND_S, lambda: claude_logged(fake, *flags))

    assert run.exit_code == code, run.stderr
    assert "CLI-SENTINEL" not in run.stdout
    assert "CLI-SENTINEL" not in run.stderr
    assert run.stdout == ""
    assert_one_error_line(run.stderr)


@pytest.mark.parametrize(("tail", "flags", "code"), FAILED_CALLS.values(), ids=FAILED_CALLS.keys())
def test_p1_s5_ac5_cli_output_doesnt_change_the_code_or_message(claude_logged, tail, flags, code):
    quiet = within(BOUND_S, lambda: claude_logged(FakeSdk(*tail), *flags))
    noisy = within(BOUND_S, lambda: claude_logged(FakeSdk(cli_stderr(CLI_SENTINEL), *tail), *flags))

    assert quiet.exit_code == noisy.exit_code == code
    assert noisy.stderr == quiet.stderr


def test_p1_s5_ac5_provider_error_carries_the_raw_cli_lines(claude_provider):
    error = provider_failure(
        claude_provider, cli_stderr("first CLI line", CLI_SENTINEL), error_result(401, "api_error")
    )

    assert error.category is ErrorCategory.AUTH
    assert list(error.diagnostics)[-2:] == ["first CLI line", CLI_SENTINEL]


def test_p1_s5_ac5_cli_lines_are_kept_in_the_order_written(claude_logged, log_path):
    lines = [f"cli line {index:02d} end" for index in range(5)]

    claude_logged(FakeSdk(cli_stderr(*lines), error_result(401, "api_error")))

    assert kept_diagnostics(log_path) == lines


# --- AC6 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        (cli_stderr(CLI_SENTINEL), result_message(findings_json(HIGH_FINDING))),
        (cli_stderr(CLI_SENTINEL), assistant_error("rate_limit"), result_message(findings_json())),
        (result_message(findings_json()), cli_stderr(CLI_SENTINEL)),
    ],
    ids=["findings", "warned", "after_the_result"],
)
def test_p1_s5_ac6_successful_call_logs_no_cli_text(claude_logged, log_path, script):
    run = claude_logged(FakeSdk(*script))

    assert run.exit_code in {0, 1}, run.stderr
    assert "CLI-SENTINEL" not in log_text(log_path)
    assert "agent.failed" not in events(read_records(log_path))
    assert "CLI-SENTINEL" not in run.stdout + run.stderr


# --- AC7 ------------------------------------------------------------------------------------------


def failed_with(claude_logged: Any, lines: Sequence[str], diff: str = MINIMAL_DIFF) -> Any:
    fake = FakeSdk(cli_stderr(*lines), error_result(401, "api_error"))
    run = within(BOUND_S, lambda: claude_logged(fake, diff=diff))
    assert run.exit_code == 8, run.stderr
    return run


def test_p1_s5_ac7_50_lines_keep_only_the_last_20(claude_logged, log_path):
    lines = [f"cli line {index:02d} end" for index in range(50)]

    failed_with(claude_logged, lines)

    assert kept_diagnostics(log_path) == lines[-KEPT_LINES:]
    text = log_text(log_path)
    assert not [line for line in lines[:-KEPT_LINES] if line in text]


def test_p1_s5_ac7_exactly_20_lines_are_all_kept(claude_logged, log_path):
    lines = [f"cli line {index:02d} end" for index in range(KEPT_LINES)]

    failed_with(claude_logged, lines)

    assert kept_diagnostics(log_path) == lines


@pytest.mark.parametrize(
    "lines",
    [["." * 10_000], ["." * 10_000] * 50, [f"{index:02d}" + "." * 300 for index in range(20)]],
    ids=["one_long_line", "fifty_long_lines", "twenty_302_character_lines"],
)
def test_p1_s5_ac7_total_kept_is_at_most_2000_characters(claude_logged, log_path, lines):
    failed_with(claude_logged, lines)

    assert sum(len(line) for line in kept_diagnostics(log_path)) <= KEPT_CHARACTERS


# Where the cap falls in one 10,000-character line: 2,000 from the end or the start of what is
# kept, or 100 from a line's end if the cap is shared out per line. The key straddles each.
SPLIT_AT = {"tail_cap": 10_000 - 2_000 - 10, "head_cap": 2_000 - 10, "per_line_cap": 10_000 - 110}


@pytest.mark.parametrize("start", SPLIT_AT.values(), ids=SPLIT_AT.keys())
def test_p1_s5_ac7_secret_spanning_the_cap_leaves_no_part(claude_logged, log_path, start):
    value = aws_key(seed=9)
    line = "." * start + value
    line += "." * (10_000 - len(line))

    failed_with(claude_logged, [line])

    text = log_text(log_path)
    assert not [piece for piece in fragments(value) if piece in text]


def test_p1_s5_ac7_pem_block_longer_than_the_cap_leaves_no_part(claude_logged, log_path):
    block = pem_block(body_lines=40, seed=2)
    assert sum(map(len, block)) > KEPT_CHARACTERS
    assert len(block) > KEPT_LINES

    failed_with(claude_logged, ["CLI starting", *block, "CLI exiting"])

    text = log_text(log_path)
    assert "PRIVATE" not in text
    assert not [piece for body in block[1:-1] for piece in fragments(body) if piece in text]


def test_p1_s5_interfaces_provider_holds_at_most_the_last_1000_lines(claude_provider):
    lines = [f"cli line {index:05d}" for index in range(5_000)]

    error = provider_failure(claude_provider, cli_stderr(*lines), error_result(401, "api_error"))

    held = list(error.diagnostics)
    assert KEPT_LINES <= len(held) <= HELD_LINES
    assert held == lines[-len(held) :]


def test_p1_s5_ac7_limits_200000_cli_lines_are_bounded_in_time_and_size(claude_logged, log_path):
    lines = [f"cli line {index:06d} " + "." * 40 for index in range(200_000)]
    started = time.monotonic()

    failed_with(claude_logged, lines)

    assert time.monotonic() - started < BOUND_S
    kept = kept_diagnostics(log_path)
    assert len(kept) <= KEPT_LINES
    assert kept[-1] == lines[-1]


# --- AC8 ------------------------------------------------------------------------------------------


def secret_cases() -> dict[str, tuple[list[str], list[str]]]:
    """Name: (CLI lines, values that must not be kept)."""
    aws, github, anthropic = aws_key(seed=1), github_token(seed=2), anthropic_key(seed=3)
    split = pem_block(body_lines=3, seed=5)
    tail = pem_block(body_lines=3, seed=6)[1:]
    return {
        "aws_key": ([f"token {aws} was rejected"], [aws]),
        "github_token": ([f"remote said {github}"], [github]),
        "anthropic_key": ([f"key={anthropic}"], [anthropic]),
        "pem_split_over_lines": (["CLI starting", *split], split[1:-1]),
        "pem_end_line_and_body_only": (["CLI starting", *tail], tail[:-1]),
    }


SECRET_CASES = secret_cases()


@pytest.mark.parametrize(("lines", "values"), SECRET_CASES.values(), ids=SECRET_CASES.keys())
def test_p1_s5_ac8_secret_in_cli_lines_is_redacted_in_the_log(
    claude_logged, log_path, lines, values
):
    failed_with(claude_logged, lines)

    text = log_text(log_path)
    assert REDACTED in "\n".join(kept_diagnostics(log_path))
    assert not [value for value in values if value in text]


@pytest.mark.parametrize(("lines", "values"), SECRET_CASES.values(), ids=SECRET_CASES.keys())
def test_p1_s5_ac8_scrub_diagnostics_redacts_the_secret(lines, values):
    scrubbed = scrub_diagnostics(lines, diff_text=MINIMAL_DIFF, environ={})

    joined = "\n".join(scrubbed)
    assert all(type(line) is str for line in scrubbed)
    assert REDACTED in joined
    assert not [value for value in values if value in joined]


@pytest.mark.parametrize("variable", CREDENTIAL_VARIABLES)
def test_p1_s5_ac8_credential_value_from_the_environment_is_redacted_in_the_log(
    claude_logged, log_path, monkeypatch, variable
):
    monkeypatch.setenv(variable, KEY_SENTINEL)

    failed_with(claude_logged, [f"auth with {KEY_SENTINEL} failed"])

    assert KEY_SENTINEL not in log_text(log_path)
    assert REDACTED in "\n".join(kept_diagnostics(log_path))


@pytest.mark.parametrize("variable", CREDENTIAL_VARIABLES)
def test_p1_s5_ac8_scrub_diagnostics_redacts_a_credential_value(variable):
    scrubbed = scrub_diagnostics(
        [f"auth with {KEY_SENTINEL} failed"],
        diff_text=MINIMAL_DIFF,
        environ={variable: KEY_SENTINEL},
    )

    assert KEY_SENTINEL not in "\n".join(scrubbed)
    assert REDACTED in "\n".join(scrubbed)


@pytest.mark.parametrize("variable", CREDENTIAL_VARIABLES)
def test_p1_s5_ac8_empty_credential_value_leaves_the_line_unchanged(variable):
    line = "CLI-SENTINEL: plain line"

    scrubbed = scrub_diagnostics([line], diff_text=MINIMAL_DIFF, environ={variable: ""})

    assert tuple(scrubbed) == (line,)


@pytest.mark.parametrize("variable", CREDENTIAL_VARIABLES)
def test_p1_s5_ac8_empty_credential_in_the_environment_leaves_the_log_line_unchanged(
    claude_logged, log_path, monkeypatch, variable
):
    monkeypatch.setenv(variable, "")

    failed_with(claude_logged, [CLI_SENTINEL])

    assert kept_diagnostics(log_path) == [CLI_SENTINEL]


def test_p1_s5_ac8_credential_values_are_never_written_to_the_log(
    claude_logged, log_path, monkeypatch
):
    # Notes: scrubbing reads the variables and never stores or logs them.
    for index, variable in enumerate(CREDENTIAL_VARIABLES):
        monkeypatch.setenv(variable, f"{KEY_SENTINEL}-{index}")

    failed_with(claude_logged, [CLI_SENTINEL])

    assert KEY_SENTINEL not in log_text(log_path)


# --- AC9 ------------------------------------------------------------------------------------------

CONTEXT_LINE = "    result = load_config(path)"
REMOVED_LINE = "    legacy = old_handler(event)"
ADDED_LINE = "    total = compute_total(items)"
QUOTED_LINE = '    name = "report-name"'
BACKSLASH_LINE = "    windows_root = C:\\Users\\data"
SHORT_LINE = "x = 1"
EIGHT_LINE = "total+=1"
# 7 characters: 8 with its marker, 10 with its outer whitespace. Spec Notes count without both.
SEVEN_LINE = "total=1"
SEVEN_DIFF_LINE = f"+  {SEVEN_LINE} "
BLANK_LINE = " " * 12
AC9_DIFF = (
    "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
    "@@ -1,2 +1,9 @@\n"
    f" {CONTEXT_LINE}\n-{REMOVED_LINE}\n+{ADDED_LINE}\n+{QUOTED_LINE}\n+{BACKSLASH_LINE}\n"
    f"+{SHORT_LINE}\n+{EIGHT_LINE}\n+{BLANK_LINE}\n+\t\t\t\t\t\t\t\t\n{SEVEN_DIFF_LINE}\n"
)
DIFF_WORDS = ("load_config", "old_handler", "compute_total", "report-name", "windows_root")

QUOTING_LINES = {
    "added_without_marker": f"tool input: {ADDED_LINE} (end)",
    "removed_without_marker": f"tool input: {REMOVED_LINE} (end)",
    "context_without_marker": f"saw:{CONTEXT_LINE}!",
    "with_its_marker": f"saw: +{ADDED_LINE}",
    "without_leading_whitespace": f"saw:{ADDED_LINE.strip()}!",
    "removed_without_leading_whitespace": f"saw:{REMOVED_LINE.strip()}!",
    "json_escaped_quotes": json.dumps({"input": QUOTED_LINE}),
    "json_escaped_quotes_stripped": json.dumps({"input": QUOTED_LINE.strip()}),
    "json_escaped_backslashes": json.dumps({"input": BACKSLASH_LINE}),
    "json_escaped_newlines": json.dumps({"input": f"{ADDED_LINE}\n{CONTEXT_LINE}\n"}),
    "exactly_8_characters": f"saw {EIGHT_LINE} here",
}


@pytest.mark.parametrize("line", QUOTING_LINES.values(), ids=QUOTING_LINES.keys())
def test_p1_s5_ac9_cli_line_quoting_the_diff_is_replaced_whole(line):
    scrubbed = scrub_diagnostics([line], diff_text=AC9_DIFF, environ={})

    assert tuple(scrubbed) == (QUOTES_THE_DIFF,)


@pytest.mark.parametrize("line", QUOTING_LINES.values(), ids=QUOTING_LINES.keys())
def test_p1_s5_ac9_cli_line_quoting_the_diff_is_replaced_whole_in_the_log(
    claude_logged, log_path, line
):
    failed_with(claude_logged, ["CLI starting", line], diff=AC9_DIFF)

    assert kept_diagnostics(log_path) == ["CLI starting", QUOTES_THE_DIFF]
    text = log_text(log_path)
    assert not [word for word in DIFF_WORDS if word in text]
    assert EIGHT_LINE not in text


@pytest.mark.parametrize(
    "line",
    [
        f"CLI-SENTINEL: {SHORT_LINE} is fine",
        f"CLI-SENTINEL: {SEVEN_LINE} is fine",
        f"CLI-SENTINEL: saw {SEVEN_DIFF_LINE}!",
        "CLI-SENTINEL: plain words",
        "+x",
        "app.py",
    ],
    ids=[
        "short_diff_line",
        "seven_characters",
        "seven_characters_with_marker_and_whitespace",
        "plain",
        "minimal_added_line",
        "path",
    ],
)
def test_p1_s5_ac9_diff_line_under_8_characters_isnt_matched(line):
    assert tuple(scrub_diagnostics([line], diff_text=AC9_DIFF, environ={})) == (line,)


def test_p1_s5_ac9_short_diff_line_is_kept_in_the_log(claude_logged, log_path):
    line = f"CLI-SENTINEL: {SHORT_LINE} is fine"

    failed_with(claude_logged, [line], diff=AC9_DIFF)

    assert kept_diagnostics(log_path) == [line]


def test_p1_s5_ac9_limits_whitespace_only_diff_lines_match_nothing():
    # Limits: a blank or whitespace-only diff line, stripped, is empty and under 8 characters.
    lines = ["CLI-SENTINEL", "a b c d e f g h", "\t\t"]

    assert tuple(scrub_diagnostics(lines, diff_text=AC9_DIFF, environ={})) == tuple(lines)


def test_p1_s5_ac9_only_the_quoting_line_is_replaced():
    lines = ["before", f"tool input: {ADDED_LINE}", "after"]

    scrubbed = scrub_diagnostics(lines, diff_text=AC9_DIFF, environ={})

    assert tuple(scrubbed) == ("before", QUOTES_THE_DIFF, "after")


def test_p1_s5_ac9_limits_large_diff_and_many_cli_lines_scrub_in_bounded_time():
    body = "".join(
        f"+    value_{index:06d} = compute(item_{index:06d})\n" for index in range(8_000)
    )
    diff = f"diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n@@ -0,0 +1,8000 @@\n{body}"
    lines = [f"cli line {index:04d} " + "." * 200 for index in range(HELD_LINES)]
    started = time.monotonic()

    scrubbed = scrub_diagnostics(lines, diff_text=diff, environ={})

    assert time.monotonic() - started < BOUND_S
    assert QUOTES_THE_DIFF not in scrubbed
    assert REDACTED not in "\n".join(scrubbed)


def test_p1_s5_ac9_limits_large_diff_and_long_cli_lines_add_bounded_time_to_a_failed_call(
    claude_logged, log_path
):
    # Both sizes at once, end to end: about 400 KB of distinct diff lines, and 20 CLI lines of
    # 20,000 characters, each full of near misses that share a prefix with many diff lines.
    # The bound is on the time the CLI lines add, so the parse of the diff doesn't count.
    count = 9_000
    body = "".join(
        f"+    value_{index:06d} = compute(item_{index:06d})\n" for index in range(count)
    )
    diff = (
        f"diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n@@ -0,0 +1,{count} @@\n{body}"
    )
    assert 350_000 < len(diff.encode("utf-8")) < 400_000  # under the default --max-diff-bytes
    lines = [
        (f"value_{index:04d} = compute(item_{index:04d} " * 600)[:20_000] for index in range(20)
    ]

    def failed_run(cli_lines: Sequence[str]) -> float:
        started = time.monotonic()
        failed_with(claude_logged, cli_lines, diff=diff)
        return time.monotonic() - started

    quiet = failed_run(["CLI-SENTINEL"])
    log_path.unlink()  # so the log holds only the run under test
    noisy = failed_run(lines)

    assert noisy - quiet < SCRUB_BOUND_S, (
        f"{noisy:.1f} s with the long lines, {quiet:.1f} s without"
    )
    assert QUOTES_THE_DIFF not in kept_diagnostics(log_path)


def test_p1_s5_ac9_limits_long_hostile_cli_line_scrubs_in_bounded_time():
    lines = ["\\" * 200_000, '\\"' * 100_000, "-----" * 40_000, "a" * 200_000]
    started = time.monotonic()

    scrub_diagnostics(lines, diff_text=AC9_DIFF, environ={})

    assert time.monotonic() - started < BOUND_S


# --- AC10 -----------------------------------------------------------------------------------------

MOCK_DIAGNOSTIC = "MOCK-DIAGNOSTIC-SENTINEL d"


def test_p1_s5_ac10_mock_diagnostics_reach_the_log_not_stderr(mock_logged, log_path):
    error = ProviderError(ErrorCategory.AUTH, "m", diagnostics=(MOCK_DIAGNOSTIC,))

    run = mock_logged(MockProvider(error=error))

    assert run.exit_code == 8, run.stderr
    assert kept_diagnostics(log_path) == [MOCK_DIAGNOSTIC]
    assert "MOCK-DIAGNOSTIC-SENTINEL" not in run.stderr
    assert "MOCK-DIAGNOSTIC-SENTINEL" not in run.stdout
    assert_one_error_line(run.stderr)


def test_p1_s5_ac10_spec_literal_d_is_logged(mock_logged, log_path):
    error = ProviderError(ErrorCategory.AUTH, "m", diagnostics=("d",))

    run = mock_logged(MockProvider(error=error))

    assert run.exit_code == 8, run.stderr
    assert kept_diagnostics(log_path) == ["d"]
    assert "d" not in run.stderr.splitlines()


def test_p1_s5_ac10_mock_diagnostics_are_scrubbed_by_the_orchestrator(mock_logged, log_path):
    # ADR 0004: providers hand over raw text; the scrub is the audit side's, whichever provider.
    value = aws_key(seed=7)
    raw = (f"token {value}", f"tool input: {ADDED_LINE}")
    error = ProviderError(ErrorCategory.AUTH, "m", diagnostics=raw)

    mock_logged(MockProvider(error=error), diff=AC9_DIFF)

    kept = kept_diagnostics(log_path)
    assert value not in log_text(log_path)
    assert REDACTED in kept[0]
    assert kept[1] == QUOTES_THE_DIFF


def test_p1_s5_ac10_hostile_diagnostics_keep_the_log_one_record_per_line(mock_logged, log_path):
    raw = ("line\nbreak", "esc\x1b[31m", "sep\u2028x\x85y")
    error = ProviderError(ErrorCategory.AUTH, "m", diagnostics=raw)

    run = mock_logged(MockProvider(error=error))

    assert run.exit_code == 8, run.stderr
    assert kept_diagnostics(log_path)  # `read_records` checks one ASCII record per line
    assert raw_controls(run.stderr.removesuffix("\n")) == []


def test_p1_s5_ac10_beyond_criterion_lone_surrogate_in_diagnostics_is_escaped(
    mock_logged, log_path
):
    # Beyond the criterion: design.md states "no lone surrogates" for paths; a CLI line decoded
    # with `surrogateescape` could carry one too. Flagged for the designer if this fails.
    error = ProviderError(ErrorCategory.AUTH, "m", diagnostics=("bad" + chr(0xDCFF) + "byte",))

    run = mock_logged(MockProvider(error=error))

    assert run.exit_code == 8, run.stderr
    assert len(kept_diagnostics(log_path)) == 1


def test_p1_s5_ac10_message_is_still_shown_as_today(mock_logged):
    plain = mock_logged(MockProvider(error=ProviderError(ErrorCategory.AUTH, "m")))
    with_diagnostics = mock_logged(
        MockProvider(error=ProviderError(ErrorCategory.AUTH, "m", diagnostics=(MOCK_DIAGNOSTIC,)))
    )

    assert with_diagnostics.exit_code == plain.exit_code == 8
    assert re.sub(r"run_id=\S+", "", with_diagnostics.stderr) == re.sub(
        r"run_id=\S+", "", plain.stderr
    )
