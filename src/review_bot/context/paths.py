"""The paths and globs the tools accept from the model, the files they withhold, and path display.

The rules are the Phase 3 design's Tool safety. Pure.
"""

import re
import unicodedata
from fnmatch import fnmatchcase
from typing import Final

from review_bot.path_quoting import c_quoted, has_lone_surrogate

__all__ = [
    "MAX_PATH_CHARS",
    "is_denied",
    "is_valid_glob",
    "is_valid_path",
    "shown_path",
]

MAX_PATH_CHARS: Final = 1000
# Pathspec magic, such as ``:(top)``, or a Windows drive, such as ``C:/``; ``x:notes.md`` is a name.
_MAGIC_OR_DRIVE: Final = re.compile(r":|[A-Za-z]:(?:/|$)")
_NOT_A_NAME: Final = frozenset({"", ".", ".."})
# Secrets files, matched on the last segment in lower case.
_DENIED_NAMES: Final = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.ppk",
    "*.kdbx",
    "*.tfstate",
    "*.tfvars",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".htpasswd",
    ".pgpass",
    "credentials",
    "credentials.*",
)
# SSH private keys: each name, and each of its variants but the ``.pub`` public key.
_PRIVATE_KEY_NAMES: Final = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")


def is_valid_path(path: str) -> bool:
    """Whether ``path`` is a file path the tools accept from the model.

    It is relative and ``/``-separated, at most 1,000 characters, with no empty, ``.`` or ``..``
    segment, no backslash, control character or byte that isn't UTF-8, and doesn't start with
    pathspec magic or a drive. It is accepted as given, never normalised.
    """
    return (
        0 < len(path) <= MAX_PATH_CHARS
        and _is_plain(path)
        and "\\" not in path
        and not _MAGIC_OR_DRIVE.match(path)
        and _NOT_A_NAME.isdisjoint(path.split("/"))
    )


def is_valid_glob(pattern: str) -> bool:
    """Whether ``pattern`` is a glob the tools accept.

    It has no ``..`` segment and no backslash, and doesn't start with ``/`` or ``:``. A glob is
    only ever matched in our code, never passed to git.
    """
    return not ("\\" in pattern or pattern.startswith(("/", ":")) or ".." in pattern.split("/"))


def is_denied(path: str) -> bool:
    """Whether the tools keep ``path`` from the model: anything under ``.git``, or a secrets file.

    Both are matched case-insensitively, ``.git`` on any segment and secrets files on the last.
    """
    segments = path.lower().split("/")
    name = segments[-1]
    return (
        ".git" in segments
        or any(fnmatchcase(name, pattern) for pattern in _DENIED_NAMES)
        or _is_private_key_name(name)
    )


def shown_path(path: str) -> str:
    r"""``path`` as a tool result shows it.

    In git's C-quoted form if it holds a control character, a byte that isn't UTF-8, ``"`` or
    ``\``, so no raw control character reaches the model, and a path is quoted exactly when it
    starts with ``"``. Otherwise as it is.
    """
    if _is_plain(path) and not any(character in path for character in '"\\'):
        return path
    return c_quoted(path.encode("utf-8", "surrogateescape"))


def _is_plain(text: str) -> bool:
    """Whether ``text`` is valid UTF-8 with no control character, C0, DEL or C1."""
    return not has_lone_surrogate(text) and not any(
        unicodedata.category(character) == "Cc" for character in text
    )


def _is_private_key_name(name: str) -> bool:
    return not name.endswith(".pub") and any(
        name == key or name.startswith(f"{key}.") for key in _PRIVATE_KEY_NAMES
    )
