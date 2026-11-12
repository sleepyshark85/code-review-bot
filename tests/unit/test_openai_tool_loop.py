"""The OpenAI-compatible provider's tool loop, caps and cost (P4-S6), over a recording executor."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from diff_samples import FINDING_FIELDS
from git_samples import commit, init_repo
from openai_samples import (
    API_KEY,
    FakeTransport,
    RecordingTools,
    completion,
    error_response,
    tool_call,
    tool_turn,
    transport,
)

from review_bot.context.ledger import RefusalReason, ToolOutcome
from review_bot.context.tool_args import TOOL_DEFINITIONS
from review_bot.context.tools import RepoRef, RepoTools, ToolCaps
from review_bot.errors import ConfigError, ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.git import GitRepo
from review_bot.price_flags import Prices
from review_bot.providers.base import ReviewRequest, TokenUsage, ToolExecutor
from review_bot.providers.openai_compat import HttpResponse, OpenAICompatibleProvider, token_prices
from review_bot.settings import Settings

FINDINGS_JSON = json.dumps({"findings": [FINDING_FIELDS]})
PRICES = {"price_input_usd_per_mtok": Decimal("2.50"), "price_output_usd_per_mtok": Decimal(10)}
FIRST_TURN = {"prompt_tokens": 1_000_000, "completion_tokens": 10_000}
SECOND_TURN = {"prompt_tokens": 500_000, "completion_tokens": 5_000}


async def no_wait(_seconds: float) -> None:
    return None


def provider(fake: FakeTransport, **settings: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        Settings(provider="openai", model="m1", **settings),
        base_url="https://api.openai.com/v1",
        api_key=API_KEY,
        transport=fake,
        sleep=no_wait,
    )


def context_request(tools: ToolExecutor) -> ReviewRequest:
    return ReviewRequest(system_prompt="System.", user_prompt="+diff", model="m1", tools=tools)


async def failure(fake: FakeTransport, tools: RecordingTools, **settings: Any) -> ProviderError:
    with pytest.raises(ProviderError) as caught:
        await provider(fake, **settings).review(context_request(tools))
    return caught.value


async def test_each_post_offers_the_executors_tools_as_functions_in_its_order() -> None:
    fake = transport(tool_turn(tool_call("c1", "read", {"path": "a.py"})), completion())

    await provider(fake).review(context_request(RecordingTools()))

    expected = [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in TOOL_DEFINITIONS
    ]
    assert [sent.body["tools"] for sent in fake.sent] == [expected, expected]


async def test_each_call_runs_in_order_and_its_result_answers_it_in_the_next_post() -> None:
    calls = (tool_call("c1", "read", {"path": "a.py"}), tool_call("c2", "glob", {"pattern": "*"}))
    fake = transport(tool_turn(*calls, content="Looking."), completion(FINDINGS_JSON))
    tools = RecordingTools()

    response = await provider(fake).review(context_request(tools))

    assert tools.calls == [("read", {"path": "a.py"}), ("glob", {"pattern": "*"})]
    assert fake.sent[1].body["messages"][2:] == [
        {"role": "assistant", "content": "Looking.", "tool_calls": list(calls)},
        {"role": "tool", "tool_call_id": "c1", "content": "result 1"},
        {"role": "tool", "tool_call_id": "c2", "content": "result 2"},
    ]
    assert response.findings == (Finding(**FINDING_FIELDS),)


async def test_the_whole_conversation_is_sent_on_every_turn() -> None:
    fake = transport(
        tool_turn(tool_call("c1", "read", {"path": "a.py"})),
        tool_turn(tool_call("c2", "read", {"path": "b.py"})),
        completion(),
    )

    await provider(fake).review(context_request(RecordingTools()))

    roles = [[message["role"] for message in sent.body["messages"]] for sent in fake.sent]
    assert roles == [
        ["system", "user"],
        ["system", "user", "assistant", "tool"],
        ["system", "user", "assistant", "tool", "assistant", "tool"],
    ]


async def test_a_call_with_bad_arguments_still_reaches_the_executor_with_none() -> None:
    fake = transport(tool_turn(tool_call("c1", "read", "{not json")), completion())
    tools = RecordingTools()

    response = await provider(fake).review(context_request(tools))

    assert tools.calls == [("read", {})]
    echoed = fake.sent[1].body["messages"][2]["tool_calls"][0]["function"]["arguments"]
    assert echoed == "{}"
    assert response.findings == ()


async def test_the_usage_is_the_sum_of_every_turns() -> None:
    fake = transport(
        tool_turn(tool_call("c1", "read", {"path": "a.py"}), usage=FIRST_TURN),
        completion(usage=SECOND_TURN),
    )

    response = await provider(fake).review(context_request(RecordingTools()))

    assert response.usage == TokenUsage(1_500_000, 15_000)


async def test_the_cost_is_every_turns_tokens_at_the_prices() -> None:
    fake = transport(
        tool_turn(tool_call("c1", "read", {"path": "a.py"}), usage=FIRST_TURN),
        completion(usage=SECOND_TURN),
    )

    response = await provider(fake, **PRICES).review(context_request(RecordingTools()))

    assert response.cost_usd == Decimal("3.90")


async def test_without_prices_the_cost_is_unknown() -> None:
    response = await provider(transport(completion())).review(context_request(RecordingTools()))

    assert response.cost_usd is None


async def test_a_turn_asking_for_tools_past_the_cost_cap_fails_cost_and_runs_none() -> None:
    fake = transport(
        tool_turn(tool_call("c1", "read", {"path": "a.py"}), usage=FIRST_TURN),
        tool_turn(tool_call("c2", "read", {"path": "b.py"}), usage=SECOND_TURN),
        completion(),
    )
    tools = RecordingTools()

    error = await failure(fake, tools, max_cost_usd=Decimal("3.00"), **PRICES)

    assert (error.category, error.cap) == (ErrorCategory.PERMANENT, "cost")
    assert "--max-cost-usd" in str(error)
    assert len(fake.sent) == 2
    assert [name for name, _ in tools.calls] == ["read"]


async def test_an_answer_past_the_cost_cap_is_returned_with_its_cost() -> None:
    fake = transport(
        tool_turn(tool_call("c1", "read", {"path": "a.py"}), usage=FIRST_TURN),
        completion(FINDINGS_JSON, usage=SECOND_TURN),
    )
    settings = {"max_cost_usd": Decimal("3.00"), **PRICES}

    response = await provider(fake, **settings).review(context_request(RecordingTools()))

    assert response.findings == (Finding(**FINDING_FIELDS),)
    assert response.cost_usd == Decimal("3.90")


async def test_a_cost_at_the_cap_goes_on() -> None:
    fake = transport(
        tool_turn(tool_call("c1", "read", {"path": "a.py"}), usage=FIRST_TURN), completion()
    )
    settings = {"max_cost_usd": Decimal("2.60"), **PRICES}

    await provider(fake, **settings).review(context_request(RecordingTools()))

    assert len(fake.sent) == 2


async def test_a_model_that_keeps_calling_tools_fails_turns_after_the_cap_plus_2() -> None:
    fake = transport(*[tool_turn(tool_call("c1", "read", {"path": "a.py"}))] * 4)
    tools = RecordingTools()

    error = await failure(fake, tools, max_tool_calls=1)

    assert (error.category, error.cap) == (ErrorCategory.PERMANENT, "turns")
    assert "--max-tool-calls" in str(error)
    assert len(fake.sent) == 3
    assert len(tools.calls) == 2


async def test_in_diff_mode_no_tools_are_sent() -> None:
    fake = transport(completion())

    await provider(fake).review(ReviewRequest(system_prompt="S", user_prompt="U", model="m1"))

    assert "tools" not in fake.sent[0].body


async def test_in_diff_mode_a_response_asking_for_tools_fails_invalid_output() -> None:
    fake = transport(tool_turn(tool_call("c1", "read", {"path": "a.py"})))

    with pytest.raises(ProviderError) as caught:
        await provider(fake).review(ReviewRequest(system_prompt="S", user_prompt="U", model="m1"))

    assert caught.value.category is ErrorCategory.INVALID_OUTPUT
    assert len(fake.sent) == 1


async def test_a_later_turn_is_retried_on_its_own() -> None:
    fake = transport(
        tool_turn(tool_call("c1", "read", {"path": "a.py"})),
        error_response(503),
        completion(),
    )
    tools = RecordingTools()

    response = await provider(fake, max_retries=1).review(context_request(tools))

    assert len(fake.sent) == 3
    assert fake.sent[1].body == fake.sent[2].body
    assert len(tools.calls) == 1
    assert len(response.warnings) == 1


async def test_the_deadline_bounds_the_tool_calls_too() -> None:
    class SlowTools(RecordingTools):
        async def call(self, name: str, arguments: object) -> str:
            await asyncio.Event().wait()
            raise AssertionError

    fake = transport(tool_turn(tool_call("c1", "read", {"path": "a.py"})))

    with pytest.raises(ProviderError, match="--deadline-s") as caught:
        await provider(fake, deadline_s=0.01).review(context_request(SlowTools()))

    assert caught.value.category is ErrorCategory.TRANSIENT


def test_token_prices_are_both_prices() -> None:
    settings = Settings(**PRICES)

    assert token_prices(settings) == Prices(Decimal("2.50"), Decimal(10))


def test_token_prices_without_prices_or_a_cap_are_none() -> None:
    assert token_prices(Settings()) is None


@pytest.mark.parametrize(
    ("given", "missing"),
    [
        ({"price_input_usd_per_mtok": Decimal(1)}, "--price-output-usd-per-mtok"),
        ({"price_output_usd_per_mtok": Decimal(1)}, "--price-input-usd-per-mtok"),
    ],
)
def test_token_prices_with_one_price_name_the_other(
    given: dict[str, Decimal], missing: str
) -> None:
    with pytest.raises(ConfigError, match=missing):
        token_prices(Settings(**given))


def test_token_prices_with_a_cap_and_no_prices_name_both_flags() -> None:
    with pytest.raises(ConfigError) as caught:
        token_prices(Settings(max_cost_usd=Decimal("0.50")))

    assert "--price-input-usd-per-mtok" in str(caught.value)
    assert "--price-output-usd-per-mtok" in str(caught.value)


def test_the_provider_refuses_a_cost_cap_it_cant_enforce() -> None:
    with pytest.raises(ConfigError, match="--max-cost-usd"):
        provider(transport(), max_cost_usd=Decimal("0.50"))


async def test_over_real_tools_a_bad_call_is_refused_and_recorded_and_the_review_goes_on(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    sha = commit(repo, {"a.py": "x = 1\n"})
    caps = ToolCaps(max_calls=12, max_result_bytes=16_000, max_total_bytes=96_000)
    tools = RepoTools(RepoRef(git=GitRepo(repo, timeout_s=60), commit=sha), caps=caps)
    fake = transport(
        tool_turn(tool_call("c1", "read", "[1]"), tool_call("c2", "read", {"path": "a.py"})),
        completion(),
    )

    await provider(fake).review(context_request(tools))

    assert [(entry.outcome, entry.reason) for entry in tools.ledger] == [
        (ToolOutcome.REFUSED, RefusalReason.INVALID_ARGUMENTS),
        (ToolOutcome.OK, None),
    ]
    answers = fake.sent[1].body["messages"][3:]
    assert "invalid_arguments" in answers[0]["content"]
    assert "1\tx = 1" in answers[1]["content"]


UNUSABLE_USAGE = [None, "many", {"prompt_tokens": 5}, {"prompt_tokens": -1, "completion_tokens": 2}]


def with_usage(response: HttpResponse, usage: object) -> HttpResponse:
    """``response`` with its ``usage`` replaced by ``usage``, or removed if it is None."""
    document = json.loads(response.body)
    if usage is None:
        del document["usage"]
    else:
        document["usage"] = usage
    return HttpResponse(response.status, response.headers, json.dumps(document).encode())


@pytest.mark.parametrize("usage", UNUSABLE_USAGE)
async def test_with_a_cost_cap_a_turn_with_unusable_usage_fails_cost(usage: object) -> None:
    turn = with_usage(tool_turn(tool_call("c1", "read", {"path": "a.py"})), usage)
    fake = transport(turn, completion())
    tools = RecordingTools()

    error = await failure(fake, tools, max_cost_usd=Decimal("3.00"), **PRICES)

    assert (error.category, error.cap) == (ErrorCategory.PERMANENT, "cost")
    assert "--max-cost-usd" in str(error)
    assert len(fake.sent) == 1
    assert tools.calls == []


async def test_with_a_cost_cap_an_answer_with_unusable_usage_is_returned_costing_the_cap() -> None:
    fake = transport(
        tool_turn(tool_call("c1", "read", {"path": "a.py"}), usage=SECOND_TURN),
        with_usage(completion(FINDINGS_JSON), None),
    )
    settings = {"max_cost_usd": Decimal("3.00"), **PRICES}

    response = await provider(fake, **settings).review(context_request(RecordingTools()))

    assert response.findings == (Finding(**FINDING_FIELDS),)
    assert response.cost_usd == Decimal("1.30") + Decimal("3.00")


@pytest.mark.parametrize("usage", UNUSABLE_USAGE)
async def test_without_a_cost_cap_a_turn_with_unusable_usage_counts_no_tokens(
    usage: object,
) -> None:
    fake = transport(
        with_usage(tool_turn(tool_call("c1", "read", {"path": "a.py"})), usage),
        completion(usage=SECOND_TURN),
    )

    response = await provider(fake, **PRICES).review(context_request(RecordingTools()))

    assert response.usage == TokenUsage(500_000, 5_000)
    assert response.cost_usd == Decimal("1.30")
