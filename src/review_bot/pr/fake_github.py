"""An in-memory GitHub for tests, in ``src/`` as ``MockProvider`` is (Phase 2 design, Module map).

It holds one pull request's inline comments, each its own thread with who resolved it, and the
bot's summary. It records every call, and can be told to fail one operation by name. It doesn't
work out where GitHub would show a comment after a push: a test moves its line, or marks it
outdated.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from review_bot.errors import GitHubError
from review_bot.pr.comments import read_marker
from review_bot.pr.event import PullRequestRef
from review_bot.pr.github import BOT_LOGIN, BOT_USER_TYPE, BotComment, NewComment, Resolver

__all__ = ["FakeCall", "FakeComment", "FakeGitHubClient", "FakeReview"]


@dataclass(slots=True)
class FakeComment:
    """One inline comment and its thread. ``author`` is the REST ``user.login``."""

    comment_id: int
    path: str
    line: int
    body: str
    author: str = BOT_LOGIN
    author_type: str = BOT_USER_TYPE
    is_outdated: bool = False
    resolved_by: Resolver | None = None

    @property
    def thread_id(self) -> str:
        """Its thread's ID; each comment is its own thread."""
        return f"thread-{self.comment_id}"


@dataclass(frozen=True, slots=True)
class FakeReview:
    """A review the bot posted: the commit it is on, and its comments."""

    commit_sha: str
    comments: tuple[NewComment, ...]


@dataclass(frozen=True, slots=True)
class FakeCall:
    """One call to the client: the operation's name and its arguments."""

    name: str
    args: tuple[object, ...]


class FakeGitHubClient:
    """A ``GitHubClient`` over one pull request held in memory.

    ``comments`` are oldest first, and IDs grow with each one added or posted. ``summary`` is
    the bot's summary body, or None before one is written. ``reviews`` are the reviews the bot
    posted, and ``calls`` every call made through ``GitHubClient``, in order.
    """

    def __init__(self) -> None:
        """Start with no comments, no summary, no calls and no failing operation."""
        self.comments: list[FakeComment] = []
        self.summary: str | None = None
        self.reviews: list[FakeReview] = []
        self.calls: list[FakeCall] = []
        self._failing: str | None = None

    def add_comment(
        self,
        *,
        path: str,
        line: int,
        body: str,
        author: str = BOT_LOGIN,
        author_type: str = BOT_USER_TYPE,
    ) -> int:
        """Add an open comment, as if posted before the run, and return its ID."""
        comment_id = len(self.comments) + 1
        self.comments.append(FakeComment(comment_id, path, line, body, author, author_type))
        return comment_id

    def mark_outdated(self, comment_id: int) -> None:
        """Mark a comment outdated, as GitHub does when the diff around its line changes."""
        self._held(comment_id).is_outdated = True

    def set_line(self, comment_id: int, line: int) -> None:
        """Move a comment to the line where GitHub now shows it, after a push."""
        self._held(comment_id).line = line

    def resolve(self, comment_id: int, *, by: str) -> None:
        """Resolve a comment's thread, as ``bot`` or ``person``."""
        self._held(comment_id).resolved_by = Resolver(by)

    def fail_on(self, name: str) -> None:
        """Make every later call to the operation ``name`` raise ``GitHubError``, once recorded."""
        self._failing = name

    def list_bot_comments(self, pr: PullRequestRef) -> tuple[BotComment, ...]:
        """Return the comments by the bot whose body ``read_marker`` reads, oldest first."""
        self._record("list_bot_comments", pr)
        return tuple(
            BotComment(
                comment_id=comment.comment_id,
                thread_id=comment.thread_id,
                fingerprint=marker.fingerprint,
                rule_part=marker.rule_part,
                anchor=marker.anchor,
                path=comment.path,
                line=None if comment.is_outdated else comment.line,
                severity=marker.severity,
                resolved_by=comment.resolved_by,
            )
            for comment in self.comments
            # The author first, so nobody else's text is parsed.
            if comment.author == BOT_LOGIN
            and comment.author_type == BOT_USER_TYPE
            and (marker := read_marker(comment.body)) is not None
        )

    def post_review(self, pr: PullRequestRef, comments: Sequence[NewComment]) -> None:
        """Post ``comments`` by the bot, as one review on ``pr``'s head commit."""
        self._record("post_review", pr, tuple(comments))
        self.reviews.append(FakeReview(pr.head_sha, tuple(comments)))
        for new in comments:
            self.comments.append(FakeComment(len(self.comments) + 1, new.path, new.line, new.body))

    def update_comment(self, comment_id: int, body: str) -> None:
        """Replace a comment's body."""
        self._record("update_comment", comment_id, body)
        comment = self._find(comment_id)
        if comment is None:
            raise GitHubError(f"GitHub update_comment failed: no comment {comment_id}.")
        comment.body = body

    def resolve_thread(self, thread_id: str) -> None:
        """Resolve a thread, as the bot."""
        self._record("resolve_thread", thread_id)
        thread = [comment for comment in self.comments if comment.thread_id == thread_id]
        if not thread:
            raise GitHubError(f"GitHub resolve_thread failed: no thread {thread_id}.")
        for comment in thread:
            comment.resolved_by = Resolver.BOT

    def write_summary(self, pr: PullRequestRef, body: str) -> None:
        """Write the bot's summary, replacing any earlier one."""
        self._record("write_summary", pr, body)
        self.summary = body

    def _record(self, name: str, *args: object) -> None:
        self.calls.append(FakeCall(name, args))
        if name == self._failing:
            raise GitHubError(f"GitHub {name} failed: FakeGitHubClient was told to fail it.")

    def _find(self, comment_id: int) -> FakeComment | None:
        return next(
            (comment for comment in self.comments if comment.comment_id == comment_id), None
        )

    def _held(self, comment_id: int) -> FakeComment:
        comment = self._find(comment_id)
        if comment is None:
            raise ValueError(f"FakeGitHubClient holds no comment {comment_id}.")
        return comment
