"""The files of one commit the tools may show, their text, and the lines that may hold a secret.

The rules are the Phase 3 design's Tool safety. Every file a result shows lines of passes
``CommitFiles.text``, and every listed path ``CommitFiles.listed``, so no path outside the rules,
``.git`` or secrets file, path holding a possible secret, link, submodule, binary or oversized
file reaches the model.
"""

from dataclasses import dataclass
from typing import Final

from review_bot.context.ledger import RefusalReason
from review_bot.context.paths import MAX_PATH_CHARS, is_denied, is_valid_path
from review_bot.errors import NotARegularFileError
from review_bot.git import GitRepo
from review_bot.secret_scan import SecretSpan, path_holds_secret, secret_line_spans

__all__ = ["MAX_LISTED_ENTRIES", "CommitFiles", "FileText", "Refusal"]

MAX_LISTED_ENTRIES: Final = 50_000
_MAX_BLOB_BYTES: Final = 1_000_000
_NOT_A_FILE_REASONS: Final = {
    "directory": RefusalReason.DIRECTORY,
    "symbolic link": RefusalReason.SYMLINK,
    "submodule": RefusalReason.SUBMODULE,
}


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a file isn't shown: the reason, and one sentence for the model."""

    reason: RefusalReason
    sentence: str


@dataclass(frozen=True, slots=True)
class FileText:
    """A file's text, and its lines that may hold a secret, sorted and disjoint."""

    text: str
    secret_spans: tuple[SecretSpan, ...]


class CommitFiles:
    """The files of one commit the tools may show, each secret-scanned at most once."""

    def __init__(self, git: GitRepo, commit: str) -> None:
        """Read ``commit`` of ``git``'s clone; git runs only when a method is called."""
        self._git = git
        self._commit = commit
        # The commit doesn't change, so it is listed, and each file scanned, once per review.
        self._secret_spans_by_path: dict[str, tuple[SecretSpan, ...]] = {}
        self._listed: frozenset[str] | None = None

    def text(self, path: str) -> FileText | Refusal:
        """The regular file at ``path``, or the refusal saying why it isn't shown.

        ``path`` must equal a tree entry's path exactly.

        Raises:
            GitError: If git fails, times out or can't start, or its listing can't be read.
        """
        refusal = _path_refusal(path)
        if refusal is not None:
            return refusal
        try:
            blob = self._git.read_file(self._commit, path, max_bytes=_MAX_BLOB_BYTES)
        except NotARegularFileError as exc:
            reason = _NOT_A_FILE_REASONS.get(exc.kind, RefusalReason.NOT_FOUND)
            return Refusal(reason, f"The path names a {exc.kind}, not a regular file.")
        if blob is None:
            return Refusal(RefusalReason.NOT_FOUND, "The commit has no file at that path.")
        text = _blob_text(blob)
        if isinstance(text, Refusal):
            return text
        spans = self._secret_spans_by_path.get(path)
        if spans is None:
            spans = secret_line_spans(text)
            self._secret_spans_by_path[path] = spans
        return FileText(text, spans)

    def listed(self) -> frozenset[str]:
        """The paths of the commit's regular files the tools may show, listed once per review.

        Raises:
            ListingTooLargeError: If the commit has more than 50,000 entries.
            GitError: If git fails, times out or can't start, or its listing can't be read.
        """
        if self._listed is None:
            entries = self._git.list_files(self._commit, max_entries=MAX_LISTED_ENTRIES)
            self._listed = frozenset(
                entry.path for entry in entries if entry.is_regular_file and _is_shown(entry.path)
            )
        return self._listed


def _is_shown(path: str) -> bool:
    """Whether a path may appear in a result: not ``.git``, a secrets file, or a possible secret."""
    return not (is_denied(path) or path_holds_secret(path))


def _path_refusal(path: str) -> Refusal | None:
    """The refusal for a path the tools never read, or None if they may read it."""
    if not is_valid_path(path):
        return Refusal(
            RefusalReason.INVALID_PATH,
            f"Give a path from the repository's root, with / between segments, at most "
            f"{MAX_PATH_CHARS} characters, with no empty, . or .. segment, backslash or control "
            "character.",
        )
    if is_denied(path):
        return Refusal(
            RefusalReason.DENIED_PATH,
            "The tools never read .git or secrets files such as .env or private keys.",
        )
    if path_holds_secret(path):
        return Refusal(
            RefusalReason.POSSIBLE_SECRET,
            "That path may hold a secret, so the tools never read or show it.",
        )
    return None


def _blob_text(blob: bytes) -> str | Refusal:
    """A blob read up to one byte past the cap, as text, or the refusal saying why it isn't."""
    if len(blob) > _MAX_BLOB_BYTES:
        return Refusal(
            RefusalReason.TOO_LARGE, f"The file is over {_MAX_BLOB_BYTES} bytes, too large to read."
        )
    if b"\0" in blob:
        return _binary()
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return _binary()


def _binary() -> Refusal:
    return Refusal(RefusalReason.BINARY, "The file isn't UTF-8 text, so it isn't shown.")
