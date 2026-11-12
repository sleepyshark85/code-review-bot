"""Mask personal data with typed placeholders before text is sent to a model (ADR 0022).

Pure. The text is untrusted and as long as a diff or a tool result, so, as in ``secret_scan``,
every pattern matches within one line and in linear time: a match starts only where a run of
its characters starts, and each run is consumed once. Each match becomes ``[REDACTED:<kind>]``
on its own line, so lines keep their numbers. Addresses in the reserved documentation domains
and ranges are left alone (Phase 4 design, Redaction).
"""

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

__all__ = [
    "Masked",
    "RedactionCounts",
    "RedactionKind",
    "add_counts",
    "mask_diff",
    "mask_personal_data",
]


class RedactionKind(StrEnum):
    """A kind of personal data a policy can have masked, in the order they are masked."""

    EMAIL = "email"
    PHONE = "phone"
    IPV4 = "ipv4"


# How many values of each kind were masked; a kind with none is left out.
type RedactionCounts = Mapping[RedactionKind, int]


@dataclass(frozen=True, slots=True)
class Masked:
    """Text with personal data masked, and how many values of each kind were."""

    text: str
    counts: RedactionCounts = field(default_factory=dict)


# Possessive runs, and a lookbehind so a match starts only at the start of a run, keep each
# pattern linear: no run is rescanned from a later position.
_LOCAL_CHARS: Final = r"A-Za-z0-9._%+-"
_EMAIL: Final = re.compile(
    rf"(?<![{_LOCAL_CHARS}])[{_LOCAL_CHARS}]++@"
    # Labels, then a top-level domain of letters, so a package pin such as `lib@1.2.3` isn't
    # one. The labels may give back their last dot, so `a@b.com.` ends before it.
    r"(?P<domain>(?:[A-Za-z0-9-]++\.)+[A-Za-z]{2,}+)(?![A-Za-z0-9-])"
)
# E.164: `+`, then digits in groups split by one space or hyphen; their count is checked apart.
# A group is given back if what follows it would continue it, so `+12025550143 10.0.0.1` ends
# before ` 10`.
_PHONE: Final = re.compile(r"(?<![A-Za-z0-9_+])\+[0-9]++(?:[ -][0-9]++)*(?![A-Za-z0-9_]|\.[0-9])")
# Four parts of at most three digits, not part of a longer dotted number, version or word.
_IPV4: Final = re.compile(
    r"(?<![A-Za-z0-9_.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![A-Za-z0-9_]|\.[0-9])"
)

# RFC 2606 and RFC 6761: names that are only ever examples.
_DOCUMENTATION_DOMAINS: Final = ("example.com", "example.org", "example.net")
_DOCUMENTATION_TOP_LEVEL: Final = frozenset({"example", "test", "invalid"})
# RFC 5737: TEST-NET-1, -2 and -3, by their first three parts.
_DOCUMENTATION_NETWORKS: Final = frozenset({(192, 0, 2), (198, 51, 100), (203, 0, 113)})
_MIN_PHONE_DIGITS: Final = 8
_MAX_PHONE_DIGITS: Final = 15
_MAX_OCTET: Final = 255
_DIFF_MARKERS: Final = "+- "


def _is_personal_email(match: re.Match[str]) -> bool:
    domain = match["domain"].lower()
    is_documentation = domain.rpartition(".")[2] in _DOCUMENTATION_TOP_LEVEL or any(
        domain == name or domain.endswith(f".{name}") for name in _DOCUMENTATION_DOMAINS
    )
    return not is_documentation


def _is_personal_phone(match: re.Match[str]) -> bool:
    digits = sum(char in "0123456789" for char in match[0])
    return _MIN_PHONE_DIGITS <= digits <= _MAX_PHONE_DIGITS


def _is_personal_ipv4(match: re.Match[str]) -> bool:
    octets = tuple(int(part) for part in match[0].split("."))
    return max(octets) <= _MAX_OCTET and octets[:3] not in _DOCUMENTATION_NETWORKS


# Each kind's pattern, and the check a match must pass to be masked.
_KINDS: Final[Mapping[RedactionKind, tuple[re.Pattern[str], Callable[[re.Match[str]], bool]]]] = {
    RedactionKind.EMAIL: (_EMAIL, _is_personal_email),
    RedactionKind.PHONE: (_PHONE, _is_personal_phone),
    RedactionKind.IPV4: (_IPV4, _is_personal_ipv4),
}


def mask_personal_data(text: str, kinds: frozenset[RedactionKind]) -> Masked:
    """``text`` with each value of ``kinds`` replaced by ``[REDACTED:<kind>]``, and the counts.

    Kinds are masked in catalogue order, each over the text the one before left, so no value is
    counted twice. A placeholder matches no pattern, so masked text masks to itself. With no
    kinds, ``text`` is returned as it is.
    """
    counts: dict[RedactionKind, int] = {}
    for kind in RedactionKind:
        if kind in kinds:
            text, masked = _mask_kind(text, kind)
            if masked:
                counts[kind] = masked
    return Masked(text, counts)


def _mask_kind(text: str, kind: RedactionKind) -> tuple[str, int]:
    """``text`` with each value of ``kind`` masked, and how many were."""
    pattern, is_personal = _KINDS[kind]
    placeholder = f"[REDACTED:{kind}]"
    masked = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal masked
        if not is_personal(match):
            return match[0]
        masked += 1
        return placeholder

    return pattern.sub(replace, text), masked


def mask_diff(diff: str, kinds: frozenset[RedactionKind]) -> Masked:
    """``diff`` masked as ``mask_personal_data`` masks text, keeping each line's diff marker.

    A line's leading ``+``, ``-`` or space is a marker, not part of its text, so an added line
    ``12025550143`` isn't read as ``+12025550143``, and the marker is never masked away.
    """
    if not kinds:
        return Masked(diff)
    lines: list[str] = []
    line_counts: list[RedactionCounts] = []
    for line in diff.split("\n"):
        marker = line[:1] if line[:1] in _DIFF_MARKERS else ""
        masked = mask_personal_data(line[len(marker) :], kinds)
        lines.append(marker + masked.text)
        line_counts.append(masked.counts)
    return Masked("\n".join(lines), add_counts(line_counts))


def add_counts(counts: Iterable[RedactionCounts]) -> RedactionCounts:
    """The sum of ``counts``, kind by kind, leaving out any kind with none."""
    total: Counter[RedactionKind] = Counter()
    for each in counts:
        total.update(each)
    return {kind: count for kind, count in total.items() if count}
