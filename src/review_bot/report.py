"""A review's result, its triage, and how its diff paths are written in machine-readable output.

Pure: the audit log and the JSON report both write paths with these helpers, and the audit log
redacts them first.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from review_bot.context.ledger import ToolCall
from review_bot.diff_parser import FileDiff, ParsedDiff
from review_bot.findings import ReviewedFinding
from review_bot.path_quoting import c_quoted, has_lone_surrogate
from review_bot.policy import Decision
from review_bot.providers.base import TokenUsage
from review_bot.verify import VerifyResult

__all__ = [
    "PipelineResult",
    "TriageDecision",
    "TriagedFinding",
    "not_rejected",
    "path_fields",
    "section_path",
]

# What a section's headers name when they say /dev/null for both the old and the new file.
_NO_PATH: Final = "/dev/null"


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """A review's verified and decided findings, and what it skipped.

    ``parsed`` is the reviewed diff, which every finding is anchored in. ``model_called`` is
    False when every file was skipped; ``verification`` and ``decision`` are then empty, as are
    ``warnings``. ``warnings`` are the provider's, already written to stderr and the audit log.
    ``prompt_version`` is the version of the prompt the review used. ``context_commit`` is the
    commit the tools read in context mode, and None in diff mode; ``ledger`` holds each tool
    call, in order. ``cost_usd`` is the call's cost as the provider reported it, or None.
    """

    verification: VerifyResult
    decision: Decision
    usage: TokenUsage
    skipped_files: tuple[FileDiff, ...]
    parsed: ParsedDiff
    model_called: bool
    prompt_version: str
    warnings: tuple[str, ...] = ()
    context_commit: str | None = None
    ledger: tuple[ToolCall, ...] = ()
    cost_usd: Decimal | None = None


class TriageDecision(StrEnum):
    """A person's call on a reported finding, or ``undecided`` if triage stopped before it."""

    ACCEPT = "accept"
    REJECT = "reject"
    EDIT = "edit"
    UNDECIDED = "undecided"


@dataclass(frozen=True, slots=True)
class TriagedFinding:
    """A reported finding, with any edit applied, and the call triage made on it.

    ``original_rationale`` is the rationale an edit replaced, or None if it wasn't changed.
    """

    reviewed: ReviewedFinding
    decision: TriageDecision
    original_rationale: str | None = None


def not_rejected(triaged: Iterable[TriagedFinding]) -> tuple[ReviewedFinding, ...]:
    """The findings a person didn't reject, undecided ones included, in order."""
    return tuple(item.reviewed for item in triaged if item.decision is not TriageDecision.REJECT)


def section_path(file: FileDiff) -> str:
    """The path naming a file's section: its new path, else its old one, else ``/dev/null``.

    So a deleted file is named by its old path, and never by null.
    """
    if file.path is not None:
        return file.path
    return _NO_PATH if file.old_path is None else file.old_path


def path_fields(path: str) -> dict[str, str | bool]:
    """Return ``file`` for a diff path, plus ``path_quoted`` if it had to be quoted.

    A valid UTF-8 path is written as it is. A path holding lone surrogates, which stand for bytes
    that aren't valid UTF-8 (``surrogateescape``), is written in git's C-quoted form (Phase 1
    design, Paths in audit/v1), so it stays portable JSON.

    Raises:
        UnicodeEncodeError: If ``path`` holds a surrogate that ``surrogateescape`` can't produce.
    """
    if has_lone_surrogate(path):
        return {"file": c_quoted(path.encode("utf-8", "surrogateescape")), "path_quoted": True}
    return {"file": path}
