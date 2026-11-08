r"""P2-S11: capture the value baseline from PR history.

Written from the spec's criteria and design.md (How the gate is evidenced, Baseline; Q8) while
the developer built the slice, before any of its code was read.

- **AC1 to AC8 and AC10** run `review_bot.pr.cli.main(["baseline", "--repo", ..., "--since", ...,
  "--until", ..., "--out", FILE], github=FAKE)` in process. `FAKE` is the developer's
  `FakeGitHubClient`, reached only through `History` below, the one place to change if its seam
  differs.
- **AC9** runs `baseline` with no injected client, so it builds `RestGitHubClient` from
  `GITHUB_TOKEN` and `GITHUB_API_URL`, against `github_stub.GitHubStub` on 127.0.0.1. The stub
  serves the pulls list, each PR's commits, the issues list and issue search, so the tests check
  the numbers, not which endpoints the client chose.
- **Reading `FILE`:** the spec names `schema`, `pr_count`, `fix_prs`, `bug_issues`,
  `escaped_defects_per_merged_pr`, `repo`, `since`, `until` and `captured_at`, but not where the
  medians and p90s live. `stat` finds them by meaning: the one leaf whose path names the measure
  (`merge`, or `commit`) and the statistic (`median` or `p90`).

PR titles are canaries: none may appear in `FILE`.
"""

import importlib
import json
import math
import re
import socket
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from github_stub import GitHubStub

pytestmark = pytest.mark.integration

REPO = "octo-org/review-bot"
SINCE, UNTIL = "2026-10-01", "2026-10-31"
USAGE_EXIT = 2
GITHUB_ERROR_EXIT = 18
TOKEN = "ghs_canary_s11"  # noqa: S105 - a canary to look for, not a credential
TIMEOUT_LIMIT_S = 30.0
GITHUB_VARIABLES = ("GITHUB_TOKEN", "GITHUB_API_URL", "GITHUB_GRAPHQL_URL", "GITHUB_REPOSITORY")
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")

# AC1: open for 2, 5, 10 and 30 hours. Median (5 + 10) / 2; p90 by nearest rank is the 4th of 4.
HOURS_OPEN = (2, 5, 10, 30)
# AC3: only the first two are `fix` PRs.
TITLES = ("Fix rounding", "fix: totals", "Fixture cleanup", "Prefix tidy")
PLAIN_TITLE = "Tidy the docs"
# AC2: commits after opening, per PR: 1, 2, 3 and 5. Median 2.5; p90 by nearest rank 5.
COMMIT_OFFSETS_H = (
    (-1.0, 0.5),  # one before opening isn't counted
    (1.0, 2.0),
    (-2.0, 1.0, 3.0, 9.0),
    (1.0, 2.0, 3.0, 4.0, 5.0),
)


def at(day: int, hour: float = 0.0, *, month: int = 10) -> datetime:
    return datetime(2026, month, day, tzinfo=UTC) + timedelta(hours=hour)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No GitHub variable, proxy or CI flag from the machine running the tests."""
    for name in (*GITHUB_VARIABLES, *PROXY_VARIABLES, "CI"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


# The FakeGitHubClient seam: the only code that knows the fake's own names.


class History:
    """A repository's PRs and issues, held by the developer's `FakeGitHubClient`."""

    def __init__(self) -> None:
        self.client = importlib.import_module("review_bot.pr.fake_github").FakeGitHubClient()

    def merged(
        self,
        number: int,
        *,
        opened: datetime,
        merged: datetime,
        title: str = PLAIN_TITLE,
        commits: Sequence[datetime] = (),
    ) -> None:
        """A merged PR; `commits` are its commits' committer dates."""
        self.client.add_pull_request(
            number=number,
            title=title,
            created_at=opened,
            closed_at=merged,
            merged_at=merged,
            commit_dates=tuple(commits),
        )

    def closed(self, number: int, *, opened: datetime, closed: datetime, title: str) -> None:
        """A PR closed without merging."""
        self.client.add_pull_request(
            number=number,
            title=title,
            created_at=opened,
            closed_at=closed,
            merged_at=None,
            commit_dates=(),
        )

    def issue(self, number: int, *, created: datetime, labels: Sequence[str] = ("bug",)) -> None:
        self.client.add_issue(number=number, labels=tuple(labels), created_at=created)

    def fail(self, operation: str) -> None:
        self.client.fail_on(operation)


def ac1_history(history: History, *, titles: Sequence[str] = TITLES) -> None:
    """AC1's four merged PRs, with AC2's commits and AC3's titles, all inside the window."""
    for number, (hours, title, offsets) in enumerate(
        zip(HOURS_OPEN, titles, COMMIT_OFFSETS_H, strict=True), start=1
    ):
        opened = at(9 + number)
        history.merged(
            number,
            opened=opened,
            merged=opened + timedelta(hours=hours),
            title=title,
            commits=[opened + timedelta(hours=offset) for offset in offsets],
        )


# Running `baseline` and reading what it wrote.


def _strict_constant(name: str) -> Any:
    raise ValueError(f"not JSON: {name}")


@dataclass(frozen=True, slots=True)
class Baseline:
    exit_code: int
    stdout: str
    stderr: str
    file: Path

    @property
    def raw(self) -> bytes:
        return self.file.read_bytes()

    @property
    def doc(self) -> dict[str, Any]:
        """`FILE` as one strict ASCII JSON document: no NaN or Infinity, nothing after it."""
        text = self.raw.decode("ascii")
        document = json.loads(text, parse_constant=_strict_constant)
        assert isinstance(document, dict), document
        return document


def baseline(  # noqa: PLR0913 - keyword-only, one per flag
    where: Path,
    capsys: pytest.CaptureFixture[str],
    github: Any = None,
    *,
    repo: str = REPO,
    since: str = SINCE,
    until: str = UNTIL,
) -> Baseline:
    """`baseline` over the window; `github` None lets it build `RestGitHubClient`."""
    where.mkdir(parents=True, exist_ok=True)
    file = where / "baseline.json"
    argv = ["baseline", "--repo", repo, "--since", since, "--until", until, "--out", str(file)]
    pr_cli = importlib.import_module("review_bot.pr.cli")
    extra = {} if github is None else {"github": github}
    capsys.readouterr()
    try:
        exit_code = pr_cli.main(argv, **extra)
    except SystemExit as exc:  # argparse's own usage exit is still an exit code
        exit_code = exc.code if isinstance(exc.code, int) else USAGE_EXIT
    captured = capsys.readouterr()
    return Baseline(exit_code, captured.out, captured.err, file)


def leaves(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from leaves(child, f"{path}.{key}" if path else str(key))
    else:
        yield path, value


def stat(doc: dict[str, Any], measure: str, statistic: str) -> Any:
    """The one leaf whose path names `measure` (`merge` or `commit`) and `statistic`."""
    found = [
        value
        for path, value in leaves(doc)
        if measure in path.lower() and statistic in path.lower()
    ]
    assert len(found) == 1, (measure, statistic, list(leaves(doc)))
    return found[0]


def assert_close(actual: Any, expected: float) -> None:
    assert isinstance(actual, int | float) and not isinstance(actual, bool), actual
    assert math.isclose(actual, expected, abs_tol=1e-9), (actual, expected)


@pytest.fixture
def history() -> History:
    return History()


def captured(history: History, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Baseline:
    run = baseline(tmp_path, capsys, history.client)
    assert run.exit_code == 0, run.stderr
    return run


# AC1: one ASCII JSON document; open-to-merge median (the middle two's mean) and p90 (nearest rank).


def test_p2_s11_ac1_four_merged_prs_give_median_7_5_and_p90_30_hours(history, tmp_path, capsys):
    ac1_history(history)

    doc = captured(history, tmp_path, capsys).doc

    assert doc["schema"] == "baseline/v1"
    assert doc["pr_count"] == 4
    assert_close(stat(doc, "merge", "median"), 7.5)
    assert_close(stat(doc, "merge", "p90"), 30.0)


def test_p2_s11_ac1_file_is_one_ascii_json_document(history, tmp_path, capsys):
    ac1_history(history)

    run = captured(history, tmp_path, capsys)

    assert run.raw.isascii()
    assert run.raw.rstrip(b"\n").count(b"\n") == 0, "more than one line"
    assert run.doc["schema"] == "baseline/v1"


# AC2: commits whose committer date is after the PR opened, per PR, as commits, not pushes.


def test_p2_s11_ac2_commits_after_opening_give_median_2_5_and_p90_5(history, tmp_path, capsys):
    ac1_history(history)

    doc = captured(history, tmp_path, capsys).doc

    assert_close(stat(doc, "commit", "median"), 2.5)
    assert_close(stat(doc, "commit", "p90"), 5.0)
    assert not [path for path, _ in leaves(doc) if "push" in path.lower()]


# AC3: `fix` as a whole first word, case-insensitive; bug issues; the escaped-defect proxy.


def test_p2_s11_ac3_fix_prs_bug_issues_and_escaped_defects_per_merged_pr(history, tmp_path, capsys):
    ac1_history(history)
    history.issue(101, created=at(12))
    history.issue(102, created=at(20, 5))
    history.issue(103, created=at(15), labels=("enhancement",))  # not `bug`
    history.issue(104, created=at(20, month=9))  # before the window
    history.issue(105, created=at(5, month=11))  # after the window

    doc = captured(history, tmp_path, capsys).doc

    assert doc["fix_prs"] == 2
    assert doc["bug_issues"] == 2
    assert_close(doc["escaped_defects_per_merged_pr"], 1.0)


@pytest.mark.parametrize(
    ("titles", "fix_prs"),
    [
        (("FIX totals", "Fix", "fix(core): x", PLAIN_TITLE), 3),
        (("Fixes crash", "Fixed it", "fixup! x", "prefix fix"), 0),
    ],
    ids=["whole-word-any-case", "not-a-whole-first-word"],
)
def test_p2_s11_ac3_fix_is_a_whole_first_word_in_any_case(
    history, tmp_path, capsys, titles, fix_prs
):
    ac1_history(history, titles=titles)

    assert captured(history, tmp_path, capsys).doc["fix_prs"] == fix_prs


# AC4: PRs closed without merging, or merged outside the window, aren't counted.


def test_p2_s11_ac4_unmerged_and_out_of_window_prs_arent_counted(history, tmp_path, capsys):
    ac1_history(history, titles=(PLAIN_TITLE,) * 4)
    history.closed(11, opened=at(12), closed=at(12, 1), title="Fix closed unmerged")
    history.merged(12, opened=at(20, month=9), merged=at(25, month=9), title="Fix too early")
    history.merged(13, opened=at(30, 20), merged=at(5, month=11), title="Fix too late")

    doc = captured(history, tmp_path, capsys).doc

    assert doc["pr_count"] == 4
    assert_close(stat(doc, "merge", "median"), 7.5)
    assert_close(stat(doc, "merge", "p90"), 30.0)
    assert doc["fix_prs"] == 0


def test_p2_s11_ac4_a_pr_opened_before_the_window_and_merged_in_it_counts(
    history, tmp_path, capsys
):
    history.merged(1, opened=at(29, month=9), merged=at(3))  # open 96 hours

    doc = captured(history, tmp_path, capsys).doc

    assert doc["pr_count"] == 1
    assert_close(stat(doc, "merge", "median"), 96.0)
    assert_close(stat(doc, "merge", "p90"), 96.0)


# AC5: an empty window.


def test_p2_s11_ac5_empty_window_has_null_medians_and_p90s_and_exits_0(history, tmp_path, capsys):
    run = captured(history, tmp_path, capsys)

    doc = run.doc
    assert doc["pr_count"] == 0
    for measure in ("merge", "commit"):
        for statistic in ("median", "p90"):
            assert stat(doc, measure, statistic) is None, (measure, statistic)


# AC6: the window and capture time in FILE; the same numbers on stdout.


def test_p2_s11_ac6_file_holds_the_repo_window_and_a_utc_capture_time(history, tmp_path, capsys):
    ac1_history(history)
    before = datetime.now(UTC)

    doc = captured(history, tmp_path, capsys).doc

    after = datetime.now(UTC)
    assert doc["repo"] == REPO
    assert str(doc["since"]).startswith(SINCE)
    assert str(doc["until"]).startswith(UNTIL)
    captured_at = datetime.fromisoformat(str(doc["captured_at"]).replace("Z", "+00:00"))
    assert captured_at.utcoffset() == timedelta(0), doc["captured_at"]
    assert before - timedelta(seconds=1) <= captured_at <= after + timedelta(seconds=1)


def test_p2_s11_ac6_stdout_shows_the_same_numbers_as_text(history, tmp_path, capsys):
    ac1_history(history)
    history.issue(101, created=at(12))
    history.issue(102, created=at(13))

    run = captured(history, tmp_path, capsys)

    assert REPO in run.stdout
    numbers = re.findall(r"\d+(?:\.\d+)?", run.stdout)
    values = {float(number) for number in numbers}
    for expected in (4, 7.5, 30, 2.5, 5, 2, 1.0):
        assert float(expected) in values, (expected, run.stdout)


# AC7: usage errors exit 2 naming the flag or variable, and write nothing.


@pytest.mark.parametrize(
    ("flags", "named"),
    [
        ({"since": "2026-10-31", "until": "2026-10-01"}, r"--until|--since"),
        ({"since": "yesterday"}, r"--since"),
        ({"since": "2026-13-01"}, r"--since"),
        ({"until": "31/10/2026"}, r"--until"),
        ({"repo": "review-bot"}, r"--repo"),
        ({"repo": "octo-org/review-bot/extra"}, r"--repo"),
        ({"repo": "/review-bot"}, r"--repo"),
    ],
    ids=["until-before-since", "since-word", "since-month-13", "until-not-iso", "repo-no-owner",
         "repo-three-parts", "repo-empty-owner"],
)  # fmt: skip
def test_p2_s11_ac7_bad_flag_exits_2_naming_it(history, tmp_path, capsys, flags, named):
    ac1_history(history)

    run = baseline(tmp_path, capsys, history.client, **flags)

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert re.search(named, run.stderr), run.stderr
    assert not run.file.exists()


def test_p2_s11_ac7_no_token_exits_2_naming_it_and_sends_nothing(
    tmp_path, capsys, monkeypatch, stub
):
    monkeypatch.setenv("GITHUB_API_URL", stub.url)

    run = baseline(tmp_path, capsys)

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert "GITHUB_TOKEN" in run.stderr
    assert stub.requests == []
    assert not run.file.exists()


# AC8: a failing client exits 18 and writes no FILE.


@pytest.mark.parametrize("operation", ["list_merged_pull_requests", "count_issues"])
@pytest.mark.parametrize("existing", [False, True], ids=["no-file", "old-file"])
def test_p2_s11_ac8_a_failing_client_exits_18_and_writes_no_file(
    history, tmp_path, capsys, operation, existing
):
    ac1_history(history)
    history.fail(operation)
    old = b'{"schema": "baseline/v1", "old": true}\n'
    if existing:
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "baseline.json").write_bytes(old)

    run = baseline(tmp_path, capsys, history.client)

    assert run.exit_code == GITHUB_ERROR_EXIT, run.stderr
    if existing:
        assert run.raw == old
    else:
        assert not run.file.exists()


# AC9: the real client against the local stub.


@pytest.fixture
def stub() -> Iterator[GitHubStub]:
    with GitHubStub() as server:
        yield server


@pytest.fixture
def stub_env(stub: GitHubStub, monkeypatch: pytest.MonkeyPatch) -> GitHubStub:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_API_URL", stub.url)
    return stub


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


def stub_history(stub: GitHubStub) -> None:
    """AC1 to AC4 on the stub. Each commit's author date is an hour before its committer date,
    so a client reading the author date counts PR 1's half-hour commit as before opening."""
    for number, (hours, title, offsets) in enumerate(
        zip(HOURS_OPEN, TITLES, COMMIT_OFFSETS_H, strict=True), start=1
    ):
        opened = at(9 + number)
        merged = opened + timedelta(hours=hours)
        commits = [
            (iso(opened + timedelta(hours=offset - 1)), iso(opened + timedelta(hours=offset)))
            for offset in offsets
        ]
        stub.add_pull(
            number,
            title=title,
            created_at=iso(opened),
            closed_at=iso(merged),
            merged_at=iso(merged),
            commits=commits,
        )
    stub.add_pull(
        11, title="Fix unmerged", created_at=iso(at(12)), closed_at=iso(at(12, 1)), merged_at=None
    )
    stub.add_pull(
        12,
        title="Fix early",
        created_at=iso(at(20, month=9)),
        closed_at=iso(at(25, month=9)),
        merged_at=iso(at(25, month=9)),
    )
    stub.add_pull(
        13,
        title="Fix late",
        created_at=iso(at(30, 20)),
        closed_at=iso(at(5, month=11)),
        merged_at=iso(at(5, month=11)),
    )
    stub.add_pull(14, title="Still open", created_at=iso(at(14)), closed_at=None, merged_at=None,
                  labels=("bug",))  # fmt: skip
    stub.add_issue(101, title="Crash", labels=("bug",), created_at=iso(at(12)))
    stub.add_issue(102, title="Wrong total", labels=("bug", "p1"), created_at=iso(at(20, 5)))
    stub.add_issue(103, title="Idea", labels=("enhancement",), created_at=iso(at(15)))
    stub.add_issue(104, title="Old bug", labels=("bug",), created_at=iso(at(20, month=9)))


def test_p2_s11_ac9_rest_client_gives_the_same_baseline(stub_env, tmp_path, capsys):
    stub = stub_env
    stub_history(stub)

    run = baseline(tmp_path, capsys)

    assert run.exit_code == 0, run.stderr
    doc = run.doc
    assert doc["pr_count"] == 4
    assert_close(stat(doc, "merge", "median"), 7.5)
    assert_close(stat(doc, "merge", "p90"), 30.0)
    assert_close(stat(doc, "commit", "median"), 2.5)
    assert_close(stat(doc, "commit", "p90"), 5.0)
    assert doc["fix_prs"] == 2
    assert doc["bug_issues"] == 2  # not the bug-labelled PR, nor the old or other-label issues
    assert_close(doc["escaped_defects_per_merged_pr"], 1.0)
    assert stub.requests, "no request reached the stub"
    assert all(TOKEN in r.headers.get("authorization", "") for r in stub.requests)
    for text in (run.stdout, run.stderr, run.raw.decode("ascii")):
        assert TOKEN not in text


def test_p2_s11_ac9_merged_prs_over_two_pages_are_all_read(stub_env, tmp_path, capsys):
    stub = stub_env
    for number in range(1, 151):
        opened = at(1 + number % 28, 1)
        stub.add_pull(number, title=PLAIN_TITLE, created_at=iso(opened),
                      closed_at=iso(opened + timedelta(hours=3)),
                      merged_at=iso(opened + timedelta(hours=3)))  # fmt: skip

    run = baseline(tmp_path, capsys)

    assert run.exit_code == 0, run.stderr
    assert run.doc["pr_count"] == 150
    assert_close(stat(run.doc, "merge", "median"), 3.0)


def test_p2_s11_limits_more_than_1000_merged_prs_exits_18_naming_the_limit(
    stub_env, tmp_path, capsys
):
    stub = stub_env
    for number in range(1, 1002):
        opened = at(1 + number % 28, 1)
        stub.add_pull(number, title=PLAIN_TITLE, created_at=iso(opened),
                      closed_at=iso(opened + timedelta(hours=2)),
                      merged_at=iso(opened + timedelta(hours=2)))  # fmt: skip

    run = baseline(tmp_path, capsys)

    assert run.exit_code == GITHUB_ERROR_EXIT, run.stderr
    assert re.search(r"1,?000|10 pages", run.stderr), run.stderr
    assert not run.file.exists()
    listing = [r for r in stub.requests if r.route in ("list_pulls", "list_search_issues")]
    assert len(listing) <= 11


FAILURE_KINDS = {
    "401": "401", "404": "404", "500": "500", "rate_limited": "403", "429": "429",
    "not_json": None, "over_5_mb": None,
}  # fmt: skip
HISTORY_ROUTES = ("list_pulls", "list_pull_commits", "list_issues", "list_search_issues")


@pytest.mark.parametrize("kind", FAILURE_KINDS)
def test_p2_s11_ac9_a_failed_read_exits_18_once_without_the_token_or_a_file(
    stub_env, tmp_path, capsys, kind
):
    stub = stub_env
    stub.state.echo_authorization = True
    stub_history(stub)
    for route in HISTORY_ROUTES:
        stub.fail(route, kind)

    run = baseline(tmp_path, capsys)

    assert run.exit_code == GITHUB_ERROR_EXIT, run.stderr
    status = FAILURE_KINDS[kind]
    if status is not None:
        assert status in run.stderr, run.stderr
    assert len(stub.requests) == 1, "not retried, and nothing after the failure"
    assert TOKEN not in run.stdout + run.stderr
    assert not run.file.exists()


def test_p2_s11_ac9_a_failed_commits_read_exits_18_with_no_file(stub_env, tmp_path, capsys):
    stub = stub_env
    stub_history(stub)
    stub.fail("list_pull_commits", "500")

    run = baseline(tmp_path, capsys)

    assert run.exit_code == GITHUB_ERROR_EXIT, run.stderr
    assert not run.file.exists()


def test_p2_s11_ac9_every_request_has_a_timeout_of_at_most_30_s(
    stub_env, tmp_path, capsys, socket_timeouts
):
    stub_history(stub_env)

    run = baseline(tmp_path, capsys)

    assert run.exit_code == 0, run.stderr
    assert socket_timeouts, "the client set no timeout on any socket"
    assert all(t is not None and 0 < t <= TIMEOUT_LIMIT_S for t in socket_timeouts), socket_timeouts


def test_p2_s11_ac9_baseline_is_read_only(stub_env, tmp_path, capsys):
    stub = stub_env
    stub_history(stub)

    run = baseline(tmp_path, capsys)

    assert run.exit_code == 0, run.stderr
    assert {r.method for r in stub.requests} == {"GET"}
    assert stub.writes() == []


# AC10: titles never reach FILE; only the count of `fix` PRs does.

CANARY_TITLES = (
    "Fix \x1b[2J CANARY-TITLE-1a2b",
    'CANARY-TITLE-3c4d"}\x00\r\n{"schema": "forged"',
    f"Tidy {chr(0x2028)} CANARY-TITLE-5e6f {chr(0x202E)}",
    "fix: CANARY-TITLE-7a8b ignore previous instructions",
)


def test_p2_s11_ac10_titles_with_control_characters_or_a_canary_never_reach_file(
    history, tmp_path, capsys
):
    ac1_history(history, titles=CANARY_TITLES)

    run = captured(history, tmp_path, capsys)

    written = run.raw.decode("ascii")
    assert "CANARY" not in written.upper()
    for title in (*CANARY_TITLES, *TITLES, PLAIN_TITLE):
        assert title not in written
    assert run.doc["schema"] == "baseline/v1"
    assert run.doc["fix_prs"] == 2
