"""The issues a pull request closes, and the part of each one a context review may send.

Pure. Issue text is untrusted: it is secret-scanned, masked as the policy asks, capped, and only
ever sent framed as data (P3-S9, P4-S4).
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from review_bot.governance.redaction import (
    RedactionCounts,
    RedactionKind,
    add_counts,
    mask_personal_data,
    placeholder,
)
from review_bot.secret_scan import scan_secrets

__all__ = [
    "MAX_ISSUE_BODY_CHARS",
    "MAX_ISSUE_TITLE_CHARS",
    "MAX_LINKED_ISSUES",
    "MAX_LISTED_ISSUES",
    "IssueStatus",
    "IssueText",
    "LinkedIssue",
    "linked_issue",
    "linked_issue_numbers",
    "read_linked_issues",
]

# The most issues a review reads; any further one is only listed (P3-S9 Limits).
MAX_LINKED_ISSUES: Final = 3
# The most distinct issues one body links, read or listed, so no report grows with the body.
MAX_LISTED_ISSUES: Final = 50
MAX_ISSUE_TITLE_CHARS: Final = 256
MAX_ISSUE_BODY_CHARS: Final = 8_000

# GitHub's closing keywords, each optionally followed by a colon.
_CLOSING_KEYWORDS: Final = frozenset(
    {"close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved"}
)
_LONGEST_KEYWORD: Final = len("resolves:")
# One run of non-space characters: a single greedy class, so matching never backtracks.
_WORD: Final = re.compile(r"\S+")
# What may follow a reference in prose, as in "Fixes #12, and ...".
_TRAILING_PUNCTUATION: Final = ".,;:!?)"
# GitHub's issue numbers fit in 32 bits, so no reference needs more digits than this.
_MAX_NUMBER_DIGITS: Final = 10


class IssueStatus(StrEnum):
    """What became of one linked issue."""

    INCLUDED = "included"
    CUT = "cut"
    NOT_FOUND = "not_found"
    IS_PULL_REQUEST = "is_pull_request"
    UNAVAILABLE = "unavailable"
    NO_TOKEN = "no_token"  # noqa: S105 - a status's name, not a secret
    POSSIBLE_SECRET = "possible_secret"  # noqa: S105 - a status's name, not a secret
    OVER_LIMIT = "over_limit"


@dataclass(frozen=True, slots=True)
class IssueText:
    """An issue as GitHub returns it: its title and body, and whether it is a pull request.

    Both are untrusted. GitHub's null body is an empty one.
    """

    title: str
    body: str
    is_pull_request: bool = False


@dataclass(frozen=True, slots=True)
class LinkedIssue:
    """One linked issue, by number, what became of it, and the text a review sends of it.

    ``text`` is the title, a blank line and the body, each masked, then cut to its limit and
    marked if it was; it is empty unless the status is ``included`` or ``cut``. ``redacted``
    counts the masked values ``text`` holds.
    """

    number: int
    status: IssueStatus
    text: str = ""
    redacted: RedactionCounts = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse text for an issue that isn't sent, so no record can carry it by mistake."""
        if self.text and not self.is_sent:
            raise ValueError(f"Issue #{self.number} is {self.status}, so it has no text to send.")

    @property
    def is_sent(self) -> bool:
        """Whether a review sends this issue's text to the model."""
        return self.status in {IssueStatus.INCLUDED, IssueStatus.CUT}


def linked_issue_numbers(body: str, repo: str) -> tuple[int, ...]:
    """The issues of ``repo`` that a pull request body closes, each once, in order of mention.

    A closing keyword (one of ``_CLOSING_KEYWORDS``, such as ``Fixes``, in any case, with an
    optional colon) is followed, after white space, by ``#N`` or by
    ``owner/name#N`` naming ``repo``, in any case. A reference to another repository, one with
    no keyword before it, or one joined to other text is ignored. At most ``MAX_LISTED_ISSUES``
    numbers are returned.

    Each word is looked at once, with no pattern that can backtrack, so the time is linear in
    the body's length.
    """
    numbers: dict[int, None] = {}
    follows_keyword = False
    for match in _WORD.finditer(body):
        word = match.group()
        if follows_keyword and (number := _issue_number(word, repo)) is not None:
            numbers[number] = None
            if len(numbers) == MAX_LISTED_ISSUES:
                break
        follows_keyword = _is_closing_keyword(word)
    return tuple(numbers)


def _is_closing_keyword(word: str) -> bool:
    # ASCII only, so no other script's letter folds into a keyword.
    if len(word) > _LONGEST_KEYWORD or not word.isascii():
        return False
    return word.lower().removesuffix(":") in _CLOSING_KEYWORDS


def _issue_number(word: str, repo: str) -> int | None:
    """The number ``word`` refers to as ``#N`` or ``repo#N``, or None if it is neither."""
    owner_name, hash_sign, digits = word.rstrip(_TRAILING_PUNCTUATION).partition("#")
    if not hash_sign or (owner_name and owner_name.lower() != repo.lower()):
        return None
    if not (digits.isascii() and digits.isdigit() and len(digits) <= _MAX_NUMBER_DIGITS):
        return None
    number = int(digits)
    return number if number > 0 else None


def read_linked_issues(
    numbers: Iterable[int], read: Callable[[int], LinkedIssue]
) -> tuple[LinkedIssue, ...]:
    """Each of ``numbers``, in order: the first ``MAX_LINKED_ISSUES`` as ``read`` makes them.

    Any further one isn't read, and is ``over_limit``.
    """
    return tuple(
        read(number)
        if position < MAX_LINKED_ISSUES
        else LinkedIssue(number, IssueStatus.OVER_LIMIT)
        for position, number in enumerate(numbers)
    )


def linked_issue(
    number: int, issue: IssueText | None, kinds: frozenset[RedactionKind] = frozenset()
) -> LinkedIssue:
    """What a review may send of issue ``number``, as read: None means it wasn't found.

    A pull request isn't an issue, and an issue whose title or body holds a possible secret
    sends nothing. Otherwise the ``kinds`` of personal data are masked in its title and body,
    then its title is cut to ``MAX_ISSUE_TITLE_CHARS`` and its body to
    ``MAX_ISSUE_BODY_CHARS``, each with a mark saying so, and the status is ``cut`` if either
    was. Masking first means a cut never splits a value.
    """
    if issue is None:
        return LinkedIssue(number, IssueStatus.NOT_FOUND)
    if issue.is_pull_request:
        return LinkedIssue(number, IssueStatus.IS_PULL_REQUEST)
    if scan_secrets(issue.title) or scan_secrets(issue.body):
        return LinkedIssue(number, IssueStatus.POSSIBLE_SECRET)
    title, is_title_cut, title_counts = _masked_and_cut(
        issue.title, kinds, MAX_ISSUE_TITLE_CHARS, "title"
    )
    body, is_body_cut, body_counts = _masked_and_cut(
        issue.body, kinds, MAX_ISSUE_BODY_CHARS, "body"
    )
    status = IssueStatus.CUT if is_title_cut or is_body_cut else IssueStatus.INCLUDED
    redacted = add_counts([title_counts, body_counts])
    return LinkedIssue(number, status, f"{title}\n\n{body}", redacted)


def _masked_and_cut(
    text: str, kinds: frozenset[RedactionKind], max_chars: int, name: str
) -> tuple[str, bool, RedactionCounts]:
    """``text`` masked, then cut as ``_cut`` cuts it, and how many masked values it still holds.

    A placeholder the cut drops or splits isn't counted. One that was in ``text`` already looks
    the same as a masked one, so each kind's count is at most the number masked.
    """
    masked = mask_personal_data(text, kinds)
    kept, is_cut = _cut(masked.text, max_chars, name)
    counts = {
        kind: min(count, kept.count(placeholder(kind))) for kind, count in masked.counts.items()
    }
    return kept, is_cut, add_counts([counts])


def _cut(text: str, max_chars: int, name: str) -> tuple[str, bool]:
    """``text`` cut to ``max_chars``, marked if it was, and whether it was."""
    if len(text) <= max_chars:
        return text, False
    return f"{text[:max_chars]} [{name} cut: over {max_chars:,} characters]", True
