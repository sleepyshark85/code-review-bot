"""P4-S7: fall back to a second model when the first is unavailable.

Written from the spec's criteria, Limits and Interfaces, Phase 4 design.md ("Second provider and
routing", "Policy as code", Interfaces) and ADR 0021, in parallel with the developer and before
their code was read.

Every command runs in process through its public entry point, building its own providers from
the policy, so the routing is the commands' own: nothing injects a provider for a routed run.
Each route is a fake at the outside boundary:
- Claude: the SDK's `query`, as `providers.claude_agent` calls it, replaced by a scripted
  `FakeSdk` (`use_fake_claude`), so `ClaudeAgentProvider` maps its failures as it really would.
- OpenAI-compatible: a scripted `HttpTransport` (`fake_openai.use_fake_transport`). AC7 and AC8
  route between two of its models, `openai:m1` and `openai:m2`, so one script plays both
  attempts in order, and each POST's `model` says which route sent it.
Nothing reaches a model or the network.

Readings, each used in one place:
- A run's attempts are its `agent`/`attempt` audit records and, when a report is written, its
  `report/v1` `route` (a failed run writes no report). A tried attempt is exactly `{route,
  category, cost_usd}` and a skipped one exactly `{route, skipped}`, as the clarified spec says.
- "Naming the allowed entries" (AC5) is each entry's route name, `<provider>:<model>`.
- AC8's `over_budget` is checked in both modes under a fallback, and in diff mode without one,
  where it is judged as today (on tokens only).
- The spec's Notes are tested too: the fallback must match an entry's `base_url`, an `openai`
  fallback needs both prices under a cost cap, and a fallback without its key is skipped.
- AC11's 240 s and 200 s are scaled down to 6 s and 4 s (and 1.5 s where no time is left), so
  each test takes seconds. "Fails at the deadline" is read as: well after the fallback started,
  and well before a fresh deadline for the fallback would have ended.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import CLIConnectionError, CLINotFoundError
from diff_support import FINDING_DIFF, FINDING_FIELDS
from fake_mcp import CHECKOUT, commit_repo, init_message
from fake_openai import (
    KEY,
    FakeTransport,
    completion,
    connection_failure,
    fake,
    findings_content,
    hang_forever,
    http,
    tool_call,
    tool_calls_completion,
    use_fake_transport,
)
from fake_sdk import FakeSdk, error_result, findings_json, pause, result_message
from test_p1_s4_audit_log import read_records
from test_p2_s1_shadow_report_local import load_shadow
from test_p2_s13_shadow_backfill import WITHIN_BUDGET, Backfiller, History
from test_p3_s3_claude_drives_tools import cap_result
from test_p4_s1_policy_file import copy_cases, live_args, run_evals
from test_p4_s2_openai_compatible_provider import (
    EXIT_CODES,
    KEY_VARIABLE,
    USAGE_EXIT,
    PrCommands,
    Reply,
    server,
)

from review_bot.errors import ErrorCategory
from review_bot.providers import claude_agent
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

__all__ = ["server"]  # a fixture, used by being imported

CLAUDE = "claude:claude-opus-5-5"
OPENAI = "openai:m1"
OPENAI_2 = "openai:m2"
CLAUDE_KEY_VARIABLE = "ANTHROPIC_API_KEY"
FINDINGS_EXIT = 1
DIFF_NOT_FOUND_EXIT = 3
# Exactly two calls at one cap each (AC10).
TWO_CALLS_BUDGET = ("--max-usd", "0.20", "--max-cost-usd", "0.10")
PRICE_FLAGS = ("--price-input-usd-per-mtok", "2.50", "--price-output-usd-per-mtok", "10")
# 123 prompt tokens at $2.50 per million, plus 45 completion tokens at $10 per million.
DEFAULT_TURN_COST = Decimal("0.0007575")
# Prompt tokens at $2.50 per million: 200,000 cost $0.50, 280,000 cost $0.70.
HALF_DOLLAR = {"prompt_tokens": 200_000, "completion_tokens": 0}
SEVENTY_CENTS = {"prompt_tokens": 280_000, "completion_tokens": 0}
FREE = {"prompt_tokens": 0, "completion_tokens": 0}
READ_CHECKOUT = ("read", {"path": CHECKOUT})
GLOB_PY = ("glob", {"pattern": "*.py"})
ATTEMPT_KEYS = {"route", "category", "cost_usd"}
# Generous: it catches a hang or a fresh deadline for the fallback, not a slow CI machine.
BOUND_S = 30.0


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key, price, cap, policy, CI flag or proxy from the machine running the tests."""
    for name in (
        KEY_VARIABLE,
        CLAUDE_KEY_VARIABLE,
        "REVIEW_BOT_OPENAI_BASE_URL",
        "REVIEW_BOT_MAX_COST_USD",
        "REVIEW_BOT_POLICY",
        "REVIEW_BOT_PRICE_INPUT_USD_PER_MTOK",
        "REVIEW_BOT_PRICE_OUTPUT_USD_PER_MTOK",
        "CI",
        "GITHUB_EVENT_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")


# --- The policy -----------------------------------------------------------------------------------


def _split(route: str) -> tuple[str, str]:
    provider, model = route.split(":", 1)
    return provider, model


def routing_policy(
    *models: tuple[str, Sequence[str]],
    fallback: str | None = None,
    max_cost_usd: str | None = None,
    base_url: str | None = None,
    version: str = "fallback-1",
) -> str:
    """A `policy/v1` file listing `models` (route, modes), primary first, and its fallback.

    `base_url`, if given, is set on every `openai` entry and on the fallback.
    """
    lines = [f'version = "{version}"']
    if max_cost_usd is not None:
        lines += ["[cost]", f'max_cost_usd = "{max_cost_usd}"']
    for route, modes in models:
        provider, model = _split(route)
        lines += [
            "[[models]]",
            f'provider = "{provider}"',
            f'model = "{model}"',
            "modes = [" + ", ".join(f'"{mode}"' for mode in modes) + "]",
        ]
        if base_url is not None and provider == "openai":
            lines.append(f'base_url = "{base_url}"')
    if fallback is not None:
        provider, model = _split(fallback)
        url = "" if base_url is None else f', base_url = "{base_url}"'
        lines += ["[routing]", f'fallback = {{ provider = "{provider}", model = "{model}"{url} }}']
    return "\n".join(lines) + "\n"


# AC1's policy: Claude for both modes, the fallback for diff mode only.
AC1_POLICY = routing_policy((CLAUDE, ("diff", "context")), (OPENAI, ("diff",)), fallback=OPENAI)
# Two models of the second provider, both for both modes (AC7, AC8).
OPENAI_PAIR_POLICY = routing_policy(
    (OPENAI, ("diff", "context")), (OPENAI_2, ("diff", "context")), fallback=OPENAI_2
)


# --- The fakes at the boundary --------------------------------------------------------------------


def use_fake_claude(monkeypatch: pytest.MonkeyPatch, sdk: FakeSdk) -> None:
    """Make every Claude provider the commands build stream from `sdk` instead of the CLI."""

    def query(*, prompt: Any, options: Any) -> Any:
        return sdk(prompt, options)

    monkeypatch.setattr(claude_agent, "query", query)


def claude_answers(*fields: dict[str, Any]) -> FakeSdk:
    return FakeSdk(
        result_message(
            findings_json(*fields),
            usage={"input_tokens": 100, "output_tokens": 10},
            total_cost_usd=0.01,
        )
    )


def claude_fails(category: ErrorCategory) -> FakeSdk:
    """A Claude call that fails with `category`, as `ClaudeAgentProvider` maps the SDK's signals."""
    return FakeSdk(CLAUDE_FAILURES[category])


CLAUDE_FAILURES: dict[ErrorCategory, Any] = {
    ErrorCategory.UNAVAILABLE: CLINotFoundError(),
    ErrorCategory.TRANSIENT: error_result(503),
    ErrorCategory.RATE_LIMIT: error_result(429),
    ErrorCategory.AUTH: error_result(401),
    ErrorCategory.PERMANENT: error_result(400),
    ErrorCategory.REFUSAL: result_message(findings_json(), stop_reason="refusal"),
    ErrorCategory.INVALID_OUTPUT: result_message("Looks good to me."),
}


def answer(*fields: dict[str, Any], **usage: int) -> Any:
    return completion(findings_content(*fields), **usage)


def asks(*calls: tuple[str, Any], **usage: int) -> Any:
    """A turn asking for `calls`, each `(name, arguments)`."""
    entries = [tool_call(f"call_{n}", name, args) for n, (name, args) in enumerate(calls, 1)]
    return tool_calls_completion(*entries, **usage)


# --- Running review-bot ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Done:
    exit_code: int
    stdout: str
    stderr: str
    audit_path: Path
    elapsed_s: float

    @property
    def records(self) -> list[dict[str, Any]]:
        return read_records(self.audit_path) if self.audit_path.exists() else []

    @property
    def report(self) -> dict[str, Any]:
        import json  # noqa: PLC0415 - only reports need it

        document = json.loads(self.stdout)
        assert document["schema"] == "report/v1", document
        return document

    @property
    def route(self) -> list[dict[str, Any]]:
        route = self.report["route"]
        assert isinstance(route, list), route
        return route

    @property
    def attempts(self) -> list[dict[str, Any]]:
        """The run's `agent`/`attempt` audit records, in order."""
        return attempt_records(self.records)

    def events(self, stage: str, event: str) -> list[dict[str, Any]]:
        return [r["data"] for r in self.records if (r["stage"], r["event"]) == (stage, event)]

    def fails_as(self, category: ErrorCategory) -> None:
        assert self.exit_code == EXIT_CODES[category], self.stderr
        assert f"{category}:" in self.stderr, self.stderr


def attempt_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r["data"] for r in records if (r["stage"], r["event"]) == ("agent", "attempt")]


class Router:
    """`review-bot DIFF --provider P --model M --policy POLICY --format json --audit-log LOG`."""

    def __init__(self, bot: Any, write_diff: Callable[..., Path], tmp_path: Path, monkeypatch):
        self._bot = bot
        self._write_diff = write_diff
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch
        self.runs = 0

    def policy(self, text: str) -> Path:
        self.runs += 1
        path = self._tmp_path / f"policy-{self.runs}.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def review(  # noqa: PLR0913 - one keyword per thing a test varies
        self,
        policy: str | None,
        *flags: str,
        claude: FakeSdk | None = None,
        transport: FakeTransport | None = None,
        route: str = CLAUDE,
        key: str | None = KEY,
        diff_path: Path | None = None,
        real_transport: bool = False,
    ) -> Done:
        """Run with `claude` and `transport` as the routes' fakes; `policy` None is the built-in.

        With `real_transport`, the second provider sends through its own `UrllibTransport`.
        """
        if key is None:
            self._monkeypatch.delenv(KEY_VARIABLE, raising=False)
        else:
            self._monkeypatch.setenv(KEY_VARIABLE, key)
        use_fake_claude(self._monkeypatch, claude or FakeSdk())
        if not real_transport:
            use_fake_transport(self._monkeypatch, transport or fake())
        provider, model = _split(route)
        policy_flags = () if policy is None else ("--policy", str(self.policy(policy)))
        self.runs += 1
        audit = self._tmp_path / f"audit-{self.runs}.jsonl"
        path = self._write_diff(FINDING_DIFF) if diff_path is None else diff_path
        started = time.monotonic()
        run = self._bot.run(
            str(path), "--provider", provider, "--model", model, *policy_flags,
            "--format", "json", "--max-retries", "0", "--audit-log", str(audit), *flags,
        )  # fmt: skip
        elapsed = time.monotonic() - started
        return Done(run.exit_code, run.stdout, run.stderr, audit, elapsed)


@pytest.fixture
def router(bot, write_diff, tmp_path, monkeypatch) -> Router:
    return Router(bot, write_diff, tmp_path, monkeypatch)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return commit_repo(tmp_path / "repo").path


def as_decimal(value: object) -> Decimal:
    """A dollar amount written as a string or a number, read exactly."""
    assert isinstance(value, str | int | float), value
    return Decimal(str(value))


def sent_models(transport: FakeTransport) -> list[str]:
    return [sent.body["model"] for sent in transport.sent]


def claude_prompt(sdk: FakeSdk) -> str:
    [(prompt, _options)] = sdk.calls
    assert isinstance(prompt, str), "diff mode sends its prompt as a string"
    return prompt


# --- AC1: an unavailable primary hands the same request to the fallback, once ---------------------


def test_p4_s7_ac1_fallback_gets_the_same_request_once(router):
    claude = claude_fails(ErrorCategory.UNAVAILABLE)
    transport = fake(answer(FINDING_FIELDS))

    done = router.review(AC1_POLICY, *PRICE_FLAGS, claude=claude, transport=transport)

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert len(claude.calls) == 1
    sent = transport.one
    assert sent.body["model"] == "m1"
    [system, user] = sent.messages
    assert system["content"] == claude.options.system_prompt
    assert user["content"] == claude_prompt(claude)


def test_p4_s7_ac1_report_route_names_both_attempts_in_order(router):
    transport = fake(answer(FINDING_FIELDS))

    done = router.review(
        AC1_POLICY,
        *PRICE_FLAGS,
        claude=claude_fails(ErrorCategory.UNAVAILABLE),
        transport=transport,
    )

    first, second = done.route
    assert first == {"route": CLAUDE, "category": "unavailable", "cost_usd": None}
    assert set(second) == ATTEMPT_KEYS, second
    assert (second["route"], second["category"]) == (OPENAI, None)
    assert as_decimal(second["cost_usd"]) == DEFAULT_TURN_COST


def test_p4_s7_ac1_route_is_appended_after_the_earlier_report_keys(router):
    """Design: report keys are appended only, so `route` follows `policy`."""
    done = router.review(
        AC1_POLICY, claude=claude_fails(ErrorCategory.UNAVAILABLE), transport=fake(answer())
    )

    keys = tuple(done.report)
    assert keys.index("route") > keys.index("policy"), keys


def test_p4_s7_ac1_findings_are_the_fallbacks(router):
    done = router.review(
        AC1_POLICY,
        claude=claude_fails(ErrorCategory.UNAVAILABLE),
        transport=fake(answer(FINDING_FIELDS)),
    )

    [finding] = done.report["findings"]
    assert (finding["file"], finding["line"]) == (FINDING_FIELDS["file"], FINDING_FIELDS["line"])
    assert finding["rationale"] == FINDING_FIELDS["rationale"]


def test_p4_s7_ac1_fallback_with_no_findings_exits_0(router):
    done = router.review(
        AC1_POLICY, claude=claude_fails(ErrorCategory.UNAVAILABLE), transport=fake(answer())
    )

    assert done.exit_code == 0, done.stderr
    assert done.report["findings"] == []


def test_p4_s7_ac1_audit_has_one_attempt_record_per_attempt_in_order(router):
    done = router.review(
        AC1_POLICY, claude=claude_fails(ErrorCategory.UNAVAILABLE), transport=fake(answer())
    )

    assert [record["route"] for record in done.attempts] == [CLAUDE, OPENAI]
    first, second = done.attempts
    assert first == {"route": CLAUDE, "category": "unavailable", "cost_usd": None}
    assert set(second) == ATTEMPT_KEYS, second
    assert second["category"] is None


def test_p4_s7_ac1_fallback_posts_to_its_entrys_base_url(router):
    policy = routing_policy(
        (CLAUDE, ("diff",)),
        (OPENAI, ("diff",)),
        fallback=OPENAI,
        base_url="https://llm.example.com/v1",
    )
    transport = fake(answer())

    done = router.review(
        policy, claude=claude_fails(ErrorCategory.UNAVAILABLE), transport=transport
    )

    assert done.exit_code == 0, done.stderr
    assert transport.one.url == "https://llm.example.com/v1/chat/completions"


def test_p4_s7_ac1_fallback_key_reaches_no_output(router):
    done = router.review(
        AC1_POLICY, claude=claude_fails(ErrorCategory.UNAVAILABLE), transport=fake(answer())
    )

    for text in (done.stdout, done.stderr, done.audit_path.read_text(encoding="ascii")):
        assert KEY not in text


# --- AC2: only availability failures route --------------------------------------------------------


ROUTABLE = [
    pytest.param(ErrorCategory.UNAVAILABLE, CLINotFoundError(), id="unavailable"),
    pytest.param(ErrorCategory.TRANSIENT, error_result(503), id="transient_503"),
    pytest.param(ErrorCategory.TRANSIENT, error_result(529), id="transient_529"),
    pytest.param(
        ErrorCategory.TRANSIENT, CLIConnectionError("Could not connect."), id="transient_conn"
    ),
    pytest.param(ErrorCategory.RATE_LIMIT, error_result(429), id="rate_limit"),
]


@pytest.mark.parametrize(("category", "failure"), ROUTABLE)
def test_p4_s7_ac2_routable_failure_hands_over_to_the_fallback(router, category, failure):
    transport = fake(answer(FINDING_FIELDS))

    done = router.review(AC1_POLICY, claude=FakeSdk(failure), transport=transport)

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert len(transport.sent) == 1
    assert [(a["route"], a["category"]) for a in done.route] == [
        (CLAUDE, str(category)),
        (OPENAI, None),
    ]
    assert [record["route"] for record in done.attempts] == [CLAUDE, OPENAI]


NOT_ROUTABLE = [
    ErrorCategory.AUTH,
    ErrorCategory.REFUSAL,
    ErrorCategory.INVALID_OUTPUT,
    ErrorCategory.PERMANENT,
]


@pytest.mark.parametrize("category", NOT_ROUTABLE, ids=str)
def test_p4_s7_ac2_other_failures_never_reach_the_fallback(router, category):
    claude = claude_fails(category)
    transport = fake(answer())

    done = router.review(AC1_POLICY, claude=claude, transport=transport)

    done.fails_as(category)
    assert len(claude.calls) == 1
    assert transport.sent == []
    assert [record["route"] for record in done.attempts] == [CLAUDE]


@pytest.mark.parametrize("category", NOT_ROUTABLE, ids=str)
def test_p4_s7_ac2_other_failures_fail_as_without_a_fallback(router, category):
    """ "Fails as today": the same exit code and error line as a run under the built-in policy."""
    routed = router.review(AC1_POLICY, claude=claude_fails(category), transport=fake(answer()))
    plain = router.review(None, claude=claude_fails(category))

    assert routed.exit_code == plain.exit_code
    assert routed.stderr.splitlines()[0] == plain.stderr.splitlines()[0]


def test_p4_s7_ac2_cost_cap_failure_never_reaches_the_fallback(router):
    claude = FakeSdk(cap_result("error_max_budget_usd"))
    transport = fake(answer())

    done = router.review(
        AC1_POLICY, *PRICE_FLAGS, "--max-cost-usd", "0.60", claude=claude, transport=transport
    )

    done.fails_as(ErrorCategory.PERMANENT)
    assert "--max-cost-usd" in done.stderr
    assert transport.sent == []
    assert [record["route"] for record in done.attempts] == [CLAUDE]


def test_p4_s7_ac2_turn_cap_failure_in_context_mode_never_reaches_the_fallback(router, repo):
    """The fallback lists context mode here, so only the cap can keep it out."""
    policy = routing_policy(
        (CLAUDE, ("diff", "context")), (OPENAI, ("diff", "context")), fallback=OPENAI
    )
    claude = FakeSdk(init_message(), cap_result("error_max_turns"))
    transport = fake(answer())

    done = router.review(policy, "--context-repo", str(repo), claude=claude, transport=transport)

    done.fails_as(ErrorCategory.PERMANENT)
    assert transport.sent == []
    assert [record["route"] for record in done.attempts] == [CLAUDE]


def test_p4_s7_ac2_openai_primary_cost_cap_never_reaches_the_fallback(router, repo):
    """A cap on the second provider (P4-S6 AC6) doesn't route either, to its other model."""
    transport = fake(asks(READ_CHECKOUT, **SEVENTY_CENTS), answer())

    done = router.review(
        OPENAI_PAIR_POLICY, *PRICE_FLAGS, "--max-cost-usd", "0.60", "--context-repo", str(repo),
        route=OPENAI, transport=transport,
    )  # fmt: skip

    done.fails_as(ErrorCategory.PERMANENT)
    assert sent_models(transport) == ["m1"]
    assert [record["route"] for record in done.attempts] == [OPENAI]


def test_p4_s7_ac2_openai_primary_turn_cap_never_reaches_the_fallback(router, repo):
    """`--max-tool-calls 1` allows 3 turns (P4-S6 AC4); the third still asks, so `cap` `turns`."""
    transport = fake(asks(READ_CHECKOUT), asks(GLOB_PY), asks(GLOB_PY), answer())

    done = router.review(
        OPENAI_PAIR_POLICY, "--context-repo", str(repo), "--max-tool-calls", "1",
        route=OPENAI, transport=transport,
    )  # fmt: skip

    done.fails_as(ErrorCategory.PERMANENT)
    assert "--max-tool-calls" in done.stderr
    assert set(sent_models(transport)) == {"m1"}
    assert [record["route"] for record in done.attempts] == [OPENAI]


# --- AC3: both routes failing ---------------------------------------------------------------------


def test_p4_s7_ac3_both_unavailable_fails_unavailable_listing_both(router):
    transport = fake(connection_failure("refused"))

    done = router.review(
        AC1_POLICY, claude=claude_fails(ErrorCategory.UNAVAILABLE), transport=transport
    )

    done.fails_as(ErrorCategory.UNAVAILABLE)
    assert done.stdout == ""
    assert len(transport.sent) == 1
    assert [record["route"] for record in done.attempts] == [CLAUDE, OPENAI]


@pytest.mark.parametrize(
    ("claude_category", "reply", "last_category"),
    [
        pytest.param(
            ErrorCategory.UNAVAILABLE, http(503), ErrorCategory.TRANSIENT, id="then_transient"
        ),
        pytest.param(
            ErrorCategory.TRANSIENT,
            connection_failure("dns"),
            ErrorCategory.UNAVAILABLE,
            id="transient_then_unavailable",
        ),
        pytest.param(ErrorCategory.UNAVAILABLE, http(401), ErrorCategory.AUTH, id="then_auth"),
        pytest.param(
            ErrorCategory.RATE_LIMIT, http(429), ErrorCategory.RATE_LIMIT, id="both_rate_limited"
        ),
    ],
)
def test_p4_s7_ac3_run_fails_with_the_last_attempts_category_and_exit_code(
    router, claude_category, reply, last_category
):
    transport = fake(reply)

    done = router.review(AC1_POLICY, claude=claude_fails(claude_category), transport=transport)

    done.fails_as(last_category)
    assert len(transport.sent) == 1
    assert [record["route"] for record in done.attempts] == [CLAUDE, OPENAI]


def test_p4_s7_ac3_fallback_is_tried_once_never_back_to_the_primary(router):
    claude = claude_fails(ErrorCategory.UNAVAILABLE)
    transport = fake(connection_failure("refused"))

    router.review(AC1_POLICY, claude=claude, transport=transport)

    assert len(claude.calls) == 1
    assert len(transport.sent) == 1


# --- AC4: no routing, as before -------------------------------------------------------------------


NO_ROUTING_POLICY = routing_policy((CLAUDE, ("diff", "context")), (OPENAI, ("diff",)))


@pytest.mark.parametrize("policy", [None, NO_ROUTING_POLICY], ids=["builtin", "no_routing"])
def test_p4_s7_ac4_unavailable_primary_without_routing_calls_nothing_else(router, policy):
    claude = claude_fails(ErrorCategory.UNAVAILABLE)
    transport = fake(answer())

    done = router.review(policy, claude=claude, transport=transport)

    done.fails_as(ErrorCategory.UNAVAILABLE)
    assert len(claude.calls) == 1
    assert transport.sent == []
    assert [record["route"] for record in done.attempts] == [CLAUDE]


@pytest.mark.parametrize("policy", [None, NO_ROUTING_POLICY], ids=["builtin", "no_routing"])
def test_p4_s7_ac4_route_holds_the_single_attempt(router, policy):
    done = router.review(policy, claude=claude_answers(FINDING_FIELDS))

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    [attempt] = done.route
    assert set(attempt) == ATTEMPT_KEYS, attempt
    assert (attempt["route"], attempt["category"]) == (CLAUDE, None)
    assert as_decimal(attempt["cost_usd"]) == Decimal("0.01")


def test_p4_s7_ac4_request_findings_and_exit_code_are_as_without_a_policy(router):
    plain_sdk, routed_sdk = claude_answers(FINDING_FIELDS), claude_answers(FINDING_FIELDS)

    plain = router.review(None, claude=plain_sdk)
    routed = router.review(NO_ROUTING_POLICY, claude=routed_sdk)

    assert routed.exit_code == plain.exit_code == FINDINGS_EXIT
    assert claude_prompt(routed_sdk) == claude_prompt(plain_sdk)
    assert routed_sdk.options.system_prompt == plain_sdk.options.system_prompt
    assert routed.report["findings"] == plain.report["findings"]


def test_p4_s7_ac4_openai_primary_under_the_builtin_policy_records_one_attempt(router):
    transport = fake(answer())

    done = router.review(None, route=OPENAI, transport=transport)

    assert done.exit_code == 0, done.stderr
    assert len(transport.sent) == 1
    [attempt] = done.route
    assert (attempt["route"], attempt["category"]) == (OPENAI, None)


def test_p4_s7_ac4_injected_mock_provider_records_one_attempt(bot, sample_diff):
    provider = MockProvider(findings=[])

    run = bot.review(sample_diff, "--format", "json", provider=provider)

    assert run.exit_code == FINDINGS_EXIT, run.stderr  # the sample's rule findings
    assert len(provider.requests) == 1
    import json  # noqa: PLC0415 - only this test reads a report without `Done`

    [attempt] = json.loads(run.stdout)["route"]
    assert attempt["category"] is None


# --- AC5: only models the policy lists may review -------------------------------------------------


def test_p4_s7_ac5_unlisted_claude_model_exits_2_before_the_diff_naming_the_entries(
    router, tmp_path
):
    claude = claude_answers()
    transport = fake(answer())

    done = router.review(
        AC1_POLICY,
        route="claude:other",
        claude=claude,
        transport=transport,
        diff_path=tmp_path / "missing.diff",
    )

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert CLAUDE in done.stderr
    assert OPENAI in done.stderr
    assert claude.calls == []
    assert transport.sent == []


def test_p4_s7_ac5_unlisted_openai_model_exits_2_before_the_diff(router, tmp_path):
    transport = fake(answer())

    done = router.review(
        AC1_POLICY, route="openai:m9", transport=transport, diff_path=tmp_path / "missing.diff"
    )

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert OPENAI in done.stderr
    assert transport.sent == []


def test_p4_s7_ac5_listed_model_of_another_provider_doesnt_match(router, tmp_path):
    """`m1` is listed for `openai`, not for `claude`: an entry is a provider and a model."""
    claude = claude_answers()

    done = router.review(
        AC1_POLICY, route="claude:m1", claude=claude, diff_path=tmp_path / "missing.diff"
    )

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert claude.calls == []


def test_p4_s7_ac5_context_mode_on_a_diff_only_entry_exits_2_before_the_diff(
    router, repo, tmp_path
):
    policy = routing_policy((CLAUDE, ("diff",)))
    claude = claude_answers()

    done = router.review(
        policy, "--context-repo", str(repo), claude=claude, diff_path=tmp_path / "missing.diff"
    )

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert CLAUDE in done.stderr
    assert claude.calls == []


def test_p4_s7_ac5_listed_model_runs(router):
    done = router.review(AC1_POLICY, claude=claude_answers())

    assert done.exit_code == 0, done.stderr


def test_p4_s7_ac5_mock_provider_is_exempt(bot, sample_diff, router):
    path = router.policy(AC1_POLICY)
    provider = MockProvider(findings=[])

    run = bot.review(sample_diff, "--policy", str(path), provider=provider)

    assert run.exit_code == FINDINGS_EXIT, run.stderr  # the sample's rule findings
    assert len(provider.requests) == 1


def test_p4_s7_ac5_mock_provider_without_injection_is_exempt(bot, sample_diff, router):
    path = router.policy(AC1_POLICY)

    run = bot.review(sample_diff, "--policy", str(path))

    assert run.exit_code == FINDINGS_EXIT, run.stderr


CAPPED_FALLBACK_POLICY = routing_policy(
    (CLAUDE, ("diff",)), (OPENAI, ("diff",)), fallback=OPENAI, max_cost_usd="0.60"
)


def test_p4_s7_ac5_mock_is_neither_checked_nor_routed_under_a_capped_fallback(
    bot, write_diff, router, monkeypatch
):
    """No price flags: an `openai` fallback would need them, but mock runs are never routed."""
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    transport = fake(answer())
    use_fake_transport(monkeypatch, transport)
    provider = MockProvider(findings=[])
    path = router.policy(CAPPED_FALLBACK_POLICY)

    run = bot.review(write_diff(FINDING_DIFF), "--policy", str(path), provider=provider)

    assert run.exit_code == 0, run.stderr
    assert len(provider.requests) == 1
    assert transport.sent == []


def test_p4_s7_ac5_mock_without_injection_isnt_routed_under_a_capped_fallback(
    bot, sample_diff, router, monkeypatch
):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    transport = fake(answer())
    use_fake_transport(monkeypatch, transport)
    path = router.policy(CAPPED_FALLBACK_POLICY)

    run = bot.review(sample_diff, "--policy", str(path))

    assert run.exit_code == FINDINGS_EXIT, run.stderr  # the sample's rule findings
    assert transport.sent == []


ENTRY_BASE = "https://a.example.com/v1"


@pytest.mark.parametrize(
    "base_flags",
    [("--openai-base-url", "https://b.example.com/v1"), ()],
    ids=["other_base", "default_base"],
)
def test_p4_s7_ac5_openai_base_url_unlike_the_entrys_exits_2_before_the_diff(
    router, tmp_path, base_flags
):
    policy = routing_policy((OPENAI, ("diff",)), base_url=ENTRY_BASE)
    transport = fake(answer())

    done = router.review(
        policy, *base_flags, route=OPENAI, transport=transport,
        diff_path=tmp_path / "missing.diff",
    )  # fmt: skip

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert OPENAI in done.stderr
    assert transport.sent == []


def test_p4_s7_ac5_openai_base_url_matching_the_entrys_runs(router):
    policy = routing_policy((OPENAI, ("diff",)), base_url=ENTRY_BASE)
    transport = fake(answer())

    done = router.review(policy, "--openai-base-url", ENTRY_BASE, route=OPENAI, transport=transport)

    assert done.exit_code == 0, done.stderr
    assert transport.one.url == f"{ENTRY_BASE}/chat/completions"


@pytest.mark.parametrize(
    "fallback", ["openai:m9", "claude:claude-sonnet-x"], ids=["unlisted_model", "unlisted_claude"]
)
def test_p4_s7_ac5_fallback_not_in_models_makes_the_policy_invalid(router, tmp_path, fallback):
    policy = routing_policy((CLAUDE, ("diff",)), (OPENAI, ("diff",)), fallback=fallback)
    claude = claude_answers()
    transport = fake(answer())

    done = router.review(
        policy, claude=claude, transport=transport, diff_path=tmp_path / "missing.diff"
    )

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert claude.calls == []
    assert transport.sent == []


def test_p4_s7_ac5_fallback_with_another_base_url_isnt_the_entry(router, tmp_path):
    """Notes: the fallback must match an entry's provider, model and `base_url`."""
    entries = routing_policy(
        (CLAUDE, ("diff",)), (OPENAI, ("diff",)), base_url="https://a.example.com/v1"
    )
    policy = (
        entries + '[routing]\nfallback = { provider = "openai", model = "m1", '
        'base_url = "https://b.example.com/v1" }\n'
    )
    claude = claude_answers()

    done = router.review(policy, claude=claude, diff_path=tmp_path / "missing.diff")

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert claude.calls == []


def test_p4_s7_ac5_fallback_without_any_models_makes_the_policy_invalid(router, tmp_path):
    policy = 'version = "fallback-1"\n[routing]\nfallback = { provider = "openai", model = "m1" }\n'

    done = router.review(policy, diff_path=tmp_path / "missing.diff")

    assert done.exit_code == USAGE_EXIT, done.stderr


@pytest.mark.parametrize(
    "entry",
    [
        'provider = "claude"\nmodel = "claude-opus-5-5"\nmodes = ["diff"]\nextra = 1',
        'provider = "claude"\nmodel = "claude-opus-5-5"\nmodes = ["tools"]',
        'provider = "claude"\nmodes = ["diff"]',
        'model = "claude-opus-5-5"\nmodes = ["diff"]',
        'provider = "claude"\nmodel = "claude-opus-5-5"\nmodes = "diff"',
    ],
    ids=["unknown_key", "unknown_mode", "no_model", "no_provider", "modes_not_a_list"],
)
def test_p4_s7_ac5_malformed_models_entry_makes_the_policy_invalid(router, tmp_path, entry):
    """Design: unknown keys in `policy/v1` are invalid, and an entry needs its fields."""
    policy = f'version = "fallback-1"\n[[models]]\n{entry}\n'

    done = router.review(policy, diff_path=tmp_path / "missing.diff")

    assert done.exit_code == USAGE_EXIT, done.stderr


def test_p4_s7_ac5_pr_shadow_refuses_an_unlisted_model_before_any_call(pr, router, monkeypatch):
    path = router.policy(AC1_POLICY)
    claude = claude_answers()
    use_fake_claude(monkeypatch, claude)

    done = pr.run("shadow", "--provider", "claude", "--model", "other", "--policy", str(path))

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert CLAUDE in done.stderr
    assert claude.calls == []


# --- AC6: a fallback that doesn't serve context mode ----------------------------------------------


def test_p4_s7_ac6_context_mode_never_falls_back_to_a_diff_only_model(router, repo):
    claude = claude_fails(ErrorCategory.UNAVAILABLE)
    transport = fake(answer())

    done = router.review(
        AC1_POLICY, "--context-repo", str(repo), claude=claude, transport=transport
    )

    done.fails_as(ErrorCategory.UNAVAILABLE)
    assert len(claude.calls) == 1
    assert transport.sent == []


def test_p4_s7_ac6_the_same_policy_falls_back_in_diff_mode(router):
    """The control: only the mode keeps the fallback out above."""
    transport = fake(answer())

    done = router.review(
        AC1_POLICY, claude=claude_fails(ErrorCategory.UNAVAILABLE), transport=transport
    )

    assert done.exit_code == 0, done.stderr
    assert len(transport.sent) == 1


def test_p4_s7_ac6_context_mode_falls_back_to_a_model_listing_context(router, repo):
    policy = routing_policy(
        (CLAUDE, ("diff", "context")), (OPENAI, ("diff", "context")), fallback=OPENAI
    )
    transport = fake(answer())

    done = router.review(
        policy,
        "--context-repo",
        str(repo),
        claude=claude_fails(ErrorCategory.UNAVAILABLE),
        transport=transport,
    )

    assert done.exit_code == 0, done.stderr
    assert len(transport.sent) == 1
    assert "tools" in transport.one.body


# --- AC7: one executor, so the tool caps cover the whole review -----------------------------------


def primary_makes_five_calls_then_fails() -> list[Any]:
    """`openai:m1`'s script: five turns of one `read` each, then a 503 (`transient`)."""
    return [*(asks(READ_CHECKOUT) for _ in range(5)), http(503)]


def tool_calls(done: Done) -> list[dict[str, Any]]:
    return done.events("context", "tool_call")


def test_p4_s7_ac7_fallback_has_the_calls_left_and_the_ledger_holds_all_12(router, repo):
    transport = fake(
        *primary_makes_five_calls_then_fails(), asks(*[GLOB_PY] * 7), answer(FINDING_FIELDS)
    )

    done = router.review(
        OPENAI_PAIR_POLICY, "--context-repo", str(repo), "--max-tool-calls", "12",
        route=OPENAI, transport=transport,
    )  # fmt: skip

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert sent_models(transport) == ["m1"] * 6 + ["m2"] * 2
    calls = tool_calls(done)
    assert [call["tool"] for call in calls] == ["read"] * 5 + ["glob"] * 7
    assert all(call["outcome"] == "ok" for call in calls), calls


def test_p4_s7_ac7_fallbacks_eighth_call_is_refused_at_the_call_limit(router, repo):
    transport = fake(
        *primary_makes_five_calls_then_fails(), asks(*[GLOB_PY] * 8), answer(FINDING_FIELDS)
    )

    done = router.review(
        OPENAI_PAIR_POLICY, "--context-repo", str(repo), "--max-tool-calls", "12",
        route=OPENAI, transport=transport,
    )  # fmt: skip

    calls = tool_calls(done)
    assert [call["outcome"] for call in calls[:12]] == ["ok"] * 12, calls
    assert calls[12]["outcome"] != "ok"
    assert calls[12]["reason"] == "call_limit"


def test_p4_s7_ac7_fallback_starts_a_fresh_conversation(router, repo):
    """The same request: the primary's tool turns aren't carried into the fallback's."""
    transport = fake(*primary_makes_five_calls_then_fails(), answer())

    router.review(
        OPENAI_PAIR_POLICY, "--context-repo", str(repo), route=OPENAI, transport=transport
    )

    first_primary, first_fallback = transport.sent[0], transport.sent[6]
    assert first_fallback.body["model"] == "m2"
    assert first_fallback.messages == first_primary.messages
    assert first_fallback.body["tools"] == first_primary.body["tools"]


def test_p4_s7_ac7_primary_calls_are_recorded_even_when_every_route_fails(router, repo):
    transport = fake(*primary_makes_five_calls_then_fails(), connection_failure("refused"))

    done = router.review(
        OPENAI_PAIR_POLICY, "--context-repo", str(repo), route=OPENAI, transport=transport
    )

    done.fails_as(ErrorCategory.UNAVAILABLE)
    assert [call["tool"] for call in tool_calls(done)] == ["read"] * 5


# --- AC8: each attempt has its own cost cap -------------------------------------------------------


def test_p4_s7_ac8_both_attempts_are_capped_at_the_ceiling(router):
    policy = routing_policy(
        (CLAUDE, ("diff",)), (OPENAI, ("diff",)), fallback=OPENAI, max_cost_usd="0.60"
    )
    claude = claude_fails(ErrorCategory.UNAVAILABLE)
    transport = fake(answer(FINDING_FIELDS))

    done = router.review(policy, *PRICE_FLAGS, claude=claude, transport=transport)

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert claude.options.max_budget_usd == 0.60
    assert len(transport.sent) == 1


def test_p4_s7_ac8_fallback_gets_a_whole_cap_not_what_the_primary_left(router, repo):
    """$0.50 each: a cap shared across attempts would stop the fallback after its first turn."""
    policy = routing_policy(
        (OPENAI, ("diff", "context")),
        (OPENAI_2, ("diff", "context")),
        fallback=OPENAI_2,
        max_cost_usd="0.60",
    )
    transport = fake(
        asks(READ_CHECKOUT, **HALF_DOLLAR),
        http(503),
        asks(GLOB_PY, **HALF_DOLLAR),
        answer(FINDING_FIELDS, **FREE),
    )

    done = router.review(
        policy, *PRICE_FLAGS, "--context-repo", str(repo), route=OPENAI, transport=transport
    )

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert sent_models(transport) == ["m1", "m1", "m2", "m2"]


def test_p4_s7_ac8_fallback_over_its_own_cap_still_fails_on_the_cap(router, repo):
    policy = routing_policy(
        (OPENAI, ("diff", "context")),
        (OPENAI_2, ("diff", "context")),
        fallback=OPENAI_2,
        max_cost_usd="0.60",
    )
    transport = fake(http(503), asks(GLOB_PY, **SEVENTY_CENTS), answer(FINDING_FIELDS))

    done = router.review(
        policy, *PRICE_FLAGS, "--context-repo", str(repo), route=OPENAI, transport=transport
    )

    done.fails_as(ErrorCategory.PERMANENT)
    assert "--max-cost-usd" in done.stderr
    assert sent_models(transport) == ["m1", "m2"]


@pytest.fixture
def pr(capsys, tmp_path, write_diff, monkeypatch) -> PrCommands:
    monkeypatch.delenv("REVIEW_BOT_AUDIT_LOG", raising=False)  # so each log lands in its --out
    return PrCommands(capsys, tmp_path, write_diff)


def pr_context_run(
    pr: PrCommands, router: Router, monkeypatch: pytest.MonkeyPatch, transport: FakeTransport
) -> Any:
    """`pr run --context repo` with `openai:m1` routing to `openai:m2`, capped at $0.60."""
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    use_fake_transport(monkeypatch, transport)
    policy = routing_policy(
        (OPENAI, ("diff", "context")),
        (OPENAI_2, ("diff", "context")),
        fallback=OPENAI_2,
        max_cost_usd="0.60",
    )
    return pr.run(
        "run", "--provider", "openai", "--model", "m1", "--context", "repo",
        "--policy", str(router.policy(policy)), "--max-retries", "0", *PRICE_FLAGS,
    )  # fmt: skip


def test_p4_s7_ac8_over_budget_when_the_fallbacks_cost_is_over_the_cap(pr, router, monkeypatch):
    """P4-S6 AC6: a final answer over the cap is returned, so its cost is reported over it."""
    transport = fake(http(503), answer(**SEVENTY_CENTS))

    done = pr_context_run(pr, router, monkeypatch, transport)

    shadow = load_shadow(done.out)
    assert shadow["outcome"] == "reviewed", done.stderr
    assert sent_models(transport) == ["m1", "m2"]
    assert shadow["over_budget"] is True


def test_p4_s7_ac8_not_over_budget_when_only_the_attempts_together_are_over(
    pr, router, monkeypatch
):
    """$0.50 and $0.50: $1.00 in all, but no attempt over $0.60."""
    transport = fake(
        asks(READ_CHECKOUT, **HALF_DOLLAR),
        http(503),
        asks(GLOB_PY, **HALF_DOLLAR),
        answer(**FREE),
    )

    done = pr_context_run(pr, router, monkeypatch, transport)

    shadow = load_shadow(done.out)
    assert shadow["outcome"] == "reviewed", done.stderr
    assert sent_models(transport) == ["m1", "m1", "m2", "m2"]
    assert shadow["over_budget"] is False


def test_p4_s7_ac8_shadow_caps_hold_the_per_attempt_cap(pr, router, monkeypatch):
    transport = fake(http(503), answer(**FREE))

    done = pr_context_run(pr, router, monkeypatch, transport)

    assert as_decimal(load_shadow(done.out)["caps"]["max_cost_usd"]) == Decimal("0.60")


def pr_diff_shadow(
    pr: PrCommands, router: Router, monkeypatch: pytest.MonkeyPatch, transport: FakeTransport
) -> Any:
    """`pr shadow` in diff mode, Claude `unavailable`, falling back to `openai:m1`, capped $0.60."""
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    use_fake_transport(monkeypatch, transport)
    use_fake_claude(monkeypatch, claude_fails(ErrorCategory.UNAVAILABLE))
    policy = routing_policy(
        (CLAUDE, ("diff",)), (OPENAI, ("diff",)), fallback=OPENAI, max_cost_usd="0.60"
    )
    return pr.run(
        "shadow", "--provider", "claude", "--policy", str(router.policy(policy)),
        "--max-retries", "0", *PRICE_FLAGS,
    )  # fmt: skip


def test_p4_s7_ac8_diff_mode_over_budget_when_the_fallbacks_cost_is_over_the_cap(
    pr, router, monkeypatch
):
    """A one-turn answer over the cap is returned (P4-S6 AC6), so its cost is reported over it."""
    transport = fake(answer(**SEVENTY_CENTS))

    done = pr_diff_shadow(pr, router, monkeypatch, transport)

    shadow = load_shadow(done.out)
    assert shadow["outcome"] == "reviewed", done.stderr
    assert shadow["report"]["mode"] == "diff"
    assert len(transport.sent) == 1
    assert shadow["over_budget"] is True


def test_p4_s7_ac8_diff_mode_not_over_budget_when_the_fallback_is_within_the_cap(
    pr, router, monkeypatch
):
    transport = fake(answer(**HALF_DOLLAR))

    done = pr_diff_shadow(pr, router, monkeypatch, transport)

    shadow = load_shadow(done.out)
    assert shadow["outcome"] == "reviewed", done.stderr
    assert shadow["over_budget"] is False


def test_p4_s7_ac8_diff_mode_without_a_fallback_is_judged_as_today(pr, router, monkeypatch):
    """No fallback: diff mode is judged on tokens only, so a $0.70 review isn't over budget."""
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    transport = fake(answer(**SEVENTY_CENTS))
    use_fake_transport(monkeypatch, transport)
    policy = routing_policy((OPENAI, ("diff",)), max_cost_usd="0.60")

    done = pr.run(
        "shadow", "--provider", "openai", "--model", "m1", "--policy",
        str(router.policy(policy)), "--max-retries", "0", *PRICE_FLAGS,
    )  # fmt: skip

    shadow = load_shadow(done.out)
    assert shadow["outcome"] == "reviewed", done.stderr
    assert len(transport.sent) == 1
    assert shadow["over_budget"] is False


def test_p4_s7_ac8_openai_fallback_under_a_cap_needs_both_prices(router):
    """Notes: an `openai` fallback needs both prices under a cost cap, or the run exits 2."""
    policy = routing_policy(
        (CLAUDE, ("diff",)), (OPENAI, ("diff",)), fallback=OPENAI, max_cost_usd="0.60"
    )
    claude = claude_answers()
    transport = fake(answer())

    done = router.review(policy, claude=claude, transport=transport)

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert "--price-" in done.stderr
    assert claude.calls == []
    assert transport.sent == []


def test_p4_s7_ac8_missing_prices_message_names_the_fallback_route(router):
    """The run's provider is Claude, so the message names the fallback, not `--provider openai`."""
    policy = routing_policy(
        (CLAUDE, ("diff",)), (OPENAI, ("diff",)), fallback=OPENAI, max_cost_usd="0.60"
    )

    done = router.review(policy, claude=claude_answers(), transport=fake(answer()))

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert OPENAI in done.stderr
    assert "--provider openai" not in done.stderr


# --- Notes: a fallback without its key is skipped, not refused ------------------------------------


def test_p4_s7_notes_fallback_without_its_key_leaves_the_primary_serving(router):
    claude = claude_answers()

    done = router.review(AC1_POLICY, claude=claude, key=None)

    assert done.exit_code == 0, done.stderr
    assert len(claude.calls) == 1
    [attempt] = done.route
    assert (attempt["route"], attempt["category"]) == (CLAUDE, None)


def test_p4_s7_notes_fallback_without_its_key_is_skipped_no_credential(router):
    transport = fake(answer())

    done = router.review(
        AC1_POLICY, claude=claude_fails(ErrorCategory.UNAVAILABLE), transport=transport, key=None
    )

    done.fails_as(ErrorCategory.UNAVAILABLE)
    assert transport.sent == []
    primary, fallback = done.attempts
    assert primary == {"route": CLAUDE, "category": "unavailable", "cost_usd": None}
    assert fallback == {"route": OPENAI, "skipped": "no_credential"}


# --- AC9: in CI, a missing primary key goes straight to the fallback ------------------------------


def ci_run(  # noqa: PLR0913 - one keyword per thing a test varies
    pr: PrCommands,
    router: Router,
    monkeypatch: pytest.MonkeyPatch,
    *,
    claude: FakeSdk,
    transport: FakeTransport,
    claude_key: bool,
    openai_key: bool,
    policy: str = AC1_POLICY,
) -> Any:
    monkeypatch.setenv("CI", "true")
    for name, present in ((CLAUDE_KEY_VARIABLE, claude_key), (KEY_VARIABLE, openai_key)):
        if present:
            monkeypatch.setenv(name, "canary-key-for-" + name.lower())
        else:
            monkeypatch.delenv(name, raising=False)
    use_fake_claude(monkeypatch, claude)
    use_fake_transport(monkeypatch, transport)
    return pr.run(
        "run", "--provider", "claude", "--policy", str(router.policy(policy)),
        "--max-retries", "0",
    )  # fmt: skip


def test_p4_s7_ac9_no_claude_key_in_ci_goes_straight_to_the_fallback(pr, router, monkeypatch):
    claude = claude_answers()
    transport = fake(answer())

    done = ci_run(
        pr, router, monkeypatch, claude=claude, transport=transport,
        claude_key=False, openai_key=True,
    )  # fmt: skip

    shadow = load_shadow(done.out)
    assert shadow["outcome"] == "reviewed", done.stderr
    assert claude.calls == []
    assert len(transport.sent) == 1


def test_p4_s7_ac9_audit_records_the_skipped_primary_then_the_fallback(pr, router, monkeypatch):
    done = ci_run(
        pr, router, monkeypatch, claude=claude_answers(), transport=fake(answer()),
        claude_key=False, openai_key=True,
    )  # fmt: skip

    skipped, served = attempt_records(read_records(done.audit_path))
    assert skipped == {"route": CLAUDE, "skipped": "no_credential"}
    assert set(served) == ATTEMPT_KEYS, served
    assert (served["route"], served["category"]) == (OPENAI, None)


def test_p4_s7_ac9_report_route_holds_both_attempts(pr, router, monkeypatch):
    done = ci_run(
        pr, router, monkeypatch, claude=claude_answers(), transport=fake(answer()),
        claude_key=False, openai_key=True,
    )  # fmt: skip

    skipped, served = load_shadow(done.out)["report"]["route"]
    assert skipped == {"route": CLAUDE, "skipped": "no_credential"}
    assert set(served) == ATTEMPT_KEYS, served
    assert (served["route"], served["category"]) == (OPENAI, None)


def test_p4_s7_ac9_neither_key_in_ci_is_skipped_no_credential(pr, router, monkeypatch):
    claude = claude_answers()
    transport = fake(answer())

    done = ci_run(
        pr, router, monkeypatch, claude=claude, transport=transport,
        claude_key=False, openai_key=False,
    )  # fmt: skip

    shadow = load_shadow(done.out)
    assert (shadow["outcome"], shadow["reason"]) == ("skipped", "no_credential")
    assert claude.calls == []
    assert transport.sent == []


def test_p4_s7_ac9_no_claude_key_and_no_fallback_is_skipped_as_today(pr, router, monkeypatch):
    transport = fake(answer())

    done = ci_run(
        pr, router, monkeypatch, claude=claude_answers(), transport=transport,
        claude_key=False, openai_key=True, policy=NO_ROUTING_POLICY,
    )  # fmt: skip

    shadow = load_shadow(done.out)
    assert (shadow["outcome"], shadow["reason"]) == ("skipped", "no_credential")
    assert transport.sent == []


def test_p4_s7_ac9_claude_key_in_ci_uses_the_primary(pr, router, monkeypatch):
    claude = claude_answers()
    transport = fake(answer())

    done = ci_run(
        pr, router, monkeypatch, claude=claude, transport=transport,
        claude_key=True, openai_key=True,
    )  # fmt: skip

    assert load_shadow(done.out)["outcome"] == "reviewed", done.stderr
    assert len(claude.calls) == 1
    assert transport.sent == []


# --- AC10: live commands never route --------------------------------------------------------------


def test_p4_s7_ac10_evals_live_calls_only_the_primary(capsys, tmp_path, router, monkeypatch):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    transport = fake(answer())
    use_fake_transport(monkeypatch, transport)
    sdk = claude_fails(ErrorCategory.UNAVAILABLE)
    root = copy_cases(tmp_path, "sample")

    live = run_evals(
        capsys, *live_args(root, runs=2, max_calls=2), "--max-usd", "0.20",
        "--max-cost-usd", "0.10", "--policy", router.policy(AC1_POLICY), sdk=sdk,
    )  # fmt: skip

    assert live.exit_code != USAGE_EXIT, live.stderr
    assert len(sdk.calls) == 2
    assert transport.sent == []


def test_p4_s7_ac10_evals_live_budget_is_planned_calls_times_one_cap(
    capsys, tmp_path, router, monkeypatch
):
    """2 calls x $0.10 = $0.20 is enough: a fallback would need twice that."""
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    use_fake_transport(monkeypatch, fake(answer()))
    sdk = claude_answers()
    root = copy_cases(tmp_path, "sample")

    live = run_evals(
        capsys, *live_args(root, runs=2, max_calls=2), "--max-usd", "0.20",
        "--max-cost-usd", "0.10", "--policy", router.policy(AC1_POLICY), sdk=sdk,
    )  # fmt: skip

    assert live.exit_code != USAGE_EXIT, live.stderr
    assert len(sdk.calls) == 2


@pytest.fixture
def history(tmp_path: Path) -> History:
    return History(tmp_path / "history")


@pytest.fixture
def backfill(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> Backfiller:
    return Backfiller(capsys, tmp_path)


def test_p4_s7_ac10_backfill_calls_only_the_primary(backfill, history, router, monkeypatch):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    transport = fake(answer())
    use_fake_transport(monkeypatch, transport)
    claude = claude_fails(ErrorCategory.UNAVAILABLE)
    use_fake_claude(monkeypatch, claude)

    run = backfill.run(
        history.merged_many(41, 42), history.repo.path, "--policy", str(router.policy(AC1_POLICY)),
        max_calls=2, provider_name="claude", dollar_flags=TWO_CALLS_BUDGET,
    )  # fmt: skip

    assert run.exit_code != USAGE_EXIT, run.stderr
    assert len(claude.calls) == 2
    assert transport.sent == []


def test_p4_s7_ac10_backfill_budget_is_planned_calls_times_one_cap(
    backfill, history, router, monkeypatch
):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    use_fake_transport(monkeypatch, fake(answer()))
    claude = claude_answers()
    use_fake_claude(monkeypatch, claude)

    run = backfill.run(
        history.merged_many(43, 44), history.repo.path, "--policy", str(router.policy(AC1_POLICY)),
        max_calls=2, provider_name="claude", dollar_flags=TWO_CALLS_BUDGET,
    )  # fmt: skip

    assert run.exit_code == 0, run.stderr
    assert len(claude.calls) == 2


def test_p4_s7_ac10_backfill_reports_record_one_attempt_each(
    backfill, history, router, monkeypatch
):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    use_fake_transport(monkeypatch, fake(answer()))
    use_fake_claude(monkeypatch, claude_answers())
    pulls = history.merged_many(45)

    run = backfill.run(
        pulls, history.repo.path, "--policy", str(router.policy(AC1_POLICY)),
        max_calls=1, provider_name="claude", dollar_flags=WITHIN_BUDGET,
    )  # fmt: skip

    assert run.exit_code == 0, run.stderr
    [attempt] = run.shadow(45)["report"]["route"]
    assert attempt["route"] == CLAUDE


# --- AC11: the deadline covers the whole review ---------------------------------------------------


DEADLINE_S = 6.0
PRIMARY_FAILS_AT_S = 4.0
LEFT_S = DEADLINE_S - PRIMARY_FAILS_AT_S


def slow_transient_claude(after_s: float) -> FakeSdk:
    return FakeSdk(pause(after_s), CLIConnectionError("Could not connect."))


def test_p4_s7_ac11_fallback_runs_with_only_the_time_left(router):
    transport = fake(answer(FINDING_FIELDS))

    done = router.review(
        AC1_POLICY, "--deadline-s", str(DEADLINE_S),
        claude=slow_transient_claude(PRIMARY_FAILS_AT_S), transport=transport,
    )  # fmt: skip

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    # The fallback's POST may wait no longer than what is left of the review's deadline.
    assert 0 < transport.one.timeout_s <= LEFT_S + 0.5


def test_p4_s7_ac11_whole_review_fails_transient_at_the_deadline(router):
    transport = fake(hang_forever())

    done = router.review(
        AC1_POLICY, "--deadline-s", str(DEADLINE_S),
        claude=slow_transient_claude(PRIMARY_FAILS_AT_S), transport=transport,
    )  # fmt: skip

    done.fails_as(ErrorCategory.TRANSIENT)
    assert len(transport.sent) == 1
    # A fresh deadline for the fallback would end at 4 + 6 = 10 s.
    assert DEADLINE_S - 0.5 <= done.elapsed_s < PRIMARY_FAILS_AT_S + DEADLINE_S - 1.0
    assert [record["route"] for record in done.attempts] == [CLAUDE, OPENAI]


def test_p4_s7_ac11_no_time_left_skips_the_fallback_as_deadline(router):
    """The primary uses the whole deadline, so it fails `transient` with nothing left."""
    claude = FakeSdk(pause(BOUND_S))
    transport = fake(answer())

    done = router.review(AC1_POLICY, "--deadline-s", "1.5", claude=claude, transport=transport)

    done.fails_as(ErrorCategory.TRANSIENT)
    assert done.elapsed_s < BOUND_S
    assert transport.sent == []
    primary, fallback = done.attempts
    assert primary == {"route": CLAUDE, "category": "transient", "cost_usd": None}
    assert fallback == {"route": OPENAI, "skipped": "deadline"}


def test_p4_s7_ac11_real_transport_fallback_ends_at_the_reviews_deadline(router, server):
    """Over the real `UrllibTransport`, whose socket waits in a thread the review can't cancel.

    The server answers after 9 s. A fallback that keeps a whole deadline of its own waits on the
    socket until 4 + 6 = 10 s, and the run can't end before that thread does.
    """
    server.reply = Reply(body=b"{}", delay_s=9)
    policy = routing_policy(
        (CLAUDE, ("diff",)), (OPENAI, ("diff",)), fallback=OPENAI, base_url=server.base
    )

    done = router.review(
        policy, "--deadline-s", str(DEADLINE_S), claude=slow_transient_claude(PRIMARY_FAILS_AT_S),
        key=None, real_transport=True,
    )  # fmt: skip

    done.fails_as(ErrorCategory.TRANSIENT)
    assert server.paths == ["/v1/chat/completions"]
    assert done.elapsed_s < PRIMARY_FAILS_AT_S + DEADLINE_S - 1.0
