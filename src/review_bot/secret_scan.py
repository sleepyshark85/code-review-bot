"""Detect and redact secrets in raw text, by vendor format and one assignment pattern.

The forms, boundaries and near misses are specified in
``docs/phases/phase-1/slices/p1-s3-secret-block.md``. Every pattern is matched within one line
and runs in linear time, because the text is an untrusted diff of up to the size limit.
"""

import bisect
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NamedTuple

__all__ = ["SecretHit", "SecretKind", "redact", "scan_secrets"]


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


def _vendor_format(value_pattern: str) -> re.Pattern[str]:
    return re.compile(rf"{_PREFIX_BOUNDARY}(?P<secret>{value_pattern})")


# Each pattern's `secret` group is the value. The Anthropic prefix is spelt with a group so the
# repository's key-literal check can't mistake a pattern for a key.
_VENDOR_FORMATS: Final[Mapping[SecretKind, re.Pattern[str]]] = {
    SecretKind.AWS_ACCESS_KEY: _vendor_format(r"AKIA[A-Z0-9]{16}(?![A-Z0-9])"),
    SecretKind.GITHUB_TOKEN: _vendor_format(
        r"gh[pousr]_[A-Za-z0-9]{36}(?![A-Za-z0-9])|github_pat_[A-Za-z0-9_]{22,}"
    ),
    SecretKind.ANTHROPIC_KEY: _vendor_format(r"(?:sk)-ant-[A-Za-z0-9_-]{20,}"),
    # Not where an Anthropic key matches, and with a digit somewhere in the value.
    SecretKind.OPENAI_KEY: _vendor_format(
        r"sk-(?!ant-[A-Za-z0-9_-]{20})(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{20,}"
    ),
    SecretKind.SLACK_TOKEN: _vendor_format(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    SecretKind.PRIVATE_KEY: re.compile(rf"(?P<secret>{_PEM_BEGIN})"),
}

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


def scan_secrets(text: str) -> tuple[SecretHit, ...]:
    """Return every possible secret in ``text``, in order of line, then position in the line.

    Lines end only at a line feed, as in a diff, so ``diff_line`` matches the diff file's lines.
    """
    return tuple(
        SecretHit(span.kind, line_number)
        for line_number, line in enumerate(text.split("\n"), start=1)
        for span in _find_secrets(line)
    )


def redact(text: str) -> str:
    """Return ``text`` with each secret value, and each whole private key block, as ``[REDACTED]``.

    A private key block runs from its ``BEGIN`` line to its ``END`` line. With no ``END``, it runs
    to the end of the text; an ``END`` with no ``BEGIN`` is redacted from the start of the text,
    or from the end of the block before it.
    """
    lines = _redact_private_key_blocks(text).split("\n")
    return "\n".join(_redact_line(line) for line in lines)


def _find_secrets(line: str) -> list[_Span]:
    vendor_spans = sorted(
        _Span(match.start("secret"), match.end("secret"), kind)
        for kind, pattern in _VENDOR_FORMATS.items()
        for match in pattern.finditer(line)
    )
    credential_spans = (
        _Span(match.start("value"), match.end("value"), SecretKind.ASSIGNED_CREDENTIAL)
        for pattern in _ASSIGNMENTS
        for match in pattern.finditer(line)
        if _is_credential_value(match["value"])
    )
    # A value that holds a vendor-format secret is already reported under that kind.
    unreported_credentials = [
        span for span in credential_spans if not _overlaps_any(span, vendor_spans)
    ]
    return sorted(vendor_spans + unreported_credentials)


def _is_credential_value(value: str) -> bool:
    # Case-sensitive, so a random value holding `XXX` or `Example` is still reported.
    return not (
        value.startswith(("http://", "https://"))
        or any(marker in value for marker in _PLACEHOLDER_MARKERS)
    )


def _overlaps_any(span: _Span, sorted_spans: Sequence[_Span]) -> bool:
    """Say whether ``span`` overlaps any of ``sorted_spans``, which are sorted and disjoint.

    Disjoint spans sorted by start are sorted by end too, so only the first one ending after
    ``span`` starts can overlap it. A binary search finds it, so a line of many hits stays fast.
    """
    index = bisect.bisect_right(sorted_spans, span.start, key=lambda other: other.end)
    return index < len(sorted_spans) and sorted_spans[index].start < span.end


def _redact_line(line: str) -> str:
    pieces: list[str] = []
    copied_to = 0
    for span in _find_secrets(line):
        pieces += [line[copied_to : span.start], _REDACTED]
        # Spans don't overlap today; if one ever did, no part of it would be copied out.
        copied_to = max(copied_to, span.end)
    pieces.append(line[copied_to:])
    return "".join(pieces)


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
