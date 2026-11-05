"""P0-S2 AC9 to AC12: a successful review through `--provider claude`."""

import subprocess

import pytest
from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock
from fake_sdk import HIGH_FINDING, FakeSdk, findings_json, result_message, succeeds
from repo_paths import REPO_ROOT

from review_bot import cli
from review_bot.providers.base import ReviewRequest, TokenUsage
from review_bot.providers.claude_agent import ClaudeAgentProvider

pytestmark = pytest.mark.integration

HIGH_FINDING_LINE = (
    (REPO_ROOT / "tests" / "fixtures" / "p0_s2" / "high_finding.golden.txt")
    .read_bytes()
    .decode("utf-8")
    .removesuffix("\n")
)
USAGE_WITH_CACHE = {
    "input_tokens": 2,
    "cache_creation_input_tokens": 100,
    "cache_read_input_tokens": 50,
    "output_tokens": 7,
}


def test_p0_s2_ac9_build_provider_returns_claude_and_starts_no_subprocess(bot, monkeypatch):
    def refuse_process(*args, **kwargs):
        raise AssertionError("building the provider must not start a process")

    monkeypatch.setattr(subprocess.Popen, "__init__", refuse_process)
    _, settings = bot.parse_args("change.diff", "--provider", "claude")

    provider = cli.build_provider(settings)

    assert isinstance(provider, ClaudeAgentProvider)


def test_p0_s2_ac9_provider_variable_also_selects_claude(bot, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_PROVIDER", "claude")
    _, settings = bot.parse_args("change.diff")

    assert isinstance(cli.build_provider(settings), ClaudeAgentProvider)


def test_p0_s2_ac10_valid_finding_is_printed_and_exits_1(claude_review):
    fake = FakeSdk(result_message(findings_json(HIGH_FINDING)))

    run = claude_review(fake)

    assert run.exit_code == 1, run.stderr
    assert run.stdout_body == HIGH_FINDING_LINE


def test_p0_s2_ac10_no_findings_exits_0(claude_review):
    fake = FakeSdk(result_message(findings_json()))

    run = claude_review(fake)

    assert run.exit_code == 0, run.stderr
    assert run.stdout_body == "No findings at or above low."


def test_p0_s2_ac10_result_is_found_after_other_messages(claude_review):
    fake = FakeSdk(
        SystemMessage(subtype="init", data={}),
        AssistantMessage(content=[TextBlock(text=findings_json())], model="m"),
        result_message(findings_json(HIGH_FINDING)),
    )

    run = claude_review(fake)

    assert run.exit_code == 1, run.stderr
    assert run.stdout_body == HIGH_FINDING_LINE


def test_p0_s2_ac10_threshold_applies_to_claude_findings(claude_review):
    fake = FakeSdk(result_message(findings_json(HIGH_FINDING)))

    run = claude_review(fake, "--threshold", "critical")

    assert run.exit_code == 0, run.stderr
    assert run.stdout_body == "No findings at or above critical."


def test_p0_s2_ac10_real_review_has_no_mock_warning(claude_review):
    run = claude_review(succeeds())

    assert "canned" not in run.stderr.lower()


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (USAGE_WITH_CACHE, TokenUsage(152, 7)),
        ({"input_tokens": 2, "output_tokens": 7}, TokenUsage(2, 7)),
        (None, TokenUsage(0, 0)),
    ],
    ids=["with_cache", "no_cache_fields", "none"],
)
async def test_p0_s2_ac11_usage_becomes_token_usage(claude_provider, usage, expected):
    fake = FakeSdk(result_message(findings_json(), usage=usage))

    response = await claude_provider(fake).review(
        ReviewRequest(system_prompt="S", user_prompt="U", model="M")
    )

    assert response.usage == expected


@pytest.mark.parametrize(
    ("usage", "input_tokens", "output_tokens"),
    [(USAGE_WITH_CACHE, "152", "7"), (None, "0", "0")],
    ids=["with_cache", "none"],
)
def test_p0_s2_ac11_summary_shows_provider_and_tokens(
    claude_review, usage, input_tokens, output_tokens
):
    fake = FakeSdk(result_message(findings_json(), usage=usage))

    run = claude_review(fake)

    assert run.exit_code == 0, run.stderr
    words = run.stderr.split()
    assert "provider=claude" in words
    assert f"input_tokens={input_tokens}" in words
    assert f"output_tokens={output_tokens}" in words


def test_p0_s2_ac12_no_api_key_still_calls_the_sdk(claude_review, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = succeeds()

    run = claude_review(fake)

    assert run.exit_code == 0, run.stderr
    assert fake.started
    assert "ANTHROPIC_API_KEY" not in fake.options.env
