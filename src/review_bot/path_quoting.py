"""git's C-quoting of a path's bytes, and whether a path's text is valid UTF-8.

Pure. ``report`` quotes diff paths with it in machine-readable output, and ``context.paths``
quotes the repository paths the tools show the model.
"""

from typing import Final

__all__ = ["c_quoted", "has_lone_surrogate"]

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


def c_quoted(raw: bytes) -> str:
    r"""``raw`` in git's C-quoted form, as git shows a path with its default ``core.quotePath``.

    Wrapped in double quotes, with ``"`` and ``\`` escaped, a short escape for each of
    ``\a \b \t \n \v \f \r``, and every other byte outside printable ASCII in octal.
    """
    return '"' + "".join(_c_escaped(byte) for byte in raw) + '"'


def has_lone_surrogate(text: str) -> bool:
    """Whether ``text`` holds a lone surrogate, the only thing that stops it encoding as UTF-8."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _c_escaped(byte: int) -> str:
    if byte in _SHORT_ESCAPES:
        return _SHORT_ESCAPES[byte]
    if byte in _PRINTABLE_ASCII:
        return chr(byte)
    return f"\\{byte:03o}"
