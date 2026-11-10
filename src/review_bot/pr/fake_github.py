"""An in-memory GitHub for tests, in ``src/`` as ``MockProvider`` is (Phase 2 design, Module map).

It holds one pull request's inline comments, each its own thread with who resolved it and the
reactions on it, and the bot's summary comment; and for the value baseline and linked issues,
one repository's pull requests and issues. It records every call, and can be told to fail one
operation by name. It doesn't work out where GitHub would show a comment after a push: a test
moves its line, or marks it outdated.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Final

from review_bot.context.issues import IssueText
from review_bot.errors import GitHubError
from review_bot.pr.comments import (
    Marker,
    is_summary,
    read_marker,
    read_reviewed_sha,
    with_summary_marker,
)
from review_bot.pr.event import PullRequestRef
from review_bot.pr.github import (
    BOT_LOGIN,
    BOT_USER_TYPE,
    MAX_MERGED_PULLS,
    BotComment,
    CommentSignals,
    MergedPullRequest,
    NewComment,
    Resolver,
    Summary,
    in_window,
    is_any_bot,
    is_bot_user,
    too_many_merged_pulls,
)

__all__ = [
    "FakeCall",
    "FakeComment",
    "FakeGitHubClient",
    "FakeIssue",
    "FakePull",
    "FakeReaction",
    "FakeReview",
]

# An issue added with no creation time falls in no realistic baseline window.
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FakeReaction:
    """A reaction on a comment: its content, such as ``+1``, and its REST user's login and type."""

    content: str
    user: str
    user_type: str


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
    reactions: list[FakeReaction] = field(default_factory=list)

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
class FakePull:
    """A pull request of the repository.

    ``closed_at`` is None while it is open, and ``merged_at`` None unless it was merged.
    ``commit_dates`` are its commits' committer dates.
    """

    number: int
    title: str
    created_at: datetime
    closed_at: datetime | None
    merged_at: datetime | None
    commit_dates: tuple[datetime, ...]


@dataclass(frozen=True, slots=True)
class FakeIssue:
    """An issue of the repository, never a pull request: labels, creation time, title and body."""

    number: int
    labels: tuple[str, ...]
    created_at: datetime
    title: str = ""
    body: str = ""


@dataclass(frozen=True, slots=True)
class FakeCall:
    """One call to the client: the operation's name and its arguments."""

    name: str
    args: tuple[object, ...]


class FakeGitHubClient:
    """A ``GitHubClient`` over one pull request held in memory.

    ``comments`` are oldest first, and IDs grow with each one added or posted. ``summary`` is
    the body of the bot's summary comment as posted, its marker last, or None before one is
    written; a test may set it to any body. ``reviews`` are the reviews the bot
    posted, and ``calls`` every call made through ``GitHubClient``, in order. ``pulls`` and
    ``issues`` are the repository's, in the order added; they are read whatever repository a
    call names.
    """

    def __init__(self) -> None:
        """Start with no comments, no summary, no calls and no failing operation."""
        self.comments: list[FakeComment] = []
        self.summary: str | None = None
        self.reviews: list[FakeReview] = []
        self.pulls: list[FakePull] = []
        self.issues: list[FakeIssue] = []
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

    def add_pull_request(  # noqa: PLR0913 - keyword-only, one per fact a pull request has
        self,
        *,
        number: int,
        title: str,
        created_at: datetime,
        closed_at: datetime | None,
        merged_at: datetime | None,
        commit_dates: tuple[datetime, ...] = (),
    ) -> None:
        """Add a pull request of the repository, open, closed or merged."""
        self.pulls.append(FakePull(number, title, created_at, closed_at, merged_at, commit_dates))

    def add_issue(
        self,
        *,
        number: int,
        labels: tuple[str, ...] = (),
        created_at: datetime = _EPOCH,
        title: str = "",
        body: str = "",
    ) -> None:
        """Add an issue of the repository, labelled with ``labels``, with its title and body."""
        self.issues.append(FakeIssue(number, labels, created_at, title, body))

    def mark_outdated(self, comment_id: int) -> None:
        """Mark a comment outdated, as GitHub does when the diff around its line changes."""
        self._held(comment_id).is_outdated = True

    def set_line(self, comment_id: int, line: int) -> None:
        """Move a comment to the line where GitHub now shows it, after a push."""
        self._held(comment_id).line = line

    def resolve(self, comment_id: int, *, by: str) -> None:
        """Resolve a comment's thread, as ``bot`` or ``person``."""
        self._held(comment_id).resolved_by = Resolver(by)

    def react(self, comment_id: int, content: str, *, by: str, by_type: str = "User") -> None:
        """Add a reaction to a comment, by the REST user with login ``by`` and type ``by_type``."""
        self._held(comment_id).reactions.append(FakeReaction(content, by, by_type))

    def fail_on(self, name: str) -> None:
        """Make every later call to the operation ``name`` raise ``GitHubError``, once recorded."""
        self._failing = name

    def list_bot_comments(self, pr: PullRequestRef) -> tuple[BotComment, ...]:
        """Return the comments by the bot whose body ``read_marker`` reads, oldest first."""
        self._record("list_bot_comments", pr)
        return tuple(own for _comment, own in self._own_comments())

    def list_signals(self, pr: PullRequestRef) -> tuple[CommentSignals, ...]:
        """Return what ``list_bot_comments`` does, each with the content of people's reactions."""
        self._record("list_signals", pr)
        return tuple(
            CommentSignals(
                own,
                frozenset(
                    reaction.content
                    for reaction in comment.reactions
                    if not is_any_bot(reaction.user_type)
                ),
            )
            for comment, own in self._own_comments()
        )

    def read_summary(self, pr: PullRequestRef) -> Summary | None:
        """Return the summary, or None if there is none or its last line isn't a summary marker."""
        self._record("read_summary", pr)
        if self.summary is None or not is_summary(self.summary):
            return None
        return Summary(read_reviewed_sha(self.summary))

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

    def write_summary(self, pr: PullRequestRef, body: str, *, reviewed_sha: str | None) -> None:
        """Write the bot's summary with its marker recording ``reviewed_sha``, replacing any."""
        self._record("write_summary", pr, body)
        self.summary = with_summary_marker(body, reviewed_sha=reviewed_sha)

    def list_merged_pull_requests(
        self, repo: str, since: date, until: date
    ) -> tuple[MergedPullRequest, ...]:
        """Return the pulls merged in the window, in the order held.

        Raises:
            GitHubError: If more than ``MAX_MERGED_PULLS`` were merged in it.
        """
        self._record("list_merged_pull_requests", repo, since, until)
        merged = tuple(
            MergedPullRequest(
                pull.number, pull.title, pull.created_at, pull.merged_at, pull.commit_dates
            )
            for pull in self.pulls
            if pull.merged_at is not None and in_window(pull.merged_at, since, until)
        )
        if len(merged) > MAX_MERGED_PULLS:
            raise too_many_merged_pulls(len(merged))
        return merged

    def count_issues(self, repo: str, label: str, since: date, until: date) -> int:
        """Return how many issues labelled ``label`` were created in the window."""
        self._record("count_issues", repo, label, since, until)
        return sum(
            1
            for issue in self.issues
            if label in issue.labels and in_window(issue.created_at, since, until)
        )

    def read_issue(self, repo: str, number: int) -> IssueText | None:
        """Return issue ``number``: an issue held, else a pull request held, else None."""
        self._record("read_issue", repo, number)
        issue = next((issue for issue in self.issues if issue.number == number), None)
        if issue is not None:
            return IssueText(issue.title, issue.body)
        pull = next((pull for pull in self.pulls if pull.number == number), None)
        if pull is not None:
            return IssueText(pull.title, "", is_pull_request=True)
        return None

    def _record(self, name: str, *args: object) -> None:
        self.calls.append(FakeCall(name, args))
        if name == self._failing:
            raise GitHubError(f"GitHub {name} failed: FakeGitHubClient was told to fail it.")

    def _own_comments(self) -> list[tuple[FakeComment, BotComment]]:
        """The comments by the bot whose body ``read_marker`` reads, each with its read-back."""
        return [
            (comment, _bot_comment(comment, marker))
            for comment in self.comments
            # The author first, so nobody else's text is parsed.
            if is_bot_user(comment.author, comment.author_type)
            and (marker := read_marker(comment.body)) is not None
        ]

    def _find(self, comment_id: int) -> FakeComment | None:
        return next(
            (comment for comment in self.comments if comment.comment_id == comment_id), None
        )

    def _held(self, comment_id: int) -> FakeComment:
        comment = self._find(comment_id)
        if comment is None:
            raise ValueError(f"FakeGitHubClient holds no comment {comment_id}.")
        return comment


def _bot_comment(comment: FakeComment, marker: Marker) -> BotComment:
    return BotComment(
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
