"""The real ``GitHubClient``: GitHub's REST and GraphQL APIs over ``urllib`` (P2-S6, S8, S11).

It has its own module so the pure ``plan`` and ``shadow`` modules, which import the data types in
``github``, never load the HTTP stack.

Every request sends the token only in its ``Authorization`` header, has a 30-second socket
timeout, follows no redirect and is never retried. A response is read up to 5 MB, or 1 MB for an
issue, and parsed with a schema that ignores unknown fields. Any failure raises ``GitHubError``:
one line naming the operation and the status or problem, never the token or the response's text.
"""

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from http.client import HTTPException
from typing import Final, Self
from urllib.parse import urlencode, urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic.alias_generators import to_camel

from review_bot.context.issues import IssueText
from review_bot.errors import ConfigError, GitHubError
from review_bot.governance.gate import GateResult
from review_bot.no_redirect import NoRedirect
from review_bot.pr.comments import (
    Marker,
    holds_gate_state,
    is_summary,
    read_marker,
    read_reviewed_sha,
    with_summary_marker,
)
from review_bot.pr.event import PullRequestRef
from review_bot.pr.github import (
    BOT_LOGIN,
    MAX_MERGED_PULLS,
    BotComment,
    CommentSignals,
    MergedPullRequest,
    NewComment,
    Resolver,
    Summary,
    SummaryState,
    is_any_bot,
    is_bot_user,
    too_many_merged_pulls,
)
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
_MEGABYTE: Final = 1024 * 1024
_MAX_RESPONSE_BYTES: Final = 5 * _MEGABYTE
# An issue is read for its title and body, which are cut far below this (P3-S9 Limits).
_MAX_ISSUE_BYTES: Final = _MEGABYTE
_NOT_FOUND: Final = 404
_PER_PAGE: Final = 100
_MAX_PAGES: Final = 10
_LOCAL_HOSTS: Final = frozenset({"127.0.0.1", "localhost"})
# A token is visible ASCII, so it can't break the header it is sent in.
_TOKEN: Final = re.compile(r"[\x21-\x7e]+")
# GraphQL names the Actions bot without REST's "[bot]" (Phase 2 design, Comment state).
_GRAPHQL_BOT_LOGIN: Final = "github-actions"
# GitHub's name for a deleted account, which is never the bot.
_DELETED_ACCOUNT: Final = "ghost"
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
    f"{GITHUB_TOKEN_VARIABLE} holds characters a token can't have, so nothing ran. Set it "
    "to the workflow's token."
)
_BAD_API_URL: Final = (
    f"{API_URL_VARIABLE} must be an https:// URL, or an http:// one on 127.0.0.1 or localhost, "
    "so nothing ran. Unset it to use https://api.github.com."
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
# Who last edited each comment, by its node ID: an IssueComment or a PullRequestReviewComment.
_EDITORS_QUERY: Final = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on IssueComment { id lastEditedAt editor { login } }
    ... on PullRequestReviewComment { id lastEditedAt editor { login } }
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


class _RestSchema(BaseModel):
    """A REST response's shape, with REST's snake_case names; unknown fields are ignored."""

    model_config = ConfigDict(frozen=True)


class _User(_Schema):
    login: str
    user_type: str = Field(alias="type")


class _ReviewComment(_Schema):
    id: int
    body: str
    path: str
    line: int | None
    user: _User | None
    # REST's own name; read only to ask GraphQL who last edited the comment.
    node_id: str | None = Field(default=None, alias="node_id")


class _IssueComment(_Schema):
    id: int
    body: str
    user: _User | None
    node_id: str | None = Field(default=None, alias="node_id")


class _Reaction(_Schema):
    content: str
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


class _Edited(_Schema):
    id: str
    # Null until someone edits the comment.
    last_edited_at: str | None
    # Null for a deleted account.
    editor: _Actor | None


class _EditedNodes(_Schema):
    nodes: list[_Edited | None]


class _ThreadId(_Schema):
    id: str


class _ResolvePayload(_Schema):
    thread: _ThreadId


class _ResolveData(_Schema):
    resolve_review_thread: _ResolvePayload


class _SearchPull(_RestSchema):
    merged_at: AwareDatetime | None


class _SearchItem(_RestSchema):
    number: int
    title: str
    created_at: AwareDatetime
    pull_request: _SearchPull | None = None


class _SearchCount(_RestSchema):
    total_count: int
    incomplete_results: bool


class _SearchPage(_SearchCount):
    items: list[_SearchItem]


class _GitActor(_RestSchema):
    date: AwareDatetime


class _GitCommit(_RestSchema):
    committer: _GitActor


class _PullCommit(_RestSchema):
    commit: _GitCommit


class _Issue(_RestSchema):
    title: str
    body: str | None
    # Present, as an object, only when the issue is a pull request.
    pull_request: dict[str, object] | None = None


_REVIEW_COMMENTS: Final = TypeAdapter(list[_ReviewComment])
_ISSUE_COMMENTS: Final = TypeAdapter(list[_IssueComment])
_REACTIONS: Final = TypeAdapter(list[_Reaction])
_WITH_ID: Final = TypeAdapter(_WithId)
_GRAPHQL_RESPONSE: Final = TypeAdapter(_GraphQLResponse)
_THREADS_DATA: Final = TypeAdapter(_ThreadsData)
_RESOLVE_DATA: Final = TypeAdapter(_ResolveData)
_EDITED_NODES: Final = TypeAdapter(_EditedNodes)
_SEARCH_COUNT: Final = TypeAdapter(_SearchCount)
_SEARCH_PAGE: Final = TypeAdapter(_SearchPage)
_PULL_COMMITS: Final = TypeAdapter(list[_PullCommit])
_ISSUE: Final = TypeAdapter(_Issue)


class _StatusError(GitHubError):
    """A call GitHub answered with an error status, so a caller can tell a 404 apart."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class RestGitHubClient:
    """A ``GitHubClient`` for one repository, through GitHub's REST and GraphQL APIs.

    Its calls take pull requests of that repository only.
    """

    def __init__(self, repo: str, access: GitHubAccess, *, timeout_s: float = _TIMEOUT_S) -> None:
        """Talk to ``repo`` (``owner/name``) with ``access``, each request within ``timeout_s``."""
        self._repo = repo
        self._search_url = f"{access.api_url}/search/issues"
        self._repo_url = f"{access.api_url}/repos/{repo}"
        # Right for api.github.com; GitHub Enterprise Server would need GITHUB_GRAPHQL_URL.
        self._graphql_url = f"{access.api_url}/graphql"
        self._token = access.token
        self._timeout_s = timeout_s
        self._opener = urllib.request.build_opener(NoRedirect)

    def list_bot_comments(self, pr: PullRequestRef) -> tuple[BotComment, ...]:
        """Return the bot's inline comments on ``pr`` with a valid marker, oldest first.

        A comment is the bot's when its REST author is ``github-actions[bot]`` of type ``Bot``.
        Its thread is the one its ID starts, and its line is None when that thread is outdated.
        """
        return self._bot_comments("list_bot_comments", pr)

    def list_signals(self, pr: PullRequestRef) -> tuple[CommentSignals, ...]:
        """Return what ``list_bot_comments`` does, each with the content of people's reactions.

        Each comment's reactions are one more listing. A reaction by any user of type ``Bot`` is
        left out; who left the others is dropped.
        """
        operation = "list_signals"
        return tuple(
            CommentSignals(comment, self._reactions(operation, comment.comment_id))
            for comment in self._bot_comments(operation, pr)
        )

    def read_summary(self, pr: PullRequestRef) -> Summary | None:
        """Return the bot's summary comment on ``pr``, or None if it has none.

        The summary is the bot's oldest issue comment whose last line is a summary marker.
        """
        summary = self._summary_comment("read_summary", pr)
        if summary is None:
            return None
        return Summary(read_reviewed_sha(summary.body), holds_gate_state(summary.body))

    def read_summary_state(self, pr: PullRequestRef) -> SummaryState | None:
        """Return the bot's summary comment on ``pr`` and who last edited it, or None.

        The summary is found as ``read_summary`` finds it; its last editor is one GraphQL query.
        """
        operation = "read_summary_state"
        summary = self._summary_comment(operation, pr)
        if summary is None:
            return None
        node_id = _node_id(operation, summary)
        return SummaryState(summary.body, self._last_editors(operation, [node_id])[node_id])

    def list_bot_comment_editors(self, pr: PullRequestRef) -> Mapping[int, str | None]:
        """Return each inline comment by the bot on ``pr``, by ID, mapped to its last editor.

        A comment is the bot's by its REST author, whatever its body. The last editors are
        GraphQL queries of up to 100 comments each.
        """
        operation = "list_bot_comment_editors"
        comments = self._list(operation, f"{self._pull_url(pr)}/comments", _REVIEW_COMMENTS)
        own = [comment for comment in comments if _is_bot(comment.user)]
        editors = self._last_editors(operation, [_node_id(operation, comment) for comment in own])
        return {comment.id: editors[_node_id(operation, comment)] for comment in own}

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

    def write_summary(
        self,
        pr: PullRequestRef,
        body: str,
        *,
        reviewed_sha: str | None,
        gate: GateResult | None = None,
    ) -> None:
        """Edit the bot's summary comment on ``pr`` in place, or create it if there is none.

        The posted body ends with the summary marker recording ``reviewed_sha`` and ``gate``,
        which is how the next run finds it (``read_summary``).
        """
        operation = "write_summary"
        summary = self._summary_comment(operation, pr)
        payload = {"body": with_summary_marker(body, reviewed_sha=reviewed_sha, gate=gate)}
        if summary is None:
            url = f"{self._issue_url(pr)}/comments"
            self._send(operation, "POST", url, payload, _WITH_ID)
        else:
            url = f"{self._repo_url}/issues/comments/{summary.id}"
            self._send(operation, "PATCH", url, payload, _WITH_ID)

    def list_merged_pull_requests(
        self, repo: str, since: date, until: date
    ) -> tuple[MergedPullRequest, ...]:
        """Return the pull requests merged in the window, found by the search API.

        Each one's commits are one more listing, of the at most 250 GitHub lists for a pull
        request.
        """
        operation = "list_merged_pull_requests"
        query = f"repo:{self._own(repo)} is:pr is:merged merged:{_window(since, until)}"
        return tuple(
            MergedPullRequest(
                number=item.number,
                title=item.title,
                created_at=item.created_at,
                merged_at=item.pull_request.merged_at,
                commit_dates=self._commit_dates(operation, item.number),
            )
            for item in self._search_merged_pulls(operation, query)
            if item.pull_request is not None and item.pull_request.merged_at is not None
        )

    def count_issues(self, repo: str, label: str, since: date, until: date) -> int:
        """Return the search API's count of the issues labelled ``label`` created in the window."""
        query = f'repo:{self._own(repo)} is:issue label:"{label}" created:{_window(since, until)}'
        return self._search(
            "count_issues", query, page=1, per_page=1, schema=_SEARCH_COUNT
        ).total_count

    def read_issue(self, repo: str, number: int) -> IssueText | None:
        """Return issue ``number`` of ``repo``, this client's repository, or None on a 404.

        The response is read up to 1 MB.
        """
        self._own(repo)
        url = f"{self._repo_url}/issues/{number}"
        try:
            issue = self._send("read_issue", "GET", url, None, _ISSUE, max_bytes=_MAX_ISSUE_BYTES)
        except _StatusError as exc:
            if exc.status == _NOT_FOUND:
                return None
            raise
        is_pull_request = issue.pull_request is not None
        return IssueText(issue.title, issue.body or "", is_pull_request=is_pull_request)

    def _search_merged_pulls(self, operation: str, query: str) -> list[_SearchItem]:
        """Every pull request the search API matches ``query`` with, page by page, once each.

        A pull request seen on two pages, as results shift while paging, is kept once.

        Raises:
            GitHubError: If it matches more than ``MAX_MERGED_PULLS``, the most the search API
                returns.
        """
        found: dict[int, _SearchItem] = {}
        for page in range(1, _MAX_PAGES + 1):
            batch = self._search(
                operation, query, page=page, per_page=_PER_PAGE, schema=_SEARCH_PAGE
            )
            if batch.total_count > MAX_MERGED_PULLS:
                raise too_many_merged_pulls(batch.total_count)
            for item in batch.items:
                found.setdefault(item.number, item)
            if len(batch.items) < _PER_PAGE or len(found) >= batch.total_count:
                break
        return list(found.values())

    def _search[T: _SearchCount](
        self, operation: str, query: str, *, page: int, per_page: int, schema: TypeAdapter[T]
    ) -> T:
        """One page of the search API's issues and pull requests matching ``query``.

        Results are oldest first, a stable order to page through.

        Raises:
            GitHubError: If the call fails, or the search timed out before finding every match.
        """
        parameters = {
            "q": query,
            "sort": "created",
            "order": "asc",
            "per_page": per_page,
            "page": page,
        }
        url = f"{self._search_url}?{urlencode(parameters)}"
        result = self._send(operation, "GET", url, None, schema)
        if result.incomplete_results:
            raise GitHubError(
                f"GitHub {operation} failed: the search timed out before finding every match. "
                f"{_RETRY_ADVICE}"
            )
        return result

    def _commit_dates(self, operation: str, number: int) -> tuple[datetime, ...]:
        """The committer date of each of pull request ``number``'s commits."""
        commits = self._list(operation, f"{self._repo_url}/pulls/{number}/commits", _PULL_COMMITS)
        return tuple(commit.commit.committer.date for commit in commits)

    def _bot_comments(self, operation: str, pr: PullRequestRef) -> tuple[BotComment, ...]:
        """The bot's inline comments on ``pr`` with a valid marker, oldest first."""
        comments = self._list(operation, f"{self._pull_url(pr)}/comments", _REVIEW_COMMENTS)
        threads = self._threads_by_comment(operation, pr)
        return tuple(
            _bot_comment(comment, marker, _thread_of(operation, threads, comment.id))
            for comment in sorted(comments, key=lambda comment: comment.id)
            # The author first, so nobody else's text is parsed.
            if _is_bot(comment.user) and (marker := read_marker(comment.body)) is not None
        )

    def _reactions(self, operation: str, comment_id: int) -> frozenset[str]:
        """The content of every reaction people left on an inline comment."""
        url = f"{self._repo_url}/pulls/comments/{comment_id}/reactions"
        reactions = self._list(operation, url, _REACTIONS)
        return frozenset(
            reaction.content for reaction in reactions if not _is_any_bot(reaction.user)
        )

    def _summary_comment(self, operation: str, pr: PullRequestRef) -> _IssueComment | None:
        """The bot's oldest issue comment on ``pr`` whose last line is a summary marker."""
        comments = self._list(operation, f"{self._issue_url(pr)}/comments", _ISSUE_COMMENTS)
        return min(
            (
                comment
                for comment in comments
                # The author first, so nobody else's text is parsed.
                if _is_bot(comment.user) and is_summary(comment.body)
            ),
            key=lambda comment: comment.id,
            default=None,
        )

    def _number(self, pr: PullRequestRef) -> int:
        """``pr``'s number, once it is known to be of this client's repository."""
        self._own(pr.repo)
        return pr.number

    def _own(self, repo: str) -> str:
        """``repo``, once it is known to be this client's repository."""
        if repo != self._repo:
            raise ValueError(f"This client is for {self._repo}, not {repo}.")
        return repo

    def _pull_url(self, pr: PullRequestRef) -> str:
        return f"{self._repo_url}/pulls/{self._number(pr)}"

    def _issue_url(self, pr: PullRequestRef) -> str:
        return f"{self._repo_url}/issues/{self._number(pr)}"

    def _last_editors(self, operation: str, node_ids: Sequence[str]) -> dict[str, str | None]:
        """Who last edited each comment, by node ID (``_last_editor``).

        Raises:
            GitHubError: If a query fails, or returns no comment for a node ID asked about.
        """
        editors: dict[str, str | None] = {}
        for start in range(0, len(node_ids), _PER_PAGE):
            batch = list(node_ids[start : start + _PER_PAGE])
            page = self._graphql(operation, _EDITORS_QUERY, {"ids": batch}, _EDITED_NODES)
            editors.update((node.id, _last_editor(node)) for node in page.nodes if node is not None)
        missing = next((node_id for node_id in node_ids if node_id not in editors), None)
        if missing is not None:
            raise GitHubError(
                f"GitHub {operation} failed: GraphQL returned no comment for node {missing}. "
                f"{_RETRY_ADVICE}"
            )
        return editors

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

    def _send[T](  # noqa: PLR0913 - max_bytes is keyword-only
        self,
        operation: str,
        method: str,
        url: str,
        payload: Mapping[str, object] | None,
        schema: TypeAdapter[T],
        *,
        max_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> T:
        """Make one request, and parse its JSON response, read up to ``max_bytes``, with ``schema``.

        Raises:
            GitHubError: If the request fails, or the response is over ``max_bytes``, isn't JSON
                or doesn't match ``schema``; ``_StatusError`` if GitHub answered with an error
                status.
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
                body = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise _StatusError(
                _status_message(operation, exc.code, exc.headers.get("X-RateLimit-Remaining")),
                status=exc.code,
            ) from exc
        except (OSError, HTTPException) as exc:
            raise GitHubError(
                f"GitHub {operation} failed: {self._transport_problem(exc)}. {_RETRY_ADVICE}"
            ) from exc
        if len(body) > max_bytes:
            raise GitHubError(
                f"GitHub {operation} failed: the response is over {max_bytes // _MEGABYTE} MB, "
                f"the most read. {_RETRY_ADVICE}"
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


def _window(since: date, until: date) -> str:
    """A search qualifier's range: the UTC days from ``since`` to ``until``, both included."""
    return f"{since.isoformat()}T00:00:00Z..{until.isoformat()}T23:59:59Z"


def _is_bot(user: _User | None) -> bool:
    return user is not None and is_bot_user(user.login, user.user_type)


def _is_any_bot(user: _User | None) -> bool:
    """Whether ``user`` is a bot of any kind; a deleted account, with no user, is a person."""
    return user is not None and is_any_bot(user.user_type)


def _thread_of(operation: str, threads: Mapping[int, _Thread], comment_id: int) -> _Thread:
    thread = threads.get(comment_id)
    if thread is None:
        raise GitHubError(
            f"GitHub {operation} failed: no review thread holds comment {comment_id}. "
            f"{_RETRY_ADVICE}"
        )
    return thread


def _node_id(operation: str, comment: _ReviewComment | _IssueComment) -> str:
    if comment.node_id is None:
        raise GitHubError(
            f"GitHub {operation} failed: comment {comment.id} has no node_id. {_RETRY_ADVICE}"
        )
    return comment.node_id


def _last_editor(node: _Edited) -> str | None:
    """Who last edited a comment, or None if nobody did; GraphQL names the bot without "[bot]"."""
    if node.last_edited_at is None:
        return None
    if node.editor is None:
        return _DELETED_ACCOUNT
    return BOT_LOGIN if node.editor.login == _GRAPHQL_BOT_LOGIN else node.editor.login


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
        confidence=marker.confidence,
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
