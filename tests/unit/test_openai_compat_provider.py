import asyncio
import json
import socket
from datetime import UTC, datetime
from http.client import IncompleteRead, RemoteDisconnected

import pytest
from diff_samples import FINDING_FIELDS
from openai_samples import API_KEY, FakeTransport, completion, error_response, transport

from review_bot.errors import ConfigError, ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.providers.base import ReviewRequest, TokenUsage
from review_bot.providers.openai_compat import HttpResponse, OpenAICompatibleProvider
from review_bot.settings import Settings

REQUEST = ReviewRequest(system_prompt="System.", user_prompt="+diff", model="m1")
FINDINGS_JSON = json.dumps({"findings": [FINDING_FIELDS]})
BASE_URL = "https://api.openai.com/v1"
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


class Waits:
    """Records each wait between retries, without waiting."""

    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


def provider(
    fake: FakeTransport,
    *,
    settings: Settings | None = None,
    base_url: str = BASE_URL,
    api_key: str | None = API_KEY,
    waits: Waits | None = None,
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        Settings(provider="openai", model="m1") if settings is None else settings,
        base_url=base_url,
        api_key=api_key,
        transport=fake,
        sleep=Waits() if waits is None else waits,
        clock=lambda: NOW,
    )


async def failure(fake: FakeTransport, **options: object) -> ProviderError:
    with pytest.raises(ProviderError) as caught:
        await provider(fake, **options).review(REQUEST)  # type: ignore[arg-type]
    return caught.value


async def test_review_posts_one_chat_completion_with_the_key_and_both_prompts() -> None:
    fake = transport(completion())

    await provider(fake).review(REQUEST)

    (sent,) = fake.sent
    assert sent.url == "https://api.openai.com/v1/chat/completions"
    assert sent.headers == {"Authorization": f"Bearer {API_KEY}"}
    assert sent.body == {
        "model": "m1",
        "messages": [
            {"role": "system", "content": "System."},
            {"role": "user", "content": "+diff"},
        ],
    }


async def test_review_joins_a_base_url_with_a_trailing_slash_once() -> None:
    fake = transport(completion())

    await provider(fake, base_url="https://models.example.org/v1/").review(REQUEST)

    assert fake.sent[0].url == "https://models.example.org/v1/chat/completions"


async def test_review_sends_the_output_cap_when_set() -> None:
    fake = transport(completion())
    settings = Settings(provider="openai", max_output_tokens=900)

    await provider(fake, settings=settings).review(REQUEST)

    assert fake.sent[0].body["max_completion_tokens"] == 900


@pytest.mark.parametrize("api_key", [None, ""])
async def test_review_on_loopback_without_a_key_sends_no_authorization(
    api_key: str | None,
) -> None:
    fake = transport(completion())

    await provider(fake, base_url="http://127.0.0.1:8000/v1", api_key=api_key).review(REQUEST)

    assert fake.sent[0].headers == {}


async def test_review_bounds_each_request_by_the_api_timeout() -> None:
    fake = transport(completion())
    settings = Settings(provider="openai", api_timeout_ms=2_500)

    await provider(fake, settings=settings).review(REQUEST)

    assert fake.sent[0].timeout_s == 2.5


async def test_review_bounds_each_request_by_the_time_left_before_the_deadline() -> None:
    fake = transport(completion())
    settings = Settings(provider="openai", api_timeout_ms=90_000, deadline_s=5)

    await provider(fake, settings=settings).review(REQUEST)

    assert 0 < fake.sent[0].timeout_s <= 5


async def test_review_returns_the_validated_findings_and_usage() -> None:
    response = await provider(transport(completion(FINDINGS_JSON))).review(REQUEST)

    assert response.findings == (Finding.model_validate(FINDING_FIELDS),)
    assert response.usage == TokenUsage(input_tokens=11, output_tokens=7)
    assert response.warnings == ()
    assert response.cost_usd is None


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (error_response(401), ErrorCategory.AUTH),
        (error_response(403), ErrorCategory.AUTH),
        (error_response(429, code="insufficient_quota"), ErrorCategory.PERMANENT),
        (error_response(404), ErrorCategory.PERMANENT),
        (
            error_response(302, headers=[("Location", "https://elsewhere.example")]),
            ErrorCategory.PERMANENT,
        ),
        (completion(finish_reason="content_filter"), ErrorCategory.REFUSAL),
        (completion(finish_reason="length"), ErrorCategory.INVALID_OUTPUT),
        (completion("not json"), ErrorCategory.INVALID_OUTPUT),
        (HttpResponse(200, {}, b"x" * (4 * 1024 * 1024 + 1)), ErrorCategory.INVALID_OUTPUT),
    ],
    ids=["401", "403", "quota", "404", "302", "filter", "length", "content", "over_4_mb"],
)
async def test_review_fails_at_once_with_the_responses_category(
    response: HttpResponse, category: ErrorCategory
) -> None:
    fake = transport(response, completion())

    error = await failure(fake)

    assert error.category is category
    assert len(fake.sent) == 1


@pytest.mark.parametrize(
    ("raised", "category"),
    [
        (TimeoutError(), ErrorCategory.TRANSIENT),
        (ConnectionResetError(), ErrorCategory.TRANSIENT),
        (RemoteDisconnected(), ErrorCategory.TRANSIENT),
        (IncompleteRead(b""), ErrorCategory.TRANSIENT),
        (ConnectionRefusedError(), ErrorCategory.UNAVAILABLE),
        (socket.gaierror(socket.EAI_NONAME, "unknown host"), ErrorCategory.UNAVAILABLE),
        (OSError("network unreachable"), ErrorCategory.UNAVAILABLE),
    ],
    ids=["timeout", "reset", "disconnected", "incomplete", "refused", "dns", "other"],
)
async def test_review_without_a_response_fails_with_its_category(
    raised: BaseException, category: ErrorCategory
) -> None:
    settings = Settings(provider="openai", max_retries=0)

    error = await failure(transport(raised), settings=settings)

    assert error.category is category
    assert error.__cause__ is raised


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (error_response(503), ErrorCategory.TRANSIENT),
        (error_response(429), ErrorCategory.RATE_LIMIT),
    ],
)
async def test_review_without_retries_fails_with_the_status_category(
    response: HttpResponse, category: ErrorCategory
) -> None:
    error = await failure(transport(response), settings=Settings(max_retries=0))

    assert error.category is category
    assert "HTTP" in str(error)


async def test_a_failures_message_is_fixed_and_its_body_only_a_diagnostic() -> None:
    error = await failure(transport(error_response(401, message="Bad key.\nSee the docs.")))

    assert "Bad key" not in str(error)
    assert "OPENAI_API_KEY" in str(error)
    assert error.diagnostics == ("Bad key.", "See the docs.")


async def test_an_echoed_key_is_removed_from_the_diagnostics() -> None:
    error = await failure(transport(error_response(401, message=f"Key {API_KEY} is invalid.")))

    assert error.diagnostics == ("Key [REDACTED] is invalid.",)
    assert API_KEY not in str(error)


async def test_an_error_with_an_empty_body_has_no_diagnostics() -> None:
    error = await failure(transport(HttpResponse(400, {}, b"")))

    assert error.diagnostics == ()


async def test_review_retries_a_transient_failure_once_with_one_warning() -> None:
    fake = transport(error_response(503), completion(FINDINGS_JSON))
    settings = Settings(provider="openai", max_retries=1)

    response = await provider(fake, settings=settings).review(REQUEST)

    assert len(response.findings) == 1
    assert len(fake.sent) == 2
    (warning,) = response.warnings
    assert warning.startswith("review-bot: warning: ")
    assert "HTTP 503" in warning
    assert "retry 1 of 1" in warning


async def test_review_retries_a_dropped_connection() -> None:
    fake = transport(ConnectionResetError(), completion())

    response = await provider(fake).review(REQUEST)

    assert "ConnectionResetError" in response.warnings[0]


async def test_review_backs_off_between_retries_without_retry_after() -> None:
    waits = Waits()
    fake = transport(error_response(500), error_response(429), completion())

    await provider(fake, waits=waits).review(REQUEST)

    assert waits.seconds == [1, 2]


@pytest.mark.parametrize("seconds", ["0", "7", "30", " 12 "])
async def test_review_honours_a_retry_after_of_up_to_30_seconds(seconds: str) -> None:
    waits = Waits()
    fake = transport(error_response(429, headers=[("retry-after", seconds)]), completion())

    await provider(fake, waits=waits).review(REQUEST)

    assert waits.seconds == [int(seconds)]


async def test_review_reads_retry_after_whatever_its_case() -> None:
    waits = Waits()
    fake = transport(error_response(503, headers=[("Retry-After", "4")]), completion())

    await provider(fake, waits=waits).review(REQUEST)

    assert waits.seconds == [4]


@pytest.mark.parametrize("seconds", ["-1", "1.5", "soon", ""])
async def test_review_backs_off_when_retry_after_is_unparsable(seconds: str) -> None:
    waits = Waits()
    fake = transport(error_response(429, headers=[("Retry-After", seconds)]), completion())

    await provider(fake, waits=waits).review(REQUEST)

    assert waits.seconds == [1]


async def test_review_fails_at_once_when_retry_after_is_over_30_seconds() -> None:
    waits = Waits()
    fake = transport(error_response(429, headers=[("Retry-After", "31")]), completion())

    error = await failure(fake, waits=waits)

    assert error.category is ErrorCategory.RATE_LIMIT
    assert waits.seconds == []
    assert len(fake.sent) == 1


@pytest.mark.parametrize(
    ("date", "wait_s"),
    [("Thu, 12 Nov 2026 10:00:20 GMT", 20), ("Thu, 12 Nov 2026 09:00:00 GMT", 0)],
    ids=["ahead", "past"],
)
async def test_review_honours_a_retry_after_date_against_the_clock(date: str, wait_s: int) -> None:
    waits = Waits()
    fake = transport(error_response(429, headers=[("Retry-After", date)]), completion())

    await provider(fake, waits=waits).review(REQUEST)

    assert waits.seconds == [wait_s]


async def test_review_fails_at_once_when_a_retry_after_date_is_over_30_seconds_ahead() -> None:
    waits = Waits()
    later = "Thu, 12 Nov 2026 11:00:00 GMT"
    fake = transport(error_response(503, headers=[("Retry-After", later)]), completion())

    error = await failure(fake, waits=waits)

    assert error.category is ErrorCategory.TRANSIENT
    assert waits.seconds == []
    assert len(fake.sent) == 1


async def test_review_fails_at_once_when_the_wait_would_pass_the_deadline() -> None:
    waits = Waits()
    fake = transport(error_response(503, headers=[("Retry-After", "20")]), completion())
    settings = Settings(provider="openai", deadline_s=10)

    error = await failure(fake, settings=settings, waits=waits)

    assert error.category is ErrorCategory.TRANSIENT
    assert waits.seconds == []


async def test_review_fails_with_the_last_error_once_the_retries_are_spent() -> None:
    fake = transport(error_response(503), error_response(503), error_response(429))

    error = await failure(fake)

    assert error.category is ErrorCategory.RATE_LIMIT
    assert len(fake.sent) == 3


@pytest.mark.parametrize(
    "response",
    [error_response(401), error_response(400), completion("not json")],
    ids=["auth", "permanent", "invalid_output"],
)
async def test_review_never_retries_other_categories(response: HttpResponse) -> None:
    fake = transport(response, completion())

    await failure(fake)

    assert len(fake.sent) == 1


async def test_review_past_the_deadline_fails_transient_naming_the_deadline() -> None:
    class Hanging:
        async def post_json(self, *_args: object, **_kwargs: object) -> HttpResponse:
            await asyncio.Event().wait()
            raise AssertionError

    with pytest.raises(ProviderError, match="--deadline-s") as caught:
        await OpenAICompatibleProvider(
            Settings(deadline_s=0.01), base_url=BASE_URL, api_key=API_KEY, transport=Hanging()
        ).review(REQUEST)

    assert caught.value.category is ErrorCategory.TRANSIENT


@pytest.mark.parametrize("api_key", [None, ""])
def test_provider_without_a_key_for_a_remote_host_raises_config_error(
    api_key: str | None,
) -> None:
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        provider(transport(), api_key=api_key)


def test_provider_refuses_an_unsafe_base_url() -> None:
    with pytest.raises(ValueError, match="https"):
        provider(transport(), base_url="http://models.example.org/v1")


@pytest.mark.parametrize(
    "suffix", ["\n", " ", "\r\n", "\x00"], ids=["newline", "space", "crlf", "nul"]
)
def test_provider_refuses_a_key_that_cant_go_in_a_header_without_quoting_it(suffix: str) -> None:
    with pytest.raises(ConfigError, match="OPENAI_API_KEY") as caught:
        provider(transport(), api_key=API_KEY + suffix)

    assert API_KEY not in str(caught.value)


def test_provider_refuses_an_unsendable_key_on_loopback_too() -> None:
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        provider(transport(), base_url="http://127.0.0.1/v1", api_key=f"{API_KEY}\n")
