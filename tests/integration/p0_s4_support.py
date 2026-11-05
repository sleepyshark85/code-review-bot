"""What the P0-S4 acceptance tests share: the expected codes and the bounded runs."""

import asyncio
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

from fake_sdk import FakeSdk

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.providers.base import ReviewRequest, ReviewResponse

PREFIX = "review-bot: error:"
REQUEST = ReviewRequest(system_prompt="Review.", user_prompt="+x", model="claude-opus-5-5")
MARK = "MODEL-SENTINEL\x1b[31m"
EXIT_CODES = {
    ErrorCategory.UNAVAILABLE: 7,
    ErrorCategory.AUTH: 8,
    ErrorCategory.RATE_LIMIT: 9,
    ErrorCategory.REFUSAL: 10,
    ErrorCategory.TRANSIENT: 11,
    ErrorCategory.PERMANENT: 12,
    ErrorCategory.INVALID_OUTPUT: 13,
}


def raw_controls(text: str) -> list[str]:
    return [char for char in text if unicodedata.category(char) in {"Cc", "Zl", "Zp"}]


def assert_one_error_line(stderr: str) -> None:
    """P0-S3 AC5 and AC6 still hold for every failure this slice classifies."""
    lines = stderr.splitlines()
    assert len(lines) == 1, repr(stderr)
    assert lines[0].startswith(PREFIX), repr(stderr)
    assert raw_controls(stderr.removesuffix("\n")) == [], repr(stderr)


def failure(claude_provider: Any, script: tuple[Any, ...], *flags: str) -> ProviderError:
    """The `ProviderError` from one review; anything else raised fails the test."""
    provider = claude_provider(FakeSdk(*script), *flags)
    try:
        asyncio.run(provider.review(REQUEST))
    except ProviderError as error:
        return error
    raise AssertionError("the review returned instead of raising a ProviderError")


def assert_maps_to(
    claude_provider: Any, claude_review: Any, script: tuple[Any, ...], category: ErrorCategory
) -> None:
    """The provider raises this category, and `main` turns it into its code and one safe line."""
    assert failure(claude_provider, script).category is category

    run = claude_review(FakeSdk(*script))

    assert run.exit_code == EXIT_CODES[category], run.stderr
    assert run.stdout == ""
    assert_one_error_line(run.stderr)
    assert category.value in run.stderr


def main_within_5s(claude_review: Any, fake: FakeSdk, *flags: str) -> tuple[Any, float]:
    """Run `main` on a thread, so one that never returns fails the test instead of hanging it."""
    outcome: list[Any] = []
    thread = threading.Thread(
        target=lambda: outcome.append(claude_review(fake, *flags)), daemon=True
    )
    started = time.monotonic()
    thread.start()
    thread.join(5)
    elapsed = time.monotonic() - started

    assert not thread.is_alive(), "main was still running after 5 seconds"
    assert outcome, "main raised instead of returning"
    return outcome[0], elapsed


@dataclass(frozen=True, slots=True)
class BoundedReview:
    response: ReviewResponse | None
    error: ProviderError | None
    closed_on_return: bool
    elapsed: float


def review_within_5s(provider: Any, fake: FakeSdk) -> BoundedReview:
    """Run `review` on a thread, and note whether the stream was closed when it came back.

    The note is taken inside the coroutine: `asyncio.run` closes abandoned generators on its way
    out, so a check after it can't tell a provider that closed its stream from one that didn't.
    """
    outcome: dict[str, Any] = {}

    async def call() -> None:
        try:
            outcome["response"] = await provider.review(REQUEST)
        except ProviderError as error:
            outcome["error"] = error
        outcome["closed"] = fake.closed

    thread = threading.Thread(target=lambda: asyncio.run(call()), daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(5)
    elapsed = time.monotonic() - started

    assert not thread.is_alive(), "review was still running after 5 seconds"
    assert "closed" in outcome, "review raised something other than a ProviderError"
    return BoundedReview(outcome.get("response"), outcome.get("error"), outcome["closed"], elapsed)
