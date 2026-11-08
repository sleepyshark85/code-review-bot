"""The ``read``, ``grep`` and ``glob`` tools over one commit, with caps and a ledger (ADR 0013).

Every argument comes from the model, so each call is checked against its tool's schema before
git runs, and every way a call can fail returns a refusal rather than raising (ADR 0014).
"""

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_bot.context.ledger import LineSpan, RefusalReason, ToolCall, ToolName, ToolOutcome
from review_bot.diff_parser import glob_matches
from review_bot.errors import GitError, NotARegularFileError
from review_bot.git import GIT_FATAL_EXIT_CODE, GitRepo
from review_bot.prompt import frame_tool_result
from review_bot.providers.base import ToolDefinition
from review_bot.report import path_fields

__all__ = ["RepoRef", "RepoTools", "ToolCaps"]

_MAX_LINE_CHARS: Final = 2000
_CUT_MARK: Final = " (cut)"
_MAX_GREP_MATCHES: Final = 50
_MAX_GLOB_PATHS: Final = 200
_MAX_BLOB_BYTES: Final = 1_000_000
# How much of ``git grep``'s output is read: far more than 50 matches, so ``path_glob`` has
# plenty to filter.
_MAX_GREP_OUTPUT_BYTES: Final = 1_000_000
_MAX_LISTED_ENTRIES: Final = 50_000
# Every note fits in this, so a capped result stays within its cap.
_MIN_RESULT_BYTES: Final = 1000
_REGULAR_FILE_MODES: Final = frozenset({"100644", "100755"})
_NOT_A_FILE_REASONS: Final = {
    "directory": RefusalReason.DIRECTORY,
    "symbolic link": RefusalReason.SYMLINK,
    "submodule": RefusalReason.SUBMODULE,
}

_BYTES_NOTE: Final = "(truncated: the result reached max_tool_result_bytes; ask for less)"
_MORE_MATCHES_NOTE: Final = (
    "(truncated: more lines match than are shown; narrow the search with path_glob or a longer "
    "literal)"
)
_MORE_PATHS_NOTE: Final = (
    f"(truncated: more than {_MAX_GLOB_PATHS} files match; narrow the pattern)"
)


@dataclass(frozen=True, slots=True)
class RepoRef:
    """One commit of a clone: ``commit`` is its full SHA, resolved before the review starts."""

    git: GitRepo
    commit: str


@dataclass(frozen=True, slots=True)
class ToolCaps:
    """A review's tool caps: calls, bytes per result, and bytes over all results.

    Raises:
        ValueError: If a cap is below 1, or ``max_result_bytes`` below 1,000, the least that
            holds a result's notes.
    """

    max_calls: int
    max_result_bytes: int
    max_total_bytes: int

    def __post_init__(self) -> None:
        """Check each cap, as the class says."""
        if min(self.max_calls, self.max_total_bytes) < 1:
            raise ValueError("Tool caps must be at least 1.")
        if self.max_result_bytes < _MIN_RESULT_BYTES:
            raise ValueError(f"max_result_bytes must be at least {_MIN_RESULT_BYTES}.")


# For strings that reach git's command line, which can't take a NUL. A path holds no control
# character at all; a literal may hold a tab, as indented code does, but no line break.
_NO_CONTROL_CHARACTERS: Final = r"^[^\x00-\x1f\x7f]*$"
_ONE_LINE: Final = r"^[^\x00-\x08\x0a-\x1f\x7f]*$"


class _Arguments(BaseModel):
    # Strict, so ``"10"`` isn't taken for a line number, nor ``true`` for 1.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _ReadArguments(_Arguments):
    path: str = Field(
        min_length=1,
        max_length=1000,
        pattern=_NO_CONTROL_CHARACTERS,
        description="The file's path from the repository's root, with / between segments.",
    )
    start_line: int = Field(default=1, ge=1, description="The first line to return, from 1.")
    line_count: int = Field(default=200, ge=1, le=400, description="How many lines to return.")


class _GrepArguments(_Arguments):
    literal: str = Field(
        min_length=1,
        max_length=200,
        pattern=_ONE_LINE,
        description="The exact text to find, on one line: not a regular expression.",
    )
    path_glob: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description="Search only files whose path matches this glob, as glob matches it.",
    )
    ignore_case: bool = Field(default=False, description="Whether to ignore ASCII case.")


class _GlobArguments(_Arguments):
    pattern: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "A glob: * matches within one path segment and ** any number of segments. A pattern "
            "with no / matches file names at any depth; one with a / matches from the root."
        ),
    )


_DEFINITIONS: Final = (
    ToolDefinition(
        name=ToolName.READ,
        description=(
            "Read up to 400 numbered lines of one file at the commit, each as <line>, a tab, "
            "and the line's text."
        ),
        input_schema=_ReadArguments.model_json_schema(),
    ),
    ToolDefinition(
        name=ToolName.GREP,
        description=(
            f"Find lines holding a literal string in the commit's text files, as path:line:text, "
            f"sorted by path then line, at most {_MAX_GREP_MATCHES}."
        ),
        input_schema=_GrepArguments.model_json_schema(),
    ),
    ToolDefinition(
        name=ToolName.GLOB,
        description=(
            f"List the commit's file paths matching a glob, sorted, one per line, at most "
            f"{_MAX_GLOB_PATHS}."
        ),
        input_schema=_GlobArguments.model_json_schema(),
    ),
)


@dataclass(frozen=True, slots=True)
class _Answer:
    """A call's result text, and what goes in its ledger entry."""

    text: str
    outcome: ToolOutcome
    reason: RefusalReason | None = None
    spans: tuple[LineSpan, ...] = ()
    listed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _GrepMatch:
    path: str
    line: int
    text: str


class RepoTools:
    """The three read-only tools over one commit, recording every call in a ledger."""

    def __init__(self, repo: RepoRef, *, caps: ToolCaps) -> None:
        """Serve ``repo`` within ``caps``; git runs only when a tool is called."""
        self._repo = repo
        self._caps = caps
        self._ledger: list[ToolCall] = []
        self._calls_started = 0
        self._bytes_returned = 0
        self._is_out_of_bytes = False

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """``read``, ``grep`` and ``glob``."""
        return _DEFINITIONS

    @property
    def ledger(self) -> tuple[ToolCall, ...]:
        """Every call so far, in the order each finished."""
        return tuple(self._ledger)

    async def call(self, name: str, arguments: Mapping[str, object]) -> str:
        """Run one call, record it, and return its result between delimiter lines.

        Never raises on model input: a bad call returns ``refused: <reason>: <sentence>``.

        Raises:
            GitError: If git times out or can't start, fails during a ``grep`` or ``glob``, or
                writes output that can't be read.
        """
        tool = ToolName(name) if name in ToolName else None
        answer = await self._answer(tool, arguments)
        self._ledger.append(
            ToolCall(
                tool=tool,
                outcome=answer.outcome,
                reason=answer.reason,
                spans=answer.spans,
                listed=answer.listed,
                result_bytes=_utf8_size(answer.text),
                input_sha256=_input_sha256(arguments),
            )
        )
        return frame_tool_result(answer.text)

    async def _answer(self, tool: ToolName | None, arguments: Mapping[str, object]) -> _Answer:
        """Check the review's caps and the tool's name, then run the tool in a thread."""
        self._calls_started += 1
        if self._calls_started > self._caps.max_calls:
            return _refused(
                RefusalReason.CALL_LIMIT,
                "This review's tool calls are used up; give your findings now.",
            )
        if self._is_out_of_bytes:
            return _out_of_bytes()
        if tool is None:
            return _refused(
                RefusalReason.UNKNOWN_TOOL, "There is no such tool; use read, grep or glob."
            )
        answer = await asyncio.to_thread(self._run, tool, arguments)
        if answer.outcome is ToolOutcome.REFUSED:
            return answer
        size = _utf8_size(answer.text)
        if self._bytes_returned + size > self._caps.max_total_bytes:
            self._is_out_of_bytes = True
            return _out_of_bytes()
        self._bytes_returned += size
        return answer

    def _run(self, tool: ToolName, arguments: Mapping[str, object]) -> _Answer:
        match tool:
            case ToolName.READ:
                return self._read(arguments)
            case ToolName.GREP:
                return self._grep(arguments)
            case ToolName.GLOB:
                return self._glob(arguments)

    def _read(self, arguments: Mapping[str, object]) -> _Answer:
        """Numbered lines of one file, from ``start_line``, at most ``line_count`` of them."""
        args = _parsed(_ReadArguments, arguments)
        if args is None:
            return _invalid_arguments()
        blob = self._blob(args.path)
        if isinstance(blob, _Answer):
            return blob
        lines = _text_lines(blob)
        first = args.start_line
        if first > len(lines):
            return _Answer(
                f"start_line {first} is past the end of the file, whose line count is "
                f"{len(lines)}.",
                ToolOutcome.OK,
            )
        window = lines[first - 1 : first - 1 + args.line_count]
        numbered = [f"{number}\t{_cut(line)}" for number, line in enumerate(window, start=first)]
        kept, text = _fit(numbered, (), self._caps.max_result_bytes)
        spans = (LineSpan(args.path, first, first + kept - 1),) if kept else ()
        return _Answer(text, _outcome(is_cut=kept < len(numbered)), spans=spans)

    def _blob(self, path: str) -> bytes | _Answer:
        """The bytes of the file at ``path``, or the refusal saying why it can't be read.

        The path goes to ``git ls-tree`` as a pathspec, and the first entry listed is read, so
        a pattern, or a directory with a trailing ``/``, can read a file of another name until
        P3-S2 requires an exact match. A path git rejects (exit 128) is refused ``invalid_path``.

        Raises:
            GitError: If git times out or can't start.
        """
        try:
            blob = self._repo.git.read_file(self._repo.commit, path, max_bytes=_MAX_BLOB_BYTES)
        except NotARegularFileError as exc:
            reason = _NOT_A_FILE_REASONS.get(exc.kind, RefusalReason.NOT_FOUND)
            return _refused(reason, f"The path names a {exc.kind}, not a regular file.")
        except GitError as exc:
            # A fatal error on the path, such as ``../x`` or ``/x``, outside the repository. A
            # timeout or a missing git is no fault of the path's, so it propagates (P3-S2 AC9).
            if exc.exit_code != GIT_FATAL_EXIT_CODE:
                raise
            return _refused(
                RefusalReason.INVALID_PATH,
                "git couldn't read that path; give a path relative to the repository's root.",
            )
        if blob is None:
            return _refused(RefusalReason.NOT_FOUND, "The commit has no file at that path.")
        if len(blob) > _MAX_BLOB_BYTES:
            return _refused(
                RefusalReason.TOO_LARGE,
                f"The file is over {_MAX_BLOB_BYTES} bytes, too large to read.",
            )
        return blob

    def _grep(self, arguments: Mapping[str, object]) -> _Answer:
        """Lines holding a literal, as ``path:line:text``, by path then line, at most 50."""
        args = _parsed(_GrepArguments, arguments)
        if args is None:
            return _invalid_arguments()
        commit = self._repo.commit
        raw = self._repo.git.grep(
            commit, args.literal, ignore_case=args.ignore_case, max_bytes=_MAX_GREP_OUTPUT_BYTES
        )
        path_glob = args.path_glob
        matches = sorted(
            (
                match
                for match in _grep_matches(raw, commit)
                if path_glob is None or glob_matches(match.path, path_glob)
            ),
            key=lambda match: (match.path, match.line),
        )
        has_more = len(raw) > _MAX_GREP_OUTPUT_BYTES or len(matches) > _MAX_GREP_MATCHES
        shown = matches[:_MAX_GREP_MATCHES]
        if not shown and not has_more:
            return _Answer("No line matches.", ToolOutcome.OK)
        lines = [f"{_shown_path(match.path)}:{match.line}:{_cut(match.text)}" for match in shown]
        notes = [_MORE_MATCHES_NOTE] if has_more else []
        kept, text = _fit(lines, notes, self._caps.max_result_bytes)
        spans = tuple(LineSpan(match.path, match.line, match.line) for match in shown[:kept])
        return _Answer(text, _outcome(is_cut=has_more or kept < len(lines)), spans=spans)

    def _glob(self, arguments: Mapping[str, object]) -> _Answer:
        """The regular files whose paths match a pattern, sorted, at most 200."""
        args = _parsed(_GlobArguments, arguments)
        if args is None:
            return _invalid_arguments()
        entries = self._repo.git.list_files(self._repo.commit, max_entries=_MAX_LISTED_ENTRIES)
        paths = sorted(
            entry.path
            for entry in entries
            if entry.mode in _REGULAR_FILE_MODES and glob_matches(entry.path, args.pattern)
        )
        if not paths:
            return _Answer("No file matches.", ToolOutcome.OK)
        shown = paths[:_MAX_GLOB_PATHS]
        has_more = len(paths) > _MAX_GLOB_PATHS
        notes = [_MORE_PATHS_NOTE] if has_more else []
        kept, text = _fit([_shown_path(path) for path in shown], notes, self._caps.max_result_bytes)
        return _Answer(
            text, _outcome(is_cut=has_more or kept < len(shown)), listed=tuple(shown[:kept])
        )


def _parsed[T: _Arguments](model: type[T], arguments: Mapping[str, object]) -> T | None:
    """``arguments`` as ``model``, or None if they don't fit its schema."""
    try:
        return model.model_validate(dict(arguments))
    except ValidationError:
        return None


def _grep_matches(raw: bytes, commit: str) -> list[_GrepMatch]:
    """Parse ``git grep -n -z`` output, leaving out a last record cut short by the read's cap.

    Each record is ``<commit>:<path>``, a NUL, the line number, a NUL, then the line up to a line
    feed. A path can hold a line feed and a line a NUL, so the fields are found in order.

    Raises:
        GitError: If a complete record isn't in that form.
    """
    prefix = f"{commit}:".encode("ascii")
    matches: list[_GrepMatch] = []
    position = 0
    while True:
        path_end = raw.find(b"\0", position)
        line_end = -1 if path_end == -1 else raw.find(b"\0", path_end + 1)
        text_end = -1 if line_end == -1 else raw.find(b"\n", line_end + 1)
        if text_end == -1:
            return matches
        path, number = raw[position:path_end], raw[path_end + 1 : line_end]
        if not path.startswith(prefix) or not number.isdigit():
            raise GitError("git grep wrote output that can't be read.")
        matches.append(
            _GrepMatch(
                path=path.removeprefix(prefix).decode("utf-8", "surrogateescape"),
                line=int(number),
                text=raw[line_end + 1 : text_end].decode("utf-8", "replace"),
            )
        )
        position = text_end + 1


def _fit(lines: Sequence[str], notes: Sequence[str], max_bytes: int) -> tuple[int, str]:
    """How many leading ``lines`` fit in ``max_bytes`` with ``notes`` after them, and the text.

    If they don't all fit, a note says the result was truncated, and the text ends at a line
    boundary, within ``max_bytes`` as long as the notes alone fit.
    """
    text = "\n".join([*lines, *notes])
    if _utf8_size(text) <= max_bytes:
        return len(lines), text
    notes = [*notes, _BYTES_NOTE]
    budget = max_bytes - _utf8_size("\n".join(notes))
    kept = 0
    for line in lines:
        budget -= _utf8_size(line) + 1  # the line and the line feed after it
        if budget < 0:
            break
        kept += 1
    return kept, "\n".join([*lines[:kept], *notes])


def _text_lines(blob: bytes) -> list[str]:
    """The blob's lines, split on line feeds as git numbers them, without the line feeds."""
    lines = blob.decode("utf-8", "replace").split("\n")
    if lines[-1] == "":
        lines.pop()  # after the last line feed, or the whole of an empty file
    return lines


def _cut(line: str) -> str:
    if len(line) <= _MAX_LINE_CHARS:
        return line
    return line[:_MAX_LINE_CHARS] + _CUT_MARK


def _shown_path(path: str) -> str:
    """A path as a result shows it: C-quoted if it isn't valid UTF-8, so the text encodes."""
    return str(path_fields(path)["file"])


def _outcome(*, is_cut: bool) -> ToolOutcome:
    return ToolOutcome.TRUNCATED if is_cut else ToolOutcome.OK


def _refused(reason: RefusalReason, sentence: str) -> _Answer:
    return _Answer(f"refused: {reason}: {sentence}", ToolOutcome.REFUSED, reason)


def _invalid_arguments() -> _Answer:
    return _refused(
        RefusalReason.INVALID_ARGUMENTS,
        "The arguments don't fit the tool's input schema; check their names, types and limits.",
    )


def _out_of_bytes() -> _Answer:
    return _refused(
        RefusalReason.BYTE_LIMIT,
        "The bytes tools may return in this review are used up; give your findings now.",
    )


def _utf8_size(text: str) -> int:
    # Paths are C-quoted and file text decoded with replacement, so the text always encodes.
    return len(text.encode("utf-8"))


def _input_sha256(arguments: Mapping[str, object]) -> str:
    """The SHA-256 of ``arguments`` as canonical JSON: sorted keys, ASCII, no spaces."""
    canonical = json.dumps(
        arguments, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=repr
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
