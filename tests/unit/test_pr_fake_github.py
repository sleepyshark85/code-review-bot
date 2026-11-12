from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta

import pytest

from review_bot.context.issues import IssueText
from review_bot.errors import GitHubError
from review_bot.findings import Category, Finding, ReviewedFinding, Severity, Source
from review_bot.pr.comments import comment_body
from review_bot.pr.event import PullRequestRef
from review_bot.pr.fake_github import FakeCall, FakeGitHubClient, FakeReview
from review_bot.pr.github import (
    BOT_LOGIN,
    MAX_MERGED_PULLS,
    BotComment,
    BotIssueComment,
    MergedPullRequest,
    NewComment,
    Resolver,
    Summary,
)
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
            confidence=0.5,
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
    github.write_summary(PR, "first", reviewed_sha=None)
    github.write_summary(PR, "second", reviewed_sha=None)

    (comment,) = github.comments
    assert (comment.body, comment.resolved_by) == (BODY, Resolver.BOT)
    assert github.summary == "second\n\n<!-- review-bot:v1 summary reviewed=none -->"
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
        github.write_summary(PR, "body", reviewed_sha=None)

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


def test_read_summary_is_none_before_any_summary_is_written() -> None:
    github = FakeGitHubClient()

    assert github.read_summary(PR) is None
    assert [call.name for call in github.calls] == ["read_summary"]


def test_read_summary_reads_back_the_reviewed_commit_write_summary_recorded() -> None:
    github = FakeGitHubClient()
    github.write_summary(PR, "Body.", reviewed_sha="a" * 40)

    assert github.read_summary(PR) == Summary("a" * 40)


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("Body with no marker.", None),
        ("Body.\n\n<!-- review-bot:v1 summary reviewed=none -->", Summary(None)),
        (f"Body.\n\n<!-- review-bot:v1 summary reviewed={'b' * 39} -->", Summary(None)),
    ],
    ids=["no-marker", "none", "39-hex"],
)
def test_read_summary_reads_a_summary_a_test_set(summary: str, expected: Summary | None) -> None:
    github = FakeGitHubClient()
    github.summary = summary

    assert github.read_summary(PR) == expected


# --- list_signals ------------------------------------------------------------------------------


def test_list_signals_gives_each_bot_comment_with_the_content_of_peoples_reactions() -> None:
    github = FakeGitHubClient()
    first = github.add_comment(path="a.py", line=3, body=BODY)
    second = github.add_comment(path="a.py", line=4, body=BODY)
    github.add_comment(path="a.py", line=5, body=BODY, author="someone", author_type="User")
    github.react(first, "+1", by="alice")
    github.react(first, "+1", by="bob")
    github.react(first, "heart", by="bob")
    github.react(second, "-1", by="github-actions[bot]", by_type="Bot")
    github.react(second, "+1", by="dependabot[bot]", by_type="Bot")
    github.react(3, "-1", by="alice")
    github.resolve(second, by="person")

    signals = github.list_signals(PR)

    assert [(signal.comment, signal.reactions) for signal in signals] == [
        (github.list_bot_comments(PR)[0], frozenset({"+1", "heart"})),
        (github.list_bot_comments(PR)[1], frozenset()),
    ]
    assert signals[1].comment.resolved_by is Resolver.PERSON
    assert github.calls[0] == FakeCall("list_signals", (PR,))


def test_list_signals_fails_when_told_to() -> None:
    github = FakeGitHubClient()
    github.fail_on("list_signals")

    with pytest.raises(GitHubError, match="GitHub list_signals failed"):
        github.list_signals(PR)


# P2-S11: merged pull requests and issues.

SINCE, UNTIL = date(2026, 10, 1), date(2026, 10, 31)
OPENED = datetime(2026, 10, 10, tzinfo=UTC)


def add_pull(
    client: FakeGitHubClient, number: int, merged_at: datetime | None, *, closed: bool = True
) -> None:
    client.add_pull_request(
        number=number,
        title=f"Change {number}",
        created_at=OPENED - timedelta(days=30),
        closed_at=(merged_at or OPENED) if closed else None,
        merged_at=merged_at,
        commit_dates=(OPENED,),
    )


def test_list_merged_pull_requests_returns_only_those_merged_in_the_window() -> None:
    client = FakeGitHubClient()
    add_pull(client, 1, OPENED)
    add_pull(client, 2, None)
    add_pull(client, 3, None, closed=False)
    add_pull(client, 4, datetime(2026, 9, 30, 23, 59, tzinfo=UTC))
    add_pull(client, 5, datetime(2026, 10, 31, 23, 59, tzinfo=UTC))
    add_pull(client, 6, datetime(2026, 11, 1, tzinfo=UTC))

    merged = client.list_merged_pull_requests("owner/repo", SINCE, UNTIL)

    assert [pull.number for pull in merged] == [1, 5]
    assert merged[0] == MergedPullRequest(
        1, "Change 1", OPENED - timedelta(days=30), OPENED, (OPENED,)
    )
    assert client.calls == [FakeCall("list_merged_pull_requests", ("owner/repo", SINCE, UNTIL))]


def test_list_merged_pull_requests_over_the_limit_fails_naming_it() -> None:
    client = FakeGitHubClient()
    for number in range(MAX_MERGED_PULLS + 1):
        add_pull(client, number, OPENED)

    with pytest.raises(
        GitHubError, match=r"1001 merged pull requests, more than the limit of 1000"
    ):
        client.list_merged_pull_requests("owner/repo", SINCE, UNTIL)


def test_list_merged_pull_requests_at_the_limit_returns_them_all() -> None:
    client = FakeGitHubClient()
    for number in range(MAX_MERGED_PULLS):
        add_pull(client, number, OPENED)

    assert len(client.list_merged_pull_requests("owner/repo", SINCE, UNTIL)) == MAX_MERGED_PULLS


def test_count_issues_counts_those_with_the_label_created_in_the_window() -> None:
    client = FakeGitHubClient()
    client.add_issue(number=1, labels=("bug",), created_at=OPENED)
    client.add_issue(number=2, labels=("docs", "bug"), created_at=OPENED)
    client.add_issue(number=3, labels=("docs",), created_at=OPENED)
    client.add_issue(number=4, labels=("bug",), created_at=datetime(2026, 11, 1, tzinfo=UTC))

    assert client.count_issues("owner/repo", "bug", SINCE, UNTIL) == 2
    assert client.calls == [FakeCall("count_issues", ("owner/repo", "bug", SINCE, UNTIL))]


@pytest.mark.parametrize(
    ("operation", "call"),
    [
        ("list_merged_pull_requests", lambda c: c.list_merged_pull_requests("o/r", SINCE, UNTIL)),
        ("count_issues", lambda c: c.count_issues("o/r", "bug", SINCE, UNTIL)),
    ],
)
def test_a_failing_history_call_raises_github_error(
    operation: str, call: Callable[[FakeGitHubClient], object]
) -> None:
    client = FakeGitHubClient()
    client.fail_on(operation)

    with pytest.raises(GitHubError, match=operation):
        call(client)


def test_read_issue_returns_an_issue_a_pull_request_or_none_and_records_each_call() -> None:
    github = FakeGitHubClient()
    github.add_issue(number=12, title="Export", body="Discount.")
    github.add_pull_request(
        number=8, title="A pull", created_at=OPENED, closed_at=None, merged_at=None
    )

    found = [github.read_issue("owner/repo", number) for number in (12, 8, 404)]

    assert found == [
        IssueText("Export", "Discount."),
        IssueText("A pull", "", is_pull_request=True),
        None,
    ]
    assert [call.args for call in github.calls] == [
        ("owner/repo", 12),
        ("owner/repo", 8),
        ("owner/repo", 404),
    ]


def test_a_failing_read_issue_raises_github_error() -> None:
    github = FakeGitHubClient()
    github.fail_on("read_issue")

    with pytest.raises(GitHubError, match="read_issue"):
        github.read_issue("owner/repo", 12)


def test_read_issue_refuses_another_repository_as_the_rest_client_does() -> None:
    github = FakeGitHubClient("owner/repo")
    github.add_issue(number=12, title="Export", body="Discount.")

    with pytest.raises(ValueError, match=r"for owner/repo, not other/repo"):
        github.read_issue("other/repo", 12)


def editors(github: FakeGitHubClient) -> Mapping[int, str | None]:
    return github.read_inline_comments(PR).editors


def test_a_person_editing_a_comment_becomes_its_last_editor_and_the_bot_after_an_update() -> None:
    github = FakeGitHubClient()
    own = github.add_comment(path="a.py", line=3, body=BODY)
    theirs = github.add_comment(path="a.py", line=3, body="Hi.", author="alice", author_type="User")

    github.edit_comment(own, BODY, by="mallory")

    assert editors(github) == {own: "mallory"}
    github.update_comment(own, BODY)
    assert editors(github) == {own: BOT_LOGIN}
    assert theirs not in editors(github)


def test_read_inline_comments_reads_back_the_comments_and_lists_one_whose_marker_is_gone() -> None:
    github = FakeGitHubClient()
    kept = github.add_comment(path="a.py", line=3, body=BODY)
    edited = github.add_comment(path="a.py", line=3, body=BODY)

    github.edit_comment(edited, "Nothing to see.", by="mallory")

    inline = github.read_inline_comments(PR)
    assert inline.comments == github.list_bot_comments(PR)
    assert [comment.comment_id for comment in inline.comments] == [kept]
    assert inline.editors == {kept: None, edited: "mallory"}


def test_a_deleted_comment_is_gone_and_its_id_is_never_given_out_again() -> None:
    github = FakeGitHubClient()
    first = github.add_comment(path="a.py", line=3, body=BODY)

    github.delete_comment(first)
    second = github.add_comment(path="a.py", line=3, body=BODY)

    assert editors(github) == {second: None}
    assert second != first


def test_the_bots_issue_comments_carry_the_summarys_last_editor() -> None:
    github = FakeGitHubClient()
    assert github.list_bot_issue_comments(PR) == ()

    github.write_summary(PR, "Body.", reviewed_sha=None)
    (created,) = github.list_bot_issue_comments(PR)
    assert (created.body, created.last_editor) == (github.summary, None)
    github.write_summary(PR, "Again.", reviewed_sha=None)
    assert github.list_bot_issue_comments(PR) == (
        BotIssueComment(created.comment_id, github.summary or "", BOT_LOGIN),
    )
    github.delete_summary()
    assert github.list_bot_issue_comments(PR) == ()


def test_a_summary_whose_marker_was_edited_away_stays_when_the_bot_writes_a_new_one() -> None:
    github = FakeGitHubClient()
    github.write_summary(PR, "Body.", reviewed_sha=None)
    github.edit_summary("All clear.", by="mallory")
    assert github.read_summary(PR) is None

    github.write_summary(PR, "New.", reviewed_sha=None)

    old, new = github.list_bot_issue_comments(PR)
    assert (old.body, old.last_editor) == ("All clear.", "mallory")
    assert (new.body, new.last_editor) == (github.summary, None)
    assert old.comment_id < new.comment_id


def test_read_summary_says_whether_the_marker_holds_gate_state() -> None:
    github = FakeGitHubClient()
    github.summary = "Body.\n\n<!-- review-bot:v1 summary reviewed=none tampered -->"

    assert github.read_summary(PR) == Summary(None, holds_gate_state=True)
