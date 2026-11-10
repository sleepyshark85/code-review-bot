"""A scripted provider that needs no model, account or network."""

from collections.abc import Mapping, Sequence
from decimal import Decimal
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
    """Records each request, makes any scripted tool calls, and answers as scripted.

    With no arguments it returns the canned finding.
    """

    def __init__(  # noqa: PLR0913 - every parameter is keyword-only
        self,
        *,
        findings: Sequence[Finding] | None = None,
        raw_output: str | None = None,
        error: ProviderError | None = None,
        warnings: Sequence[str] = (),
        usage: TokenUsage | None = None,
        tool_calls: Sequence[tuple[str, Mapping[str, object]]] = (),
        cost_usd: Decimal | None = None,
    ) -> None:
        """Script the answer.

        Args:
            findings: Findings to return instead of the canned one.
            raw_output: Model text to pass through ``parse_findings``; wins over ``findings``.
            error: An error to raise, after any tool calls; wins over both.
            warnings: Warnings to return with the findings.
            usage: The token usage to report; None reports none.
            tool_calls: Tool names and arguments to call, in order, through a request's
                ``tools`` before answering. A request with no tools gets no calls.
            cost_usd: The cost to report; None reports none.
        """
        self.requests: list[ReviewRequest] = []
        self.tool_results: list[str] = []
        self._findings = (_CANNED_FINDING,) if findings is None else tuple(findings)
        self._raw_output = raw_output
        self._error = error
        self._warnings = tuple(warnings)
        self._usage = TokenUsage(0, 0) if usage is None else usage
        self._tool_calls = tuple(tool_calls)
        self._cost_usd = cost_usd

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        """Record the request, make the scripted tool calls, and return or raise as scripted."""
        self.requests.append(request)
        if request.tools is not None:
            for name, arguments in self._tool_calls:
                self.tool_results.append(await request.tools.call(name, arguments))
        if self._error is not None:
            raise self._error
        findings = self._findings if self._raw_output is None else parse_findings(self._raw_output)
        return ReviewResponse(
            findings=findings, usage=self._usage, warnings=self._warnings, cost_usd=self._cost_usd
        )
