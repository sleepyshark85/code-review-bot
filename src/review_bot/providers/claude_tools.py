"""The Claude provider's context mode: its tools as an in-process MCP server, and nothing else.

Part of the Claude provider, the only code that uses the Claude Agent SDK (ADR 0001); split
from ``claude_agent`` to keep each module to one concern. Lockdown: Phase 3 design, Tool safety.
"""

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Final

from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookCallback,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    McpSdkServerConfig,
    SystemMessage,
)
from mcp.server import Server
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, TextContent, Tool

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.providers.base import ToolExecutor

__all__ = ["ToolListCheck", "open_to_tools"]

# The in-process MCP server offering the review's tools; the CLI names them ``mcp__repo__<tool>``.
_TOOL_SERVER: Final = "repo"
# The most tool names the error names from each of the unexpected and the missing ones, and the
# most characters it shows of each: the names are the CLI's untrusted text.
_MAX_NAMED_TOOLS: Final = 10
_MAX_NAME_CHARS: Final = 100
_STOPPED_TOOL_TEXT: Final = "The review has stopped: answer with no further tool calls."


@dataclasses.dataclass(slots=True)
class ToolListCheck:
    """Checks that each of the CLI's init messages lists exactly the allowed tools.

    ``listed`` is the first init message's list. ``has_failed`` is set once a list is wrong, so
    no tool runs after that, even if the CLI goes on.
    """

    allowed: tuple[str, ...]
    listed: tuple[str, ...] | None = None
    has_failed: bool = False

    def check(self, message: object) -> None:
        """Raise a ``permanent`` ProviderError if ``message`` is an init listing other tools."""
        if not (isinstance(message, SystemMessage) and message.subtype == "init"):
            return
        listed = _listed_tools(message.data)
        if self.listed is None:
            self.listed = listed
        unexpected = list(dict.fromkeys(name for name in listed if name not in self.allowed))
        missing = [name for name in self.allowed if name not in listed]
        if unexpected or missing:
            self.has_failed = True
            raise ProviderError(ErrorCategory.PERMANENT, _tool_list_message(unexpected, missing))


def open_to_tools(
    options: ClaudeAgentOptions, tools: ToolExecutor, *, max_tool_calls: int
) -> tuple[ClaudeAgentOptions, ToolListCheck]:
    """Open diff mode's locked-down ``options`` to ``tools``, by full name, and nothing else.

    Returns:
        The options, and the check the call's messages must pass: a tool runs only while no init
        message has listed other tools.
    """
    tool_check = ToolListCheck(
        tuple(f"mcp__{_TOOL_SERVER}__{definition.name}" for definition in tools.definitions)
    )
    opened = dataclasses.replace(
        options,
        allowed_tools=list(tool_check.allowed),
        mcp_servers={_TOOL_SERVER: _tool_server(tools, tool_check)},
        hooks={"PreToolUse": [HookMatcher(hooks=[_deny_other_tools(tool_check.allowed)])]},
        permission_mode="dontAsk",
        # A backstop: the tools themselves refuse every call past ``max_tool_calls``.
        max_turns=max_tool_calls + 2,
    )
    return opened, tool_check


def _tool_server(tools: ToolExecutor, tool_check: ToolListCheck) -> McpSdkServerConfig:
    """The in-process MCP server through which Claude calls ``tools``.

    Our own server, not the SDK's ``create_sdk_mcp_server``, which checks the arguments against
    the schema first: they must reach the executor unchanged, so it records and refuses every
    call itself, within the review's caps (slice Limits).
    """
    listed = ListToolsResult(
        tools=[
            Tool(
                name=definition.name,
                description=definition.description,
                input_schema=dict(definition.input_schema),
            )
            for definition in tools.definitions
        ]
    )

    async def list_tools(_context: object, _params: object) -> ListToolsResult:
        return listed

    async def call_tool(_context: object, params: CallToolRequestParams) -> CallToolResult:
        if tool_check.has_failed:
            text = _STOPPED_TOOL_TEXT
        else:
            text = await tools.call(params.name, params.arguments or {})
        return CallToolResult(content=[TextContent(type="text", text=text)])

    server = Server(_TOOL_SERVER, on_list_tools=list_tools, on_call_tool=call_tool)
    return McpSdkServerConfig(type="sdk", name=_TOOL_SERVER, instance=server)


def _deny_other_tools(allowed: Sequence[str]) -> HookCallback:
    """A ``PreToolUse`` hook denying every tool but ``allowed``, whatever the CLI offers."""
    reason = f"Only {', '.join(allowed)} may be used in this review."

    async def hook(
        hook_input: HookInput, _tool_use_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        if hook_input.get("tool_name") in allowed:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return hook


def _listed_tools(data: object) -> tuple[str, ...]:
    """The init message's untrusted tool list; anything but a list counts as listing none."""
    tools = data.get("tools") if isinstance(data, Mapping) else None
    return tuple(str(name) for name in tools) if isinstance(tools, list) else ()


def _tool_list_message(unexpected: Sequence[str], missing: Sequence[str]) -> str:
    found = []
    if unexpected:
        found.append(f"unexpected {_named(unexpected)}")
    if missing:
        found.append(f"missing {_named(missing)}")
    return (
        "The Claude Code CLI didn't offer exactly the review's own tools, so the review "
        f"stopped: {'; '.join(found)}. Check the Claude Code CLI and its version."
    )


def _named(names: Sequence[str]) -> str:
    shown = ", ".join(_shortened(name) for name in names[:_MAX_NAMED_TOOLS])
    hidden_count = len(names) - _MAX_NAMED_TOOLS
    return shown if hidden_count <= 0 else f"{shown} and {hidden_count} more"


def _shortened(name: str) -> str:
    return name if len(name) <= _MAX_NAME_CHARS else f"{name[:_MAX_NAME_CHARS]}…"
