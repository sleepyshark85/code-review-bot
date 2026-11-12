"""P0-S4 AC14: `ClaudeAgentProvider` runs the same suite as `MockProvider`.

The suite is `test_p0_s3_provider_conformance.py`, unchanged: the `[claude]` cases of its tests are
this criterion. These tests only show that `claude` really is one of the factories.
"""

import pytest

from review_bot.providers.claude_agent import ClaudeAgentProvider

pytestmark = pytest.mark.integration


def test_p0_s4_ac14_mock_and_claude_are_both_in_the_suite(provider_factories):
    # P4-S2 adds `openai` beside them.
    assert {"mock", "claude"} <= set(provider_factories)


def test_p0_s4_ac14_claude_factory_builds_the_claude_provider(provider_factories):
    assert isinstance(provider_factories["claude"]("valid"), ClaudeAgentProvider)
