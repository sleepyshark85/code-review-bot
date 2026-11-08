r"""P2-S8: reactions and resolutions on the bot's comments become `feedback/v1` labels.

Written from the spec's criteria, ADR 0011 and design.md (Comment state, `feedback/v1`) while the
developer built the slice, before any of its code was read.

- **AC1 to AC7, AC11** run `review_bot.pr.cli.main(["collect", "--event", PATH, "--out", DIR],
  github=FAKE)` in process. `FAKE` is the developer's `FakeGitHubClient`, reached only through
  `ClosedPr` below, the one place to change if its seam differs. The event is P2-S2's, with
  action `closed`; collect needs no clone.
- **AC8** runs `collect` with no injected client, so it builds `RestGitHubClient` from
  `GITHUB_TOKEN` and `GITHUB_API_URL`, against `github_stub.GitHubStub` on 127.0.0.1. Reactions
  are assumed read from REST `GET /pulls/comments/{id}/reactions` (the stub also serves them in
  each GraphQL comment node), and resolvers from GraphQL `reviewThreads.resolvedBy`, as in S6.
- **AC9** reads `.github/workflows/review.yml` with the P2-S3 helpers. P2-S3's AC6, AC9, AC10
  and AC11 tests already walk every job, so they cover the collect job unchanged.
- **AC10** feeds `collect`'s own `feedback.jsonl` to P2-S4's `label` as its labels file.

Usernames, resolver logins and comment bodies are canaries: none may reach any file in `DIR`.
"""

import importlib
import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from github_stub import GitHubStub, review_comment
from test_p2_s1_shadow_report_local import marker
from test_p2_s2_review_pr_event import GitRepo, event
from test_p2_s3_shadow_workflow import (
    OUT_DIR,
    REPO,
    SHADOW_ACTIONS,
    WORKFLOW_PATH,
    argv_of,
    collect_job_name,
    collect_step_in,
    job_runs,
    jobs_of,
    option_value,
    review_job,
    same_dir,
    steps_of,
    steps_using,
    triggers_of,
    walk,
)
from test_p2_s4_label_shadow_findings import (
    APP as LABEL_APP,
)
from test_p2_s4_label_shadow_findings import (
    HEAD,
    RECORD_KEYS,
    SHIFTED_HEAD,
    Labeller,
    Reports,
    keys,
    load,
)

pytestmark = pytest.mark.integration

BOT_LOGIN = "github-actions[bot]"
GRAPHQL_BOT_LOGIN = "github-actions"
NUMBER = 7
BASE_SHA, HEAD_SHA = "a" * 40, "b" * 40
GITHUB_ERROR_EXIT = 18
EVENT_ERROR_EXIT = 19
USAGE_EXIT = 2
TOKEN = "ghs_canary_s8"  # noqa: S105 - a canary to look for, not a credential
# Generous: it catches a hang or quadratic work, not a slow CI machine.
BOUND_S = 60.0

# Canaries: none of these may appear in any file `collect` writes.
PERSON_A = "canary-alice-5c1e"
PERSON_B = "canary-bob-9d2f"
RESOLVER = "canary-resolver-7a3b"
RATIONALE = "CANARY-RATIONALE-e41d the value is never checked"
OTHERS_BODY = "CANARY-OTHERS-BODY-0b77"
CANARIES = (PERSON_A, PERSON_B, RESOLVER, "CANARY-RATIONALE", "e41d", "CANARY-OTHERS")

APP = "src/app.py"
OTHER = "src/other.py"
RULE = "ai:logic"
FP1, ANCHOR1 = "1f" * 8, "a1" * 8
FP2, ANCHOR2 = "2e" * 8, "b2" * 8
FP3, ANCHOR3 = "3d" * 8, "c3" * 8
NULL_FIELDS = ("index", "source", "severity", "reason", "run_id", "model", "prompt_version")
GITHUB_VARIABLES = ("GITHUB_TOKEN", "GITHUB_API_URL", "GITHUB_GRAPHQL_URL", "GITHUB_REPOSITORY")
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No GitHub variable, proxy or CI flag from the machine running the tests."""
    for name in (*GITHUB_VARIABLES, *PROXY_VARIABLES, "CI"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


def own_body(fingerprint: str, anchor: str, *, rule: str = RULE, rationale: str = RATIONALE) -> str:
    """A body as `comment_body` writes it: severity heading first, marker last."""
    return (
        f"**high** logic (source ai, confidence 0.90)\n\n{rationale}\n\n"
        "Is this right and worth acting on? React with a thumbs-up if so, or a thumbs-down if "
        f"not.\n\n{marker(fingerprint, rule, anchor)}"
    )


# The FakeGitHubClient seam: the only code that knows the fake's own names.


class ClosedPr:
    """One closed pull request held by the developer's `FakeGitHubClient`."""

    def __init__(self) -> None:
        self.client = importlib.import_module("review_bot.pr.fake_github").FakeGitHubClient()

    def own(  # noqa: PLR0913 - keyword-only, one per fact an own comment has
        self,
        fingerprint: str = FP1,
        anchor: str = ANCHOR1,
        *,
        path: str = APP,
        line: int = 3,
        rule: str = RULE,
        resolved_by: str | None = None,
    ) -> int:
        body = own_body(fingerprint, anchor, rule=rule)
        return self.add(path=path, line=line, body=body, resolved_by=resolved_by)

    def add(  # noqa: PLR0913 - keyword-only, one per GitHub fact a comment has
        self,
        *,
        path: str = APP,
        line: int = 3,
        body: str,
        author: str = BOT_LOGIN,
        author_type: str = "Bot",
        resolved_by: str | None = None,
    ) -> int:
        comment_id: int = self.client.add_comment(
            path=path, line=line, body=body, author=author, author_type=author_type
        )
        if resolved_by is not None:
            self.client.resolve(comment_id, by=resolved_by)
        return comment_id

    def react(self, comment_id: int, content: str, by: str = PERSON_A) -> None:
        by_type = "Bot" if by == BOT_LOGIN else "User"
        self.client.react(comment_id, content, by=by, by_type=by_type)

    def fail(self, operation: str) -> None:
        self.client.fail_on(operation)

    def call_names(self) -> list[str]:
        return [call.name for call in self.client.calls]


# Running `collect` and reading what it wrote.


@dataclass(frozen=True, slots=True)
class Collected:
    exit_code: int
    stderr: str
    out: Path

    @property
    def feedback(self) -> Path:
        return self.out / "feedback.jsonl"

    @property
    def records(self) -> list[dict[str, Any]]:
        raw = self.feedback.read_bytes()
        assert not raw or raw.endswith(b"\n"), "feedback.jsonl doesn't end in a newline"
        return [json.loads(line) for line in raw.splitlines()]

    def labels(self) -> list[tuple[str, str, str]]:
        """Each record as (channel, label, fingerprint), sorted."""
        return sorted((r["channel"], r["label"], r["fingerprint"]) for r in self.records)

    def written_text(self) -> str:
        return "".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(self.out.rglob("*"))
            if path.is_file()
        )


def collect(
    where: Path,
    capsys: pytest.CaptureFixture[str],
    github: Any = None,
    *,
    name: str = "out",
) -> Collected:
    """`collect` for a closed same-repo PR; `github` None lets it build `RestGitHubClient`."""
    where.mkdir(parents=True, exist_ok=True)
    event_path = where / f"{name}-event.json"
    payload = event(BASE_SHA, HEAD_SHA, action="closed")
    event_path.write_text(json.dumps(payload), encoding="utf-8")
    out = where / name
    pr_cli = importlib.import_module("review_bot.pr.cli")
    extra = {} if github is None else {"github": github}
    capsys.readouterr()
    exit_code = pr_cli.main(["collect", "--event", str(event_path), "--out", str(out)], **extra)
    return Collected(exit_code, capsys.readouterr().err, out)


@pytest.fixture
def pr() -> ClosedPr:
    return ClosedPr()


def collected(pr: ClosedPr, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Collected:
    run = collect(tmp_path, capsys, pr.client)
    assert run.exit_code == 0, run.stderr
    return run


# AC1: one thumbs-up from a person is one `reaction` `accept` record.


def test_p2_s8_ac1_one_thumbs_up_from_a_person_is_one_reaction_accept_record(pr, tmp_path, capsys):
    pr.react(pr.own(), "+1")

    run = collected(pr, tmp_path, capsys)

    assert run.labels() == [("reaction", "accept", FP1)]


# AC2: thumbs-down alone is `reject`; both, from one person or two, is `conflict`.


@pytest.mark.parametrize(
    ("reactions", "label"),
    [
        ([("-1", PERSON_A)], "reject"),
        ([("+1", PERSON_A), ("-1", PERSON_A)], "conflict"),
        ([("+1", PERSON_A), ("-1", PERSON_B)], "conflict"),
        ([("-1", PERSON_A), ("+1", PERSON_B), ("+1", PERSON_A)], "conflict"),
        ([("+1", PERSON_A), ("+1", PERSON_B)], "accept"),
        ([("-1", PERSON_A), ("-1", PERSON_B)], "reject"),
    ],
    ids=[
        "down-only",
        "both-one-person",
        "both-two-people",
        "down-then-ups",
        "two-ups",
        "two-downs",
    ],
)
def test_p2_s8_ac2_thumbs_combine_into_one_label(pr, tmp_path, capsys, reactions, label):
    comment = pr.own()
    for content, person in reactions:
        pr.react(comment, content, person)

    run = collected(pr, tmp_path, capsys)

    assert run.labels() == [("reaction", label, FP1)]


# AC3: other reactions, and the bot's own, give no reaction record.


@pytest.mark.parametrize(
    "reactions",
    [
        [("heart", PERSON_A), ("laugh", PERSON_B), ("eyes", PERSON_A)],
        [("+1", BOT_LOGIN)],
        [("-1", BOT_LOGIN)],
        [("+1", BOT_LOGIN), ("-1", BOT_LOGIN), ("heart", PERSON_A)],
        [("hooray", PERSON_A), ("rocket", PERSON_A), ("confused", PERSON_B)],
    ],
    ids=["heart-laugh-eyes", "bot-up", "bot-down", "bot-both-and-heart", "other-contents"],
)
def test_p2_s8_ac3_no_thumbs_from_a_person_is_no_reaction_record(pr, tmp_path, capsys, reactions):
    comment = pr.own()
    for content, person in reactions:
        pr.react(comment, content, person)

    run = collected(pr, tmp_path, capsys)

    assert run.records == []


def test_p2_s8_ac3_the_bots_thumbs_up_doesnt_turn_a_persons_reject_into_conflict(
    pr, tmp_path, capsys
):
    comment = pr.own()
    pr.react(comment, "+1", BOT_LOGIN)
    pr.react(comment, "-1", PERSON_A)

    run = collected(pr, tmp_path, capsys)

    assert run.labels() == [("reaction", "reject", FP1)]


# AC4: a thread a person resolved is one `resolution` `resolved` record; the bot's, none.


def test_p2_s8_ac4_a_person_resolved_thread_is_one_resolution_record(pr, tmp_path, capsys):
    pr.own(resolved_by="person")

    run = collected(pr, tmp_path, capsys)

    assert run.labels() == [("resolution", "resolved", FP1)]


def test_p2_s8_ac4_a_resolution_record_comes_with_the_reaction_record(pr, tmp_path, capsys):
    pr.react(pr.own(resolved_by="person"), "-1")

    run = collected(pr, tmp_path, capsys)

    assert run.labels() == [("reaction", "reject", FP1), ("resolution", "resolved", FP1)]


def test_p2_s8_ac4_a_bot_resolved_thread_gives_no_resolution_record(pr, tmp_path, capsys):
    pr.own(resolved_by="bot")
    pr.react(pr.own(FP2, ANCHOR2, resolved_by="bot"), "+1")

    run = collected(pr, tmp_path, capsys)

    assert run.labels() == [("reaction", "accept", FP2)]


# AC5: comments by others, and bot comments without a valid marker, give no record.

FOREIGN = {
    "person-with-valid-marker": {"author": PERSON_A, "author_type": "User"},
    "bot-login-not-a-bot": {"author": BOT_LOGIN, "author_type": "User"},
    "graphql-login": {"author": GRAPHQL_BOT_LOGIN, "author_type": "Bot"},
    "other-bot": {"author": "dependabot[bot]", "author_type": "Bot"},
}
UNMARKED = {
    "no-marker": f"Coverage is 91%. {OTHERS_BODY}",
    "unknown-rule": own_body(FP1, ANCHOR1, rule="ai:bogus"),
    "short-fingerprint": own_body("1f" * 7, ANCHOR1),
    "text-after-marker": own_body(FP1, ANCHOR1) + f"\nP.S. {OTHERS_BODY}",
    "summary-marker": f"Summary.\n\n<!-- review-bot:v1 summary reviewed={'c' * 40} -->",
}


def react_and_resolve(pr: ClosedPr, comment_id: int) -> None:
    for content, person in (("+1", PERSON_A), ("-1", PERSON_B)):
        pr.react(comment_id, content, person)
    pr.client.resolve(comment_id, by="person")


@pytest.mark.parametrize("who", FOREIGN)
def test_p2_s8_ac5_a_comment_by_someone_else_gives_no_record(pr, tmp_path, capsys, who):
    react_and_resolve(pr, pr.add(body=own_body(FP1, ANCHOR1), **FOREIGN[who]))

    run = collected(pr, tmp_path, capsys)

    assert run.records == []


@pytest.mark.parametrize("body", UNMARKED)
def test_p2_s8_ac5_a_bot_comment_without_a_valid_marker_gives_no_record(pr, tmp_path, capsys, body):
    react_and_resolve(pr, pr.add(body=UNMARKED[body]))

    run = collected(pr, tmp_path, capsys)

    assert run.records == []


def test_p2_s8_ac5_no_own_comments_writes_no_record(pr, tmp_path, capsys):
    run = collected(pr, tmp_path, capsys)

    assert run.feedback.is_file(), "a successful collect writes feedback.jsonl, even if empty"
    assert run.records == []


# AC6: the record's keys and values; nothing personal reaches `DIR`.


def assert_record(record: dict[str, Any], *, channel: str, label: str, before: datetime) -> None:
    assert set(record) == RECORD_KEYS, sorted(record)
    assert {k: record[k] for k in RECORD_KEYS - {"recorded_at"}} == {
        "schema": "feedback/v1",
        "channel": channel,
        "repo": REPO,
        "pr": NUMBER,
        "fingerprint": FP3,
        "file": OTHER,
        "rule_part": "swallowed-exception",
        "anchor": ANCHOR3,
        "label": label,
    } | dict.fromkeys(NULL_FIELDS)
    recorded_at = datetime.fromisoformat(record["recorded_at"])
    assert recorded_at.utcoffset() == timedelta(0), record["recorded_at"]
    assert before - timedelta(seconds=1) <= recorded_at <= datetime.now().astimezone()


def test_p2_s8_ac6_records_have_s4s_keys_marker_fields_and_null_join_fields(pr, tmp_path, capsys):
    comment = pr.own(
        FP3, ANCHOR3, path=OTHER, line=12, rule="swallowed-exception", resolved_by="person"
    )
    pr.react(comment, "+1")
    before = datetime.now().astimezone()

    run = collected(pr, tmp_path, capsys)

    by_channel = {record["channel"]: record for record in run.records}
    assert len(run.records) == len(by_channel) == 2, run.records
    assert_record(by_channel["reaction"], channel="reaction", label="accept", before=before)
    assert_record(by_channel["resolution"], channel="resolution", label="resolved", before=before)


def test_p2_s8_ac6_no_username_or_body_reaches_dir(pr, tmp_path, capsys):
    first = pr.own(resolved_by="person")
    pr.react(first, "+1", PERSON_A)
    pr.react(first, "-1", PERSON_B)
    pr.react(first, "heart", RESOLVER)
    react_and_resolve(pr, pr.add(body=f"{OTHERS_BODY}\n\n{marker(FP2, RULE, ANCHOR2)}",
                                 author=PERSON_B, author_type="User"))  # fmt: skip

    run = collected(pr, tmp_path, capsys)

    assert run.records, "precondition: something was written"
    written = run.written_text()
    for canary in CANARIES:
        assert canary not in written, f"{canary!r} is in {run.out}"


# AC7: a failing GitHub call exits 18 and writes no feedback.jsonl.


def test_p2_s8_ac7_each_failing_github_call_exits_18_with_no_feedback_file(tmp_path, capsys):
    def seeded() -> ClosedPr:
        closed = ClosedPr()
        closed.react(closed.own(resolved_by="person"), "+1")
        return closed

    healthy = seeded()
    assert collect(tmp_path, capsys, healthy.client, name="healthy").exit_code == 0
    operations = sorted(set(healthy.call_names()))
    assert operations, "precondition: collect calls the client"

    for operation in operations:
        failing = seeded()
        failing.fail(operation)

        run = collect(tmp_path, capsys, failing.client, name=f"fail-{operation}")

        assert run.exit_code == GITHUB_ERROR_EXIT, (operation, run.stderr)
        assert run.stderr.strip(), operation
        assert not run.feedback.exists(), operation


# AC8: the real client reads reactions and resolvers, with S6's limits, page cap and errors.


@pytest.fixture
def stub() -> Iterator[GitHubStub]:
    with GitHubStub() as server:
        yield server


@pytest.fixture
def stub_env(stub: GitHubStub, monkeypatch: pytest.MonkeyPatch) -> GitHubStub:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_API_URL", stub.url)
    return stub


def stub_own(  # noqa: PLR0913 - keyword-only, one per fact a seeded comment has
    stub: GitHubStub,
    comment_id: int,
    fingerprint: str,
    anchor: str,
    *,
    resolved_by: tuple[str, str] | None = None,
    login: str = BOT_LOGIN,
    user_type: str = "Bot",
) -> None:
    body = own_body(fingerprint, anchor)
    comment = review_comment(
        comment_id, path=APP, line=3, body=body, login=login, user_type=user_type
    )
    stub.add_review_comment(comment, resolved_by=resolved_by)


def test_p2_s8_ac8_rest_client_reads_reactions_and_resolvers(stub_env, tmp_path, capsys):
    stub = stub_env
    stub_own(stub, 1001, FP1, ANCHOR1, resolved_by=(RESOLVER, "User"))
    stub.add_reaction(1001, "+1", PERSON_A)
    stub_own(stub, 1002, FP2, ANCHOR2, resolved_by=(GRAPHQL_BOT_LOGIN, "Bot"))
    stub.add_reaction(1002, "-1", PERSON_B)
    stub.add_reaction(1002, "+1", BOT_LOGIN, "Bot")
    stub.add_reaction(1002, "heart", PERSON_A)
    stub_own(stub, 1003, FP3, ANCHOR3, login=PERSON_A, user_type="User")
    stub.add_reaction(1003, "+1", PERSON_B)

    run = collect(tmp_path, capsys)

    assert run.exit_code == 0, run.stderr
    assert run.labels() == [
        ("reaction", "accept", FP1),
        ("reaction", "reject", FP2),
        ("resolution", "resolved", FP1),
    ]
    written = run.written_text()
    for canary in (*CANARIES, TOKEN):
        assert canary not in written, canary
    assert all(TOKEN in r.headers.get("authorization", "") for r in stub.requests)


def test_p2_s8_ac8_reactions_over_two_pages_are_all_read(stub_env, tmp_path, capsys):
    stub = stub_env
    stub_own(stub, 1001, FP1, ANCHOR1)
    for number in range(150):
        stub.add_reaction(1001, "+1", f"person-{number}")
    stub.add_reaction(1001, "-1", PERSON_B)  # on the second page of 100

    run = collect(tmp_path, capsys)

    assert run.exit_code == 0, run.stderr
    assert run.labels() == [("reaction", "conflict", FP1)]


def test_p2_s8_ac8_more_than_10_pages_of_reactions_exits_18_naming_the_limit(
    stub_env, tmp_path, capsys
):
    stub = stub_env
    stub_own(stub, 1001, FP1, ANCHOR1)
    stub.state.endless_reactions = True

    run = collect(tmp_path, capsys)

    assert run.exit_code == GITHUB_ERROR_EXIT, run.stderr
    assert "10" in run.stderr, run.stderr
    assert re.search(r"page|limit", run.stderr, re.IGNORECASE), run.stderr
    assert len(stub.routed("list_reactions")) <= 11
    assert not run.feedback.exists()


STATUS_IN_MESSAGE = {
    "401": "401", "404": "404", "500": "500", "rate_limited": "403", "429": "429",
    "not_json": None, "over_5_mb": None,
}  # fmt: skip


@pytest.mark.parametrize("route", ["list_reactions", "list_threads", "list_review_comments"])
@pytest.mark.parametrize("kind", STATUS_IN_MESSAGE)
def test_p2_s8_ac8_a_failed_read_exits_18_once_with_one_line_and_no_file(
    stub_env, tmp_path, capsys, route, kind
):
    stub = stub_env
    stub.state.echo_authorization = True
    stub_own(stub, 1001, FP1, ANCHOR1, resolved_by=(RESOLVER, "User"))
    stub.add_reaction(1001, "+1", PERSON_A)
    stub.fail(route, kind)

    run = collect(tmp_path, capsys)

    assert run.exit_code == GITHUB_ERROR_EXIT, run.stderr
    status = STATUS_IN_MESSAGE[kind]
    if status is not None:
        assert status in run.stderr, run.stderr
    assert len(stub.routed(route)) == 1, "not retried"
    assert TOKEN not in run.stderr
    assert not run.feedback.exists()


def test_p2_s8_ac8_no_token_exits_2_naming_it_and_sends_nothing(
    stub, tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("GITHUB_API_URL", stub.url)

    run = collect(tmp_path, capsys)

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert "GITHUB_TOKEN" in run.stderr
    assert stub.requests == []


def test_p2_s8_ac8_collect_makes_no_write_call(stub_env, tmp_path, capsys):
    stub = stub_env
    stub_own(stub, 1001, FP1, ANCHOR1, resolved_by=(RESOLVER, "User"))
    stub.add_reaction(1001, "+1", PERSON_A)

    run = collect(tmp_path, capsys)

    assert run.exit_code == 0, run.stderr
    assert {r.method for r in stub.requests} <= {"GET", "POST"}
    assert [r.route for r in stub.writes() if r.route != "list_threads"] == []


# AC9: the workflow's `closed` trigger and collect job.


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def collect_job(workflow: dict[Any, Any]) -> dict[str, Any]:
    return jobs_of(workflow)[collect_job_name(workflow)]


def collect_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    step = collect_step_in(collect_job(workflow))
    assert step is not None
    return step


def test_p2_s8_ac9_closed_is_added_to_the_pull_request_types(workflow):
    assert set(triggers_of(workflow)["pull_request"]["types"]) == SHADOW_ACTIONS | {"closed"}


@pytest.mark.parametrize("mode", ["", "shadow", "comment"])
def test_p2_s8_ac9_collect_runs_for_a_closed_same_repo_pr_unless_mode_is_off(workflow, mode):
    job = collect_job(workflow)
    assert job_runs(job, head_repo=REPO, mode=mode, action="closed")
    assert not job_runs(job, head_repo=REPO, mode="off", action="closed")


@pytest.mark.parametrize(
    "head_repo",
    ["someone/review-bot", "octo/review-bot-fork", None],
    ids=["fork", "similar-name", "deleted-fork"],
)
def test_p2_s8_ac9_collect_doesnt_run_for_a_fork(workflow, head_repo):
    assert not job_runs(collect_job(workflow), head_repo=head_repo, mode="", action="closed")


@pytest.mark.parametrize("action", sorted(SHADOW_ACTIONS))
def test_p2_s8_ac9_collect_doesnt_run_for_any_other_action(workflow, action):
    for mode in ("", "shadow", "comment"):
        assert not job_runs(collect_job(workflow), head_repo=REPO, mode=mode, action=action)


@pytest.mark.parametrize("mode", ["", "off", "shadow", "comment"])
def test_p2_s8_ac9_shadow_and_comment_jobs_dont_run_on_closed(workflow, mode):
    for review_mode in ("shadow", "comment"):
        job = review_job(workflow, review_mode)
        assert not job_runs(job, head_repo=REPO, mode=mode, action="closed"), review_mode


def test_p2_s8_ac9_collect_permissions_are_exactly_contents_and_pull_requests_read(workflow):
    assert collect_job(workflow).get("permissions") == {
        "contents": "read",
        "pull-requests": "read",
    }


CREDENTIAL = re.compile(r"secrets\.|github\.token|GITHUB_TOKEN|ANTHROPIC_API_KEY", re.IGNORECASE)


def test_p2_s8_ac9_only_the_collect_step_gets_the_token_and_no_other_secret(workflow):
    job = collect_job(workflow)
    step = collect_step(workflow)
    index = steps_of(job).index(step)
    holders = set()
    for path, node in walk(job):
        names = [str(key) for key in node] if isinstance(node, dict) else []
        texts = [node] if isinstance(node, str) else names
        if any(CREDENTIAL.search(item) for item in texts):
            holders.add(path)

    assert holders == {("steps", index, "env"), ("steps", index, "env", "GITHUB_TOKEN")}
    assert re.fullmatch(
        r"\$\{\{\s*(secrets\.GITHUB_TOKEN|github\.token)\s*\}\}", step["env"]["GITHUB_TOKEN"]
    )
    assert "env" not in job
    for checkout in steps_using(job, "actions/checkout"):
        assert checkout["with"]["persist-credentials"] is False


def test_p2_s8_ac9_collect_step_passes_the_event_and_out(workflow):
    argv = argv_of(collect_step(workflow))

    assert argv[0] == "collect"
    assert option_value(argv, "--event") == "$GITHUB_EVENT_PATH"
    assert same_dir(option_value(argv, "--out")) == OUT_DIR
    assert "--repo" not in argv
    assert "--provider" not in argv


def test_p2_s8_ac9_feedback_is_uploaded_for_90_days_named_by_pr_and_attempt(workflow):
    (upload,) = steps_using(collect_job(workflow), "actions/upload-artifact")

    paths = [same_dir(line) for line in str(upload["with"]["path"]).splitlines() if line.strip()]
    assert paths == [f"{OUT_DIR}/feedback.jsonl"]
    assert upload["with"]["retention-days"] == 90
    for part in ("github.event.pull_request.number", "github.run_attempt"):
        assert re.search(r"\$\{\{\s*" + re.escape(part) + r"\s*\}\}", upload["with"]["name"])
    shadow_upload = steps_using(review_job(workflow, "shadow"), "actions/upload-artifact")[0]
    assert upload["with"]["name"] != shadow_upload["with"]["name"]


def test_p2_s8_ac9_collect_has_its_own_concurrency_group_that_never_cancels(workflow):
    """A `closed` run neither cancels a review in progress nor is cancelled by a reopen."""
    assert "concurrency" not in workflow, "a workflow-level group would span every job"
    collect_concurrency = collect_job(workflow)["concurrency"]
    assert collect_concurrency["cancel-in-progress"] is False
    assert "github.event.pull_request.number" in collect_concurrency["group"]
    for review_mode in ("shadow", "comment"):
        review_concurrency = review_job(workflow, review_mode)["concurrency"]
        assert review_concurrency["cancel-in-progress"] is True, review_mode
        assert "github.event.pull_request.number" in review_concurrency["group"], review_mode
        assert review_concurrency["group"] != collect_concurrency["group"], review_mode


def test_p2_s8_ac9_collect_command_is_accepted_by_the_cli(workflow, tmp_path, monkeypatch, capsys):
    """The step's own argv, with a broken event, gets past parsing to the event (19)."""
    event_path = tmp_path / "event.json"
    event_path.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:9")  # never reached: the event fails
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    argv = [os.path.expandvars(word) for word in argv_of(collect_step(workflow))]

    exit_code = importlib.import_module("review_bot.pr.cli").main(argv)

    assert exit_code == EVENT_ERROR_EXIT, capsys.readouterr().err


# AC10: `label` reads collect's records, and ignores those whose channel isn't `shadow`.


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Reports:
    """P2-S4's PR: a clone whose head adds K1 to K3 to its `APP`."""
    monkeypatch.delenv("CI", raising=False)
    clone = GitRepo(tmp_path / "clone")
    base = clone.commit({LABEL_APP: "base = 0\n"}, "base")
    clone.branch("feature")
    first = clone.commit({LABEL_APP: HEAD}, "head")
    second = clone.commit({LABEL_APP: SHIFTED_HEAD}, "push")
    clone.checkout("main")
    events = tmp_path / "events"
    events.mkdir()
    return Reports(tmp_path / "reports", clone, base, {"first": first, "second": second}, events)


def test_p2_s8_ac10_label_reads_collected_records_and_still_asks_about_the_same_comment(
    reports, tmp_path, capsys, monkeypatch
):
    """The reaction and resolution records name K1's file, rule part, anchor and fingerprint;
    a `label` that counted them as judgements would not ask about K1."""
    k1 = load(reports.run(keys("K1", tag="r1"), "one"))["comments"][0]
    closed = ClosedPr()
    comment = closed.own(k1["fingerprint"], k1["anchor"], path=LABEL_APP, line=k1["line"],
                         resolved_by="person")  # fmt: skip
    closed.react(comment, "-1")
    run = collect(tmp_path, capsys, closed.client)
    assert run.exit_code == 0, run.stderr
    assert {r["channel"] for r in run.records} == {"reaction", "resolution"}
    labeller = Labeller(monkeypatch, reports.root, run.feedback)

    session = labeller.label(["a"])

    assert session.exit_code == 0, session.stderr
    assert session.stdin_reads == 1, session.segments
    assert session.asked_about("K1r1"), session.segments
    channels = [record["channel"] for record in labeller.records()]
    assert sorted(channels) == ["reaction", "resolution", "shadow"]


# AC11: comments sharing a fingerprint combine into one reaction record.


# (reaction on the old thread, reaction on the moved comment) -> label
SHARED = {
    ("+1", "-1"): "conflict",
    ("+1", "+1"): "accept",
    ("-1", "-1"): "reject",
    ("heart", "+1"): "accept",
}


@pytest.mark.parametrize("reactions", SHARED, ids="/".join)
def test_p2_s8_ac11_a_moved_comment_and_its_old_thread_give_one_reaction_record(
    pr, tmp_path, capsys, reactions
):
    old = pr.own(line=3, resolved_by="bot")  # the bot resolved the old thread when it moved
    new = pr.own(line=9)
    pr.react(old, reactions[0], PERSON_A)
    pr.react(new, reactions[1], PERSON_B)
    label = SHARED[reactions]

    run = collected(pr, tmp_path, capsys)

    assert run.labels() == [("reaction", label, FP1)]


def test_p2_s8_ac11_different_fingerprints_stay_separate(pr, tmp_path, capsys):
    pr.react(pr.own(FP1, ANCHOR1), "+1")
    pr.react(pr.own(FP2, ANCHOR1, line=9), "-1")

    run = collected(pr, tmp_path, capsys)

    assert run.labels() == [("reaction", "accept", FP1), ("reaction", "reject", FP2)]


# Limits: many comments and reactions, read in bounded time.


def test_p2_s8_limits_many_comments_and_reactions_are_reduced_quickly(pr, tmp_path, capsys):
    for number in range(2_000):
        comment = pr.own(f"{number:016x}", ANCHOR1, line=number + 1)
        for person in range(5):
            pr.react(comment, "+1" if person % 2 == 0 else "heart", f"person-{person}")
    pr.react(1, "-1", PERSON_B)

    started = time.monotonic()
    run = collected(pr, tmp_path, capsys)
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    assert len(run.records) == 2_000
    assert ("reaction", "conflict", f"{0:016x}") in run.labels()
