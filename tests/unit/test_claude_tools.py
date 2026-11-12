"""The Claude provider in context mode: its MCP server, permission layers, tool list and caps."""

import dataclasses
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    SystemMessage,
    ToolPermissionContext,
)
from fake_sdk import HIGH_FINDING, FakeSdk, findings_json, result_message, succeeds

from review_bot.context.ledger import ToolName
from review_bot.context.tool_args import TOOL_DEFINITIONS
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.governance.policy import BUILTIN_POLICY, Policy
from review_bot.providers.base import DeniedTool, ReviewRequest, ReviewResponse, ToolDefinition
from review_bot.providers.claude_agent import ClaudeAgentProvider, SdkCall
from review_bot.settings import Settings

ALLOWED = ["mcp__repo__read", "mcp__repo__grep", "mcp__repo__glob"]
NO_FINDINGS = result_message(findings_json())


class FakeTools:
    """A ``ToolExecutor`` that records each call and answers with a fixed text."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return TOOL_DEFINITIONS

    async def call(self, name: str, arguments: Mapping[str, object]) -> str:
        self.calls.append((name, arguments))
        return f"result of {name}"


def init_message(tools: object) -> SystemMessage:
    return SystemMessage(subtype="init", data={"type": "system", "tools": tools})


async def call_tool(options: ClaudeAgentOptions, name: str, arguments: dict[str, object]) -> str:
    """Call ``name`` through the options' MCP server, as the CLI would."""
    server = options.mcp_servers["repo"]["instance"]
    entry = server.get_request_handler("tools/call")
    result = await entry.handler(None, entry.params_type(name=name, arguments=arguments))
    return str(result.content[0].text)


def calls_read(path: str) -> Any:
    """A script step in which the model calls ``read`` on ``path``."""

    async def step(options: ClaudeAgentOptions) -> None:
        await call_tool(options, "read", {"path": path})

    return step


async def review(
    sdk: SdkCall,
    *,
    tools: FakeTools | None = None,
    settings: Settings | None = None,
    policy: Policy = BUILTIN_POLICY,
) -> ReviewResponse:
    request = ReviewRequest(
        system_prompt="S", user_prompt="U", model="M", tools=tools, policy=policy
    )
    return await ClaudeAgentProvider(settings or Settings(), sdk_call=sdk).review(request)


async def context_options(settings: Settings | None = None) -> ClaudeAgentOptions:
    sdk = succeeds()
    await review(sdk, tools=FakeTools(), settings=settings)
    return sdk.options


async def test_context_options_offer_only_the_three_tools_by_full_name() -> None:
    options = await context_options()

    assert options.tools == []
    assert options.allowed_tools == ALLOWED
    assert list(options.mcp_servers) == ["repo"]
    assert options.strict_mcp_config is True
    assert options.setting_sources == []
    assert options.agents is None
    assert options.permission_mode == "dontAsk"


async def test_context_server_lists_the_executors_tools_with_their_schemas() -> None:
    options = await context_options()
    server = options.mcp_servers["repo"]["instance"]
    entry = server.get_request_handler("tools/list")

    listed = (await entry.handler(None, None)).tools

    assert [(tool.name, tool.input_schema) for tool in listed] == [
        (definition.name, definition.input_schema) for definition in TOOL_DEFINITIONS
    ]


async def test_context_hooks_hold_only_one_pre_tool_use_hook() -> None:
    options = await context_options()

    assert options.hooks is not None
    assert list(options.hooks) == ["PreToolUse"]
    [matcher] = options.hooks["PreToolUse"]
    assert matcher.matcher is None
    assert len(matcher.hooks) == 1


@pytest.mark.parametrize(("max_tool_calls", "max_turns"), [(12, 14), (1, 3), (30, 32)])
async def test_context_max_turns_is_two_past_the_tool_call_cap(
    max_tool_calls: int, max_turns: int
) -> None:
    options = await context_options(Settings(max_tool_calls=max_tool_calls))

    assert options.max_turns == max_turns


async def test_context_env_is_diff_modes() -> None:
    settings = Settings(max_retries=0, max_output_tokens=500)
    diff_sdk = succeeds()
    await review(diff_sdk, settings=settings)

    options = await context_options(settings)

    assert options.env == diff_sdk.options.env


async def test_diff_options_have_no_tools_hooks_or_permission_mode() -> None:
    sdk = succeeds()

    await review(sdk)

    assert (sdk.options.allowed_tools, sdk.options.mcp_servers) == ([], {})
    assert (sdk.options.hooks, sdk.options.permission_mode, sdk.options.max_turns) == (
        None,
        None,
        1,
    )


# The SDK passes ``str(max_budget_usd)`` to the CLI, so that is the budget the CLI gets.
@pytest.mark.parametrize(
    ("max_cost_usd", "cli_budget"),
    [
        ("0.309", "0.3"),
        ("0.01", "0.01"),
        ("0.019", "0.01"),
        ("0.1", "0.1"),
        ("999.999", "999.99"),
        ("1000", "1000.0"),
    ],
)
@pytest.mark.parametrize("has_tools", [False, True], ids=["diff", "context"])
async def test_budget_is_the_cost_cap_rounded_down_to_the_cent(
    max_cost_usd: str, cli_budget: str, has_tools: bool
) -> None:
    sdk = succeeds()
    settings = Settings(max_cost_usd=Decimal(max_cost_usd))

    await review(sdk, tools=FakeTools() if has_tools else None, settings=settings)

    assert str(sdk.options.max_budget_usd) == cli_budget


@pytest.mark.parametrize("has_tools", [False, True], ids=["diff", "context"])
async def test_budget_without_a_cost_cap_is_unset(has_tools: bool) -> None:
    sdk = succeeds()

    await review(sdk, tools=FakeTools() if has_tools else None)

    assert sdk.options.max_budget_usd is None


async def test_tool_call_reaches_the_executor_unchanged_and_returns_its_text() -> None:
    tools = FakeTools()
    results: list[str] = []

    async def model_reads(options: ClaudeAgentOptions) -> None:
        results.append(await call_tool(options, "read", {"path": "a.py", "start_line": 3}))

    await review(FakeSdk(model_reads, NO_FINDINGS), tools=tools)

    assert tools.calls == [("read", {"path": "a.py", "start_line": 3})]
    assert results == ["result of read"]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [("read", {"path": 5, "extra": [1]}), ("read", {}), ("bash", {"command": "ls"})],
    ids=["schema_breaking", "missing_path", "unknown_tool"],
)
async def test_tool_call_the_schema_rejects_still_reaches_the_executor(
    name: str, arguments: dict[str, object]
) -> None:
    tools = FakeTools()
    results: list[str] = []

    async def model_calls(options: ClaudeAgentOptions) -> None:
        results.append(await call_tool(options, name, arguments))

    await review(FakeSdk(model_calls, NO_FINDINGS), tools=tools)

    assert tools.calls == [(name, arguments)]
    assert results == [f"result of {name}"]


async def pre_tool_use(options: ClaudeAgentOptions, tool_name: str) -> object:
    assert options.hooks is not None
    hook = options.hooks["PreToolUse"][0].hooks[0]
    hook_input = {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": {}}
    return await hook(hook_input, "tool-use-id", {"signal": None})


OTHER_TOOLS = [
    "Bash",
    "Read",
    "Write",
    "WebFetch",
    "Task",
    "ToolSearch",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
    "mcp__other__read",
    "read",
    "mcp__repo__read2",
    "",
]


@pytest.mark.parametrize("tool_name", OTHER_TOOLS)
async def test_hook_denies_any_other_tool_naming_the_allowed_ones(tool_name: str) -> None:
    options = await context_options()

    decision = await pre_tool_use(options, tool_name)

    assert decision == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "The policy builtin-v1 allows only mcp__repo__read, mcp__repo__grep, "
                "mcp__repo__glob in this review."
            ),
        }
    }


@pytest.mark.parametrize("tool_name", ALLOWED)
async def test_hook_lets_the_three_tools_through(tool_name: str) -> None:
    options = await context_options()

    assert await pre_tool_use(options, tool_name) == {}


async def test_hook_without_a_tool_name_denies() -> None:
    options = await context_options()
    assert options.hooks is not None
    hook = options.hooks["PreToolUse"][0].hooks[0]

    decision = await hook({"hook_event_name": "PreToolUse"}, None, {"signal": None})

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_init_listing_exactly_the_three_proceeds_and_is_kept() -> None:
    tools = FakeTools()
    listed = ["mcp__repo__glob", "mcp__repo__read", "mcp__repo__grep"]
    sdk = FakeSdk(init_message(listed), calls_read("a.py"), NO_FINDINGS)

    response = await review(sdk, tools=tools)

    assert response.offered_tools == tuple(listed)
    assert [name for name, _ in tools.calls] == ["read"]


async def test_context_review_without_an_init_offers_no_list() -> None:
    response = await review(succeeds(), tools=FakeTools())

    assert response.offered_tools is None


async def test_diff_review_ignores_the_init_tool_list() -> None:
    response = await review(FakeSdk(init_message(["Bash"]), NO_FINDINGS))

    assert response.offered_tools is None


@pytest.mark.parametrize(
    ("listed", "named"),
    [
        ([*ALLOWED, "Bash"], "unexpected Bash."),
        (["mcp__repo__read", "mcp__repo__glob"], "missing mcp__repo__grep."),
        (["Bash", "mcp__repo__read"], "unexpected Bash; missing mcp__repo__grep, mcp__repo__glob."),
        ([*ALLOWED, "Bash", "Bash"], "unexpected Bash."),
        ("mcp__repo__read", "missing mcp__repo__read, mcp__repo__grep, mcp__repo__glob."),
        (None, "missing mcp__repo__read, mcp__repo__grep, mcp__repo__glob."),
    ],
    ids=["extra", "missing", "both", "repeated", "not_a_list", "absent"],
)
async def test_init_listing_other_tools_fails_permanent_before_any_tool_runs(
    listed: object, named: str
) -> None:
    tools = FakeTools()
    sdk = FakeSdk(init_message(listed), calls_read("a.py"), NO_FINDINGS)

    with pytest.raises(ProviderError) as caught:
        await review(sdk, tools=tools)

    assert caught.value.category is ErrorCategory.PERMANENT
    assert str(caught.value).endswith(f"{named} Check the Claude Code CLI and its version.")
    assert tools.calls == []


async def test_init_listing_many_other_tools_names_ten_and_counts_the_rest() -> None:
    listed = [*ALLOWED, *(f"Tool{number}" for number in range(12))]

    with pytest.raises(ProviderError) as caught:
        await review(FakeSdk(init_message(listed), NO_FINDINGS), tools=FakeTools())

    assert "Tool8, Tool9 and 2 more." in str(caught.value)
    assert "Tool10" not in str(caught.value)


async def test_tool_call_after_a_wrong_init_is_refused_without_running() -> None:
    tools = FakeTools()
    sdk = FakeSdk(init_message(["Bash"]))
    with pytest.raises(ProviderError):
        await review(sdk, tools=tools)

    text = await call_tool(sdk.options, "read", {"path": "a.py"})

    assert text == "The review has stopped: answer with no further tool calls."
    assert tools.calls == []


async def test_wrong_init_outranks_an_earlier_signal() -> None:
    rate_limited = AssistantMessage(content=[], model="M", error="rate_limit")
    sdk = FakeSdk(rate_limited, init_message(["Bash"]), NO_FINDINGS)

    with pytest.raises(ProviderError) as caught:
        await review(sdk, tools=FakeTools())

    assert caught.value.category is ErrorCategory.PERMANENT
    assert "unexpected Bash" in str(caught.value)


async def test_wrong_init_holds_the_cli_stderr_as_diagnostics() -> None:
    async def cli_writes(options: ClaudeAgentOptions) -> None:
        assert options.stderr is not None
        options.stderr("cli line")

    with pytest.raises(ProviderError) as caught:
        await review(FakeSdk(cli_writes, init_message(["Bash"])), tools=FakeTools())

    assert caught.value.diagnostics == ("cli line",)


async def test_init_naming_a_very_long_tool_shows_only_its_first_100_characters() -> None:
    long_name = "B" * 200_000

    with pytest.raises(ProviderError) as caught:
        await review(FakeSdk(init_message([*ALLOWED, long_name]), NO_FINDINGS), tools=FakeTools())

    assert f"unexpected {'B' * 100}…." in str(caught.value)
    assert len(str(caught.value)) < 400


async def test_later_init_listing_other_tools_stops_the_review_and_any_later_tool_call() -> None:
    tools = FakeTools()
    later_results: list[str] = []

    async def model_reads_again(options: ClaudeAgentOptions) -> None:
        later_results.append(await call_tool(options, "read", {"path": "b.py"}))

    sdk = FakeSdk(
        init_message(ALLOWED), calls_read("a.py"), init_message([*ALLOWED, "Bash"]), NO_FINDINGS
    )
    with pytest.raises(ProviderError) as caught:
        await review(sdk, tools=tools)
    await model_reads_again(sdk.options)

    assert caught.value.category is ErrorCategory.PERMANENT
    assert "unexpected Bash" in str(caught.value)
    assert tools.calls == [("read", {"path": "a.py"})]
    assert later_results == ["The review has stopped: answer with no further tool calls."]


# A policy allowing two of the three tools, under its own version (P4-S3).
READ_AND_GLOB = dataclasses.replace(
    BUILTIN_POLICY, version="team-7", tools=frozenset({ToolName.READ, ToolName.GLOB})
)
READ_AND_GLOB_REASON = (
    "The policy team-7 allows only mcp__repo__read, mcp__repo__glob in this review."
)


async def read_and_glob_options() -> ClaudeAgentOptions:
    sdk = succeeds()
    await review(sdk, tools=FakeTools(), policy=READ_AND_GLOB)
    return sdk.options


async def can_use(options: ClaudeAgentOptions, tool_name: object) -> object:
    assert options.can_use_tool is not None
    context = ToolPermissionContext(tool_use_id="tool-use-id")
    return await options.can_use_tool(cast(str, tool_name), {}, context)


async def test_policy_limits_the_allowlist_and_the_servers_list_to_its_tools() -> None:
    options = await read_and_glob_options()
    server = options.mcp_servers["repo"]["instance"]
    entry = server.get_request_handler("tools/list")

    listed = (await entry.handler(None, None)).tools

    assert options.allowed_tools == ["mcp__repo__read", "mcp__repo__glob"]
    assert [tool.name for tool in listed] == ["read", "glob"]


@pytest.mark.parametrize(
    "listed",
    [ALLOWED, ["mcp__repo__read", "mcp__repo__glob", "Bash"], ["mcp__repo__read"]],
    ids=["with_grep", "with_bash", "missing_glob"],
)
async def test_policy_init_listing_other_than_its_tools_fails_permanent(listed: object) -> None:
    sdk = FakeSdk(init_message(listed), NO_FINDINGS)

    with pytest.raises(ProviderError) as caught:
        await review(sdk, tools=FakeTools(), policy=READ_AND_GLOB)

    assert caught.value.category is ErrorCategory.PERMANENT


async def test_policy_init_listing_exactly_its_tools_proceeds() -> None:
    sdk = FakeSdk(init_message(["mcp__repo__glob", "mcp__repo__read"]), NO_FINDINGS)

    response = await review(sdk, tools=FakeTools(), policy=READ_AND_GLOB)

    assert response.offered_tools == ("mcp__repo__glob", "mcp__repo__read")


@pytest.mark.parametrize("tool_name", ["mcp__repo__read", "mcp__repo__glob"])
async def test_policy_hook_and_can_use_tool_allow_its_tools(tool_name: str) -> None:
    options = await read_and_glob_options()

    assert await pre_tool_use(options, tool_name) == {}
    assert await can_use(options, tool_name) == PermissionResultAllow()


@pytest.mark.parametrize(
    "tool_name", ["mcp__repo__grep", "Bash", "Read", "read", "mcp__repo__READ", "mcp__repo__read "]
)
async def test_policy_hook_and_can_use_tool_deny_any_other_naming_the_version(
    tool_name: str,
) -> None:
    options = await read_and_glob_options()

    decision = await pre_tool_use(options, tool_name)
    permission = await can_use(options, tool_name)

    assert decision == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": READ_AND_GLOB_REASON,
        }
    }
    assert permission == PermissionResultDeny(message=READ_AND_GLOB_REASON)


@pytest.mark.parametrize("tool_name", [None, 7, ["mcp__repo__read"]])
async def test_can_use_tool_denies_a_tool_name_that_isnt_text(tool_name: object) -> None:
    options = await read_and_glob_options()

    permission = await can_use(options, tool_name)

    assert isinstance(permission, PermissionResultDeny)


async def test_context_call_sends_the_user_prompt_as_streaming_input() -> None:
    sdk = succeeds()

    await review(sdk, tools=FakeTools())

    [(prompt, options)] = sdk.calls
    assert not isinstance(prompt, str)
    assert sdk.input_messages == [
        {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": "U"},
            "parent_tool_use_id": None,
        }
    ]
    assert options.permission_prompt_tool_name is None


async def test_diff_call_sends_a_string_prompt_and_no_can_use_tool() -> None:
    sdk = succeeds()

    await review(sdk)

    [(prompt, options)] = sdk.calls
    assert prompt == "U"
    assert options.can_use_tool is None


def asks_hook(tool_name: object) -> Any:
    """A script step in which the CLI asks the hook whether the model may use ``tool_name``."""

    async def step(options: ClaudeAgentOptions) -> None:
        await pre_tool_use(options, cast(str, tool_name))

    return step


def asks_can_use_tool(tool_name: str) -> Any:
    """A script step in which the CLI asks ``can_use_tool`` about ``tool_name``."""

    async def step(options: ClaudeAgentOptions) -> None:
        await can_use(options, tool_name)

    return step


async def test_response_carries_each_denial_in_order_and_the_review_goes_on() -> None:
    sdk = FakeSdk(
        asks_hook("Bash"),
        asks_hook("mcp__repo__read"),
        asks_can_use_tool("mcp__repo__grep"),
        asks_can_use_tool("mcp__repo__glob"),
        result_message(findings_json(HIGH_FINDING)),
    )

    response = await review(sdk, tools=FakeTools(), policy=READ_AND_GLOB)

    assert len(response.findings) == 1
    assert response.denied_tools == (
        DeniedTool("Bash", "hook"),
        DeniedTool("mcp__repo__grep", "can_use_tool"),
    )


async def test_response_without_a_denial_carries_none() -> None:
    response = await review(succeeds(), tools=FakeTools())

    assert response.denied_tools == ()


@pytest.mark.parametrize(
    ("tool_name", "shown"),
    [
        ("B" * 200, "B" * 100),
        ("Bash\nrm\x1b[2J", "Bash\\nrm\\x1b[2J"),
        ("x" * 98 + "\n", "x" * 98 + "\\n"),
        ("x" * 99 + "\n", "x" * 99),
        (None, "None"),
    ],
    ids=["long", "control", "escape_fits", "escape_cut", "not_text"],
)
async def test_denied_tool_name_is_at_most_100_characters_escaped(
    tool_name: object, shown: str
) -> None:
    sdk = FakeSdk(asks_hook(tool_name), NO_FINDINGS)

    response = await review(sdk, tools=FakeTools())

    assert response.denied_tools == (DeniedTool(shown, "hook"),)
