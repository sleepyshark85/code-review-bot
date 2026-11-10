"""Builders of paired ``shadow/v1`` reports for the compare unit tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from shadow_samples import report

HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40
_FINISHED = datetime(2026, 11, 9, 12, tzinfo=UTC)


def pr_report(  # noqa: PLR0913 - keyword-only facts of one sample report
    number: int,
    head_sha: str,
    *entries: dict[str, Any],
    cost_usd: Decimal | None = None,
    tool_calls: int | None = None,
    input_tokens: int = 1000,
    output_tokens: int = 100,
    review_s: int = 10,
    finished_minute: int = 0,
    **fields: Any,
) -> dict[str, Any]:
    """A reviewed shadow report of PR ``number`` at ``head_sha``, with ``fields`` replaced."""
    finished = _FINISHED + timedelta(minutes=finished_minute)
    usage: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if cost_usd is not None:
        usage["cost_usd"] = str(cost_usd)
    review: dict[str, Any] = {
        "schema": "report/v1",
        "model": "claude-x",
        "prompt_version": "p1-v1",
        "usage": usage,
    }
    if tool_calls is not None:
        review["context"] = {"commit": head_sha, "tool_calls": tool_calls}
    document = report(
        *entries,
        finished_at=finished.isoformat(),
        number=number,
        started_at=(finished - timedelta(seconds=review_s)).isoformat(),
        report=review,
    )
    document["pr"]["head_sha"] = head_sha
    return document | fields
