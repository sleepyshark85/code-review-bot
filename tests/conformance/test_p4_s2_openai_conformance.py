"""P4-S2 AC5: `OpenAICompatibleProvider` runs the same suite as the mock and Claude.

The suite is the rest of this directory, unchanged: the `[openai]` cases of its tests are this
criterion, over a scripted `HttpTransport` (`fake_openai`). Since P4-S6 that includes
`tool_loop` (`test_p4_s6_openai_tool_loop_conformance.py`). These tests only show that `openai`
really is a factory.
"""

import pytest

from review_bot.providers.openai_compat import OpenAICompatibleProvider

pytestmark = pytest.mark.integration


def test_p4_s2_ac5_mock_claude_and_openai_are_all_in_the_suite(provider_factories):
    assert set(provider_factories) == {"mock", "claude", "openai"}


def test_p4_s2_ac5_openai_factory_builds_the_openai_compatible_provider(provider_factories):
    assert isinstance(provider_factories["openai"]("valid"), OpenAICompatibleProvider)
