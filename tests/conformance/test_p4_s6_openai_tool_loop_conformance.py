"""P4-S6 AC9: the `tool_loop` scenario now runs for `openai`, so every scenario runs for all three.

The scenario itself is `test_p3_s3_tool_loop_conformance.py` and
`test_p4_s3_tool_policy_conformance.py`, whose `[openai]` cases drive the real `RepoTools`
through function calling over a scripted `HttpTransport`. This file shows that no provider skips
any scenario, and that `openai`'s tool loop is the real provider's: the read is in the ledger,
and the finding is the second scripted response's, reached only after the first asked for it.
"""

from pathlib import Path

import pytest
from fake_mcp import CHECKOUT, commit_repo, repo_tools
from fake_sdk import HIGH_FINDING

from review_bot.errors import ErrorCategory
from review_bot.findings import Finding
from review_bot.prompt import build_context_request
from review_bot.providers.openai_compat import OpenAICompatibleProvider

pytestmark = pytest.mark.integration

SCENARIOS = ["valid", "empty", "invalid", "warned", "diagnosed", "tool_loop", *ErrorCategory]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=str)
def test_p4_s6_ac9_no_provider_skips_any_scenario(provider_factories, scenario):
    for name, factory in provider_factories.items():
        try:
            factory(scenario)
        except pytest.skip.Exception as skipped:
            pytest.fail(f"{name} skips {scenario}: {skipped}")


async def test_p4_s6_ac9_openai_tool_loop_reads_through_the_executor(
    provider_factories, tmp_path: Path
):
    tools = repo_tools(commit_repo(tmp_path / "repo"))
    request = build_context_request("+x\n", model="m1", tools=tools)
    provider = provider_factories["openai"]("tool_loop")
    assert isinstance(provider, OpenAICompatibleProvider)

    response = await provider.review(request)

    assert response.findings == (Finding(**HIGH_FINDING),)
    [entry] = tools.ledger
    assert [span.path for span in entry.spans] == [CHECKOUT]
