"""The seam between the review bot and any model (ADR 0002), and the tools a model may call."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from review_bot.findings import Finding

__all__ = [
    "LLMProvider",
    "ReviewRequest",
    "ReviewResponse",
    "TokenUsage",
    "ToolDefinition",
    "ToolExecutor",
]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A tool as the model sees it: its name, what it does, and its arguments' JSON Schema."""

    name: str
    description: str
    input_schema: Mapping[str, object]


class ToolExecutor(Protocol):
    """The tools a model may call during one review, and how to call them (ADR 0014)."""

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """The tools on offer."""
        ...

    async def call(self, name: str, arguments: Mapping[str, object]) -> str:
        """Run one call and return its result, already delimited as untrusted.

        Never raises on model input: a bad call returns a refusal.
        """
        ...


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """What a provider sends to its model.

    ``tools`` is None in diff mode: one turn, with no tools.
    """

    system_prompt: str
    user_prompt: str
    model: str
    tools: ToolExecutor | None = None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Tokens used by one call."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ReviewResponse:
    """A provider's validated answer.

    ``warnings`` are whole stderr lines for the orchestrator to show and record, since providers
    write to no stream themselves (ADR 0004).
    """

    findings: tuple[Finding, ...]
    usage: TokenUsage
    warnings: tuple[str, ...] = ()


class LLMProvider(Protocol):
    """A model that can review a diff."""

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        """Return validated findings; raise ProviderError for every known failure.

        An unexpected exception is a bug: it propagates to ``main``.
        """
        ...
