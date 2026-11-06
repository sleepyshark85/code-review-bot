"""Orchestrate one review: load and parse the diff, build the prompt, call the provider."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from review_bot.diff_input import load_diff
from review_bot.diff_parser import FileDiff, parse_diff
from review_bot.errors import DiffNotParsableError
from review_bot.findings import Finding
from review_bot.prompt import build_request
from review_bot.providers.base import LLMProvider, TokenUsage
from review_bot.settings import Settings

__all__ = ["ReviewResult", "review_diff"]


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """A review's findings, and what it skipped.

    ``model_called`` is False when every file was skipped; the findings are then empty.
    """

    findings: tuple[Finding, ...]
    usage: TokenUsage
    skipped_files: tuple[FileDiff, ...]
    model_called: bool


async def review_diff(diff_path: Path, settings: Settings, provider: LLMProvider) -> ReviewResult:
    """Review the diff at ``diff_path`` with ``provider``, sending only the files not skipped.

    Raises:
        InputError: If the diff can't be reviewed; the provider is not called.
    """
    diff = await asyncio.to_thread(load_diff, diff_path, max_bytes=settings.max_diff_bytes)
    try:
        parsed = parse_diff(diff)
    except DiffNotParsableError as exc:
        raise DiffNotParsableError(f"Diff {diff_path} can't be parsed. {exc}") from exc

    skipped = tuple(file for file in parsed.files if file.skip_reason is not None)
    if len(skipped) == len(parsed.files):
        return ReviewResult((), TokenUsage(0, 0), skipped, model_called=False)

    response = await provider.review(build_request(parsed.reviewable_text, model=settings.model))
    return ReviewResult(response.findings, response.usage, skipped, model_called=True)
