"""The ledger: what each tool call returned, with no file content and no model text (ADR 0014).

Pure.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "LineSpan",
    "RefusalReason",
    "ToolCall",
    "ToolName",
    "ToolOutcome",
    "merged_spans_by_file",
]


class ToolName(StrEnum):
    """The repository tools the model can call."""

    READ = "read"
    GREP = "grep"
    GLOB = "glob"


class ToolOutcome(StrEnum):
    """How a tool call ended."""

    OK = "ok"
    TRUNCATED = "truncated"
    REFUSED = "refused"


class RefusalReason(StrEnum):
    """Why a tool call was refused."""

    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_PATH = "invalid_path"
    DENIED_PATH = "denied_path"
    NOT_FOUND = "not_found"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    SUBMODULE = "submodule"
    BINARY = "binary"
    TOO_LARGE = "too_large"
    LISTING_TOO_LARGE = "listing_too_large"
    POSSIBLE_SECRET = "possible_secret"  # noqa: S105 - a reason's name, not a secret
    TIMEOUT = "timeout"
    GIT_FAILED = "git_failed"
    CALL_LIMIT = "call_limit"
    BYTE_LIMIT = "byte_limit"
    POLICY_DENIED = "policy_denied"


@dataclass(frozen=True, slots=True)
class LineSpan:
    """Lines ``first_line`` to ``last_line`` of ``path``, both included, counted from 1."""

    path: str
    first_line: int
    last_line: int


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call.

    ``tool`` is None when the model named no tool of ours, so its text isn't kept. ``spans``
    are the lines ``read`` and ``grep`` returned, ``listed`` the paths ``glob`` returned.
    ``result_bytes`` is the UTF-8 size of the result's text, without its delimiters.
    ``input_sha256`` is the SHA-256 of the arguments as canonical JSON.
    """

    tool: ToolName | None
    outcome: ToolOutcome
    reason: RefusalReason | None
    spans: tuple[LineSpan, ...]
    listed: tuple[str, ...]
    result_bytes: int
    input_sha256: str


def merged_spans_by_file(spans: Iterable[LineSpan]) -> dict[str, list[LineSpan]]:
    """Group ``spans`` by path, sorted by path, merging the spans that overlap or touch.

    Each path's spans are sorted by first line, whatever order ``spans`` come in.
    """
    by_path: defaultdict[str, list[LineSpan]] = defaultdict(list)
    for span in spans:
        by_path[span.path].append(span)
    return {path: _merged(by_path[path]) for path in sorted(by_path)}


def _merged(spans: list[LineSpan]) -> list[LineSpan]:
    merged: list[LineSpan] = []
    for span in sorted(spans, key=lambda item: (item.first_line, item.last_line)):
        if merged and span.first_line <= merged[-1].last_line + 1:
            last = merged[-1]
            merged[-1] = LineSpan(span.path, last.first_line, max(last.last_line, span.last_line))
        else:
            merged.append(span)
    return merged
