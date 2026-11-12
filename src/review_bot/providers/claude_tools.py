"""The Claude provider's context mode: its tools as an in-process MCP server, and nothing else.

Part of the Claude provider, the only code that uses the Claude Agent SDK (ADR 0001); split
from ``claude_agent`` to keep each module to one concern. Lockdown: Phase 3 design, Tool safety.
Every tool the CLI asks for is decided by the policy, as the executor decides it (ADR 0018).
"""

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Final

from claude_agent_sdk import (
    CanUseTool,
    ClaudeAgentOptions,
    HookCallback,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    McpSdkServerConfig,
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    SystemMessage,
    ToolPermissionContext,
)
from mcp.server import Server
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, TextContent, Tool

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.governance.policy import Policy, allows_tool
from review_bot.providers.base import DenialLayer, DeniedTool, ToolDefinition, ToolExecutor
from review_bot.render import escape_unprintable

__all__ = ["ToolLockdown", "open_to_tools"]

# The in-process MCP server offering the review's tools; the CLI names them ``mcp__repo__<tool>``.
_TOOL_SERVER: Final = "repo"
_TOOL_PREFIX: Final = f"mcp__{_TOOL_SERVER}__"
# The most tool names the error names from each of the unexpected and the missing ones, and the
# most characters it shows of each, here and in a denial: the names are the CLI's untrusted text.
_MAX_NAMED_TOOLS: Final = 10
_MAX_NAME_CHARS: Final = 100
_STOPPED_TOOL_TEXT: Final = "The review has stopped: answer with no further tool calls."


@dataclasses.dataclass(slots=True)
class ToolLockdown:
    """What one context call's lockdown has seen: the CLI's tool list, and each tool it denied.

    ``allowed`` are the full names of the tools offered, all of which the policy allows.
    ``listed`` is the first init message's list. ``has_failed`` is set once a list is wrong, so
    no tool runs after that, even if the CLI goes on. ``denied`` holds each tool the hook or
    ``can_use_tool`` denied, in order.
    """

    allowed: tuple[str, ...]
    listed: tuple[str, ...] | None = None
    has_failed: bool = False
    denied: list[DeniedTool] = dataclasses.field(default_factory=list)

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

    def record_denial(self, tool_name: object, layer: DenialLayer) -> None:
        """Record that ``layer`` denied the CLI's untrusted ``tool_name``, cut and escaped."""
        self.denied.append(DeniedTool(_shown_name(str(tool_name)), layer))


def open_to_tools(
    options: ClaudeAgentOptions, tools: ToolExecutor, *, policy: Policy, max_tool_calls: int
) -> tuple[ClaudeAgentOptions, ToolLockdown]:
    """Open diff mode's locked-down ``options`` to those of ``tools`` that ``policy`` allows.

    The CLI's built-in tools stay off. Its allowlist names the allowed tools by full name, and
    its ``PreToolUse`` hook and ``can_use_tool`` deny any other tool, each deciding by
    ``policy`` as the executor does.

    Returns:
        The options, and the lockdown the call's messages must pass: a tool runs only while no
        init message has listed other tools.
    """
    offered = tuple(
        definition for definition in tools.definitions if allows_tool(policy.tools, definition.name)
    )
    lockdown = ToolLockdown(tuple(f"{_TOOL_PREFIX}{definition.name}" for definition in offered))
    reason = (
        f"The policy {policy.version} allows only {', '.join(lockdown.allowed)} in this review."
    )
    opened = dataclasses.replace(
        options,
        allowed_tools=list(lockdown.allowed),
        mcp_servers={_TOOL_SERVER: _tool_server(offered, tools, lockdown)},
        hooks={"PreToolUse": [HookMatcher(hooks=[_pre_tool_use(policy, lockdown, reason)])]},
        can_use_tool=_can_use_tool(policy, lockdown, reason),
        permission_mode="dontAsk",
        # A backstop: the tools themselves refuse every call past ``max_tool_calls``.
        max_turns=max_tool_calls + 2,
    )
    return opened, lockdown


def _tool_server(
    offered: Sequence[ToolDefinition], tools: ToolExecutor, lockdown: ToolLockdown
) -> McpSdkServerConfig:
    """The in-process MCP server listing the ``offered`` tools, and calling ``tools``.

    Our own server, not the SDK's ``create_sdk_mcp_server``, which checks the arguments against
    the schema first: they must reach the executor unchanged, so it records and refuses every
    call itself, within the review's caps and the policy (P3-S3 Limits).
    """
    listed = ListToolsResult(
        tools=[
            Tool(
                name=definition.name,
                description=definition.description,
                input_schema=dict(definition.input_schema),
            )
            for definition in offered
        ]
    )

    async def list_tools(_context: object, _params: object) -> ListToolsResult:
        return listed

    async def call_tool(_context: object, params: CallToolRequestParams) -> CallToolResult:
        if lockdown.has_failed:
            text = _STOPPED_TOOL_TEXT
        else:
            text = await tools.call(params.name, params.arguments or {})
        return CallToolResult(content=[TextContent(type="text", text=text)])

    server = Server(_TOOL_SERVER, on_list_tools=list_tools, on_call_tool=call_tool)
    return McpSdkServerConfig(type="sdk", name=_TOOL_SERVER, instance=server)


def _pre_tool_use(policy: Policy, lockdown: ToolLockdown, reason: str) -> HookCallback:
    """A ``PreToolUse`` hook denying, with ``reason``, every tool ``policy`` doesn't allow."""

    async def hook(
        hook_input: HookInput, _tool_use_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        tool_name = hook_input.get("tool_name")
        if _policy_allows(policy, tool_name):
            return {}
        lockdown.record_denial(tool_name, "hook")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return hook


def _can_use_tool(policy: Policy, lockdown: ToolLockdown, reason: str) -> CanUseTool:
    """A ``can_use_tool`` callback denying, with ``reason``, every tool ``policy`` doesn't allow.

    The backstop to the hook: the CLI may not consult it for the tools its allowlist names.
    """

    async def can_use_tool(
        tool_name: str, _input: dict[str, object], _context: ToolPermissionContext
    ) -> PermissionResult:
        if _policy_allows(policy, tool_name):
            return PermissionResultAllow()
        lockdown.record_denial(tool_name, "can_use_tool")
        return PermissionResultDeny(message=reason)

    return can_use_tool


def _policy_allows(policy: Policy, tool_name: object) -> bool:
    """Whether ``policy`` allows the CLI's untrusted ``tool_name``: our tool, by its full name."""
    if not isinstance(tool_name, str) or not tool_name.startswith(_TOOL_PREFIX):
        return False
    return allows_tool(policy.tools, tool_name.removeprefix(_TOOL_PREFIX))


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


def _shown_name(name: str) -> str:
    """As much of ``name`` as fits in ``_MAX_NAME_CHARS`` characters once escaped."""
    shown: list[str] = []
    length = 0
    for char in name[:_MAX_NAME_CHARS]:
        escaped = escape_unprintable(char)
        length += len(escaped)
        if length > _MAX_NAME_CHARS:
            break
        shown.append(escaped)
    return "".join(shown)
