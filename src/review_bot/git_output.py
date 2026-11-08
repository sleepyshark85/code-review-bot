"""Parsing git's output: tree entries from ``ls-tree`` and matches from ``git grep``.

No process runs here: ``git`` runs the commands, with the options these formats depend on,
and hands their output to these readers.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Final

from review_bot.errors import GitError

__all__ = [
    "REGULAR_FILE_MODES",
    "GrepMatch",
    "GrepReader",
    "GrepResult",
    "TreeEntry",
    "find_tree_entry",
    "parse_tree_entry",
]

# ``git ls-tree`` modes of a regular file, plain or executable.
REGULAR_FILE_MODES: Final = frozenset({"100644", "100755"})
_ENTRY_FIELD_COUNT: Final = 3  # mode, type and SHA, before the tab and the path
_LISTING_FIELD_COUNT: Final = 4  # with ``-l``, the size too
# How much of git grep's output is read at a time.
_GREP_CHUNK_BYTES: Final = 65_536


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One entry of a commit's tree.

    ``path`` is the repository path, ``/`` separated, as git stores it: bytes that aren't UTF-8
    are lone surrogates (``surrogateescape``). ``mode`` is git's, such as ``100644``, ``120000``
    for a symbolic link or ``160000`` for a submodule. ``size`` is None for a submodule.
    """

    path: str
    mode: str
    size: int | None

    @property
    def is_regular_file(self) -> bool:
        """Whether the entry is a regular file, plain or executable."""
        return self.mode in REGULAR_FILE_MODES


@dataclass(frozen=True, slots=True)
class GrepMatch:
    """One line ``git grep`` matched.

    ``path`` is its file's path, as in ``TreeEntry``; ``line`` counts from 1; ``text`` is the
    line without its line feed, with bytes that aren't UTF-8 replaced.
    """

    path: str
    line: int
    text: str


@dataclass(frozen=True, slots=True)
class GrepResult:
    """``git grep``'s matches, by path then line, and whether its output reached the cap.

    When ``is_cut``, more lines may match than ``matches`` holds.
    """

    matches: tuple[GrepMatch, ...]
    is_cut: bool


def find_tree_entry(listing: bytes, path: str, *, max_bytes: int) -> tuple[str, str, str] | None:
    """The mode, type and SHA of the ``ls-tree -z`` entry whose path is exactly ``path``.

    ``listing`` was read up to ``max_bytes`` plus one bytes, so a longer one was cut short.

    Raises:
        GitError: If the listing was cut short, or an entry isn't in ``ls-tree``'s form.
    """
    if len(listing) > max_bytes:
        raise _unreadable_listing()
    wanted = path.encode("utf-8", "surrogateescape")
    for record in listing.split(b"\0")[:-1]:  # git ends every record with a NUL
        meta, tab, entry_path = record.partition(b"\t")
        fields = meta.split(b" ")
        if not tab or len(fields) != _ENTRY_FIELD_COUNT:
            raise _unreadable_listing()
        if entry_path == wanted:
            mode, kind, object_id = (field.decode("ascii", "replace") for field in fields)
            return mode, kind, object_id
    return None


def parse_tree_entry(record: bytes) -> TreeEntry:
    """Parse one ``ls-tree -l -z`` record: mode, type, SHA, padded size, a tab and the path.

    Raises:
        GitError: If the record isn't in that form.
    """
    meta, tab, path = record.partition(b"\t")
    fields = meta.split()
    if not tab or len(fields) != _LISTING_FIELD_COUNT:
        raise _unreadable_listing()
    mode, _kind, _sha, size = fields
    if size != b"-" and not size.isdigit():
        raise _unreadable_listing()
    return TreeEntry(
        path=path.decode("utf-8", "surrogateescape"),
        mode=mode.decode("ascii"),
        size=None if size == b"-" else int(size),
    )


def _unreadable_listing() -> GitError:
    return GitError("git ls-tree wrote a listing that can't be read.")


class GrepReader:
    """Reads ``git grep -n -z`` output as it streams, keeping the matches in files ``keep`` takes.

    Each record is ``<commit>:<path>``, a NUL, the line number, a NUL, then the line up to a line
    feed. A path can hold a line feed and a line a NUL, so the fields are found in order. A record
    whose path isn't kept is dropped as it streams by, so what is held stays within
    ``max_bytes``, whatever git writes.
    """

    def __init__(self, commit: str, *, keep: Callable[[str], bool], max_bytes: int) -> None:
        """Read the output of a ``git grep`` of ``commit``."""
        self._prefix = f"{commit}:".encode("ascii")
        self._keep = keep
        self._max_bytes = max_bytes
        self._matches: list[GrepMatch] = []
        self._kept_bytes = 0
        self._is_cut = False
        self._is_skipping = False  # inside a dropped record, until its line feed

    def read(self, stdout: IO[bytes]) -> tuple[GrepResult, bool]:
        """The matches kept, and whether reading stopped because they reached the cap.

        Raises:
            GitError: If a record isn't in ``git grep``'s form.
        """
        pending = b""
        while not self._is_cut and (chunk := stdout.read(_GREP_CHUNK_BYTES)):
            pending = self._consume(pending + chunk)
        if pending and not self._is_cut:
            raise _unreadable_grep()
        return GrepResult(tuple(self._matches), is_cut=self._is_cut), self._is_cut

    def _consume(self, data: bytes) -> bytes:
        """Take every complete record from ``data``, and return the incomplete rest."""
        position = 0
        while True:
            if self._is_skipping:
                end = data.find(b"\n", position)
                if end == -1:
                    return b""
                position, self._is_skipping = end + 1, False
            path_end = data.find(b"\0", position)
            line_end = -1 if path_end == -1 else data.find(b"\0", path_end + 1)
            if line_end == -1:
                break
            path, number = data[position:path_end], data[path_end + 1 : line_end]
            if not path.startswith(self._prefix) or not number.isdigit():
                raise _unreadable_grep()
            decoded = path.removeprefix(self._prefix).decode("utf-8", "surrogateescape")
            if not self._keep(decoded):
                position, self._is_skipping = line_end + 1, True
                continue
            text_end = data.find(b"\n", line_end + 1)
            if text_end == -1:
                break
            self._kept_bytes += text_end + 1 - position
            if self._kept_bytes > self._max_bytes:
                self._is_cut = True
                return b""
            text = data[line_end + 1 : text_end].decode("utf-8", "replace")
            self._matches.append(GrepMatch(decoded, int(number), text))
            position = text_end + 1
        rest = data[position:]
        # A kept record this long can't fit within the cap either.
        if self._kept_bytes + len(rest) > self._max_bytes:
            self._is_cut = True
        return rest


def _unreadable_grep() -> GitError:
    return GitError("git grep wrote output that can't be read.")
