"""Detect and redact secrets in raw text, by vendor format and one assignment pattern.

The forms, boundaries and near misses are specified in
``docs/phases/phase-1/slices/p1-s3-secret-block.md``. Every pattern is matched within one line
and runs in linear time, because the text is an untrusted diff of up to the size limit.
"""

import bisect
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NamedTuple

__all__ = [
    "SecretHit",
    "SecretKind",
    "SecretSpan",
    "path_holds_secret",
    "redact",
    "scan_secrets",
    "secret_line_spans",
]


class SecretKind(StrEnum):
    """What a detected secret looks like."""

    AWS_ACCESS_KEY = "aws-access-key"
    GITHUB_TOKEN = "github-token"  # noqa: S105 - a kind's name, not a secret
    ANTHROPIC_KEY = "anthropic-key"
    OPENAI_KEY = "openai-key"
    SLACK_TOKEN = "slack-token"  # noqa: S105 - a kind's name, not a secret
    PRIVATE_KEY = "private-key"
    ASSIGNED_CREDENTIAL = "assigned-credential"


@dataclass(frozen=True, slots=True)
class SecretHit:
    """Where a possible secret is. It never holds the value, so it can be reported safely."""

    kind: SecretKind
    diff_line: int  # 1-based line number in the scanned text


_REDACTED: Final = "[REDACTED]"

# A vendor prefix must not continue a longer token, but may follow a line's leading run of diff
# markers: a combined diff has one marker column per parent. The run is possessive, so it is
# never re-split, which keeps matching linear.
_PREFIX_BOUNDARY: Final = r"(?:^[-+ ]*+|(?<![A-Za-z0-9_-]))"

_PEM_BEGIN: Final = r"-----BEGIN(?:[A-Z0-9 ]* PRIVATE KEY| PGP PRIVATE KEY BLOCK)-----"
_PEM_END: Final = r"-----END(?:[A-Z0-9 ]* PRIVATE KEY| PGP PRIVATE KEY BLOCK)-----"


# In a path, a key may also follow a separator, as in ``cfg_<key>.txt``.
_PATH_BOUNDARY: Final = r"(?:^|(?<=[/_.-])|(?<![A-Za-z0-9_-]))"

# The value of each vendor-format key. The Anthropic prefix is spelt with a group so the
# repository's key-literal check can't mistake a pattern for a key.
_VENDOR_VALUES: Final[Mapping[SecretKind, str]] = {
    SecretKind.AWS_ACCESS_KEY: r"AKIA[A-Z0-9]{16}(?![A-Z0-9])",
    SecretKind.GITHUB_TOKEN: (
        r"gh[pousr]_[A-Za-z0-9]{36}(?![A-Za-z0-9])|github_pat_[A-Za-z0-9_]{22,}"
    ),
    SecretKind.ANTHROPIC_KEY: r"(?:sk)-ant-[A-Za-z0-9_-]{20,}",
    # Not where an Anthropic key matches, and with a digit somewhere in the value.
    SecretKind.OPENAI_KEY: (
        r"sk-(?!ant-[A-Za-z0-9_-]{20})(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{20,}"
    ),
    SecretKind.SLACK_TOKEN: r"xox[abprs]-[A-Za-z0-9-]{10,}",
}

# Each pattern's `secret` group is the value.
_VENDOR_FORMATS: Final[Mapping[SecretKind, re.Pattern[str]]] = {
    **{
        kind: re.compile(rf"{_PREFIX_BOUNDARY}(?P<secret>{value})")
        for kind, value in _VENDOR_VALUES.items()
    },
    SecretKind.PRIVATE_KEY: re.compile(rf"(?P<secret>{_PEM_BEGIN})"),
}
_PATH_KEYS: Final = tuple(
    re.compile(rf"{_PATH_BOUNDARY}(?:{value})")
    for kind, value in _VENDOR_VALUES.items()
    if kind is not SecretKind.OPENAI_KEY
)
# OpenAI's lookahead for a digit would rescan the rest of the run at each ``sk-`` a separator
# precedes, so in a path its whole run is matched once and searched for a digit in code. An
# Anthropic key it takes in is a secret all the same.
_PATH_OPENAI_KEY: Final = re.compile(rf"{_PATH_BOUNDARY}sk-(?P<value>[A-Za-z0-9_-]{{20,}})")

# The credential's name: its last word is one of these, at the name's start or after `_`, `-`
# or `.`, and nothing follows it in the name.
_NAME: Final = (
    r"(?:[A-Za-z0-9_.-]*[_.-])?(?i:password|passwd|secret|token|api_key|apikey)"
    r"(?![A-Za-z0-9_.-])"
)
_OPEN_QUOTE: Final = r"""(?P<quote>["'])"""
# The other quote character may appear inside the value.
_VALUE_AND_CLOSE_QUOTE: Final = r"(?P<value>(?:(?!(?P=quote))\S){16,})(?P=quote)"
# An unquoted name before `:` may be a type annotation, such as `token: "Optional[Token]"`.
_NOT_TYPE_LIKE: Final = r"(?![A-Za-z.\[\]|]+(?P=quote))"

_ASSIGNMENTS: Final = (
    # `name = "..."`, optionally after a type: `token: str = "..."`.
    re.compile(
        rf"(?<![A-Za-z0-9_.-]){_NAME}[ \t]*(?::[A-Za-z0-9_.\[\] \t|]*)?=[ \t]*"
        rf"{_OPEN_QUOTE}{_VALUE_AND_CLOSE_QUOTE}"
    ),
    # JSON: `"name": "..."`.
    re.compile(
        rf"""(?P<name_quote>["']){_NAME}(?P=name_quote)[ \t]*:[ \t]*"""
        rf"{_OPEN_QUOTE}{_VALUE_AND_CLOSE_QUOTE}"
    ),
    # YAML, with the name first on its line after the diff markers, indent and any list dash.
    # Possessive for the same reason as the prefix boundary.
    re.compile(
        rf"^[-+ \t]*+{_NAME}[ \t]*:[ \t]*"
        rf"{_OPEN_QUOTE}{_NOT_TYPE_LIKE}{_VALUE_AND_CLOSE_QUOTE}"
    ),
)

_PLACEHOLDER_MARKERS: Final = (
    "your",
    "example",
    "placeholder",
    "changeme",
    "replace",
    "xxx",
    "<",
    "${",
)

_PEM_MARKER: Final = re.compile(rf"(?P<begin>{_PEM_BEGIN})|{_PEM_END}")


class _Span(NamedTuple):
    start: int
    end: int
    kind: SecretKind


def path_holds_secret(path: str) -> bool:
    """Whether ``path`` holds a possible secret anywhere.

    That is any ``scan_secrets`` hit, or a vendor key that starts after ``/``, ``_``, ``-`` or
    ``.``, as in ``cfg_<key>.txt``. A constant number of passes over ``path``.
    """
    return (
        bool(scan_secrets(path))
        or any(pattern.search(path) for pattern in _PATH_KEYS)
        or any(_has_digit(match["value"]) for match in _PATH_OPENAI_KEY.finditer(path))
    )


def _has_digit(text: str) -> bool:
    return any(character in "0123456789" for character in text)


def scan_secrets(text: str) -> tuple[SecretHit, ...]:
    """Return every possible secret in ``text``, in order of line, then position in the line.

    Lines end only at a line feed, as in a diff, so ``diff_line`` matches the diff file's lines.
    """
    return tuple(
        SecretHit(span.kind, line_number)
        for line_number, line in enumerate(text.split("\n"), start=1)
        for span in _find_secrets(line)
    )


class SecretSpan(NamedTuple):
    """Lines ``first_line`` to ``last_line`` of a text, both included and counted from 1."""

    first_line: int
    last_line: int


def secret_line_spans(text: str) -> tuple[SecretSpan, ...]:
    """Return the lines of ``text`` that may hold a secret, merged so they are sorted and disjoint.

    Each hit of ``scan_secrets`` covers its line, and each private key block covers its lines
    as ``redact`` bounds the block. Lines end only at a line feed.
    """
    spans = sorted(
        [SecretSpan(hit.diff_line, hit.diff_line) for hit in scan_secrets(text)]
        + _private_key_block_spans(text)
    )
    merged: list[SecretSpan] = []
    for span in spans:
        if merged and span.first_line <= merged[-1].last_line:
            merged[-1] = SecretSpan(
                merged[-1].first_line, max(merged[-1].last_line, span.last_line)
            )
        else:
            merged.append(span)
    return tuple(merged)


def redact(text: str) -> str:
    """Return ``text`` with each secret value, and each whole private key block, as ``[REDACTED]``.

    A private key block runs from its ``BEGIN`` line to its ``END`` line. With no ``END``, it runs
    to the end of the text; an ``END`` with no ``BEGIN`` is redacted from the start of the text,
    or from the end of the block before it.
    """
    lines = _redact_private_key_blocks(text).split("\n")
    return "\n".join(_redact_line(line) for line in lines)


def _find_secrets(line: str) -> list[_Span]:
    vendor_spans = [
        _Span(match.start("secret"), match.end("secret"), kind)
        for kind, pattern in _VENDOR_FORMATS.items()
        for match in pattern.finditer(line)
    ]
    credential_spans = (
        _Span(match.start("value"), match.end("value"), SecretKind.ASSIGNED_CREDENTIAL)
        for pattern in _ASSIGNMENTS
        for match in pattern.finditer(line)
        if _is_credential_value(match["value"])
    )
    # A value that holds a vendor-format secret is already reported under that kind.
    unreported_credentials = _spans_clear_of(credential_spans, vendor_spans)
    return sorted(vendor_spans + unreported_credentials)


def _is_credential_value(value: str) -> bool:
    # Case-sensitive, so a random value holding `XXX` or `Example` is still reported.
    return not (
        value.startswith(("http://", "https://"))
        or any(marker in value for marker in _PLACEHOLDER_MARKERS)
    )


def _spans_clear_of(spans: Iterable[_Span], others: Iterable[_Span]) -> list[_Span]:
    """Return the ``spans`` that overlap none of ``others``, which may nest or overlap each other.

    Merged, ``others`` cover disjoint ranges sorted by both start and end, so only the first range
    ending after a span starts can overlap it. A binary search finds it, so a line of many hits
    stays fast.
    """
    covered = _covered_ranges(others)
    covered_ends = [end for _, end in covered]
    clear = []
    for span in spans:
        index = bisect.bisect_right(covered_ends, span.start)
        if index == len(covered) or covered[index][0] >= span.end:
            clear.append(span)
    return clear


def _covered_ranges(spans: Iterable[_Span]) -> list[tuple[int, int]]:
    """Return the ranges ``spans`` cover, merged so they are sorted and disjoint."""
    ranges: list[tuple[int, int]] = []
    for start, end, _ in sorted(spans):
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def _redact_line(line: str) -> str:
    pieces: list[str] = []
    copied_to = 0
    for span in _find_secrets(line):
        pieces += [line[copied_to : span.start], _REDACTED]
        # Copy on only from where the furthest span so far ends, so no part of an overlapping
        # span is kept.
        copied_to = max(copied_to, span.end)
    pieces.append(line[copied_to:])
    return "".join(pieces)


def _private_key_block_spans(text: str) -> list[SecretSpan]:
    """The lines of each private key block, bounded as ``_redact_private_key_blocks`` bounds it.

    A block runs from its ``BEGIN`` line to its ``END`` line, or to the end of the text with no
    ``END``; an ``END`` with no ``BEGIN`` runs from line 1, or from the line the block before it
    ends on.
    """
    spans: list[SecretSpan] = []
    line, counted_to = 1, 0
    block_first: int | None = None
    previous_end = 1
    for marker in _PEM_MARKER.finditer(text):
        line += text.count("\n", counted_to, marker.start())
        counted_to = marker.start()
        if marker["begin"] is None:
            spans.append(SecretSpan(previous_end if block_first is None else block_first, line))
            previous_end, block_first = line, None
        elif block_first is None:
            block_first = line
    if block_first is not None:
        spans.append(SecretSpan(block_first, line + text.count("\n", counted_to)))
    return spans


def _redact_private_key_blocks(text: str) -> str:
    pieces: list[str] = []
    copied_to = 0
    block_start: int | None = None
    for marker in _PEM_MARKER.finditer(text):
        if marker["begin"] is None:
            start = copied_to if block_start is None else block_start
            pieces += [text[copied_to:start], _REDACTED]
            copied_to, block_start = marker.end(), None
        elif block_start is None:
            block_start = marker.start()
    if block_start is not None:
        pieces += [text[copied_to:block_start], _REDACTED]
        copied_to = len(text)
    pieces.append(text[copied_to:])
    return "".join(pieces)
