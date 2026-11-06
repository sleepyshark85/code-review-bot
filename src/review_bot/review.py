"""Orchestrate one review: load, scan and parse the diff, call the provider, record each stage."""

import asyncio
import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Final

from review_bot.audit import AuditLog, JsonValue, Stage, file_fields
from review_bot.diff_input import load_diff
from review_bot.diff_parser import FileDiff, ParsedDiff, parse_diff
from review_bot.errors import DiffNotParsableError, ProviderError, SecretDetectedError
from review_bot.findings import Finding
from review_bot.prompt import PROMPT_VERSION, build_request
from review_bot.providers.base import LLMProvider, TokenUsage
from review_bot.secret_scan import SecretHit, scan_secrets
from review_bot.settings import Settings

__all__ = ["PipelineResult", "run_pipeline"]

_MAX_NAMED_SECRETS: Final = 5
# Caps each list in the parse record, so its size doesn't grow with the diff.
_MAX_LISTED_IN_AUDIT: Final = 50
# What a section's headers name when they say /dev/null for both the old and the new file.
_NO_PATH: Final = "/dev/null"


class _AgentSkipReason(StrEnum):
    """Why the model wasn't called."""

    SECRET_DETECTED = "secret_detected"  # noqa: S105 - a reason's name, not a secret
    NOTHING_TO_REVIEW = "nothing_to_review"


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """A review's findings, and what it skipped.

    ``model_called`` is False when every file was skipped; the findings are then empty.
    """

    findings: tuple[Finding, ...]
    usage: TokenUsage
    skipped_files: tuple[FileDiff, ...]
    model_called: bool


async def run_pipeline(
    diff_path: Path, settings: Settings, provider: LLMProvider, audit: AuditLog
) -> PipelineResult:
    """Review the diff at ``diff_path`` with ``provider``, sending only the files not skipped.

    Each stage's decision goes to ``audit``; the run's end is the caller's to record.

    Raises:
        InputError: If the diff can't be reviewed, or holds a possible secret; the provider is
            not called.
        ProviderError: If the provider fails.
        AuditError: If a record can't be written.
    """
    diff = await asyncio.to_thread(load_diff, diff_path, max_bytes=settings.max_diff_bytes)
    audit.record(Stage.RUN, "start", _start_data(settings, diff))
    # Before the parser, so nothing the parser misreads or drops can carry a secret past the scan.
    hits = scan_secrets(diff)
    if hits:
        audit.record(Stage.STATIC, "secrets", _secrets_data(hits))
        audit.record(Stage.AGENT, "skipped", {"reason": _AgentSkipReason.SECRET_DETECTED})
        raise SecretDetectedError(_secrets_message(hits))
    try:
        parsed = parse_diff(diff)
    except DiffNotParsableError as exc:
        raise DiffNotParsableError(f"Diff {diff_path} can't be parsed. {exc}") from exc
    audit.record(Stage.PARSE, "parsed", _parsed_data(parsed))

    skipped = tuple(file for file in parsed.files if file.skip_reason is not None)
    if len(skipped) == len(parsed.files):
        audit.record(Stage.AGENT, "skipped", {"reason": _AgentSkipReason.NOTHING_TO_REVIEW})
        return PipelineResult((), TokenUsage(0, 0), skipped, model_called=False)

    started = time.perf_counter()
    try:
        response = await provider.review(
            build_request(parsed.reviewable_text, model=settings.model)
        )
    except ProviderError as exc:
        audit.record(Stage.AGENT, "failed", {"category": exc.category})
        raise
    audit.record(
        Stage.AGENT,
        "called",
        {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "duration_s": round(time.perf_counter() - started, 3),
        },
    )
    return PipelineResult(response.findings, response.usage, skipped, model_called=True)


def _start_data(settings: Settings, diff: str) -> dict[str, JsonValue]:
    # ``load_diff`` decoded the file as strict UTF-8, so encoding gives back its exact bytes.
    diff_bytes = diff.encode("utf-8")
    return {
        "version": version("review-bot"),
        "provider": settings.provider,
        "model": settings.model,
        "prompt_version": PROMPT_VERSION,
        "threshold": settings.threshold,
        "diff_sha256": hashlib.sha256(diff_bytes).hexdigest(),
        "diff_bytes": len(diff_bytes),
    }


def _secrets_data(hits: Sequence[SecretHit]) -> dict[str, JsonValue]:
    return {"hits": [{"kind": hit.kind, "diff_line": hit.diff_line} for hit in hits]}


def _parsed_data(parsed: ParsedDiff) -> dict[str, JsonValue]:
    """Counts, then skipped files and flagged hunks in diff order, capped, with their totals.

    A hunk is named by its file, its 0-based index within the file and its declared new start,
    never by its header text, which the diff's author controls. Only listed entries are built, so
    a long path costs at most one entry per list slot.
    """
    skipped = [file for file in parsed.files if file.skip_reason is not None]
    flagged = [
        (file, index, hunk)
        for file in parsed.files
        for index, hunk in enumerate(file.hunks)
        if hunk.has_count_mismatch
    ]
    return {
        "files": len(parsed.files),
        "hunks": sum(len(file.hunks) for file in parsed.files),
        "skipped_files": [
            {**_logged_path_fields(file), "reason": file.skip_reason}
            for file in skipped[:_MAX_LISTED_IN_AUDIT]
        ],
        "skipped_files_total": len(skipped),
        "flagged_hunks": [
            {**_logged_path_fields(file), "index": index, "new_start": hunk.new_start}
            for file, index, hunk in flagged[:_MAX_LISTED_IN_AUDIT]
        ],
        "flagged_hunks_total": len(flagged),
    }


def _logged_path_fields(file: FileDiff) -> dict[str, JsonValue]:
    """Name a deleted file by its old path, so no entry's ``file`` is null."""
    if file.path is not None:
        return file_fields(file.path)
    return file_fields(_NO_PATH if file.old_path is None else file.old_path)


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
