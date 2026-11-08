"""Parse a unified diff tolerantly, number its new-version lines, and classify skipped files.

The hunk body wins over its header: numbering starts at the header's new start and advances on
added and context lines. Declared counts are only compared, never used (Phase 1 design, Line
anchors).
"""

import itertools
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from enum import Enum, StrEnum, auto
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Final

from review_bot.errors import DiffNotParsableError

__all__ = [
    "DecodedPath",
    "DiffLine",
    "FileDiff",
    "Hunk",
    "LineKind",
    "ParsedDiff",
    "SkipReason",
    "parse_diff",
]


class LineKind(StrEnum):
    """What a hunk body line does to the file."""

    ADDED = "added"
    CONTEXT = "context"
    REMOVED = "removed"


class SkipReason(StrEnum):
    """Why a file isn't sent for review."""

    DOCS = "docs"
    LOCK = "lock"
    GENERATED = "generated"
    BINARY = "binary"
    # Matched by one of the repository's ``exclude`` globs (P2-S9).
    EXCLUDED_BY_CONFIG = "excluded_by_config"


@dataclass(frozen=True, slots=True)
class DiffLine:
    """One body line, without its marker or line ending.

    ``new_line`` is its line number in the new version: None for a removed line, or for any line
    of a hunk whose header has no readable new start.
    """

    kind: LineKind
    text: str
    new_line: int | None


@dataclass(frozen=True, slots=True)
class Hunk:
    """One ``@@`` hunk. ``has_count_mismatch`` means its declared counts are wrong or unreadable.

    ``new_start`` is the header's declared new start, or None if the header can't be read.
    """

    header: str
    new_start: int | None
    lines: tuple[DiffLine, ...]
    has_count_mismatch: bool


@dataclass(frozen=True, slots=True)
class FileDiff:
    """One file's section.

    ``path`` is the new path, or None for a deleted file; ``old_path`` is the old path, or None
    for a new file. A quoted path's bytes that aren't valid UTF-8 are kept as lone surrogates
    (``surrogateescape``), so ``path.encode("utf-8", "surrogateescape")`` gives back its bytes.
    """

    path: str | None
    old_path: str | None
    hunks: tuple[Hunk, ...]
    skip_reason: SkipReason | None


@dataclass(frozen=True, slots=True)
class DecodedPath:
    """A string read from a file's header lines, and the 1-based diff line it was read from.

    It is either a path the parser read, exactly as read, or any other C-quoted string on a
    header line, decoded the same way. A file's header lines run from its file-start line to its
    first ``@@``.
    """

    text: str
    diff_line: int


@dataclass(frozen=True, slots=True)
class ParsedDiff:
    """The parsed files, and the input minus skipped file sections, otherwise byte for byte.

    ``decoded_paths`` are every file's decoded header strings, in diff line order, so a secret
    spelt in escapes can be scanned for in its decoded form.
    """

    files: tuple[FileDiff, ...]
    reviewable_text: str
    decoded_paths: tuple[DecodedPath, ...]


class _PathSource(Enum):
    """The kind of header line a path was read from."""

    FILE_START = auto()  # diff --git, diff --cc or diff --combined
    MINUS = auto()  # ---
    PLUS = auto()  # +++
    SOURCE = auto()  # rename from, copy from
    TARGET = auto()  # rename to, copy to


# Git starts each file with one of these; a combined diff (merge conflicts) names a single path.
_COMBINED_FILE_STARTS: Final = ("diff --cc ", "diff --combined ")
_GIT_FILE_STARTS: Final = ("diff --git ", *_COMBINED_FILE_STARTS)
_UNIFIED_FILE_OPENING: Final = ("--- ", "+++ ", "@@")
# Capped digits keep each number under Python's int-length limit; a longer one is unreadable.
_HUNK_HEADER: Final = re.compile(r"@@ -(\d{1,9})(?:,(\d{1,9}))? \+(\d{1,9})(?:,(\d{1,9}))? @@")
_BINARY_PREFIX: Final = "Binary files "
_BINARY_SUFFIX: Final = " differ"
_GIT_BINARY_PATCH: Final = "GIT binary patch"
# Possessive, so a string that never closes fails in one pass instead of backtracking.
_QUOTED_AT_START: Final = re.compile(r'"(?:[^"\\]|\\.)*+"')
_ESCAPE: Final = re.compile(rb"\\(?:([0-3][0-7]{2})|(.))")
_ESCAPED_BYTES: Final = {
    b"a": b"\a",
    b"b": b"\b",
    b"f": b"\f",
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"v": b"\v",
}
# The headers whose path is the whole rest of the line. A combined diff's path has no ``a/`` or
# ``b/`` prefix to strip, and neither do a renamed or copied file's.
_REST_OF_LINE_PATHS: Final = {
    "diff --cc ": _PathSource.FILE_START,
    "diff --combined ": _PathSource.FILE_START,
    "rename from ": _PathSource.SOURCE,
    "copy from ": _PathSource.SOURCE,
    "rename to ": _PathSource.TARGET,
    "copy to ": _PathSource.TARGET,
}
_DEV_NULL: Final = "/dev/null"
_MARKERS: Final = {"+": LineKind.ADDED, "-": LineKind.REMOVED, " ": LineKind.CONTEXT}

_DOCS_SUFFIXES: Final = frozenset({".md", ".rst", ".adoc", ".txt"})
_DOCS_NAME_PREFIXES: Final = ("LICENSE", "CHANGELOG")
_DOCS_EXCEPTIONS: Final = ("requirements*.txt", "CMakeLists.txt")
_LOCK_FILES: Final = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "go.sum",
        "composer.lock",
        "Gemfile.lock",
    }
)
_GENERATED_PATTERNS: Final = (
    "*.min.js",
    "*.min.css",
    "*.map",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.pb.go",
)
_GENERATED_ROOT_DIRS: Final = frozenset({"dist", "build"})
_GENERATED_ANY_DEPTH_DIRS: Final = frozenset({"vendor", "node_modules"})


def parse_diff(text: str, *, exclude: Sequence[str] = ()) -> ParsedDiff:
    """Parse a unified diff, with or without git's file headers.

    Git's file headers are ``diff --git``, ``diff --cc`` and ``diff --combined`` lines.

    A file the built-in rules don't skip is skipped ``excluded_by_config`` if its path, the new
    one or a deleted file's old one, matches a glob in ``exclude``. ``*`` matches within one
    path segment and ``**`` any number of whole segments, including none. A glob with no ``/``
    matches the file name at any depth; one with a ``/`` matches the whole path from the root.
    Matching takes time linear in the number of globs times their segments times the path's
    segments.

    Text before the first file, such as a commit message, is kept in ``reviewable_text``.

    Raises:
        DiffNotParsableError: If the text has no file header at all.
    """
    raw_lines = _split_lines(text)
    lines = [line.removesuffix("\n").removesuffix("\r") for line in raw_lines]
    git_starts = [index for index, line in enumerate(lines) if line.startswith(_GIT_FILE_STARTS)]
    starts = git_starts or _unified_file_starts(lines)
    if not starts:
        raise DiffNotParsableError(
            "It has no file header: no `diff --git` line, and no `---` and `+++` pair followed "
            "by a `@@` hunk header. Pass a unified diff, such as the output of git diff."
        )

    bounds = list(itertools.pairwise([*starts, len(lines)]))
    parsed_files = [
        _parse_file(lines[start:end], first_diff_line=start + 1, exclude=exclude)
        for start, end in bounds
    ]
    files = tuple(file for file, _ in parsed_files)
    decoded_paths = tuple(decoded for _, file_paths in parsed_files for decoded in file_paths)
    if not git_starts:
        # Without git's file headers, a file header faked inside a hunk looks like a real one, so
        # nothing is skipped: a faked "file" is still sent for review instead of being hidden.
        files = tuple(replace(file, skip_reason=None) for file in files)
    kept_sections = [
        "".join(raw_lines[start:end])
        for (start, end), file in zip(bounds, files, strict=True)
        if file.skip_reason is None
    ]
    preamble = "".join(raw_lines[: starts[0]])
    return ParsedDiff(
        files=files,
        reviewable_text=preamble + "".join(kept_sections),
        decoded_paths=decoded_paths,
    )


def _split_lines(text: str) -> list[str]:
    # Not ``str.splitlines``, which also splits on characters such as U+2028 that a diff
    # treats as ordinary text.
    *complete, last = text.split("\n")
    lines = [f"{line}\n" for line in complete]
    return [*lines, last] if last else lines


def _unified_file_starts(lines: list[str]) -> list[int]:
    """Return where each file starts in a diff with no git file header lines.

    In a git diff only a ``diff --git``, ``diff --cc`` or ``diff --combined`` line starts a file,
    and no hunk line can start with ``diff``, so hunk lines such as a removed ``-- a`` and an
    added ``++ b`` can't pose as a file header there.
    """
    return [index for index in range(len(lines)) if _opens_unified_file(lines, index)]


def _opens_unified_file(lines: list[str], index: int) -> bool:
    """A ``---`` and ``+++`` pair starts a file only if a hunk header follows it."""
    following = lines[index : index + len(_UNIFIED_FILE_OPENING)]
    return len(following) == len(_UNIFIED_FILE_OPENING) and all(
        line.startswith(prefix)
        for line, prefix in zip(following, _UNIFIED_FILE_OPENING, strict=True)
    )


def _parse_file(
    lines: list[str], *, first_diff_line: int, exclude: Sequence[str]
) -> tuple[FileDiff, tuple[DecodedPath, ...]]:
    """Parse one file's section, and decode its header lines' paths and quoted strings."""
    first_hunk = next(
        (index for index, line in enumerate(lines) if line.startswith("@@")), len(lines)
    )
    header = _read_file_header(lines[:first_hunk], first_diff_line=first_diff_line)
    file = FileDiff(
        path=header.path,
        old_path=header.old_path,
        hunks=_parse_hunks(lines[first_hunk:]),
        skip_reason=_skip_reason(header, exclude),
    )
    return file, header.decoded_paths


@dataclass(frozen=True, slots=True)
class _PathRead:
    source: _PathSource
    path: str
    token_span: tuple[int, int]  # where the path's token sits in its line, as ``re.Match.span``


@dataclass(frozen=True, slots=True)
class _FileHeader:
    path: str | None  # None if deleted
    old_path: str | None  # None if new
    is_binary: bool
    decoded_paths: tuple[DecodedPath, ...]


def _skip_reason(header: _FileHeader, exclude: Sequence[str]) -> SkipReason | None:
    """Why the file is skipped: binary, then a built-in path rule, then an ``exclude`` glob."""
    if header.is_binary:
        return SkipReason.BINARY
    # A deleted file has no new path, so it's classified by its old one.
    path = header.path or header.old_path or ""
    built_in = _skip_reason_for(path)
    if built_in is not None:
        return built_in
    if any(_glob_matches(path, glob) for glob in exclude):
        return SkipReason.EXCLUDED_BY_CONFIG
    return None


def _read_file_header(lines: list[str], *, first_diff_line: int) -> _FileHeader:
    paths: dict[_PathSource, str] = {}
    decoded_paths: list[DecodedPath] = []
    is_new = is_deleted = is_binary = False
    for diff_line, line in enumerate(lines, start=first_diff_line):
        read = _read_path(line)
        decoded_paths += _decode_line(line, read, diff_line)
        if read is not None:
            paths[read.source] = read.path
            is_new = is_new or (read.source is _PathSource.MINUS and read.path == _DEV_NULL)
            is_deleted = is_deleted or (read.source is _PathSource.PLUS and read.path == _DEV_NULL)
        elif line.startswith("new file mode"):
            is_new = True
        elif line.startswith("deleted file mode"):
            is_deleted = True
        elif line == _GIT_BINARY_PATCH or (
            line.startswith(_BINARY_PREFIX) and line.endswith(_BINARY_SUFFIX)
        ):
            is_binary = True
    # Git names a renamed or copied file's paths in its own headers. Any other file keeps its
    # path, so ``diff --git``'s new path is also its old one.
    git_path = paths.get(_PathSource.FILE_START)
    old_path = paths.get(_PathSource.MINUS) or paths.get(_PathSource.SOURCE) or git_path
    path = paths.get(_PathSource.PLUS) or paths.get(_PathSource.TARGET) or git_path
    return _FileHeader(
        path=None if is_deleted else path,
        old_path=None if is_new else old_path,
        is_binary=is_binary,
        decoded_paths=tuple(decoded_paths),
    )


def _decode_line(line: str, read: _PathRead | None, diff_line: int) -> Iterator[DecodedPath]:
    """Yield a header line's path as ``read``, then its other C-quoted strings, decoded.

    The quoted string that is the path's own token isn't yielded again, so nothing is counted
    twice. One that pairs the line's quotes differently, and so only overlaps it, is yielded.
    """
    path_span = None
    if read is not None:
        yield DecodedPath(read.path, diff_line)
        path_span = read.token_span
    for quoted in _quoted_strings(line):
        if quoted.span() != path_span:
            yield DecodedPath(_unquote(quoted.group()), diff_line)


def _quoted_strings(line: str) -> Iterator[re.Match[str]]:
    """Read a line's C-quoted strings left to right, stopping at one that never closes.

    A string that never closes runs to the end of the line, so no other string follows it. Each
    search resumes where the last string ended, so the cost is linear in the line.
    """
    opening = line.find('"')
    while opening != -1:
        quoted = _QUOTED_AT_START.match(line, opening)
        if quoted is None:
            return
        yield quoted
        opening = line.find('"', quoted.end())


def _read_path(line: str) -> _PathRead | None:
    """Read the path a header line names, or return None if it names none."""
    if line.startswith("diff --git "):
        start = len("diff --git ") + _new_path_start(line.removeprefix("diff --git "))
        return _PathRead(_PathSource.FILE_START, _path_from(line[start:]), (start, len(line)))
    if line.startswith(("--- ", "+++ ")):
        token = _first_path_token(line[4:])
        source = _PathSource.MINUS if line.startswith("-") else _PathSource.PLUS
        return _PathRead(source, _path_from(token), (4, 4 + len(token)))
    for prefix, source in _REST_OF_LINE_PATHS.items():
        if line.startswith(prefix):
            return _PathRead(source, _unquote(line[len(prefix) :]), (len(prefix), len(line)))
    return None


def _path_from(token: str) -> str:
    return _strip_prefix(_unquote(token))


def _first_path_token(rest: str) -> str:
    """The path at the start of a ``---`` or ``+++`` line, before any tab and timestamp."""
    if match := _QUOTED_AT_START.match(rest):
        return match.group()
    return rest.split("\t", 1)[0]


def _new_path_start(rest: str) -> int:
    """Where ``diff --git``'s ``a/old b/new`` has its new path; a best guess when paths hold spaces.

    The new path runs from there to the end of the line.
    """
    if rest.endswith('"'):
        # Inside a quoted path a quote is always escaped, so a space then a quote can only open
        # the last quoted path.
        return rest.rfind(' "') + 1
    if " b/" in rest:
        return rest.rindex(" b/") + 1
    return rest.rfind(" ") + 1


def _unquote(token: str) -> str:
    """Undo git's C-style quoting, where octal escapes spell UTF-8 bytes.

    A byte that isn't valid UTF-8 is kept as a lone surrogate (``surrogateescape``), so it is
    neither lost nor confused with a real U+FFFD, and the audit log can quote it as git does.
    """
    if not _QUOTED_AT_START.fullmatch(token):
        return token

    def unescape(match: re.Match[bytes]) -> bytes:
        octal, other = match.groups()
        if octal is not None:
            return bytes([int(octal, 8)])
        return _ESCAPED_BYTES.get(other, other)

    return _ESCAPE.sub(unescape, token[1:-1].encode("utf-8")).decode("utf-8", "surrogateescape")


def _strip_prefix(path: str) -> str:
    return path.removeprefix("a/") if path.startswith("a/") else path.removeprefix("b/")


def _parse_hunks(lines: list[str]) -> tuple[Hunk, ...]:
    """Group lines into hunks. A line that isn't a body line ends the hunk it follows."""
    groups: list[tuple[str, list[str]]] = []
    body: list[str] | None = None
    for line in lines:
        if line.startswith("@@"):
            body = []
            groups.append((line, body))
        elif body is not None and (line == "" or line[0] in "+- \\"):
            body.append(line)
        else:
            body = None
    return tuple(_build_hunk(header, body) for header, body in groups)


def _build_hunk(header: str, body: list[str]) -> Hunk:
    # A blank line is context only when more of the hunk follows it; trailing blanks are the gap
    # before whatever comes next.
    end = len(body)
    while end and body[end - 1] == "":
        end -= 1

    match = _HUNK_HEADER.match(header)
    new_start = int(match[3]) if match else None
    next_new_line = new_start
    lines: list[DiffLine] = []
    for raw in body[:end]:
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        kind = _MARKERS.get(raw[:1], LineKind.CONTEXT)
        if kind is LineKind.REMOVED or next_new_line is None:
            new_line = None
        else:
            new_line = next_new_line
            next_new_line += 1
        lines.append(DiffLine(kind=kind, text=raw[1:], new_line=new_line))

    return Hunk(
        header=header,
        new_start=new_start,
        lines=tuple(lines),
        has_count_mismatch=_counts_differ(match, lines),
    )


def _counts_differ(match: re.Match[str] | None, lines: list[DiffLine]) -> bool:
    if match is None:
        return True
    old_count = int(match[2] or "1")
    new_count = int(match[4] or "1")
    body_old = sum(line.kind is not LineKind.ADDED for line in lines)
    body_new = sum(line.kind is not LineKind.REMOVED for line in lines)
    return (old_count, new_count) != (body_old, body_new)


def _skip_reason_for(path: str) -> SkipReason | None:
    """Classify by path alone, so content such as ``# @generated`` can't opt a file out."""
    *directories, name = PurePosixPath(path).parts or ("",)
    if _is_docs(name):
        return SkipReason.DOCS
    if name in _LOCK_FILES:
        return SkipReason.LOCK
    if (
        _matches_any(name, _GENERATED_PATTERNS)
        or (directories and directories[0] in _GENERATED_ROOT_DIRS)
        or not _GENERATED_ANY_DEPTH_DIRS.isdisjoint(directories)
    ):
        return SkipReason.GENERATED
    return None


def _is_docs(name: str) -> bool:
    suffix = PurePosixPath(name).suffix
    if name.startswith(_DOCS_NAME_PREFIXES):
        # So that ``LICENSE.js`` or ``CHANGELOG.py`` can't hide code behind a docs name.
        return not suffix or suffix in _DOCS_SUFFIXES
    return suffix in _DOCS_SUFFIXES and not _matches_any(name, _DOCS_EXCEPTIONS)


def _matches_any(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def _glob_matches(path: str, glob: str) -> bool:
    """Whether ``path`` matches ``glob``, as ``parse_diff`` describes."""
    segments = path.split("/")
    if "/" not in glob:
        return fnmatchcase(segments[-1], glob)
    # ``matched[i]``: the glob's segments so far match the path's first ``i`` segments.
    matched = [True] + [False] * len(segments)
    for part in glob.split("/"):
        if part == "**":
            first = matched.index(True) if True in matched else len(matched)
            matched = [index >= first for index in range(len(matched))]
        else:
            matched = [False] + [
                matched[index] and fnmatchcase(segment, part)
                for index, segment in enumerate(segments)
            ]
    return matched[-1]
