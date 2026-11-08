from datetime import UTC, datetime

import pytest

from review_bot.errors import GitHubError
from review_bot.findings import Category, Finding, ReviewedFinding, Severity, Source
from review_bot.pr.comments import comment_body
from review_bot.pr.event import PullRequestRef
from review_bot.pr.fake_github import FakeCall, FakeGitHubClient, FakeReview
from review_bot.pr.github import BotComment, NewComment, Resolver
from review_bot.pr.plan import Action, PlannedComment

PR = PullRequestRef(
    repo="owner/repo",
    number=7,
    base_sha="0" * 40,
    head_sha="1" * 40,
    is_fork=False,
    is_draft=False,
    event_at=datetime(2026, 11, 8, tzinfo=UTC),
    action="opened",
)
FINDING = ReviewedFinding(
    Finding.model_construct(
        file="a.py",
        line=3,
        severity=Severity.LOW,
        category=Category.LOGIC,
        rationale="Why.",
        confidence=0.5,
    ),
    Source.AI,
    None,
    "0123456789abcdef",
)
BODY = comment_body(PlannedComment(Action.CREATE, FINDING, "fedcba9876543210"))


def test_list_bot_comments_reads_back_the_bots_comments_with_their_thread_and_resolver() -> None:
    github = FakeGitHubClient()
    comment_id = github.add_comment(path="a.py", line=3, body=BODY)
    github.resolve(comment_id, by="person")

    assert github.list_bot_comments(PR) == (
        BotComment(
            comment_id=1,
            thread_id="thread-1",
            fingerprint="0123456789abcdef",
            rule_part="ai:logic",
            anchor="fedcba9876543210",
            path="a.py",
            line=3,
            severity=Severity.LOW,
            resolved_by=Resolver.PERSON,
        ),
    )
    assert github.calls == [FakeCall("list_bot_comments", (PR,))]


def test_list_bot_comments_gives_an_outdated_comment_no_line_and_a_moved_one_its_new_line() -> None:
    github = FakeGitHubClient()
    outdated = github.add_comment(path="a.py", line=3, body=BODY)
    moved = github.add_comment(path="a.py", line=3, body=BODY)
    github.mark_outdated(outdated)
    github.set_line(moved, 5)

    assert [comment.line for comment in github.list_bot_comments(PR)] == [None, 5]


@pytest.mark.parametrize(
    ("author", "author_type", "body"),
    [
        ("someone", "User", BODY),
        ("github-actions[bot]", "User", BODY),
        ("github-actions[bot]", "Bot", "No marker."),
        ("github-actions[bot]", "Bot", BODY + " and more"),
    ],
    ids=["another-user", "not-a-bot", "no-marker", "malformed-marker"],
)
def test_list_bot_comments_leaves_out_comments_that_arent_the_bots_own(
    author: str, author_type: str, body: str
) -> None:
    github = FakeGitHubClient()
    github.add_comment(path="a.py", line=3, body=body, author=author, author_type=author_type)

    assert github.list_bot_comments(PR) == ()


def test_post_review_adds_the_bots_comments_as_one_review_on_the_head() -> None:
    github = FakeGitHubClient()
    github.add_comment(path="a.py", line=1, body="Earlier.", author="someone")
    new = (NewComment("a.py", 3, BODY), NewComment("b.py", 1, BODY))

    github.post_review(PR, new)

    assert github.reviews == [FakeReview("1" * 40, new)]
    assert [(comment.comment_id, comment.path, comment.line) for comment in github.comments] == [
        (1, "a.py", 1),
        (2, "a.py", 3),
        (3, "b.py", 1),
    ]
    assert [comment.comment_id for comment in github.list_bot_comments(PR)] == [2, 3]


def test_update_resolve_and_summary_change_the_held_state() -> None:
    github = FakeGitHubClient()
    comment_id = github.add_comment(path="a.py", line=3, body="Old.")

    github.update_comment(comment_id, BODY)
    github.resolve_thread("thread-1")
    github.write_summary(PR, "first")
    github.write_summary(PR, "second")

    (comment,) = github.comments
    assert (comment.body, comment.resolved_by, github.summary) == (BODY, Resolver.BOT, "second")
    assert [call.name for call in github.calls] == [
        "update_comment",
        "resolve_thread",
        "write_summary",
        "write_summary",
    ]


def test_fail_on_makes_that_operation_raise_after_recording_it() -> None:
    github = FakeGitHubClient()
    github.fail_on("write_summary")

    with pytest.raises(GitHubError, match="write_summary"):
        github.write_summary(PR, "body")

    assert github.summary is None
    assert [call.name for call in github.calls] == ["write_summary"]


@pytest.mark.parametrize(
    ("call", "operation"),
    [
        (lambda github: github.update_comment(9, "x"), "update_comment"),
        (lambda github: github.resolve_thread("thread-9"), "resolve_thread"),
    ],
)
def test_an_unknown_comment_or_thread_is_a_github_error(call, operation: str) -> None:
    with pytest.raises(GitHubError, match=operation):
        call(FakeGitHubClient())


def test_a_test_helper_given_an_unknown_comment_raises_value_error() -> None:
    with pytest.raises(ValueError, match="no comment 9"):
        FakeGitHubClient().mark_outdated(9)
