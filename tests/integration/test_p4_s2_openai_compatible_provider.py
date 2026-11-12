"""P4-S2: review with a second, OpenAI-compatible provider.

Written from the spec's criteria, Limits and Interfaces, Phase 4 design.md (Interfaces, "Second
provider and routing") and ADRs 0002 and 0020, in parallel with the developer and before their
code was read. AC5 lives in `tests/conformance` (the `openai` factory).

Every command runs in process through its public entry point, as the earlier suites drive it:
`review_bot.cli.main`, `review_bot.pr.cli.main` and `review_bot.evals.main`. Nothing injects a
provider for `--provider openai`: the command builds its own, and `fake_openai.use_fake_transport`
makes it send through a scripted `HttpTransport`. The last section runs the real `UrllibTransport`
against an HTTP server on 127.0.0.1, so redirects, the 4 MB cap and a refused connection are seen
as the standard library really produces them. Nothing reaches a model or the external network.

Readings, each used in one place:
- "One warning naming the retry" is one stderr line holding "warning" that mentions a retry.
- "Fails at once" (a `Retry-After` over 30 s) is one request, and the run ending well inside the
  wait the server asked for.
- "4 MB" is read as 4 MiB or 4,000,000 bytes: bodies of 3,900,000 bytes are accepted and of
  4 MiB + 1 refused.
- An empty `OPENAI_API_KEY` is treated as unset (AC7): an empty bearer token can't be meant.
"""

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from diff_support import FINDING_DIFF, FINDING_FIELDS, FINDING_LINE
from fake_openai import (
    COMPLETIONS,
    KEY,
    MAX_BODY_BYTES,
    SK_KEY,
    FakeTransport,
    api_error,
    completion,
    completion_body,
    connection_failure,
    fake,
    findings_content,
    hang_forever,
    http,
    use_fake_transport,
)
from fake_sdk import FakeSdk, result_message
from test_p1_s4_audit_log import read_records
from test_p2_s2_review_pr_event import event
from test_p3_s8_pr_review_with_context import CHECKOUT, CHECKOUT_AT_C, make_context_pull
from test_p4_s1_policy_file import EvalSdk, copy_cases, live_args, policy_toml, run_evals

from review_bot import cli
from review_bot.errors import ErrorCategory
from review_bot.pr import cli as pr_cli
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

KEY_VARIABLE = "OPENAI_API_KEY"
BASE_VARIABLE = "REVIEW_BOT_OPENAI_BASE_URL"
USAGE_EXIT = 2
FINDINGS_EXIT = 1
EXIT_CODES = {
    ErrorCategory.UNAVAILABLE: 7,
    ErrorCategory.AUTH: 8,
    ErrorCategory.RATE_LIMIT: 9,
    ErrorCategory.REFUSAL: 10,
    ErrorCategory.TRANSIENT: 11,
    ErrorCategory.PERMANENT: 12,
    ErrorCategory.INVALID_OUTPUT: 13,
}
COST_REFUSAL = "--max-cost-usd isn't supported with --provider openai yet"
PROSE_CANARY = "UPSTREAM-PROSE-CANARY-51c2"
BODY_CANARY = "UPSTREAM-BODY-CANARY-8d0e"
# Generous: they catch a hang or an honoured long wait, not a slow CI machine.
QUICK_S = 10.0


# --- Running the commands -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Done:
    exit_code: int
    stdout: str
    stderr: str
    audit_path: Path

    @property
    def records(self) -> list[dict[str, Any]]:
        return read_records(self.audit_path) if self.audit_path.exists() else []

    @property
    def audit_text(self) -> str:
        return self.audit_path.read_text(encoding="ascii") if self.audit_path.exists() else ""

    @property
    def warnings(self) -> list[str]:
        return [line for line in self.stderr.splitlines() if "warning" in line.lower()]

    @property
    def report(self) -> dict[str, Any]:
        document = json.loads(self.stdout)
        assert document["schema"] == "report/v1"
        return document

    def record(self, name: str) -> dict[str, Any]:
        found = [r["data"] for r in self.records if f"{r['stage']}.{r['event']}" == name]
        assert len(found) == 1, (name, self.records)
        return found[0]

    def fails_as(self, category: ErrorCategory) -> None:
        assert self.exit_code == EXIT_CODES[category], self.stderr
        assert f"{category}:" in self.stderr, self.stderr


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key, no base URL, no cost cap and no proxy from the machine running the tests."""
    for name in (KEY_VARIABLE, BASE_VARIABLE, "REVIEW_BOT_MAX_COST_USD", "REVIEW_BOT_POLICY", "CI"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")


class OpenAIBot:
    def __init__(self, bot: Any, write_diff: Callable[..., Path], tmp_path: Path, monkeypatch):
        self._bot = bot
        self._write_diff = write_diff
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch
        self.runs = 0

    def diff(self, content: str = FINDING_DIFF) -> Path:
        return self._write_diff(content)

    def review(
        self,
        transport: FakeTransport | None,
        *flags: str,
        key: str | None = KEY,
        diff: str = FINDING_DIFF,
        provider_flags: tuple[str, ...] = ("--provider", "openai", "--model", "m1"),
    ) -> Done:
        """`review-bot DIFF --provider openai --model m1 --audit-log LOG FLAGS`."""
        if key is None:
            self._monkeypatch.delenv(KEY_VARIABLE, raising=False)
        else:
            self._monkeypatch.setenv(KEY_VARIABLE, key)
        if transport is not None:
            use_fake_transport(self._monkeypatch, transport)
        self.runs += 1
        audit = self._tmp_path / f"audit-{self.runs}.jsonl"
        path = self.diff(diff)
        run = self._bot.run(str(path), *provider_flags, "--audit-log", str(audit), *flags)
        return Done(run.exit_code, run.stdout, run.stderr, audit)


@pytest.fixture
def openai(bot, write_diff, tmp_path, monkeypatch) -> OpenAIBot:
    return OpenAIBot(bot, write_diff, tmp_path, monkeypatch)


@dataclass(frozen=True, slots=True)
class PrDone:
    exit_code: int
    stdout: str
    stderr: str
    out: Path
    audit_path: Path

    @property
    def audit_text(self) -> str:
        return self.audit_path.read_text(encoding="ascii") if self.audit_path.exists() else ""


class PrCommands:
    """`pr shadow DIFF` and `pr run` on a pull request's event, each with its own out and log."""

    def __init__(self, capsys: Any, tmp_path: Path, write_diff: Callable[..., Path]) -> None:
        self._capsys = capsys
        self._tmp_path = tmp_path
        self._write_diff = write_diff
        self._runs = 0

    def run(self, subcommand: str, *flags: str) -> PrDone:
        self._runs += 1
        out = self._tmp_path / f"pr-out-{self._runs}"
        audit = out / "audit.jsonl"
        if subcommand == "shadow":
            target = [str(self._write_diff(FINDING_DIFF))]
        else:
            pull, _ = make_context_pull(
                self._tmp_path / f"clone-{self._runs}", {CHECKOUT: CHECKOUT_AT_C}
            )
            event_path = self._tmp_path / f"event-{self._runs}.json"
            event_path.write_text(json.dumps(event(pull.base, pull.head)), encoding="utf-8")
            target = ["--event", str(event_path), "--repo", str(pull.repo.path)]
        self._capsys.readouterr()
        exit_code = pr_cli.main([subcommand, *target, "--out", str(out), *flags])
        captured = self._capsys.readouterr()
        return PrDone(exit_code, captured.out, captured.err, out, audit)


@pytest.fixture
def pr(capsys, tmp_path, write_diff, monkeypatch) -> PrCommands:
    monkeypatch.delenv("REVIEW_BOT_AUDIT_LOG", raising=False)  # so each log lands in its --out
    return PrCommands(capsys, tmp_path, write_diff)


def no_findings() -> Any:
    return completion(findings_content())


def one_finding() -> Any:
    return completion(findings_content(FINDING_FIELDS))


def timed(action: Callable[[], Done]) -> tuple[Done, float]:
    started = time.monotonic()
    done = action()
    return done, time.monotonic() - started


# --- AC1: one POST, the prompts byte for byte, the cap's field by host ----------------------------


def test_p4_s2_ac1_one_post_to_the_default_base_with_the_bearer_key(openai):
    transport = fake(no_findings())

    done = openai.review(transport)

    assert done.exit_code == 0, done.stderr
    sent = transport.one
    assert sent.url == COMPLETIONS
    assert sent.header("Authorization") == f"Bearer {KEY}"


def test_p4_s2_ac1_body_model_is_the_flags_model(openai):
    transport = fake(no_findings())

    openai.review(transport)

    assert transport.one.body["model"] == "m1"


def test_p4_s2_ac1_messages_are_the_system_and_user_prompts_byte_for_byte(openai, bot):
    mock_request = bot.request_for(openai.diff(), "--model", "m1")
    transport = fake(no_findings())

    openai.review(transport)

    messages = transport.one.body["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[0]["content"] == mock_request.system_prompt
    assert messages[1]["content"] == mock_request.user_prompt


def test_p4_s2_ac1_prompts_hold_non_ascii_and_crlf_unchanged(openai, bot):
    diff = FINDING_DIFF.replace("+x\n", "+ café = 'ünï' # ☃\r\n", 1)
    mock_request = bot.request_for(openai.diff(diff), "--model", "m1")
    transport = fake(no_findings())

    openai.review(transport, diff=diff)

    assert transport.one.body["messages"][1]["content"] == mock_request.user_prompt


def test_p4_s2_ac1_no_output_cap_and_no_tools_without_the_flag(openai):
    transport = fake(no_findings())

    openai.review(transport)

    body = transport.one.body
    assert "max_tokens" not in body
    assert "max_completion_tokens" not in body
    assert "tools" not in body


@pytest.mark.parametrize(
    "base", ["https://api.openai.com/v1", "https://api.openai.com:443/v1"], ids=["plain", "port"]
)
def test_p4_s2_ac1_cap_is_max_completion_tokens_for_api_openai_com(openai, base):
    transport = fake(no_findings())

    done = openai.review(transport, "--max-output-tokens", "500", "--openai-base-url", base)

    assert done.exit_code == 0, done.stderr
    assert transport.one.body["max_completion_tokens"] == 500
    assert "max_tokens" not in transport.one.body


@pytest.mark.parametrize(
    "base",
    [
        "http://127.0.0.1:8000/v1",
        "https://llm.example.com/v1",
        "https://api.openai.com.evil.example/v1",
        "https://eu.api.openai.com/v1",
    ],
    ids=["loopback", "self_hosted", "lookalike_suffix", "subdomain"],
)
def test_p4_s2_ac1_cap_is_max_tokens_for_any_other_host(openai, base):
    transport = fake(no_findings())

    done = openai.review(transport, "--max-output-tokens", "500", "--openai-base-url", base)

    assert done.exit_code == 0, done.stderr
    assert transport.one.body["max_tokens"] == 500
    assert "max_completion_tokens" not in transport.one.body


def test_p4_s2_ac1_base_with_a_path_prefix_gets_chat_completions_appended(openai):
    transport = fake(no_findings())

    openai.review(transport, "--openai-base-url", "https://llm.example.com/gateway/v1")

    assert transport.one.url == "https://llm.example.com/gateway/v1/chat/completions"


def test_p4_s2_ac1_base_from_the_variable(openai, monkeypatch):
    monkeypatch.setenv(BASE_VARIABLE, "https://llm.example.com/v1")
    transport = fake(no_findings())

    done = openai.review(transport)

    assert done.exit_code == 0, done.stderr
    assert transport.one.url == "https://llm.example.com/v1/chat/completions"


# --- AC2: findings through the shared parser, usage, provider and model ---------------------------


def test_p4_s2_ac2_finding_is_reported_as_claudes_would_be(openai):
    done = openai.review(fake(one_finding()))

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert done.stdout.splitlines() == [FINDING_LINE]


def test_p4_s2_ac2_report_names_provider_openai_and_model_m1(openai):
    done = openai.review(fake(one_finding()), "--format", "json")

    report = done.report
    assert report["provider"] == "openai"
    assert report["model"] == "m1"
    assert [finding["line"] for finding in report["findings"]] == [FINDING_FIELDS["line"]]


def test_p4_s2_ac2_usage_comes_from_prompt_and_completion_tokens(openai):
    reply = completion(findings_content(), prompt_tokens=1234, completion_tokens=56)

    done = openai.review(fake(reply))

    assert done.exit_code == 0, done.stderr
    assert "input_tokens=1234 output_tokens=56" in done.stderr
    called = done.record("agent.called")
    assert (called["input_tokens"], called["output_tokens"]) == (1234, 56)


def test_p4_s2_ac2_summary_and_audit_name_openai_and_m1(openai):
    done = openai.review(fake(no_findings()))

    assert "provider=openai model=m1 " in done.stderr
    start = done.record("run.start")
    assert (start["provider"], start["model"]) == ("openai", "m1")


SHARED_PARSER_CONTENTS = {
    "one_finding": findings_content(FINDING_FIELDS),
    "empty": findings_content(),
    "bad_severity": findings_content(FINDING_FIELDS | {"severity": "catastrophic"}),
    "confidence_over_one": findings_content(FINDING_FIELDS | {"confidence": 1.5}),
    "missing_rationale": findings_content(
        {k: v for k, v in FINDING_FIELDS.items() if k != "rationale"}
    ),
    "fenced": "```json\n" + findings_content(FINDING_FIELDS) + "\n```",
    "prose": "Looks good to me.",
    "array": "[]",
    "unanchored": findings_content(FINDING_FIELDS | {"line": 999}),
}


@pytest.mark.parametrize("content", SHARED_PARSER_CONTENTS.values(), ids=SHARED_PARSER_CONTENTS)
def test_p4_s2_ac2_same_content_gives_claudes_exit_code_and_report(openai, claude_review, content):
    claude = claude_review(FakeSdk(result_message(content)))

    done = openai.review(fake(completion(content)))

    assert (done.exit_code, done.stdout) == (claude.exit_code, claude.stdout), done.stderr


# --- AC3: each status, error code and finish_reason to its category and exit code -----------------


def statuses(category: ErrorCategory, *codes: int) -> list[Any]:
    return [pytest.param(api_error(code, PROSE_CANARY), category, id=str(code)) for code in codes]


def redirect(status: int) -> Any:
    return http(status, b"", location="https://elsewhere.example/v1/chat/completions")


FAILURES = [
    *statuses(ErrorCategory.AUTH, 401, 403),
    *statuses(ErrorCategory.RATE_LIMIT, 429),
    pytest.param(
        api_error(429, PROSE_CANARY, code="insufficient_quota"),
        ErrorCategory.PERMANENT,
        id="429_insufficient_quota",
    ),
    *statuses(ErrorCategory.TRANSIENT, 500, 502, 503, 504, 529, 599),
    pytest.param(
        http(503, b"<html>Service Unavailable</html>"), ErrorCategory.TRANSIENT, id="503_html"
    ),
    pytest.param(connection_failure("timeout"), ErrorCategory.TRANSIENT, id="timeout"),
    pytest.param(connection_failure("reset"), ErrorCategory.TRANSIENT, id="reset"),
    pytest.param(connection_failure("refused"), ErrorCategory.UNAVAILABLE, id="refused"),
    pytest.param(connection_failure("dns"), ErrorCategory.UNAVAILABLE, id="dns"),
    *statuses(ErrorCategory.PERMANENT, 400, 402, 404, 405, 409, 413, 418, 422, 451, 499),
    *[
        pytest.param(redirect(code), ErrorCategory.PERMANENT, id=f"{code}_redirect")
        for code in (300, 301, 302, 303, 307, 308)
    ],
    pytest.param(
        completion(findings_content(), finish_reason="content_filter"),
        ErrorCategory.REFUSAL,
        id="content_filter",
    ),
    pytest.param(
        completion(None, refusal="I can't help with that."), ErrorCategory.REFUSAL, id="refusal"
    ),
    pytest.param(
        completion(findings_content(), refusal="I can't help with that."),
        ErrorCategory.REFUSAL,
        id="refusal_beside_findings",
    ),
    pytest.param(completion("Looks good."), ErrorCategory.INVALID_OUTPUT, id="prose"),
    pytest.param(completion(""), ErrorCategory.INVALID_OUTPUT, id="empty_content"),
    pytest.param(completion(None), ErrorCategory.INVALID_OUTPUT, id="null_content"),
    pytest.param(completion('{"other": 1}'), ErrorCategory.INVALID_OUTPUT, id="other_object"),
    pytest.param(
        completion(findings_content(), finish_reason="length"),
        ErrorCategory.INVALID_OUTPUT,
        id="length",
    ),
    pytest.param(
        completion('{"findings": [', finish_reason="length"),
        ErrorCategory.INVALID_OUTPUT,
        id="length_cut",
    ),
]


@pytest.mark.parametrize(("reply", "category"), FAILURES)
def test_p4_s2_ac3_response_fails_with_its_category_and_exit_code(openai, reply, category):
    transport = fake(reply)

    done = openai.review(transport, "--max-retries", "0")

    done.fails_as(category)
    assert len(transport.sent) == 1


@pytest.mark.parametrize(("reply", "category"), FAILURES)
def test_p4_s2_ac3_audit_records_the_failure_category(openai, reply, category):
    done = openai.review(fake(reply), "--max-retries", "0")

    assert done.record("agent.failed")["category"] == category
    assert done.record("run.end")["exit_code"] == EXIT_CODES[category]


def test_p4_s2_ac3_redirect_is_never_followed(openai):
    transport = fake(redirect(302), no_findings())

    done = openai.review(transport, "--max-retries", "2")

    done.fails_as(ErrorCategory.PERMANENT)
    assert [sent.url for sent in transport.sent] == [COMPLETIONS]


def test_p4_s2_ac3_quota_error_with_a_retry_after_is_still_permanent(openai):
    transport = fake(api_error(429, code="insufficient_quota", retry_after="0"), no_findings())

    done = openai.review(transport, "--max-retries", "2")

    done.fails_as(ErrorCategory.PERMANENT)
    assert len(transport.sent) == 1


def test_p4_s2_ac3_empty_refusal_beside_findings_is_not_a_refusal(openai):
    done = openai.review(fake(completion(findings_content(), refusal="")))

    assert done.exit_code == 0, done.stderr


# Limits: the body is untrusted.

MALFORMED_BODIES = {
    "not_json": b"not json at all",
    "html": b"<html><body>gateway</body></html>",
    "json_array": b"[]",
    "json_null": b"null",
    "json_string": b'"findings"',
    "no_choices": json.dumps({"id": "x", "usage": {}}).encode(),
    "empty_choices": json.dumps({"choices": []}).encode(),
    "choices_not_a_list": json.dumps({"choices": {"0": {}}}).encode(),
    "choice_without_message": json.dumps({"choices": [{"finish_reason": "stop"}]}).encode(),
    "message_not_an_object": json.dumps({"choices": [{"message": "hi"}]}).encode(),
    "content_not_a_string": json.dumps(
        {"choices": [{"message": {"content": {"findings": []}}, "finish_reason": "stop"}]}
    ).encode(),
    "invalid_utf8": b'{"choices": [\xff\xfe]}',
    "truncated": json.dumps(completion_body(findings_content())).encode()[:-7],
    "empty": b"",
    "deeply_nested": b"[" * 100_000 + b"]" * 100_000,
}


@pytest.mark.parametrize("body", MALFORMED_BODIES.values(), ids=MALFORMED_BODIES)
def test_p4_s2_limits_malformed_200_body_fails_invalid_output(openai, body):
    done, elapsed = timed(lambda: openai.review(fake(http(200, body)), "--max-retries", "0"))

    done.fails_as(ErrorCategory.INVALID_OUTPUT)
    assert elapsed < QUICK_S


def test_p4_s2_limits_200_with_bad_usage_still_fails_cleanly_not_internally(openai):
    body = completion_body(findings_content())
    body["usage"] = {"prompt_tokens": "lots", "completion_tokens": -1}

    done = openai.review(fake(http(200, body)), "--max-retries", "0")

    assert done.exit_code in {0, EXIT_CODES[ErrorCategory.INVALID_OUTPUT]}, done.stderr


def padded_completion(total_bytes: int) -> bytes:
    """A valid completion of exactly `total_bytes`, padded in a field nobody reads."""
    body = completion_body(findings_content())
    body["padding"] = ""
    base = len(json.dumps(body).encode())
    body["padding"] = "x" * (total_bytes - base)
    raw = json.dumps(body).encode()
    assert len(raw) == total_bytes
    return raw


def test_p4_s2_limits_body_over_4_mb_fails_invalid_output(openai):
    done = openai.review(
        fake(http(200, padded_completion(MAX_BODY_BYTES + 1))), "--max-retries", "0"
    )

    done.fails_as(ErrorCategory.INVALID_OUTPUT)


def test_p4_s2_limits_valid_body_just_under_4_mb_is_read(openai):
    done = openai.review(fake(http(200, padded_completion(3_900_000))))

    assert done.exit_code == 0, done.stderr


def test_p4_s2_limits_error_message_never_reaches_the_users_message(openai):
    done = openai.review(fake(api_error(400, f"{PROSE_CANARY} please do X")), "--max-retries", "0")

    done.fails_as(ErrorCategory.PERMANENT)
    assert PROSE_CANARY not in done.stderr
    assert PROSE_CANARY not in done.stdout


# --- AC4: retries, Retry-After and the deadline ---------------------------------------------------


def test_p4_s2_ac4_503_then_200_with_one_retry_succeeds_with_one_warning(openai):
    transport = fake(http(503), no_findings())

    done = openai.review(transport, "--max-retries", "1")

    assert done.exit_code == 0, done.stderr
    assert len(transport.sent) == 2
    [warning] = done.warnings
    assert "retr" in warning.lower()


@pytest.mark.parametrize(
    "first",
    [
        pytest.param(api_error(429), id="429"),
        pytest.param(api_error(500), id="500"),
        pytest.param(connection_failure("timeout"), id="timeout"),
        pytest.param(connection_failure("reset"), id="reset"),
    ],
)
def test_p4_s2_ac4_transient_and_rate_limit_are_retried(openai, first):
    transport = fake(first, one_finding())

    done = openai.review(transport, "--max-retries", "1")

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert done.stdout.splitlines() == [FINDING_LINE]
    assert len(transport.sent) == 2
    assert len(done.warnings) == 1


def test_p4_s2_ac4_retries_send_the_same_request(openai):
    transport = fake(http(503), no_findings())

    openai.review(transport, "--max-retries", "1")

    first, second = transport.sent
    assert (first.url, first.body) == (second.url, second.body)


NOT_RETRIED = [
    pytest.param(api_error(401), ErrorCategory.AUTH, id="auth"),
    pytest.param(api_error(400), ErrorCategory.PERMANENT, id="permanent"),
    pytest.param(api_error(429, code="insufficient_quota"), ErrorCategory.PERMANENT, id="quota"),
    pytest.param(redirect(307), ErrorCategory.PERMANENT, id="redirect"),
    pytest.param(
        completion(None, finish_reason="content_filter"), ErrorCategory.REFUSAL, id="refusal"
    ),
    pytest.param(completion("prose"), ErrorCategory.INVALID_OUTPUT, id="invalid_output"),
    pytest.param(
        completion(findings_content(), finish_reason="length"),
        ErrorCategory.INVALID_OUTPUT,
        id="length",
    ),
    pytest.param(connection_failure("refused"), ErrorCategory.UNAVAILABLE, id="refused"),
    pytest.param(connection_failure("dns"), ErrorCategory.UNAVAILABLE, id="dns"),
]


@pytest.mark.parametrize(("first", "category"), NOT_RETRIED)
def test_p4_s2_ac4_other_categories_are_never_retried(openai, first, category):
    transport = fake(first, no_findings())

    done = openai.review(transport, "--max-retries", "2")

    done.fails_as(category)
    assert len(transport.sent) == 1


def test_p4_s2_ac4_no_retry_with_max_retries_0(openai):
    transport = fake(http(503), no_findings())

    done = openai.review(transport, "--max-retries", "0")

    done.fails_as(ErrorCategory.TRANSIENT)
    assert len(transport.sent) == 1


def test_p4_s2_ac4_retries_stop_at_max_retries(openai):
    transport = fake(http(503, retry_after="0"))

    done = openai.review(transport, "--max-retries", "2")

    done.fails_as(ErrorCategory.TRANSIENT)
    assert len(transport.sent) == 3


def test_p4_s2_ac4_rate_limit_after_its_retries_is_rate_limit(openai):
    transport = fake(api_error(429, retry_after="0"))

    done = openai.review(transport, "--max-retries", "1")

    done.fails_as(ErrorCategory.RATE_LIMIT)
    assert len(transport.sent) == 2


def test_p4_s2_ac4_retry_after_within_30_s_is_honoured(openai):
    transport = fake(api_error(429, retry_after="1"), no_findings())

    done, elapsed = timed(lambda: openai.review(transport, "--max-retries", "1"))

    assert done.exit_code == 0, done.stderr
    assert len(transport.sent) == 2
    assert elapsed >= 0.95


@pytest.mark.parametrize(
    ("status", "category"),
    [(429, ErrorCategory.RATE_LIMIT), (503, ErrorCategory.TRANSIENT)],
    ids=["429", "503"],
)
@pytest.mark.parametrize(
    "retry_after",
    ["31", "3600", formatdate(time.time() + 3600, usegmt=True)],
    ids=["31_s", "an_hour", "http_date_an_hour_ahead"],
)
def test_p4_s2_ac4_retry_after_over_30_s_fails_at_once(openai, status, category, retry_after):
    transport = fake(api_error(status, retry_after=retry_after), no_findings())

    done, elapsed = timed(lambda: openai.review(transport, "--max-retries", "2"))

    done.fails_as(category)
    assert len(transport.sent) == 1
    assert elapsed < QUICK_S


@pytest.mark.parametrize("retry_after", ["soon", "-5", "1e9", "", "NaN"])
def test_p4_s2_ac4_unreadable_retry_after_never_hangs(openai, retry_after):
    transport = fake(api_error(503, retry_after=retry_after), no_findings())

    done, elapsed = timed(
        lambda: openai.review(transport, "--max-retries", "1", "--deadline-s", "60")
    )

    assert done.exit_code in {0, EXIT_CODES[ErrorCategory.TRANSIENT]}, done.stderr
    assert elapsed < 40


def test_p4_s2_ac4_deadline_bounds_a_call_that_never_answers(openai):
    transport = fake(hang_forever())

    done, elapsed = timed(lambda: openai.review(transport, "--deadline-s", "1"))

    done.fails_as(ErrorCategory.TRANSIENT)
    assert elapsed < QUICK_S


def test_p4_s2_ac4_no_retry_waits_past_the_deadline(openai):
    transport = fake(http(503, retry_after="20"), no_findings())

    done, elapsed = timed(
        lambda: openai.review(transport, "--max-retries", "3", "--deadline-s", "2")
    )

    done.fails_as(ErrorCategory.TRANSIENT)
    assert elapsed < QUICK_S
    assert len(transport.sent) == 1


def test_p4_s2_ac4_deadline_bounds_the_retries_together(openai):
    transport = fake(http(503, retry_after="1"))

    done, elapsed = timed(
        lambda: openai.review(transport, "--max-retries", "20", "--deadline-s", "2.5")
    )

    done.fails_as(ErrorCategory.TRANSIENT)
    assert elapsed < 2.5 + 3
    assert len(transport.sent) <= 4


def test_p4_s2_ac4_transport_timeout_is_within_the_deadline(openai):
    transport = fake(no_findings())

    openai.review(transport, "--deadline-s", "5")

    assert 0 < transport.one.timeout_s <= 5


# --- AC6: the base URL ----------------------------------------------------------------------------

REFUSED_BASES = {
    "http_remote": "http://api.openai.com/v1",
    "http_lookalike_loopback": "http://127.0.0.1.evil.example/v1",
    "http_localhost_subdomain": "http://localhost.evil.example/v1",
    "userinfo": "https://user:secret@llm.example.com/v1",
    "userinfo_name_only": "https://user@llm.example.com/v1",
    "loopback_userinfo": "http://user:pw@127.0.0.1:8000/v1",
    "query": "https://llm.example.com/v1?api-version=1",
    "fragment": "https://llm.example.com/v1#top",
    "loopback_query": "http://localhost/v1?x=1",
    "ftp": "ftp://llm.example.com/v1",
    "no_scheme": "llm.example.com/v1",
}


@pytest.mark.parametrize("base", REFUSED_BASES.values(), ids=REFUSED_BASES)
def test_p4_s2_ac6_refused_base_flag_exits_2_naming_it_before_any_request(openai, base):
    transport = fake(no_findings())

    done = openai.review(transport, "--openai-base-url", base)

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert "--openai-base-url" in done.stderr
    assert transport.sent == []


@pytest.mark.parametrize("base", REFUSED_BASES.values(), ids=REFUSED_BASES)
def test_p4_s2_ac6_refused_base_variable_exits_2_naming_it(openai, monkeypatch, base):
    monkeypatch.setenv(BASE_VARIABLE, base)
    transport = fake(no_findings())

    done = openai.review(transport)

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert BASE_VARIABLE in done.stderr
    assert transport.sent == []


def test_p4_s2_ac6_refusal_never_shows_the_userinfos_password(openai):
    done = openai.review(
        fake(no_findings()), "--openai-base-url", "https://u:pw-canary-77@h.example/v1"
    )

    assert done.exit_code == USAGE_EXIT
    assert "pw-canary-77" not in done.stderr


@pytest.mark.parametrize(
    "base", ["http://127.0.0.1:8000/v1", "http://localhost/v1", "http://[::1]/v1"]
)
def test_p4_s2_ac6_http_on_loopback_is_accepted(openai, base):
    transport = fake(no_findings())

    done = openai.review(transport, "--openai-base-url", base)

    assert done.exit_code == 0, done.stderr
    assert transport.one.url == f"{base}/chat/completions"


# --- AC7: the key ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_flags",
    [(), ("--openai-base-url", "https://llm.example.com/v1")],
    ids=["default_base", "remote_base"],
)
@pytest.mark.parametrize("key", [None, ""], ids=["unset", "empty"])
def test_p4_s2_ac7_no_key_with_a_remote_base_exits_2_naming_it_before_any_request(
    openai, base_flags, key
):
    transport = fake(no_findings())

    done = openai.review(transport, *base_flags, key=key)

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert KEY_VARIABLE in done.stderr
    assert transport.sent == []


@pytest.mark.parametrize(
    "base", ["http://127.0.0.1:8000/v1", "http://localhost/v1", "http://[::1]/v1"]
)
def test_p4_s2_ac7_no_key_with_a_loopback_base_sends_no_authorization(openai, base):
    transport = fake(no_findings())

    done = openai.review(transport, "--openai-base-url", base, key=None)

    assert done.exit_code == 0, done.stderr
    assert transport.one.header("Authorization") is None


def test_p4_s2_ac7_mock_run_needs_no_openai_key(openai):
    done = openai.review(None, provider_flags=("--provider", "mock"), key=None)

    assert done.exit_code == 0, done.stderr


# --- AC8: the key leaves the environment first, and never reaches any output ----------------------


def assert_key_gone() -> None:
    assert KEY_VARIABLE not in os.environ


@pytest.mark.parametrize("provider_name", ["openai", "claude", "mock"])
def test_p4_s2_ac8_review_bot_removes_the_key_before_building_a_provider(
    bot, write_diff, monkeypatch, provider_name
):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    seen: list[bool] = []

    def build(*_args: Any, **_kwargs: Any) -> Any:
        seen.append(KEY_VARIABLE in os.environ)
        return MockProvider(findings=[])

    monkeypatch.setattr(cli, "build_provider", build)

    bot.run(str(write_diff(FINDING_DIFF)), "--provider", provider_name, "--model", "m1")

    assert seen == [False]
    assert_key_gone()


def test_p4_s2_ac8_claude_subprocess_never_holds_the_key(claude_review, monkeypatch):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    seen: list[bool] = []

    async def record_environment(options: Any) -> None:
        seen.append(KEY_VARIABLE in os.environ)
        seen.append(KEY_VARIABLE in (options.env or {}))

    sdk = FakeSdk(record_environment, result_message(findings_content()))

    done = claude_review(sdk)

    assert done.exit_code == 0, done.stderr
    assert seen == [False, False]


@pytest.mark.parametrize("subcommand", ["shadow", "run"])
def test_p4_s2_ac8_pr_removes_the_key_before_building_a_provider(pr, monkeypatch, subcommand):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    seen: list[bool] = []

    def build(*_args: Any, **_kwargs: Any) -> Any:
        seen.append(KEY_VARIABLE in os.environ)
        return MockProvider(findings=[])

    monkeypatch.setattr(pr_cli, "build_provider", build)
    monkeypatch.setattr(cli, "build_provider", build)

    done = pr.run(subcommand, "--provider", "claude")

    assert done.exit_code == 0, done.stderr
    assert seen == [False]
    assert_key_gone()


def test_p4_s2_ac8_evals_live_claude_call_never_holds_the_key(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    root = copy_cases(tmp_path, "sample")
    seen: list[bool] = []

    class RecordingSdk(EvalSdk):
        def __call__(self, prompt: str, options: Any) -> Any:
            seen.append(KEY_VARIABLE in os.environ)
            seen.append(KEY_VARIABLE in (options.env or {}))
            return super().__call__(prompt, options)

    sdk = RecordingSdk()
    live = run_evals(
        capsys, *live_args(root, runs=1, max_calls=1), "--max-usd", "0.10",
        "--max-cost-usd", "0.10", sdk=sdk,
    )  # fmt: skip

    assert live.exit_code != USAGE_EXIT, live.stderr
    assert seen == [False, False]
    assert_key_gone()


def test_p4_s2_ac8_evals_replay_removes_the_key(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_VARIABLE, KEY)

    run_evals(capsys, "replay", "--cases", tmp_path / "none", sdk=EvalSdk())

    assert_key_gone()


def leaks(done: Done, *texts: str) -> list[str]:
    places = {"stdout": done.stdout, "stderr": done.stderr, "audit log": done.audit_text}
    return [
        f"{text} in {place}" for text in texts for place, value in places.items() if text in value
    ]


@pytest.mark.parametrize("key", [KEY, SK_KEY], ids=["plain", "sk_shaped"])
def test_p4_s2_ac8_key_is_in_no_report_message_or_audit_record_on_success(openai, key):
    for output in ("text", "json", "markdown"):
        done = openai.review(fake(one_finding()), "--format", output, key=key)

        assert done.exit_code == FINDINGS_EXIT, done.stderr
        assert leaks(done, key) == []


ECHOING_KEY = {
    "401_json": lambda key: api_error(401, f"Incorrect API key provided: {key}."),
    "401_plain": lambda key: http(401, f"bad key {key}\n{BODY_CANARY}"),
    "500_plain": lambda key: http(500, f"upstream saw Authorization: Bearer {key}"),
    "400_json_key_in_code": lambda key: api_error(400, "x", code=key),
    "content_echoes_key": lambda key: completion(f"Your key is {key}"),
    "refusal_echoes_key": lambda key: completion(None, refusal=f"not with key {key}"),
}


@pytest.mark.parametrize("key", [KEY, SK_KEY], ids=["plain", "sk_shaped"])
@pytest.mark.parametrize("reply", ECHOING_KEY.values(), ids=ECHOING_KEY)
def test_p4_s2_ac8_key_echoed_by_the_server_reaches_no_output(openai, key, reply):
    done = openai.review(fake(reply(key)), "--max-retries", "0", "--format", "json", key=key)

    assert done.exit_code in EXIT_CODES.values(), done.stderr
    assert leaks(done, key) == []


def test_p4_s2_ac8_key_echoed_in_a_retry_warning_reaches_no_output(openai):
    transport = fake(http(503, f"overloaded for {KEY}", retry_after="0"), no_findings())

    done = openai.review(transport, "--max-retries", "1")

    assert done.exit_code == 0, done.stderr
    assert leaks(done, KEY) == []


def test_p4_s2_ac8_error_body_is_kept_only_as_scrubbed_diagnostics(openai):
    done = openai.review(fake(http(502, f"{BODY_CANARY}\nsecond line")), "--max-retries", "0")

    done.fails_as(ErrorCategory.TRANSIENT)
    assert BODY_CANARY not in done.stdout
    assert BODY_CANARY not in done.stderr
    for record in done.records:
        if BODY_CANARY in json.dumps(record):
            assert f"{record['stage']}.{record['event']}" == "agent.failed", record
            assert any(BODY_CANARY in line for line in record["data"]["diagnostics"]), record


def test_p4_s2_ac8_error_body_quoting_the_diff_is_scrubbed(openai):
    quoted = "secret_business_logic = compute_the_thing(1)"
    diff = FINDING_DIFF.replace("+x\n", f"+{quoted}\n", 1)

    done = openai.review(fake(http(500, f"model saw: {quoted}")), "--max-retries", "0", diff=diff)

    done.fails_as(ErrorCategory.TRANSIENT)
    diagnostics = done.record("agent.failed")["diagnostics"]
    assert not any(quoted in line for line in diagnostics), diagnostics


@pytest.mark.parametrize("subcommand", ["shadow", "run"])
def test_p4_s2_ac8_pr_reviews_over_openai_and_never_write_the_key(pr, monkeypatch, subcommand):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    transport = fake(one_finding())
    use_fake_transport(monkeypatch, transport)

    done = pr.run(subcommand, "--provider", "openai", "--model", "m1")

    assert done.exit_code == 0, done.stderr
    assert transport.one.header("Authorization") == f"Bearer {KEY}"
    written = [path.read_text(encoding="utf-8") for path in done.out.iterdir()]
    assert written
    for text in [*written, done.audit_text, done.stdout, done.stderr]:
        assert KEY not in text
    assert_key_gone()


# --- AC9: no cost cap with openai until S6 --------------------------------------------------------


def test_p4_s2_ac9_max_cost_usd_flag_exits_2_before_any_request(openai):
    transport = fake(no_findings())

    done = openai.review(transport, "--max-cost-usd", "1")

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert COST_REFUSAL in done.stderr
    assert transport.sent == []


def test_p4_s2_ac9_max_cost_usd_variable_exits_2_before_any_request(openai, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_MAX_COST_USD", "1")
    transport = fake(no_findings())

    done = openai.review(transport)

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert transport.sent == []


def test_p4_s2_ac9_policy_cost_ceiling_exits_2_before_any_request(openai, tmp_path):
    policy = tmp_path / "policy.toml"
    policy.write_text(policy_toml(max_cost_usd="0.60"), encoding="utf-8")
    transport = fake(no_findings())

    done = openai.review(transport, "--policy", str(policy))

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert COST_REFUSAL in done.stderr
    assert transport.sent == []


def test_p4_s2_ac9_policy_without_a_cost_ceiling_runs(openai, tmp_path):
    policy = tmp_path / "policy.toml"
    policy.write_text(policy_toml(), encoding="utf-8")

    done = openai.review(fake(no_findings()), "--policy", str(policy))

    assert done.exit_code == 0, done.stderr


@pytest.mark.parametrize("subcommand", ["shadow", "run"])
def test_p4_s2_ac9_pr_max_cost_usd_exits_2_before_any_request(pr, monkeypatch, subcommand):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    transport = fake(no_findings())
    use_fake_transport(monkeypatch, transport)

    done = pr.run(subcommand, "--provider", "openai", "--model", "m1", "--max-cost-usd", "1")

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert COST_REFUSAL in done.stderr
    assert transport.sent == []


def test_p4_s2_ac9_cost_refusal_is_before_the_key_check(openai):
    transport = fake(no_findings())

    done = openai.review(transport, "--max-cost-usd", "1", key=None)

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert transport.sent == []


# --- AC10: openai needs a model of its own --------------------------------------------------------


def test_p4_s2_ac10_review_bot_without_model_exits_2_naming_it_before_any_request(openai):
    transport = fake(no_findings())

    done = openai.review(transport, provider_flags=("--provider", "openai"))

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert "--model" in done.stderr
    assert transport.sent == []


def test_p4_s2_ac10_never_sends_claudes_default_model(openai):
    transport = fake(no_findings())

    openai.review(transport, "--deadline-s", "5", provider_flags=("--provider", "openai"))

    assert all(not sent.body["model"].startswith("claude") for sent in transport.sent)


def test_p4_s2_ac10_model_from_the_variable_satisfies_it(openai, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_MODEL", "m2")
    transport = fake(no_findings())

    done = openai.review(transport, provider_flags=("--provider", "openai"))

    assert done.exit_code == 0, done.stderr
    assert transport.one.body["model"] == "m2"


def test_p4_s2_ac10_provider_from_the_variable_without_model_exits_2(openai, monkeypatch):
    monkeypatch.setenv("REVIEW_BOT_PROVIDER", "openai")
    transport = fake(no_findings())

    done = openai.review(transport, provider_flags=())

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert "--model" in done.stderr
    assert transport.sent == []


@pytest.mark.parametrize("subcommand", ["shadow", "run"])
def test_p4_s2_ac10_pr_without_model_exits_2_naming_it_before_any_request(
    pr, monkeypatch, subcommand
):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    transport = fake(no_findings())
    use_fake_transport(monkeypatch, transport)

    done = pr.run(subcommand, "--provider", "openai")

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert "--model" in done.stderr
    assert transport.sent == []


@pytest.mark.parametrize("subcommand", ["shadow", "run"])
def test_p4_s2_ac10_pr_model_from_the_variable_satisfies_it(pr, monkeypatch, subcommand):
    monkeypatch.setenv(KEY_VARIABLE, KEY)
    monkeypatch.setenv("REVIEW_BOT_MODEL", "m2")
    transport = fake(no_findings())
    use_fake_transport(monkeypatch, transport)

    done = pr.run(subcommand, "--provider", "openai")

    assert done.exit_code == 0, done.stderr
    assert transport.one.body["model"] == "m2"


def test_p4_s2_ac10_claude_and_mock_still_default_their_model(openai):
    done = openai.review(None, provider_flags=("--provider", "mock"), key=None)

    assert done.exit_code == 0, done.stderr
    assert "model=claude-opus-5-5" in done.stderr


# --- The real UrllibTransport against a server on 127.0.0.1 ---------------------------------------


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()
    content_length: bool = True
    delay_s: float = 0.0


class LocalServer:
    def __init__(self) -> None:
        self.reply = Reply()
        self.paths: list[str] = []
        self.headers: list[dict[str, str]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                server.paths.append(self.path)
                server.headers.append(dict(self.headers))
                reply = server.reply
                time.sleep(reply.delay_s)
                try:
                    self.send_response(reply.status)
                    for name, value in reply.headers:
                        self.send_header(name, value)
                    if reply.content_length:
                        self.send_header("Content-Length", str(len(reply.body)))
                    self.end_headers()
                    self.wfile.write(reply.body)
                except OSError:
                    pass  # the client stopped reading, as a capped read should

            do_GET = do_POST  # noqa: N815 - a followed redirect would arrive as a GET

            def log_message(self, *_args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self) -> "LocalServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def server() -> Iterator[LocalServer]:
    with LocalServer() as running:
        yield running


def real_review(openai: OpenAIBot, base: str, *flags: str) -> Done:
    return openai.review(None, "--openai-base-url", base, "--max-retries", "0", *flags, key=None)


def test_p4_s2_ac1_real_transport_posts_to_chat_completions(openai, server):
    server.reply = Reply(
        body=json.dumps(completion_body(findings_content(FINDING_FIELDS))).encode()
    )

    done = real_review(openai, server.base)

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert server.paths == ["/v1/chat/completions"]
    assert "Authorization" not in server.headers[0]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_p4_s2_ac3_real_transport_never_follows_a_redirect(openai, server, status):
    target = f"http://127.0.0.1:{server.port}/followed"
    server.reply = Reply(status=status, headers=(("Location", target),))

    done = real_review(openai, server.base)

    done.fails_as(ErrorCategory.PERMANENT)
    assert server.paths == ["/v1/chat/completions"]


@pytest.mark.parametrize("content_length", [True, False], ids=["with_length", "until_close"])
def test_p4_s2_limits_real_transport_reads_at_most_4_mb(openai, server, content_length):
    server.reply = Reply(body=padded_completion(MAX_BODY_BYTES + 1), content_length=content_length)

    done, elapsed = timed(lambda: real_review(openai, server.base))

    done.fails_as(ErrorCategory.INVALID_OUTPUT)
    assert elapsed < QUICK_S


def test_p4_s2_limits_real_transport_lying_content_length_is_still_capped(openai, server):
    server.reply = Reply(
        body=padded_completion(MAX_BODY_BYTES + 1),
        content_length=False,
        headers=(("Content-Length", "10"),),
    )

    done = real_review(openai, server.base)

    assert done.exit_code == EXIT_CODES[ErrorCategory.INVALID_OUTPUT], done.stderr


def test_p4_s2_limits_real_transport_reads_a_body_under_4_mb(openai, server):
    server.reply = Reply(body=padded_completion(3_900_000))

    done = real_review(openai, server.base)

    assert done.exit_code == 0, done.stderr


def test_p4_s2_ac3_real_transport_refused_connection_is_unavailable(openai):
    with LocalServer() as closed:
        port = closed.port
    # The server is closed, so nothing listens on the port.

    done = real_review(openai, f"http://127.0.0.1:{port}/v1")

    done.fails_as(ErrorCategory.UNAVAILABLE)


def test_p4_s2_ac4_real_transport_slow_server_is_bounded_by_the_deadline(openai, server):
    server.reply = Reply(body=json.dumps(completion_body(findings_content())).encode(), delay_s=4)

    done, elapsed = timed(lambda: real_review(openai, server.base, "--deadline-s", "1"))

    done.fails_as(ErrorCategory.TRANSIENT)
    assert elapsed < 3.5


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, ErrorCategory.AUTH), (503, ErrorCategory.TRANSIENT), (429, ErrorCategory.RATE_LIMIT)],
)
def test_p4_s2_ac3_real_transport_error_statuses_map_to_their_categories(
    openai, server, status, category
):
    server.reply = Reply(status=status, body=b'{"error": {"message": "x"}}')

    done = real_review(openai, server.base)

    done.fails_as(category)
