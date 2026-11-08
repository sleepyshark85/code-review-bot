"""The text of tool results: lines cut and fitted within the caps, and secret spans met.

Pure.
"""

import bisect
from collections.abc import Sequence
from typing import Final

from review_bot.secret_scan import SecretSpan

__all__ = [
    "cut_line",
    "fit_lines",
    "overlaps_any",
    "text_lines",
    "utf8_size",
]

_MAX_LINE_CHARS: Final = 2000
_CUT_MARK: Final = " (cut)"
_BYTES_NOTE: Final = "(truncated: the result reached max_tool_result_bytes; ask for less)"


def overlaps_any(spans: Sequence[SecretSpan], first_line: int, last_line: int) -> bool:
    """Whether lines ``first_line`` to ``last_line`` meet any of ``spans``.

    ``spans`` are sorted and disjoint, as ``secret_line_spans`` returns them, so only the first
    span ending at or after ``first_line`` can meet the lines.
    """
    index = bisect.bisect_left(spans, first_line, key=lambda span: span.last_line)
    return index < len(spans) and spans[index].first_line <= last_line


def fit_lines(lines: Sequence[str], notes: Sequence[str], max_bytes: int) -> tuple[int, str]:
    """How many leading ``lines`` fit in ``max_bytes`` with ``notes`` after them, and the text.

    If they don't all fit, a note says the result was truncated, and the text ends at a line
    boundary, within ``max_bytes`` as long as the notes alone fit.
    """
    text = "\n".join([*lines, *notes])
    if utf8_size(text) <= max_bytes:
        return len(lines), text
    notes = [*notes, _BYTES_NOTE]
    budget = max_bytes - utf8_size("\n".join(notes))
    kept = 0
    for line in lines:
        budget -= utf8_size(line) + 1  # the line and the line feed after it
        if budget < 0:
            break
        kept += 1
    return kept, "\n".join([*lines[:kept], *notes])


def text_lines(text: str) -> list[str]:
    """The text's lines, split on line feeds as git numbers them, without the line feeds."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()  # after the last line feed, or the whole of an empty file
    return lines


def cut_line(line: str) -> str:
    """``line``, cut at 2,000 characters and marked ``(cut)`` if it is longer."""
    if len(line) <= _MAX_LINE_CHARS:
        return line
    return line[:_MAX_LINE_CHARS] + _CUT_MARK


def utf8_size(text: str) -> int:
    """The size of ``text`` in UTF-8, in bytes.

    Paths are shown quoted unless plain UTF-8, and file text is UTF-8, so a result encodes.
    """
    return len(text.encode("utf-8"))
