"""P0-S2 AC15: the guard in `tests/conftest.py` fails any test that reaches the real `query`."""

import contextlib
import shutil
import subprocess
import sys

import pytest
from claude_agent_sdk import ClaudeAgentOptions, query
from repo_paths import REPO_ROOT

from review_bot import cli
from review_bot.providers.base import ReviewRequest

pytestmark = pytest.mark.integration

# `real_query_guard` is what is on trial here, so this runs under a copy of the real conftest. The
# first test's own assertion holds because `main` swallows the guard, so only the guard can fail it.
VICTIMS = """
from review_bot import cli


def test_reaches_the_real_query(tmp_path):
    diff = tmp_path / "change.diff"
    diff.write_text("+x")
    assert cli.main([str(diff), "--provider", "claude"]) == 14


def test_does_not_reach_it():
    assert True
"""


async def test_p0_s2_ac15_control_direct_call_fires_the_guard(real_query_calls):
    with pytest.raises(AssertionError, match=r"real claude_agent_sdk\.query"):
        async for _ in query(prompt="x", options=ClaudeAgentOptions()):
            pass

    assert len(real_query_calls) == 1
    real_query_calls.clear()


def test_p0_s2_ac15_control_default_provider_route_fires_the_guard(
    bot, write_diff, real_query_calls
):
    # No injected provider or fake: the route a real run takes through `build_provider`.
    run = bot.run(str(write_diff("+x\n")), "--provider", "claude")

    assert run.exit_code == 14
    assert len(real_query_calls) == 1
    real_query_calls.clear()


async def test_p0_s2_ac15_default_sdk_call_passes_the_lockdown_to_the_real_query(
    bot, real_query_calls
):
    # AC1 reads the fake's options. This is the one place the real `query`'s arguments are visible.
    _, settings = bot.parse_args("change.diff", "--provider", "claude")
    request = ReviewRequest(system_prompt="S", user_prompt="U", model="M")

    # The provider may raise the guard's exception or its own: this test is about the call.
    with contextlib.suppress(Exception):
        await cli.build_provider(settings).review(request)

    call = real_query_calls[0]
    real_query_calls.clear()
    assert call["prompt"] == "U"
    assert call["options"].tools == []
    assert call["options"].setting_sources == []
    assert call["options"].max_turns == 1
    assert call["options"].system_prompt == "S"


def test_p0_s2_ac15_control_guard_fails_a_test_whose_assertions_hold(tmp_path):
    shutil.copy(REPO_ROOT / "tests" / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_victims.py").write_text(VICTIMS, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "test_victims.py"],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout
    assert "this test reached the real claude_agent_sdk.query" in completed.stdout
    assert "2 passed, 1 error" in completed.stdout
