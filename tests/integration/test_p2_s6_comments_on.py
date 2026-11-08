r"""P2-S6: turn comments on with one repository variable.

Written from the spec's criteria, design.md (Interfaces, Comment state, Safety on the PR path)
and ADRs 0008 and 0012 while the developer built `RestGitHubClient`, before any of its code was
read.

- **AC1 to AC7** run the real client against `github_stub.GitHubStub`, a GitHub API on
  127.0.0.1 that records every request with its headers. Some tests call the client directly,
  built only by `make_client` below, the one place to change if its seam differs. The others run
  `python -m review_bot.pr run --mode comment` in process with no injected client, so the run
  builds it from `GITHUB_TOKEN` and `GITHUB_API_URL` as Actions sets them. Nothing reaches a
  model, the network or api.github.com: AC7's default URL is seen at the name lookup, which
  fails.
- **AC8** reads the environment the Claude CLI subprocess would get, from the fake SDK's options
  merged over `os.environ` as `SubprocessCLITransport.connect` merges them.
- **AC9 to AC11** read `.github/workflows/review.yml` with the P2-S3 helpers. P2-S3's AC6, AC9
  and AC10 tests already walk every job, so they cover the comment job unchanged.

Timeouts are tested without waiting 30 s: the held-response cases build the client with
`timeout_s=SHORT_TIMEOUT_S`, and the stub holds its answer far longer. The 30 s default is read
by recording each timeout the client gives a socket.
"""

import importlib
import json
import os
import re
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from fake_sdk import FakeSdk, findings_json, result_message
from github_stub import (
    FAILURE_KINDS,
    GRAPHQL_BOT_LOGIN,
    GitHubStub,
    issue_comment,
    review_comment,
)
from test_p2_s1_shadow_report_local import load_shadow, marker
from test_p2_s2_review_pr_event import event
from test_p2_s3_shadow_workflow import (
    CAPS,
    OUT_DIR,
    REPORT_FILES,
    SHADOW_ACTIONS,
    WORKFLOW_PATH,
    argv_of,
    collect_job_name,
    collect_step_in,
    job_runs,
    jobs_of,
    option_value,
    review_job,
    run_script,
    run_step_in,
    same_dir,
    steps_of,
    steps_using,
    summary_step_in,
    triggers_of,
    upload_step_in,
    walk,
)
from test_p2_s5_comment_sync import (
    APP,
    APP_V1,
    BASE_FILE,
    BASE_TEXT,
    EVAL_LINE,
    WRITE_LINE,
    PullRequestRepo,
    ai,
    text,
)

from review_bot import cli as review_bot_cli
from review_bot.errors import GitHubError
from review_bot.pr import cli as pr_cli
from review_bot.pr.event import PullRequestRef
from review_bot.pr.github import NewComment
from review_bot.providers.claude_agent import ClaudeAgentProvider
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

TOKEN = "ghs_canary"  # noqa: S105 - a canary to look for, not a credential
REPO = "octo/review-bot"  # the event's repository (P2-S2's `event`)
NUMBER = 7
BASE_SHA, HEAD_SHA = "a" * 40, "b" * 40
GITHUB_ERROR_EXIT = 18
USAGE_EXIT = 2
EVENT_ERROR_EXIT = 19
SHORT_TIMEOUT_S = 0.5  # the stub holds a `timeout` answer for 20 s
TIMEOUT_LIMIT_S = 30
OTHER_FILE = "src/other.py"
BIG_COMMENT_ID = 2**31 + 1250
# The variables a run reads, or might, for GitHub; each test sets the ones it means.
GITHUB_VARIABLES = ("GITHUB_TOKEN", "GITHUB_API_URL", "GITHUB_GRAPHQL_URL", "GITHUB_REPOSITORY")
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


# The RestGitHubClient seam: the only code that knows how the client is built.

CLIENT_MODULE = "review_bot.pr.rest_github"


def make_client(api_url: str, token: str = TOKEN, *, timeout_s: float | None = None) -> Any:
    """The client for `REPO`; `timeout_s` None keeps the client's own default."""
    module = importlib.import_module(CLIENT_MODULE)
    access = module.GitHubAccess(api_url, token)
    if timeout_s is None:
        return module.RestGitHubClient(REPO, access)
    return module.RestGitHubClient(REPO, access, timeout_s=timeout_s)


def client_for(stub: GitHubStub, kind: str) -> Any:
    """A short timeout only where the stub holds its answer, so no other case can time out."""
    return make_client(stub.url, timeout_s=SHORT_TIMEOUT_S if kind == "timeout" else None)


# Set-up.


@pytest.fixture(autouse=True)
def github_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No GitHub variable, proxy or CI flag from the machine running the tests."""
    for name in (*GITHUB_VARIABLES, *PROXY_VARIABLES, "CI"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


@pytest.fixture
def stub() -> Iterator[GitHubStub]:
    with GitHubStub() as server:
        yield server


@pytest.fixture
def socket_timeouts(monkeypatch: pytest.MonkeyPatch) -> list[float | None]:
    """Every timeout a socket is given."""
    recorded: list[float | None] = []
    real_settimeout = socket.socket.settimeout

    def settimeout(self: socket.socket, value: float | None) -> None:
        recorded.append(value)
        real_settimeout(self, value)

    monkeypatch.setattr(socket.socket, "settimeout", settimeout)
    return recorded


def pr_ref() -> PullRequestRef:
    return PullRequestRef(
        repo=REPO,
        number=NUMBER,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        is_fork=False,
        is_draft=False,
        event_at=datetime(2026, 11, 8, 10, 0, tzinfo=UTC),
        action="synchronize",
    )


def bot_body(fingerprint: str, anchor: str, severity: str = "high", rule: str = "ai:logic") -> str:
    """A body as `comment_body` writes it: severity heading first, marker last."""
    heading = f"**{severity}** logic (source ai, confidence 0.90)"
    return f"{heading}\n\nR.\n\nReact.\n\n{marker(fingerprint, rule, anchor)}"


def fingerprint_for(comment_id: int) -> str:
    return f"{comment_id:016x}"


def anchor_for(comment_id: int) -> str:
    return f"{comment_id * 31:016x}"


def own_body(comment_id: int, severity: str = "high") -> str:
    return bot_body(fingerprint_for(comment_id), anchor_for(comment_id), severity)


# Comment-mode runs through the command line.


@dataclass(frozen=True, slots=True)
class CliRun:
    exit_code: int
    stderr: str
    out: Path
    provider: Any

    def written_text(self) -> str:
        """Every file the run wrote in `--out`: the report, its Markdown and the audit log."""
        return "".join(path.read_text(encoding="utf-8") for path in sorted(self.out.iterdir()))


def comment_run(  # noqa: PLR0913 - two fixtures, then keyword-only run inputs
    where: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    env: dict[str, str],
    findings: tuple[Any, ...] = (),
    provider: Any = None,
    mode: str = "comment",
) -> CliRun:
    """`run --mode comment` over a real PR adding `APP`, with no injected GitHub client.

    The provider is a `MockProvider` with `findings` unless one is given; `mode` may be `shadow`.
    """
    repo = PullRequestRepo(where / "clone", {BASE_FILE: BASE_TEXT})
    head = repo.commit({APP: text(APP_V1)})
    event_path = where / "event.json"
    event_path.write_text(json.dumps(event(repo.base, head, action="synchronize")), "utf-8")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("REVIEW_BOT_AUDIT_LOG")  # so the audit log lands in --out
    if provider is None:
        provider = MockProvider(findings=list(findings))
    provider_name = "mock" if isinstance(provider, MockProvider) else "claude"
    out = where / "out"
    capsys.readouterr()
    exit_code = pr_cli.main(
        [
            "run", "--event", str(event_path), "--repo", str(repo.path), "--out", str(out),
            "--provider", provider_name, "--mode", mode,
        ],
        provider=provider,
    )  # fmt: skip
    return CliRun(exit_code, capsys.readouterr().err, out, provider)


def stub_env(stub: GitHubStub, token: str = TOKEN) -> dict[str, str]:
    return {"GITHUB_TOKEN": token, "GITHUB_API_URL": stub.url}


# --- AC1: comment mode builds the real client from GITHUB_TOKEN, or exits 2 without it --------


def test_p2_s6_ac1_comment_mode_with_a_token_posts_through_the_rest_client(
    stub, tmp_path, capsys, monkeypatch
):
    run = comment_run(
        tmp_path, capsys, monkeypatch, env=stub_env(stub), findings=(ai(EVAL_LINE), ai(WRITE_LINE))
    )

    assert run.exit_code == 0, run.stderr
    assert stub.routed("list_review_comments")
    (review,) = stub.routed("create_review")
    assert [c["line"] for c in review.json["comments"]] == [EVAL_LINE, WRITE_LINE]
    summary_writes = stub.routed("create_issue_comment") + stub.routed("edit_issue_comment")
    assert len(summary_writes) == 1
    assert all(TOKEN in request.headers.get("authorization", "") for request in stub.requests)
    shadow = load_shadow(run.out)
    assert shadow["mode"] == "comment"
    assert [entry["applied"] for entry in shadow["comments"]] == ["done", "done"]


@pytest.mark.parametrize("token", [None, ""], ids=["unset", "empty"])
def test_p2_s6_ac1_no_token_exits_2_naming_it_before_the_provider(
    stub, tmp_path, capsys, monkeypatch, token
):
    env = {"GITHUB_API_URL": stub.url} | ({} if token is None else {"GITHUB_TOKEN": token})

    run = comment_run(tmp_path, capsys, monkeypatch, env=env, findings=(ai(EVAL_LINE),))

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert "GITHUB_TOKEN" in run.stderr
    assert run.provider.requests == []
    assert stub.requests == []


# --- AC2: list_bot_comments over 3 pages, with threads and who resolved them --------------------


def seed_listing(stub: GitHubStub) -> list[tuple[Any, ...]]:
    """250 review comments (3 pages of 100); return the expected `BotComment` fields."""
    expected: list[tuple[Any, ...]] = []

    def own(  # noqa: PLR0913 - keyword-only, one per fact the listing must read back
        comment_id: int,
        *,
        path: str = APP,
        line: int = 3,
        severity: str = "high",
        outdated: bool = False,
        resolved_by: tuple[str, str] | None = None,
        resolver: str | None = None,
    ) -> None:
        body = own_body(comment_id, severity)
        comment = review_comment(comment_id, path=path, line=line, body=body, outdated=outdated)
        stub.add_review_comment(comment, resolved_by=resolved_by)
        expected.append(
            (
                comment_id,
                f"PRRT_kw{comment_id}",
                fingerprint_for(comment_id),
                "ai:logic",
                anchor_for(comment_id),
                path,
                None if outdated else line,
                severity,
                resolver,
            )
        )

    def foreign(comment_id: int, body: str, **facts: Any) -> None:
        stub.add_review_comment(review_comment(comment_id, path=APP, line=3, body=body, **facts))

    own(1001)
    foreign(1002, own_body(1002), login="octocat", user_type="User")  # a person, valid marker
    foreign(1003, own_body(1003), user_type="User")  # the bot's login, but not a Bot
    foreign(1004, own_body(1004), login=GRAPHQL_BOT_LOGIN)  # GraphQL's login, not REST's
    foreign(1005, "Coverage is 91%.")  # another workflow's comment, as the same bot
    foreign(1006, bot_body(fingerprint_for(1006), anchor_for(1006), rule="ai:bogus"))
    foreign(1007, own_body(1007) + "\nP.S. text after the marker")
    for comment_id in range(1008, 1241):
        own(comment_id, line=comment_id % 50 + 1)
    # Page 3.
    own(1241, outdated=True, severity="medium")
    own(1242, resolved_by=(GRAPHQL_BOT_LOGIN, "Bot"), resolver="bot")
    own(1243, resolved_by=("octocat", "User"), resolver="person")
    foreign(1244, own_body(1244), login="octocat", user_type="User", in_reply_to=1243)
    own(1245, path=OTHER_FILE, line=12, severity="low")
    for comment_id in range(1246, 1250):
        own(comment_id, line=comment_id % 50 + 1)
    own(BIG_COMMENT_ID, line=20)  # past 32 bits: only `fullDatabaseId` links it to its thread
    stub.state.threads.reverse()  # so threads are matched by comment ID, not by order
    return expected


def test_p2_s6_ac2_lists_only_own_marked_comments_over_three_pages_with_their_threads(stub):
    expected = seed_listing(stub)

    listed = make_client(stub.url).list_bot_comments(pr_ref())

    assert len(stub.state.review_comments) == 250
    got = sorted(
        (
            c.comment_id,
            c.thread_id,
            c.fingerprint,
            c.rule_part,
            c.anchor,
            c.path,
            c.line,
            str(c.severity),
            None if c.resolved_by is None else str(c.resolved_by),
        )
        for c in listed
    )
    assert got == sorted(expected)


# --- AC3: post_review sends one COMMENT review on the head --------------------------------------


def test_p2_s6_ac3_post_review_sends_one_comment_review_with_path_line_side_and_body(stub):
    comments = [NewComment(APP, EVAL_LINE, "First body."), NewComment(OTHER_FILE, 12, "Second.")]

    make_client(stub.url).post_review(pr_ref(), comments)

    (request,) = stub.requests
    assert (request.method, request.path) == ("POST", f"/repos/{REPO}/pulls/{NUMBER}/reviews")
    assert request.json["event"] == "COMMENT"
    assert request.json["commit_id"] == HEAD_SHA
    sent = [
        {key: c.get(key) for key in ("path", "line", "side", "body")}
        for c in request.json["comments"]
    ]
    assert sent == [
        {"path": APP, "line": EVAL_LINE, "side": "RIGHT", "body": "First body."},
        {"path": OTHER_FILE, "line": 12, "side": "RIGHT", "body": "Second."},
    ]


# --- AC4: one edit, one resolve, and a summary created or edited in place ----------------------


def test_p2_s6_ac4_update_comment_sends_one_edit(stub):
    stub.add_review_comment(review_comment(1001, path=APP, line=3, body=own_body(1001)))

    make_client(stub.url).update_comment(1001, "New body.")

    (request,) = stub.requests
    assert (request.method, request.path) == ("PATCH", f"/repos/{REPO}/pulls/comments/1001")
    assert request.json == {"body": "New body."}


def test_p2_s6_ac4_resolve_thread_sends_one_resolve_mutation(stub):
    stub.add_review_comment(review_comment(1001, path=APP, line=3, body=own_body(1001)))

    make_client(stub.url).resolve_thread("PRRT_kw1001")

    (request,) = stub.requests
    assert request.route == "graphql_resolve"
    assert "PRRT_kw1001" in json.dumps(request.json)
    assert stub.state.threads[0].resolved_by is not None


SUMMARY_MARKER = f"<!-- review-bot:v1 summary reviewed={'c' * 40} -->"
OLD_SUMMARY = f"**Review bot:** `reviewed`. No findings at or above medium.\n\n{SUMMARY_MARKER}"
SUMMARY_ID = 5110  # on the second page of 100


def seed_issue_comments(stub: GitHubStub, *, with_summary: bool) -> None:
    """120 issue comments; decoys early, and the bot's own summary on page 2 if asked."""
    for comment_id in range(5001, 5121):
        if comment_id == 5002:
            body, login = "Coverage is 91%.", "github-actions[bot]"  # another workflow
        elif comment_id == 5003:
            body, login = OLD_SUMMARY, "octocat"  # a person quoting the summary
        elif comment_id == SUMMARY_ID and with_summary:
            body, login = OLD_SUMMARY, "github-actions[bot]"
        else:
            body, login = f"Comment {comment_id}.", "octocat"
        stub.add_issue_comment(issue_comment(comment_id, body=body, login=login))


def test_p2_s6_ac4_write_summary_creates_one_when_the_bot_has_none(stub):
    seed_issue_comments(stub, with_summary=False)

    make_client(stub.url).write_summary(pr_ref(), "New summary.", reviewed_sha=None)

    (write,) = stub.writes()
    assert (write.method, write.path) == ("POST", f"/repos/{REPO}/issues/{NUMBER}/comments")
    assert write.json["body"].startswith("New summary.")


def test_p2_s6_ac4_write_summary_edits_the_bots_existing_one_in_place(stub):
    seed_issue_comments(stub, with_summary=True)

    make_client(stub.url).write_summary(pr_ref(), "New summary.", reviewed_sha=None)

    (write,) = stub.writes()
    assert (write.method, write.path) == ("PATCH", f"/repos/{REPO}/issues/comments/{SUMMARY_ID}")
    assert write.json["body"].startswith("New summary.")


# --- AC5: every failure is one GitHubError line naming the call and status, never retried ------

# Each operation, the stub route its failing request takes, and how its name may be written.
OPERATIONS: dict[str, tuple[str, str, Callable[[Any], object]]] = {
    "list_bot_comments": (
        "list_review_comments",
        r"list",
        lambda client: client.list_bot_comments(pr_ref()),
    ),
    "post_review": (
        "create_review",
        r"post|review",
        lambda client: client.post_review(pr_ref(), [NewComment(APP, EVAL_LINE, "Body.")]),
    ),
    "update_comment": (
        "edit_review_comment",
        r"updat|edit",
        lambda client: client.update_comment(1001, "Body."),
    ),
    "resolve_thread": (
        "graphql_resolve",
        r"resolv",
        lambda client: client.resolve_thread("PRRT_kw1001"),
    ),
    "write_summary": (
        "create_issue_comment",
        r"summary",
        lambda client: client.write_summary(pr_ref(), "Summary.", reviewed_sha=None),
    ),
}
STATUS_IN_MESSAGE = {
    "401": "401",
    "403": "403",
    "404": "404",
    "422": "422",
    "500": "500",
    "rate_limited": "403",
    "429": "429",
}


def seed_one_own_comment(stub: GitHubStub) -> None:
    stub.add_review_comment(review_comment(1001, path=APP, line=3, body=own_body(1001)))


@pytest.mark.parametrize("kind", FAILURE_KINDS)
@pytest.mark.parametrize("operation", OPERATIONS)
def test_p2_s6_ac5_failure_is_one_github_error_line_naming_call_and_status_and_isnt_retried(
    stub, operation, kind
):
    route, name, call = OPERATIONS[operation]
    seed_one_own_comment(stub)
    stub.fail(route, kind)

    with pytest.raises(GitHubError) as caught:
        call(client_for(stub, kind))

    message = str(caught.value).strip()
    assert message
    assert "\n" not in message, message
    assert re.search(name, message, re.IGNORECASE), message
    if kind in STATUS_IN_MESSAGE:
        assert STATUS_IN_MESSAGE[kind] in message, message
    if kind == "timeout":
        assert re.search(r"no response within|time", message, re.IGNORECASE), message
    assert len(stub.routed(route)) == 1, [r.route for r in stub.requests]


@pytest.mark.parametrize(
    "failure",
    [("list_review_comments", "500"), ("create_review", "422")],
    ids="-".join,
)
def test_p2_s6_ac5_failed_call_in_a_run_exits_18_naming_call_and_status(
    stub, tmp_path, capsys, monkeypatch, failure
):
    route, status = failure
    stub.fail(route, status)

    run = comment_run(tmp_path, capsys, monkeypatch, env=stub_env(stub), findings=(ai(EVAL_LINE),))

    assert run.exit_code == GITHUB_ERROR_EXIT, run.stderr
    assert status in run.stderr
    assert len(stub.routed(route)) == 1


# --- AC6: the token goes only in the Authorization header, and is written nowhere --------------


def test_p2_s6_ac6_token_is_sent_only_in_the_authorization_header(stub):
    seed_one_own_comment(stub)
    seed_issue_comments(stub, with_summary=True)
    client = make_client(stub.url)

    for _, _, call in OPERATIONS.values():
        call(client)

    assert {request.route for request in stub.requests} >= {
        "list_review_comments", "list_threads", "create_review", "edit_review_comment",
        "graphql_resolve", "list_issue_comments", "edit_issue_comment",
    }  # fmt: skip
    for request in stub.requests:
        assert TOKEN in request.headers.get("authorization", ""), request.path
    leaks = [where for where, part in stub.all_text() if TOKEN in part]
    assert leaks == []


@pytest.mark.parametrize("kind", FAILURE_KINDS)
def test_p2_s6_ac6_token_is_in_no_error_even_when_github_quotes_it_back(stub, kind):
    stub.state.echo_authorization = True
    stub.fail("list_review_comments", kind)

    with pytest.raises(GitHubError) as caught:
        client_for(stub, kind).list_bot_comments(pr_ref())

    error = caught.value
    assert TOKEN not in str(error)
    assert TOKEN not in repr(error)
    assert all(TOKEN not in str(arg) for arg in error.args)


def test_p2_s6_ac6_token_is_in_no_error_from_a_graphql_error_quoting_it(stub):
    stub.state.echo_authorization = True

    with pytest.raises(GitHubError) as caught:
        make_client(stub.url).resolve_thread("PRRT_missing")

    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("failing", [None, "create_review"], ids=["posted", "post_fails"])
def test_p2_s6_ac6_token_is_in_no_stderr_line_audit_record_or_report(
    stub, tmp_path, capsys, monkeypatch, failing
):
    stub.state.echo_authorization = True
    if failing is not None:
        stub.fail(failing, "500")

    run = comment_run(tmp_path, capsys, monkeypatch, env=stub_env(stub), findings=(ai(EVAL_LINE),))

    assert run.exit_code == (0 if failing is None else GITHUB_ERROR_EXIT), run.stderr
    assert {path.name for path in run.out.iterdir()} >= set(REPORT_FILES)
    assert TOKEN not in run.stderr
    assert TOKEN not in run.written_text()


# --- AC7: the API URL ---------------------------------------------------------------------------


def test_p2_s6_ac7_unset_api_url_sends_requests_to_api_github_com(tmp_path, capsys, monkeypatch):
    looked_up: list[tuple[str, str]] = []
    loopback_lookup = socket.getaddrinfo

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        looked_up.append((str(host), str(port)))
        if host in {"127.0.0.1", "::1", "localhost"}:
            return loopback_lookup(host, port, *args, **kwargs)
        raise socket.gaierror(socket.EAI_NONAME, "no network in tests")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    run = comment_run(tmp_path, capsys, monkeypatch, env={"GITHUB_TOKEN": TOKEN})

    assert ("api.github.com", "443") in looked_up, looked_up
    assert run.exit_code == GITHUB_ERROR_EXIT, run.stderr


@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com",
        "http://10.0.0.5:8080",
        "http://127.0.0.1.evil.example",
        "http://localhost.evil.example",
        "http://127.0.0.1@evil.example",
        "http://evil.example#@127.0.0.1",
    ],
)
def test_p2_s6_ac7_plain_http_to_a_host_other_than_loopback_exits_2_naming_the_variable(
    tmp_path, capsys, monkeypatch, url
):
    run = comment_run(
        tmp_path, capsys, monkeypatch, env={"GITHUB_TOKEN": TOKEN, "GITHUB_API_URL": url}
    )

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert "GITHUB_API_URL" in run.stderr


def test_p2_s6_ac7_plain_http_to_localhost_is_accepted(stub, tmp_path, capsys, monkeypatch):
    """`localhost` resolves to IPv4 only here, as the stub listens there; else Windows waits."""
    lookup = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: lookup(
            "127.0.0.1" if host == "localhost" else host, *args, **kwargs
        ),
    )
    url = stub.url.replace("127.0.0.1", "localhost")

    run = comment_run(
        tmp_path, capsys, monkeypatch, env={"GITHUB_TOKEN": TOKEN, "GITHUB_API_URL": url}
    )

    assert run.exit_code == 0, run.stderr
    assert stub.routed("list_review_comments")


# --- AC8: the Claude CLI's environment holds no GITHUB_TOKEN -------------------------------------


def claude_over_fake_sdk(seen: list[dict[str, str]]) -> ClaudeAgentProvider:
    """Claude over the fake SDK, recording what `SubprocessCLITransport.connect` (SDK 0.2.178)
    would pass the CLI subprocess: the inherited environment, then `options.env` on top.
    """

    async def capture(options: Any) -> None:
        inherited = {key: value for key, value in os.environ.items() if key != "CLAUDECODE"}
        seen.append({**inherited, "CLAUDE_CODE_ENTRYPOINT": "sdk-py", **options.env})

    _, settings = review_bot_cli.parse_args(["unused.diff", "--provider", "claude"])
    return ClaudeAgentProvider(settings, sdk_call=FakeSdk(capture, result_message(findings_json())))


def assert_no_token_in(environment: dict[str, str]) -> None:
    assert "GITHUB_TOKEN" not in environment
    assert not [name for name, value in environment.items() if TOKEN in value]


def test_p2_s6_ac8_claude_subprocess_environment_has_no_github_token(
    stub, tmp_path, capsys, monkeypatch
):
    """A comment-mode run reads the token for its client, then calls Claude."""
    seen: list[dict[str, str]] = []

    run = comment_run(
        tmp_path, capsys, monkeypatch, env=stub_env(stub), provider=claude_over_fake_sdk(seen)
    )

    assert run.exit_code == 0, run.stderr
    assert stub.requests, "the run made no GitHub call, so it may never have read the token"
    assert all(TOKEN in request.headers.get("authorization", "") for request in stub.requests)
    (environment,) = seen
    assert_no_token_in(environment)


def test_p2_s6_ac8_shadow_run_with_a_token_set_keeps_it_from_claude_too(
    tmp_path, capsys, monkeypatch
):
    seen: list[dict[str, str]] = []

    run = comment_run(
        tmp_path,
        capsys,
        monkeypatch,
        env={"GITHUB_TOKEN": TOKEN},
        provider=claude_over_fake_sdk(seen),
        mode="shadow",
    )

    assert run.exit_code == 0, run.stderr
    (environment,) = seen
    assert_no_token_in(environment)


# --- AC9 to AC11: the workflow --------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def comment_job(workflow: dict[Any, Any]) -> dict[str, Any]:
    return review_job(workflow, "comment")


def comment_run_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    step = run_step_in(comment_job(workflow))
    assert step is not None
    return step


def test_p2_s6_ac9_comment_job_runs_for_a_same_repo_pr_when_mode_is_comment(workflow):
    assert job_runs(comment_job(workflow), head_repo=REPO, mode="comment")


@pytest.mark.parametrize("mode", ["", "off", "shadow", "bogus"])
def test_p2_s6_ac9_comment_job_doesnt_run_for_any_other_mode(workflow, mode):
    assert not job_runs(comment_job(workflow), head_repo=REPO, mode=mode)


@pytest.mark.parametrize(
    "head_repo",
    ["someone/review-bot", "octo/review-bot-fork", None],
    ids=["fork", "similar-name", "deleted-fork"],
)
def test_p2_s6_ac9_comment_job_doesnt_run_for_a_fork(workflow, head_repo):
    assert not job_runs(comment_job(workflow), head_repo=head_repo, mode="comment")


@pytest.mark.parametrize(
    ("mode", "running"),
    [("", {"shadow"}), ("shadow", {"shadow"}), ("comment", {"comment"}), ("off", set())],
)
def test_p2_s6_ac9_at_most_one_review_job_runs_per_mode(workflow, mode, running):
    jobs = {"shadow": review_job(workflow, "shadow"), "comment": comment_job(workflow)}

    ran = {name for name, job in jobs.items() if job_runs(job, head_repo=REPO, mode=mode)}

    assert ran == running


def test_p2_s6_ac10_comment_job_runs_only_for_the_shadow_jobs_four_actions(workflow):
    """P2-S3 AC1's action list, for this job; S8's `closed` runs no comment job."""
    for action in set(triggers_of(workflow)["pull_request"]["types"]):
        runs = job_runs(comment_job(workflow), head_repo=REPO, mode="comment", action=action)
        assert runs == (action in SHADOW_ACTIONS), action


def test_p2_s6_ac10_comment_job_permissions_are_exactly_contents_read_and_pull_requests_write(
    workflow,
):
    assert comment_job(workflow).get("permissions") == {
        "contents": "read",
        "pull-requests": "write",
    }


def test_p2_s6_ac10_run_step_gets_both_credentials_and_passes_mode_comment_and_out(workflow):
    step = comment_run_step(workflow)

    assert re.fullmatch(
        r"\$\{\{\s*(secrets\.GITHUB_TOKEN|github\.token)\s*\}\}", step["env"]["GITHUB_TOKEN"]
    )
    assert step["env"]["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    argv = argv_of(step)
    assert argv[0] == "run"
    assert option_value(argv, "--mode") == "comment"
    assert same_dir(option_value(argv, "--out")) == OUT_DIR


CREDENTIAL = re.compile(r"secrets\.|github\.token|GITHUB_TOKEN|ANTHROPIC_API_KEY", re.IGNORECASE)


def test_p2_s6_ac10_no_other_step_gets_either_credential(workflow):
    """Keys and values in every job; the shadow job's key is P2-S3 AC5's, and the collect
    step's token is P2-S8 AC9's."""
    holders = set()
    for job_name, job in jobs_of(workflow).items():
        for path, node in walk(job):
            names = [str(key) for key in node] if isinstance(node, dict) else []
            texts = [node] if isinstance(node, str) else names
            if any(CREDENTIAL.search(item) for item in texts):
                holders.add((job_name, *path))
    comment_index = steps_of(comment_job(workflow)).index(comment_run_step(workflow))
    shadow = review_job(workflow, "shadow")
    shadow_step = run_step_in(shadow)
    assert shadow_step is not None
    shadow_index = steps_of(shadow).index(shadow_step)
    comment_name = next(n for n, j in jobs_of(workflow).items() if j is comment_job(workflow))
    shadow_name = next(n for n, j in jobs_of(workflow).items() if j is shadow)
    collect_name = collect_job_name(workflow)
    collect = jobs_of(workflow)[collect_name]
    collect_step = collect_step_in(collect)
    assert collect_step is not None
    collect_index = steps_of(collect).index(collect_step)

    allowed = {
        (comment_name, "steps", comment_index, "env"),
        (comment_name, "steps", comment_index, "env", "GITHUB_TOKEN"),
        (comment_name, "steps", comment_index, "env", "ANTHROPIC_API_KEY"),
        (shadow_name, "steps", shadow_index, "env"),
        (shadow_name, "steps", shadow_index, "env", "ANTHROPIC_API_KEY"),
        (collect_name, "steps", collect_index, "env"),
        (collect_name, "steps", collect_index, "env", "GITHUB_TOKEN"),
    }
    assert holders <= allowed, sorted(map(str, holders - allowed))
    assert "env" not in workflow
    assert "env" not in comment_job(workflow)


def test_p2_s6_ac10_comment_job_runs_the_bot_from_the_base_and_only_fetches_the_head(workflow):
    """P2-S3 AC4, for this job."""
    job = comment_job(workflow)
    steps = steps_of(job)
    (checkout,) = steps_using(job, "actions/checkout")
    assert re.fullmatch(
        r"\$\{\{\s*github\.event\.pull_request\.base\.sha\s*\}\}", checkout["with"]["ref"]
    )
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] is False
    readers = [
        step
        for step in steps
        if any("head.sha" in str(value) for value in (step.get("env") or {}).values())
    ]
    assert len(readers) == 1
    assert re.fullmatch(r'git fetch [^;&|`$]*"\$\w+"', readers[0]["run"].strip())
    (install,) = [i for i, step in enumerate(steps) if "uv sync" in step.get("run", "")]
    assert steps[install]["run"].split() == ["uv", "sync", "--locked", "--no-dev"]
    assert steps.index(checkout) < install < steps.index(comment_run_step(workflow))


def test_p2_s6_ac10_comment_job_has_the_design_caps_timeout_and_concurrency(workflow):
    """P2-S3 AC7, for this job."""
    job = comment_job(workflow)
    env = {name: str(value) for name, value in comment_run_step(workflow)["env"].items()}
    assert {name: env.get(name) for name in CAPS} == CAPS
    assert job["timeout-minutes"] == 10
    concurrency = job.get("concurrency") or workflow.get("concurrency")
    assert "github.event.pull_request.number" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is True


def test_p2_s6_ac10_comment_job_shows_the_report_and_uploads_it_always(workflow, tmp_path):
    """P2-S3 AC8, for this job: the summary script runs, and the artifact is as S3's."""
    job = comment_job(workflow)
    summary = summary_step_in(job)
    assert summary.get("if") == "always()"
    out = tmp_path / "review-bot"
    out.mkdir()
    (out / "shadow.md").write_text("# Review\n\n- commented on `a.py:3`\n", encoding="utf-8")
    step_summary = tmp_path / "summary.md"
    done = run_script(
        summary["run"],
        {"RUNNER_TEMP": tmp_path.as_posix(), "GITHUB_STEP_SUMMARY": step_summary.as_posix()},
        tmp_path,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "commented on `a.py:3`" in step_summary.read_text(encoding="utf-8")

    upload = upload_step_in(job)
    paths = [same_dir(line) for line in upload["with"]["path"].splitlines() if line.strip()]
    assert sorted(paths) == sorted(f"{OUT_DIR}/{name}" for name in REPORT_FILES)
    for part in (
        "github.event.pull_request.number",
        "github.event.pull_request.head.sha",
        "github.run_attempt",
    ):
        assert re.search(r"\$\{\{\s*" + re.escape(part) + r"\s*\}\}", upload["with"]["name"])
    assert upload["with"]["retention-days"] == 90
    assert upload.get("if") == "always()"


def test_p2_s6_ac10_run_command_and_caps_are_accepted_by_the_cli(
    workflow, tmp_path, monkeypatch, capsys
):
    """The step's own argv and env, with a broken event, get past parsing to the event (19)."""
    event_path = tmp_path / "event.json"
    event_path.write_text("not json", encoding="utf-8")
    for name in [name for name in os.environ if name.startswith("REVIEW_BOT_")]:
        monkeypatch.delenv(name)
    for name, value in comment_run_step(workflow)["env"].items():
        if not str(value).startswith("${{"):
            monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:9")  # never reached: the event fails
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    argv = [os.path.expandvars(word) for word in argv_of(comment_run_step(workflow))]

    exit_code = pr_cli.main(argv, provider=MockProvider(findings=[]))

    assert exit_code == EVENT_ERROR_EXIT, capsys.readouterr().err


def test_p2_s6_ac11_shadow_job_permissions_are_still_contents_read_only(workflow):
    assert review_job(workflow, "shadow").get("permissions") == {"contents": "read"}


# --- Limits: pages, timeouts and schemas ---------------------------------------------------------


def test_p2_s6_limits_more_than_10_pages_of_comments_exits_18_naming_the_limit(stub):
    stub.state.endless_review_comments = True

    with pytest.raises(GitHubError) as caught:
        make_client(stub.url).list_bot_comments(pr_ref())

    message = str(caught.value)
    assert "10" in message, message
    assert re.search(r"page|limit", message, re.IGNORECASE), message
    assert len(stub.routed("list_review_comments")) <= 11


def test_p2_s6_limits_every_request_has_a_timeout_of_at_most_30_s(stub, socket_timeouts):
    seed_one_own_comment(stub)
    client = make_client(stub.url)

    for _, _, call in OPERATIONS.values():
        call(client)

    assert socket_timeouts, "the client set no timeout on any socket"
    assert all(t is not None and 0 < t <= TIMEOUT_LIMIT_S for t in socket_timeouts), socket_timeouts


def test_p2_s6_limits_a_comment_missing_a_field_is_a_github_error(stub):
    comment = review_comment(1001, path=APP, line=3, body=own_body(1001))
    del comment["user"]
    stub.add_review_comment(comment)

    with pytest.raises(GitHubError):
        make_client(stub.url).list_bot_comments(pr_ref())


def test_p2_s6_limits_a_graphql_error_answer_is_a_github_error(stub):
    with pytest.raises(GitHubError) as caught:
        make_client(stub.url).resolve_thread("PRRT_missing")

    assert re.search(r"resolv", str(caught.value), re.IGNORECASE)
