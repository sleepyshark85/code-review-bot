"""A scripted provider that needs no model, account or network."""

from collections.abc import Sequence
from typing import Final

from review_bot.errors import ProviderError
from review_bot.findings import Category, Finding, Severity, parse_findings
from review_bot.providers.base import ReviewRequest, ReviewResponse, TokenUsage

__all__ = ["MockProvider"]

_CANNED_FINDING: Final = Finding(
    file="mock/example.py",
    line=1,
    severity=Severity.HIGH,
    category=Category.LOGIC,
    rationale="Canned MockProvider finding, not a real review.",
    confidence=1.0,
)


class MockProvider:
    """Records each request and answers as scripted.

    With no arguments it returns the canned finding.
    """

    def __init__(
        self,
        *,
        findings: Sequence[Finding] | None = None,
        raw_output: str | None = None,
        error: ProviderError | None = None,
    ) -> None:
        """Script the answer.

        Args:
            findings: Findings to return instead of the canned one.
            raw_output: Model text to pass through ``parse_findings``; wins over ``findings``.
            error: An error to raise; wins over both.
        """
        self.requests: list[ReviewRequest] = []
        self._findings = (_CANNED_FINDING,) if findings is None else tuple(findings)
        self._raw_output = raw_output
        self._error = error

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        """Record the request and return or raise what was scripted."""
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        findings = self._findings if self._raw_output is None else parse_findings(self._raw_output)
        return ReviewResponse(findings=findings, usage=TokenUsage(0, 0))
