"""The GitHub API a pull request run uses, behind a ``Protocol`` (Phase 2 design, Interfaces).

``RestGitHubClient`` implements it against GitHub, and ``FakeGitHubClient`` in memory for tests.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from review_bot.findings import Severity
from review_bot.pr.event import PullRequestRef

__all__ = [
    "BOT_LOGIN",
    "BOT_USER_TYPE",
    "BotComment",
    "GitHubClient",
    "NewComment",
    "Resolver",
]

# The bot's comments are those by this REST user (ADR 0008, ADR 0009).
BOT_LOGIN: Final = "github-actions[bot]"
BOT_USER_TYPE: Final = "Bot"


class Resolver(StrEnum):
    """Who resolved a comment's thread."""

    BOT = "bot"
    PERSON = "person"


@dataclass(frozen=True, slots=True)
class BotComment:
    """One of the bot's own inline comments, read back from its author and marker.

    ``fingerprint``, ``rule_part`` and ``anchor`` are its marker's, and ``severity`` is the one
    its body shows. ``line`` is None when GitHub marks the comment outdated. ``resolved_by`` is
    None while its thread is open.
    """

    comment_id: int
    thread_id: str
    fingerprint: str
    rule_part: str
    anchor: str
    path: str
    line: int | None
    severity: Severity
    resolved_by: Resolver | None


@dataclass(frozen=True, slots=True)
class NewComment:
    """An inline comment to post on ``line`` of ``path``, on the new side of the diff."""

    path: str
    line: int
    body: str


class GitHubClient(Protocol):
    """The calls a comment-mode run makes. Each raises ``GitHubError`` naming itself on failure."""

    def list_bot_comments(self, pr: PullRequestRef) -> tuple[BotComment, ...]:
        """Return the bot's own inline comments on ``pr``, oldest first."""
        ...

    def post_review(self, pr: PullRequestRef, comments: Sequence[NewComment]) -> None:
        """Post ``comments`` on ``pr``'s head commit, as one review."""
        ...

    def update_comment(self, comment_id: int, body: str) -> None:
        """Replace an inline comment's body."""
        ...

    def resolve_thread(self, thread_id: str) -> None:
        """Resolve a comment's thread."""
        ...

    def write_summary(self, pr: PullRequestRef, body: str) -> None:
        """Create the bot's summary comment on ``pr``, or edit it in place if it exists."""
        ...
