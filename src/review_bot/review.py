"""Orchestrate one review: load the diff, build the prompt, call the provider."""

import asyncio
from pathlib import Path

from review_bot.diff_input import load_diff
from review_bot.prompt import build_request
from review_bot.providers.base import LLMProvider, ReviewResponse
from review_bot.settings import Settings

__all__ = ["review_diff"]


async def review_diff(diff_path: Path, settings: Settings, provider: LLMProvider) -> ReviewResponse:
    """Review the diff at ``diff_path`` with ``provider``.

    Raises:
        InputError: If the diff can't be reviewed; the provider is not called.
    """
    diff = await asyncio.to_thread(load_diff, diff_path, max_bytes=settings.max_diff_bytes)
    return await provider.review(build_request(diff, model=settings.model))
