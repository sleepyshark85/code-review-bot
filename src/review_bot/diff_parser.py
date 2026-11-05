"""Parse a unified diff tolerantly, number its new-version lines, and classify skipped files.

The hunk body wins over its header: numbering starts at the header's new start and advances on
added and context lines. Declared counts are only compared, never used (Phase 1 design, Line
anchors).
"""

import itertools
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Final

from review_bot.errors import DiffNotParsableError

__all__ = ["DiffLine", "FileDiff", "Hunk", "LineKind", "ParsedDiff", "SkipReason", "parse_diff"]


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
    """One ``@@`` hunk. ``has_count_mismatch`` means its declared counts are wrong or unreadable."""

    header: str
    lines: tuple[DiffLine, ...]
    has_count_mismatch: bool


@dataclass(frozen=True, slots=True)
class FileDiff:
    """One file's section. ``path`` is the new path, or None for a deleted file."""

    path: str | None
    hunks: tuple[Hunk, ...]
    skip_reason: SkipReason | None


@dataclass(frozen=True, slots=True)
class ParsedDiff:
    """The parsed files, and the input minus skipped file sections, otherwise byte for byte."""

    files: tuple[FileDiff, ...]
    reviewable_text: str


_UNIFIED_FILE_OPENING: Final = ("--- ", "+++ ", "@@")
# Capped digits keep each number under Python's int-length limit; a longer one is unreadable.
_HUNK_HEADER: Final = re.compile(r"@@ -(\d{1,9})(?:,(\d{1,9}))? \+(\d{1,9})(?:,(\d{1,9}))? @@")
_BINARY_PREFIX: Final = "Binary files "
_BINARY_SUFFIX: Final = " differ"
_GIT_BINARY_PATCH: Final = "GIT binary patch"
_QUOTED_AT_START: Final = re.compile(r'"(?:[^"\\]|\\.)*"')
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


def parse_diff(text: str) -> ParsedDiff:
    """Parse a unified diff, with or without ``diff --git`` headers.

    Text before the first file, such as a commit message, is kept in ``reviewable_text``.

    Raises:
        DiffNotParsableError: If the text has no file header at all.
    """
    raw_lines = _split_lines(text)
    lines = [line.removesuffix("\n").removesuffix("\r") for line in raw_lines]
    git_starts = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    starts = git_starts or _unified_file_starts(lines)
    if not starts:
        raise DiffNotParsableError(
            "It has no file header: no `diff --git` line, and no `---` and `+++` pair followed "
            "by a `@@` hunk header. Pass a unified diff, such as the output of git diff."
        )

    bounds = list(itertools.pairwise([*starts, len(lines)]))
    files = tuple(_parse_file(lines[start:end]) for start, end in bounds)
    if not git_starts:
        # Without ``diff --git``, a file header faked inside a hunk looks like a real one, so
        # nothing is skipped: a faked "file" is still sent for review instead of being hidden.
        files = tuple(replace(file, skip_reason=None) for file in files)
    kept_sections = [
        "".join(raw_lines[start:end])
        for (start, end), file in zip(bounds, files, strict=True)
        if file.skip_reason is None
    ]
    preamble = "".join(raw_lines[: starts[0]])
    return ParsedDiff(files=files, reviewable_text=preamble + "".join(kept_sections))


def _split_lines(text: str) -> list[str]:
    # Not ``str.splitlines``, which also splits on characters such as U+2028 that a diff
    # treats as ordinary text.
    *complete, last = text.split("\n")
    lines = [f"{line}\n" for line in complete]
    return [*lines, last] if last else lines


def _unified_file_starts(lines: list[str]) -> list[int]:
    """Return where each file starts in a diff with no ``diff --git`` lines.

    In a git diff only ``diff --git`` starts a file, so hunk lines such as a removed ``-- a``
    and an added ``++ b`` can't pose as a file header there.
    """
    return [index for index in range(len(lines)) if _opens_unified_file(lines, index)]


def _opens_unified_file(lines: list[str], index: int) -> bool:
    """A ``---`` and ``+++`` pair starts a file only if a hunk header follows it."""
    following = lines[index : index + len(_UNIFIED_FILE_OPENING)]
    return len(following) == len(_UNIFIED_FILE_OPENING) and all(
        line.startswith(prefix)
        for line, prefix in zip(following, _UNIFIED_FILE_OPENING, strict=True)
    )


def _parse_file(lines: list[str]) -> FileDiff:
    first_hunk = next(
        (index for index, line in enumerate(lines) if line.startswith("@@")), len(lines)
    )
    header = _read_file_header(lines[:first_hunk])
    # A deleted file has no new path, so it's classified by its old one.
    path_reason = _skip_reason_for(header.path or header.old_path or "")
    return FileDiff(
        path=header.path,
        hunks=_parse_hunks(lines[first_hunk:]),
        skip_reason=SkipReason.BINARY if header.is_binary else path_reason,
    )


@dataclass(frozen=True, slots=True)
class _FileHeader:
    path: str | None  # None if deleted
    old_path: str | None
    is_binary: bool


def _read_file_header(lines: list[str]) -> _FileHeader:
    plus_path = minus_path = rename_path = git_path = None
    is_deleted = is_binary = False
    for line in lines:
        if line.startswith("diff --git "):
            git_path = _path_from(_last_path_token(line.removeprefix("diff --git ")))
        elif line.startswith("--- "):
            minus_path = _path_from(_first_path_token(line.removeprefix("--- ")))
        elif line.startswith("+++ "):
            plus_path = _path_from(_first_path_token(line.removeprefix("+++ ")))
            is_deleted = is_deleted or plus_path == _DEV_NULL
        elif line.startswith("rename to "):
            rename_path = _unquote(line.removeprefix("rename to "))
        elif line.startswith("deleted file mode"):
            is_deleted = True
        elif line == _GIT_BINARY_PATCH or (
            line.startswith(_BINARY_PREFIX) and line.endswith(_BINARY_SUFFIX)
        ):
            is_binary = True
    # A deletion's ``diff --git`` line names the old path twice.
    old_path = minus_path or git_path
    if is_deleted:
        return _FileHeader(path=None, old_path=old_path, is_binary=is_binary)
    path = plus_path or rename_path or git_path
    return _FileHeader(path=path, old_path=old_path, is_binary=is_binary)


def _path_from(token: str) -> str:
    return _strip_prefix(_unquote(token))


def _first_path_token(rest: str) -> str:
    """The path at the start of a ``---`` or ``+++`` line, before any tab and timestamp."""
    if match := _QUOTED_AT_START.match(rest):
        return match.group()
    return rest.split("\t", 1)[0]


def _last_path_token(rest: str) -> str:
    """The new path in ``diff --git``'s ``a/old b/new``; a best guess when paths hold spaces."""
    if rest.endswith('"'):
        # Inside a quoted path a quote is always escaped, so a space then a quote can only open
        # the last quoted path.
        return rest[rest.rfind(' "') + 1 :]
    if " b/" in rest:
        return "b/" + rest.rsplit(" b/", 1)[1]
    return rest.rsplit(" ", 1)[-1]


def _unquote(token: str) -> str:
    """Undo git's C-style quoting, where octal escapes spell UTF-8 bytes."""
    if not _QUOTED_AT_START.fullmatch(token):
        return token

    def unescape(match: re.Match[bytes]) -> bytes:
        octal, other = match.groups()
        if octal is not None:
            return bytes([int(octal, 8)])
        return _ESCAPED_BYTES.get(other, other)

    return _ESCAPE.sub(unescape, token[1:-1].encode("utf-8")).decode("utf-8", "replace")


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
    next_new_line = int(match[3]) if match else None
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

    return Hunk(header=header, lines=tuple(lines), has_count_mismatch=_counts_differ(match, lines))


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
