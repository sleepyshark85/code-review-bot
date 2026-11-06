"""Orchestrate one review: load, scan and parse the diff, build the prompt, call the provider."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from review_bot.diff_input import load_diff
from review_bot.diff_parser import FileDiff, parse_diff
from review_bot.errors import DiffNotParsableError, SecretDetectedError
from review_bot.findings import Finding
from review_bot.prompt import build_request
from review_bot.providers.base import LLMProvider, TokenUsage
from review_bot.secret_scan import SecretHit, scan_secrets
from review_bot.settings import Settings

__all__ = ["ReviewResult", "review_diff"]

_MAX_NAMED_SECRETS: Final = 5


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
        InputError: If the diff can't be reviewed, or holds a possible secret; the provider is
            not called.
    """
    diff = await asyncio.to_thread(load_diff, diff_path, max_bytes=settings.max_diff_bytes)
    # Before the parser, so nothing the parser misreads or drops can carry a secret past the scan.
    hits = scan_secrets(diff)
    if hits:
        raise SecretDetectedError(_secrets_message(hits))
    try:
        parsed = parse_diff(diff)
    except DiffNotParsableError as exc:
        raise DiffNotParsableError(f"Diff {diff_path} can't be parsed. {exc}") from exc

    skipped = tuple(file for file in parsed.files if file.skip_reason is not None)
    if len(skipped) == len(parsed.files):
        return ReviewResult((), TokenUsage(0, 0), skipped, model_called=False)

    response = await provider.review(build_request(parsed.reviewable_text, model=settings.model))
    return ReviewResult(response.findings, response.usage, skipped, model_called=True)


def _secrets_message(hits: Sequence[SecretHit]) -> str:
    """Say where the secrets are, without their values or the diff's path, which may hold one."""
    named = [f"{hit.kind} at diff line {hit.diff_line}" for hit in hits[:_MAX_NAMED_SECRETS]]
    if len(hits) > _MAX_NAMED_SECRETS:
        named.append(f"{len(hits) - _MAX_NAMED_SECRETS} more")
    if len(hits) == 1:
        found, advice = "1 possible secret", "Remove it and rotate it if it is real"
    else:
        found, advice = f"{len(hits)} possible secrets", "Remove them and rotate any that are real"
    return (
        f"secret: {found} in the diff ({'; '.join(named)}), so it was not sent to the model. "
        f"{advice}, then run again."
    )
