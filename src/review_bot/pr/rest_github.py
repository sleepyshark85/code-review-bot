"""The real ``GitHubClient``: GitHub's REST and GraphQL APIs over ``urllib`` (P2-S6).

It has its own module so the pure ``plan`` and ``shadow`` modules, which import the data types in
``github``, never load the HTTP stack.

Every request sends the token only in its ``Authorization`` header, has a 30-second socket
timeout, follows no redirect and is never retried. A response is read up to 5 MB and parsed with
a schema that ignores unknown fields. Any failure raises ``GitHubError``: one line naming the
operation and the status or problem, never the token or the response's text.
"""

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http.client import HTTPException, HTTPMessage
from typing import Final, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from pydantic.alias_generators import to_camel

from review_bot.errors import ConfigError, GitHubError
from review_bot.pr.comments import Marker, is_summary, read_marker, with_summary_marker
from review_bot.pr.event import PullRequestRef
from review_bot.pr.github import BOT_LOGIN, BOT_USER_TYPE, BotComment, NewComment, Resolver
from review_bot.secret_scan import redact
from review_bot.settings import GITHUB_TOKEN_VARIABLE

__all__ = [
    "API_URL_VARIABLE",
    "DEFAULT_API_URL",
    "GitHubAccess",
    "RestGitHubClient",
    "github_access",
]

API_URL_VARIABLE: Final = "GITHUB_API_URL"
DEFAULT_API_URL: Final = "https://api.github.com"
_TIMEOUT_S: Final = 30.0
_MAX_RESPONSE_BYTES: Final = 5 * 1024 * 1024
_PER_PAGE: Final = 100
_MAX_PAGES: Final = 10
_LOCAL_HOSTS: Final = frozenset({"127.0.0.1", "localhost"})
# A token is visible ASCII, so it can't break the header it is sent in.
_TOKEN: Final = re.compile(r"[\x21-\x7e]+")
# GraphQL names the Actions bot without REST's "[bot]" (Phase 2 design, Comment state).
_GRAPHQL_BOT_LOGIN: Final = "github-actions"
# GraphQL error messages are shown at most this long, on one line.
_MAX_SHOWN_CHARS: Final = 200
_RETRY_ADVICE: Final = "Nothing was retried; run the job again."
_STATUS_PROBLEMS: Final = {
    401: "the token was refused. Nothing was retried; check that the job passes GITHUB_TOKEN.",
    403: (
        "the token lacks a permission the call needs. Nothing was retried; check the job's "
        "permissions."
    ),
    404: "the pull request, comment or repository wasn't found. Nothing was retried.",
    422: "GitHub rejected the request as invalid. Nothing was retried.",
}

_NO_TOKEN: Final = (
    f"--mode comment needs {GITHUB_TOKEN_VARIABLE}, which is unset or empty, so nothing was "
    "reviewed. Run it in the comment job, which sets it, or run with --mode shadow."
)
_BAD_TOKEN: Final = (
    f"{GITHUB_TOKEN_VARIABLE} holds characters a token can't have, so nothing was reviewed. Set "
    "it to the workflow's token."
)
_BAD_API_URL: Final = (
    f"{API_URL_VARIABLE} must be an https:// URL, or an http:// one on 127.0.0.1 or localhost, "
    "so nothing was reviewed. Unset it to use https://api.github.com."
)

_THREADS_QUERY: Final = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated resolvedBy { login }
          comments(first: 1) { nodes { fullDatabaseId } }
        }
      }
    }
  }
}
"""
_RESOLVE_MUTATION: Final = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { id } }
}
"""


@dataclass(frozen=True, slots=True)
class GitHubAccess:
    """Where the API is, and the token for it, which no ``repr`` shows."""

    api_url: str
    token: str = field(repr=False)


def github_access(token: str | None, api_url: str | None) -> GitHubAccess:
    """Check the token from ``GITHUB_TOKEN`` and the API's URL from ``GITHUB_API_URL``.

    No URL, or an empty one, means ``https://api.github.com``.

    Raises:
        ConfigError: If the token is missing or malformed, or the URL is neither ``https://`` nor
            ``http://`` on ``127.0.0.1`` or ``localhost``.
    """
    if not token:
        raise ConfigError(_NO_TOKEN)
    if _TOKEN.fullmatch(token) is None:
        raise ConfigError(_BAD_TOKEN)
    return GitHubAccess(_api_url(api_url or DEFAULT_API_URL), token)


def _api_url(url: str) -> str:
    """``url`` without a trailing slash, if it is a URL the token may be sent to."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as exc:
        raise ConfigError(_BAD_API_URL) from exc
    allowed = parts.scheme == "https" or (parts.scheme == "http" and host in _LOCAL_HOSTS)
    if not allowed or not host or parts.username is not None or parts.query or parts.fragment:
        raise ConfigError(_BAD_API_URL)
    return url.rstrip("/")


class _Schema(BaseModel):
    """A response's shape: unknown fields are ignored, and GraphQL's camelCase names are read."""

    model_config = ConfigDict(alias_generator=to_camel, frozen=True)


class _User(_Schema):
    login: str
    user_type: str = Field(alias="type")


class _ReviewComment(_Schema):
    id: int
    body: str
    path: str
    line: int | None
    user: _User | None


class _IssueComment(_Schema):
    id: int
    body: str
    user: _User | None


class _WithId(_Schema):
    id: int


class _GraphQLError(_Schema):
    message: str


class _GraphQLResponse(_Schema):
    data: dict[str, object] | None = None
    errors: list[_GraphQLError] = []


class _Actor(_Schema):
    login: str


class _CommentId(_Schema):
    # A BigInt, which GraphQL sends as a string; ``databaseId`` is 32-bit and deprecated.
    full_database_id: int | None


class _CommentIds(_Schema):
    nodes: list[_CommentId]


class _Thread(_Schema):
    id: str
    is_resolved: bool
    is_outdated: bool
    resolved_by: _Actor | None
    comments: _CommentIds


class _PageInfo(_Schema):
    has_next_page: bool
    end_cursor: str | None

    @model_validator(mode="after")
    def _next_page_has_a_cursor(self) -> Self:
        if self.has_next_page and self.end_cursor is None:
            raise ValueError("hasNextPage is true with no endCursor")
        return self


class _Threads(_Schema):
    page_info: _PageInfo
    nodes: list[_Thread]


class _PullRequest(_Schema):
    review_threads: _Threads


class _Repository(_Schema):
    pull_request: _PullRequest


class _ThreadsData(_Schema):
    repository: _Repository


class _ThreadId(_Schema):
    id: str


class _ResolvePayload(_Schema):
    thread: _ThreadId


class _ResolveData(_Schema):
    resolve_review_thread: _ResolvePayload


_REVIEW_COMMENTS: Final = TypeAdapter(list[_ReviewComment])
_ISSUE_COMMENTS: Final = TypeAdapter(list[_IssueComment])
_WITH_ID: Final = TypeAdapter(_WithId)
_GRAPHQL_RESPONSE: Final = TypeAdapter(_GraphQLResponse)
_THREADS_DATA: Final = TypeAdapter(_ThreadsData)
_RESOLVE_DATA: Final = TypeAdapter(_ResolveData)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, so the token never follows one to another host."""

    def redirect_request(  # noqa: PLR0913, PLR0917 - urllib's signature
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        """Return None, so urllib raises the redirect's status as an ``HTTPError``."""


class RestGitHubClient:
    """A ``GitHubClient`` for one repository, through GitHub's REST and GraphQL APIs.

    Its calls take pull requests of that repository only.
    """

    def __init__(self, repo: str, access: GitHubAccess, *, timeout_s: float = _TIMEOUT_S) -> None:
        """Talk to ``repo`` (``owner/name``) with ``access``, each request within ``timeout_s``."""
        self._repo = repo
        self._repo_url = f"{access.api_url}/repos/{repo}"
        # Right for api.github.com; GitHub Enterprise Server would need GITHUB_GRAPHQL_URL.
        self._graphql_url = f"{access.api_url}/graphql"
        self._token = access.token
        self._timeout_s = timeout_s
        self._opener = urllib.request.build_opener(_NoRedirect)

    def list_bot_comments(self, pr: PullRequestRef) -> tuple[BotComment, ...]:
        """Return the bot's inline comments on ``pr`` with a valid marker, oldest first.

        A comment is the bot's when its REST author is ``github-actions[bot]`` of type ``Bot``.
        Its thread is the one its ID starts, and its line is None when that thread is outdated.
        """
        operation = "list_bot_comments"
        comments = self._list(operation, f"{self._pull_url(pr)}/comments", _REVIEW_COMMENTS)
        threads = self._threads_by_comment(operation, pr)
        return tuple(
            _bot_comment(comment, marker, _thread_of(operation, threads, comment.id))
            for comment in sorted(comments, key=lambda comment: comment.id)
            # The author first, so nobody else's text is parsed.
            if _is_bot(comment.user) and (marker := read_marker(comment.body)) is not None
        )

    def post_review(self, pr: PullRequestRef, comments: Sequence[NewComment]) -> None:
        """Post ``comments`` on ``pr``'s head commit as one ``COMMENT`` review, on the new side."""
        payload = {
            "commit_id": pr.head_sha,
            "event": "COMMENT",
            "comments": [
                {"path": new.path, "line": new.line, "side": "RIGHT", "body": new.body}
                for new in comments
            ],
        }
        self._send("post_review", "POST", f"{self._pull_url(pr)}/reviews", payload, _WITH_ID)

    def update_comment(self, comment_id: int, body: str) -> None:
        """Replace an inline comment's body."""
        url = f"{self._repo_url}/pulls/comments/{comment_id}"
        self._send("update_comment", "PATCH", url, {"body": body}, _WITH_ID)

    def resolve_thread(self, thread_id: str) -> None:
        """Resolve a review thread, by its GraphQL node ID."""
        self._graphql("resolve_thread", _RESOLVE_MUTATION, {"threadId": thread_id}, _RESOLVE_DATA)

    def write_summary(self, pr: PullRequestRef, body: str) -> None:
        """Edit the bot's summary comment on ``pr`` in place, or create it if there is none.

        The posted body ends with the summary marker, which is how the next run finds it: the
        bot's oldest issue comment whose last line is that marker.
        """
        operation = "write_summary"
        issue_url = f"{self._repo_url}/issues/{self._number(pr)}"
        comments = self._list(operation, f"{issue_url}/comments", _ISSUE_COMMENTS)
        summary = min(
            (
                comment.id
                for comment in comments
                if _is_bot(comment.user) and is_summary(comment.body)
            ),
            default=None,
        )
        payload = {"body": with_summary_marker(body)}
        if summary is None:
            self._send(operation, "POST", f"{issue_url}/comments", payload, _WITH_ID)
        else:
            url = f"{self._repo_url}/issues/comments/{summary}"
            self._send(operation, "PATCH", url, payload, _WITH_ID)

    def _number(self, pr: PullRequestRef) -> int:
        """``pr``'s number, once it is known to be of this client's repository."""
        if pr.repo != self._repo:
            raise ValueError(f"This client is for {self._repo}, not {pr.repo}.")
        return pr.number

    def _pull_url(self, pr: PullRequestRef) -> str:
        return f"{self._repo_url}/pulls/{self._number(pr)}"

    def _threads_by_comment(self, operation: str, pr: PullRequestRef) -> dict[int, _Thread]:
        """``pr``'s review threads, by the ID of the comment each starts with."""
        owner, name = self._repo.split("/")
        variables: dict[str, object] = {"owner": owner, "name": name, "number": self._number(pr)}
        threads: dict[int, _Thread] = {}
        for _ in range(_MAX_PAGES):
            page = self._graphql(operation, _THREADS_QUERY, variables, _THREADS_DATA)
            review_threads = page.repository.pull_request.review_threads
            for thread in review_threads.nodes:
                for comment in thread.comments.nodes:
                    if comment.full_database_id is not None:
                        threads[comment.full_database_id] = thread
            if not review_threads.page_info.has_next_page:
                return threads
            variables["cursor"] = review_threads.page_info.end_cursor
        raise _over_page_limit(operation)

    def _list[T](self, operation: str, url: str, schema: TypeAdapter[list[T]]) -> list[T]:
        """Every item of a REST list, page by page, stopping at the first page that isn't full."""
        items: list[T] = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self._send(
                operation, "GET", f"{url}?per_page={_PER_PAGE}&page={page}", None, schema
            )
            items.extend(batch)
            if len(batch) < _PER_PAGE:
                return items
        raise _over_page_limit(operation)

    def _graphql[T](
        self, operation: str, query: str, variables: Mapping[str, object], schema: TypeAdapter[T]
    ) -> T:
        """Run a GraphQL query or mutation, and parse its ``data`` with ``schema``."""
        payload = {"query": query, "variables": dict(variables)}
        response = self._send(operation, "POST", self._graphql_url, payload, _GRAPHQL_RESPONSE)
        if response.errors:
            message = self._shown(response.errors[0].message)
            raise GitHubError(
                f"GitHub {operation} failed: GraphQL error: {message}. {_RETRY_ADVICE}"
            )
        try:
            return schema.validate_python(response.data)
        except ValidationError as exc:
            raise _schema_error(operation, exc) from exc

    def _send[T](
        self,
        operation: str,
        method: str,
        url: str,
        payload: Mapping[str, object] | None,
        schema: TypeAdapter[T],
    ) -> T:
        """Make one request, and parse its JSON response with ``schema``.

        Raises:
            GitHubError: If the request fails, or the response is over 5 MB, isn't JSON or
                doesn't match ``schema``.
        """
        request = urllib.request.Request(  # noqa: S310 - the URL's scheme is checked in _api_url
            url,
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "review-bot",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout_s) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise GitHubError(
                _status_message(operation, exc.code, exc.headers.get("X-RateLimit-Remaining"))
            ) from exc
        except (OSError, HTTPException) as exc:
            raise GitHubError(
                f"GitHub {operation} failed: {self._transport_problem(exc)}. {_RETRY_ADVICE}"
            ) from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise GitHubError(
                f"GitHub {operation} failed: the response is over 5 MB, the most read. "
                f"{_RETRY_ADVICE}"
            )
        try:
            return schema.validate_json(body)
        except ValidationError as exc:
            raise _schema_error(operation, exc) from exc

    def _shown(self, text: str) -> str:
        """Response text fit for an error line: on one line, cut, and with no token or secret.

        GitHub may quote the request back, ``Authorization`` header included.
        """
        scrubbed = redact(text.replace(self._token, "[token]"))
        return " ".join(scrubbed.split())[:_MAX_SHOWN_CHARS]

    def _transport_problem(self, exc: OSError | HTTPException) -> str:
        """What went wrong when no HTTP status came back."""
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, TimeoutError):
            return f"no response within {self._timeout_s:g} s"
        return f"the connection failed ({type(reason).__name__})"


def _is_bot(user: _User | None) -> bool:
    return user is not None and user.login == BOT_LOGIN and user.user_type == BOT_USER_TYPE


def _thread_of(operation: str, threads: Mapping[int, _Thread], comment_id: int) -> _Thread:
    thread = threads.get(comment_id)
    if thread is None:
        raise GitHubError(
            f"GitHub {operation} failed: no review thread holds comment {comment_id}. "
            f"{_RETRY_ADVICE}"
        )
    return thread


def _bot_comment(comment: _ReviewComment, marker: Marker, thread: _Thread) -> BotComment:
    return BotComment(
        comment_id=comment.id,
        thread_id=thread.id,
        fingerprint=marker.fingerprint,
        rule_part=marker.rule_part,
        anchor=marker.anchor,
        path=comment.path,
        line=None if thread.is_outdated else comment.line,
        severity=marker.severity,
        resolved_by=_resolver(thread),
    )


def _resolver(thread: _Thread) -> Resolver | None:
    """Who resolved ``thread``, or None while it is open.

    GraphQL's ``resolvedBy`` is a ``User``, so a bot that resolved a thread may read as null; a
    null resolver counts as the bot, so a thread the bot resolved is never a pairing candidate.
    """
    if not thread.is_resolved:
        return None
    if thread.resolved_by is None or thread.resolved_by.login == _GRAPHQL_BOT_LOGIN:
        return Resolver.BOT
    return Resolver.PERSON


def _status_message(operation: str, status: int, rate_limit_remaining: str | None) -> str:
    rate_limited = status == 429 or (  # noqa: PLR2004 - HTTP's "Too Many Requests"
        status == 403 and rate_limit_remaining == "0"  # noqa: PLR2004
    )
    if rate_limited:
        problem = "the rate limit is used up. Nothing was retried; run the job again later."
    else:
        problem = _STATUS_PROBLEMS.get(status, f"GitHub returned an error. {_RETRY_ADVICE}")
    return f"GitHub {operation} failed: HTTP {status}, {problem}"


def _schema_error(operation: str, exc: ValidationError) -> GitHubError:
    """The error for a response that isn't JSON, or lacks a field; it names no value."""
    first = exc.errors()[0]
    if first["type"] == "json_invalid":
        problem = "the response isn't JSON"
    else:
        where = ".".join(str(part) for part in first["loc"]) or "its top level"
        problem = f"the response doesn't have the expected shape at {where}"
    return GitHubError(f"GitHub {operation} failed: {problem}. {_RETRY_ADVICE}")


def _over_page_limit(operation: str) -> GitHubError:
    return GitHubError(
        f"GitHub {operation} failed: it reached the limit of {_MAX_PAGES} pages of {_PER_PAGE} "
        "items, so not every item could be read."
    )
