"""Stable finding identity: file, rule part and anchored line text, not line number (ADR 0003).

A fingerprint is the first 16 hex digits of the SHA-256 of the version, the file, the rule part,
the SHA-256 of the anchored line with its whitespace collapsed, and an ordinal (Phase 1 design,
Fingerprints and dedup).
"""

import hashlib
import json
from collections import Counter
from typing import Final

__all__ = ["Fingerprinter", "fingerprint_for"]

_VERSION: Final = "v1"
_HEX_DIGITS: Final = 16


def fingerprint_for(*, file: str, rule: str, anchor_text: str, ordinal: int) -> str:
    """Return the fingerprint of one finding.

    Args:
        file: The diff path the finding is anchored in.
        rule: The rule part: a rule ID, or ``ai:<category>`` for an untagged AI finding.
        anchor_text: The anchored line's text. Only its words count, not its whitespace.
        ordinal: The finding's 0-based place among findings sharing every other part.

    Raises:
        ValueError: If ``ordinal`` is negative.
    """
    if ordinal < 0:
        raise ValueError(f"A fingerprint's ordinal can't be negative, but it is {ordinal}.")
    return _fingerprint(file, rule, _anchor_sha256(anchor_text), ordinal)


class Fingerprinter:
    """Fingerprints findings in the order given, numbering those that share every other part.

    The first finding with a given file, rule part and collapsed anchor text gets ordinal 0, the
    next one 1, and so on, so two identical findings on one line, or two identical lines in one
    file, keep separate identities.
    """

    def __init__(self) -> None:
        """Start with no finding seen."""
        self._seen: Counter[tuple[str, str, str]] = Counter()
        # Each anchor text's hash, worked out once, so many findings on one long line cost
        # that line's length once, not once each.
        self._anchor_sha256s: dict[str, str] = {}

    def fingerprint(self, *, file: str, rule: str, anchor_text: str) -> str:
        """Return the next finding's fingerprint, and count it for the ones after it."""
        anchor_sha256 = self._anchor_sha256s.get(anchor_text)
        if anchor_sha256 is None:
            anchor_sha256 = _anchor_sha256(anchor_text)
            self._anchor_sha256s[anchor_text] = anchor_sha256
        parts = (file, rule, anchor_sha256)
        ordinal = self._seen[parts]
        self._seen[parts] += 1
        return _fingerprint(file, rule, anchor_sha256, ordinal)


def _anchor_sha256(anchor_text: str) -> str:
    """The SHA-256 of ``anchor_text`` with its whitespace collapsed, in hex."""
    # ``surrogatepass``, so text holding a lone surrogate hashes too, instead of raising.
    return hashlib.sha256(_collapsed(anchor_text).encode("utf-8", "surrogatepass")).hexdigest()


def _fingerprint(file: str, rule: str, anchor_sha256: str, ordinal: int) -> str:
    # A JSON array keeps the parts apart whatever characters they hold, and escapes to ASCII.
    parts = json.dumps([_VERSION, file, rule, anchor_sha256, ordinal])
    return hashlib.sha256(parts.encode("ascii")).hexdigest()[:_HEX_DIGITS]


def _collapsed(text: str) -> str:
    """``text`` with each whitespace run made one space, and both ends stripped."""
    return " ".join(text.split())
