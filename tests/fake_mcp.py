"""What the Claude CLI does with the provider's tools, played offline over a `FakeSdk` (P3-S3).

The real CLI talks JSON-RPC to an in-process ("sdk") MCP server through the SDK's bridge, and
calls `PreToolUse` hooks by name. These steps do the same through the SDK's own `SdkMcpBridge`,
so a call reaches the server exactly as the CLI's would: `initialize`, then `tools/list` or
`tools/call`. Nothing here knows how the provider builds its server or hook.

Also a small committed repository and a recording `ToolExecutor`, for criteria about what the
executor sees.
"""

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, SystemMessage
from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge

from review_bot.context.tools import RepoRef, RepoTools, ToolCaps
from review_bot.git import GitRepo
from review_bot.providers.base import ToolDefinition

SERVER = "repo"
SHORT_NAMES = ("read", "grep", "glob")
ALLOWED_TOOLS = ("mcp__repo__read", "mcp__repo__grep", "mcp__repo__glob")

CHECKOUT = "billing/checkout.py"
CHECKOUT_TEXT = "".join(f"line_{n} = {n}\n" for n in range(1, 31))
REPO_FILES = {CHECKOUT: CHECKOUT_TEXT, "README.txt": "Fixture repository.\n"}
DEFAULT_CAPS = ToolCaps(max_calls=12, max_result_bytes=16_000, max_total_bytes=96_000)

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "fake-claude-cli", "version": "0"},
    },
}
_INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def init_message(tools: Any = ALLOWED_TOOLS) -> SystemMessage:
    """The CLI's first message: its session metadata, including the tools the model may use."""
    data: dict[str, Any] = {
        "type": "system",
        "subtype": "init",
        "session_id": "session",
        "mcp_servers": [{"name": SERVER, "status": "connected"}],
    }
    if tools is not None:
        data["tools"] = list(tools) if isinstance(tools, tuple) else tools
    return SystemMessage(subtype="init", data=data)


def server_instance(options: ClaudeAgentOptions, name: str = SERVER) -> Any:
    servers = options.mcp_servers
    assert isinstance(servers, dict), f"mcp_servers isn't a mapping: {servers!r}"
    config = servers[name]
    assert config["type"] == "sdk", f"server {name!r} isn't in-process: {config!r}"
    return config["instance"]


async def _rpc(options: ClaudeAgentOptions, method: str, params: Any = None) -> dict[str, Any]:
    bridge = SdkMcpBridge(SERVER, server_instance(options))
    try:
        answer = await bridge.handle(_INITIALIZE)
        assert answer is not None
        assert "result" in answer, answer
        await bridge.handle(_INITIALIZED)
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            request["params"] = params
        response = await bridge.handle(request)
    finally:
        await bridge.aclose()
    assert response is not None
    assert "result" in response, response
    result = response["result"]
    assert isinstance(result, dict)
    return result


async def list_server_tools(options: ClaudeAgentOptions) -> list[dict[str, Any]]:
    """The server's `tools/list` answer, as the CLI sees it."""
    tools = (await _rpc(options, "tools/list"))["tools"]
    assert isinstance(tools, list)
    return tools


@dataclass
class ToolCallLog:
    """What each scripted call sent and got back, in order."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def texts(self) -> list[str]:
        """Each result's text content, joined, as the model would read it."""
        return [
            "".join(part["text"] for part in result["content"] if part.get("type") == "text")
            for result in self.results
        ]


def tool_call(name: str, arguments: Mapping[str, Any], log: ToolCallLog) -> Any:
    """A script step in which the CLI calls the `repo` server's tool `name` (its short name)."""

    async def step(options: ClaudeAgentOptions) -> None:
        log.calls.append((name, dict(arguments)))
        params = {"name": name, "arguments": dict(arguments)}
        log.results.append(await _rpc(options, "tools/call", params))

    return step


def pre_tool_use_hook(options: ClaudeAgentOptions) -> Any:
    """The one `PreToolUse` callback, checked to run for every tool name."""
    hooks = options.hooks
    assert hooks, "no hooks are set"
    assert set(hooks) == {"PreToolUse"}, f"hooks other than PreToolUse: {sorted(hooks)}"
    [matcher] = hooks["PreToolUse"]
    # A matcher naming tools would let every other name through unchecked.
    assert matcher.matcher in (None, "", "*"), f"the hook only runs for {matcher.matcher!r}"
    [callback] = matcher.hooks
    return callback


async def call_pre_tool_use(options: ClaudeAgentOptions, tool_name: Any) -> Any:
    """Call the hook as the CLI does before running `tool_name`."""
    callback = pre_tool_use_hook(options)
    hook_input = {
        "hook_event_name": "PreToolUse",
        "session_id": "session",
        "transcript_path": "/dev/null",
        "cwd": "/",
        "tool_name": tool_name,
        "tool_input": {},
        "tool_use_id": "toolu_1",
    }
    return await callback(hook_input, "toolu_1", {"signal": None})


def denial_reason(output: Any) -> str | None:
    """The reason if `output` denies the call, else None."""
    assert isinstance(output, dict), f"the hook returned {output!r}"
    specific = output.get("hookSpecificOutput") or {}
    if specific.get("permissionDecision") == "deny":
        assert specific.get("hookEventName") == "PreToolUse", specific
        reason = specific.get("permissionDecisionReason")
        assert isinstance(reason, str), specific
        return reason
    if output.get("decision") == "block":
        return str(output.get("reason", ""))
    return None


# A committed repository and the executors over it.


@dataclass(frozen=True, slots=True)
class CommittedRepo:
    path: Path  # what `--context-repo` names
    ref: RepoRef  # what the tools read


def commit_repo(path: Path, files: Mapping[str, str] = REPO_FILES) -> CommittedRepo:
    """A git repository at `path` holding `files` in one commit."""
    path.mkdir(parents=True)

    def git(*args: str) -> str:
        done = subprocess.run(  # noqa: S603 - fixed git arguments, test-owned repository
            ["git", "-C", str(path), *args],  # noqa: S607 - git from PATH, as the bot uses
            capture_output=True,
            check=True,
        )
        return done.stdout.decode("utf-8")

    git("init", "-q", "--initial-branch=main")
    git("config", "user.name", "Tester")
    git("config", "user.email", "tester@example.invalid")
    git("config", "commit.gpgsign", "false")
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    git("add", "-A")
    git("commit", "-q", "-m", "fixture")
    commit = git("rev-parse", "HEAD").strip()
    return CommittedRepo(path, RepoRef(GitRepo(path, timeout_s=30), commit))


def repo_tools(repo: CommittedRepo) -> RepoTools:
    return RepoTools(repo.ref, caps=DEFAULT_CAPS)


# With no "properties" key, the SDK would read this as a map of argument names to Python types.
_ANY_OBJECT = {"type": "object", "properties": {}, "additionalProperties": True}


class RecordingTools:
    """A `ToolExecutor` that records each call and answers with a fixed text."""

    def __init__(self, answer: str = "recorded-answer-7f3a") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answer = answer

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(ToolDefinition(name, f"The {name} tool.", _ANY_OBJECT) for name in SHORT_NAMES)

    async def call(self, name: str, arguments: Mapping[str, object]) -> str:
        self.calls.append((name, dict(arguments)))
        return self.answer
