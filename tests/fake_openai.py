"""A scripted `HttpTransport` for `OpenAICompatibleProvider`, the seam P4-S2 names for offline
tests, and builders for the Chat Completions responses it plays back.

Every call is recorded (`sent`), with its body round-tripped through JSON, so a body that isn't
JSON fails the call loudly. A script step is a response to return, an exception to raise, or an
async callable to await for the response. The last step repeats, so a provider that retries keeps
getting the same answer.

This module and `use_fake_transport` are the only places that know how the developer's code takes
a transport: the CLI builds its own provider, so the fake replaces the real `UrllibTransport` that
the provider builds when it is given none.
"""

import asyncio
import json
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from review_bot.providers import openai_compat

KEY = "canary-openai-key-7f3a9c41e2"  # not shaped like any known secret, so only removal hides it
# Shaped like a real OpenAI key, so it is assembled at run time: a literal would trip the
# secret scanners (CONVENTIONS.md, P3-S17).
SK_KEY = "-".join(["sk", "proj", "Canary0123456789abcdefABCDEF0123456789"])
DEFAULT_BASE = "https://api.openai.com/v1"
COMPLETIONS = f"{DEFAULT_BASE}/chat/completions"
MAX_BODY_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Sent:
    url: str
    body: dict[str, Any]
    headers: dict[str, str]
    timeout_s: float

    def header(self, name: str) -> str | None:
        """The header's value, matched without regard to case, as HTTP does."""
        found = [value for key, value in self.headers.items() if key.lower() == name.lower()]
        assert len(found) <= 1, self.headers
        return found[0] if found else None


type Step = Any  # an `HttpResponse`, a `BaseException`, or an async callable returning either


@dataclass
class FakeTransport:
    script: tuple[Step, ...]
    sent: list[Sent] = field(default_factory=list)

    async def post_json(
        self,
        url: str,
        body: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> Any:
        self.sent.append(Sent(url, json.loads(json.dumps(body)), dict(headers), timeout_s))
        step = self.script[min(len(self.sent), len(self.script)) - 1]
        if callable(step) and not isinstance(step, BaseException):
            step = await step()
        if isinstance(step, BaseException):
            raise step
        return step

    @property
    def one(self) -> Sent:
        assert len(self.sent) == 1, self.sent
        return self.sent[0]


def fake(*script: Step) -> FakeTransport:
    return FakeTransport(script)


def use_fake_transport(monkeypatch: pytest.MonkeyPatch, transport: FakeTransport) -> None:
    """Make every provider the commands build send through `transport`."""
    monkeypatch.setattr(openai_compat, "UrllibTransport", lambda *_args, **_kwargs: transport)


# --- Responses ------------------------------------------------------------------------------------


def http(status: int, body: bytes | str | Mapping[str, Any] = b"", **headers: str) -> Any:
    """A response; a mapping body is sent as JSON. `retry_after="1"` sets `Retry-After`."""
    if isinstance(body, Mapping):
        body = json.dumps(body)
    if isinstance(body, str):
        body = body.encode("utf-8")
    named = {name.replace("_", "-").title(): value for name, value in headers.items()}
    return openai_compat.HttpResponse(status=status, headers=named, body=body)


def completion_body(
    content: str | None,
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    prompt_tokens: int = 123,
    completion_tokens: int = 45,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if refusal is not None:
        message["refusal"] = refusal
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "m1",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def completion(content: str | None, **fields: Any) -> Any:
    return http(200, completion_body(content, **fields))


def findings_content(*findings: Mapping[str, Any]) -> str:
    return json.dumps({"findings": list(findings)})


def api_error(status: int, message: str = "error prose", code: str | None = None, **headers: str):
    return http(
        status, {"error": {"message": message, "type": "api_error", "code": code}}, **headers
    )


# --- Failures below HTTP --------------------------------------------------------------------------


def connection_failure(kind: str) -> BaseException:
    """What the transport raises when the connection fails, by kind."""
    match kind:
        case "timeout":
            return TimeoutError("timed out")
        case "reset":
            return ConnectionResetError(104, "Connection reset by peer")
        case "refused":
            return ConnectionRefusedError(111, "Connection refused")
        case "dns":
            return socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    raise AssertionError(kind)


def hang_forever() -> Callable[[], Awaitable[Any]]:
    async def wait() -> Any:
        await asyncio.Event().wait()

    return wait
