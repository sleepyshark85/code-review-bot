"""P0-S1 AC1 to AC5: the command, its output, settings and usage errors.

P0-S4 AC13 replaced AC6 and the "neither" case of AC4 for `--provider`: see
`test_p0_s4_messages.py`. P1-S6 AC11 replaced AC1's exit 1 and canned stdout: the canned finding's
anchor isn't in any diff, so it's dropped (`test_p1_s6_verify_anchors.py`). Since P1-S6, a test
that needs a finding to print injects one over a diff holding its anchor.
"""

import itertools
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from diff_support import FINDING_DIFF, FINDING_LINE, MINIMAL_DIFF, section
from repo_paths import REPO_ROOT

pytestmark = pytest.mark.integration

SEVERITIES_ASCENDING = ("low", "medium", "high", "critical")
SUMMARY = "provider=mock model=claude-opus-5-5 prompt=p0-v1 input_tokens=0 output_tokens=0"
SENTINEL = "SENTINEL-DIFF-TEXT"
SENTINEL_DIFF = section("app.py", f"+{SENTINEL}\n")
CANNED_NOTICE = "review-bot: warning: MockProvider output is canned, not a real review."
NO_FINDINGS_LOW = "No findings at or above low."


def seven_line_diff(*paths: str) -> str:
    """A diff adding lines 1 to 7 to each path, so `bot.finding(file=path)` is anchored."""
    return "".join(section(path, "+x\n" * 7) for path in paths)


def high_on_the_sample(bot):
    """A `high` finding on an added line of the sample diff, as the canned one was before P1-S6."""
    return bot.mock(findings=[bot.finding(file="billing/invoice.py", line=14)])


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int
    stdout_body: str
    stderr: str


@pytest.fixture(scope="module")
def real_command() -> CommandResult:
    """Run the documented command once, as a user would, from the repository root."""
    completed = subprocess.run(
        [sys.executable, "-m", "review_bot", "Assignment/sample_diff.txt", "--provider", "mock"],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    stdout_body = completed.stdout.removesuffix("\n")
    return CommandResult(completed.returncode, stdout_body, completed.stderr)


def test_p0_s1_ac1_replaced_by_p1_s6_ac11_command_exits_0(real_command):
    assert real_command.exit_code == 0, real_command.stderr


def test_p0_s1_ac1_replaced_by_p1_s6_ac11_canned_finding_is_dropped(real_command):
    assert real_command.stdout_body == NO_FINDINGS_LOW


def test_p0_s1_ac1_stderr_warns_mock_output_is_canned_and_not_a_real_review(real_command):
    stderr = real_command.stderr.lower()

    assert "mockprovider" in stderr
    assert "canned" in stderr
    assert "not a real review" in stderr


def test_p0_s1_ac2_threshold_critical_exits_0(bot, sample_diff):
    run = bot.review(sample_diff, "--threshold", "critical", provider=high_on_the_sample(bot))

    assert run.exit_code == 0, run.stderr


def test_p0_s1_ac2_threshold_critical_stdout_is_exactly_no_findings(bot, sample_diff, golden):
    run = bot.review(sample_diff, "--threshold", "critical", provider=high_on_the_sample(bot))

    assert run.stdout_body == golden("no_findings_critical.golden.txt")


@pytest.mark.parametrize(
    ("severity", "threshold"), list(itertools.product(SEVERITIES_ASCENDING, SEVERITIES_ASCENDING))
)
def test_p0_s1_ac2_finding_is_kept_only_at_or_above_threshold(bot, write_diff, severity, threshold):
    provider = bot.mock(findings=[bot.finding(severity=severity)])
    is_kept = SEVERITIES_ASCENDING.index(severity) >= SEVERITIES_ASCENDING.index(threshold)

    run = bot.review(write_diff(FINDING_DIFF), "--threshold", threshold, provider=provider)

    assert run.exit_code == (1 if is_kept else 0)
    if is_kept:
        assert f"[{severity}/logic]" in run.stdout_body
    else:
        assert run.stdout_body == f"No findings at or above {threshold}."


def test_p0_s1_ac2_only_findings_at_or_above_threshold_are_printed(bot, write_diff):
    provider = bot.mock(
        findings=[
            bot.finding(severity="low", file="low.py"),
            bot.finding(severity="critical", file="critical.py"),
        ]
    )

    diff = write_diff(seven_line_diff("low.py", "critical.py"))

    run = bot.review(diff, "--threshold", "high", provider=provider)

    assert run.exit_code == 1
    assert len(run.stdout_body.split("\n")) == 1
    assert run.stdout_body.startswith("critical.py:7 [critical/logic]")


def test_p0_s1_ac2_empty_findings_are_not_replaced_by_the_canned_one(bot, write_diff):
    run = bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock(findings=[]))

    assert run.exit_code == 0
    assert run.stdout_body == "No findings at or above low."


def test_p0_s1_interface_finding_line_format(bot, write_diff, golden):
    finding = bot.finding(
        file="billing/invoice.py",
        line=17,
        severity="critical",
        category="security",
        rationale="Tax is applied twice.",
        confidence=0.5,
    )

    diff = write_diff(section("billing/invoice.py", "+x\n" * 17))

    run = bot.review(diff, provider=bot.mock(findings=[finding]))

    assert run.stdout_body == golden("rendered_finding.golden.txt")


@pytest.mark.parametrize(
    "flags", [(), ("--threshold", "critical")], ids=["findings_printed", "no_findings_printed"]
)
def test_p0_s1_ac3_successful_review_prints_the_summary_on_stderr(bot, write_diff, flags):
    provider = bot.mock(findings=[bot.finding()])

    run = bot.review(write_diff(SENTINEL_DIFF + FINDING_DIFF), *flags, provider=provider)

    assert run.exit_code == (0 if flags else 1), run.stderr
    assert SUMMARY in run.stderr


@pytest.mark.parametrize(
    "flags", [(), ("--threshold", "critical")], ids=["findings_printed", "no_findings_printed"]
)
def test_p0_s1_ac3_diff_text_appears_in_neither_stream(bot, write_diff, flags):
    provider = bot.mock(findings=[bot.finding()])

    run = bot.review(write_diff(SENTINEL_DIFF + FINDING_DIFF), *flags, provider=provider)

    assert run.exit_code == (0 if flags else 1), run.stderr
    assert SENTINEL in provider.requests[0].user_prompt  # the review did see it
    assert SENTINEL not in run.stdout
    assert SENTINEL not in run.stderr


@dataclass(frozen=True, slots=True)
class Setting:
    field: str
    flag: str
    variable: str
    variable_value: str
    variable_expected: object
    flag_value: str
    flag_expected: object
    default: object = None


MODEL = Setting(
    "model",
    "--model",
    "REVIEW_BOT_MODEL",
    "env-model",
    "env-model",
    "flag-model",
    "flag-model",
    "claude-opus-5-5",
)
THRESHOLD = Setting(
    "threshold",
    "--threshold",
    "REVIEW_BOT_THRESHOLD",
    "high",
    "high",
    "critical",
    "critical",
    "low",
)
MAX_DIFF_BYTES = Setting(
    "max_diff_bytes",
    "--max-diff-bytes",
    "REVIEW_BOT_MAX_DIFF_BYTES",
    "1234",
    1234,
    "5678",
    5678,
    400000,
)
# The default is P0-S4 AC13's. `mock` stands in for a selection here, and precedence over the
# variable is shown by test_p0_s1_ac4_flag_beats_invalid_provider_variable instead.
PROVIDER = Setting("provider", "--provider", "REVIEW_BOT_PROVIDER", "mock", "mock", "mock", "mock")
ALL_SETTINGS = [PROVIDER, MODEL, THRESHOLD, MAX_DIFF_BYTES]
SETTINGS_WITH_A_DEFAULT = [MODEL, THRESHOLD, MAX_DIFF_BYTES]


def _setting_id(setting: Setting) -> str:
    return setting.flag


def _select_provider(setting: Setting) -> tuple[str, ...]:
    return () if setting is PROVIDER else ("--provider", "mock")


@pytest.mark.parametrize("setting", ALL_SETTINGS, ids=_setting_id)
def test_p0_s1_ac4_variable_alone_sets_the_value(bot, monkeypatch, setting):
    monkeypatch.setenv(setting.variable, setting.variable_value)

    _, settings = bot.parse_args("change.diff", *_select_provider(setting))

    assert getattr(settings, setting.field) == setting.variable_expected


@pytest.mark.parametrize("setting", ALL_SETTINGS, ids=_setting_id)
def test_p0_s1_ac4_flag_alone_sets_the_value(bot, setting):
    _, settings = bot.parse_args(
        "change.diff", *_select_provider(setting), setting.flag, setting.flag_value
    )

    assert getattr(settings, setting.field) == setting.flag_expected


@pytest.mark.parametrize("setting", SETTINGS_WITH_A_DEFAULT, ids=_setting_id)
def test_p0_s1_ac4_flag_beats_variable(bot, monkeypatch, setting):
    monkeypatch.setenv(setting.variable, setting.variable_value)

    _, settings = bot.parse_args(
        "change.diff", *_select_provider(setting), setting.flag, setting.flag_value
    )

    assert getattr(settings, setting.field) == setting.flag_expected


def test_p0_s1_ac4_flag_beats_invalid_provider_variable(bot, write_diff, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_PROVIDER", "nope")

    run = bot.run(str(write_diff(MINIMAL_DIFF)), "--provider", "mock")

    # The mock's canned finding is dropped since P1-S6 (AC11); its notice shows the mock ran.
    assert run.exit_code == 0, run.stderr
    assert run.stdout_body == NO_FINDINGS_LOW
    assert CANNED_NOTICE in run.stderr.splitlines()


@pytest.mark.parametrize(
    ("environment", "flags"),
    [
        ({"REVIEW_BOT_MODEL": "bad model"}, ("--model", "flag-model")),
        ({"REVIEW_BOT_THRESHOLD": "urgent"}, ("--threshold", "low")),
        ({"REVIEW_BOT_MAX_DIFF_BYTES": "abc"}, ("--max-diff-bytes", "1000")),
    ],
    ids=["model", "threshold", "max_diff_bytes"],
)
def test_p0_s1_ac4_overridden_variable_is_not_validated(
    bot, write_diff, monkeypatch, environment, flags
):
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)

    provider = bot.mock(findings=[bot.finding()])

    run = bot.review(write_diff(FINDING_DIFF), *flags, provider=provider)

    assert run.exit_code == 1, run.stderr


@pytest.mark.parametrize("setting", SETTINGS_WITH_A_DEFAULT, ids=_setting_id)
def test_p0_s1_ac4_neither_gives_the_default(bot, setting):
    _, settings = bot.parse_args("change.diff", "--provider", "mock")

    assert getattr(settings, setting.field) == setting.default


def test_p0_s1_ac4_parse_args_returns_the_path_given(bot):
    path, _ = bot.parse_args("some/change.diff", "--provider", "mock")

    assert path == Path("some/change.diff")


def test_p0_s1_ac4_model_flag_reaches_the_request(bot, write_diff):
    request = bot.request_for(write_diff(MINIMAL_DIFF), "--model", "flag-model")

    assert request.model == "flag-model"


def test_p0_s1_ac4_model_variable_reaches_the_request(bot, write_diff, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_MODEL", "env-model")

    request = bot.request_for(write_diff(MINIMAL_DIFF))

    assert request.model == "env-model"


def test_p0_s1_ac4_model_flag_beats_variable_in_the_request(bot, write_diff, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_MODEL", "env-model")

    request = bot.request_for(write_diff(MINIMAL_DIFF), "--model", "flag-model")

    assert request.model == "flag-model"


def test_p0_s1_ac4_default_model_reaches_the_request(bot, write_diff):
    request = bot.request_for(write_diff(MINIMAL_DIFF))

    assert request.model == "claude-opus-5-5"


def test_p0_s1_ac4_threshold_variable_changes_the_outcome(bot, write_diff, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_THRESHOLD", "critical")
    provider = bot.mock(findings=[bot.finding()])

    run = bot.review(write_diff(FINDING_DIFF), provider=provider)

    assert run.exit_code == 0, run.stderr
    assert run.stdout_body == "No findings at or above critical."


def test_p0_s1_ac4_threshold_flag_beats_variable_in_the_outcome(bot, write_diff, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_THRESHOLD", "critical")

    provider = bot.mock(findings=[bot.finding()])

    run = bot.review(write_diff(FINDING_DIFF), "--threshold", "low", provider=provider)

    assert run.exit_code == 1, run.stderr
    assert run.stdout_body == FINDING_LINE


def test_p0_s1_ac4_max_diff_bytes_variable_is_enforced(bot, write_diff, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_MAX_DIFF_BYTES", "10")

    run = bot.review(write_diff(b"x" * 11))

    assert run.exit_code == 6


def test_p0_s1_ac4_max_diff_bytes_flag_beats_variable_when_enforced(bot, write_diff, monkeypatch):
    # Since P1-S2 a review needs a real diff, so the limits are set around the diff's own size.
    size = len(FINDING_DIFF.encode("utf-8"))
    monkeypatch.setenv("REVIEW_BOT_MAX_DIFF_BYTES", str(size - 1))
    provider = bot.mock(findings=[bot.finding()])

    run = bot.review(write_diff(FINDING_DIFF), "--max-diff-bytes", str(size), provider=provider)

    assert run.exit_code == 1, run.stderr


def test_p0_s1_ac4_provider_variable_selects_the_provider_for_a_real_run(
    bot, write_diff, monkeypatch
):
    monkeypatch.setenv("REVIEW_BOT_PROVIDER", "mock")

    run = bot.run(str(write_diff(MINIMAL_DIFF)))

    # The mock's canned finding is dropped since P1-S6 (AC11); its notice shows the mock ran.
    assert run.exit_code == 0, run.stderr
    assert run.stdout_body == NO_FINDINGS_LOW
    assert CANNED_NOTICE in run.stderr.splitlines()


@pytest.mark.parametrize(
    ("flags", "named"),
    [
        (("--provider", "mock", "--nope"), "--nope"),
        (("--provider", "nope"), "--provider"),
        (("--provider", "mock", "--threshold", "urgent"), "--threshold"),
        (("--provider", "mock", "--max-diff-bytes", "0"), "--max-diff-bytes"),
        (("--provider", "mock", "--max-diff-bytes", "-1"), "--max-diff-bytes"),
        (("--provider", "mock", "--max-diff-bytes", "abc"), "--max-diff-bytes"),
        (("--provider", "mock", "--max-diff-bytes", "100000001"), "--max-diff-bytes"),
        (("--provider", "mock", "--model=--dangerous"), "--model"),
        (("--provider", "mock", "--model", ""), "--model"),
        (("--provider", "mock", "--model", "bad model"), "--model"),
        (("--provider", "mock", "--model", "bad\nmodel"), "--model"),
    ],
    ids=[
        "unknown_flag",
        "provider_nope",
        "threshold_urgent",
        "max_diff_bytes_zero",
        "max_diff_bytes_negative",
        "max_diff_bytes_not_a_number",
        "max_diff_bytes_over_the_upper_bound",
        "model_that_looks_like_a_flag",
        "model_empty",
        "model_with_a_space",
        "model_with_a_newline",
    ],
)
def test_p0_s1_ac5_bad_flag_returns_2_and_names_it(bot, write_diff, flags, named):
    provider = bot.mock()

    run = bot.run(str(write_diff(MINIMAL_DIFF)), *flags, provider=provider)

    assert run.exit_code == 2
    assert named in run.stderr
    assert run.stdout == ""
    assert provider.requests == []


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("REVIEW_BOT_MAX_DIFF_BYTES", "abc"),
        ("REVIEW_BOT_MAX_DIFF_BYTES", "0"),
        ("REVIEW_BOT_THRESHOLD", "urgent"),
        ("REVIEW_BOT_MAX_DIFF_BYTES", "100000001"),
        ("REVIEW_BOT_MODEL", "bad model"),
        ("REVIEW_BOT_MODEL", "--dangerous"),
    ],
)
def test_p0_s1_ac5_bad_variable_returns_2_and_names_it(
    bot, write_diff, monkeypatch, variable, value
):
    monkeypatch.setenv(variable, value)
    provider = bot.mock()

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 2
    assert variable in run.stderr
    assert run.stdout == ""
    assert provider.requests == []


def test_p0_s1_ac5_bad_provider_variable_returns_2_and_names_it(bot, write_diff, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_PROVIDER", "nope")

    run = bot.run(str(write_diff(MINIMAL_DIFF)))

    assert run.exit_code == 2
    assert "REVIEW_BOT_PROVIDER" in run.stderr
    assert run.stdout == ""


def test_p0_s1_ac5_upper_bound_flag_is_accepted(bot, write_diff):
    # A tiny diff, but the read allocates the limit up front: about 100 MB, so this runs only once.
    provider = bot.mock(findings=[])

    run = bot.review(write_diff(MINIMAL_DIFF), "--max-diff-bytes", "100000000", provider=provider)

    assert run.exit_code == 0, run.stderr
    assert "+x\n" in provider.requests[0].user_prompt


def test_p0_s1_ac5_upper_bound_variable_is_accepted(bot, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_MAX_DIFF_BYTES", "100000000")

    _, settings = bot.parse_args("change.diff", "--provider", "mock")

    assert settings.max_diff_bytes == 100_000_000


def test_p0_s1_ac5_no_path_returns_2_and_names_the_missing_argument(bot):
    # The spec doesn't name the positional: `<diff>` in its usage line, "path" in AC5's wording.
    provider = bot.mock()

    run = bot.run("--provider", "mock", provider=provider)

    assert run.exit_code == 2
    assert re.search(r"diff|path", run.stderr, re.IGNORECASE)
    assert run.stdout == ""
    assert provider.requests == []
