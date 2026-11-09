"""P3-S3: let Claude drive the tools, locked down and cost-capped.

Written from the spec's criteria, Limits and Interfaces, Phase 3 design.md ("Tool safety",
"Spend and caps", "Interfaces") and ADR 0015, while the developer built the slice and before any
of its code was read.

`ClaudeAgentProvider` runs over `fake_sdk.FakeSdk`, as in Phase 0: no CLI is launched. Where the
CLI would use the provider's tools, `fake_mcp` does it the CLI's way: it calls the in-process
`repo` server through the SDK's own JSON-RPC bridge, and calls the `PreToolUse` hook with the
CLI's hook input. Every context-mode script starts with the CLI's init `SystemMessage`, listing
exactly the three tools unless the test is about that list (AC10).

AC7's tool-loop case is in `tests/conformance/test_p3_s3_tool_loop_conformance.py`. AC9 moved to
P3-S16.
"""

import dataclasses
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultError
from fake_mcp import (
    ALLOWED_TOOLS,
    CHECKOUT,
    SERVER,
    SHORT_NAMES,
    RecordingTools,
    ToolCallLog,
    call_pre_tool_use,
    commit_repo,
    denial_reason,
    init_message,
    list_server_tools,
    pre_tool_use_hook,
    repo_tools,
    tool_call,
)
from fake_sdk import (
    HIGH_FINDING,
    FakeSdk,
    cli_stderr,
    error_result,
    findings_json,
    result_message,
)
from test_p1_s4_audit_log import read_records

from review_bot import cli
from review_bot.context.tools import RepoTools, ToolCaps
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.prompt import build_context_request, build_request
from review_bot.providers.base import ReviewRequest
from review_bot.providers.claude_agent import ClaudeAgentProvider

pytestmark = pytest.mark.integration

USAGE_EXIT = 2
FINDINGS_EXIT = 1
PERMANENT_EXIT = 12
ERROR = "review-bot: error: "
COST_FLAG = "--max-cost-usd"
COST_VARIABLE = "REVIEW_BOT_MAX_COST_USD"
TURNS_FLAG = "--max-tool-calls"
# Text the CLI or the model could send, which no message of ours may repeat (AC5).
CLI_TEXT = "CLI-TEXT-SENTINEL-4b1e"
MODEL_TEXT = "MODEL-TEXT-SENTINEL-9c2d"

READ_CHECKOUT = {"path": CHECKOUT}
FINDING = Finding(**HIGH_FINDING)


def settings_for(*flags: str) -> Any:
    _, settings = cli.parse_args(["change.diff", "--provider", "claude", *flags])
    return settings


def provider_for(fake: FakeSdk, *flags: str) -> ClaudeAgentProvider:
    return ClaudeAgentProvider(settings_for(*flags), sdk_call=fake)


def context_request(tools: Any) -> ReviewRequest:
    return build_context_request("+x\n", model="claude-test-model", tools=tools)


def diff_request() -> ReviewRequest:
    return build_request("+x\n", model="claude-test-model")


def context_success(*steps: Any, **result_fields: Any) -> FakeSdk:
    """The CLI's init message, then `steps`, then a successful result with the high finding."""
    return FakeSdk(
        init_message(), *steps, result_message(findings_json(HIGH_FINDING), **result_fields)
    )


async def context_options(*flags: str) -> ClaudeAgentOptions:
    fake = context_success()
    await provider_for(fake, *flags).review(context_request(RecordingTools()))
    return fake.options


async def diff_options(*flags: str) -> ClaudeAgentOptions:
    fake = FakeSdk(result_message(findings_json()))
    await provider_for(fake, *flags).review(diff_request())
    return fake.options


async def raised_by(fake: FakeSdk, request: ReviewRequest, *flags: str) -> ProviderError:
    with pytest.raises(ProviderError) as caught:
        await provider_for(fake, *flags).review(request)
    return caught.value


# Running the bot end to end, with Claude over a fake.


@pytest.fixture
def repo(tmp_path: Path) -> Any:
    return commit_repo(tmp_path / "repo")


@pytest.fixture
def context_review(bot, claude_argv, tmp_path, repo):
    """Run `main` in context mode over `repo`, with Claude over `fake`.

    Returns the run and the audit log's records.
    """
    audit_log = tmp_path / "audit.jsonl"

    def review(fake: FakeSdk, *flags: str) -> tuple[Any, list[dict[str, Any]]]:
        argv = claude_argv("--context-repo", str(repo.path), "--audit-log", str(audit_log), *flags)
        _, settings = cli.parse_args(argv)
        run = bot.run(*argv, provider=ClaudeAgentProvider(settings, sdk_call=fake))
        return run, read_records(audit_log) if audit_log.exists() else []

    return review


# AC1: context mode's options.


async def test_p3_s3_ac1_system_prompt_is_the_context_prompt():
    options = await context_options()

    expected = context_request(RecordingTools()).system_prompt
    assert options.system_prompt == expected
    assert options.system_prompt != diff_request().system_prompt


async def test_p3_s3_ac1_builtin_tools_are_off():
    options = await context_options()

    assert options.tools == []


async def test_p3_s3_ac1_allowed_tools_are_exactly_the_three_full_names():
    options = await context_options()

    assert sorted(options.allowed_tools) == sorted(ALLOWED_TOOLS)
    assert len(options.allowed_tools) == len(ALLOWED_TOOLS)


async def test_p3_s3_ac1_only_the_in_process_repo_server_is_configured():
    options = await context_options()

    assert isinstance(options.mcp_servers, dict)
    assert list(options.mcp_servers) == [SERVER]
    assert options.mcp_servers[SERVER]["type"] == "sdk"


async def test_p3_s3_ac1_repo_server_offers_exactly_read_grep_and_glob():
    options = await context_options()

    tools = await list_server_tools(options)

    assert sorted(tool["name"] for tool in tools) == sorted(SHORT_NAMES)


async def test_p3_s3_ac1_strict_mcp_config_no_settings_no_agents_dont_ask():
    options = await context_options()

    assert options.strict_mcp_config is True
    assert options.setting_sources == []
    assert not options.agents
    assert options.permission_mode == "dontAsk"


@pytest.mark.parametrize(
    ("flags", "turns"),
    [((), 14), (("--max-tool-calls", "12"), 14), (("--max-tool-calls", "5"), 7)],
    ids=["default_12", "flag_12", "flag_5"],
)
async def test_p3_s3_ac1_max_turns_is_max_tool_calls_plus_2(flags, turns):
    options = await context_options(*flags)

    assert options.max_turns == turns


async def test_p3_s3_ac1_hooks_hold_only_one_pre_tool_use_hook():
    options = await context_options()

    # Checks the keys, one matcher matching every tool, and one callback.
    assert callable(pre_tool_use_hook(options))


@pytest.mark.parametrize(
    "flags",
    [(), ("--max-retries", "0", "--api-timeout-ms", "1000", "--max-output-tokens", "4000")],
    ids=["defaults", "flags"],
)
async def test_p3_s3_ac1_env_is_the_same_as_diff_mode(flags):
    context = await context_options(*flags)
    diff = await diff_options(*flags)

    assert context.env == diff.env


async def test_p3_s3_ac1_env_stays_exact_with_a_key_in_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-sentinel")

    options = await context_options()

    assert options.env == {"CLAUDE_CODE_MAX_RETRIES": "2", "API_TIMEOUT_MS": "90000"}


async def test_p3_s3_ac1_stderr_is_still_held_by_the_provider():
    options = await context_options()

    assert callable(options.stderr)


# AC2: diff mode unchanged; the budget in both modes.


PHASE_0_FIELDS = {
    "tools": [],
    "allowed_tools": [],
    "setting_sources": [],
    "mcp_servers": {},
    "strict_mcp_config": True,
    "max_turns": 1,
    "max_budget_usd": None,
}


async def test_p3_s3_ac2_diff_mode_options_are_phase_0s_exactly():
    options = await diff_options()

    defaults = ClaudeAgentOptions()
    expected = {
        **PHASE_0_FIELDS,
        "system_prompt": diff_request().system_prompt,
        "model": "claude-test-model",
        "env": {"CLAUDE_CODE_MAX_RETRIES": "2", "API_TIMEOUT_MS": "90000"},
    }
    for option in dataclasses.fields(ClaudeAgentOptions):
        name = option.name
        actual = getattr(options, name)
        if name in expected:
            assert actual == expected[name], name
        elif name in {"hooks", "agents"}:
            assert not actual, name
        elif name != "stderr":
            assert actual == getattr(defaults, name), f"{name} differs from the SDK default"


@pytest.mark.parametrize(
    ("value", "budget"),
    [
        ("0.309", 0.30),
        ("0.01", 0.01),
        ("0.019999", 0.01),
        ("0.999", 0.99),
        ("0.60", 0.60),
        ("1", 1.0),
        ("2.5", 2.5),
        ("999.999", 999.99),
    ],
)
@pytest.mark.parametrize("mode", ["diff", "context"])
async def test_p3_s3_ac2_budget_is_the_cap_rounded_down_to_the_cent(mode, value, budget):
    build = diff_options if mode == "diff" else context_options

    options = await build(COST_FLAG, value)

    assert type(options.max_budget_usd) is float
    assert options.max_budget_usd == budget
    # At the float's shortest decimal form: no binary float is exactly 0.01.
    assert Decimal(repr(options.max_budget_usd)) <= Decimal(value)


@pytest.mark.parametrize("mode", ["diff", "context"])
async def test_p3_s3_ac2_no_budget_without_the_flag(mode):
    build = diff_options if mode == "diff" else context_options

    options = await build()

    assert options.max_budget_usd is None


@pytest.mark.parametrize("mode", ["diff", "context"])
async def test_p3_s3_ac2_variable_sets_the_budget(monkeypatch, mode):
    monkeypatch.setenv(COST_VARIABLE, "0.309")
    build = diff_options if mode == "diff" else context_options

    options = await build()

    assert options.max_budget_usd == 0.30


async def test_p3_s3_ac2_diff_mode_with_a_budget_changes_nothing_else():
    without = await diff_options()
    with_cap = await diff_options(COST_FLAG, "0.5")

    for option in dataclasses.fields(ClaudeAgentOptions):
        if option.name not in {"max_budget_usd", "stderr"}:
            assert getattr(with_cap, option.name) == getattr(without, option.name), option.name


def test_p3_s3_ac2_diff_mode_through_main_stays_locked_down(claude_review):
    fake = FakeSdk(result_message(findings_json()))

    run = claude_review(fake, COST_FLAG, "0.309")

    assert run.exit_code == 0, run.stderr
    options = fake.options
    assert options.tools == []
    assert options.allowed_tools == []
    assert options.mcp_servers == {}
    assert options.max_turns == 1
    assert not options.hooks
    assert options.max_budget_usd == 0.30


# AC3: a read through the server reaches the executor and back.


async def test_p3_s3_ac3_read_is_recorded_in_the_executors_ledger(repo):
    tools = repo_tools(repo)
    log = ToolCallLog()
    fake = context_success(tool_call("read", READ_CHECKOUT, log))

    await provider_for(fake).review(context_request(tools))

    [entry] = tools.ledger
    assert entry.tool == "read"
    assert entry.outcome == "ok"


async def test_p3_s3_ac3_fake_receives_the_executors_text(repo):
    tools = repo_tools(repo)
    log = ToolCallLog()
    fake = context_success(tool_call("read", READ_CHECKOUT, log))

    await provider_for(fake).review(context_request(tools))

    expected = await repo_tools(repo).call("read", READ_CHECKOUT)
    assert log.texts == [expected]
    assert not log.results[0].get("isError")


async def test_p3_s3_ac3_findings_of_the_final_result_are_returned(repo):
    log = ToolCallLog()
    fake = context_success(tool_call("read", READ_CHECKOUT, log))

    response = await provider_for(fake).review(context_request(repo_tools(repo)))

    assert response.findings == (FINDING,)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("read", {"path": CHECKOUT, "start_line": 3, "line_count": 2}),
        ("grep", {"literal": "line_1", "path_glob": "billing/*.py", "ignore_case": True}),
        ("glob", {"pattern": "**"}),
        # Limits: invalid arguments still reach the executor, which refuses them itself.
        ("read", {"path": "../../etc/passwd"}),
        ("read", {"path": CHECKOUT, "unknown_key": [1, {"nested": None}]}),
    ],
    ids=["read", "grep", "glob", "dotdot_path", "unknown_key"],
)
async def test_p3_s3_ac3_arguments_reach_the_executor_unchanged(name, arguments):
    tools = RecordingTools()
    log = ToolCallLog()
    fake = context_success(tool_call(name, arguments, log))

    await provider_for(fake).review(context_request(tools))

    assert tools.calls == [(name, arguments)]
    assert log.texts == [tools.answer]


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": CHECKOUT, "unknown_key": 1},
        {"path": 5},
        {"path": CHECKOUT, "line_count": 401},
        {},
    ],
    ids=["unknown_key", "path_not_text", "line_count_over_400", "no_path"],
)
async def test_p3_s3_limits_invalid_arguments_reach_the_executor_and_its_ledger(repo, arguments):
    # Limits: "Tool arguments reach the executor unchanged, which validates them": the refusal is
    # the executor's own, delimited text, and the call is in the ledger, not stopped on the way.
    tools = repo_tools(repo)
    log = ToolCallLog()
    fake = context_success(tool_call("read", arguments, log))

    response = await provider_for(fake).review(context_request(tools))

    expected = await repo_tools(repo).call("read", arguments)
    assert log.texts == [expected]
    [entry] = tools.ledger
    assert entry.outcome == "refused"
    assert response.findings == (FINDING,)


async def test_p3_s3_ac3_refusal_text_is_passed_back_as_the_result(repo):
    tools = repo_tools(repo)
    log = ToolCallLog()
    arguments = {"path": "billing/missing.py"}
    fake = context_success(tool_call("read", arguments, log))

    await provider_for(fake).review(context_request(tools))

    expected = await repo_tools(repo).call("read", arguments)
    assert log.texts == [expected]
    [entry] = tools.ledger
    assert entry.outcome == "refused"


async def test_p3_s3_ac3_several_calls_are_recorded_in_order(repo):
    tools = repo_tools(repo)
    log = ToolCallLog()
    fake = context_success(
        tool_call("glob", {"pattern": "billing/*.py"}, log),
        tool_call("grep", {"literal": "line_12"}, log),
        tool_call("read", READ_CHECKOUT, log),
    )

    response = await provider_for(fake).review(context_request(tools))

    assert [entry.tool for entry in tools.ledger] == ["glob", "grep", "read"]
    assert response.findings == (FINDING,)


def test_p3_s3_ac3_review_through_main_reads_and_reports(context_review):
    log = ToolCallLog()
    fake = context_success(tool_call("read", READ_CHECKOUT, log))

    run, records = context_review(fake)

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    assert len(log.texts) == 1
    tool_calls = [
        record["data"]["tool"]
        for record in records
        if record["stage"] == "context" and record["event"] == "tool_call"
    ]
    assert tool_calls == ["read"]


# AC4: the PreToolUse hook.


DENIED_NAMES = [
    "Bash",
    "Read",
    "Write",
    "WebFetch",
    "Task",
    "ToolSearch",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
    "mcp__other__read",
]
# design.md: the hook denies "any other name", so near misses of the three are denied too.
LOOKALIKE_NAMES = [
    "read",
    "grep",
    "glob",
    "mcp__repo__write",
    "mcp__repo__Read",
    "MCP__REPO__READ",
    "mcp__repo__read ",
    " mcp__repo__read",
    "mcp__repo__readx",
    "mcp__repo__read\n",
    "mcp__repo___read",
    "mcp__repo__",
    "mcp__repox__read",
    "",
]


@pytest.mark.parametrize("name", DENIED_NAMES)
async def test_p3_s3_ac4_hook_denies_other_tools_naming_the_allowed_ones(name):
    options = await context_options()

    reason = denial_reason(await call_pre_tool_use(options, name))

    assert reason is not None, f"{name} wasn't denied"
    for allowed in ALLOWED_TOOLS:
        assert allowed in reason, reason


@pytest.mark.parametrize("name", LOOKALIKE_NAMES, ids=repr)
async def test_p3_s3_ac4_hook_denies_lookalikes_of_the_allowed_names(name):
    options = await context_options()

    assert denial_reason(await call_pre_tool_use(options, name)) is not None


@pytest.mark.parametrize("name", ALLOWED_TOOLS)
async def test_p3_s3_ac4_hook_does_not_deny_the_three(name):
    options = await context_options()

    output = await call_pre_tool_use(options, name)

    assert denial_reason(output) is None
    assert output.get("continue_", True) is not False


@pytest.mark.parametrize("name", [None, 7, ["mcp__repo__read"], {"name": "mcp__repo__read"}])
async def test_p3_s3_limits_hook_denies_a_tool_name_that_isnt_text(name):
    # Limits: the CLI's fields are untrusted; a name that isn't a string is none of the three.
    options = await context_options()

    assert denial_reason(await call_pre_tool_use(options, name)) is not None


# AC5: hitting the turn or cost cap.


CAP_CASES = [
    ("error_max_turns", TURNS_FLAG, "turns"),
    ("error_max_budget_usd", COST_FLAG, "cost"),
]
CAP_IDS = ["turns", "cost"]


def cap_result(subtype: str, *, is_error: bool = True, text: str = MODEL_TEXT) -> Any:
    return result_message(text, is_error=is_error, subtype=subtype, errors=[CLI_TEXT])


def cap_scripts(subtype: str) -> dict[str, tuple[Any, ...]]:
    """The ways the CLI ends a capped query: its result alone, or followed by the SDK's error."""
    data = {"subtype": subtype, "is_error": True, "result": MODEL_TEXT, "errors": [CLI_TEXT]}
    raised = ResultError(
        f"Claude Code returned an error result: {CLI_TEXT}", data=data, exit_code=1
    )
    return {
        "result": (cap_result(subtype),),
        "result_not_is_error": (cap_result(subtype, is_error=False),),
        "result_with_findings": (cap_result(subtype, text=findings_json(HIGH_FINDING)),),
        "result_then_result_error": (cap_result(subtype), raised),
    }


SCRIPT_KINDS = ["result", "result_not_is_error", "result_with_findings", "result_then_result_error"]


@pytest.mark.parametrize("kind", SCRIPT_KINDS)
@pytest.mark.parametrize(("subtype", "flag", "cap"), CAP_CASES, ids=CAP_IDS)
async def test_p3_s3_ac5_cap_hit_is_permanent_naming_its_flag_and_cap(subtype, flag, cap, kind):
    fake = FakeSdk(init_message(), cli_stderr(CLI_TEXT), *cap_scripts(subtype)[kind])

    error = await raised_by(fake, context_request(RecordingTools()))

    assert error.category is ErrorCategory.PERMANENT
    assert flag in str(error)
    assert error.cap == cap


async def test_p3_s3_ac5_cost_cap_hit_in_diff_mode_is_the_same_error():
    fake = FakeSdk(cap_result("error_max_budget_usd"))

    error = await raised_by(fake, diff_request())

    assert error.category is ErrorCategory.PERMANENT
    assert COST_FLAG in str(error)
    assert error.cap == "cost"


async def test_p3_s3_ac5_turn_cap_hit_in_diff_mode_is_phase_0s_permanent_error():
    # `is_error` true, as the SDK documents a capped run; with it false, Phase 0 reads the text.
    fake = FakeSdk(cli_stderr(CLI_TEXT), cap_result("error_max_turns"))

    error = await raised_by(fake, diff_request())

    phase_0 = await raised_by(FakeSdk(error_result(400, "api_error")), diff_request())
    assert error.category is ErrorCategory.PERMANENT
    assert error.cap is None
    assert TURNS_FLAG not in str(error)
    assert str(error) == str(phase_0)
    assert CLI_TEXT not in str(error)
    assert MODEL_TEXT not in str(error)


@pytest.mark.parametrize(("subtype", "_flag", "_cap"), CAP_CASES, ids=CAP_IDS)
async def test_p3_s3_ac5_message_holds_no_cli_or_model_text(subtype, _flag, _cap):
    fake = FakeSdk(init_message(), cli_stderr(CLI_TEXT), *cap_scripts(subtype)["result"])

    error = await raised_by(fake, context_request(RecordingTools()))

    assert CLI_TEXT not in str(error)
    assert MODEL_TEXT not in str(error)


@pytest.mark.parametrize(("subtype", "flag", "_cap"), CAP_CASES, ids=CAP_IDS)
def test_p3_s3_ac5_cap_hit_through_main_exits_permanent_naming_the_flag(
    context_review, subtype, flag, _cap
):
    fake = FakeSdk(init_message(), cli_stderr(CLI_TEXT), cap_result(subtype))

    run, _ = context_review(fake, COST_FLAG, "0.5")

    assert run.exit_code == PERMANENT_EXIT, run.stderr
    errors = [line for line in run.stderr.splitlines() if line.startswith(ERROR)]
    assert any(flag in line for line in errors), run.stderr
    for text in (CLI_TEXT, MODEL_TEXT):
        assert text not in run.stderr
        assert text not in run.stdout


def agent_records(records: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [r["data"] for r in records if r["stage"] == "agent" and r["event"] == event]


@pytest.mark.parametrize(("subtype", "_flag", "cap"), CAP_CASES, ids=CAP_IDS)
def test_p3_s3_ac5_audit_failed_record_carries_the_cap(context_review, subtype, _flag, cap):
    fake = FakeSdk(init_message(), cap_result(subtype))

    run, records = context_review(fake)

    assert run.exit_code == PERMANENT_EXIT, run.stderr
    [failed] = agent_records(records, "failed")
    assert failed["cap"] == cap


def test_p3_s3_ac5_audit_failed_record_has_a_null_cap_for_other_errors(context_review):
    fake = FakeSdk(init_message(), error_result(400, "api_error"))

    run, records = context_review(fake)

    assert run.exit_code == PERMANENT_EXIT, run.stderr
    [failed] = agent_records(records, "failed")
    assert "cap" in failed
    assert failed["cap"] is None


def test_p3_s3_ac5_audit_failed_record_has_a_null_cap_for_diff_mode_turns(claude_review, tmp_path):
    audit_log = tmp_path / "diff-audit.jsonl"
    fake = FakeSdk(cap_result("error_max_turns"))

    run = claude_review(fake, "--audit-log", str(audit_log))

    assert run.exit_code == PERMANENT_EXIT, run.stderr
    [failed] = agent_records(read_records(audit_log), "failed")
    assert failed["cap"] is None


OTHER_FAILURES = {
    "permanent_status": (error_result(400, "api_error"),),
    "auth": (error_result(401, "api_error"),),
    "transient": (error_result(503, "api_error"),),
    "refusal": (result_message("I won't.", stop_reason="refusal"),),
    "error_during_execution": (
        result_message(None, is_error=True, subtype="error_during_execution"),
    ),
    "invalid_output": (result_message("not json"),),
    "no_result": (),
}


@pytest.mark.parametrize("script", OTHER_FAILURES.values(), ids=OTHER_FAILURES.keys())
async def test_p3_s3_ac5_every_other_error_has_no_cap(script):
    fake = FakeSdk(init_message(), *script)

    error = await raised_by(fake, context_request(RecordingTools()))

    assert error.cap is None


def test_p3_s3_ac5_a_provider_error_has_no_cap_by_default():
    assert ProviderError(ErrorCategory.PERMANENT, "detail").cap is None


# AC6: the reported cost.


async def test_p3_s3_ac6_cost_is_the_exact_decimal_of_the_reported_value():
    fake = context_success(total_cost_usd=0.1234)

    response = await provider_for(fake).review(context_request(RecordingTools()))

    assert response.cost_usd == Decimal("0.1234")
    assert str(response.cost_usd) == "0.1234"


async def test_p3_s3_ac6_cost_is_read_in_diff_mode_too():
    fake = FakeSdk(result_message(findings_json(), total_cost_usd=0.1234))

    response = await provider_for(fake).review(diff_request())

    assert response.cost_usd == Decimal("0.1234")


def json_report(run: Any) -> dict[str, Any]:
    document = json.loads(run.stdout)
    assert document["schema"] == "report/v1"
    return document


def test_p3_s3_ac6_report_usage_cost_is_the_string(claude_review):
    fake = FakeSdk(result_message(findings_json(HIGH_FINDING), total_cost_usd=0.1234))

    run = claude_review(fake, "--format", "json")

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    assert json_report(run)["usage"]["cost_usd"] == "0.1234"


def test_p3_s3_ac6_report_usage_cost_in_context_mode(context_review):
    fake = context_success(total_cost_usd=0.1234)

    run, _ = context_review(fake, "--format", "json")

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    assert json_report(run)["usage"]["cost_usd"] == "0.1234"


@pytest.mark.parametrize(("value", "shown"), [(0.1234, "0.1234"), (None, None), (-1, None)])
def test_p3_s3_ac6_audit_called_record_has_the_cost_in_diff_mode(
    claude_review, tmp_path, value, shown
):
    audit_log = tmp_path / "diff-audit.jsonl"
    fake = FakeSdk(result_message(findings_json(HIGH_FINDING), total_cost_usd=value))

    run = claude_review(fake, "--audit-log", str(audit_log))

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    [called] = agent_records(read_records(audit_log), "called")
    assert "cost_usd" in called
    assert called["cost_usd"] == shown


@pytest.mark.parametrize(("value", "shown"), [(0.1234, "0.1234"), (None, None), (math.nan, None)])
def test_p3_s3_ac6_audit_called_record_has_the_cost_in_context_mode(context_review, value, shown):
    fake = context_success(total_cost_usd=value)

    run, records = context_review(fake)

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    [called] = agent_records(records, "called")
    assert "cost_usd" in called
    assert called["cost_usd"] == shown


def test_p3_s3_ac6_report_usage_cost_is_null_in_context_mode_when_unknown(context_review):
    fake = context_success()

    run, _ = context_review(fake, "--format", "json")

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    usage = json_report(run)["usage"]
    assert "cost_usd" in usage
    assert usage["cost_usd"] is None


BAD_COSTS = {
    "negative": -0.01,
    "negative_one": -1,
    "nan": math.nan,
    "infinity": math.inf,
    "minus_infinity": -math.inf,
    "true": True,
    "false": False,
    "text": "abc",
    "numeric_text": "0.1234",
    "list": [0.1234],
    "mapping": {"usd": 0.1234},
    "none": None,
}


@pytest.mark.parametrize("value", BAD_COSTS.values(), ids=BAD_COSTS.keys())
async def test_p3_s3_ac6_unusable_cost_is_none_and_the_review_succeeds(value):
    fake = context_success(total_cost_usd=value)

    response = await provider_for(fake).review(context_request(RecordingTools()))

    assert response.cost_usd is None
    assert response.findings == (FINDING,)


async def test_p3_s3_ac6_absent_cost_is_none():
    fake = context_success()

    response = await provider_for(fake).review(context_request(RecordingTools()))

    assert response.cost_usd is None


@pytest.mark.parametrize("value", BAD_COSTS.values(), ids=BAD_COSTS.keys())
def test_p3_s3_ac6_unusable_cost_is_null_in_the_report(claude_review, value):
    fake = FakeSdk(result_message(findings_json(HIGH_FINDING), total_cost_usd=value))

    run = claude_review(fake, "--format", "json")

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    usage = json_report(run)["usage"]
    assert "cost_usd" in usage
    assert usage["cost_usd"] is None


def test_p3_s3_ac6_mock_provider_reports_a_null_cost(bot, claude_argv):
    # A provider that reports no cost (MockProvider, as the design's default) gives null.
    argv = [claude_argv()[0], "--provider", "mock", "--format", "json"]

    run = bot.run(*argv, provider=bot.mock(findings=[bot.finding()]))

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    assert json_report(run)["usage"]["cost_usd"] is None


# AC8: the flag's range.


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "0.009",
        "-1",
        "1000.01",
        "nan",
        "abc",
        "1000.001",
        "1e4",
        "0.0099999",
        "-0.01",
        "inf",
        "-inf",
        "",
        "0.00",
        "1" + "0" * 40,
    ],
)
def test_p3_s3_ac8_bad_max_cost_exits_2_naming_the_flag(bot, claude_argv, value):
    # No fake: a bad setting has to stop the run first, and the guard fails any test that doesn't.
    run = bot.run(*claude_argv(f"{COST_FLAG}={value}"))

    assert run.exit_code == USAGE_EXIT, run.stderr
    errors = [line for line in run.stderr.splitlines() if line.startswith(ERROR)]
    assert any(COST_FLAG in line for line in errors), run.stderr
    assert "unrecognized arguments" not in run.stderr


@pytest.mark.parametrize("value", ["0", "0.009", "-1", "1000.01", "nan", "abc"])
def test_p3_s3_ac8_bad_variable_exits_2_naming_it(bot, claude_argv, monkeypatch, value):
    monkeypatch.setenv(COST_VARIABLE, value)

    run = bot.run(*claude_argv())

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert COST_VARIABLE in run.stderr


def test_p3_s3_ac8_bad_max_cost_reaches_no_provider(bot, claude_argv):
    provider = bot.mock()

    run = bot.run(claude_argv()[0], "--provider", "mock", COST_FLAG, "0.009", provider=provider)

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []


@pytest.mark.parametrize("value", ["0.01", "1000", "1000.00"])
def test_p3_s3_ac8_bounds_are_accepted(bot, value):
    _, settings = bot.parse_args("change.diff", COST_FLAG, value)

    assert settings.max_cost_usd == Decimal(value)


def test_p3_s3_ac8_unset_by_default(bot):
    _, settings = bot.parse_args("change.diff")

    assert settings.max_cost_usd is None


@pytest.mark.parametrize(("value", "budget"), [("0.01", 0.01), ("1000", 1000.0)])
def test_p3_s3_ac8_bounds_run_with_that_budget(claude_review, value, budget):
    fake = FakeSdk(result_message(findings_json()))

    run = claude_review(fake, COST_FLAG, value)

    assert run.exit_code == 0, run.stderr
    assert fake.options.max_budget_usd == budget


# AC10: the init message's tool list.


BAD_TOOL_LISTS = {
    "with_bash": ([*ALLOWED_TOOLS, "Bash"], ["Bash"]),
    "without_grep": (["mcp__repo__read", "mcp__repo__glob"], ["mcp__repo__grep"]),
    "with_other_server": ([*ALLOWED_TOOLS, "mcp__other__read"], ["mcp__other__read"]),
    "with_tool_search": ([*ALLOWED_TOOLS, "ToolSearch"], ["ToolSearch"]),
    "builtins_only": (["Read", "Grep", "Glob"], ["Read", *ALLOWED_TOOLS]),
    "empty": ([], list(ALLOWED_TOOLS)),
}


@pytest.mark.parametrize(("tools", "named"), BAD_TOOL_LISTS.values(), ids=BAD_TOOL_LISTS.keys())
async def test_p3_s3_ac10_unexpected_tool_list_fails_permanent_naming_them(tools, named):
    fake = FakeSdk(init_message(tools), result_message(findings_json(HIGH_FINDING)))

    error = await raised_by(fake, context_request(RecordingTools()))

    assert error.category is ErrorCategory.PERMANENT
    for name in named:
        assert name in str(error), str(error)


@pytest.mark.parametrize(("tools", "_named"), BAD_TOOL_LISTS.values(), ids=BAD_TOOL_LISTS.keys())
async def test_p3_s3_ac10_no_tool_handler_runs_after_a_bad_list(tools, _named):
    executor = RecordingTools()
    log = ToolCallLog()
    fake = FakeSdk(
        init_message(tools),
        tool_call("read", READ_CHECKOUT, log),
        result_message(findings_json(HIGH_FINDING)),
    )

    with pytest.raises(ProviderError):
        await provider_for(fake).review(context_request(executor))

    assert executor.calls == []
    assert log.calls == []


async def test_p3_s3_ac10_exactly_the_three_in_any_order_proceed(repo):
    tools = repo_tools(repo)
    log = ToolCallLog()
    fake = FakeSdk(
        init_message(list(reversed(ALLOWED_TOOLS))),
        tool_call("read", READ_CHECKOUT, log),
        result_message(findings_json(HIGH_FINDING)),
    )

    response = await provider_for(fake).review(context_request(tools))

    assert response.findings == (FINDING,)
    assert len(tools.ledger) == 1


@pytest.mark.parametrize(
    "tools", [None, "mcp__repo__read", {"mcp__repo__read": True}], ids=["missing", "text", "map"]
)
async def test_p3_s3_limits_init_tool_list_that_isnt_a_list_fails_permanent(tools):
    # Limits: the CLI's fields are untrusted and type-checked; no list is not the three tools.
    fake = FakeSdk(init_message(tools), result_message(findings_json(HIGH_FINDING)))

    error = await raised_by(fake, context_request(RecordingTools()))

    assert error.category is ErrorCategory.PERMANENT


def test_p3_s3_ac10_bad_list_through_main_exits_permanent(context_review):
    fake = FakeSdk(init_message([*ALLOWED_TOOLS, "Bash"]), result_message(findings_json()))

    run, _ = context_review(fake)

    assert run.exit_code == PERMANENT_EXIT, run.stderr
    assert "Bash" in run.stderr


def scalars(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [item for nested in value.values() for item in scalars(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in scalars(nested)]
    return [value]


def test_p3_s3_ac10_tool_list_is_kept_in_the_agent_tools_record(context_review):
    fake = context_success()

    run, records = context_review(fake)

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    [record] = [r for r in records if r["stage"] == "agent" and r["event"] == "tools"]
    assert sorted(scalars(record["data"])) == sorted(ALLOWED_TOOLS)


async def test_p3_s3_ac10_tool_call_after_a_wrong_list_is_refused_without_running():
    executor = RecordingTools()
    fake = FakeSdk(init_message([*ALLOWED_TOOLS, "Bash"]), result_message(findings_json()))
    with pytest.raises(ProviderError):
        await provider_for(fake).review(context_request(executor))
    log = ToolCallLog()

    # The CLI may still call the server after the provider has seen the wrong list.
    await tool_call("read", READ_CHECKOUT, log)(fake.options)

    assert executor.calls == []
    assert log.texts != [executor.answer]
    assert log.texts[0].strip()


async def test_p3_s3_ac10_second_init_listing_bash_after_a_good_one_fails():
    # The CLI re-sends init (for example after a reconnect): every list is checked, not the first.
    executor = RecordingTools()
    log = ToolCallLog()
    fake = FakeSdk(
        init_message(),
        tool_call("read", READ_CHECKOUT, log),
        init_message([*ALLOWED_TOOLS, "Bash"]),
        result_message(findings_json(HIGH_FINDING)),
    )

    error = await raised_by(fake, context_request(executor))

    assert error.category is ErrorCategory.PERMANENT
    assert "Bash" in str(error)
    assert len(executor.calls) == 1
    await tool_call("read", READ_CHECKOUT, log)(fake.options)
    assert len(executor.calls) == 1


async def test_p3_s3_limits_huge_unexpected_tool_names_give_a_bounded_message():
    # The init list is CLI text: a hostile list mustn't make an unbounded error message.
    huge = ["X" * 100_000, *(f"Tool{n}" for n in range(1_000))]
    fake = FakeSdk(init_message([*ALLOWED_TOOLS, *huge]), result_message(findings_json()))

    error = await raised_by(fake, context_request(RecordingTools()))

    assert error.category is ErrorCategory.PERMANENT
    assert "X" * 101 not in str(error)


async def test_p3_s3_ac10_context_stream_without_init_message_proceeds(repo):
    tools = repo_tools(repo)
    log = ToolCallLog()
    fake = FakeSdk(
        tool_call("read", READ_CHECKOUT, log), result_message(findings_json(HIGH_FINDING))
    )

    response = await provider_for(fake).review(context_request(tools))

    assert response.findings == (FINDING,)
    assert len(tools.ledger) == 1


def test_p3_s3_ac10_no_init_message_has_no_agent_tools_record(context_review):
    fake = FakeSdk(result_message(findings_json(HIGH_FINDING)))

    run, records = context_review(fake)

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    assert agent_records(records, "tools") == []


@pytest.mark.parametrize(
    "tools", [[*ALLOWED_TOOLS, "Bash"], ["Read", "Bash"], list(ALLOWED_TOOLS)], ids=repr
)
def test_p3_s3_ac10_diff_mode_neither_checks_nor_records_the_list(claude_review, tmp_path, tools):
    audit_log = tmp_path / "diff-audit.jsonl"
    fake = FakeSdk(init_message(tools), result_message(findings_json(HIGH_FINDING)))

    run = claude_review(fake, "--audit-log", str(audit_log))

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    assert agent_records(read_records(audit_log), "tools") == []


# Limits: a malformed call is recorded and counts towards max_tool_calls.


async def test_p3_s3_limits_malformed_calls_count_towards_max_tool_calls(repo):
    tools = RepoTools(
        repo.ref, caps=ToolCaps(max_calls=2, max_result_bytes=16_000, max_total_bytes=96_000)
    )
    log = ToolCallLog()
    fake = context_success(
        tool_call("read", {"path": 5}, log),
        tool_call("read", {"bogus": True}, log),
        tool_call("read", READ_CHECKOUT, log),
    )

    await provider_for(fake).review(context_request(tools))

    reasons = [entry.reason for entry in tools.ledger]
    assert reasons == ["invalid_arguments", "invalid_arguments", "call_limit"]
