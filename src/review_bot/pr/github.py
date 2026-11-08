"""The GitHub API a pull request run uses, behind a ``Protocol`` (Phase 2 design, Interfaces).

``RestGitHubClient`` implements it against GitHub, and ``FakeGitHubClient`` in memory for tests.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Final, Protocol

from review_bot.errors import GitHubError
from review_bot.findings import Severity
from review_bot.pr.event import PullRequestRef

__all__ = [
    "BOT_LOGIN",
    "BOT_USER_TYPE",
    "MAX_MERGED_PULLS",
    "BotComment",
    "CommentSignals",
    "GitHubClient",
    "MergedPullRequest",
    "NewComment",
    "Resolver",
    "Summary",
    "in_window",
    "is_any_bot",
    "is_bot_user",
    "too_many_merged_pulls",
]

# The bot's comments are those by this REST user (ADR 0008, ADR 0009).
BOT_LOGIN: Final = "github-actions[bot]"
BOT_USER_TYPE: Final = "Bot"
# The most merged pull requests one baseline window may cover (P2-S11, Limits).
MAX_MERGED_PULLS: Final = 1000


def is_bot_user(login: str, user_type: str) -> bool:
    """Whether a REST user, by its ``login`` and ``type``, is the bot."""
    return login == BOT_LOGIN and user_type == BOT_USER_TYPE


def is_any_bot(user_type: str) -> bool:
    """Whether a REST user of this ``type`` is any bot; reactions by any bot are ignored."""
    return user_type == BOT_USER_TYPE


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
class CommentSignals:
    """One of the bot's own inline comments, with the reactions people left on it.

    ``reactions`` holds the content values, such as ``+1``, ``-1`` or ``heart``, of the
    reactions by people: users of any type but ``Bot``. Who left them isn't kept. Who resolved
    the comment's thread is ``comment.resolved_by``.
    """

    comment: BotComment
    reactions: frozenset[str]


@dataclass(frozen=True, slots=True)
class NewComment:
    """An inline comment to post on ``line`` of ``path``, on the new side of the diff."""

    path: str
    line: int
    body: str


@dataclass(frozen=True, slots=True)
class Summary:
    """The bot's summary comment, read back from its marker.

    ``reviewed_sha`` is the last commit the bot reviewed, or None if the marker records no
    40-hex SHA, as before P2-S7 or when edited by hand.
    """

    reviewed_sha: str | None


@dataclass(frozen=True, slots=True)
class MergedPullRequest:
    """A merged pull request, as the value baseline reads it (P2-S11).

    ``created_at`` is when it was opened, and ``commit_dates`` hold each of its commits'
    committer dates. All are timezone-aware.
    """

    number: int
    title: str
    created_at: datetime
    merged_at: datetime
    commit_dates: tuple[datetime, ...]


def in_window(moment: datetime, since: date, until: date) -> bool:
    """Whether ``moment`` falls on a UTC day from ``since`` to ``until``, both included."""
    return since <= moment.astimezone(UTC).date() <= until


def too_many_merged_pulls(count: int) -> GitHubError:
    """The error for a baseline window covering more than ``MAX_MERGED_PULLS`` merged PRs."""
    return GitHubError(
        f"GitHub list_merged_pull_requests failed: the window holds {count} merged pull "
        f"requests, more than the limit of {MAX_MERGED_PULLS}. Pass a shorter window."
    )


class GitHubClient(Protocol):
    """The calls a comment-mode run, ``collect`` and ``baseline`` make.

    Each raises ``GitHubError`` naming itself on failure.
    """

    def list_bot_comments(self, pr: PullRequestRef) -> tuple[BotComment, ...]:
        """Return the bot's own inline comments on ``pr``, oldest first."""
        ...

    def list_signals(self, pr: PullRequestRef) -> tuple[CommentSignals, ...]:
        """Return the bot's own inline comments on ``pr``, oldest first, with their reactions."""
        ...

    def read_summary(self, pr: PullRequestRef) -> Summary | None:
        """Return the bot's summary comment on ``pr``, or None if it has none."""
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

    def write_summary(self, pr: PullRequestRef, body: str, *, reviewed_sha: str | None) -> None:
        """Create the bot's summary comment on ``pr``, or edit it in place if it exists.

        Its marker records ``reviewed_sha``, the last commit reviewed, or ``none`` if None.
        """
        ...

    def list_merged_pull_requests(
        self, repo: str, since: date, until: date
    ) -> tuple[MergedPullRequest, ...]:
        """Return ``repo``'s pull requests merged in the window, with their commits' dates.

        The window is the UTC days from ``since`` to ``until``, both included (``in_window``).
        Pull requests closed without merging aren't returned.

        Raises:
            GitHubError: If a call fails, or more than ``MAX_MERGED_PULLS`` were merged in the
                window (``too_many_merged_pulls``).
        """
        ...

    def count_issues(self, repo: str, label: str, since: date, until: date) -> int:
        """Return how many of ``repo``'s issues labelled ``label`` were created in the window.

        The window is as ``list_merged_pull_requests``'s; pull requests aren't counted.
        """
        ...
