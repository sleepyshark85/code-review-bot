"""P3-S3 AC7: the conformance suite's tool-loop case, run by `MockProvider` and by Claude.

`provider_factory("tool_loop")` scripts one `read` of `billing/checkout.py` and then the high
finding. Claude runs over `FakeSdk`, which makes the read through the provider's in-process MCP
server as the CLI would (`fake_mcp`). The executor is the real `RepoTools` over a one-commit
repository, so its ledger is the one a review records.
"""

from pathlib import Path

import pytest
from fake_mcp import CHECKOUT, commit_repo, repo_tools
from fake_sdk import HIGH_FINDING

from review_bot.context.ledger import ToolOutcome
from review_bot.findings import Finding
from review_bot.prompt import build_context_request

pytestmark = pytest.mark.integration


@pytest.fixture
def tools(tmp_path: Path):
    return repo_tools(commit_repo(tmp_path / "repo"))


async def test_p3_s3_ac7_tool_loop_returns_the_scripted_findings(provider_factory, tools):
    request = build_context_request("+x\n", model="claude-opus-5-5", tools=tools)

    response = await provider_factory("tool_loop").review(request)

    assert response.findings == (Finding(**HIGH_FINDING),)


async def test_p3_s3_ac7_tool_loop_leaves_one_read_in_the_ledger(provider_factory, tools):
    request = build_context_request("+x\n", model="claude-opus-5-5", tools=tools)

    await provider_factory("tool_loop").review(request)

    [entry] = tools.ledger
    assert entry.tool == "read"
    assert entry.outcome is ToolOutcome.OK
    assert [span.path for span in entry.spans] == [CHECKOUT]
