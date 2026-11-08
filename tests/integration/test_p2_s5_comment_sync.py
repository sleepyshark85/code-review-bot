r"""P2-S5: create, update, move and resolve the bot's own comments, against an in-memory GitHub.

Written from the spec's criteria, design.md (Comment state, Interfaces, Commands and exit codes)
and ADR 0009 while the developer built the slice, before any of its code was read.

Every run calls `review_bot.pr.cli.main(["run", "--event", PATH, "--repo", DIR, "--out", OUT,
"--provider", "mock", "--mode", "comment"], provider=P, github=FAKE)` in process. `FAKE` is the
developer's `review_bot.pr.fake_github.FakeGitHubClient`, reached only through `GitHub` below,
the one place to change if its seam differs. Nothing reaches a model, the network or GitHub.

Each pull request is a real git repository in `tmp_path`: a base commit on `main`, then one
commit per push on `feature`. One `FakeGitHubClient` lives for the whole PR, so a test is a
sequence of runs, as the PR would see them. Between pushes the test plays GitHub's part, which
no fake can know: it says where GitHub now shows each comment (`show_at`), or that GitHub marks
it outdated (`outdate`), and when a person resolves a thread.

What a run did is read from the calls the fake recorded during it, from the fake's comments,
and from `shadow.json`'s `comments` entries (`action`, `applied`, `fingerprint`, `file`, `line`).
"""

import importlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_p1_s9_dedupe import expected_fingerprint
from test_p2_s1_shadow_report_local import anchor_of, load_shadow, marker
from test_p2_s2_review_pr_event import event, set_in

from review_bot.diff_parser import parse_diff
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding, ReviewedFinding, Source
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

BOT_LOGIN = "github-actions[bot]"
GITHUB_ERROR_EXIT = 18
TRANSIENT_EXIT = 11
DEFAULT_MAX_OPEN_COMMENTS = 10
WRITES = ("post_review", "update_comment", "resolve_thread", "write_summary")
AI_LOGIC = "ai:logic"
# Generous: it catches a hang or quadratic work, not a slow CI machine.
BOUND_S = 60.0


@pytest.fixture(autouse=True)
def outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub Actions sets `CI`; AC11's `no_credential` case sets it on purpose."""
    monkeypatch.delenv("CI", raising=False)


# The FakeGitHubClient seam: the only code that knows the fake's own names.


@dataclass(frozen=True, slots=True)
class SeenComment:
    """One inline comment as the fake holds it."""

    comment_id: int
    thread_id: str
    author: str
    path: str
    line: int | None  # None while GitHub shows it outdated
    body: str
    resolved_by: str | None  # None (open), "bot" or "person"


@dataclass(frozen=True, slots=True)
class Call:
    name: str
    args: tuple[Any, ...]


class GitHub:
    """The developer's `FakeGitHubClient`, seeded and read the way these tests need."""

    def __init__(self) -> None:
        module = importlib.import_module("review_bot.pr.fake_github")
        self.client = module.FakeGitHubClient()

    # Seeding a comment that is already on the pull request.

    def seed(  # noqa: PLR0913 - keyword-only, one per GitHub fact a seeded comment has
        self,
        *,
        path: str,
        line: int,
        body: str,
        author: str = BOT_LOGIN,
        author_type: str | None = None,
        outdated: bool = False,
        resolved_by: str | None = None,
    ) -> int:
        if author_type is None:
            author_type = "Bot" if author.endswith("[bot]") else "User"
        comment_id: int = self.client.add_comment(
            path=path, line=line, body=body, author=author, author_type=author_type
        )
        if outdated:
            self.outdate(comment_id)
        if resolved_by is not None:
            self.client.resolve(comment_id, by=resolved_by)
        return comment_id

    # GitHub's part between pushes.

    def outdate(self, comment_id: int) -> None:
        self.client.mark_outdated(comment_id)

    def show_at(self, comment_id: int, line: int) -> None:
        """GitHub follows the unchanged line to its new number, not outdated."""
        self.client.set_line(comment_id, line)

    def person_resolves(self, comment_id: int) -> None:
        self.client.resolve(comment_id, by="person")

    def fail(self, operation: str) -> None:
        """The next call of `operation` (a `GitHubClient` method name) fails."""
        self.client.fail_on(operation)

    # Reading what is there and what was called.

    def calls(self) -> list[Call]:
        return [Call(call.name, tuple(call.args)) for call in self.client.calls]

    def comments(self) -> list[SeenComment]:
        return [
            SeenComment(
                comment_id=c.comment_id,
                thread_id=c.thread_id,
                author=c.author,
                path=c.path,
                line=c.line,
                body=c.body,
                resolved_by=None if c.resolved_by is None else str(c.resolved_by),
            )
            for c in self.client.comments
        ]

    def summary(self) -> str | None:
        summary: str | None = self.client.summary
        return summary

    # Reading the arguments of each recorded write.

    @staticmethod
    def review_of(call: Call) -> tuple[str, list[tuple[str, int, str, str]]]:
        """`post_review(pr, comments)`: the head SHA and each (path, line, side, body).

        `NewComment` has no side field: its contract is the new side, which GitHub calls RIGHT.
        """
        pr, comments = call.args
        assert all(type(c).__name__ == "NewComment" for c in comments), comments
        return pr.head_sha, [(c.path, c.line, "RIGHT", c.body) for c in comments]

    @staticmethod
    def updated_id(call: Call) -> int:
        comment_id: int = call.args[0]
        return comment_id

    @staticmethod
    def updated_body(call: Call) -> str:
        body: str = call.args[1]
        return body

    @staticmethod
    def resolved_thread(call: Call) -> str:
        thread_id: str = call.args[0]
        return thread_id


def plan_for(
    findings: Sequence[ReviewedFinding], existing: Sequence[Any], diff_text: str
) -> list[tuple[str, str]]:
    """`plan_comments` from design.md's Interfaces, as (action, fingerprint) pairs.

    Planned comments come first, then resolved comments as ("resolve", fingerprint), then
    withheld findings as ("withheld:<reason>", fingerprint).
    """
    plan_module = importlib.import_module("review_bot.pr.plan")
    plan = plan_module.plan_comments(
        findings,
        existing,
        pr_diff=parse_diff(diff_text),
        max_open_comments=DEFAULT_MAX_OPEN_COMMENTS,
    )
    planned = [(str(c.action), c.reviewed.fingerprint) for c in plan.comments]
    resolved = [("resolve", c.fingerprint) for c in plan.resolved]
    withheld = [(f"withheld:{w.reason}", w.reviewed.fingerprint) for w in plan.withheld]
    return planned + resolved + withheld


def bot_comment(**fields: Any) -> Any:
    """design.md's `BotComment`, with `resolved_by` None (open) unless given."""
    github_module = importlib.import_module("review_bot.pr.github")
    return github_module.BotComment(**({"resolved_by": None} | fields))


# A pull request with several pushes.


class PullRequestRepo:
    def __init__(self, path: Path, base_files: dict[str, str]) -> None:
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q", "--initial-branch=main")
        for key, value in {
            "user.name": "Tester",
            "user.email": "tester@example.invalid",
            "commit.gpgsign": "false",
            "core.autocrlf": "false",
        }.items():
            self.git("config", key, value)
        self.base = self.commit(base_files, "base")
        self.git("checkout", "-q", "-b", "feature")

    def git(self, *args: str) -> str:
        result = subprocess.run(  # noqa: S603 - fixed git arguments, test-owned repository
            ["git", "-C", str(self.path), *args],  # noqa: S607 - git from PATH, as the bot uses
            capture_output=True,
            check=True,
        )
        return result.stdout.decode("utf-8", "replace")

    def commit(
        self,
        files: dict[str, str | None],
        message: str = "push",
        renames: dict[str, str] | None = None,
    ) -> str:
        for old, new in (renames or {}).items():
            (self.path / new).parent.mkdir(parents=True, exist_ok=True)
            self.git("mv", old, new)
        for name, content in files.items():
            target = self.path / name
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def pr_diff(self, head: str) -> str:
        return self.git("diff", "--no-ext-diff", "--no-textconv", "-M", f"{self.base}...{head}")


@dataclass(frozen=True, slots=True)
class Run:
    exit_code: int
    stderr: str
    out: Path
    calls: list[Call]

    @property
    def shadow(self) -> dict[str, Any]:
        return load_shadow(self.out)

    @property
    def writes(self) -> list[str]:
        return [call.name for call in self.calls if call.name in WRITES]

    def calls_named(self, name: str) -> list[Call]:
        return [call for call in self.calls if call.name == name]

    @property
    def reviews(self) -> list[tuple[str, list[tuple[str, int, str, str]]]]:
        return [GitHub.review_of(call) for call in self.calls_named("post_review")]

    @property
    def posted(self) -> list[tuple[str, int, str, str]]:
        """Every comment posted in this run's reviews."""
        return [comment for _, comments in self.reviews for comment in comments]

    @property
    def resolved_threads(self) -> list[str]:
        return [GitHub.resolved_thread(call) for call in self.calls_named("resolve_thread")]

    @property
    def updated_ids(self) -> list[int]:
        return [GitHub.updated_id(call) for call in self.calls_named("update_comment")]

    @property
    def entries(self) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = self.shadow["comments"]
        return comments

    def entry(self, action: str) -> dict[str, Any]:
        (found,) = [e for e in self.entries if e["action"] == action]
        return found


class Session:
    """One pull request: its repository, its fake GitHub, and its runs in order."""

    def __init__(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        base_files: dict[str, str] | None = None,
    ) -> None:
        self._tmp_path = tmp_path
        self._capsys = capsys
        self.repo = PullRequestRepo(tmp_path / "clone", base_files or {BASE_FILE: BASE_TEXT})
        self.github = GitHub()
        self.head = self.repo.base
        self._runs = 0

    def push(self, files: dict[str, str | None], *, renames: dict[str, str] | None = None) -> str:
        self.head = self.repo.commit(files, renames=renames)
        return self.head

    def payload(self, **changes: Any) -> dict[str, Any]:
        return event(self.repo.base, self.head, **({"action": "synchronize"} | changes))

    def run(  # noqa: PLR0913 - keyword-only knobs, each defaulting to an ordinary comment run
        self,
        findings: Sequence[Finding] = (),
        *,
        mode: str = "comment",
        payload: dict[str, Any] | None = None,
        provider: Any = None,
        provider_name: str = "mock",
        with_client: bool | None = None,
    ) -> Run:
        """One `run`; the client is injected in comment mode unless `with_client` says not."""
        pr_cli = importlib.import_module("review_bot.pr.cli")
        self._runs += 1
        event_path = self._tmp_path / f"event-{self._runs}.json"
        event_path.write_text(json.dumps(payload or self.payload()), encoding="utf-8")
        out = self._tmp_path / f"out-{self._runs}"
        if provider is None:
            provider = MockProvider(findings=list(findings))
        inject = mode == "comment" if with_client is None else with_client
        extra = {"github": self.github.client} if inject else {}
        calls_before = len(self.github.calls())
        self._capsys.readouterr()
        exit_code = pr_cli.main(
            [
                "run", "--event", str(event_path), "--repo", str(self.repo.path),
                "--out", str(out), "--provider", provider_name, "--mode", mode,
            ],
            provider=provider,
            **extra,
        )  # fmt: skip
        captured = self._capsys.readouterr()
        return Run(exit_code, captured.err, out, self.github.calls()[calls_before:])

    # Reading the fake's comments.

    def comment_at(self, path: str, line: int, *, resolved_by: str | None = None) -> SeenComment:
        (found,) = [
            c
            for c in self.github.comments()
            if (c.path, c.line, c.resolved_by) == (path, line, resolved_by)
        ]
        return found

    def comment(self, comment_id: int) -> SeenComment:
        (found,) = [c for c in self.github.comments() if c.comment_id == comment_id]
        return found

    def open_comments(self) -> list[SeenComment]:
        return [c for c in self.github.comments() if c.resolved_by is None]


@pytest.fixture
def session(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Session:
    return Session(tmp_path, capsys)


# Files and findings.

BASE_FILE = "src/base.py"
BASE_TEXT = "base = 1\n"
OTHER_FILE = "src/other.py"
APP = "src/app.py"  # new in the PR, so the whole file is one added hunk
EVAL = "    value = eval(data)"
WRITE = "    write(path, repr(value))"
APP_V1 = [
    "def load(path):",
    "    data = read(path)",
    EVAL,  # line 3
    "    return value",
    "def save(path, value):",
    WRITE,  # line 6
]
EVAL_LINE, WRITE_LINE = 3, 6


def text(lines: Sequence[str]) -> str:
    return "".join(f"{line}\n" for line in lines)


def replaced(lines: Sequence[str], line: int, new: str | None) -> list[str]:
    """`lines` with 1-based `line` replaced by `new`, or deleted when `new` is None."""
    result = list(lines)
    if new is None:
        del result[line - 1]
    else:
        result[line - 1] = new
    return result


def inserted(lines: Sequence[str], line: int, new: str) -> list[str]:
    """`lines` with `new` inserted so that it becomes 1-based `line`."""
    result = list(lines)
    result.insert(line - 1, new)
    return result


def ai(line: int, severity: str = "high", file: str = APP, rationale: str = "R.") -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,
        category="logic",
        rationale=rationale,
        confidence=0.9,
    )


def fingerprint_of(line_text: str, ordinal: int = 0, file: str = APP) -> str:
    return expected_fingerprint(file, AI_LOGIC, line_text, ordinal)


def marker_of(body: str) -> dict[str, str]:
    """The fields of a body's last-line marker (design.md, Markers)."""
    last = body.splitlines()[-1]
    match = re.fullmatch(
        r"<!-- review-bot:v1 fingerprint=(?P<fingerprint>[0-9a-f]{16}) "
        r"rule=(?P<rule>\S+) anchor=(?P<anchor>[0-9a-f]{16}) -->",
        last,
    )
    assert match is not None, f"no marker on the last line: {last!r}"
    return match.groupdict()


def bot_body(
    *, fingerprint: str, anchor_text: str, severity: str = "high", rule: str = AI_LOGIC
) -> str:
    """A body as S1 renders it: the severity first, the marker on the last line."""
    return (
        f"**{severity}** logic (source ai, confidence 0.90)\n\nR.\n\n"
        "Is this right and worth acting on? React with a thumbs-up if so, or a thumbs-down if "
        f"not.\n\n{marker(fingerprint, rule, anchor_of(anchor_text))}"
    )


def posted_app(session: Session, findings: Sequence[Finding], lines: Sequence[str]) -> Run:
    """Push `lines` as APP, run with `findings`, and check every finding was created."""
    session.push({APP: text(lines)})
    run = session.run(findings)
    assert run.exit_code == 0, run.stderr
    assert len(run.posted) == len(findings), run.calls
    return run


# AC1: no own comments and 2 findings: one review, 2 comments, a summary, both `create` `done`.


def test_p2_s5_ac1_first_run_posts_one_review_on_the_head_with_both_comments(session):
    session.push({APP: text(APP_V1)})
    findings = [ai(EVAL_LINE), ai(WRITE_LINE)]

    run = session.run(findings)

    assert run.exit_code == 0, run.stderr
    assert len(run.reviews) == 1, run.calls
    head_sha, posted = run.reviews[0]
    assert head_sha == session.head
    assert sorted((path, line, side) for path, line, side, _ in posted) == [
        (APP, EVAL_LINE, "RIGHT"),
        (APP, WRITE_LINE, "RIGHT"),
    ]
    assert Counter(run.writes) == Counter({"post_review": 1, "write_summary": 1})
    assert session.github.summary()
    assert sorted((e["action"], e["applied"], e["line"]) for e in run.entries) == [
        ("create", "done", EVAL_LINE),
        ("create", "done", WRITE_LINE),
    ]


def test_p2_s5_ac1_posted_bodies_are_s1s_bodies_with_their_markers(session):
    """S1's body for the same finding is the shadow report's `body`, from a run with no client."""
    session.push({APP: text(APP_V1)})
    findings = [ai(EVAL_LINE), ai(WRITE_LINE)]
    shadow_run = session.run(findings, mode="shadow")
    s1_bodies = {e["line"]: e["body"] for e in shadow_run.shadow["comments"]}

    run = session.run(findings)

    assert run.exit_code == 0, run.stderr
    assert {line: body for _, line, _, body in run.posted} == s1_bodies
    posted_markers = {line: marker_of(body) for _, line, _, body in run.posted}
    assert posted_markers[EVAL_LINE] == {
        "fingerprint": fingerprint_of(EVAL),
        "rule": AI_LOGIC,
        "anchor": anchor_of(EVAL),
    }


# AC2: the same findings again post nothing; a severity change edits only that comment.


def test_p2_s5_ac2_same_findings_again_post_nothing_and_edit_the_summary_in_place(session):
    findings = [ai(EVAL_LINE), ai(WRITE_LINE)]
    posted_app(session, findings, APP_V1)
    session.push({OTHER_FILE: "unrelated = 1\n"})

    run = session.run(findings)

    assert run.exit_code == 0, run.stderr
    assert run.writes == ["write_summary"]
    assert sorted((e["action"], e["line"]) for e in run.entries) == [
        ("keep", EVAL_LINE),
        ("keep", WRITE_LINE),
    ]
    assert len(session.open_comments()) == 2
    assert session.github.summary()


def test_p2_s5_ac2_severity_change_edits_only_that_comment_and_keeps_its_fingerprint(session):
    posted_app(session, [ai(EVAL_LINE), ai(WRITE_LINE)], APP_V1)
    eval_comment = session.comment_at(APP, EVAL_LINE)

    run = session.run([ai(EVAL_LINE, "medium"), ai(WRITE_LINE)])

    assert run.exit_code == 0, run.stderr
    assert Counter(run.writes) == Counter({"update_comment": 1, "write_summary": 1})
    assert run.updated_ids == [eval_comment.comment_id]
    new_body = GitHub.updated_body(run.calls_named("update_comment")[0])
    assert new_body.startswith("**medium**"), new_body
    assert marker_of(new_body) == marker_of(eval_comment.body)
    assert sorted((e["line"], e["action"]) for e in run.entries) == [
        (EVAL_LINE, "update"),
        (WRITE_LINE, "keep"),
    ]
    assert run.entry("update")["applied"] == "done"


def test_p2_s5_ac2_after_the_update_the_same_severity_again_posts_nothing(session):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    first = session.run([ai(EVAL_LINE, "medium")])
    assert first.writes.count("update_comment") == 1

    run = session.run([ai(EVAL_LINE, "medium")])

    assert run.exit_code == 0, run.stderr
    assert run.writes == ["write_summary"]
    assert [e["action"] for e in run.entries] == ["keep"]


# AC3: pairing order.


def test_p2_s5_ac3_identical_line_added_above_keeps_the_comment_and_creates_only_the_new_one(
    session,
):
    """Line 3 moves to 4 under an identical new line 2: the new line takes ordinal 0."""
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    original = session.comment_at(APP, EVAL_LINE)
    session.push({APP: text(inserted(APP_V1, 2, EVAL))})
    session.github.show_at(original.comment_id, 4)

    run = session.run([ai(2), ai(4)])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == []
    assert run.updated_ids == []
    assert [(path, line) for path, line, _, _ in run.posted] == [(APP, 2)]
    kept = run.entry("keep")
    assert (kept["line"], kept["fingerprint"]) == (4, marker_of(original.body)["fingerprint"])
    assert run.entry("create")["line"] == 2
    # Two open comments sharing one fingerprint would be duplicates (AC8) on the next run.
    (_, _, _, new_body) = run.posted[0]
    assert marker_of(new_body)["fingerprint"] != marker_of(original.body)["fingerprint"]


def test_p2_s5_ac3_identical_lines_settle_and_a_third_run_posts_nothing(session):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    original = session.comment_at(APP, EVAL_LINE)
    session.push({APP: text(inserted(APP_V1, 2, EVAL))})
    session.github.show_at(original.comment_id, 4)
    assert session.run([ai(2), ai(4)]).exit_code == 0

    run = session.run([ai(2), ai(4)])

    assert run.exit_code == 0, run.stderr
    assert run.writes == ["write_summary"]
    assert sorted((e["action"], e["line"]) for e in run.entries) == [("keep", 2), ("keep", 4)]
    assert len(session.open_comments()) == 2


def seed_on_eval(session: Session, fingerprint: str, severity: str, **options: Any) -> int:
    return session.github.seed(
        path=APP,
        line=options.pop("line", EVAL_LINE),
        body=bot_body(fingerprint=fingerprint, anchor_text=EVAL, severity=severity),
        **options,
    )


# Fingerprints no current finding has, as if posted under other ordinals: pairing is by key.
OTHER_FP_1 = "a1a1a1a1a1a1a1a1"
OTHER_FP_2 = "b2b2b2b2b2b2b2b2"


def test_p2_s5_ac3_same_key_same_line_pairs_the_candidate_with_the_findings_severity(session):
    session.push({APP: text(APP_V1)})
    seed_on_eval(session, OTHER_FP_1, "medium")  # older
    seed_on_eval(session, OTHER_FP_2, "high")  # newer, but the finding's severity

    run = session.run([ai(EVAL_LINE, "high")])

    assert run.exit_code == 0, run.stderr
    assert run.writes == ["write_summary"], "pairing the medium one would update it"
    assert run.entry("keep")["fingerprint"] == OTHER_FP_2


def test_p2_s5_ac3_nearer_candidate_wins_over_an_equal_severity_one_further_away(session):
    session.push({APP: text([*APP_V1, "def again(data):", EVAL])})  # EVAL on lines 3 and 8
    seed_on_eval(session, OTHER_FP_1, "high", line=8)  # older, equal severity, 5 lines away
    near = seed_on_eval(session, OTHER_FP_2, "medium")  # on the finding's line

    run = session.run([ai(EVAL_LINE, "high")])

    assert run.exit_code == 0, run.stderr
    assert run.updated_ids == [near]
    assert run.posted == []
    assert run.resolved_threads == []


def test_p2_s5_ac3_tie_goes_to_the_open_thread_over_a_person_resolved_one(session):
    session.push({APP: text(APP_V1)})
    seed_on_eval(session, OTHER_FP_1, "high", resolved_by="person")  # older
    open_one = seed_on_eval(session, OTHER_FP_2, "high")

    run = session.run([ai(EVAL_LINE, "medium")])  # equal to neither, so severity can't decide

    assert run.exit_code == 0, run.stderr
    assert run.updated_ids == [open_one]
    assert run.posted == []


def test_p2_s5_ac3_remaining_tie_goes_to_the_oldest_comment_id(session):
    session.push({APP: text(APP_V1)})
    older = seed_on_eval(session, OTHER_FP_2, "high")
    newer = seed_on_eval(session, OTHER_FP_1, "high")
    assert older < newer

    run = session.run([ai(EVAL_LINE, "medium")])

    assert run.exit_code == 0, run.stderr
    assert run.updated_ids == [older]
    assert run.posted == []


def test_p2_s5_ac3_bot_resolved_thread_with_the_same_fingerprint_is_never_a_candidate(session):
    session.push({APP: text(APP_V1)})
    seed_on_eval(session, fingerprint_of(EVAL), "high", resolved_by="bot")

    run = session.run([ai(EVAL_LINE)])

    assert run.exit_code == 0, run.stderr
    assert [(path, line) for path, line, _, _ in run.posted] == [(APP, EVAL_LINE)]
    assert run.entry("create")["applied"] == "done"
    assert run.updated_ids == []


# AC4: an unpaired open comment is resolved only when its own line is gone.


@pytest.mark.parametrize(
    "new_text", [None, "    value = safe_eval(data)"], ids=["deleted", "edited"]
)
def test_p2_s5_ac4_comment_whose_own_line_is_deleted_or_edited_is_resolved(session, new_text):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    comment = session.comment_at(APP, EVAL_LINE)
    session.push({APP: text(replaced(APP_V1, EVAL_LINE, new_text))})
    session.github.outdate(comment.comment_id)

    run = session.run([])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == [comment.thread_id]
    assert run.posted == []
    assert run.entry("resolve")["applied"] == "done"


def test_p2_s5_ac4_edited_line_github_still_shows_current_is_resolved(session):
    """The outdated flag is only a hint: a current position without the anchor is gone."""
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    comment = session.comment_at(APP, EVAL_LINE)
    session.push({APP: text(replaced(APP_V1, EVAL_LINE, "    value = safe_eval(data)"))})

    run = session.run([])  # GitHub left it at line 3, not outdated

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == [comment.thread_id]
    assert run.posted == []


@pytest.mark.parametrize("github_view", ["followed", "outdated"])
@pytest.mark.parametrize(
    "change",
    ["edit_line_above", "insert_lines_above", "edit_line_below"],
)
def test_p2_s5_ac4_only_other_lines_changed_in_a_new_file_keeps_it(session, github_view, change):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    comment = session.comment_at(APP, EVAL_LINE)
    new_lines, new_line = {
        "edit_line_above": (replaced(APP_V1, 2, "    data = read_text(path)"), EVAL_LINE),
        "insert_lines_above": (inserted(inserted(APP_V1, 1, "import os"), 1, "import re"), 5),
        "edit_line_below": (replaced(APP_V1, 4, "    return value or None"), EVAL_LINE),
    }[change]
    session.push({APP: text(new_lines)})
    if github_view == "outdated":
        session.github.outdate(comment.comment_id)
    else:
        session.github.show_at(comment.comment_id, new_line)

    run = session.run([])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == []
    assert run.posted == []
    assert run.writes == ["write_summary"]


MODULE = "src/module.py"
MODULE_BASE = [f"def step_{n}():\n    return {n}" for n in range(1, 7)]


def test_p2_s5_ac4_only_other_lines_changed_in_an_existing_file_keeps_it(tmp_path, capsys):
    session = Session(tmp_path, capsys, base_files={MODULE: text(MODULE_BASE)})
    with_eval = inserted(text(MODULE_BASE).splitlines(), 3, EVAL)  # line 3, inside step_1's hunk
    session.push({MODULE: text(with_eval)})
    assert len(session.run([ai(3, file=MODULE)]).posted) == 1
    comment = session.comment_at(MODULE, 3)
    session.push({MODULE: text(replaced(with_eval, 4, "def step_two():"))})
    session.github.outdate(comment.comment_id)

    run = session.run([])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == []
    assert run.writes == ["write_summary"]


RISKY = "src/risky.py"
RISKY_V1 = ["def risky():", "    try:", "        run()", "    except Exception:", "        pass"]
RISKY_FIXED = replaced(RISKY_V1, 5, '        log.exception("run failed")')


@pytest.mark.parametrize("github_view", ["followed", "outdated"])
def test_p2_s5_ac4_rule_issue_fixed_below_an_unchanged_anchored_line_is_kept(session, github_view):
    """`swallowed-exception` anchors on `except Exception:`; replacing `pass` doesn't touch it."""
    session.push({RISKY: text(RISKY_V1)})
    first = session.run([])
    assert [(path, line) for path, line, _, _ in first.posted] == [(RISKY, 4)], first.calls
    comment = session.comment_at(RISKY, 4)
    assert marker_of(comment.body)["rule"] == "swallowed-exception"
    session.push({RISKY: text(RISKY_FIXED)})
    if github_view == "outdated":
        session.github.outdate(comment.comment_id)

    run = session.run([])

    assert run.exit_code == 0, run.stderr
    assert run.shadow["report"] is not None
    assert run.resolved_threads == []
    assert run.writes == ["write_summary"]


# AC5: moving an outdated comment to its pairing finding's line.

NEIGHBOUR_EDIT = replaced(APP_V1, 2, "    data = read_text(path)")


def test_p2_s5_ac5_outdated_paired_comment_is_resolved_and_reposted_on_the_findings_line(
    session,
):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    old = session.comment_at(APP, EVAL_LINE)
    session.push({APP: text(NEIGHBOUR_EDIT)})
    session.github.outdate(old.comment_id)

    run = session.run([ai(EVAL_LINE)])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == [old.thread_id]
    assert run.posted == [(APP, EVAL_LINE, "RIGHT", old.body)]
    assert len(run.reviews) == 1
    moved = run.entry("move")
    assert (moved["fingerprint"], moved["line"], moved["applied"]) == (
        marker_of(old.body)["fingerprint"],
        EVAL_LINE,
        "done",
    )


def test_p2_s5_ac5_moved_comment_takes_the_reworded_rationale_and_keeps_its_fingerprint(
    session,
):
    """A move re-renders the body from the pairing finding; the marker keeps the posted one."""
    posted_app(session, [ai(EVAL_LINE, rationale="Old wording here")], APP_V1)
    old = session.comment_at(APP, EVAL_LINE)
    session.push({APP: text(NEIGHBOUR_EDIT)})
    session.github.outdate(old.comment_id)

    run = session.run([ai(EVAL_LINE, rationale="New wording here")])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == [old.thread_id]
    ((path, line, _, body),) = run.posted
    assert (path, line) == (APP, EVAL_LINE)
    assert "New wording here" in body
    assert "Old wording here" not in body
    assert marker_of(body) == marker_of(old.body)
    assert run.entry("move")["fingerprint"] == marker_of(old.body)["fingerprint"]


def test_p2_s5_ac5_move_goes_to_the_findings_line_when_the_anchor_is_on_several(session):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    old = session.comment_at(APP, EVAL_LINE)
    session.push({APP: text([*NEIGHBOUR_EDIT, "def again(data):", EVAL])})  # EVAL on 3 and 8
    session.github.outdate(old.comment_id)

    run = session.run([ai(8)])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == [old.thread_id]
    assert run.posted == [(APP, 8, "RIGHT", old.body)]


def test_p2_s5_ac5_the_old_bot_resolved_thread_cant_pair_again(session):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    old = session.comment_at(APP, EVAL_LINE)
    session.push({APP: text(NEIGHBOUR_EDIT)})
    session.github.outdate(old.comment_id)
    assert session.run([ai(EVAL_LINE)]).exit_code == 0
    assert session.comment(old.comment_id).resolved_by == "bot"

    run = session.run([ai(EVAL_LINE)])

    assert run.exit_code == 0, run.stderr
    assert run.writes == ["write_summary"]
    assert [e["action"] for e in run.entries] == ["keep"]
    (moved,) = session.open_comments()
    assert moved.comment_id != old.comment_id


def test_p2_s5_ac5_move_to_a_line_not_commentable_resolves_nothing_and_posts_nothing():
    """Through `plan_comments`: in S5 the reviewed diff is the PR diff, so verify would drop
    such a finding before the plan; only S7's incremental diff can reach this end to end."""
    fingerprint = fingerprint_of(EVAL)
    finding = ReviewedFinding(finding=ai(9), source=Source.AI, rule=None, fingerprint=fingerprint)
    outdated = bot_comment(
        comment_id=1,
        thread_id="T1",
        fingerprint=fingerprint,
        rule_part=AI_LOGIC,
        anchor=anchor_of(EVAL),
        path=APP,
        line=None,
        severity=finding.finding.severity,
    )
    # APP's PR diff holds lines 1 to 3 only: line 9 can't be commented on.
    diff = (
        "diff --git a/src/app.py b/src/app.py\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/src/app.py\n@@ -0,0 +1,3 @@\n+{APP_V1[0]}\n+{APP_V1[1]}\n+{EVAL}\n"
    )

    planned = plan_for([finding], [outdated], diff)

    assert planned == [("withheld:not_commentable", fingerprint)]


@pytest.mark.parametrize(
    ("new_lines", "resolved"),
    [
        (replaced(APP_V1, EVAL_LINE, "    value = safe_eval(data)"), True),
        ([*replaced(APP_V1, EVAL_LINE, "    value = 0"), EVAL], False),
    ],
    ids=["anchor_on_no_line", "anchor_moved_to_another_line"],
)
def test_p2_s5_ac5_outdated_comment_with_no_pairing_finding(session, new_lines, resolved):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    comment = session.comment_at(APP, EVAL_LINE)
    session.push({APP: text(new_lines)})
    session.github.outdate(comment.comment_id)

    run = session.run([])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == ([comment.thread_id] if resolved else [])
    assert run.posted == []


def test_p2_s5_ac5_anchor_only_in_another_file_doesnt_keep_an_outdated_comment(session):
    """Gone means on no line of *its file's* PR diff."""
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    comment = session.comment_at(APP, EVAL_LINE)
    session.push(
        {
            APP: text(replaced(APP_V1, EVAL_LINE, "    value = safe_eval(data)")),
            OTHER_FILE: text(["def elsewhere(data):", EVAL]),
        }
    )
    session.github.outdate(comment.comment_id)

    run = session.run([])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == [comment.thread_id]


# AC6: a renamed file: outdated comments move to the new path when found again there.

OLD_PATH = "src/old_name.py"
NEW_PATH = "src/new_name.py"
RENAME_BASE = [f"def helper_{n}(data):\n    return {n}" for n in range(1, 6)]
FIRST, SECOND, THIRD = "    first = eval(data)", "    second = eval(data)", "    third = eval(data)"
# Lines 2, 5 and 8 are the PR's own, inside base helpers 1, 2 and 3.
RENAME_LINES = (2, 5, 8)
RENAME_HEAD = inserted(
    inserted(inserted(text(RENAME_BASE).splitlines(), 2, FIRST), 5, SECOND), 8, THIRD
)


@pytest.fixture
def renamed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Session:
    """Comments on OLD_PATH lines 2, 5 and 8; then a push renames the file, unchanged."""
    session = Session(tmp_path, capsys, base_files={OLD_PATH: text(RENAME_BASE)})
    session.push({OLD_PATH: text(RENAME_HEAD)})
    assert [RENAME_HEAD[i - 1] for i in RENAME_LINES] == [FIRST, SECOND, THIRD]
    first = session.run([ai(line, file=OLD_PATH) for line in RENAME_LINES])
    assert len(first.posted) == 3, first.calls
    return session


def push_rename(session: Session) -> None:
    session.push({}, renames={OLD_PATH: NEW_PATH})
    assert f"rename to {NEW_PATH}" in session.repo.pr_diff(session.head)
    for comment in session.open_comments():
        session.github.outdate(comment.comment_id)


def test_p2_s5_ac6_comments_found_again_under_the_new_path_move_there_with_their_fingerprints(
    renamed,
):
    old = {line: renamed.comment_at(OLD_PATH, line) for line in RENAME_LINES}
    push_rename(renamed)

    run = renamed.run([ai(2, file=NEW_PATH), ai(5, file=NEW_PATH)])

    assert run.exit_code == 0, run.stderr
    assert sorted(run.resolved_threads) == sorted([old[2].thread_id, old[5].thread_id])
    assert sorted(run.posted) == [
        (NEW_PATH, 2, "RIGHT", old[2].body),
        (NEW_PATH, 5, "RIGHT", old[5].body),
    ]
    moved = sorted((e["file"], e["line"], e["fingerprint"]) for e in run.entries
                   if e["action"] == "move")  # fmt: skip
    assert moved == [
        (NEW_PATH, 2, marker_of(old[2].body)["fingerprint"]),
        (NEW_PATH, 5, marker_of(old[5].body)["fingerprint"]),
    ]
    assert not [e for e in run.entries if e["action"] == "create"]
    assert renamed.comment(old[8].comment_id).resolved_by is None, "not found again: kept"


def test_p2_s5_ac6_after_the_move_nothing_is_created_twice(renamed):
    push_rename(renamed)
    findings = [ai(2, file=NEW_PATH), ai(5, file=NEW_PATH)]
    assert renamed.run(findings).exit_code == 0

    run = renamed.run(findings)

    assert run.exit_code == 0, run.stderr
    assert run.writes == ["write_summary"]
    paths = Counter((c.path, marker_of(c.body)["fingerprint"]) for c in renamed.open_comments())
    assert all(count == 1 for count in paths.values()), paths
    assert len(renamed.open_comments()) == 3


# AC7: a person-resolved thread is never posted again; a bot-resolved one never blocks.


def test_p2_s5_ac7_finding_paired_with_a_person_resolved_thread_posts_nothing(session):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    session.github.person_resolves(session.comment_at(APP, EVAL_LINE).comment_id)
    session.push({OTHER_FILE: "unrelated = 1\n"})

    run = session.run([ai(EVAL_LINE)])

    assert run.exit_code == 0, run.stderr
    assert run.writes == ["write_summary"]


def test_p2_s5_ac7_person_resolved_outdated_thread_isnt_moved_or_reposted(session):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    comment = session.comment_at(APP, EVAL_LINE)
    session.github.person_resolves(comment.comment_id)
    session.push({APP: text(NEIGHBOUR_EDIT)})
    session.github.outdate(comment.comment_id)

    run = session.run([ai(EVAL_LINE)])

    assert run.exit_code == 0, run.stderr
    assert run.writes == ["write_summary"]


def test_p2_s5_ac7_person_resolved_thread_isnt_reposted_after_a_rename(renamed):
    comment = renamed.comment_at(OLD_PATH, 2)
    renamed.github.person_resolves(comment.comment_id)
    push_rename(renamed)
    renamed.github.outdate(comment.comment_id)

    run = renamed.run([ai(2, file=NEW_PATH)])

    assert run.exit_code == 0, run.stderr
    assert run.writes == ["write_summary"]


def test_p2_s5_ac7_finding_whose_only_match_is_bot_resolved_is_created(session):
    session.push({APP: text(APP_V1)})
    seed_on_eval(session, fingerprint_of(EVAL), "high", resolved_by="bot", outdated=True)
    seed_on_eval(session, OTHER_FP_1, "high", resolved_by="bot")  # same key, current line

    run = session.run([ai(EVAL_LINE)])

    assert run.exit_code == 0, run.stderr
    assert [(path, line) for path, line, _, _ in run.posted] == [(APP, EVAL_LINE)]
    assert run.entry("create")["applied"] == "done"


# AC8: two open comments with one fingerprint.


def test_p2_s5_ac8_the_one_that_isnt_outdated_is_kept_and_the_other_resolved(session):
    session.push({APP: text(APP_V1)})
    fingerprint = fingerprint_of(EVAL)
    older = seed_on_eval(session, fingerprint, "high", outdated=True)
    current = seed_on_eval(session, fingerprint, "high")

    run = session.run([ai(EVAL_LINE)])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == [session.comment(older).thread_id]
    assert run.posted == []
    assert session.comment(current).resolved_by is None


def test_p2_s5_ac8_both_current_keeps_the_older_and_resolves_the_newer(session):
    session.push({APP: text(APP_V1)})
    fingerprint = fingerprint_of(EVAL)
    older = seed_on_eval(session, fingerprint, "high")
    newer = seed_on_eval(session, fingerprint, "high")

    run = session.run([ai(EVAL_LINE)])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == [session.comment(newer).thread_id]
    assert run.posted == []
    assert session.comment(older).resolved_by is None


def test_p2_s5_ac8_both_outdated_with_no_finding_keeps_the_older(session):
    session.push({APP: text(APP_V1)})  # the anchor is still in the file: neither line is gone
    fingerprint = fingerprint_of(EVAL)
    older = seed_on_eval(session, fingerprint, "high", outdated=True)
    newer = seed_on_eval(session, fingerprint, "high", outdated=True)

    run = session.run([])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == [session.comment(newer).thread_id]
    assert run.posted == []
    assert session.comment(older).resolved_by is None


# AC9: comments that aren't the bot's own are never touched and never block a create.

VALID_MARKER_BODY = bot_body(fingerprint=fingerprint_of(EVAL), anchor_text=EVAL)
NOT_OWN = {
    "other_user_valid_marker": {"author": "octocat", "body": VALID_MARKER_BODY},
    "other_bot_valid_marker": {"author": "dependabot[bot]", "body": VALID_MARKER_BODY},
    "bot_no_marker": {"body": "**high** logic (source ai, confidence 0.90)\n\nR."},
    "bot_marker_15_hex": {
        "body": "Note.\n\n" + marker(fingerprint_of(EVAL)[:15], AI_LOGIC, anchor_of(EVAL))
    },
    "bot_text_after_marker_line": {"body": VALID_MARKER_BODY + "\n\nThanks."},
    "bot_text_after_marker_on_its_line": {"body": VALID_MARKER_BODY + " trailing"},
}


@pytest.mark.parametrize("case", NOT_OWN)
def test_p2_s5_ac9_not_own_comment_doesnt_block_a_create(session, case):
    session.push({APP: text(APP_V1)})
    foreign = session.github.seed(path=APP, line=EVAL_LINE, **NOT_OWN[case])

    run = session.run([ai(EVAL_LINE)])

    assert run.exit_code == 0, run.stderr
    assert [(path, line) for path, line, _, _ in run.posted] == [(APP, EVAL_LINE)]
    assert foreign not in run.updated_ids
    assert session.comment(foreign).thread_id not in run.resolved_threads
    assert session.comment(foreign).body == NOT_OWN[case]["body"]


@pytest.mark.parametrize("case", NOT_OWN)
def test_p2_s5_ac9_not_own_comment_on_a_gone_line_isnt_resolved(session, case):
    """Were it the bot's, its deleted line would resolve it (AC4)."""
    session.push({APP: text(replaced(APP_V1, EVAL_LINE, None))})
    session.github.seed(path=APP, line=EVAL_LINE, outdated=True, **NOT_OWN[case])

    run = session.run([])

    assert run.exit_code == 0, run.stderr
    assert run.writes == ["write_summary"]


# AC10: at most 10 open own comments per PR.

MANY = [f"    item_{n:02} = compute({n})" for n in range(1, 21)]
EIGHT = [1, 2, 3, 4, 5, 6, 7, 8]
NEW_FIVE = {11: "critical", 12: "high", 13: "low", 14: "low", 15: "low"}


def test_p2_s5_ac10_eight_open_and_five_new_creates_two_and_withholds_three(session):
    posted_app(session, [ai(line) for line in EIGHT], MANY)

    run = session.run([ai(line, severity) for line, severity in NEW_FIVE.items()])

    assert run.exit_code == 0, run.stderr
    assert sorted(line for _, line, _, _ in run.posted) == [11, 12]  # by severity first
    withheld = sorted((w["line"], w["reason"]) for w in run.shadow["withheld"])
    assert withheld == [(13, "over_limit"), (14, "over_limit"), (15, "over_limit")]
    assert len(session.open_comments()) == DEFAULT_MAX_OPEN_COMMENTS
    assert run.resolved_threads == []


def test_p2_s5_ac10_resolved_threads_dont_count_towards_the_limit(session):
    posted_app(session, [ai(line) for line in EIGHT], MANY)
    for line, resolver in ((16, "person"), (17, "person"), (18, "bot")):
        session.github.seed(
            path=APP,
            line=line,
            body=bot_body(fingerprint=OTHER_FP_1[:-2] + f"{line:02}", anchor_text=MANY[line - 1]),
            resolved_by=resolver,
        )

    run = session.run([ai(line, severity) for line, severity in NEW_FIVE.items()])

    assert run.exit_code == 0, run.stderr
    assert sorted(line for _, line, _, _ in run.posted) == [11, 12]


def test_p2_s5_ac10_creates_never_take_a_pr_past_the_limit_in_one_run(session):
    session.push({APP: text(MANY)})

    run = session.run([ai(line) for line in range(1, 13)])

    assert run.exit_code == 0, run.stderr
    assert sorted(line for _, line, _, _ in run.posted) == list(range(1, 11))
    assert sorted(w["line"] for w in run.shadow["withheld"]) == [11, 12]


# AC11: a skipped outcome makes no call at all.


def seed_gone_comment(session: Session) -> None:
    """An own comment whose line is gone: any plan would resolve it."""
    session.github.seed(
        path=APP,
        line=EVAL_LINE,
        body=bot_body(fingerprint=fingerprint_of(EVAL), anchor_text=EVAL),
        outdated=True,
    )


SKIPS = ["fork", "action", "no_credential", "draft", "nothing_to_review", "too_large"]


@pytest.mark.parametrize("reason", SKIPS)
def test_p2_s5_ac11_skipped_outcome_records_no_call(session, monkeypatch, reason):
    session.push({APP: text(replaced(APP_V1, EVAL_LINE, None))})
    seed_gone_comment(session)
    payload = session.payload()
    provider_name = "mock"
    if reason == "fork":
        payload = session.payload(head_repo="someone/review-bot")
    elif reason == "action":
        payload = session.payload(action="closed")
    elif reason == "no_credential":
        monkeypatch.setenv("CI", "true")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        provider_name = "claude"
    elif reason == "draft":
        set_in(payload, "pull_request.draft", True)
    elif reason == "nothing_to_review":
        session.repo.git("reset", "-q", "--hard", session.repo.base)
        session.push({"README.md": "# Title\n\nWords.\n"})
        payload = session.payload()
    elif reason == "too_large":
        monkeypatch.setenv("REVIEW_BOT_MAX_DIFF_BYTES", "100")
    provider = MockProvider(findings=[])
    calls_before = len(session.github.calls())

    run = session.run(payload=payload, provider=provider, provider_name=provider_name)

    assert run.exit_code == 0, run.stderr
    assert (run.shadow["outcome"], run.shadow["reason"]) == ("skipped", reason)
    assert session.github.calls()[calls_before:] == []
    assert provider.requests == []


# AC12: a secret or a provider failure changes only the summary.

# Built at run time so no key-shaped literal sits in the repository.
AWS_KEY = "AKIA" + "Q7XW3RT9LP2MN5VB"


def test_p2_s5_ac12_secret_in_the_diff_changes_only_the_summary_naming_kind_and_line(session):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    session.push(
        {
            APP: text(replaced(APP_V1, EVAL_LINE, None)),  # would resolve the comment if planned
            OTHER_FILE: f'KEY_ID = "{AWS_KEY}"\n',
        }
    )
    diff_lines = session.repo.pr_diff(session.head).splitlines()
    (secret_line,) = [n for n, line in enumerate(diff_lines, start=1) if AWS_KEY in line]
    provider = MockProvider(findings=[ai(1)])

    run = session.run(provider=provider)

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "blocked_secret"
    assert run.writes == ["write_summary"]
    assert provider.requests == []
    summary = session.github.summary()
    assert summary is not None
    assert "aws-access-key" in summary
    assert re.search(rf"\b{secret_line}\b", summary), summary
    assert AWS_KEY not in repr(run.calls)
    assert AWS_KEY not in summary


def test_p2_s5_ac12_transient_failure_changes_only_the_summary_and_exits_11(session):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    session.push({APP: text(replaced(APP_V1, EVAL_LINE, None))})
    provider = MockProvider(error=ProviderError(ErrorCategory.TRANSIENT, "Try again."))

    run = session.run(provider=provider)

    assert run.exit_code == TRANSIENT_EXIT, run.stderr
    assert (run.shadow["outcome"], run.shadow["reason"]) == ("failed", "transient")
    assert run.writes == ["write_summary"]
    summary = session.github.summary()
    assert summary is not None
    assert "transient" in summary
    # "saying the review is unavailable", in the words S1's summary already uses or the spec's.
    assert re.search(r"unavailable|didn't complete", summary), summary


# AC13: a failed GitHub call exits 18 naming it; every entry says what was applied.

APPLIES = {"post_review": "create", "update_comment": "update", "resolve_thread": "resolve"}
NAMES = {
    "post_review": r"post",
    "update_comment": r"updat|edit",
    "resolve_thread": r"resolv",
    "write_summary": r"summary",
}
THIRD_RUN_LINES = [
    *replaced(APP_V1, WRITE_LINE, "    write(path, str(value))"),
    "    other = eval(more)",
]


def three_operations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Session:
    """Ready for a run that updates line 3, resolves line 6's comment and creates line 7."""
    session = Session(tmp_path, capsys)
    posted_app(session, [ai(EVAL_LINE), ai(WRITE_LINE)], APP_V1)
    write_comment = session.comment_at(APP, WRITE_LINE)
    session.push({APP: text(THIRD_RUN_LINES)})
    session.github.outdate(write_comment.comment_id)
    return session


THIRD_RUN_FINDINGS = [ai(EVAL_LINE, "medium"), ai(7)]


def test_p2_s5_ac13_precondition_the_run_has_three_entry_operations(tmp_path, capsys):
    session = three_operations(tmp_path, capsys)

    run = session.run(THIRD_RUN_FINDINGS)

    assert run.exit_code == 0, run.stderr
    assert Counter(run.writes) == Counter(
        {"update_comment": 1, "resolve_thread": 1, "post_review": 1, "write_summary": 1}
    )
    assert sorted(e["action"] for e in run.entries) == ["create", "resolve", "update"]


def test_p2_s5_ac13_failing_the_second_operation_exits_18_and_reports_what_was_applied(
    tmp_path, capsys
):
    rehearsal = three_operations(tmp_path / "rehearsal", capsys).run(THIRD_RUN_FINDINGS)
    order = rehearsal.writes
    failing = order[1]
    session = three_operations(tmp_path / "failing", capsys)
    session.github.fail(failing)

    run = session.run(THIRD_RUN_FINDINGS)

    assert run.exit_code == GITHUB_ERROR_EXIT, run.stderr
    assert re.search(NAMES[failing], run.stderr, re.IGNORECASE), run.stderr
    expected = {}
    for operation, action in APPLIES.items():
        position = order.index(operation)
        expected[action] = (
            "done" if position < 1 else "failed" if position == 1 else ("not_attempted")
        )
    assert {e["action"]: e["applied"] for e in run.entries} == expected
    assert run.writes[:1] == order[:1]


@pytest.mark.parametrize("failing", ["post_review", "update_comment", "resolve_thread"])
def test_p2_s5_ac13_any_failed_operation_still_writes_the_shadow_report(tmp_path, capsys, failing):
    session = three_operations(tmp_path, capsys)
    session.github.fail(failing)

    run = session.run(THIRD_RUN_FINDINGS)

    assert run.exit_code == GITHUB_ERROR_EXIT, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    applied = {e["action"]: e["applied"] for e in run.entries}
    assert applied[APPLIES[failing]] == "failed"
    assert set(applied.values()) <= {"done", "failed", "not_attempted"}


# AC14: a draft in comment mode is skipped.


def test_p2_s5_ac14_draft_pr_in_comment_mode_is_skipped_draft(session):
    session.push({APP: text(APP_V1)})
    provider = MockProvider(findings=[ai(EVAL_LINE)])

    run = session.run(
        payload=set_in(session.payload(), "pull_request.draft", True), provider=provider
    )

    assert run.exit_code == 0, run.stderr
    assert (run.shadow["mode"], run.shadow["outcome"], run.shadow["reason"]) == (
        "comment",
        "skipped",
        "draft",
    )
    assert run.shadow["comments"] == []
    assert provider.requests == []


def test_p2_s5_ac14_the_same_draft_in_shadow_mode_is_still_reviewed(session):
    session.push({APP: text(APP_V1)})

    run = session.run(
        [ai(EVAL_LINE)],
        mode="shadow",
        payload=set_in(session.payload(), "pull_request.draft", True),
    )

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"


# AC15: shadow mode builds no client and reads no token. Its interim exit 2 for comment mode
# with no client is replaced by P2-S6 AC1 (test_p2_s6_comments_on.py).


class WatchedEnviron(dict[str, str]):
    """`os.environ` that remembers each variable read by name."""

    def __init__(self, source: Any) -> None:
        super().__init__(source)
        self.read: set[str] = set()

    def __getitem__(self, key: str) -> str:
        self.read.add(key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        self.read.add(key)
        return super().get(key, default)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            self.read.add(key)
        return super().__contains__(key)


def test_p2_s5_ac15_shadow_mode_reads_no_token(session, monkeypatch):
    session.push({APP: text(APP_V1)})
    monkeypatch.setenv("GITHUB_TOKEN", "token-canary-not-a-real-token")
    watched = WatchedEnviron(os.environ)
    monkeypatch.setattr(os, "environ", watched)

    run = session.run([ai(EVAL_LINE)], mode="shadow")

    assert run.exit_code == 0, run.stderr
    assert run.shadow["mode"] == "shadow"
    assert "GITHUB_TOKEN" not in watched.read
    assert len(run.shadow["comments"]) == 1


def test_p2_s5_ac15_shadow_mode_uses_no_client_even_when_one_is_given(session):
    session.push({APP: text(APP_V1)})

    run = session.run([ai(EVAL_LINE)], mode="shadow", with_client=True)

    assert run.exit_code == 0, run.stderr
    assert run.calls == []


# AC16: no churn after a move, and after a line comes back.


def test_p2_s5_ac16_two_runs_after_a_move_post_move_and_resolve_nothing(session):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    old = session.comment_at(APP, EVAL_LINE)
    session.push({APP: text(NEIGHBOUR_EDIT)})
    session.github.outdate(old.comment_id)
    move = session.run([ai(EVAL_LINE)])
    assert move.entry("move")["applied"] == "done"

    again = session.run([ai(EVAL_LINE)])
    session.push({OTHER_FILE: "unrelated = 1\n"})
    after_push = session.run([ai(EVAL_LINE)])

    for run in (again, after_push):
        assert run.exit_code == 0, run.stderr
        assert run.writes == ["write_summary"]
        kept = run.entry("keep")
        assert kept["fingerprint"] == marker_of(old.body)["fingerprint"]
        assert kept["line"] == EVAL_LINE
    assert len(session.open_comments()) == 1


@pytest.mark.parametrize("github_view", ["stays_outdated", "current_again"])
def test_p2_s5_ac16_restored_line_creates_one_comment_then_nothing(session, github_view):
    posted_app(session, [ai(EVAL_LINE)], APP_V1)
    original = session.comment_at(APP, EVAL_LINE)
    session.push({APP: text(replaced(APP_V1, EVAL_LINE, None))})
    session.github.outdate(original.comment_id)
    deleted = session.run([])
    assert deleted.resolved_threads == [original.thread_id]
    session.push({APP: text(APP_V1)})
    if github_view == "current_again":
        session.github.show_at(original.comment_id, EVAL_LINE)

    restored = session.run([ai(EVAL_LINE)])
    again = session.run([ai(EVAL_LINE)])
    session.push({OTHER_FILE: "unrelated = 1\n"})
    after_push = session.run([ai(EVAL_LINE)])

    assert restored.exit_code == 0, restored.stderr
    assert [(path, line) for path, line, _, _ in restored.posted] == [(APP, EVAL_LINE)]
    assert restored.resolved_threads == []
    for run in (again, after_push):
        assert run.exit_code == 0, run.stderr
        assert run.writes == ["write_summary"]
    assert len(session.open_comments()) == 1


# Limits: own comments by author plus an exact marker; planning linear per key.


def test_p2_s5_limits_huge_foreign_body_full_of_marker_text_is_ignored_quickly(session):
    session.push({APP: text(APP_V1)})
    hostile = "<!-- review-bot:v1 fingerprint=" + "a" * 1_000_000 + " rule=" * 50_000
    session.github.seed(path=APP, line=EVAL_LINE, author="octocat", body=hostile)
    session.github.seed(path=APP, line=EVAL_LINE, body=hostile)  # the bot's, but malformed

    started = time.monotonic()
    run = session.run([ai(EVAL_LINE)])
    elapsed = time.monotonic() - started

    assert run.exit_code == 0, run.stderr
    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    assert [(path, line) for path, line, _, _ in run.posted] == [(APP, EVAL_LINE)]


def test_p2_s5_limits_thousands_of_comments_on_one_key_pair_quickly(session):
    """2,000 identical lines, each with an open comment under another ordinal and a finding."""
    count = 2_000
    session.push({APP: text([EVAL] * count)})
    for line in range(1, count + 1):
        session.github.seed(
            path=APP,
            line=line,
            body=bot_body(fingerprint=f"{line:016x}", anchor_text=EVAL),
        )

    started = time.monotonic()
    run = session.run([ai(line) for line in range(1, count + 1)])
    elapsed = time.monotonic() - started

    assert run.exit_code == 0, run.stderr
    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    assert run.writes == ["write_summary"]
