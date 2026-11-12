"""P4-S6: review with repository context on the second provider, within a cost cap.

Written from the spec's criteria, Limits and Interfaces, Phase 4 design.md ("Second provider and
routing") and ADRs 0014, 0015 and 0020, in parallel with the developer and before their code was
read. AC9 lives in `tests/conformance` (the `openai` factory's `tool_loop`).

Most criteria drive `OpenAICompatibleProvider` directly: its settings come from the real CLI
parser, it sends through a scripted `HttpTransport` (`fake_openai`), and its executor is the real
`RepoTools` over a one-commit fixture repository (`fake_mcp.commit_repo`). `Watched` only
delegates to that `RepoTools`, recording what each call was given and returned, so "goes to the
executor with its parsed arguments" can be seen. The rest run `review-bot` (and `pr shadow|run`)
in process, as P4-S2's suite does, with `--context-repo` on the same kind of repository.

Readings, each used in one place:
- "Over 64 KB" is read as over 64 KiB of the arguments' UTF-8 text: 60,000 bytes are parsed,
  70,000 are not, and so is 34,000 two-byte characters (68,000 bytes).
- A message whose `tool_calls` is an empty list has no tool calls (AC2), so it ends the loop.
- "Retries apply per POST" (AC10): each POST has its own `--max-retries`, and a retried POST is
  still one model turn for AC4's count.
- The deadline failing is `transient`, as P4-S2 AC4's deadline is.
- AC7's "naming the missing price flag" with neither price given is read as naming at least one.
- A response whose `tool_calls` is malformed in shape (not a list, an entry without `function`,
  a name that isn't a string) is untrusted model output: it may fail with any `ProviderError`, or
  be refused through the executor, but never raise anything else.
"""

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from diff_support import FINDING_DIFF
from fake_mcp import CHECKOUT, CommittedRepo, commit_repo
from fake_openai import (
    KEY,
    FakeTransport,
    api_error,
    completion,
    completion_body,
    delayed,
    fake,
    findings_content,
    hang_forever,
    http,
    tool_call,
    tool_calls_body,
    tool_calls_completion,
    use_fake_transport,
)
from fake_sdk import HIGH_FINDING
from test_p4_s1_policy_file import policy_toml
from test_p4_s2_openai_compatible_provider import (
    EXIT_CODES,
    KEY_VARIABLE,
    USAGE_EXIT,
    OpenAIBot,
    PrCommands,
)

from review_bot import cli
from review_bot.context.ledger import RefusalReason, ToolName, ToolOutcome
from review_bot.context.tools import RepoTools, ToolCaps
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.prompt import build_context_request, build_request
from review_bot.providers.base import ReviewResponse, ToolDefinition
from review_bot.providers.openai_compat import OpenAICompatibleProvider

pytestmark = pytest.mark.integration

MODEL = "m1"
FINDING = Finding(**HIGH_FINDING)
READ_CHECKOUT = ("read", {"path": CHECKOUT})
PRICE_INPUT_FLAG = "--price-input-usd-per-mtok"
PRICE_OUTPUT_FLAG = "--price-output-usd-per-mtok"
PRICES = (PRICE_INPUT_FLAG, "2.50", PRICE_OUTPUT_FLAG, "10")
PRICE_VARIABLES = ("REVIEW_BOT_PRICE_INPUT_USD_PER_MTOK", "REVIEW_BOT_PRICE_OUTPUT_USD_PER_MTOK")
# AC5's two turns: $2.50 + $0.10, then $1.25 + $0.05.
FIRST_TURN = {"prompt_tokens": 1_000_000, "completion_tokens": 10_000}
SECOND_TURN = {"prompt_tokens": 500_000, "completion_tokens": 5_000}
AC5_COST = Decimal("3.90")
MAX_ARGUMENT_BYTES = 64 * 1024
# Generous: they catch a hang or super-linear work, not a slow CI machine.
BOUND_S = 30.0


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key, prices, cap, policy or proxy from the machine running the tests."""
    for name in (
        KEY_VARIABLE,
        "REVIEW_BOT_OPENAI_BASE_URL",
        "REVIEW_BOT_MAX_COST_USD",
        "REVIEW_BOT_POLICY",
        "CI",
        *PRICE_VARIABLES,
    ):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")


# --- The provider over the real RepoTools ---------------------------------------------------------


class Watched:
    """A `ToolExecutor` that hands every call to a real `RepoTools`, recording what passed."""

    def __init__(self, tools: RepoTools, *, delay_s: float = 0.0) -> None:
        self.tools = tools
        self.delay_s = delay_s
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.results: list[str] = []

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self.tools.definitions

    @property
    def ledger(self) -> Any:
        return self.tools.ledger

    async def call(self, name: str, arguments: Mapping[str, object]) -> str:
        self.calls.append((name, dict(arguments)))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        result = await self.tools.call(name, arguments)
        self.results.append(result)
        return result


@pytest.fixture
def repo(tmp_path: Path) -> CommittedRepo:
    return commit_repo(tmp_path / "repo")


def watched(
    repo: CommittedRepo,
    *,
    max_calls: int = 12,
    allowed: frozenset[ToolName] = frozenset(ToolName),
    delay_s: float = 0.0,
) -> Watched:
    caps = ToolCaps(max_calls=max_calls, max_result_bytes=16_000, max_total_bytes=96_000)
    return Watched(RepoTools(repo.ref, caps=caps, allowed=allowed), delay_s=delay_s)


def provider(transport: FakeTransport, *flags: str) -> OpenAICompatibleProvider:
    """The provider as `review-bot --provider openai --model m1 --max-retries 0 FLAGS` sets it."""
    _, settings = cli.parse_args(
        ["change.diff", "--provider", "openai", "--model", MODEL, "--max-retries", "0", *flags]
    )
    return OpenAICompatibleProvider(
        settings, base_url=settings.openai_base_url, api_key=KEY, transport=transport
    )


async def review(transport: FakeTransport, tools: Watched, *flags: str) -> ReviewResponse:
    request = build_context_request(FINDING_DIFF, model=MODEL, tools=tools)
    return await provider(transport, *flags).review(request)


async def failure(transport: FakeTransport, tools: Watched, *flags: str) -> ProviderError:
    with pytest.raises(ProviderError) as caught:
        await review(transport, tools, *flags)
    return caught.value


def asks(*calls: tuple[str, Any], **usage: int) -> Any:
    """A turn asking for `calls`, each `(name, arguments)`, with ids `call_1`, `call_2`, ..."""
    entries = [tool_call(f"call_{n}", name, args) for n, (name, args) in enumerate(calls, 1)]
    return tool_calls_completion(*entries, **usage)


def answer(*findings: Finding, **usage: int) -> Any:
    """The final turn: `FINDING` if given, else no findings."""
    assert findings in ((), (FINDING,)), findings
    fields = [HIGH_FINDING] if findings else []
    return completion(findings_content(*fields), **usage)


def tool_messages(messages: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [message for message in messages if message.get("role") == "tool"]


def function(definition: ToolDefinition) -> dict[str, Any]:
    """AC1's shape for one definition, with its schema as JSON would carry it."""
    return {
        "type": "function",
        "function": {
            "name": definition.name,
            "description": definition.description,
            "parameters": json.loads(json.dumps(definition.input_schema)),
        },
    }


# --- AC1: every POST offers the executor's tools as functions -------------------------------------


async def test_p4_s6_ac1_every_post_carries_one_function_per_definition_in_order(repo):
    tools = watched(repo)
    transport = fake(asks(READ_CHECKOUT), asks(("glob", {"pattern": "*.py"})), answer(FINDING))

    await review(transport, tools)

    assert len(transport.sent) == 3
    expected = [function(definition) for definition in tools.definitions]
    assert [definition.name for definition in tools.definitions] == ["read", "grep", "glob"]
    for sent in transport.sent:
        assert sent.body["tools"] == expected


async def test_p4_s6_ac1_one_turn_review_still_offers_the_tools(repo):
    tools = watched(repo)
    transport = fake(answer(FINDING))

    response = await review(transport, tools)

    assert response.findings == (FINDING,)
    assert [tool["function"]["name"] for tool in transport.one.body["tools"]] == [
        "read",
        "grep",
        "glob",
    ]


async def test_p4_s6_ac1_policy_narrowed_executor_offers_only_its_tools(repo):
    tools = watched(repo, allowed=frozenset({ToolName.GLOB, ToolName.READ}))
    transport = fake(answer(FINDING))

    await review(transport, tools)

    assert transport.one.body["tools"] == [function(d) for d in tools.definitions]
    assert [tool["function"]["name"] for tool in transport.one.body["tools"]] == ["read", "glob"]


# --- AC2: the loop --------------------------------------------------------------------------------


GREP_LINE = ("grep", {"literal": "line_12"})
GLOB_PY = ("glob", {"pattern": "*.py"})


async def test_p4_s6_ac2_each_call_goes_to_the_executor_in_order_with_its_arguments(repo):
    tools = watched(repo)
    transport = fake(asks(READ_CHECKOUT, GREP_LINE, GLOB_PY), answer(FINDING))

    response = await review(transport, tools)

    assert response.findings == (FINDING,)
    assert tools.calls == [READ_CHECKOUT, GREP_LINE, GLOB_PY]
    assert [entry.tool for entry in tools.ledger] == [ToolName.READ, ToolName.GREP, ToolName.GLOB]
    assert all(entry.outcome is ToolOutcome.OK for entry in tools.ledger), tools.ledger


async def test_p4_s6_ac2_results_follow_the_assistant_message_with_their_call_ids(repo):
    tools = watched(repo)
    transport = fake(asks(READ_CHECKOUT, GREP_LINE), answer(FINDING))

    await review(transport, tools)

    first, second = transport.sent
    conversation = second.messages
    assert conversation[: len(first.messages)] == first.messages
    assistant, *results = conversation[len(first.messages) :]
    assert assistant["role"] == "assistant"
    asked = [
        (call["id"], call["function"]["name"], json.loads(call["function"]["arguments"]))
        for call in assistant["tool_calls"]
    ]
    assert asked == [("call_1", *READ_CHECKOUT), ("call_2", *GREP_LINE)]
    assert [m["role"] for m in results] == ["tool", "tool"]
    assert [m["tool_call_id"] for m in results] == ["call_1", "call_2"]
    assert [m["content"] for m in results] == tools.results
    assert "line_12 = 12" in tools.results[0]


async def test_p4_s6_ac2_each_post_carries_the_whole_conversation_so_far(repo):
    tools = watched(repo)
    transport = fake(asks(READ_CHECKOUT), asks(GLOB_PY), asks(GREP_LINE), answer(FINDING))

    await review(transport, tools)

    assert len(transport.sent) == 4
    [system, user] = transport.sent[0].messages
    assert (system["role"], user["role"]) == ("system", "user")
    for earlier, later in zip(transport.sent, transport.sent[1:], strict=False):
        assert later.messages[: len(earlier.messages)] == earlier.messages
        added = later.messages[len(earlier.messages) :]
        assert [message["role"] for message in added] == ["assistant", "tool"]
    assert [m["tool_call_id"] for m in tool_messages(transport.sent[-1].messages)] == [
        "call_1",
        "call_1",
        "call_1",
    ]
    assert all(sent.body["model"] == MODEL for sent in transport.sent)


async def test_p4_s6_ac2_loop_ends_at_the_first_message_without_tool_calls(repo):
    tools = watched(repo)
    # A third POST would get another request for tools; there must be none.
    transport = fake(asks(READ_CHECKOUT), answer(FINDING), asks(GLOB_PY))

    response = await review(transport, tools)

    assert response.findings == (FINDING,)
    assert len(transport.sent) == 2
    assert len(tools.calls) == 1


async def test_p4_s6_ac2_content_beside_tool_calls_is_not_read_as_findings(repo):
    tools = watched(repo)
    thinking = tool_calls_completion(
        tool_call("call_1", *READ_CHECKOUT), content="Let me read the file first."
    )
    transport = fake(thinking, answer(FINDING))

    response = await review(transport, tools)

    assert response.findings == (FINDING,)
    assert len(transport.sent) == 2


async def test_p4_s6_ac2_empty_tool_calls_list_ends_the_loop(repo):
    tools = watched(repo)
    empty = tool_calls_completion(content=findings_content(HIGH_FINDING))
    transport = fake(empty, asks(READ_CHECKOUT))

    response = await review(transport, tools)

    assert response.findings == (FINDING,)
    assert len(transport.sent) == 1
    assert tools.calls == []


async def test_p4_s6_ac2_final_content_that_isnt_findings_fails_invalid_output(repo):
    tools = watched(repo)
    transport = fake(asks(READ_CHECKOUT), completion("I looked, and it seems fine."))

    error = await failure(transport, tools)

    assert error.category is ErrorCategory.INVALID_OUTPUT


@pytest.mark.parametrize(
    ("reply", "category"),
    [
        (api_error(401), ErrorCategory.AUTH),
        (api_error(400), ErrorCategory.PERMANENT),
        (completion(None, finish_reason="content_filter"), ErrorCategory.REFUSAL),
        (completion(findings_content(), finish_reason="length"), ErrorCategory.INVALID_OUTPUT),
    ],
    ids=["auth", "permanent", "refusal", "length"],
)
async def test_p4_s6_ac2_failure_mid_loop_keeps_its_category(repo, reply, category):
    tools = watched(repo)
    transport = fake(asks(READ_CHECKOUT), reply)

    error = await failure(transport, tools)

    assert error.category is category
    assert len(transport.sent) == 2


# --- AC3 and Limits: untrusted arguments go to the executor, which refuses ------------------------


BAD_ARGUMENTS = {
    "not_json": "{path: billing/checkout.py}",
    "truncated": '{"path": "billing/checkout.py"',
    "empty_string": "",
    "array": '["billing/checkout.py"]',
    "string": '"billing/checkout.py"',
    "null": "null",
    "number": "42",
    "true": "true",
    "over_64_kb": json.dumps({"path": CHECKOUT, "pad": "x" * 70_000}),
    "over_64_kb_of_utf8": json.dumps({"path": CHECKOUT, "pad": "é" * 34_000}, ensure_ascii=False),
    # Valid JSON under 64 KB that Python's parser can't take: it must be refused, not crash.
    "nested_30000_deep": "[" * 30_000 + "]" * 30_000,
    "objects_10000_deep": '{"a":' * 10_000 + "1" + "}" * 10_000,
}


@pytest.mark.parametrize("arguments", BAD_ARGUMENTS.values(), ids=BAD_ARGUMENTS.keys())
async def test_p4_s6_ac3_bad_arguments_reach_the_executor_as_an_empty_mapping(repo, arguments):
    tools = watched(repo)
    transport = fake(tool_calls_completion(tool_call("call_1", "read", arguments)), answer(FINDING))

    started = time.monotonic()
    response = await review(transport, tools)

    assert time.monotonic() - started < BOUND_S
    assert tools.calls == [("read", {})]
    [entry] = tools.ledger
    assert entry.outcome is ToolOutcome.REFUSED
    assert entry.reason is RefusalReason.INVALID_ARGUMENTS
    assert response.findings == (FINDING,)
    [result] = tool_messages(transport.sent[1].messages)
    assert result["tool_call_id"] == "call_1"
    assert "refused" in result["content"]


async def test_p4_s6_ac3_arguments_under_64_kb_are_parsed(repo):
    tools = watched(repo)
    arguments = {"path": CHECKOUT, "pad": "x" * 60_000}
    text = json.dumps(arguments)
    assert len(text.encode("utf-8")) < MAX_ARGUMENT_BYTES
    transport = fake(tool_calls_completion(tool_call("call_1", "read", text)), answer(FINDING))

    await review(transport, tools)

    assert tools.calls == [("read", arguments)]


async def test_p4_s6_ac3_refused_call_is_counted_towards_the_tool_cap(repo):
    tools = watched(repo, max_calls=1)
    bad = tool_calls_completion(tool_call("call_1", "read", "not json"))
    transport = fake(bad, asks(READ_CHECKOUT), answer(FINDING))

    response = await review(transport, tools, "--max-tool-calls", "1")

    assert response.findings == (FINDING,)
    assert [entry.reason for entry in tools.ledger] == [
        RefusalReason.INVALID_ARGUMENTS,
        RefusalReason.CALL_LIMIT,
    ]


async def test_p4_s6_ac3_valid_json_beyond_pythons_number_limits_never_crashes(repo):
    tools = watched(repo)
    huge = '{"path": "billing/checkout.py", "start_line": ' + "9" * 5_000 + "}"
    transport = fake(tool_calls_completion(tool_call("call_1", "read", huge)), answer(FINDING))

    response = await review(transport, tools)

    assert response.findings == (FINDING,)
    [entry] = tools.ledger
    assert entry.outcome is ToolOutcome.REFUSED


@pytest.mark.parametrize(
    "arguments",
    ['{"path": NaN}', '{"path": "\\ud800"}', '{"path": "a", "path": "billing/checkout.py"}'],
    ids=["nan", "lone_surrogate", "duplicate_key"],
)
async def test_p4_s6_limits_odd_json_arguments_leave_the_review_going(repo, arguments):
    tools = watched(repo)
    transport = fake(tool_calls_completion(tool_call("call_1", "read", arguments)), answer(FINDING))

    response = await review(transport, tools)

    assert response.findings == (FINDING,)
    assert len(tools.calls) == 1
    assert len(tools.ledger) == 1
    json.dumps(transport.sent[1].body)  # the next request is still sendable JSON


async def test_p4_s6_ac3_unknown_tool_goes_to_the_executor_and_is_refused(repo):
    tools = watched(repo)
    shell = ("bash", {"command": "cat /etc/passwd"})
    transport = fake(asks(shell, READ_CHECKOUT), answer(FINDING))

    response = await review(transport, tools)

    assert response.findings == (FINDING,)
    assert tools.calls == [shell, READ_CHECKOUT]
    unknown, read = tools.ledger
    assert (unknown.tool, unknown.reason) == (None, RefusalReason.UNKNOWN_TOOL)
    assert read.outcome is ToolOutcome.OK
    first, _ = tool_messages(transport.sent[1].messages)
    assert "refused" in first["content"]


async def test_p4_s6_ac3_policy_denied_tool_is_refused_and_the_review_goes_on(repo):
    tools = watched(repo, allowed=frozenset({ToolName.READ}))
    transport = fake(asks(GREP_LINE), asks(READ_CHECKOUT), answer(FINDING))

    response = await review(transport, tools)

    assert response.findings == (FINDING,)
    assert [definition.name for definition in tools.definitions] == ["read"]
    denied, read = tools.ledger
    assert (denied.tool, denied.reason) == (ToolName.GREP, RefusalReason.POLICY_DENIED)
    assert read.outcome is ToolOutcome.OK
    assert "line_12" not in tool_messages(transport.sent[1].messages)[0]["content"]


async def test_p4_s6_limits_calls_past_max_tool_calls_in_one_message_are_refused(repo):
    tools = watched(repo, max_calls=3)
    transport = fake(asks(*[READ_CHECKOUT] * 40), answer(FINDING))

    response = await review(transport, tools, "--max-tool-calls", "3")

    assert response.findings == (FINDING,)
    results = tool_messages(transport.sent[1].messages)
    assert [m["tool_call_id"] for m in results] == [f"call_{n}" for n in range(1, 41)]
    answered = [m for m in results if "refused" not in m["content"]]
    assert len(answered) <= 3
    assert all("refused" in m["content"] for m in results[3:])
    assert sum(entry.outcome is ToolOutcome.OK for entry in tools.ledger) <= 3


async def test_p4_s6_limits_thousands_of_calls_in_one_message_finish_quickly(repo):
    tools = watched(repo, max_calls=2)
    transport = fake(asks(*[READ_CHECKOUT] * 3_000), answer(FINDING))

    started = time.monotonic()
    response = await review(transport, tools, "--max-tool-calls", "2")

    assert time.monotonic() - started < BOUND_S
    assert response.findings == (FINDING,)
    assert len(tool_messages(transport.sent[1].messages)) == 3_000
    assert sum(entry.outcome is ToolOutcome.OK for entry in tools.ledger) <= 2


async def test_p4_s6_limits_many_oversized_arguments_in_one_response_finish_quickly(repo):
    tools = watched(repo)
    oversized = json.dumps({"path": CHECKOUT, "pad": "y" * 70_000})
    calls = [tool_call(f"call_{n}", "read", oversized) for n in range(1, 51)]
    transport = fake(tool_calls_completion(*calls), answer(FINDING))

    started = time.monotonic()
    response = await review(transport, tools)

    assert time.monotonic() - started < BOUND_S
    assert response.findings == (FINDING,)
    assert all(arguments == {} for _, arguments in tools.calls)


MALFORMED_TOOL_CALLS: dict[str, Any] = {
    "not_a_list": "read",
    "entry_not_an_object": [42],
    "entry_without_function": [{"id": "call_1", "type": "function"}],
    "name_not_a_string": [tool_call("call_1", 42, "{}")],
    "arguments_missing": [{"id": "call_1", "type": "function", "function": {"name": "read"}}],
    "arguments_an_object": [
        {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": {}}}
    ],
    "id_missing": [{"type": "function", "function": {"name": "read", "arguments": "{}"}}],
}


@pytest.mark.parametrize(
    "tool_calls", MALFORMED_TOOL_CALLS.values(), ids=MALFORMED_TOOL_CALLS.keys()
)
async def test_p4_s6_limits_malformed_tool_calls_fail_only_as_a_provider_error(repo, tool_calls):
    tools = watched(repo)
    body = tool_calls_body()
    body["choices"][0]["message"]["tool_calls"] = tool_calls
    transport = fake(http(200, body), answer(FINDING))

    try:
        response = await review(transport, tools)
    except ProviderError:
        return
    assert response.findings == (FINDING,)


# --- AC4: the turn cap ----------------------------------------------------------------------------


@pytest.mark.parametrize("max_tool_calls", [1, 3])
async def test_p4_s6_ac4_model_that_keeps_calling_fails_after_max_tool_calls_plus_2_turns(
    repo, max_tool_calls
):
    tools = watched(repo, max_calls=max_tool_calls)
    transport = fake(asks(READ_CHECKOUT))  # repeats: every turn asks again

    error = await failure(transport, tools, "--max-tool-calls", str(max_tool_calls))

    assert error.category is ErrorCategory.PERMANENT
    assert error.cap == "turns"
    assert "--max-tool-calls" in str(error)
    assert len(transport.sent) == max_tool_calls + 2


async def test_p4_s6_ac4_answer_on_the_last_allowed_turn_is_returned(repo):
    tools = watched(repo, max_calls=3)
    transport = fake(*[asks(READ_CHECKOUT)] * 4, answer(FINDING))

    response = await review(transport, tools, "--max-tool-calls", "3")

    assert response.findings == (FINDING,)
    assert len(transport.sent) == 5


# --- AC5: cost from the configured prices ---------------------------------------------------------


async def test_p4_s6_ac5_two_turns_cost_exactly_3_90(repo):
    tools = watched(repo)
    transport = fake(asks(READ_CHECKOUT, **FIRST_TURN), answer(FINDING, **SECOND_TURN))

    response = await review(transport, tools, *PRICES)

    assert isinstance(response.cost_usd, Decimal)
    assert response.cost_usd == AC5_COST


async def test_p4_s6_ac5_one_diff_turn_is_priced_exactly():
    transport = fake(answer(FINDING, prompt_tokens=123, completion_tokens=45))

    response = await provider(transport, *PRICES).review(build_request(FINDING_DIFF, model=MODEL))

    # 123 tokens at $2.50 per million, plus 45 at $10 per million
    assert response.cost_usd == Decimal("0.0007575")


async def test_p4_s6_ac5_without_prices_cost_is_unknown(repo):
    tools = watched(repo)
    transport = fake(asks(READ_CHECKOUT, **FIRST_TURN), answer(FINDING, **SECOND_TURN))

    response = await review(transport, tools)

    assert response.cost_usd is None


# --- AC6: the cost cap, checked after each turn ---------------------------------------------------


async def test_p4_s6_ac6_cap_passed_after_the_second_tool_turn_fails_with_no_third_post(repo):
    tools = watched(repo)
    transport = fake(
        asks(READ_CHECKOUT, **FIRST_TURN), asks(GLOB_PY, **SECOND_TURN), answer(FINDING)
    )

    error = await failure(transport, tools, *PRICES, "--max-cost-usd", "3.00")

    assert error.category is ErrorCategory.PERMANENT
    assert error.cap == "cost"
    assert "--max-cost-usd" in str(error)
    assert len(transport.sent) == 2


async def test_p4_s6_ac6_cap_passed_by_the_final_answer_returns_its_findings(repo):
    tools = watched(repo)
    transport = fake(asks(READ_CHECKOUT, **FIRST_TURN), answer(FINDING, **SECOND_TURN))

    response = await review(transport, tools, *PRICES, "--max-cost-usd", "3.00")

    assert response.findings == (FINDING,)
    assert response.cost_usd == AC5_COST


async def test_p4_s6_ac6_cap_passed_on_the_first_turn_stops_there(repo):
    tools = watched(repo)
    transport = fake(asks(READ_CHECKOUT, **FIRST_TURN), answer(FINDING))

    error = await failure(transport, tools, *PRICES, "--max-cost-usd", "2.00")

    assert (error.category, error.cap) == (ErrorCategory.PERMANENT, "cost")
    assert len(transport.sent) == 1


async def test_p4_s6_ac6_review_under_the_cap_runs_to_its_answer(repo):
    tools = watched(repo)
    transport = fake(
        asks(READ_CHECKOUT, **FIRST_TURN),
        asks(GLOB_PY, **SECOND_TURN),
        answer(FINDING, prompt_tokens=0, completion_tokens=0),
    )

    response = await review(transport, tools, *PRICES, "--max-cost-usd", "4.00")

    assert response.findings == (FINDING,)
    assert response.cost_usd == AC5_COST
    assert len(transport.sent) == 3


async def test_p4_s6_ac6_cost_exactly_at_the_cap_isnt_over_it(repo):
    """ "Goes over" is read as strictly greater: a running total equal to the cap goes on."""
    tools = watched(repo)
    transport = fake(
        asks(READ_CHECKOUT, **FIRST_TURN),
        asks(GLOB_PY, **SECOND_TURN),
        answer(FINDING, prompt_tokens=0, completion_tokens=0),
    )

    response = await review(transport, tools, *PRICES, "--max-cost-usd", "3.90")

    assert response.findings == (FINDING,)
    assert response.cost_usd == AC5_COST
    assert len(transport.sent) == 3


async def test_p4_s6_ac6_first_turn_exactly_at_the_cap_goes_on(repo):
    tools = watched(repo)
    transport = fake(
        asks(READ_CHECKOUT, **FIRST_TURN), answer(FINDING, prompt_tokens=0, completion_tokens=0)
    )

    response = await review(transport, tools, *PRICES, "--max-cost-usd", "2.60")

    assert response.findings == (FINDING,)
    assert len(transport.sent) == 2


# --- AC6a: a turn with no usable usage ------------------------------------------------------------


_MISSING = object()
BAD_USAGE: dict[str, Any] = {
    "missing": _MISSING,
    "null": None,
    "string": "lots",
    "list": [1_000, 10],
    "negative_prompt": {"prompt_tokens": -1, "completion_tokens": 10},
    "negative_completion": {"prompt_tokens": 10, "completion_tokens": -5},
    "fractional": {"prompt_tokens": 1.5, "completion_tokens": 10},
    "string_count": {"prompt_tokens": "100", "completion_tokens": 10},
    "boolean_count": {"prompt_tokens": True, "completion_tokens": 10},
    "null_count": {"prompt_tokens": None, "completion_tokens": 10},
    "count_missing": {"prompt_tokens": 100},
    "empty_object": {},
}


def with_usage(body: dict[str, Any], usage: Any) -> Any:
    if usage is _MISSING:
        del body["usage"]
    else:
        body["usage"] = usage
    return http(200, body)


def tool_turn_with(usage: Any) -> Any:
    return with_usage(tool_calls_body(tool_call("call_1", *READ_CHECKOUT)), usage)


def answer_with(usage: Any) -> Any:
    return with_usage(completion_body(findings_content(HIGH_FINDING)), usage)


@pytest.mark.parametrize("usage", BAD_USAGE.values(), ids=BAD_USAGE.keys())
async def test_p4_s6_ac6a_tool_turn_without_usable_usage_fails_the_cap(repo, usage):
    tools = watched(repo)
    transport = fake(tool_turn_with(usage), answer(FINDING))

    error = await failure(transport, tools, *PRICES, "--max-cost-usd", "3.00")

    assert (error.category, error.cap) == (ErrorCategory.PERMANENT, "cost")
    assert len(transport.sent) == 1


async def test_p4_s6_ac6a_later_turn_without_usage_stops_there(repo):
    tools = watched(repo)
    transport = fake(
        asks(READ_CHECKOUT, prompt_tokens=10, completion_tokens=10),
        tool_turn_with(_MISSING),
        answer(FINDING),
    )

    error = await failure(transport, tools, *PRICES, "--max-cost-usd", "3.00")

    assert (error.category, error.cap) == (ErrorCategory.PERMANENT, "cost")
    assert len(transport.sent) == 2


@pytest.mark.parametrize("usage", BAD_USAGE.values(), ids=BAD_USAGE.keys())
async def test_p4_s6_ac6a_final_answer_without_usage_is_returned_with_the_cap_spent(repo, usage):
    """As in AC6, the final answer's findings are returned; its turn costs the whole cap.

    It is the review's only turn, so "the cap as spent" is $3.00 however it is read.
    """
    tools = watched(repo)
    transport = fake(answer_with(usage), asks(READ_CHECKOUT))

    response = await review(transport, tools, *PRICES, "--max-cost-usd", "3.00")

    assert response.findings == (FINDING,)
    assert response.cost_usd == Decimal("3.00")
    assert len(transport.sent) == 1


@pytest.mark.parametrize("usage", BAD_USAGE.values(), ids=BAD_USAGE.keys())
async def test_p4_s6_ac6a_without_a_cap_the_turn_counts_as_zero_tokens(repo, usage):
    tools = watched(repo)
    transport = fake(
        tool_turn_with(usage), answer(FINDING, prompt_tokens=123, completion_tokens=45)
    )

    response = await review(transport, tools, *PRICES)

    assert response.findings == (FINDING,)
    assert response.cost_usd == Decimal("0.0007575")  # the second turn's alone
    assert len(transport.sent) == 2


async def test_p4_s6_ac6a_without_cap_or_prices_the_review_goes_on(repo):
    tools = watched(repo)
    transport = fake(tool_turn_with(_MISSING), answer_with(_MISSING))

    response = await review(transport, tools)

    assert response.findings == (FINDING,)
    assert response.cost_usd is None


def test_p4_s6_ac6a_command_fails_permanent_on_a_turn_without_usage(openai, repo):
    transport = fake(tool_turn_with(_MISSING), answer(FINDING))

    done = openai.review(
        transport, "--context-repo", str(repo.path), "--max-cost-usd", "3.00", *PRICES
    )

    done.fails_as(ErrorCategory.PERMANENT)
    assert len(transport.sent) == 1


# --- The commands: context mode, AC7's refusals and the report ------------------------------------


@pytest.fixture
def openai(bot, write_diff, tmp_path, monkeypatch) -> OpenAIBot:
    return OpenAIBot(bot, write_diff, tmp_path, monkeypatch)


@pytest.fixture
def pr(capsys, tmp_path, write_diff, monkeypatch) -> PrCommands:
    monkeypatch.delenv("REVIEW_BOT_AUDIT_LOG", raising=False)
    return PrCommands(capsys, tmp_path, write_diff)


def names_a_price_flag(stderr: str) -> bool:
    return PRICE_INPUT_FLAG in stderr or PRICE_OUTPUT_FLAG in stderr


def test_p4_s6_ac5_report_shows_the_context_reviews_cost(openai, repo):
    transport = fake(asks(READ_CHECKOUT, **FIRST_TURN), answer(FINDING, **SECOND_TURN))

    done = openai.review(transport, "--context-repo", str(repo.path), "--format", "json", *PRICES)

    assert done.exit_code in (0, 1), done.stderr
    assert len(transport.sent) == 2
    assert Decimal(done.report["usage"]["cost_usd"]) == AC5_COST


def test_p4_s6_ac6_command_fails_permanent_naming_max_cost_usd(openai, repo):
    transport = fake(
        asks(READ_CHECKOUT, **FIRST_TURN), asks(GLOB_PY, **SECOND_TURN), answer(FINDING)
    )

    done = openai.review(
        transport, "--context-repo", str(repo.path), "--max-cost-usd", "3.00", *PRICES
    )

    done.fails_as(ErrorCategory.PERMANENT)
    assert "--max-cost-usd" in done.stderr
    assert len(transport.sent) == 2


def test_p4_s6_ac6_policy_ceiling_is_enforced_as_the_cap(openai, repo, tmp_path):
    policy = tmp_path / "policy.toml"
    policy.write_text(policy_toml(max_cost_usd="3.00"), encoding="utf-8")
    transport = fake(
        asks(READ_CHECKOUT, **FIRST_TURN), asks(GLOB_PY, **SECOND_TURN), answer(FINDING)
    )

    done = openai.review(
        transport, "--context-repo", str(repo.path), "--policy", str(policy), *PRICES
    )

    done.fails_as(ErrorCategory.PERMANENT)
    assert len(transport.sent) == 2


def test_p4_s6_ac4_command_fails_permanent_naming_max_tool_calls(openai, repo):
    transport = fake(asks(READ_CHECKOUT))

    done = openai.review(transport, "--context-repo", str(repo.path), "--max-tool-calls", "2")

    done.fails_as(ErrorCategory.PERMANENT)
    assert "--max-tool-calls" in done.stderr
    assert len(transport.sent) == 4


def test_p4_s6_ac2_command_reviews_with_context_over_openai(openai, repo):
    transport = fake(asks(READ_CHECKOUT), answer(FINDING))

    done = openai.review(transport, "--context-repo", str(repo.path))

    assert done.exit_code in (0, 1), done.stderr
    assert len(transport.sent) == 2
    assert [t["function"]["name"] for t in transport.sent[0].body["tools"]] == [
        "read",
        "grep",
        "glob",
    ]
    tool_records = [r for r in done.records if (r["stage"], r["event"]) == ("context", "tool_call")]
    assert len(tool_records) == 1


@pytest.mark.parametrize(
    ("prices", "missing"),
    [
        ((), None),
        ((PRICE_INPUT_FLAG, "2.50"), PRICE_OUTPUT_FLAG),
        ((PRICE_OUTPUT_FLAG, "10"), PRICE_INPUT_FLAG),
    ],
    ids=["neither", "input_only", "output_only"],
)
def test_p4_s6_ac7_cap_without_both_prices_exits_2_naming_the_missing_one(openai, prices, missing):
    transport = fake(answer(FINDING))

    done = openai.review(transport, "--max-cost-usd", "1", *prices)

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert transport.sent == []
    if missing is None:
        assert names_a_price_flag(done.stderr), done.stderr
    else:
        assert missing in done.stderr, done.stderr


def test_p4_s6_ac7_cap_from_the_variable_without_prices_exits_2(openai, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_MAX_COST_USD", "1")
    transport = fake(answer(FINDING))

    done = openai.review(transport)

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert names_a_price_flag(done.stderr), done.stderr
    assert transport.sent == []


def test_p4_s6_ac7_policy_ceiling_without_prices_exits_2(openai, tmp_path):
    policy = tmp_path / "policy.toml"
    policy.write_text(policy_toml(max_cost_usd="0.60"), encoding="utf-8")
    transport = fake(answer(FINDING))

    done = openai.review(transport, "--policy", str(policy))

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert names_a_price_flag(done.stderr), done.stderr
    assert transport.sent == []


def test_p4_s6_ac7_context_review_with_a_cap_and_no_prices_exits_2(openai, repo):
    transport = fake(asks(READ_CHECKOUT), answer(FINDING))

    done = openai.review(transport, "--context-repo", str(repo.path), "--max-cost-usd", "1")

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert transport.sent == []


@pytest.mark.parametrize("subcommand", ["shadow", "run"])
def test_p4_s6_ac7_pr_cap_without_prices_exits_2(pr, monkeypatch, subcommand):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    transport = fake(answer(FINDING))
    use_fake_transport(monkeypatch, transport)

    done = pr.run(subcommand, "--provider", "openai", "--model", MODEL, "--max-cost-usd", "1")

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert names_a_price_flag(done.stderr), done.stderr
    assert transport.sent == []


@pytest.mark.parametrize("subcommand", ["shadow", "run"])
def test_p4_s6_ac7_pr_cap_with_both_prices_runs(pr, monkeypatch, subcommand):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    transport = fake(answer())
    use_fake_transport(monkeypatch, transport)

    done = pr.run(
        subcommand, "--provider", "openai", "--model", MODEL, "--max-cost-usd", "1", *PRICES
    )

    assert done.exit_code == 0, done.stderr
    assert len(transport.sent) == 1


def test_p4_s6_ac7_cap_with_both_prices_runs_and_reports_the_cost(openai):
    transport = fake(answer(FINDING, prompt_tokens=123, completion_tokens=45))

    done = openai.review(transport, "--max-cost-usd", "1", "--format", "json", *PRICES)

    assert done.exit_code in (0, 1), done.stderr
    assert Decimal(done.report["usage"]["cost_usd"]) == Decimal("0.0007575")


def test_p4_s6_ac7_prices_from_the_variables_satisfy_the_cap(openai, monkeypatch):
    monkeypatch.setenv(PRICE_VARIABLES[0], "2.50")
    monkeypatch.setenv(PRICE_VARIABLES[1], "10")
    transport = fake(answer(FINDING, prompt_tokens=123, completion_tokens=45))

    done = openai.review(transport, "--max-cost-usd", "1", "--format", "json")

    assert done.exit_code in (0, 1), done.stderr
    assert Decimal(done.report["usage"]["cost_usd"]) == Decimal("0.0007575")


def test_p4_s6_ac7_without_cap_or_prices_cost_usd_is_null(openai):
    transport = fake(answer(FINDING))

    done = openai.review(transport, "--format", "json")

    assert done.exit_code in (0, 1), done.stderr
    assert done.report["usage"]["cost_usd"] is None


@pytest.mark.parametrize("value", ["-1", "1e3", "nan", "two"])
@pytest.mark.parametrize("flag", [PRICE_INPUT_FLAG, PRICE_OUTPUT_FLAG])
def test_p4_s6_interfaces_price_that_isnt_a_decimal_exits_2(openai, flag, value):
    transport = fake(answer(FINDING))

    done = openai.review(transport, *PRICES, flag, value)

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert transport.sent == []


def test_p4_s6_interfaces_mock_with_a_cap_needs_no_prices(openai):
    done = openai.review(None, "--max-cost-usd", "1", provider_flags=("--provider", "mock"))

    assert done.exit_code in (0, 1), done.stderr


# --- AC8: diff mode has no tools ------------------------------------------------------------------


async def test_p4_s6_ac8_diff_mode_sends_no_tools_and_refuses_tool_calls():
    transport = fake(asks(READ_CHECKOUT), answer(FINDING))

    with pytest.raises(ProviderError) as caught:
        await provider(transport).review(build_request(FINDING_DIFF, model=MODEL))

    assert caught.value.category is ErrorCategory.INVALID_OUTPUT
    assert "tools" not in transport.one.body


def test_p4_s6_ac8_command_in_diff_mode_fails_invalid_output_on_tool_calls(openai):
    transport = fake(asks(READ_CHECKOUT), answer(FINDING))

    done = openai.review(transport)

    done.fails_as(ErrorCategory.INVALID_OUTPUT)
    assert "tools" not in transport.one.body
    assert done.exit_code == EXIT_CODES[ErrorCategory.INVALID_OUTPUT]


# --- AC10: the deadline and retries over the whole loop -------------------------------------------


async def test_p4_s6_ac10_deadline_bounds_a_loop_of_slow_turns(repo):
    tools = watched(repo, max_calls=30)
    transport = fake(delayed(0.5, asks(READ_CHECKOUT)))  # unbounded: 32 turns, 16 s

    started = time.monotonic()
    error = await failure(transport, tools, "--max-tool-calls", "30", "--deadline-s", "1.5")

    assert error.category is ErrorCategory.TRANSIENT
    assert time.monotonic() - started < 8
    assert len(transport.sent) < 8


async def test_p4_s6_ac10_deadline_counts_the_tools_time_too(repo):
    tools = watched(repo, max_calls=30, delay_s=0.5)
    transport = fake(asks(READ_CHECKOUT))

    started = time.monotonic()
    error = await failure(transport, tools, "--max-tool-calls", "30", "--deadline-s", "1.5")

    assert error.category is ErrorCategory.TRANSIENT
    assert time.monotonic() - started < 8


async def test_p4_s6_ac10_deadline_bounds_a_later_turn_that_never_answers(repo):
    tools = watched(repo)
    transport = fake(asks(READ_CHECKOUT), hang_forever())

    started = time.monotonic()
    error = await failure(transport, tools, "--deadline-s", "1")

    assert error.category is ErrorCategory.TRANSIENT
    assert time.monotonic() - started < 10


async def test_p4_s6_ac10_each_posts_timeout_is_the_time_left(repo):
    tools = watched(repo)
    transport = fake(delayed(0.6, asks(READ_CHECKOUT)), answer(FINDING))

    await review(transport, tools, "--deadline-s", "5")

    first, second = transport.sent
    assert 0 < first.timeout_s <= 5
    assert 0 < second.timeout_s <= 5 - 0.5


async def test_p4_s6_ac10_each_post_gets_its_own_retries(repo):
    tools = watched(repo)
    busy = http(503, retry_after="0")
    transport = fake(busy, asks(READ_CHECKOUT), busy, answer(FINDING))

    response = await review(transport, tools, "--max-retries", "1")

    assert response.findings == (FINDING,)
    assert len(transport.sent) == 4
    assert transport.sent[0].body == transport.sent[1].body
    assert transport.sent[2].body == transport.sent[3].body
    assert len(tool_messages(transport.sent[3].messages)) == 1
    assert len(tools.calls) == 1


async def test_p4_s6_ac10_a_retried_post_is_one_turn(repo):
    tools = watched(repo, max_calls=1)
    busy = http(503, retry_after="0")
    # Three turns (--max-tool-calls 1 plus 2), the last one retried once.
    transport = fake(asks(READ_CHECKOUT), asks(READ_CHECKOUT), busy, answer(FINDING))

    response = await review(transport, tools, "--max-tool-calls", "1", "--max-retries", "1")

    assert response.findings == (FINDING,)
    assert len(transport.sent) == 4


async def test_p4_s6_ac10_no_retry_waits_past_the_deadline_mid_loop(repo):
    tools = watched(repo)
    transport = fake(asks(READ_CHECKOUT), http(503, retry_after="20"), answer(FINDING))

    started = time.monotonic()
    error = await failure(transport, tools, "--max-retries", "3", "--deadline-s", "2")

    assert error.category is ErrorCategory.TRANSIENT
    assert time.monotonic() - started < 10
    assert len(transport.sent) == 2
