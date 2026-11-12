"""P4-S3 AC6: the tool loop still passes for every provider, now with Claude's permission layers.

Every other scenario, `tool_loop` included, is run by the existing conformance files; Claude's
context review there now carries the hook and `can_use_tool` over streaming input, which
`FakeSdk` reads as the SDK does. This adds the one thing S3 makes every provider report: a review
that only uses allowed tools carries no denial.
"""

from pathlib import Path

import pytest
from fake_mcp import commit_repo, repo_tools

from review_bot.prompt import build_context_request

pytestmark = pytest.mark.integration


async def test_p4_s3_ac6_tool_loop_carries_no_denied_tool(provider_factory, tmp_path: Path):
    tools = repo_tools(commit_repo(tmp_path / "repo"))
    request = build_context_request("+x\n", model="claude-opus-5-5", tools=tools)

    response = await provider_factory("tool_loop").review(request)

    assert response.denied_tools == ()
    assert len(tools.ledger) == 1
