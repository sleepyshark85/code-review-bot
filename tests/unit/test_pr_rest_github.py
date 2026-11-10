import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from review_bot.context.issues import IssueText
from review_bot.errors import ConfigError, GitHubError
from review_bot.findings import Category, Finding, ReviewedFinding, Severity, Source
from review_bot.pr.comments import comment_body
from review_bot.pr.event import PullRequestRef
from review_bot.pr.github import BotComment, MergedPullRequest, NewComment, Resolver, Summary
from review_bot.pr.plan import Action, PlannedComment
from review_bot.pr.rest_github import GitHubAccess, RestGitHubClient, github_access

TOKEN = "ghs_canary"  # noqa: S105 - a canary, not a real token
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
        severity=Severity.HIGH,
        category=Category.LOGIC,
        rationale="Why.",
        confidence=0.5,
    ),
    Source.AI,
    None,
    "0123456789abcdef",
)
BOT_BODY = comment_body(PlannedComment(Action.CREATE, FINDING, "fedcba9876543210"))
BOT = {"login": "github-actions[bot]", "type": "Bot"}
PERSON = {"login": "alice", "type": "User"}
SUMMARY_MARKER = "<!-- review-bot:v1 summary reviewed=none -->"
SHA = "0123456789abcdef" * 2 + "01234567"


def marker_for(reviewed: str) -> str:
    return f"<!-- review-bot:v1 summary reviewed={reviewed} -->"


@dataclass(frozen=True)
class Received:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: Any


@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: bytes = b"{}"
    headers: tuple[tuple[str, str], ...] = ()
    hang_up: bool = False


def reply(document: object, status: int = 200) -> Reply:
    return Reply(status, json.dumps(document).encode())


class Stub:
    """A local GitHub API: records each request, and answers it with ``respond``."""

    def __init__(self) -> None:
        self.url = ""
        self.received: list[Received] = []
        self.respond: Callable[[Received], Reply] = lambda _request: reply({"id": 1})


@pytest.fixture
def stub() -> Iterator[Stub]:
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            request = Received(
                self.command,
                parts.path,
                parse_qs(parts.query),
                {name.lower(): value for name, value in self.headers.items()},
                json.loads(raw) if raw else None,
            )
            state.received.append(request)
            answer = state.respond(request)
            if answer.hang_up:
                self.close_connection = True
                return
            self.send_response(answer.status)
            for name, value in answer.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(answer.body)))
            self.end_headers()
            self.wfile.write(answer.body)

        do_GET = do_POST = do_PATCH = _handle  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()


def client(stub: Stub, *, timeout_s: float = 5.0) -> RestGitHubClient:
    return RestGitHubClient("owner/repo", GitHubAccess(stub.url, TOKEN), timeout_s=timeout_s)


def review_comment(comment_id: int, user: object, body: str = BOT_BODY, line: int = 3) -> dict:
    return {"id": comment_id, "user": user, "body": body, "path": "a.py", "line": line, "x": 1}


def thread(thread_id: str, comment_id: int, **fields: Any) -> dict:
    return {
        "id": thread_id,
        "isResolved": False,
        "isOutdated": False,
        "resolvedBy": None,
        "comments": {"nodes": [{"fullDatabaseId": str(comment_id)}]},
        **fields,
    }


def threads_page(threads: list[dict], cursor: str | None = None) -> dict:
    page = {"hasNextPage": cursor is not None, "endCursor": cursor}
    connection = {"pageInfo": page, "nodes": threads}
    return {"data": {"repository": {"pullRequest": {"reviewThreads": connection}}}}


# --- github_access ----------------------------------------------------------------------------


def test_access_reads_the_token_and_defaults_to_github_com() -> None:
    access = github_access(TOKEN, None)

    assert access == GitHubAccess("https://api.github.com", TOKEN)
    assert TOKEN not in repr(access)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ghe.example.com/api/v3/", "https://ghe.example.com/api/v3"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("http://localhost:9000/", "http://localhost:9000"),
        ("", "https://api.github.com"),
    ],
)
def test_access_takes_an_https_url_or_a_local_http_one(url: str, expected: str) -> None:
    access = github_access(TOKEN, url)

    assert access.api_url == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com",
        "http://127.0.0.2",
        "ftp://127.0.0.1",
        "file:///etc/passwd",
        "https://user:pass@api.example.com",
        "https://api.example.com/?q=1",
        "https://",
        "https://[::1",
    ],
)
def test_access_refuses_any_other_url_naming_the_variable(url: str) -> None:
    with pytest.raises(ConfigError) as caught:
        github_access(TOKEN, url)

    # The URL isn't echoed: it could hold credentials.
    assert str(caught.value) == (
        "GITHUB_API_URL must be an https:// URL, or an http:// one on 127.0.0.1 or localhost, so "
        "nothing ran. Unset it to use https://api.github.com."
    )


@pytest.mark.parametrize("token", [None, ""])
def test_access_without_a_token_names_the_variable(token: str | None) -> None:
    with pytest.raises(ConfigError, match=r"^--mode comment needs GITHUB_TOKEN, which is unset"):
        github_access(token, None)


def test_access_refuses_a_token_that_would_break_its_header() -> None:
    with pytest.raises(ConfigError, match=r"^GITHUB_TOKEN holds characters a token can't have"):
        github_access("ghs_x\r\nX-Evil: 1", None)


# --- list_bot_comments -------------------------------------------------------------------------


def test_list_bot_comments_reads_every_page_and_keeps_only_the_bots_marked_comments(
    stub: Stub,
) -> None:
    others = [review_comment(1000 + n, PERSON) for n in range(199)]
    first_page = [review_comment(30, BOT, line=9), *others[:99]]
    second_page = [review_comment(10, BOT), *others[99:]]
    third_page = [
        review_comment(20, BOT),
        review_comment(40, {"login": "github-actions[bot]", "type": "User"}),
        review_comment(50, BOT, body="no marker"),
        review_comment(60, None),
    ]
    pages = {"1": first_page, "2": second_page, "3": third_page}
    threads = {
        None: threads_page(
            [thread("T10", 10, isResolved=True, resolvedBy={"login": "alice"})], cursor="c1"
        ),
        "c1": threads_page(
            [
                thread("T20", 20, isResolved=True, resolvedBy={"login": "github-actions"}),
                thread("T30", 30, isOutdated=True),
                thread("T50", 50),
            ]
        ),
    }

    def respond(request: Received) -> Reply:
        if request.path == "/graphql":
            return reply(threads[request.body["variables"].get("cursor")])
        assert request.path == "/repos/owner/repo/pulls/7/comments"
        assert request.query["per_page"] == ["100"]
        return reply(pages[request.query["page"][0]])

    stub.respond = respond

    comments = client(stub).list_bot_comments(PR)

    expected = BotComment(
        comment_id=10,
        thread_id="T10",
        fingerprint="0123456789abcdef",
        rule_part="ai:logic",
        anchor="fedcba9876543210",
        path="a.py",
        line=3,
        severity=Severity.HIGH,
        resolved_by=Resolver.PERSON,
    )
    assert [comment.comment_id for comment in comments] == [10, 20, 30]
    assert comments[0] == expected
    assert (comments[1].thread_id, comments[1].resolved_by) == ("T20", Resolver.BOT)
    assert (comments[2].line, comments[2].resolved_by) == (None, None)
    assert [request.query.get("page") for request in stub.received[:3]] == [["1"], ["2"], ["3"]]
    assert len(stub.received) == 5


def test_list_bot_comments_counts_a_resolved_thread_with_no_resolver_as_the_bots(
    stub: Stub,
) -> None:
    stub.respond = lambda request: (
        reply(threads_page([thread("T1", 1, isResolved=True)]))
        if request.path == "/graphql"
        else reply([review_comment(1, BOT)])
    )

    (comment,) = client(stub).list_bot_comments(PR)

    assert comment.resolved_by is Resolver.BOT


def test_list_bot_comments_fails_for_a_bot_comment_in_no_thread(stub: Stub) -> None:
    stub.respond = lambda request: (
        reply(threads_page([])) if request.path == "/graphql" else reply([review_comment(5, BOT)])
    )

    with pytest.raises(
        GitHubError, match=r"list_bot_comments failed: no review thread holds comment 5"
    ):
        client(stub).list_bot_comments(PR)


def test_listing_more_than_ten_full_pages_fails_naming_the_limit(stub: Stub) -> None:
    stub.respond = lambda _request: reply([review_comment(1, PERSON)] * 100)

    with pytest.raises(GitHubError, match=r"limit of 10 pages of 100 items"):
        client(stub).list_bot_comments(PR)

    assert len(stub.received) == 10


def test_listing_more_than_ten_pages_of_threads_fails_naming_the_limit(stub: Stub) -> None:
    stub.respond = lambda request: (
        reply(threads_page([], cursor="next")) if request.path == "/graphql" else reply([])
    )

    with pytest.raises(GitHubError, match=r"list_bot_comments failed: .*limit of 10 pages"):
        client(stub).list_bot_comments(PR)

    assert len(stub.received) == 11


# --- list_signals ------------------------------------------------------------------------------


def reaction(content: str, user: object) -> dict:
    return {"id": 1, "node_id": "R", "content": content, "user": user, "created_at": "x"}


def test_list_signals_reads_each_bot_comments_reactions_by_people_and_its_resolver(
    stub: Stub,
) -> None:
    reactions = {
        10: [reaction("+1", PERSON), reaction("heart", None), reaction("-1", BOT)],
        20: [reaction("+1", {"login": "dependabot[bot]", "type": "Bot"})],
    }

    def respond(request: Received) -> Reply:
        if request.path == "/graphql":
            return reply(
                threads_page(
                    [
                        thread("T10", 10),
                        thread("T20", 20, isResolved=True, resolvedBy={"login": "alice"}),
                    ]
                )
            )
        if request.path == "/repos/owner/repo/pulls/7/comments":
            return reply(
                [review_comment(20, BOT), review_comment(10, BOT), review_comment(30, PERSON)]
            )
        comment_id = int(request.path.split("/")[-2])
        assert request.path == f"/repos/owner/repo/pulls/comments/{comment_id}/reactions"
        assert request.query["per_page"] == ["100"]
        return reply(reactions[comment_id])

    stub.respond = respond

    signals = client(stub).list_signals(PR)

    assert [(s.comment.comment_id, s.reactions) for s in signals] == [
        (10, frozenset({"+1", "heart"})),
        (20, frozenset()),
    ]
    assert signals[1].comment.resolved_by is Resolver.PERSON
    assert all(request.method == "GET" for request in stub.received if request.path != "/graphql")
    assert len(stub.received) == 4


def test_list_signals_reads_every_page_of_a_comments_reactions(stub: Stub) -> None:
    pages = {"1": [reaction("eyes", PERSON)] * 100, "2": [reaction("-1", PERSON)]}
    stub.respond = lambda request: (
        reply(threads_page([thread("T1", 1)]))
        if request.path == "/graphql"
        else reply([review_comment(1, BOT)])
        if request.path.endswith("/7/comments")
        else reply(pages[request.query["page"][0]])
    )

    (signal,) = client(stub).list_signals(PR)

    assert signal.reactions == frozenset({"eyes", "-1"})


def test_list_signals_over_ten_full_pages_of_reactions_fails_naming_the_limit(stub: Stub) -> None:
    stub.respond = lambda request: (
        reply(threads_page([thread("T1", 1)]))
        if request.path == "/graphql"
        else reply([review_comment(1, BOT)])
        if request.path.endswith("/7/comments")
        else reply([reaction("+1", PERSON)] * 100)
    )

    with pytest.raises(GitHubError, match=r"^GitHub list_signals failed: .*limit of 10 pages"):
        client(stub).list_signals(PR)

    assert len(stub.received) == 12


def test_list_signals_with_a_failed_reactions_call_fails_naming_list_signals(stub: Stub) -> None:
    stub.respond = lambda request: (
        reply(threads_page([thread("T1", 1)]))
        if request.path == "/graphql"
        else reply([review_comment(1, BOT)])
        if request.path.endswith("/7/comments")
        else Reply(403)
    )

    with pytest.raises(GitHubError, match=r"^GitHub list_signals failed: HTTP 403") as caught:
        client(stub).list_signals(PR)

    assert TOKEN not in str(caught.value)
    assert len(stub.received) == 3


def test_list_signals_with_a_reaction_lacking_its_content_is_a_shape_error(stub: Stub) -> None:
    stub.respond = lambda request: (
        reply(threads_page([thread("T1", 1)]))
        if request.path == "/graphql"
        else reply([review_comment(1, BOT)])
        if request.path.endswith("/7/comments")
        else reply([{"user": PERSON}])
    )

    with pytest.raises(GitHubError, match=r"list_signals failed: .*expected shape at 0\.content"):
        client(stub).list_signals(PR)


# --- writes ----------------------------------------------------------------------------------


def test_post_review_sends_one_comment_review_on_the_head_commit_on_the_new_side(
    stub: Stub,
) -> None:
    client(stub).post_review(PR, [NewComment("a.py", 3, "one"), NewComment("b.py", 5, "two")])

    (request,) = stub.received
    assert (request.method, request.path) == ("POST", "/repos/owner/repo/pulls/7/reviews")
    assert request.body == {
        "commit_id": "1" * 40,
        "event": "COMMENT",
        "comments": [
            {"path": "a.py", "line": 3, "side": "RIGHT", "body": "one"},
            {"path": "b.py", "line": 5, "side": "RIGHT", "body": "two"},
        ],
    }
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["x-github-api-version"] == "2022-11-28"


def test_update_comment_edits_the_inline_comment(stub: Stub) -> None:
    client(stub).update_comment(42, "new body")

    (request,) = stub.received
    assert (request.method, request.path) == ("PATCH", "/repos/owner/repo/pulls/comments/42")
    assert request.body == {"body": "new body"}


def test_resolve_thread_sends_one_resolve_mutation(stub: Stub) -> None:
    stub.respond = lambda _request: reply(
        {"data": {"resolveReviewThread": {"thread": {"id": "T"}}}}
    )

    client(stub).resolve_thread("T")

    (request,) = stub.received
    assert (request.method, request.path) == ("POST", "/graphql")
    assert "resolveReviewThread" in request.body["query"]
    assert request.body["variables"] == {"threadId": "T"}


def test_write_summary_creates_the_summary_with_its_marker_when_the_bot_has_none(
    stub: Stub,
) -> None:
    person_with_marker = {"id": 3, "user": PERSON, "body": f"copied\n\n{SUMMARY_MARKER}"}
    bot_without_marker = {"id": 4, "user": BOT, "body": "another workflow"}
    stub.respond = lambda request: (
        reply([person_with_marker, bot_without_marker])
        if request.method == "GET"
        else reply({"id": 9}, status=201)
    )

    client(stub).write_summary(PR, "Summary.", reviewed_sha=None)

    listing, created = stub.received
    assert (listing.method, listing.path) == ("GET", "/repos/owner/repo/issues/7/comments")
    assert (created.method, created.path) == ("POST", "/repos/owner/repo/issues/7/comments")
    assert created.body == {"body": f"Summary.\n\n{SUMMARY_MARKER}"}


def test_write_summary_edits_the_bots_oldest_summary_in_place(stub: Stub) -> None:
    summaries = [
        {"id": 8, "user": BOT, "body": f"newer\n\n{SUMMARY_MARKER}"},
        {"id": 6, "user": BOT, "body": f"older\n\n{SUMMARY_MARKER}"},
    ]
    stub.respond = lambda request: reply(summaries if request.method == "GET" else {"id": 6})

    client(stub).write_summary(PR, "Summary.", reviewed_sha=None)

    edited = stub.received[-1]
    assert len(stub.received) == 2
    assert (edited.method, edited.path) == ("PATCH", "/repos/owner/repo/issues/comments/6")
    assert edited.body == {"body": f"Summary.\n\n{SUMMARY_MARKER}"}


def test_write_summary_records_the_reviewed_commit_in_its_marker(stub: Stub) -> None:
    stub.respond = lambda request: (
        reply([]) if request.method == "GET" else reply({"id": 9}, status=201)
    )

    client(stub).write_summary(PR, "Summary.", reviewed_sha=SHA)

    created = stub.received[-1]
    assert created.body == {"body": f"Summary.\n\n{marker_for(SHA)}"}


def test_read_summary_reads_the_reviewed_commit_from_the_bots_oldest_summary(
    stub: Stub,
) -> None:
    comments = [
        {"id": 3, "user": PERSON, "body": f"copied\n\n{marker_for('d' * 40)}"},
        {"id": 8, "user": BOT, "body": f"newer\n\n{marker_for('e' * 40)}"},
        {"id": 6, "user": BOT, "body": f"older\n\n{marker_for(SHA)}"},
    ]
    stub.respond = lambda _request: reply(comments)

    summary = client(stub).read_summary(PR)

    (listing,) = stub.received
    assert (listing.method, listing.path) == ("GET", "/repos/owner/repo/issues/7/comments")
    assert summary == Summary(SHA)


def test_read_summary_ignores_a_persons_comment_with_a_valid_marker(stub: Stub) -> None:
    copied = {"id": 3, "user": PERSON, "body": f"copied\n\n{marker_for(SHA)}"}
    other_workflow = {"id": 4, "user": BOT, "body": "Coverage is 91%."}
    stub.respond = lambda _request: reply([copied, other_workflow])

    assert client(stub).read_summary(PR) is None


@pytest.mark.parametrize("reviewed", ["none", "a" * 39], ids=["none", "39-hex"])
def test_read_summary_of_a_marker_without_a_40_hex_sha_records_no_commit(
    stub: Stub, reviewed: str
) -> None:
    stub.respond = lambda _request: reply(
        [{"id": 6, "user": BOT, "body": f"Summary.\n\n{marker_for(reviewed)}"}]
    )

    assert client(stub).read_summary(PR) == Summary(None)


def test_a_pull_request_of_another_repository_is_refused_before_any_request(
    stub: Stub,
) -> None:
    with pytest.raises(ValueError, match="for owner/repo, not owner/other"):
        client(stub).post_review(replace(PR, repo="owner/other"), [])

    assert stub.received == []


# --- failures ---------------------------------------------------------------------------------

FIVE_MB = 5 * 1024 * 1024


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (Reply(401), "HTTP 401, the token was refused"),
        (Reply(403), "HTTP 403, the token lacks a permission"),
        (Reply(404), "HTTP 404, the pull request, comment or repository wasn't found"),
        (Reply(422), "HTTP 422, GitHub rejected the request as invalid"),
        (Reply(500), "HTTP 500, GitHub returned an error"),
        (Reply(403, headers=(("X-RateLimit-Remaining", "0"),)), "HTTP 403, the rate limit"),
        (Reply(429), "HTTP 429, the rate limit is used up"),
        (Reply(200, b"<html>"), "the response isn't JSON"),
        (Reply(200, b" " * FIVE_MB + b"{}"), "the response is over 5 MB"),
        (Reply(200, b'{"name": "x"}'), "the response doesn't have the expected shape at id"),
    ],
)
def test_a_failed_call_raises_one_line_naming_the_operation_and_is_not_retried(
    stub: Stub, answer: Reply, expected: str
) -> None:
    stub.respond = lambda _request: answer

    with pytest.raises(GitHubError) as caught:
        client(stub).update_comment(1, "body")

    message = str(caught.value)
    assert message.startswith("GitHub update_comment failed: ")
    assert expected in message
    assert "\n" not in message
    assert TOKEN not in message
    assert len(stub.received) == 1


def test_a_call_with_no_response_in_time_fails_naming_the_timeout(stub: Stub) -> None:
    def slow(_request: Received) -> Reply:
        time.sleep(1)
        return reply({"id": 1})

    stub.respond = slow

    with pytest.raises(GitHubError, match=r"^GitHub post_review failed: no response within 0.2 s"):
        client(stub, timeout_s=0.2).post_review(PR, [NewComment("a.py", 1, "b")])

    assert len(stub.received) == 1


def test_a_call_whose_connection_drops_fails_naming_the_operation(stub: Stub) -> None:
    stub.respond = lambda _request: Reply(hang_up=True)

    with pytest.raises(GitHubError, match=r"^GitHub update_comment failed: the connection failed"):
        client(stub).update_comment(1, "body")

    assert len(stub.received) == 1


def test_a_graphql_error_fails_naming_the_operation_and_its_message(stub: Stub) -> None:
    errors = [{"message": "Resource not\naccessible by integration", "type": "FORBIDDEN"}]
    stub.respond = lambda _request: reply({"data": None, "errors": errors})

    with pytest.raises(GitHubError) as caught:
        client(stub).resolve_thread("T")

    assert str(caught.value) == (
        "GitHub resolve_thread failed: GraphQL error: Resource not accessible by integration. "
        "Nothing was retried; run the job again."
    )


def test_a_graphql_answer_without_its_data_fails_naming_the_missing_field(stub: Stub) -> None:
    stub.respond = lambda _request: reply({"data": {"resolveReviewThread": {}}})

    with pytest.raises(GitHubError, match=r"expected shape at resolveReviewThread\.thread"):
        client(stub).resolve_thread("T")


def test_a_graphql_error_quoting_the_token_or_a_secret_shows_neither(stub: Stub) -> None:
    aws_key = "AK" + "IA" + "ABCDEFGH" + "23456789"  # assembled, so no key is written literally
    quoted = f"Bad request: Authorization: Bearer {TOKEN}, and {aws_key}"
    stub.respond = lambda _request: reply({"data": None, "errors": [{"message": quoted}]})

    with pytest.raises(GitHubError) as caught:
        client(stub).resolve_thread("PRRT_missing")

    message = str(caught.value)
    assert "Authorization: Bearer [token]" in message
    assert TOKEN not in message
    assert aws_key not in message


def test_a_redirected_listing_is_not_followed_so_the_token_stays_on_the_api_host(
    stub: Stub,
) -> None:
    # urllib's default opener follows a GET redirect, Authorization header included.
    elsewhere = f"{stub.url}/elsewhere"
    stub.respond = lambda request: (
        reply([])
        if request.path == "/elsewhere"
        else Reply(302, headers=(("Location", elsewhere),))
    )

    with pytest.raises(GitHubError, match=r"^GitHub list_bot_comments failed: HTTP 302, "):
        client(stub).list_bot_comments(PR)

    assert [request.path for request in stub.received] == ["/repos/owner/repo/pulls/7/comments"]


def test_list_bot_comments_links_threads_by_comment_ids_above_32_bits(stub: Stub) -> None:
    big_id = 2**31 + 5
    stub.respond = lambda request: (
        reply(threads_page([thread("T", big_id)]))
        if request.path == "/graphql"
        else reply([review_comment(big_id, BOT)])
    )

    (comment,) = client(stub).list_bot_comments(PR)

    assert (comment.comment_id, comment.thread_id) == (big_id, "T")
    assert "fullDatabaseId" in stub.received[1].body["query"]


def test_a_threads_page_saying_there_is_more_with_no_cursor_is_a_shape_error(stub: Stub) -> None:
    page = threads_page([])
    page["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"]["hasNextPage"] = True
    stub.respond = lambda request: reply(page) if request.path == "/graphql" else reply([])

    with pytest.raises(GitHubError, match=r"expected shape at repository\.pullRequest\."):
        client(stub).list_bot_comments(PR)

    assert len(stub.received) == 2


# P2-S11: merged pull requests and issues, through the search API.

SINCE, UNTIL = date(2026, 10, 1), date(2026, 10, 31)
WINDOW = "2026-10-01T00:00:00Z..2026-10-31T23:59:59Z"


def search_item(number: int, merged_at: str | None = "2026-10-11T09:00:00Z") -> dict:
    return {
        "number": number,
        "title": f"Fix {number}",
        "created_at": "2026-10-10T09:00:00Z",
        "pull_request": {"merged_at": merged_at, "url": "x"},
        "score": 1.0,
    }


def search_page(items: list[dict], total: int | None = None, *, incomplete: bool = False) -> dict:
    return {
        "total_count": len(items) if total is None else total,
        "incomplete_results": incomplete,
        "items": items,
    }


def pull_commit(committed: str) -> dict:
    return {"sha": "c" * 40, "commit": {"committer": {"date": committed, "name": "n"}}}


def history(search: Callable[[Received], Reply]) -> Callable[[Received], Reply]:
    """Answers search requests with ``search``, and each PR's commits with one commit."""

    def respond(request: Received) -> Reply:
        if request.path == "/search/issues":
            return search(request)
        return reply([pull_commit("2026-10-10T10:00:00+02:00")])

    return respond


def test_list_merged_pull_requests_searches_the_window_and_reads_each_prs_commits(
    stub: Stub,
) -> None:
    stub.respond = history(lambda _r: reply(search_page([search_item(4), search_item(5, None)])))

    merged = client(stub).list_merged_pull_requests("owner/repo", SINCE, UNTIL)

    assert merged == (
        MergedPullRequest(
            number=4,
            title="Fix 4",
            created_at=datetime(2026, 10, 10, 9, tzinfo=UTC),
            merged_at=datetime(2026, 10, 11, 9, tzinfo=UTC),
            commit_dates=(datetime(2026, 10, 10, 8, tzinfo=UTC),),
        ),
    )
    search, commits = stub.received
    assert search.query == {
        "q": [f"repo:owner/repo is:pr is:merged merged:{WINDOW}"],
        "sort": ["created"],
        "order": ["asc"],
        "per_page": ["100"],
        "page": ["1"],
    }
    assert commits.path == "/repos/owner/repo/pulls/4/commits"
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in stub.received)


def test_list_merged_pull_requests_reads_search_pages_until_one_isnt_full(stub: Stub) -> None:
    pages = {"1": [search_item(n) for n in range(100)], "2": [search_item(100)]}
    stub.respond = history(lambda r: reply(search_page(pages[r.query["page"][0]], total=101)))

    merged = client(stub).list_merged_pull_requests("owner/repo", SINCE, UNTIL)

    assert len(merged) == 101
    assert [r.query["page"] for r in stub.received if r.path == "/search/issues"] == [["1"], ["2"]]


def test_list_merged_pull_requests_at_the_limit_of_1000_returns_them_all(stub: Stub) -> None:
    def page(request: Received) -> Reply:
        first = (int(request.query["page"][0]) - 1) * 100
        return reply(search_page([search_item(n) for n in range(first, first + 100)], total=1000))

    stub.respond = history(page)

    merged = client(stub).list_merged_pull_requests("owner/repo", SINCE, UNTIL)

    assert [pull.number for pull in merged] == list(range(1000))
    assert len([r for r in stub.received if r.path == "/search/issues"]) == 10


def test_list_merged_pull_requests_keeps_a_pr_seen_on_two_pages_once(stub: Stub) -> None:
    # Results shifted while paging: 99 ends page 1 and starts page 2.
    pages = {"1": list(range(100)), "2": [99, 100]}
    stub.respond = history(
        lambda r: reply(search_page([search_item(n) for n in pages[r.query["page"][0]]], total=101))
    )

    merged = client(stub).list_merged_pull_requests("owner/repo", SINCE, UNTIL)

    assert [pull.number for pull in merged] == list(range(101))


def test_list_merged_pull_requests_stops_once_it_has_the_total(stub: Stub) -> None:
    stub.respond = history(lambda _r: reply(search_page([search_item(n) for n in range(100)])))

    client(stub).list_merged_pull_requests("owner/repo", SINCE, UNTIL)

    assert len([r for r in stub.received if r.path == "/search/issues"]) == 1


def test_list_merged_pull_requests_over_1000_fails_naming_the_limit_before_any_commits(
    stub: Stub,
) -> None:
    stub.respond = history(lambda _r: reply(search_page([search_item(1)] * 100, total=1001)))

    with pytest.raises(
        GitHubError, match=r"1001 merged pull requests, more than the limit of 1000"
    ):
        client(stub).list_merged_pull_requests("owner/repo", SINCE, UNTIL)

    assert [request.path for request in stub.received] == ["/search/issues"]


def test_an_incomplete_search_fails_rather_than_undercount(stub: Stub) -> None:
    stub.respond = history(lambda _r: reply(search_page([search_item(1)], incomplete=True)))

    with pytest.raises(GitHubError, match=r"list_merged_pull_requests failed: the search timed"):
        client(stub).list_merged_pull_requests("owner/repo", SINCE, UNTIL)


def test_a_commit_without_a_committer_date_is_a_shape_error(stub: Stub) -> None:
    stub.respond = lambda request: (
        reply(search_page([search_item(1)]))
        if request.path == "/search/issues"
        else reply([{"sha": "c", "commit": {"committer": {"name": "n"}}}])
    )

    with pytest.raises(GitHubError, match=r"doesn't have the expected shape at 0.commit"):
        client(stub).list_merged_pull_requests("owner/repo", SINCE, UNTIL)


def test_a_failed_search_fails_naming_the_call_and_status(stub: Stub) -> None:
    stub.respond = lambda _request: reply({"message": f"no {TOKEN}"}, status=403)

    with pytest.raises(GitHubError, match=r"^GitHub count_issues failed: HTTP 403") as caught:
        client(stub).count_issues("owner/repo", "bug", SINCE, UNTIL)

    assert TOKEN not in str(caught.value)


def test_count_issues_reads_the_searchs_total_for_issues_with_the_label(stub: Stub) -> None:
    stub.respond = lambda _request: reply(search_page([{"number": 1}], total=2))

    count = client(stub).count_issues("owner/repo", "bug", SINCE, UNTIL)

    assert count == 2
    (search,) = stub.received
    assert search.path == "/search/issues"
    assert search.query["q"] == [f'repo:owner/repo is:issue label:"bug" created:{WINDOW}']
    assert search.query["per_page"] == ["1"]


def test_history_calls_refuse_another_repository(stub: Stub) -> None:
    with pytest.raises(ValueError, match="not other/repo"):
        client(stub).list_merged_pull_requests("other/repo", SINCE, UNTIL)
    with pytest.raises(ValueError, match="not other/repo"):
        client(stub).count_issues("other/repo", "bug", SINCE, UNTIL)

    assert stub.received == []


# --- read_issue: linked issues (P3-S9) ---


def test_read_issue_gets_the_issue_and_keeps_its_title_and_body(stub: Stub) -> None:
    stub.respond = lambda _request: reply({"number": 12, "title": "Export", "body": "Discount."})

    issue = client(stub).read_issue("owner/repo", 12)

    assert issue == IssueText("Export", "Discount.")
    (request,) = stub.received
    assert (request.method, request.path) == ("GET", "/repos/owner/repo/issues/12")


def test_read_issue_reads_a_null_body_as_empty(stub: Stub) -> None:
    stub.respond = lambda _request: reply({"title": "Export", "body": None})

    assert client(stub).read_issue("owner/repo", 12) == IssueText("Export", "")


def test_read_issue_marks_a_pull_request(stub: Stub) -> None:
    stub.respond = lambda _request: reply({"title": "A pull", "body": "", "pull_request": {}})

    issue = client(stub).read_issue("owner/repo", 8)

    assert issue is not None
    assert issue.is_pull_request


def test_read_issue_not_found_is_none(stub: Stub) -> None:
    stub.respond = lambda _request: reply({"message": "Not Found"}, status=404)

    assert client(stub).read_issue("owner/repo", 404) is None


@pytest.mark.parametrize(
    ("answer", "problem"),
    [
        (Reply(403, b"{}"), "HTTP 403"),
        (Reply(200, b"{"), "the response isn't JSON"),
        (Reply(200, b'{"body": "x"}'), "doesn't have the expected shape at title"),
        (Reply(200, b" " * (1024 * 1024) + b"{}"), "the response is over 1 MB"),
    ],
)
def test_read_issue_that_fails_raises_naming_the_call(
    stub: Stub, answer: Reply, problem: str
) -> None:
    stub.respond = lambda _request: answer

    with pytest.raises(GitHubError, match=r"^GitHub read_issue failed: ") as caught:
        client(stub).read_issue("owner/repo", 12)

    assert problem in str(caught.value)


def test_read_issue_refuses_another_repository(stub: Stub) -> None:
    with pytest.raises(ValueError, match="not other/repo"):
        client(stub).read_issue("other/repo", 12)

    assert stub.received == []
