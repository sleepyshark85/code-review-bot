"""A local GitHub API on 127.0.0.1 for the real client's tests (P2-S6). Never the network.

It serves the REST and GraphQL calls a comment-mode run makes, with GitHub's field names and
more fields than a client needs, so a client that ignores unknown fields reads them:

- `GET  /repos/{o}/{r}/pulls/{n}/comments`: review comments, paged as GitHub pages them
  (`per_page` default 30, at most 100; `page`; a `Link` header with `rel="next"`);
- `POST /graphql` reading `reviewThreads`: threads with who resolved them, paged by `first` and
  `after`, as `nodes` and as `edges`; each comment's `fullDatabaseId` is a string, and its
  `databaseId` null above 2^31 - 1, as GitHub's 32-bit field is;
- `POST /graphql` with `resolveReviewThread`: the thread-resolve mutation;
- `POST /repos/{o}/{r}/pulls/{n}/reviews`: review creation;
- `PATCH /repos/{o}/{r}/pulls/comments/{id}`: a review comment edit;
- `GET  /repos/{o}/{r}/pulls/comments/{id}/reactions`: a review comment's reactions, paged as
  above (P2-S8); the same reactions are in each GraphQL comment node's `reactions`, with GraphQL's
  content names and logins, and summed in the REST comment's `reactions` counts;
- `GET`, `POST /repos/{o}/{r}/issues/{n}/comments` and `PATCH /repos/{o}/{r}/issues/comments/{id}`:
  issue comments, for the summary.

Every request is recorded with its headers and body. `fail(route, kind)` makes every request to
one route fail the same way, so a retry would be seen and counted.
"""

import contextlib
import json
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

BOT_LOGIN = "github-actions[bot]"
GRAPHQL_BOT_LOGIN = "github-actions"
OVER_5_MB = 5 * 1024 * 1024 + 4096
MAX_INT32 = 2**31 - 1
# How long a `timeout` failure holds its response; a client with a 30 s timeout capped by the
# test gives up long before this.
HOLD_S = 20.0

# REST reaction content -> GraphQL `ReactionContent`.
GRAPHQL_REACTION = {
    "+1": "THUMBS_UP",
    "-1": "THUMBS_DOWN",
    "laugh": "LAUGH",
    "confused": "CONFUSED",
    "heart": "HEART",
    "hooray": "HOORAY",
    "rocket": "ROCKET",
    "eyes": "EYES",
}

FAILURE_STATUS = {"401": 401, "403": 403, "404": 404, "422": 422, "500": 500}
FAILURE_KINDS = (*FAILURE_STATUS, "rate_limited", "429", "timeout", "not_json", "over_5_mb")


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]  # names lower-cased
    body: bytes
    route: str

    @property
    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


@dataclass(slots=True)
class Thread:
    thread_id: str
    comment_ids: list[int]
    resolved_by: tuple[str, str] | None = None  # (GraphQL login, __typename)
    outdated: bool = False


@dataclass(slots=True)
class State:
    review_comments: list[dict[str, Any]] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)
    issue_comments: list[dict[str, Any]] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    endless_review_comments: bool = False
    # P2-S8: each review comment's reactions as (REST content, REST login, user type).
    reactions: dict[int, list[tuple[str, str, str]]] = field(default_factory=dict)
    endless_reactions: bool = False
    # Hostile: a failure's message quotes the request's `Authorization` header back.
    echo_authorization: bool = False
    next_id: int = 900_000


def user(login: str, user_type: str) -> dict[str, Any]:
    return {
        "login": login,
        "id": abs(hash(login)) % 10_000_000,
        "node_id": f"U_{login}",
        "type": user_type,
        "site_admin": False,
        "avatar_url": "https://avatars.example.invalid/u",
    }


def review_comment(  # noqa: PLR0913 - one keyword per GitHub fact a comment has
    comment_id: int,
    *,
    path: str,
    line: int,
    body: str,
    login: str = BOT_LOGIN,
    user_type: str = "Bot",
    outdated: bool = False,
    in_reply_to: int | None = None,
    commit_id: str = "b" * 40,
) -> dict[str, Any]:
    """A REST pull request review comment. Outdated: `line` and `position` are null."""
    comment: dict[str, Any] = {
        "url": f"https://api.example.invalid/pulls/comments/{comment_id}",
        "pull_request_review_id": 50_000 + comment_id,
        "id": comment_id,
        "node_id": f"PRRC_{comment_id}",
        "diff_hunk": "@@ -0,0 +1,6 @@\n+def load(path):",
        "path": path,
        "position": None if outdated else line,
        "original_position": line,
        "commit_id": commit_id,
        "original_commit_id": commit_id,
        "user": user(login, user_type),
        "body": body,
        "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-01T10:00:00Z",
        "html_url": f"https://github.example.invalid/pull/7#discussion_r{comment_id}",
        "pull_request_url": "https://api.example.invalid/pulls/7",
        "author_association": "NONE" if user_type == "Bot" else "MEMBER",
        "_links": {"self": {"href": "https://api.example.invalid"}},
        "reactions": {"url": "https://api.example.invalid", "total_count": 0}
        | dict.fromkeys(GRAPHQL_REACTION, 0),
        "start_line": None,
        "original_start_line": None,
        "start_side": None,
        "line": None if outdated else line,
        "original_line": line,
        "side": "RIGHT",
        "subject_type": "line",
    }
    if in_reply_to is not None:
        comment["in_reply_to_id"] = in_reply_to
    return comment


def issue_comment(comment_id: int, *, body: str, login: str = BOT_LOGIN) -> dict[str, Any]:
    user_type = "Bot" if login.endswith("[bot]") else "User"
    return {
        "url": f"https://api.example.invalid/issues/comments/{comment_id}",
        "html_url": f"https://github.example.invalid/pull/7#issuecomment-{comment_id}",
        "issue_url": "https://api.example.invalid/issues/7",
        "id": comment_id,
        "node_id": f"IC_{comment_id}",
        "user": user(login, user_type),
        "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-01T10:00:00Z",
        "author_association": "NONE" if user_type == "Bot" else "MEMBER",
        "body": body,
        "reactions": {"url": "https://api.example.invalid", "total_count": 0},
    }


class GitHubStub:
    """The server, its state and its request log. Use as a context manager."""

    def __init__(self) -> None:
        self.state = State()
        self.requests: list[Request] = []
        self._lock = threading.Lock()
        self._release = threading.Event()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def __enter__(self) -> "GitHubStub":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()

    # Seeding.

    def add_review_comment(
        self,
        comment: dict[str, Any],
        *,
        resolved_by: tuple[str, str] | None = None,
    ) -> None:
        """Add a comment; a top-level one starts its own thread, a reply joins its parent's."""
        self.state.review_comments.append(comment)
        parent = comment.get("in_reply_to_id")
        if parent is None:
            self.state.threads.append(
                Thread(
                    thread_id=f"PRRT_kw{comment['id']}",
                    comment_ids=[comment["id"]],
                    resolved_by=resolved_by,
                    outdated=comment["line"] is None,
                )
            )
            return
        (thread,) = [t for t in self.state.threads if parent in t.comment_ids]
        thread.comment_ids.append(comment["id"])

    def add_reaction(
        self, comment_id: int, content: str, login: str, user_type: str = "User"
    ) -> None:
        """A reaction on a review comment, by the REST user `login` (P2-S8)."""
        assert content in GRAPHQL_REACTION, content
        self.state.reactions.setdefault(comment_id, []).append((content, login, user_type))
        (comment,) = [c for c in self.state.review_comments if c["id"] == comment_id]
        counts = comment["reactions"]
        counts["total_count"] = counts.get("total_count", 0) + 1
        counts[content] = counts.get(content, 0) + 1

    def add_issue_comment(self, comment: dict[str, Any]) -> None:
        self.state.issue_comments.append(comment)

    def fail(self, route: str, kind: str) -> None:
        assert kind in FAILURE_KINDS, kind
        self.state.failures[route] = kind

    # Reading what was asked.

    def routed(self, route: str) -> list[Request]:
        return [request for request in self.requests if request.route == route]

    def writes(self) -> list[Request]:
        return [request for request in self.requests if not request.route.startswith("list_")]

    def all_text(self) -> Iterator[tuple[str, str]]:
        """Each part of each request but the `Authorization` header, as (where, text)."""
        for number, request in enumerate(self.requests):
            yield f"{number}: path", request.path
            yield f"{number}: query", json.dumps(request.query)
            yield f"{number}: body", request.body.decode("utf-8", "replace")
            for name, value in request.headers.items():
                if name != "authorization":
                    yield f"{number}: header {name}", f"{name}: {value}"

    # Serving.

    def record(self, request: Request) -> None:
        with self._lock:
            self.requests.append(request)

    def hold(self) -> None:
        self._release.wait(HOLD_S)


_ROUTES = (
    ("GET", re.compile(r"/repos/[^/]+/[^/]+/pulls/\d+/comments"), "list_review_comments"),
    ("POST", re.compile(r"/repos/[^/]+/[^/]+/pulls/\d+/reviews"), "create_review"),
    ("PATCH", re.compile(r"/repos/[^/]+/[^/]+/pulls/comments/(\d+)"), "edit_review_comment"),
    ("GET", re.compile(r"/repos/[^/]+/[^/]+/pulls/comments/(\d+)/reactions"), "list_reactions"),
    ("GET", re.compile(r"/repos/[^/]+/[^/]+/issues/\d+/comments"), "list_issue_comments"),
    ("POST", re.compile(r"/repos/[^/]+/[^/]+/issues/\d+/comments"), "create_issue_comment"),
    ("PATCH", re.compile(r"/repos/[^/]+/[^/]+/issues/comments/(\d+)"), "edit_issue_comment"),
)


def route_of(method: str, path: str, body: bytes) -> str:
    if method == "POST" and path == "/graphql":
        query = str((json.loads(body or b"{}") or {}).get("query", ""))
        if "resolveReviewThread" in query:
            return "graphql_resolve"
        if "reviewThreads" in query:
            return "list_threads"
        return "graphql_other"
    for route_method, pattern, name in _ROUTES:
        if method == route_method and pattern.fullmatch(path):
            return name
    return "unknown"


def _handler_for(stub: GitHubStub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            pass

        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            route = route_of(self.command, parts.path, body)
            request = Request(
                method=self.command,
                path=parts.path,
                query=parse_qs(parts.query),
                headers={name.lower(): value for name, value in self.headers.items()},
                body=body,
                route=route,
            )
            stub.record(request)
            failure = stub.state.failures.get(route)
            if failure is not None:
                self._fail(failure, request)
                return
            status, payload, headers = _respond(stub, request)
            self._send(status, json.dumps(payload).encode("utf-8"), headers)

        do_GET = do_POST = do_PATCH = _serve  # noqa: N815 - http.server's names

        def _fail(self, kind: str, request: Request) -> None:
            echoed = (
                request.headers.get("authorization", "") if stub.state.echo_authorization else ""
            )
            if kind in FAILURE_STATUS:
                message = {
                    "message": f"Stub says {kind}. {echoed}",
                    "documentation_url": "https://x.invalid",
                }
                self._send(FAILURE_STATUS[kind], json.dumps(message).encode(), {})
            elif kind == "rate_limited":
                body = json.dumps({"message": f"API rate limit exceeded. {echoed}"}).encode()
                self._send(
                    403, body, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1893456000"}
                )
            elif kind == "429":
                body = json.dumps({"message": f"secondary rate limit. {echoed}"}).encode()
                self._send(429, body, {"Retry-After": "1"})
            elif kind == "timeout":
                stub.hold()
                self._send_quietly(200, b"{}", {})
            elif kind == "not_json":
                page = f"<html><body>Unicorn! {echoed}</body></html>".encode()
                self._send(200, page, {}, "text/html")
            else:  # over_5_mb: a well-formed answer, only too big
                _, payload, headers = _respond(stub, request)
                self._send(200, json.dumps(_padded(payload)).encode("utf-8"), headers)

        def _send(
            self,
            status: int,
            body: bytes,
            headers: dict[str, str],
            content_type: str = "application/json; charset=utf-8",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-RateLimit-Limit", "5000")
            if "X-RateLimit-Remaining" not in headers:
                self.send_header("X-RateLimit-Remaining", "4999")
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_quietly(self, status: int, body: bytes, headers: dict[str, str]) -> None:
            with contextlib.suppress(OSError):  # the client gave up first, as it should
                self._send(status, body, headers)

    return Handler


def _padded(payload: Any) -> Any:
    padding = "x" * OVER_5_MB
    if isinstance(payload, dict):
        return payload | {"padding": padding}
    if isinstance(payload, list) and payload:
        return [payload[0] | {"padding": padding}, *payload[1:]]
    return [{"padding": padding}]


def _respond(stub: GitHubStub, request: Request) -> tuple[int, Any, dict[str, str]]:
    state = stub.state
    if request.route == "list_review_comments" and state.endless_review_comments:
        return _endless_page(stub, request)
    if request.route == "list_reactions" and state.endless_reactions:
        return _endless_reactions(stub, request)
    responders = {
        "list_review_comments": lambda: _page(stub, request, state.review_comments),
        "list_reactions": lambda: _page(stub, request, _rest_reactions(state, request)),
        "list_issue_comments": lambda: _page(stub, request, state.issue_comments),
        "list_threads": lambda: (200, _threads_page(state, request.json or {}), {}),
        "graphql_resolve": lambda: (200, _resolved(state, request), {}),
        "create_review": lambda: (200, _created_review(state, request.json or {}), {}),
        "edit_review_comment": lambda: _edited(state, request),
        "edit_issue_comment": lambda: _edited(state, request),
        "create_issue_comment": lambda: (201, _created_issue_comment(state, request), {}),
        "graphql_other": lambda: (
            200,
            {"errors": [{"message": "the stub doesn't serve this query"}]},
            {},
        ),
    }
    respond = responders.get(request.route)
    if respond is None:
        return 404, {"message": "Not Found", "documentation_url": "https://x.invalid"}, {}
    return respond()


def _created_issue_comment(state: State, request: Request) -> dict[str, Any]:
    state.next_id += 1
    comment = issue_comment(state.next_id, body=(request.json or {}).get("body", ""))
    state.issue_comments.append(comment)
    return comment


def _paging(request: Request) -> tuple[int, int]:
    per_page = min(int(request.query.get("per_page", ["30"])[0]), 100)
    page = max(int(request.query.get("page", ["1"])[0]), 1)
    return per_page, page


def _link(stub: GitHubStub, request: Request, per_page: int, page: int, last: int) -> str:
    def url(number: int) -> str:
        return f"{stub.url}{request.path}?per_page={per_page}&page={number}"

    return f'<{url(page + 1)}>; rel="next", <{url(last)}>; rel="last"'


def _page(
    stub: GitHubStub, request: Request, items: list[dict[str, Any]]
) -> tuple[int, Any, dict[str, str]]:
    per_page, page = _paging(request)
    last = max(1, -(-len(items) // per_page))
    chosen = items[(page - 1) * per_page : page * per_page]
    headers = {"Link": _link(stub, request, per_page, page, last)} if page < last else {}
    return 200, chosen, headers


def _endless_page(stub: GitHubStub, request: Request) -> tuple[int, Any, dict[str, str]]:
    per_page, page = _paging(request)
    start = 1_000_000 + page * 1000
    chosen = [
        review_comment(start + offset, path="src/app.py", line=3, body="Not ours.", login="octocat",
                       user_type="User")
        for offset in range(per_page)
    ]  # fmt: skip
    return 200, chosen, {"Link": _link(stub, request, per_page, page, page + 1)}


def _rest_reactions(state: State, request: Request) -> list[dict[str, Any]]:
    comment_id = int(request.path.rsplit("/", 2)[1])
    return [
        {
            "id": 70_000 + index,
            "node_id": f"REA_{comment_id}_{index}",
            "user": user(login, user_type),
            "content": content,
            "created_at": "2026-11-02T10:00:00Z",
        }
        for index, (content, login, user_type) in enumerate(state.reactions.get(comment_id, []))
    ]


def _endless_reactions(stub: GitHubStub, request: Request) -> tuple[int, Any, dict[str, str]]:
    per_page, page = _paging(request)
    chosen = [
        {"id": page * 1000 + offset, "node_id": f"REA_{page}_{offset}",
         "user": user("octocat", "User"), "content": "+1", "created_at": "2026-11-02T10:00:00Z"}
        for offset in range(per_page)
    ]  # fmt: skip
    return 200, chosen, {"Link": _link(stub, request, per_page, page, page + 1)}


def _graphql_reactions(state: State, comment_id: int) -> dict[str, Any]:
    nodes = [
        {
            "content": GRAPHQL_REACTION[content],
            "user": {
                "login": GRAPHQL_BOT_LOGIN if login == BOT_LOGIN else login,
                "__typename": user_type,
            },
        }
        for content, login, user_type in state.reactions.get(comment_id, [])
    ]
    return {
        "totalCount": len(nodes),
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": nodes,
    }


_ARGUMENTS = re.compile(r"reviewThreads\s*\(([^)]*)\)")


def _argument(document: dict[str, Any], name: str) -> Any:
    """`name`'s value in `reviewThreads(...)`, read from the query or its variables."""
    match = _ARGUMENTS.search(str(document.get("query", "")))
    if match is None:
        return None
    found = re.search(rf"\b{name}\s*:\s*(\$\w+|\"[^\"]*\"|\d+|null)", match.group(1))
    if found is None:
        return None
    value = found.group(1)
    if value.startswith("$"):
        return (document.get("variables") or {}).get(value[1:])
    if value == "null":
        return None
    return value.strip('"') if value.startswith('"') else int(value)


def _threads_page(state: State, document: dict[str, Any]) -> dict[str, Any]:
    first = min(int(_argument(document, "first") or 100), 100)
    after = _argument(document, "after")
    start = int(after) if after else 0
    chosen = state.threads[start : start + first]
    end = start + len(chosen)
    nodes = [_thread_node(state, thread) for thread in chosen]
    threads = {
        "totalCount": len(state.threads),
        "pageInfo": {
            "hasNextPage": end < len(state.threads),
            "endCursor": str(end) if chosen else None,
            "hasPreviousPage": start > 0,
            "startCursor": str(start),
        },
        "nodes": nodes,
        "edges": [
            {"cursor": str(start + index + 1), "node": node} for index, node in enumerate(nodes)
        ],
    }
    pull_request = {"id": "PR_kw7", "number": 7, "reviewThreads": threads}
    return {"data": {"repository": {"pullRequest": pull_request}}}


def _thread_node(state: State, thread: Thread) -> dict[str, Any]:
    by_id = {comment["id"]: comment for comment in state.review_comments}
    comments = [by_id[comment_id] for comment_id in thread.comment_ids]
    first = comments[0]
    resolver = (
        None
        if thread.resolved_by is None
        else {"login": thread.resolved_by[0], "__typename": thread.resolved_by[1]}
    )
    comment_nodes = [
        {
            "id": comment["node_id"],
            # GitHub's `databaseId` is a 32-bit Int, null past it; `fullDatabaseId` is a BigInt,
            # sent as a string.
            "databaseId": comment["id"] if comment["id"] <= MAX_INT32 else None,
            "fullDatabaseId": str(comment["id"]),
            "author": {
                "login": GRAPHQL_BOT_LOGIN
                if comment["user"]["login"] == BOT_LOGIN
                else comment["user"]["login"],
                "__typename": comment["user"]["type"],
            },
            "body": comment["body"],
            "path": comment["path"],
            "outdated": thread.outdated,
            "url": comment["html_url"],
            "reactions": _graphql_reactions(state, comment["id"]),
        }
        for comment in comments
    ]
    return {
        "__typename": "PullRequestReviewThread",
        "id": thread.thread_id,
        "isResolved": thread.resolved_by is not None,
        "isOutdated": thread.outdated,
        "isCollapsed": thread.resolved_by is not None,
        "path": first["path"],
        "line": first["line"],
        "originalLine": first["original_line"],
        "startLine": None,
        "diffSide": "RIGHT",
        "subjectType": "LINE",
        "viewerCanResolve": True,
        "viewerCanUnresolve": True,
        "resolvedBy": resolver,
        "comments": {
            "totalCount": len(comment_nodes),
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": comment_nodes,
            "edges": [{"cursor": "c", "node": node} for node in comment_nodes],
        },
    }


def _resolved(state: State, request: Request) -> dict[str, Any]:
    text = json.dumps(request.json or {})
    for thread in state.threads:
        if thread.thread_id in text:
            thread.resolved_by = (GRAPHQL_BOT_LOGIN, "Bot")
            node = {"id": thread.thread_id, "isResolved": True}
            return {"data": {"resolveReviewThread": {"thread": node}}}
    echoed = request.headers.get("authorization", "") if state.echo_authorization else ""
    return {
        "data": {"resolveReviewThread": None},
        "errors": [{"type": "NOT_FOUND", "message": f"Could not resolve to a node. {echoed}"}],
    }


def _created_review(state: State, document: dict[str, Any]) -> dict[str, Any]:
    state.next_id += 1
    review_id = state.next_id
    for new in document.get("comments") or []:
        state.next_id += 1
        comment = review_comment(
            state.next_id,
            path=new.get("path", ""),
            line=int(new.get("line") or 0),
            body=new.get("body", ""),
            commit_id=document.get("commit_id", "b" * 40),
        )
        state.review_comments.append(comment)
        state.threads.append(Thread(f"PRRT_kw{state.next_id}", [state.next_id]))
    return {
        "id": review_id,
        "node_id": f"PRR_{review_id}",
        "user": user(BOT_LOGIN, "Bot"),
        "body": document.get("body") or "",
        "state": "COMMENTED",
        "html_url": f"https://github.example.invalid/pull/7#pullrequestreview-{review_id}",
        "pull_request_url": "https://api.example.invalid/pulls/7",
        "commit_id": document.get("commit_id"),
        "submitted_at": "2026-11-08T10:00:00Z",
        "author_association": "NONE",
        "_links": {},
    }


def _edited(state: State, request: Request) -> tuple[int, Any, dict[str, str]]:
    comment_id = int(request.path.rsplit("/", 1)[1])
    pool = state.review_comments if request.route == "edit_review_comment" else state.issue_comments
    for comment in pool:
        if comment["id"] == comment_id:
            comment["body"] = (request.json or {}).get("body", comment["body"])
            return 200, comment, {}
    return 404, {"message": "Not Found", "documentation_url": "https://x.invalid"}, {}
