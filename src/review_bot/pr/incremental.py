"""The part of a pull request's diff an incremental run reviews: the hunks changed since a commit.

Pure. A run that knows the last commit it reviewed sends the model only the PR diff's hunks that
hold a line changed since then, so it doesn't raise new comments on code it has already reviewed
(P2-S7; Phase 2 design, Data flow).
"""

import re
from collections.abc import Collection, Mapping
from typing import Final

from review_bot.diff_parser import Hunk, LineKind, ParsedDiff

__all__ = ["changed_hunks"]

# Where git starts each file and each hunk; no body line can start with either, since a body
# line starts with a space, ``+``, ``-`` or ``\``.
_FILE_START: Final = re.compile(r"^(?=diff --git )", re.MULTILINE)
_HUNK_START: Final = re.compile(r"^(?=@@)", re.MULTILINE)


def changed_hunks(diff: str, parsed: ParsedDiff, changed: ParsedDiff) -> str:
    """Return ``diff`` with only its hunks holding a line ``changed`` changes, or "" if none.

    ``diff`` is git's diff of the pull request, ``parsed`` its parse, and ``changed`` its diff
    since the last reviewed commit; both diffs number lines in the head commit. A line
    ``changed`` adds counts, and so do the lines on each side of a line it removes, which has
    no number of its own in the head. Each kept hunk keeps its file's header lines, and a file
    with no hunk kept is left out.

    Raises:
        ValueError: If ``diff`` doesn't start each file with ``diff --git``, as git does, or
            ``parsed`` isn't its parse.
    """
    lines = _changed_lines(changed)
    _, *sections = _FILE_START.split(diff)
    if len(sections) != len(parsed.files):
        raise ValueError("Only git's diff, whose files each start with `diff --git`, is split.")
    kept: list[str] = []
    for section, file in zip(sections, parsed.files, strict=True):
        header, *hunk_texts = _HUNK_START.split(section)
        file_lines = lines.get(file.path, frozenset()) if file.path is not None else frozenset()
        hunks = [
            text
            for text, hunk in zip(hunk_texts, file.hunks, strict=True)
            if _holds_any(hunk, file_lines)
        ]
        if hunks:
            kept.append(header + "".join(hunks))
    return "".join(kept)


def _changed_lines(changed: ParsedDiff) -> Mapping[str, frozenset[int]]:
    """Each new path's changed line numbers in the head: added, or next to a removed line."""
    lines: dict[str, frozenset[int]] = {}
    for file in changed.files:
        if file.path is None:
            continue
        numbers: set[int] = set()
        for hunk in file.hunks:
            if hunk.new_start is None:
                continue
            previous = hunk.new_start - 1
            for line in hunk.lines:
                if line.kind is LineKind.REMOVED:
                    numbers.update((previous, previous + 1))
                elif line.new_line is not None:
                    if line.kind is LineKind.ADDED:
                        numbers.add(line.new_line)
                    previous = line.new_line
        lines[file.path] = frozenset(numbers)
    return lines


def _holds_any(hunk: Hunk, numbers: Collection[int]) -> bool:
    return any(line.new_line in numbers for line in hunk.lines if line.new_line is not None)
