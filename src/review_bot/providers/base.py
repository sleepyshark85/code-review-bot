"""The seam between the review bot and any model (ADR 0002)."""

from dataclasses import dataclass
from typing import Protocol

from review_bot.findings import Finding

__all__ = ["LLMProvider", "ReviewRequest", "ReviewResponse", "TokenUsage"]


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """What a provider sends to its model."""

    system_prompt: str
    user_prompt: str
    model: str


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Tokens used by one call."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ReviewResponse:
    """A provider's validated answer."""

    findings: tuple[Finding, ...]
    usage: TokenUsage


class LLMProvider(Protocol):
    """A model that can review a diff."""

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        """Return validated findings; raise ProviderError for every known failure.

        An unexpected exception is a bug: it propagates to ``main``.
        """
        ...
