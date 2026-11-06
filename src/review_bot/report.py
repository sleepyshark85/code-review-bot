"""A review's result, and how its diff paths are written in machine-readable output.

Pure: the audit log and the JSON report both write paths with these helpers, and the audit log
redacts them first.
"""

from dataclasses import dataclass
from typing import Final

from review_bot.diff_parser import FileDiff
from review_bot.policy import Decision
from review_bot.providers.base import TokenUsage
from review_bot.verify import VerifyResult

__all__ = ["PipelineResult", "has_lone_surrogate", "path_fields", "section_path"]

# Git's C-quoting: these bytes get a short escape, other control and non-ASCII bytes an octal one.
_SHORT_ESCAPES: Final = {
    ord("\a"): r"\a",
    ord("\b"): r"\b",
    ord("\t"): r"\t",
    ord("\n"): r"\n",
    ord("\v"): r"\v",
    ord("\f"): r"\f",
    ord("\r"): r"\r",
    ord('"'): r"\"",
    ord("\\"): r"\\",
}
_PRINTABLE_ASCII: Final = range(0x20, 0x7F)
# What a section's headers name when they say /dev/null for both the old and the new file.
_NO_PATH: Final = "/dev/null"


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """A review's verified and decided findings, and what it skipped.

    ``model_called`` is False when every file was skipped; ``verification`` and ``decision`` are
    then empty, as are ``warnings``. ``warnings`` are the provider's, already written to stderr
    and the audit log.
    """

    verification: VerifyResult
    decision: Decision
    usage: TokenUsage
    skipped_files: tuple[FileDiff, ...]
    model_called: bool
    warnings: tuple[str, ...] = ()


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
        return {"file": _c_quoted(path.encode("utf-8", "surrogateescape")), "path_quoted": True}
    return {"file": path}


def has_lone_surrogate(text: str) -> bool:
    """Whether ``text`` holds a lone surrogate, the only thing that stops it encoding as UTF-8."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _c_quoted(raw: bytes) -> str:
    return '"' + "".join(_c_escaped(byte) for byte in raw) + '"'


def _c_escaped(byte: int) -> str:
    if byte in _SHORT_ESCAPES:
        return _SHORT_ESCAPES[byte]
    if byte in _PRINTABLE_ASCII:
        return chr(byte)
    return f"\\{byte:03o}"
