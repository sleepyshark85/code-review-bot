"""Who owns a path, from a repository's CODEOWNERS file (P3-S11).

Pure: the runner reads the file through git from the base commit and passes its bytes in. The
syntax is GitHub's, gitignore's minus negation and character ranges: a pattern with a leading
``/`` or a ``/`` inside it is anchored at the root, and one without matches at any depth; ``*``
matches within one segment, ``?`` one character, and ``**`` any number of segments. A pattern
ending in ``/`` or ``/**`` owns everything under that directory, one ending in a plain name
owns that path and everything under it, and one ending in a wildcard owns only the paths it
matches, so ``docs/*`` owns ``docs/a.md`` but not ``docs/x/b.md``. The last matching line wins.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "CODEOWNERS_PATHS",
    "MAX_CODEOWNERS_BYTES",
    "NO_CODEOWNERS",
    "CodeOwners",
    "CodeOwnersStatus",
    "owners_for",
    "parse_codeowners",
]

# Where GitHub looks, in its order; only the first that exists counts.
CODEOWNERS_PATHS: Final = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
# GitHub ignores a larger file.
MAX_CODEOWNERS_BYTES: Final = 3 * 1024 * 1024

# ``@user``, ``@org/team`` or an email address. Each part's length is capped (a GitHub login
# has at most 39 characters, an email's local part 64 and a domain label 63), so a summary
# listing owners stays bounded. None can hold a backtick, so a code span holds each whole.
_OWNER: Final = re.compile(
    r"@[A-Za-z0-9-]{1,39}(?:/[A-Za-z0-9_.-]{1,100})?"
    r"|[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}"
)
_UNSUPPORTED_PATTERN_CHARS: Final = frozenset("![")
_WILDCARDS: Final = frozenset("*?")


class CodeOwnersStatus(StrEnum):
    """What reading the CODEOWNERS file found."""

    FOUND = "found"
    ABSENT = "absent"
    TOO_LARGE = "too_large"
    INVALID = "invalid"
    NOT_A_FILE = "not_a_file"


@dataclass(frozen=True, slots=True)
class _Rule:
    """One valid line: its pattern's segments, what it owns, and its owners.

    An unanchored pattern's segments start with ``**``. ``owns_itself`` says whether a path the
    segments match is owned, and ``owns_descendants`` whether everything under one is.
    """

    segments: tuple[str, ...]
    owns_itself: bool
    owns_descendants: bool
    owners: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodeOwners:
    """A CODEOWNERS file's rules, in file order, and where they came from.

    ``source`` is ``<path>@<commit>``, or None if there is no file. Only a ``found`` file has
    rules; ``invalid_lines`` counts the lines skipped because they use syntax GitHub doesn't
    support or an owner that isn't a user, team or email address.
    """

    source: str | None
    status: CodeOwnersStatus
    rules: tuple[_Rule, ...] = ()
    invalid_lines: int = 0


NO_CODEOWNERS: Final = CodeOwners(source=None, status=CodeOwnersStatus.ABSENT)


def parse_codeowners(data: bytes, *, source: str) -> CodeOwners:
    """Parse the file's bytes, read from ``source``, into its rules.

    ``data`` may be longer than ``MAX_CODEOWNERS_BYTES``, so that an oversized file is reported.
    A file over the limit is ``too_large``, and one that isn't UTF-8 ``invalid``; neither has
    rules. Blank lines and comments are ignored.
    """
    if len(data) > MAX_CODEOWNERS_BYTES:
        return CodeOwners(source=source, status=CodeOwnersStatus.TOO_LARGE)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return CodeOwners(source=source, status=CodeOwnersStatus.INVALID)
    rules: list[_Rule] = []
    invalid_lines = 0
    for line in text.split("\n"):
        words = _words(line)
        if not words:
            continue
        rule = _rule(words[0], words[1:])
        if rule is None:
            invalid_lines += 1
        else:
            rules.append(rule)
    return CodeOwners(
        source=source,
        status=CodeOwnersStatus.FOUND,
        rules=tuple(rules),
        invalid_lines=invalid_lines,
    )


def owners_for(codeowners: CodeOwners, path: str) -> tuple[str, ...]:
    """The owners of ``path``, from the last rule matching it; none if no rule does."""
    segments = path.split("/")
    for rule in reversed(codeowners.rules):
        if _owns(rule, segments):
            return rule.owners
    return ()


def _words(line: str) -> list[str]:
    """The line's words, up to a word starting a comment."""
    words: list[str] = []
    for word in line.split():
        if word.startswith("#"):
            break
        words.append(word)
    return words


def _rule(pattern: str, owners: list[str]) -> _Rule | None:
    """The rule a line's pattern and owners make, or None if the line is invalid."""
    if _UNSUPPORTED_PATTERN_CHARS & set(pattern):
        return None
    if not all(_OWNER.fullmatch(owner) for owner in owners):
        return None
    is_anchored = "/" in pattern.removesuffix("/")
    body = pattern.removeprefix("/")
    owns_itself = True
    if body.endswith("/**"):
        body, owns_itself = body.removesuffix("/**"), False
    elif body.endswith("/"):
        body, owns_itself = body.removesuffix("/"), False
    segments = body.split("/")
    if "" in segments:  # ``//``, or nothing but slashes, which no path matches
        return None
    if not is_anchored:
        segments.insert(0, "**")
    # A directory or a plain name owns what is under it; a wildcard only what it matches.
    owns_descendants = not owns_itself or not _WILDCARDS & set(segments[-1])
    return _Rule(
        segments=tuple(segments),
        owns_itself=owns_itself,
        owns_descendants=owns_descendants,
        owners=tuple(dict.fromkeys(owners)),
    )


def _owns(rule: _Rule, path: list[str]) -> bool:
    """Whether ``rule`` owns the path with these segments.

    ``reached[i]`` says whether the pattern's segments so far match the path's first ``i``.
    """
    reached = [True] + [False] * len(path)
    for part in rule.segments:
        if part == "**":
            first = reached.index(True) if True in reached else len(reached)
            reached = [index >= first for index in range(len(reached))]
        else:
            reached = [False] + [
                reached[index] and _segment_matches(segment, part)
                for index, segment in enumerate(path)
            ]
    if rule.owns_itself and reached[-1]:
        return True
    # A directory the pattern matches is a proper, non-empty prefix of the path.
    return rule.owns_descendants and any(reached[1:-1])


def _segment_matches(name: str, part: str) -> bool:
    """Whether ``name`` matches ``part``, where ``*`` is any run of characters and ``?`` one.

    Greedy, going back only to the last ``*``, so it takes at most ``len(name) * len(part)``
    steps.
    """
    if not _WILDCARDS & set(part):
        return name == part
    name_at = part_at = 0
    star_at = -1  # where in ``part`` the last ``*`` was, if any
    resume_at = 0  # where in ``name`` that ``*``'s run ends so far
    while name_at < len(name):
        if part_at < len(part) and part[part_at] == "*":
            star_at, resume_at = part_at, name_at
            part_at += 1
        elif part_at < len(part) and part[part_at] in {"?", name[name_at]}:
            name_at += 1
            part_at += 1
        elif star_at >= 0:
            resume_at += 1
            name_at, part_at = resume_at, star_at + 1
        else:
            return False
    return all(char == "*" for char in part[part_at:])
