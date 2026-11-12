"""A scripted `HttpTransport`, and Chat Completions responses, for offline tests (P4-S2)."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from review_bot.providers.openai_compat import HttpResponse

# Built at run time, so it can't look like a real key to a secret scan.
API_KEY = "-".join(["test", "placeholder", "not", "a", "key"])


@dataclass(frozen=True)
class SentRequest:
    url: str
    body: Mapping[str, Any]
    headers: dict[str, str]
    timeout_s: float


@dataclass
class FakeTransport:
    """Answers each POST with the next scripted response, or raises it if it is an exception."""

    script: list[HttpResponse | BaseException]
    sent: list[SentRequest] = field(default_factory=list)

    async def post_json(
        self,
        url: str,
        body: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> HttpResponse:
        self.sent.append(SentRequest(url, body, dict(headers), timeout_s))
        answer = self.script.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def transport(*script: HttpResponse | BaseException) -> FakeTransport:
    return FakeTransport(list(script))


def completion_body(
    content: str | None = '{"findings": []}',
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    usage: object = None,
) -> bytes:
    message = {"role": "assistant", "content": content, "refusal": refusal}
    document = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7} if usage is None else usage,
    }
    return json.dumps(document).encode()


def completion(content: str | None = '{"findings": []}', **fields: Any) -> HttpResponse:
    """A 200 response whose first choice's content is ``content``."""
    return HttpResponse(
        200, {"content-type": "application/json"}, completion_body(content, **fields)
    )


def error_response(
    status: int,
    *,
    message: str = "endpoint error prose",
    code: str | None = None,
    headers: Sequence[tuple[str, str]] = (),
) -> HttpResponse:
    """An error response in OpenAI's shape: ``{"error": {"message": ..., "code": ...}}``."""
    body = json.dumps({"error": {"message": message, "type": "error", "code": code}}).encode()
    return HttpResponse(status, dict(headers), body)
