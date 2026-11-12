"""The ``read``, ``grep`` and ``glob`` tools over one commit, with caps and a ledger (ADR 0013).

Every argument comes from the model, so each call is checked against its tool's schema and the
path rules before git runs, and every way a call can fail returns a refusal rather than raising
(ADR 0014). No file under ``.git``, secrets file, symbolic link or submodule, and no line that
may hold a secret, reaches a result (Phase 3 design, Tool safety). Personal data of the kinds the
policy names is masked in every result (P4-S4).
"""

import asyncio
import hashlib
import itertools
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from review_bot.context.commit_files import MAX_LISTED_ENTRIES, CommitFiles, Refusal
from review_bot.context.ledger import LineSpan, RefusalReason, ToolCall, ToolName, ToolOutcome
from review_bot.context.paths import is_valid_glob, shown_path
from review_bot.context.tool_args import (
    MAX_GLOB_PATHS,
    MAX_GREP_MATCHES,
    TOOL_DEFINITIONS,
    GlobArguments,
    GrepArguments,
    ReadArguments,
    parse_arguments,
)
from review_bot.context.tool_text import (
    cut_line,
    fit_lines,
    overlaps_any,
    text_lines,
    utf8_size,
)
from review_bot.diff_parser import glob_matches
from review_bot.errors import GitError, GitTimeoutError, ListingTooLargeError
from review_bot.git import GitRepo
from review_bot.git_output import GrepMatch
from review_bot.governance.redaction import (
    Masked,
    RedactionCounts,
    RedactionKind,
    add_counts,
    mask_personal_data,
)
from review_bot.prompt import frame_tool_result
from review_bot.providers.base import ToolDefinition

__all__ = ["RepoRef", "RepoTools", "ToolCaps"]

# How many bytes of ``git grep``'s output one call keeps, counting only candidate files' matches:
# far more than 50 matches, so secret lines and unreadable files can drop out.
_MAX_GREP_OUTPUT_BYTES: Final = 1_000_000
# How many files with a match one grep reads at most, so its git processes are bounded.
_MAX_GREP_FILES: Final = 100
# Every note fits in this, so a capped result stays within its cap.
_MIN_RESULT_BYTES: Final = 1000

_MORE_MATCHES_NOTE: Final = (
    "(truncated: more lines match than are shown; narrow the search with path_glob or a longer "
    "literal)"
)
_MORE_PATHS_NOTE: Final = f"(truncated: more than {MAX_GLOB_PATHS} files match; narrow the pattern)"


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


@dataclass(frozen=True, slots=True)
class _Answer:
    """A call's result text, what goes in its ledger entry, and the values masked in the text."""

    text: str
    outcome: ToolOutcome
    reason: RefusalReason | None = None
    spans: tuple[LineSpan, ...] = ()
    listed: tuple[str, ...] = ()
    redacted: RedactionCounts = field(default_factory=dict)


class RepoTools:
    """The read-only tools a policy allows over one commit, recording every call in a ledger."""

    def __init__(
        self,
        repo: RepoRef,
        *,
        caps: ToolCaps,
        allowed: frozenset[ToolName] = frozenset(ToolName),
        redaction: frozenset[RedactionKind] = frozenset(),
    ) -> None:
        """Serve ``repo`` within ``caps``, offering only the ``allowed`` tools.

        Each result has the ``redaction`` kinds of personal data masked before it is fitted
        within the caps. Git runs only when a tool is called.
        """
        self._repo = repo
        self._caps = caps
        self._allowed = allowed
        self._redaction = redaction
        self._redacted: list[RedactionCounts] = []
        self._ledger: list[ToolCall] = []
        self._calls_started = 0
        self._bytes_returned = 0
        self._is_out_of_bytes = False
        self._files = CommitFiles(repo.git, repo.commit)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """The allowed tools, of ``read``, ``grep`` and ``glob`` in that order."""
        return tuple(tool for tool in TOOL_DEFINITIONS if tool.name in self._allowed)

    @property
    def ledger(self) -> tuple[ToolCall, ...]:
        """Every call so far, in the order each finished."""
        return tuple(self._ledger)

    @property
    def redacted(self) -> RedactionCounts:
        """How many values of each kind were masked in the results returned so far."""
        return add_counts(self._redacted)

    async def call(self, name: str, arguments: Mapping[str, object]) -> str:
        """Run one call, record it, and return its result between delimiter lines.

        Never raises on model input, nor when git fails or times out: such a call returns
        ``refused: <reason>: <sentence>``.
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
                result_bytes=utf8_size(answer.text),
                input_sha256=_input_sha256(arguments),
            )
        )
        return frame_tool_result(answer.text)

    async def _answer(self, tool: ToolName | None, arguments: Mapping[str, object]) -> _Answer:
        """Check the call may run, then run the tool in a thread, within the byte cap.

        Every call counts towards ``max_calls``, a refused one included.
        """
        self._calls_started += 1
        runnable = self._runnable(tool)
        if isinstance(runnable, _Answer):
            return runnable
        answer = await asyncio.to_thread(self._run, runnable, arguments)
        if answer.outcome is ToolOutcome.REFUSED:
            return answer
        size = utf8_size(answer.text)
        if self._bytes_returned + size > self._caps.max_total_bytes:
            self._is_out_of_bytes = True
            return _out_of_bytes()
        self._bytes_returned += size
        self._redacted.append(answer.redacted)
        return answer

    def _runnable(self, tool: ToolName | None) -> ToolName | _Answer:
        """``tool``, or the refusal if the review's caps are used up or the policy denies it."""
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
        if tool not in self._allowed:
            allowed = [definition.name for definition in self.definitions]
            offered = f"use {' or '.join(allowed)}" if allowed else "give your findings now"
            return _refused(
                RefusalReason.POLICY_DENIED,
                f"This review's policy doesn't allow the {tool} tool; {offered}.",
            )
        return tool

    def _run(self, tool: ToolName, arguments: Mapping[str, object]) -> _Answer:
        """Run one tool; a git failure refuses this call and leaves the review going."""
        try:
            match tool:
                case ToolName.READ:
                    return self._read(arguments)
                case ToolName.GREP:
                    return self._grep(arguments)
                case ToolName.GLOB:
                    return self._glob(arguments)
        except GitTimeoutError:
            return _refused(
                RefusalReason.TIMEOUT, "git took too long on this call; go on without it."
            )
        except ListingTooLargeError:
            return _refused(
                RefusalReason.LISTING_TOO_LARGE,
                f"The commit has more than {MAX_LISTED_ENTRIES} entries, or too long a listing "
                "of them, to search or list; read files by path instead.",
            )
        except GitError:
            return _refused(
                RefusalReason.GIT_FAILED, "git couldn't answer this call; go on without it."
            )

    def _read(self, arguments: Mapping[str, object]) -> _Answer:
        """Numbered lines of one file, from ``start_line``, at most ``line_count`` of them."""
        args = parse_arguments(ReadArguments, arguments)
        if args is None:
            return _invalid_arguments()
        file = self._files.text(args.path)
        if isinstance(file, Refusal):
            return _refused(file.reason, file.sentence)
        lines = text_lines(file.text)
        first = args.start_line
        if first > len(lines):
            return _Answer(
                f"start_line {first} is past the end of the file, whose line count is "
                f"{len(lines)}.",
                ToolOutcome.OK,
            )
        window = lines[first - 1 : first - 1 + args.line_count]
        if overlaps_any(file.secret_spans, first, first + len(window) - 1):
            return _refused(
                RefusalReason.POSSIBLE_SECRET,
                "Some of those lines may hold a secret, so none is shown; read other lines.",
            )
        masked = [self._masked(line) for line in window]
        numbered = [
            f"{number}\t{cut_line(line.text)}" for number, line in enumerate(masked, start=first)
        ]
        kept, result = fit_lines(numbered, (), self._caps.max_result_bytes)
        spans = (LineSpan(args.path, first, first + kept - 1),) if kept else ()
        return _Answer(
            result,
            _outcome(is_cut=kept < len(numbered)),
            spans=spans,
            redacted=add_counts(line.counts for line in masked[:kept]),
        )

    def _grep(self, arguments: Mapping[str, object]) -> _Answer:
        """Lines holding a literal, as ``path:line:text``, by path then line, at most 50."""
        args = parse_arguments(GrepArguments, arguments)
        if args is None:
            return _invalid_arguments()
        path_glob = args.path_glob
        if path_glob is not None and not is_valid_glob(path_glob):
            return _invalid_glob()
        listed = self._files.listed()

        def is_candidate(path: str) -> bool:
            return path in listed and (path_glob is None or glob_matches(path, path_glob))

        found = self._repo.git.grep(
            self._repo.commit,
            args.literal,
            ignore_case=args.ignore_case,
            keep=is_candidate,
            max_bytes=_MAX_GREP_OUTPUT_BYTES,
        )
        candidates = sorted(found.matches, key=lambda match: (match.path, match.line))
        matches, withheld, has_unread = self._shown_matches(candidates)
        has_more = found.is_cut or has_unread or len(matches) > MAX_GREP_MATCHES
        shown = matches[:MAX_GREP_MATCHES]
        notes = [_withheld_note(withheld)] if withheld else []
        notes += [_MORE_MATCHES_NOTE] if has_more else []
        if not shown and not notes:
            return _Answer("No line matches.", ToolOutcome.OK)
        masked = [
            (self._masked(shown_path(match.path)), self._masked(match.text)) for match in shown
        ]
        lines = [
            f"{path.text}:{match.line}:{cut_line(text.text)}"
            for match, (path, text) in zip(shown, masked, strict=True)
        ]
        kept, text = fit_lines(lines, notes, self._caps.max_result_bytes)
        spans = tuple(LineSpan(match.path, match.line, match.line) for match in shown[:kept])
        return _Answer(
            text,
            _outcome(is_cut=has_more or kept < len(lines)),
            spans=spans,
            redacted=add_counts(part.counts for parts in masked[:kept] for part in parts),
        )

    def _shown_matches(self, matches: Iterable[GrepMatch]) -> tuple[list[GrepMatch], int, bool]:
        """The ``matches`` a result may show, how many were withheld, and if files went unread.

        ``matches`` come sorted by path. A file whose text isn't shown, such as a binary one, is
        left out. No file is read once more than 50 matches are kept or 100 files were read, so
        the withheld count covers only the files read.

        Raises:
            GitError: If git fails, times out or can't start, or its listing can't be read.
        """
        shown: list[GrepMatch] = []
        withheld = 0
        by_file = itertools.groupby(matches, key=lambda match: match.path)
        for files_read, (path, file_matches) in enumerate(by_file):
            if len(shown) > MAX_GREP_MATCHES or files_read == _MAX_GREP_FILES:
                return shown, withheld, True
            file = self._files.text(path)
            if isinstance(file, Refusal):
                continue
            for match in file_matches:
                if overlaps_any(file.secret_spans, match.line, match.line):
                    withheld += 1
                else:
                    shown.append(match)
        return shown, withheld, False

    def _glob(self, arguments: Mapping[str, object]) -> _Answer:
        """The regular files whose paths match a pattern, sorted, at most 200."""
        args = parse_arguments(GlobArguments, arguments)
        if args is None:
            return _invalid_arguments()
        if not is_valid_glob(args.pattern):
            return _invalid_glob()
        paths = sorted(path for path in self._files.listed() if glob_matches(path, args.pattern))
        if not paths:
            return _Answer("No file matches.", ToolOutcome.OK)
        shown = paths[:MAX_GLOB_PATHS]
        has_more = len(paths) > MAX_GLOB_PATHS
        notes = [_MORE_PATHS_NOTE] if has_more else []
        masked = [self._masked(shown_path(path)) for path in shown]
        kept, text = fit_lines([path.text for path in masked], notes, self._caps.max_result_bytes)
        return _Answer(
            text,
            _outcome(is_cut=has_more or kept < len(shown)),
            listed=tuple(shown[:kept]),
            redacted=add_counts(path.counts for path in masked[:kept]),
        )

    def _masked(self, text: str) -> Masked:
        return mask_personal_data(text, self._redaction)


def _outcome(*, is_cut: bool) -> ToolOutcome:
    return ToolOutcome.TRUNCATED if is_cut else ToolOutcome.OK


def _refused(reason: RefusalReason, sentence: str) -> _Answer:
    return _Answer(f"refused: {reason}: {sentence}", ToolOutcome.REFUSED, reason)


def _invalid_arguments() -> _Answer:
    return _refused(
        RefusalReason.INVALID_ARGUMENTS,
        "The arguments don't fit the tool's input schema; check their names, types and limits.",
    )


def _invalid_glob() -> _Answer:
    return _refused(
        RefusalReason.INVALID_PATH,
        "Give a glob from the repository's root, with / between segments, with no .. segment "
        "or backslash, not starting with / or :.",
    )


def _withheld_note(count: int) -> str:
    if count == 1:
        return "(1 matching line in the files read withheld: it may hold a secret)"
    return f"({count} matching lines in the files read withheld: they may hold a secret)"


def _out_of_bytes() -> _Answer:
    return _refused(
        RefusalReason.BYTE_LIMIT,
        "The bytes tools may return in this review are used up; give your findings now.",
    )


def _input_sha256(arguments: Mapping[str, object]) -> str:
    """The SHA-256 of ``arguments`` as canonical JSON: sorted keys, ASCII, no spaces."""
    canonical = json.dumps(
        arguments, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=repr
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
