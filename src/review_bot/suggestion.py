"""Check a finding's suggested fix, and fence it so it can't close its block (P3-S12).

A suggestion is the model's replacement for the one line a finding is anchored on (Phase 3
design, Q7). Pure.
"""

import re
import unicodedata
from enum import StrEnum
from typing import Final

from review_bot.diff_parser import DiffLine, LineKind
from review_bot.secret_scan import scan_secrets

__all__ = ["SuggestionDropReason", "fenced_block", "suggestion_drop_reason"]

# P3-S12 Limits.
_MAX_CHARS: Final = 1_000
_MAX_LINES: Final = 10
# A line feed separates a suggestion's lines, and a tab may indent them.
_ALLOWED_CONTROLS: Final = frozenset("\t\n")
# Not control characters to Unicode, but they reorder text or break lines invisibly, so a
# committed suggestion could read one way and compile another (Trojan Source; P3-S12 Notes).
_INVISIBLE_LAYOUT: Final = frozenset(
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u2028\u2029"
)
# What "trailing whitespace" means for ``unchanged`` (P3-S12 AC3).
_TRAILING_WHITESPACE: Final = " \t\n"
_BACKTICK_RUN: Final = re.compile("`+")
_MIN_FENCE: Final = 3
# What every placeholder for masked personal data starts with (``governance.redaction``).
_PLACEHOLDER_START: Final = "[REDACTED:"


class SuggestionDropReason(StrEnum):
    """Why a finding's suggestion was dropped; the finding itself is kept."""

    NOT_ADDED_LINE = "not_added_line"
    TOO_LONG = "too_long"
    UNCHANGED = "unchanged"
    CONTROL_CHARACTERS = "control_characters"
    POSSIBLE_SECRET = "possible_secret"  # noqa: S105 - a reason's name, not a secret
    NOT_REQUESTED = "not_requested"
    REDACTED = "redacted"


def suggestion_drop_reason(  # noqa: PLR0911 - one return per reason, in the order checked
    suggestion: str, anchored: DiffLine
) -> SuggestionDropReason | None:
    """Say why ``suggestion`` can't replace the ``anchored`` line, or None if it can.

    A suggestion holding a placeholder for masked personal data would commit the placeholder,
    so it is dropped ``redacted`` before any other check. Only an added line may be replaced.
    Every check is linear in the suggestion's length, and the size check comes before the
    others but the placeholder's, so they, the secret scan included, read at most 1,000
    characters. Control characters are checked before ``unchanged``, so a suggestion holding
    one is dropped for it even if it equals the line, as in a CRLF file. An empty suggestion
    deletes the line, so it is never ``unchanged``.
    """
    if _PLACEHOLDER_START in suggestion:
        return SuggestionDropReason.REDACTED
    if anchored.kind is not LineKind.ADDED:
        return SuggestionDropReason.NOT_ADDED_LINE
    if len(suggestion) > _MAX_CHARS or suggestion.count("\n") >= _MAX_LINES:
        return SuggestionDropReason.TOO_LONG
    if _has_control_characters(suggestion):
        return SuggestionDropReason.CONTROL_CHARACTERS
    if suggestion and (
        suggestion.rstrip(_TRAILING_WHITESPACE) == anchored.text.rstrip(_TRAILING_WHITESPACE)
    ):
        return SuggestionDropReason.UNCHANGED
    if scan_secrets(suggestion):
        return SuggestionDropReason.POSSIBLE_SECRET
    return None


def fenced_block(text: str, *, info: str = "") -> str:
    """``text`` as a fenced code block opened with ``info``, which nothing in ``text`` can close.

    The backtick fence is at least 3 long and always longer than the longest backtick run in
    ``text``. Empty text gives an empty block.

    Raises:
        ValueError: If ``text`` holds a character ``suggestion_drop_reason`` refuses as
            ``control_characters``: one could end a line, or a list item the block is indented
            in, where the caller can't see it.
    """
    if _has_control_characters(text):
        raise ValueError("A fenced block's text must hold no control or bidi character.")
    longest_run = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    fence = "`" * max(_MIN_FENCE, longest_run + 1)
    lines = [f"{fence}{info}", text, fence] if text else [f"{fence}{info}", fence]
    return "\n".join(lines)


def _has_control_characters(text: str) -> bool:
    """Whether ``text`` holds a control character but tab and LF, or an invisible layout one."""
    return any(
        char in _INVISIBLE_LAYOUT
        or (unicodedata.category(char) == "Cc" and char not in _ALLOWED_CONTROLS)
        for char in text
    )
