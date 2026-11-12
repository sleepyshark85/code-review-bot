"""P4-S3: Claude's hook and `can_use_tool` enforce the tool policy.

Written from the spec's criteria, Limits and Interfaces, Phase 4 design.md ("Policy as code"),
ADR 0018 and Phase 3's "Tool safety", while the developer built the slice and before any of its
code was read.

Every review runs through `main` with `--policy` and `--context-repo`, so the policy reaches the
provider however the pipeline passes it. Claude runs over `fake_sdk.FakeSdk`; `fake_mcp` plays
the CLI: its init message, calls to the in-process `repo` server, the `PreToolUse` hook and the
`can_use_tool` callback. AC6 is in `tests/conformance/`; AC7 is a recorded live step.
"""

import asyncio
import dataclasses
import itertools
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow
from fake_mcp import (
    ALLOWED_TOOLS,
    CHECKOUT,
    DEFAULT_CAPS,
    PermissionLog,
    ToolCallLog,
    call_can_use_tool,
    call_pre_tool_use,
    can_use_tool_step,
    commit_repo,
    denial_reason,
    hook_step,
    init_message,
    list_server_tools,
    permission_denial,
    tool_call,
)
from fake_sdk import HIGH_FINDING, FakeSdk, findings_json, result_message
from test_p1_s4_audit_log import read_records

from review_bot import cli
from review_bot.context.ledger import ToolName
from review_bot.context.tools import RepoTools
from review_bot.providers.base import ReviewRequest, ReviewResponse
from review_bot.providers.claude_agent import ClaudeAgentProvider

pytestmark = pytest.mark.integration

FINDINGS_EXIT = 1
PERMANENT_EXIT = 12

VERSION = "org-s3.7_x"
READ = "mcp__repo__read"
GREP = "mcp__repo__grep"
GLOB = "mcp__repo__glob"
READ_GLOB = (READ, GLOB)
BUILTIN_VERSION = "builtin-v1"


def policy_text(*tools: str, version: str = VERSION) -> str:
    listed = ", ".join(f'"{tool}"' for tool in tools)
    return f'version = "{version}"\n\n[tools]\nallowed = [{listed}]\n'


class RecordingProvider:
    """Passes each request to `inner` and keeps what it answers, for criteria on the response."""

    def __init__(self, inner: ClaudeAgentProvider) -> None:
        self.inner = inner
        self.responses: list[ReviewResponse] = []

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        response = await self.inner.review(request)
        self.responses.append(response)
        return response

    @property
    def response(self) -> ReviewResponse:
        [response] = self.responses
        return response


@dataclasses.dataclass
class ContextRun:
    exit_code: int
    stdout: str
    stderr: str
    records: list[dict[str, Any]]
    provider: RecordingProvider

    def agent(self, event: str) -> list[dict[str, Any]]:
        return [r["data"] for r in self.records if r["stage"] == "agent" and r["event"] == event]


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return commit_repo(tmp_path_factory.mktemp("p4_s3") / "repo")


@pytest.fixture
def write_policy(tmp_path: Path) -> Callable[..., str]:
    counter = itertools.count()

    def write(*tools: str, version: str = VERSION) -> str:
        path = tmp_path / f"policy-{next(counter)}.toml"
        path.write_bytes(policy_text(*tools, version=version).encode("utf-8"))
        return str(path)

    return write


@pytest.fixture
def read_glob_policy(write_policy: Callable[..., str]) -> str:
    return write_policy("read", "glob")


@pytest.fixture
def review(bot, claude_argv, tmp_path, repo) -> Callable[..., ContextRun]:
    """Run `main` with Claude over `fake`, in context mode unless `context` is false.

    `policy` is a policy file's path, or None for the built-in policy.
    """
    counter = itertools.count()

    def run(fake: FakeSdk, policy: str | None, *, context: bool = True) -> ContextRun:
        audit_log = tmp_path / f"audit-{next(counter)}.jsonl"
        flags = ["--audit-log", str(audit_log)]
        if context:
            flags += ["--context-repo", str(repo.path)]
        if policy is not None:
            flags += ["--policy", policy]
        argv = claude_argv(*flags)
        _, settings = cli.parse_args(argv)
        provider = RecordingProvider(ClaudeAgentProvider(settings, sdk_call=fake))
        done = bot.run(*argv, provider=provider)
        records = read_records(audit_log) if audit_log.exists() else []
        return ContextRun(done.exit_code, done.stdout, done.stderr, records, provider)

    return run


def finds(*steps: Any, listed: Any = READ_GLOB) -> FakeSdk:
    """The CLI's init message listing `listed`, then `steps`, then the high finding."""
    return FakeSdk(init_message(listed), *steps, result_message(findings_json(HIGH_FINDING)))


@pytest.fixture
def options_under(review) -> Callable[..., ClaudeAgentOptions]:
    """The options of a successful context review under `policy` (whose init lists `listed`)."""

    def options(policy: str | None, listed: Any = READ_GLOB) -> ClaudeAgentOptions:
        fake = finds(listed=listed)
        run = review(fake, policy)
        assert run.exit_code == FINDINGS_EXIT, run.stderr
        return fake.options

    return options


@pytest.fixture
def read_glob_options(options_under, read_glob_policy) -> ClaudeAgentOptions:
    return options_under(read_glob_policy)


@pytest.fixture
def builtin_options(options_under) -> ClaudeAgentOptions:
    return options_under(None, listed=ALLOWED_TOOLS)


async def hook_says(options: ClaudeAgentOptions, name: Any) -> str | None:
    """The hook's denial reason for `name`, or None if it lets the call through."""
    output = await call_pre_tool_use(options, name)
    reason = denial_reason(output)
    if reason is None:
        assert output.get("continue_", True) is not False, output
    return reason


async def callback_says(options: ClaudeAgentOptions, name: Any) -> str | None:
    """`can_use_tool`'s denial message for `name`, or None if it allows it."""
    return permission_denial(await call_can_use_tool(options, name))


# Near misses of the allowed full names (P3-S3's list), and names the CLI itself offers.
LOOKALIKES = [
    "read",
    "glob",
    "Read",
    "Glob",
    "MCP__REPO__READ",
    "mcp__repo__Read",
    "mcp__repo__read ",
    " mcp__repo__read",
    "mcp__repo__readx",
    "mcp__repo__read\n",
    "mcp__repo__read\x00",
    "mcp__repo___read",
    "mcp__repo__",
    "mcp__repox__read",
    "mcp__other__read",
    "mcp__repo__read,mcp__repo__grep",
    "mcp__repo__*",
    "*",
    "",
]
OTHER_TOOLS = [GREP, "Bash", "Read", "Grep", "Glob", "Write", "WebFetch", "Task", "ToolSearch"]
NOT_TEXT = [None, 7, [READ], {"name": READ}]


# AC1: allowed_tools, the server and the init check follow the policy.


def test_p4_s3_ac1_allowed_tools_are_exactly_read_and_glob(read_glob_options):
    assert sorted(read_glob_options.allowed_tools) == sorted(READ_GLOB)
    assert len(read_glob_options.allowed_tools) == len(READ_GLOB)


async def test_p4_s3_ac1_server_lists_only_read_and_glob(read_glob_options):
    tools = await list_server_tools(read_glob_options)

    assert sorted(tool["name"] for tool in tools) == ["glob", "read"]


def test_p4_s3_ac1_init_listing_exactly_read_and_glob_in_any_order_proceeds(
    review, read_glob_policy
):
    run = review(finds(listed=[GLOB, READ]), read_glob_policy)

    assert run.exit_code == FINDINGS_EXIT, run.stderr


BAD_INIT_LISTS = {
    "the_three": ([*ALLOWED_TOOLS], [GREP]),
    "with_bash": ([*READ_GLOB, "Bash"], ["Bash"]),
    "with_other_server": ([*READ_GLOB, "mcp__other__read"], ["mcp__other__read"]),
    "without_glob": ([READ], [GLOB]),
    "builtins_only": (["Read", "Glob"], ["Read", "Glob"]),
    "empty": ([], list(READ_GLOB)),
}


@pytest.mark.parametrize(("listed", "named"), BAD_INIT_LISTS.values(), ids=BAD_INIT_LISTS.keys())
def test_p4_s3_ac1_init_listing_anything_else_fails_permanent(
    review, read_glob_policy, listed, named
):
    executor_log = ToolCallLog()
    fake = FakeSdk(
        init_message(listed),
        tool_call("read", {"path": CHECKOUT}, executor_log),
        result_message(findings_json(HIGH_FINDING)),
    )

    run = review(fake, read_glob_policy)

    assert run.exit_code == PERMANENT_EXIT, run.stderr
    assert "permanent" in run.stderr
    for name in named:
        assert name in run.stderr, run.stderr
    context_calls = [r for r in run.records if (r["stage"], r["event"]) == ("context", "tool_call")]
    assert context_calls == []


def test_p4_s3_ac1_builtin_policy_still_allows_the_three(builtin_options):
    assert sorted(builtin_options.allowed_tools) == sorted(ALLOWED_TOOLS)


# AC2: the PreToolUse hook.


async def test_p4_s3_ac2_hook_allows_read_and_glob(read_glob_options):
    for name in READ_GLOB:
        assert await hook_says(read_glob_options, name) is None, name


@pytest.mark.parametrize("name", [GREP, "Bash", "Read"])
async def test_p4_s3_ac2_hook_denies_grep_bash_and_read_naming_the_version(read_glob_options, name):
    reason = await hook_says(read_glob_options, name)

    assert reason is not None, f"{name} wasn't denied"
    assert VERSION in reason, reason


async def test_p4_s3_ac2_hook_denies_other_tools_and_lookalikes(read_glob_options):
    for name in [*OTHER_TOOLS, *LOOKALIKES]:
        reason = await hook_says(read_glob_options, name)
        assert reason is not None, f"{name!r} wasn't denied"
        assert VERSION in reason, (name, reason)


async def test_p4_s3_limits_hook_denies_a_name_that_isnt_text(read_glob_options):
    for name in NOT_TEXT:
        assert await hook_says(read_glob_options, name) is not None, f"{name!r} wasn't denied"


async def test_p4_s3_ac2_builtin_policy_hook_allows_the_three_and_names_builtin_v1(builtin_options):
    options = builtin_options

    for name in ALLOWED_TOOLS:
        assert await hook_says(options, name) is None, name
    reason = await hook_says(options, "Bash")
    assert reason is not None
    assert BUILTIN_VERSION in reason, reason


async def executor_denies(repo: Any, allowed: frozenset[ToolName], tool: ToolName) -> bool:
    tools = RepoTools(repo.ref, caps=DEFAULT_CAPS, allowed=allowed)
    await tools.call(tool, {})
    [entry] = tools.ledger
    return entry.reason == "policy_denied"


SUBSETS = [
    frozenset(combination)
    for size in range(1, len(ToolName) + 1)
    for combination in itertools.combinations(ToolName, size)
]


@pytest.mark.parametrize("allowed", SUBSETS, ids=lambda s: "+".join(sorted(s)))
def test_p4_s3_ac2_hook_decides_as_the_executor_for_every_policy(
    review, write_policy, repo, allowed
):
    # "It decides by the same pure function as the executor": for every tool set a policy can
    # name, the hook denies a repo tool exactly when the executor refuses it `policy_denied`.
    # Synchronous, since `main` runs its own event loop.
    listed = [f"mcp__repo__{tool}" for tool in ToolName if tool in allowed]
    fake = finds(listed=listed)
    run = review(fake, write_policy(*sorted(allowed)))
    assert run.exit_code == FINDINGS_EXIT, run.stderr

    for tool in ToolName:
        hook_denies = asyncio.run(hook_says(fake.options, f"mcp__repo__{tool}")) is not None
        assert hook_denies == asyncio.run(executor_denies(repo, allowed, tool)), tool


# AC3: can_use_tool, streaming input, no permission prompt tool.


def test_p4_s3_ac3_options_carry_a_can_use_tool_callback(read_glob_options):
    assert callable(read_glob_options.can_use_tool)


def test_p4_s3_ac3_permission_prompt_tool_name_is_unset(read_glob_options):
    assert read_glob_options.permission_prompt_tool_name is None


async def test_p4_s3_ac3_can_use_tool_allows_read_and_glob_by_full_name(read_glob_options):
    for name in READ_GLOB:
        tool_input = {"path": CHECKOUT}
        result = await call_can_use_tool(read_glob_options, name, tool_input)
        assert isinstance(result, PermissionResultAllow), (name, result)
        # Allowing never rewrites what the model asked for.
        assert result.updated_input in (None, tool_input), result


async def test_p4_s3_ac3_can_use_tool_denies_every_other_name_with_the_hooks_reason(
    read_glob_options,
):
    for name in [*OTHER_TOOLS, *LOOKALIKES, *NOT_TEXT]:
        message = await callback_says(read_glob_options, name)
        assert message is not None, f"{name!r} was allowed"
        assert message == await hook_says(read_glob_options, name), name
        assert VERSION in message, (name, message)


async def test_p4_s3_ac3_builtin_policy_can_use_tool_allows_the_three(builtin_options):
    options = builtin_options

    for name in ALLOWED_TOOLS:
        assert await callback_says(options, name) is None, name
    message = await callback_says(options, "Bash")
    assert message is not None
    assert BUILTIN_VERSION in message


def user_text(messages: list[Any]) -> str:
    """The text of streaming input's user messages, as the SDK would send it."""
    texts: list[str] = []
    for message in messages:
        assert message["type"] == "user", message
        assert message["message"]["role"] == "user", message
        content = message["message"]["content"]
        if isinstance(content, str):
            texts.append(content)
        else:
            texts.extend(block["text"] for block in content if block["type"] == "text")
    return "".join(texts)


@pytest.fixture
def mock_user_prompt(bot, claude_argv, repo) -> Callable[..., str]:
    """The user prompt the pipeline builds for the same review, as `MockProvider` receives it."""

    def prompt(policy: str, *, context: bool) -> str:
        provider = bot.mock(findings=[])
        flags = ["--context-repo", str(repo.path)] if context else []
        argv = [claude_argv()[0], "--provider", "mock", "--policy", policy, *flags]
        run = bot.run(*argv, provider=provider)
        assert run.exit_code == 0, run.stderr
        [request] = provider.requests
        return request.user_prompt

    return prompt


def test_p4_s3_ac3_prompt_is_streaming_input_with_the_same_user_prompt_text(
    review, read_glob_policy, mock_user_prompt
):
    fake = finds()

    run = review(fake, read_glob_policy)

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    [(prompt, _)] = fake.calls
    assert not isinstance(prompt, str | bytes)
    assert fake.input_messages, "the streaming prompt held no message"
    expected = mock_user_prompt(read_glob_policy, context=True)
    assert user_text(fake.input_messages) == expected


# AC2, AC3 Limits: the CLI's tool name is untrusted; it is shown capped and escaped.


HOSTILE_NAMES = {
    "huge": "X" * 1_000_000,
    "control": "Bash\n\x1b[31mred\r\x07\x00\u202e",
    "huge_control": "\n\x1b" * 200_000,
}


@pytest.mark.parametrize("name", HOSTILE_NAMES.values(), ids=HOSTILE_NAMES.keys())
async def test_p4_s3_limits_hostile_names_give_bounded_printable_reasons(read_glob_options, name):
    started = time.monotonic()
    reasons = [
        await hook_says(read_glob_options, name),
        await callback_says(read_glob_options, name),
    ]
    # Exact comparison is linear; generous headroom for a slow CI runner.
    assert time.monotonic() - started < 5

    for reason in reasons:
        assert reason is not None
        assert len(reason) < 2_000, len(reason)
        assert reason.isprintable(), repr(reason[:200])


# AC4: denials are carried in the response and recorded.


def denied(run: ContextRun) -> list[tuple[str, str]]:
    return [
        (item.tool, str(getattr(item.layer, "value", item.layer)))
        for item in run.provider.response.denied_tools
    ]


def test_p4_s3_ac4_each_denial_is_in_the_response_and_the_audit_log(review, read_glob_policy):
    answers = PermissionLog()
    fake = finds(
        hook_step("Bash", answers),
        can_use_tool_step(GREP, answers),
        hook_step(READ, answers),
        can_use_tool_step(GLOB, answers),
        hook_step(GREP, answers),
    )

    run = review(fake, read_glob_policy)

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    expected = [("Bash", "hook"), (GREP, "can_use_tool"), (GREP, "hook")]
    assert denied(run) == expected
    records = run.agent("denied")
    assert [(r["tool"], r["layer"]) for r in records] == expected
    assert all(r["policy_version"] == VERSION for r in records), records


def test_p4_s3_ac4_review_goes_on_after_denials(review, read_glob_policy):
    log = ToolCallLog()
    fake = finds(
        hook_step("Bash"),
        can_use_tool_step("Write"),
        tool_call("read", {"path": CHECKOUT}, log),
    )

    run = review(fake, read_glob_policy)

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    assert len(run.provider.response.findings) == 1
    assert len(log.texts) == 1
    assert len(run.agent("called")) == 1


def test_p4_s3_ac4_no_denial_means_no_record_and_an_empty_tuple(review, read_glob_policy):
    fake = finds(hook_step(READ), can_use_tool_step(GLOB))

    run = review(fake, read_glob_policy)

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    assert run.provider.response.denied_tools == ()
    assert run.agent("denied") == []


def test_p4_s3_ac4_one_record_per_denial_even_when_repeated(review, read_glob_policy):
    steps = [hook_step("Bash") for _ in range(40)] + [can_use_tool_step("Bash") for _ in range(40)]

    run = review(finds(*steps), read_glob_policy)

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    assert len(run.provider.response.denied_tools) == 80
    records = run.agent("denied")
    assert len(records) == 80
    assert [r["layer"] for r in records] == ["hook"] * 40 + ["can_use_tool"] * 40


FAILURES_AFTER_DENIALS = {
    "cost_cap": result_message(None, is_error=True, subtype="error_max_budget_usd"),
    "turn_cap": result_message(None, is_error=True, subtype="error_max_turns"),
    # The CLI re-sends init, now listing a tool outside the policy: the init check fails.
    "init_check": init_message([*READ_GLOB, "Bash"]),
}


@pytest.mark.parametrize(
    "failure", FAILURES_AFTER_DENIALS.values(), ids=FAILURES_AFTER_DENIALS.keys()
)
def test_p4_s3_ac4_denials_before_a_failure_are_recorded_before_failed(
    review, read_glob_policy, failure
):
    fake = FakeSdk(init_message(READ_GLOB), hook_step("Bash"), can_use_tool_step(GREP), failure)

    run = review(fake, read_glob_policy)

    assert run.exit_code == PERMANENT_EXIT, run.stderr
    events = [r["event"] for r in run.records if r["stage"] == "agent"]
    assert events[-3:] == ["denied", "denied", "failed"], events
    records = run.agent("denied")
    assert [(r["tool"], r["layer"]) for r in records] == [
        ("Bash", "hook"),
        (GREP, "can_use_tool"),
    ]
    assert all(r["policy_version"] == VERSION for r in records), records


def test_p4_s3_ac4_builtin_policy_records_builtin_v1(review):
    run = review(finds(hook_step("Bash"), listed=ALLOWED_TOOLS), None)

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    [record] = run.agent("denied")
    assert record["policy_version"] == BUILTIN_VERSION


@pytest.mark.parametrize("layer", ["hook", "can_use_tool"])
def test_p4_s3_ac4_long_tool_name_is_recorded_capped_at_100(review, read_glob_policy, layer):
    name = "Z" * 10_000
    step = hook_step(name) if layer == "hook" else can_use_tool_step(name)

    run = review(finds(step), read_glob_policy)

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    [record] = run.agent("denied")
    assert record["layer"] == layer
    assert 0 < len(record["tool"]) <= 100
    assert name.startswith(record["tool"])


@pytest.mark.parametrize("layer", ["hook", "can_use_tool"])
def test_p4_s3_ac4_unprintable_tool_name_is_recorded_escaped(review, read_glob_policy, layer):
    name = "Bash\n\x1b[31m\u202e"
    step = hook_step(name) if layer == "hook" else can_use_tool_step(name)

    run = review(finds(step), read_glob_policy)

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    [record] = run.agent("denied")
    tool = record["tool"]
    assert tool.startswith("Bash"), tool
    assert tool.isprintable(), repr(tool)
    assert "\\n" in tool, tool
    for text in (run.stdout, run.stderr):
        assert "\x1b" not in text
        assert "\u202e" not in text


@pytest.mark.parametrize("name", NOT_TEXT, ids=repr)
def test_p4_s3_limits_denying_a_name_that_isnt_text_is_recorded_and_the_review_goes_on(
    review, read_glob_policy, name
):
    run = review(finds(hook_step(name), can_use_tool_step(name)), read_glob_policy)

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    records = run.agent("denied")
    assert [r["layer"] for r in records] == ["hook", "can_use_tool"]
    for record in records:
        assert isinstance(record["tool"], str)
        assert len(record["tool"]) <= 100


# AC5: diff mode is exactly as in Phase 3.


PHASE_3_DIFF_FIELDS = {
    "tools": [],
    "allowed_tools": [],
    "setting_sources": [],
    "mcp_servers": {},
    "strict_mcp_config": True,
    "max_turns": 1,
    "max_budget_usd": None,
    "env": {"CLAUDE_CODE_MAX_RETRIES": "2", "API_TIMEOUT_MS": "90000"},
}


@pytest.fixture
def diff_run(review) -> Callable[..., tuple[ContextRun, FakeSdk]]:
    def run(policy: str | None) -> tuple[ContextRun, FakeSdk]:
        fake = FakeSdk(init_message(), result_message(findings_json(HIGH_FINDING)))
        done = review(fake, policy, context=False)
        assert done.exit_code == FINDINGS_EXIT, done.stderr
        return done, fake

    return run


@pytest.mark.parametrize("policy_kind", ["builtin", "read_glob"])
def test_p4_s3_ac5_diff_mode_options_are_phase_3s_exactly(
    diff_run, read_glob_policy, claude_argv, policy_kind
):
    policy = None if policy_kind == "builtin" else read_glob_policy
    _, fake = diff_run(policy)

    options = fake.options
    defaults = ClaudeAgentOptions()
    _, settings = cli.parse_args(claude_argv())
    expected = {**PHASE_3_DIFF_FIELDS, "model": settings.model}
    for option in dataclasses.fields(ClaudeAgentOptions):
        name = option.name
        actual = getattr(options, name)
        if name in expected:
            assert actual == expected[name], name
        elif name in {"hooks", "agents"}:
            assert not actual, name
        elif name not in {"stderr", "system_prompt"}:
            assert actual == getattr(defaults, name), f"{name} differs from the SDK default"
    assert options.can_use_tool is None
    assert options.permission_prompt_tool_name is None
    assert not options.hooks


@pytest.mark.parametrize("policy_kind", ["builtin", "read_glob"])
def test_p4_s3_ac5_diff_mode_prompt_is_the_same_string(
    diff_run, read_glob_policy, mock_user_prompt, policy_kind
):
    policy = read_glob_policy if policy_kind == "read_glob" else None
    _, fake = diff_run(policy)

    [(prompt, _)] = fake.calls
    assert isinstance(prompt, str)
    expected = mock_user_prompt(read_glob_policy, context=False)
    assert prompt == expected
    assert fake.input_messages == []


def test_p4_s3_ac5_diff_mode_options_dont_depend_on_the_policy(diff_run, read_glob_policy):
    _, builtin = diff_run(None)
    _, read_glob = diff_run(read_glob_policy)

    for option in dataclasses.fields(ClaudeAgentOptions):
        if option.name != "stderr":
            name = option.name
            assert getattr(read_glob.options, name) == getattr(builtin.options, name), name


def test_p4_s3_ac5_diff_mode_records_no_denial_and_an_empty_tuple(diff_run, read_glob_policy):
    run, _ = diff_run(read_glob_policy)

    assert run.provider.response.denied_tools == ()
    assert run.agent("denied") == []
