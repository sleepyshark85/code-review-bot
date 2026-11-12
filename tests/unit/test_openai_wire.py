import dataclasses
import json
from datetime import UTC, datetime

import pytest
from diff_samples import FINDING_FIELDS
from openai_samples import completion_body

from review_bot.context.tool_args import TOOL_DEFINITIONS
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.providers.base import TokenUsage
from review_bot.providers.openai_wire import (
    DEFAULT_BASE_URL,
    MAX_RESPONSE_BYTES,
    MAX_TOOL_ARGUMENTS_BYTES,
    ChatRequest,
    Completion,
    ToolCall,
    ToolTurn,
    check_base_url,
    is_loopback_url,
    is_plain_text,
    read_completion,
    read_error,
    retry_after_s,
    tool_replies,
)

FINDINGS_JSON = json.dumps({"findings": [FINDING_FIELDS]})


def chat(*, max_output_tokens: int | None = None, base_url: str = DEFAULT_BASE_URL) -> ChatRequest:
    return ChatRequest(
        model="m1",
        system_prompt="System.\nText",
        user_prompt="+diff ü",
        max_output_tokens=max_output_tokens,
        base_url=base_url,
    )


def category_of(body: bytes) -> ErrorCategory:
    with pytest.raises(ProviderError) as caught:
        read_completion(body)
    return caught.value.category


def test_chat_body_holds_the_model_and_both_prompts_and_no_cap_or_tools() -> None:
    assert chat().body() == {
        "model": "m1",
        "messages": [
            {"role": "system", "content": "System.\nText"},
            {"role": "user", "content": "+diff ü"},
        ],
    }


@pytest.mark.parametrize(
    ("base_url", "field"),
    [
        ("https://api.openai.com/v1", "max_completion_tokens"),
        ("https://API.OPENAI.COM/v1", "max_completion_tokens"),
        ("https://models.example.org/v1", "max_tokens"),
        ("http://127.0.0.1:8000/v1", "max_tokens"),
        ("https://api.openai.com.example.org/v1", "max_tokens"),
    ],
)
def test_chat_body_sends_the_output_cap_under_the_hosts_field(base_url: str, field: str) -> None:
    body = chat(max_output_tokens=500, base_url=base_url).body()

    assert body[field] == 500
    assert {"max_completion_tokens", "max_tokens"} & body.keys() == {field}


@pytest.mark.parametrize(
    "url",
    [
        DEFAULT_BASE_URL,
        "https://models.example.org/v1/",
        "http://127.0.0.1:8000/v1",
        "http://127.0.0.2/v1",
        "http://localhost/v1",
        "http://[::1]/v1",
    ],
)
def test_check_base_url_accepts_https_and_loopback_http(url: str) -> None:
    assert check_base_url(url) == url


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("http://models.example.org/v1", "https"),
        ("http://10.0.0.1/v1", "https"),
        ("http://localhost.example.org/v1", "https"),
        ("https://user:pass@models.example.org/v1", "user name"),
        ("https://user@models.example.org/v1", "user name"),
        ("https://models.example.org/v1?key=1", "query"),
        ("https://models.example.org/v1?", "query"),
        ("https://models.example.org/v1#part", "fragment"),
        ("ftp://models.example.org/v1", "http or https"),
        ("https:///v1", "http or https"),
        ("models.example.org/v1", "http or https"),
        ("http://[::1]:x/v1", "port"),
        ("https://models.example.org:99999/v1", "port"),
        ("https://models.example.org/v 1", "space"),
        ("https://models.example.org/v1\n", "control"),
        ("https://models.example.org/v1\t", "control"),
        ("https://models.example.org/v\u00e9", "ASCII"),
    ],
)
def test_check_base_url_rejects_an_unsafe_url_saying_why(url: str, problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        check_base_url(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:8000/v1", True),
        ("http://[::1]/v1", True),
        ("http://127.1.2.3/v1", True),
        ("https://api.openai.com/v1", False),
        ("https://192.0.2.1/v1", False),
    ],
)
def test_is_loopback_url(url: str, expected: bool) -> None:
    assert is_loopback_url(url) is expected


def test_read_completion_returns_the_findings_and_usage() -> None:
    completion = read_completion(completion_body(FINDINGS_JSON))

    assert completion.findings == (Finding.model_validate(FINDING_FIELDS),)
    assert completion.usage == TokenUsage(input_tokens=11, output_tokens=7)


@pytest.mark.parametrize(
    "usage",
    [
        "many",
        {"prompt_tokens": -1, "completion_tokens": 2},
        {"prompt_tokens": 1, "completion_tokens": True},
        {"prompt_tokens": 1.5, "completion_tokens": 2},
        {"prompt_tokens": 1},
        {"completion_tokens": 2},
        [],
        None,
    ],
)
def test_read_completion_of_unusable_usage_has_none(usage: object) -> None:
    document = json.loads(completion_body())
    document["usage"] = usage

    assert read_completion(json.dumps(document).encode()).usage is None


def test_read_completion_without_usage_has_none() -> None:
    document = json.loads(completion_body())
    del document["usage"]

    assert read_completion(json.dumps(document).encode()).usage is None


def test_read_completion_of_a_tool_turn_without_usage_has_none() -> None:
    document = json.loads(tool_call_body(call_with_arguments("{}")))
    del document["usage"]

    assert read_completion(json.dumps(document).encode()).usage is None


def test_read_completion_with_an_empty_refusal_reads_the_content() -> None:
    assert read_completion(completion_body(refusal="")).findings == ()


@pytest.mark.parametrize(
    "body",
    [
        completion_body(finish_reason="content_filter"),
        completion_body(None, refusal="I can't help with that."),
        completion_body(FINDINGS_JSON, refusal="I can't help with that."),
    ],
    ids=["content_filter", "refusal", "refusal_with_content"],
)
def test_read_completion_of_a_refusal_raises_refusal(body: bytes) -> None:
    assert category_of(body) is ErrorCategory.REFUSAL


def test_read_completion_cut_short_by_the_cap_raises_invalid_output_naming_the_cap() -> None:
    with pytest.raises(ProviderError, match="--max-output-tokens") as caught:
        read_completion(completion_body(FINDINGS_JSON, finish_reason="length"))

    assert caught.value.category is ErrorCategory.INVALID_OUTPUT


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"\xff\xfe\x00",
        b"[]",
        b'"text"',
        b"{}",
        b'{"choices": []}',
        b'{"choices": {}}',
        b'{"choices": ["text"]}',
        b'{"choices": [{"finish_reason": "stop"}]}',
        b'{"choices": [{"message": "text"}]}',
        b"[" * 100_000 + b"]" * 100_000,
        completion_body(None),
        completion_body("not json"),
        completion_body('{"findings": [{"file": "a.py"}]}'),
        completion_body(" " * MAX_RESPONSE_BYTES),
    ],
    ids=[
        "empty",
        "not_json",
        "not_utf8",
        "array",
        "string",
        "no_choices",
        "no_choice",
        "choices_not_a_list",
        "choice_not_an_object",
        "no_message",
        "message_not_an_object",
        "deeply_nested",
        "null_content",
        "content_not_json",
        "content_off_schema",
        "over_4_mb",
    ],
)
def test_read_completion_of_anything_else_raises_invalid_output(body: bytes) -> None:
    assert category_of(body) is ErrorCategory.INVALID_OUTPUT


def test_read_completion_never_shows_the_endpoints_text() -> None:
    with pytest.raises(ProviderError) as caught:
        read_completion(b'{"error": {"message": "secret prose"}}')

    assert "secret prose" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "body", "category"),
    [
        (401, b"", ErrorCategory.AUTH),
        (403, b"{}", ErrorCategory.AUTH),
        (429, b'{"error": {"code": "rate_limit_exceeded"}}', ErrorCategory.RATE_LIMIT),
        (429, b"slow down", ErrorCategory.RATE_LIMIT),
        (429, b'{"error": {"code": "insufficient_quota"}}', ErrorCategory.PERMANENT),
        (500, b"", ErrorCategory.TRANSIENT),
        (503, b"", ErrorCategory.TRANSIENT),
        (599, b"", ErrorCategory.TRANSIENT),
        (400, b"", ErrorCategory.PERMANENT),
        (404, b"", ErrorCategory.PERMANENT),
        (408, b"", ErrorCategory.PERMANENT),
        (301, b"", ErrorCategory.PERMANENT),
        (302, b"", ErrorCategory.PERMANENT),
        (307, b"", ErrorCategory.PERMANENT),
        (600, b"", ErrorCategory.PERMANENT),
    ],
)
def test_read_error_maps_each_status(status: int, body: bytes, category: ErrorCategory) -> None:
    assert read_error(status, body).category is category


@pytest.mark.parametrize(
    ("status", "body", "is_quota_exhausted"),
    [
        (429, b'{"error": {"code": "insufficient_quota"}}', True),
        (429, b'{"error": {"code": "rate_limit_exceeded"}}', False),
        (400, b'{"error": {"code": "insufficient_quota"}}', False),
        (429, b'{"error": "insufficient_quota"}', False),
    ],
)
def test_read_error_tells_an_exhausted_quota_only_from_a_429_code(
    status: int, body: bytes, is_quota_exhausted: bool
) -> None:
    assert read_error(status, body).is_quota_exhausted is is_quota_exhausted


@pytest.mark.parametrize(
    ("body", "text"),
    [
        (b'{"error": {"message": "Bad key.\\nTry again."}}', "Bad key.\nTry again."),
        (b"  upstream down \n", "upstream down"),
        (b'{"error": {"code": 1}}', '{"error": {"code": 1}}'),
        (b"\xffbad", "�bad"),
        (b"", None),
        (b"  \n", None),
    ],
)
def test_read_error_text_prefers_the_error_message_then_the_body(
    body: bytes, text: str | None
) -> None:
    assert read_error(500, body).text == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sk-abc_123.XYZ", True),
        ("key\n", False),
        ("key\r", False),
        ("a key", False),
        ("tab\tkey", False),
        ("ke\x00y", False),
        ("k\u00e9y", False),
    ],
)
def test_is_plain_text_allows_only_printable_ascii_without_spaces(
    text: str, expected: bool
) -> None:
    assert is_plain_text(text) is expected


NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "wait_s"),
    [
        ("0", 0.0),
        ("30", 30.0),
        (" 120 ", 120.0),
        ("Thu, 12 Nov 2026 10:00:30 GMT", 30.0),
        ("Thu, 12 Nov 2026 11:00:00 GMT", 3600.0),
        ("Thu, 12 Nov 2026 10:00:05 -0000", 5.0),
        ("Thu, 12 Nov 2026 12:00:10 +0200", 10.0),
        ("Wed, 11 Nov 2026 10:00:00 GMT", 0.0),
    ],
)
def test_retry_after_s_reads_seconds_or_an_http_date(value: str, wait_s: float) -> None:
    assert retry_after_s(value, now=NOW) == wait_s


@pytest.mark.parametrize("value", ["", "-1", "1.5", "soon", "١٢", "Thu, 99 Nov 2026 10:00:00 GMT"])
def test_retry_after_s_of_an_unparsable_value_is_none(value: str) -> None:
    assert retry_after_s(value, now=NOW) is None


# --- Tool calls (P4-S6) ---------------------------------------------------------------------------


def tool_call_body(*calls: object, content: str | None = None) -> bytes:
    message = {"role": "assistant", "content": content, "tool_calls": list(calls)}
    document = {
        "choices": [{"message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    return json.dumps(document).encode()


def call_with_arguments(arguments: object) -> dict[str, object]:
    return {"id": "c1", "type": "function", "function": {"name": "read", "arguments": arguments}}


def test_chat_body_offers_each_tool_as_a_function_in_order() -> None:
    tools = TOOL_DEFINITIONS[:2]
    body = dataclasses.replace(chat(), tools=tools).body()

    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


def test_chat_body_puts_the_replies_after_the_prompts() -> None:
    reply = {"role": "tool", "tool_call_id": "c1", "content": "text"}

    messages = chat().body([reply])["messages"]

    assert messages == [
        {"role": "system", "content": "System.\nText"},
        {"role": "user", "content": "+diff ü"},
        reply,
    ]


def test_read_completion_with_tool_calls_is_a_tool_turn_in_order() -> None:
    first = call_with_arguments('{"path": "a.py", "start_line": 3}')
    second = {"id": "c2", "function": {"name": "nope", "arguments": "{}"}}

    turn = read_completion(tool_call_body(first, second, content="Let me look."))

    assert turn == ToolTurn(
        "Let me look.",
        (ToolCall("c1", "read", {"path": "a.py", "start_line": 3}), ToolCall("c2", "nope", {})),
        TokenUsage(3, 2),
    )


def test_read_completion_with_no_tool_calls_reads_the_findings() -> None:
    document = json.loads(completion_body(FINDINGS_JSON))
    document["choices"][0]["message"]["tool_calls"] = []

    completion = read_completion(json.dumps(document).encode())

    assert isinstance(completion, Completion)


@pytest.mark.parametrize(
    "arguments",
    [
        "{not json",
        "[1]",
        '"text"',
        "null",
        "",
        {"path": "a.py"},
        None,
        '{"path": "a.py", "path": "b.py"}',
        '{"start_line": NaN}',
        '{"start_line": Infinity}',
        "[" * 10_000 + "]" * 10_000,
        json.dumps({"path": "a" * MAX_TOOL_ARGUMENTS_BYTES}),
    ],
    ids=[
        "not_json",
        "array",
        "string",
        "null",
        "empty",
        "object_not_string",
        "missing",
        "repeated_key",
        "nan",
        "infinity",
        "deeply_nested",
        "over_64_kb",
    ],
)
def test_read_completion_parses_arguments_strictly_else_gives_none(arguments: object) -> None:
    turn = read_completion(tool_call_body(call_with_arguments(arguments)))

    assert isinstance(turn, ToolTurn)
    assert turn.calls[0].arguments == {}


def test_read_completion_parses_arguments_of_exactly_64_kb() -> None:
    padding = "a" * (MAX_TOOL_ARGUMENTS_BYTES - len('{"path": ""}'))
    arguments = json.dumps({"path": padding})

    turn = read_completion(tool_call_body(call_with_arguments(arguments)))

    assert isinstance(turn, ToolTurn)
    assert turn.calls[0].arguments == {"path": padding}


@pytest.mark.parametrize(
    "tool_calls",
    [
        "read",
        ["read"],
        [{"function": {"name": "read", "arguments": "{}"}}],
        [{"id": 1, "function": {"name": "read", "arguments": "{}"}}],
        [{"id": "c1"}],
        [{"id": "c1", "function": "read"}],
        [{"id": "c1", "function": {"arguments": "{}"}}],
    ],
    ids=[
        "not_a_list",
        "call_not_an_object",
        "no_id",
        "id_not_a_string",
        "no_function",
        "function_not_an_object",
        "no_name",
    ],
)
def test_read_completion_with_malformed_tool_calls_raises_invalid_output(
    tool_calls: object,
) -> None:
    document = json.loads(completion_body(FINDINGS_JSON))
    document["choices"][0]["message"]["tool_calls"] = tool_calls

    assert category_of(json.dumps(document).encode()) is ErrorCategory.INVALID_OUTPUT


def test_tool_replies_are_the_assistant_message_then_each_result() -> None:
    turn = ToolTurn(
        None,
        (ToolCall("c1", "read", {"path": "a.py"}), ToolCall("c2", "grep", {})),
        TokenUsage(0, 0),
    )

    replies = tool_replies(turn, ["one", "two"])

    assert replies == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path": "a.py"}'},
                },
                {"id": "c2", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "one"},
        {"role": "tool", "tool_call_id": "c2", "content": "two"},
    ]


def test_tool_replies_need_one_result_per_call() -> None:
    turn = ToolTurn(None, (ToolCall("c1", "read", {}),), TokenUsage(0, 0))

    with pytest.raises(ValueError, match="zip"):
        tool_replies(turn, [])
