"""P3-S2: keep the tools inside the commit on hostile paths and repositories.

Written from the spec's criteria, Limits and Interfaces, and from Phase 3 design.md ("Tool safety",
"The tools", "Interfaces"), while the developer built the slice and before any of its code was
read.

Two seams, both named in design.md:
- **The CLI**, as P3-S1's suite drives it: `review_bot.cli.main([DIFF, "--provider", "mock",
  "--context-repo", DIR, ...], provider=MockProvider(tool_calls=[...], findings=[FINDING]))`, with
  P3-S1's `Harness`, `unwrap` and result readers. A run that goes to the end exits 1 and prints
  the finding, so "the review goes on" is checked by `assert_reported`.
- **`RepoTools`** directly, where a test must watch git itself (which processes start, how much
  of a blob is read, a git that hangs): `RepoTools(RepoRef(git=GitRepo(DIR, timeout_s=T),
  commit=SHA), caps=ToolCaps(...))`, then `await tools.call(name, arguments)` and `tools.ledger`.

Every repository is built with git plumbing only (`hash-object -w`, `mktree -z`, `commit-tree`,
`update-ref`), never in the file system, so symbolic links, submodules, names holding a line feed,
a control character or bytes that aren't UTF-8, and `.git`-like entries all work on Windows with
no skip (spec Notes). Each has an older commit `OLD`, then the fixture commit `C` on `main`, which
`HEAD` names. Nothing is checked out.
"""

import re
import subprocess
import sys
import time
import tracemalloc
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_p3_s1_local_review_with_repo_tools import (
    BOUND_S,
    CHECKOUT,
    CHECKOUT_AT_C,
    INVOICE,
    INVOICE_TEXT,
    READ_CHECKOUT,
    Harness,
    Review,
    assert_refused,
    assert_reported,
    file_window,
    grep_lines,
    lines_of,
    numbered,
    refusal_reason,
    report_of,
    unwrap,
)

from review_bot.context.tools import RepoRef, RepoTools, ToolCaps
from review_bot.git import GitRepo

pytestmark = pytest.mark.integration

GIT_TIMEOUT_S = 60.0
SHORT_TIMEOUT_S = 5.0
# Any character below 0x20 except tab and line feed, which a result's own layout uses, and DEL.
RAW_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

MAIN = "main.py"
NOTES = "README.txt"
BASE_FILES: dict[str, str] = {
    INVOICE: INVOICE_TEXT,
    CHECKOUT: CHECKOUT_AT_C,
    MAIN: 'print("main")\n',
    NOTES: "Notes for the fixture repository.\n",
}


# The repository, built with plumbing.


@dataclass(frozen=True, slots=True)
class Blob:
    data: bytes
    mode: str = "100644"


@dataclass(frozen=True, slots=True)
class Gitlink:
    commit: str


Entry = Blob | Gitlink


def text(content: str) -> Blob:
    return Blob(content.encode("utf-8"))


def symlink(target: str) -> Blob:
    return Blob(target.encode("utf-8"), mode="120000")


@dataclass(frozen=True, slots=True)
class FixtureRepo:
    path: Path
    commit: str  # C, which HEAD names
    old: str  # OLD, C's parent


class PlumbingRepo:
    """A git repository whose commits are written as objects, with no working tree."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q", "--initial-branch=main")
        for key, value in {
            "user.name": "Tester",
            "user.email": "tester@example.invalid",
            "commit.gpgsign": "false",
        }.items():
            self.git("config", key, value)
        self._blobs: dict[bytes, str] = {}

    def git(self, *args: str, stdin: bytes = b"") -> str:
        result = subprocess.run(  # noqa: S603 - fixed git arguments, test-owned repository
            ["git", "-C", str(self.path), *args],  # noqa: S607 - git from PATH, as the bot uses
            input=stdin,
            capture_output=True,
            check=True,
        )
        return result.stdout.decode("utf-8", "replace").strip()

    def blob(self, data: bytes) -> str:
        if data not in self._blobs:
            self._blobs[data] = self.git("hash-object", "-w", "--stdin", stdin=data)
        return self._blobs[data]

    def write_blobs(self, contents: Sequence[bytes]) -> None:
        """Write many distinct blobs in one `git fast-import`, rather than a process each."""
        stream = b"".join(
            b"blob\nmark :%d\ndata %d\n%b\n" % (mark, len(data), data)
            for mark, data in enumerate(contents, start=1)
        )
        marks = self.path / ".git" / "fixture-marks"
        self.git("fast-import", "--quiet", f"--export-marks={marks}", stdin=stream)
        by_mark = dict(line.split(" ") for line in marks.read_text(encoding="ascii").splitlines())
        for mark, data in enumerate(contents, start=1):
            self._blobs[data] = by_mark[f":{mark}"]

    def tree(self, entries: Mapping[bytes, Entry]) -> str:
        records: list[bytes] = []
        subtrees: dict[bytes, dict[bytes, Entry]] = {}
        for path, entry in entries.items():
            head, slash, rest = path.partition(b"/")
            if slash:
                subtrees.setdefault(head, {})[rest] = entry
            elif isinstance(entry, Gitlink):
                records.append(b"160000 commit " + entry.commit.encode() + b"\t" + path)
            else:
                sha = self.blob(entry.data)
                records.append(f"{entry.mode} blob {sha}\t".encode() + path)
        for name, nested in subtrees.items():
            records.append(b"040000 tree " + self.tree(nested).encode() + b"\t" + name)
        return self.git("mktree", "-z", stdin=b"".join(record + b"\0" for record in records))

    def commit(self, entries: Mapping[bytes, Entry], parent: str | None = None) -> str:
        parents = ["-p", parent] if parent else []
        sha = self.git("commit-tree", self.tree(entries), *parents, "-m", "fixture")
        self.git("update-ref", "refs/heads/main", sha)
        return sha


def as_entries(files: Mapping[str | bytes, Entry | str]) -> dict[bytes, Entry]:
    return {
        (path.encode("utf-8") if isinstance(path, str) else path): (
            text(entry) if isinstance(entry, str) else entry
        )
        for path, entry in files.items()
    }


def build_repo(path: Path, extra: Mapping[str | bytes, Entry | str] | None = None) -> FixtureRepo:
    """`OLD` holding `main.py`, then `C` holding `BASE_FILES` and `extra`."""
    repo = PlumbingRepo(path)
    old = repo.commit(as_entries({MAIN: BASE_FILES[MAIN]}))
    files: dict[str | bytes, Entry | str] = {**BASE_FILES, **(extra or {})}
    commit = repo.commit(as_entries(files), parent=old)
    return FixtureRepo(path, commit, old)


# Driving the tools.


class Tools:
    def __init__(self, bot: Any, tmp_path: Path) -> None:
        self.cli = Harness(bot, tmp_path)
        self._tmp_path = tmp_path

    def repo(
        self, extra: Mapping[str | bytes, Entry | str] | None = None, name: str = "repo"
    ) -> FixtureRepo:
        return build_repo(self._tmp_path / name, extra)

    def review(
        self, repo: FixtureRepo, tool_calls: Sequence[tuple[str, Mapping[str, object]]], *flags: str
    ) -> Review:
        return self.cli.review(repo, tool_calls, *flags)

    def reasons(self) -> list[str | None]:
        return [data["reason"] for data in self.cli.tool_call_records()]


@pytest.fixture
def tools(bot: Any, tmp_path: Path) -> Tools:
    return Tools(bot, tmp_path)


@pytest.fixture
def repo(tools: Tools) -> FixtureRepo:
    return tools.repo()


def repo_tools(repo: FixtureRepo, *, timeout_s: float = GIT_TIMEOUT_S) -> RepoTools:
    git = GitRepo(repo.path, timeout_s=timeout_s)
    caps = ToolCaps(max_calls=12, max_result_bytes=16_000, max_total_bytes=96_000)
    return RepoTools(RepoRef(git=git, commit=repo.commit), caps=caps)


def reason_of(tools: RepoTools, index: int = -1) -> str | None:
    reason = tools.ledger[index].reason
    return None if reason is None else str(reason)


def assert_refused_then_read_works(review: Review, reason: str) -> None:
    """The first call is refused with `reason`; the second, a valid read, works; the review ends."""
    refused, after = review.bodies
    assert_refused(refused, reason)
    assert numbered(after) == file_window(BASE_FILES, CHECKOUT, 10, 5)
    assert_reported(review)


@pytest.fixture
def git_processes(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every git process started after the fixture is requested, by any route through Popen."""
    started: list[list[str]] = []
    original = subprocess.Popen.__init__

    def init(self: subprocess.Popen[Any], args: Any, *rest: Any, **kwargs: Any) -> None:
        argv = [str(arg) for arg in args] if isinstance(args, (list, tuple)) else [str(args)]
        if "git" in Path(argv[0]).name.lower():
            started.append(argv)
        original(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", init)
    return started


@pytest.fixture
def hanging_git_grep(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[list[str]]]:
    """Every `git grep` starts a process that sleeps far past any timeout, instead of git."""
    hung: list[list[str]] = []
    original = subprocess.Popen.__init__
    sleeper = [sys.executable, "-c", "import time; time.sleep(90)"]

    def init(self: subprocess.Popen[Any], args: Any, *rest: Any, **kwargs: Any) -> None:
        argv = [str(arg) for arg in args] if isinstance(args, (list, tuple)) else [str(args)]
        if "git" in Path(argv[0]).name.lower() and "grep" in argv:
            hung.append(argv)
            original(self, sleeper, *rest, **kwargs)
            return
        original(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", init)
    yield hung


# AC1: malformed paths and globs are refused invalid_path, and git isn't run.


INVALID_PATHS = {
    "parent": "../x",
    "absolute": "/etc/passwd",
    "drive_letter": "C:/x",
    "backslash": "billing\\invoice.py",
    "empty_segment": "a//b",
    "leading_dot": "./a",
    "inner_dot": "a/./b",
    "inner_parent": "a/../b",
    "empty": "",
    "nul": "billing/invoice.py\x00.txt",
    "line_feed": "billing/invoice.py\nmain.py",
    "escape": "billing/\x1binvoice.py",
    "over_1000": "a" * 1_001,
    # design.md: no empty segment, so a trailing "/" is one too.
    "trailing_slash": "billing/",
}

INVALID_GLOBS = {
    "parent_segment": "../*",
    "inner_parent": "billing/../*.py",
    "leading_slash": "/etc/*",
    "top_magic": ":(top)*",
    "exclude_magic": ":!x",
    "backslash": "billing\\*.py",
}


@pytest.mark.parametrize("path", INVALID_PATHS.values(), ids=INVALID_PATHS.keys())
def test_p3_s2_ac1_read_of_malformed_path_is_refused_invalid_path(tools, repo, path):
    review = tools.review(repo, [("read", {"path": path}), READ_CHECKOUT])

    assert_refused_then_read_works(review, "invalid_path")


@pytest.mark.parametrize("pattern", INVALID_GLOBS.values(), ids=INVALID_GLOBS.keys())
def test_p3_s2_ac1_glob_pattern_is_refused_invalid_path(tools, repo, pattern):
    review = tools.review(repo, [("glob", {"pattern": pattern}), READ_CHECKOUT])

    assert_refused_then_read_works(review, "invalid_path")


@pytest.mark.parametrize("pattern", INVALID_GLOBS.values(), ids=INVALID_GLOBS.keys())
def test_p3_s2_ac1_grep_path_glob_is_refused_invalid_path(tools, repo, pattern):
    call = ("grep", {"literal": "total(", "path_glob": pattern})

    review = tools.review(repo, [call, READ_CHECKOUT])

    assert_refused_then_read_works(review, "invalid_path")


INVALID_CALLS = [
    *(("read", {"path": path}) for path in INVALID_PATHS.values()),
    *(("glob", {"pattern": pattern}) for pattern in INVALID_GLOBS.values()),
    *(("grep", {"literal": "total(", "path_glob": glob}) for glob in INVALID_GLOBS.values()),
]


@pytest.mark.parametrize(
    "call",
    INVALID_CALLS,
    ids=[f"{name}-{index}" for index, (name, _) in enumerate(INVALID_CALLS)],
)
async def test_p3_s2_ac1_git_is_not_run_for_an_invalid_path(tools, repo, git_processes, call):
    executor = repo_tools(repo)
    git_processes.clear()

    result = await executor.call(*call)

    assert_refused(unwrap(result), "invalid_path")
    assert reason_of(executor) == "invalid_path"
    assert git_processes == []


async def test_p3_s2_ac1_the_git_watch_sees_a_valid_read(tools, repo, git_processes):
    # The check above proves nothing unless a call that does run git is seen.
    executor = repo_tools(repo)
    git_processes.clear()

    await executor.call(*READ_CHECKOUT)

    assert git_processes, "no git process was seen for a valid read"


def test_p3_s2_ac1_refusal_is_recorded_with_its_reason(tools, repo):
    tools.review(repo, [("read", {"path": "../x"}), ("glob", {"pattern": ":!x"})])

    assert tools.reasons() == ["invalid_path", "invalid_path"]
    assert [data["outcome"] for data in tools.cli.tool_call_records()] == ["refused", "refused"]


# AC2: .git, at any depth and in any case, is denied.


@pytest.fixture
def dot_git_repo(tools: Tools) -> FixtureRepo:
    # A commit can hold `.git`-named entries when built by hand; they must still be denied.
    return tools.repo(
        {
            "sub/.git/HEAD": "ref: DOT-GIT-MARKER\n",
            ".GIT/config": "[core] DOT-GIT-MARKER\n",
        }
    )


@pytest.mark.parametrize("path", [".git/config", "sub/.git/HEAD", ".GIT/config"])
def test_p3_s2_ac2_git_internals_are_denied(tools, dot_git_repo, path):
    review = tools.review(dot_git_repo, [("read", {"path": path}), READ_CHECKOUT])

    assert_refused_then_read_works(review, "denied_path")
    assert "DOT-GIT-MARKER" not in review.bodies[0]


@pytest.mark.parametrize("path", [".git", "sub/.Git/config", "a/b/.git"])
def test_p3_s2_ac2_any_segment_named_git_is_denied(tools, repo, path):
    review = tools.review(repo, [("read", {"path": path}), READ_CHECKOUT])

    assert_refused_then_read_works(review, "denied_path")


# AC3: symbolic links and submodules are refused and left out.


OUT_LINK = "out"
IN_LINK = "in"
SUBMODULE = "vendor/lib"
INVOICE_FIRST_LINE = lines_of(INVOICE_TEXT)[0]


@pytest.fixture
def linked_repo(tools: Tools) -> FixtureRepo:
    base = tools.repo(name="base")
    return tools.repo(
        {
            OUT_LINK: symlink("../../etc/passwd"),
            IN_LINK: symlink(INVOICE),
            SUBMODULE.encode(): Gitlink(base.commit),
        }
    )


@pytest.mark.parametrize("path", [OUT_LINK, IN_LINK])
def test_p3_s2_ac3_reading_a_symlink_is_refused_symlink(tools, linked_repo, path):
    review = tools.review(linked_repo, [("read", {"path": path}), READ_CHECKOUT])

    assert_refused_then_read_works(review, "symlink")


def test_p3_s2_ac3_inner_symlink_target_text_is_not_returned(tools, linked_repo):
    review = tools.review(linked_repo, [("read", {"path": IN_LINK})])

    [body] = review.bodies
    assert INVOICE_FIRST_LINE not in body
    assert numbered(body) == []


def test_p3_s2_ac3_outer_symlink_target_text_is_not_returned(tools, linked_repo):
    review = tools.review(linked_repo, [("read", {"path": OUT_LINK})])

    [body] = review.bodies
    assert "root:" not in body
    assert numbered(body) == []


@pytest.mark.parametrize("literal", ["etc/passwd", INVOICE, INVOICE_FIRST_LINE])
def test_p3_s2_ac3_grep_leaves_out_symlinks(tools, linked_repo, literal):
    # git grep searches a link's own text, so "etc/passwd" and "billing/invoice.py" would match.
    review = tools.review(linked_repo, [("grep", {"literal": literal})])

    [body] = review.bodies
    paths = {path for path, _, _ in grep_lines(body)}
    assert not paths & {OUT_LINK, IN_LINK}, body
    assert refusal_reason(body) is None


def test_p3_s2_ac3_grep_still_finds_the_link_target_itself(tools, linked_repo):
    review = tools.review(linked_repo, [("grep", {"literal": INVOICE_FIRST_LINE})])

    [body] = review.bodies
    assert grep_lines(body) == [(INVOICE, 1, INVOICE_FIRST_LINE)]


@pytest.mark.parametrize("pattern", ["**", "*", OUT_LINK, IN_LINK, "vendor/*", "**/lib"])
def test_p3_s2_ac3_glob_leaves_out_symlinks_and_submodules(tools, linked_repo, pattern):
    review = tools.review(linked_repo, [("glob", {"pattern": pattern})])

    [body] = review.bodies
    assert not set(body.split("\n")) & {OUT_LINK, IN_LINK, SUBMODULE}, body


def test_p3_s2_ac3_glob_double_star_still_lists_the_regular_files(tools, linked_repo):
    review = tools.review(linked_repo, [("glob", {"pattern": "**"})])

    [body] = review.bodies
    assert body.split("\n") == sorted(BASE_FILES)


def test_p3_s2_ac3_reading_a_submodule_is_refused_submodule(tools, linked_repo):
    review = tools.review(linked_repo, [("read", {"path": SUBMODULE}), READ_CHECKOUT])

    assert_refused_then_read_works(review, "submodule")


def test_p3_s2_ac3_grep_leaves_out_the_submodule(tools, linked_repo):
    # The submodule's commit holds the base files too; nothing under it may be listed.
    review = tools.review(linked_repo, [("grep", {"literal": "main"})])

    [body] = review.bodies
    assert not any(path.startswith(f"{SUBMODULE}") for path, _, _ in grep_lines(body)), body


# AC4: secrets files are denied, and left out of grep and glob.


MARKER = "MARKER-4f1c9e"
SECRETS_FILES = [
    ".env",
    "config/.env.production",
    "deploy/server.PEM",
    "id_rsa",
    ".npmrc",
    "credentials.json",
    ".git-credentials",
    "putty.ppk",
    ".htpasswd",
    ".pgpass",
    "infra/terraform.tfstate",
    "prod.tfvars",
    "vault.kdbx",
]
# design.md's deny-list, beyond the spec's examples, and case variants of the last segment.
DESIGN_SECRETS_FILES = [
    "keys/app.key",
    "certs/client.p12",
    "certs/client.pfx",
    "android/release.jks",
    "android/release.keystore",
    "id_dsa",
    "id_ecdsa",
    "home/id_ed25519",
    ".netrc",
    ".pypirc",
    "credentials",
    "aws/Credentials.INI",
    "config/.ENV.local",
    "ID_RSA",
]
PUBLIC_KEY = "id_ed25519.pub"
CONTROL = "docs/readme.txt"
# Names close to the deny-list that it doesn't cover: the rule is on the last segment only.
LOOKALIKES = ["pem.py", ".envrc", "env/settings.py", "credentials_test.py", "keys.txt"]


def marked(path: str) -> str:
    return f"value = '{MARKER}'  # {path}\n"


@pytest.fixture
def secrets_repo(tools: Tools) -> FixtureRepo:
    names = [*SECRETS_FILES, *DESIGN_SECRETS_FILES, PUBLIC_KEY, CONTROL, *LOOKALIKES]
    return tools.repo({name: marked(name) for name in names})


@pytest.mark.parametrize("path", SECRETS_FILES)
def test_p3_s2_ac4_reading_a_secrets_file_is_refused_denied_path(tools, secrets_repo, path):
    review = tools.review(secrets_repo, [("read", {"path": path}), READ_CHECKOUT])

    assert_refused_then_read_works(review, "denied_path")
    assert MARKER not in review.bodies[0]


@pytest.mark.parametrize("path", DESIGN_SECRETS_FILES)
def test_p3_s2_ac4_design_deny_list_is_refused_denied_path(tools, secrets_repo, path):
    review = tools.review(secrets_repo, [("read", {"path": path}), READ_CHECKOUT])

    assert_refused_then_read_works(review, "denied_path")


def test_p3_s2_ac4_grep_returns_no_line_from_a_secrets_file(tools, secrets_repo):
    review = tools.review(secrets_repo, [("grep", {"literal": MARKER})])

    [body] = review.bodies
    readable = sorted([PUBLIC_KEY, CONTROL, *LOOKALIKES])
    assert grep_lines(body) == [(path, 1, marked(path).rstrip("\n")) for path in readable]


def test_p3_s2_ac4_glob_double_star_does_not_list_a_secrets_file(tools, secrets_repo):
    review = tools.review(secrets_repo, [("glob", {"pattern": "**"})])

    [body] = review.bodies
    readable = [PUBLIC_KEY, CONTROL, *LOOKALIKES, *BASE_FILES]
    assert body.split("\n") == sorted(readable)


@pytest.mark.parametrize("pattern", [".env", "*.PEM", "**/.env.*", "id_*", "credentials*"])
def test_p3_s2_ac4_glob_aimed_at_secrets_files_lists_none(tools, secrets_repo, pattern):
    review = tools.review(secrets_repo, [("glob", {"pattern": pattern})])

    [body] = review.bodies
    listed = set(body.split("\n"))
    assert not listed & {*SECRETS_FILES, *DESIGN_SECRETS_FILES}, body


@pytest.mark.parametrize("path", [PUBLIC_KEY, *LOOKALIKES])
def test_p3_s2_ac4_public_key_and_lookalikes_are_readable(tools, secrets_repo, path):
    review = tools.review(secrets_repo, [("read", {"path": path})])

    [body] = review.bodies
    assert numbered(body) == [(1, marked(path).rstrip("\n"))]


# AC5: a file holding a secret is refused around it, and the key reaches nothing.


AWS_KEY = "AKIA" + "Q7XW3RT9LP2MN5VB"  # split, so this file holds no key
SETTINGS = "app/settings.py"
SETTINGS_LINES = [
    '"""Application settings."""',
    "import os",
    "DEBUG = False",
    "TIMEOUT_S = 30",
    "RETRIES = 3",
    'AWS_REGION = "eu-west-1"',
    f'AWS_ACCESS_KEY_ID = "{AWS_KEY}"',
    'AWS_PROFILE = "billing"',
    'LOG_LEVEL = "info"',
    "HOME = os.environ.get('HOME')",
    "PORT = 8080",
    "WORKERS = 4",
]
SETTINGS_TEXT = "".join(f"{line}\n" for line in SETTINGS_LINES)


@pytest.fixture
def settings_repo(tools: Tools) -> FixtureRepo:
    return tools.repo({SETTINGS: SETTINGS_TEXT})


def settings_window(first: int, count: int) -> list[tuple[int, str]]:
    return file_window({SETTINGS: SETTINGS_TEXT}, SETTINGS, first, count)


@pytest.mark.parametrize(
    ("start_line", "line_count"),
    [(None, None), (7, 1), (5, 5), (1, 7), (7, 400), (6, 2)],
    ids=["whole_file", "7_to_7", "5_to_9", "1_to_7", "7_onwards", "6_to_7"],
)
def test_p3_s2_ac5_window_overlapping_the_key_is_refused_possible_secret(
    tools, settings_repo, start_line, line_count
):
    arguments: dict[str, object] = {"path": SETTINGS}
    if start_line is not None:
        arguments |= {"start_line": start_line, "line_count": line_count}

    review = tools.review(settings_repo, [("read", arguments), READ_CHECKOUT])

    assert_refused_then_read_works(review, "possible_secret")
    assert AWS_KEY not in review.bodies[0]


def test_p3_s2_ac5_lines_1_to_5_are_returned(tools, settings_repo):
    review = tools.review(settings_repo, [("read", {"path": SETTINGS, "line_count": 5})])

    [body] = review.bodies
    assert numbered(body) == settings_window(1, 5)


def test_p3_s2_ac5_window_after_the_key_is_returned(tools, settings_repo):
    # design.md: only a window that overlaps a hit's span is refused.
    review = tools.review(settings_repo, [("read", {"path": SETTINGS, "start_line": 8})])

    [body] = review.bodies
    assert numbered(body) == settings_window(8, 5)


def test_p3_s2_ac5_grep_drops_the_key_line_and_notes_one_withheld(tools, settings_repo):
    review = tools.review(settings_repo, [("grep", {"literal": "AWS_"})])

    [body] = review.bodies
    assert grep_lines(body) == [(SETTINGS, 6, SETTINGS_LINES[5]), (SETTINGS, 8, SETTINGS_LINES[7])]
    notes = [line for line in body.split("\n") if line.strip() and line not in grep_text(body)]
    assert any("withheld" in note.lower() and re.search(r"\b1\b", note) for note in notes), body


def test_p3_s2_ac5_grep_for_the_key_itself_returns_nothing(tools, settings_repo):
    review = tools.review(settings_repo, [("grep", {"literal": AWS_KEY[:12]})])

    [body] = review.bodies
    assert grep_lines(body) == []
    assert AWS_KEY not in body


def grep_text(body: str) -> set[str]:
    return {f"{path}:{line}:{content}" for path, line, content in grep_lines(body)}


def test_p3_s2_ac5_refusal_is_recorded_possible_secret(tools, settings_repo):
    tools.review(settings_repo, [("read", {"path": SETTINGS})])

    [data] = tools.cli.tool_call_records()
    assert (data["outcome"], data["reason"]) == ("refused", "possible_secret")
    assert data["spans"] == []


def test_p3_s2_ac5_key_is_in_no_request_audit_record_or_report(tools, settings_repo):
    calls = [
        ("read", {"path": SETTINGS}),
        ("read", {"path": SETTINGS, "start_line": 5, "line_count": 5}),
        ("read", {"path": SETTINGS, "line_count": 5}),
        ("grep", {"literal": "AWS_"}),
        ("grep", {"literal": "AKIA"}),
        ("glob", {"pattern": "app/*.py"}),
    ]

    review = tools.review(settings_repo, calls, "--format", "json")

    assert review.exit_code == 1, review.stderr
    request = review.request
    sent = [request.system_prompt, request.user_prompt, *review.provider.tool_results]
    assert not [part for part in sent if AWS_KEY in part]
    assert AWS_KEY not in tools.cli.audit_log.read_text(encoding="utf-8")
    assert AWS_KEY not in review.stdout
    assert AWS_KEY not in review.stderr


# AC6: binary and non-UTF-8 files are refused, and grep doesn't search them.


BINARY_MARK = "BINARY-MARK-7d20"
NUL_FILE = "data/with_nul.dat"
LATIN1_FILE = "data/latin1.py"
TEXT_FILE = "data/plain.txt"


@pytest.fixture
def binary_repo(tools: Tools) -> FixtureRepo:
    return tools.repo(
        {
            NUL_FILE: Blob(f"{BINARY_MARK}\n".encode() + b"\x00\x01\x02\n"),
            LATIN1_FILE: Blob(f"{BINARY_MARK} = 'caf".encode() + b"\xe9'\n"),
            TEXT_FILE: f"{BINARY_MARK} in text\n",
        }
    )


@pytest.mark.parametrize("path", [NUL_FILE, LATIN1_FILE])
def test_p3_s2_ac6_reading_a_binary_or_non_utf8_file_is_refused_binary(tools, binary_repo, path):
    review = tools.review(binary_repo, [("read", {"path": path}), READ_CHECKOUT])

    assert_refused_then_read_works(review, "binary")
    assert BINARY_MARK not in review.bodies[0]


def test_p3_s2_ac6_grep_does_not_search_binary_or_non_utf8_files(tools, binary_repo):
    review = tools.review(binary_repo, [("grep", {"literal": BINARY_MARK})])

    [body] = review.bodies
    assert grep_lines(body) == [(TEXT_FILE, 1, f"{BINARY_MARK} in text")]


def test_p3_s2_ac6_file_with_valid_utf8_beyond_ascii_is_readable(tools):
    repo = tools.repo({"data/accents.py": "name = 'café ✓'\n"})

    review = tools.review(repo, [("read", {"path": "data/accents.py"})])

    [body] = review.bodies
    assert numbered(body) == [(1, "name = 'café ✓'")]


# AC7: blobs over 1 MB and listings over 50,000 entries are refused.


# "1 MB" is read as either 1,000,000 bytes or 1 MiB: over both, or at most both.
OVER_1_MB = 1_048_577
AT_1_MB = 1_000_000
ROW = "x" * 99 + "\n"


def rows(size: int) -> bytes:
    assert size % len(ROW) == 0
    return (ROW * (size // len(ROW))).encode()


def test_p3_s2_ac7_blob_over_1_mb_is_refused_too_large(tools):
    repo = tools.repo({"data/big.txt": Blob(b"y" * OVER_1_MB)})

    review = tools.review(repo, [("read", {"path": "data/big.txt"}), READ_CHECKOUT])

    assert_refused_then_read_works(review, "too_large")


def test_p3_s2_ac7_window_of_a_blob_over_1_mb_is_refused_too_large(tools):
    repo = tools.repo({"data/big.txt": Blob(b"y\n" * (OVER_1_MB // 2 + 1))})

    review = tools.review(repo, [("read", {"path": "data/big.txt", "line_count": 1})])

    assert_refused(review.bodies[0], "too_large")


def test_p3_s2_ac7_blob_of_exactly_1_000_000_bytes_is_readable(tools):
    repo = tools.repo({"data/edge.txt": Blob(rows(AT_1_MB))})

    review = tools.review(repo, [("read", {"path": "data/edge.txt", "line_count": 3})])

    [body] = review.bodies
    assert numbered(body) == [(n, ROW.rstrip("\n")) for n in (1, 2, 3)]


async def test_p3_s2_ac7_at_most_1_mb_plus_one_byte_of_a_large_blob_is_read(tools):
    # A whole read would hold 32 MB in memory; reading at most 1 MB + 1 byte stays far below.
    huge = 32 * 1024 * 1024
    repo = tools.repo({"data/huge.txt": Blob(b"z" * huge)})
    executor = repo_tools(repo)

    tracemalloc.start()
    try:
        result = await executor.call("read", {"path": "data/huge.txt"})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert_refused(unwrap(result), "too_large")
    assert peak < 8 * 1024 * 1024, f"peak memory {peak:,} bytes while refusing a {huge:,}-byte blob"


def flat_repo(path: Path, count: int) -> FixtureRepo:
    """`C` holding `count` files at the root, all the same blob, plus `keep.txt`."""
    repo = PlumbingRepo(path)
    old = repo.commit(as_entries({MAIN: BASE_FILES[MAIN]}))
    filler = Blob(b"filler\n")
    entries: dict[bytes, Entry] = {f"f{n:05}.txt".encode(): filler for n in range(count - 1)}
    entries[b"keep.txt"] = text("keep LISTING-MARK\n")
    return FixtureRepo(path, repo.commit(entries, parent=old), old)


@pytest.fixture(scope="module")
def over_listing(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepo:
    return flat_repo(tmp_path_factory.mktemp("over") / "repo", 50_001)


@pytest.fixture(scope="module")
def at_listing(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepo:
    return flat_repo(tmp_path_factory.mktemp("at") / "repo", 50_000)


@pytest.mark.parametrize(
    "call",
    [("glob", {"pattern": "keep.txt"}), ("grep", {"literal": "LISTING-MARK"})],
    ids=["glob", "grep"],
)
def test_p3_s2_ac7_listing_over_50000_entries_is_refused(tools, over_listing, call):
    started = time.monotonic()
    review = tools.review(over_listing, [call, ("read", {"path": "keep.txt"})])
    elapsed = time.monotonic() - started

    refused, after = review.bodies
    assert_refused(refused, "listing_too_large")
    assert numbered(after) == [(1, "keep LISTING-MARK")]
    assert_reported(review)
    assert elapsed < BOUND_S


def test_p3_s2_ac7_listing_of_exactly_50000_entries_is_searched(tools, at_listing):
    review = tools.review(
        at_listing, [("glob", {"pattern": "keep.txt"}), ("grep", {"literal": "LISTING-MARK"})]
    )

    listed, matched = review.bodies
    assert listed.split("\n") == ["keep.txt"]
    assert grep_lines(matched) == [("keep.txt", 1, "keep LISTING-MARK")]


# AC8: odd file names are listed C-quoted, never raw, and can't be read raw.


ODD_MARK = "ODD-MARK-3b8a"
ODD_NAMES = {
    b"odd/new\nline.py": '"odd/new\\nline.py"',
    b"odd/esc\x1bname.py": '"odd/esc\\033name.py"',
    b"odd/bell\x07.py": '"odd/bell\\a.py"',
    b"odd/caf\xff.py": '"odd/caf\\377.py"',
}
PLAIN_ODD = "odd/plain.py"


@pytest.fixture
def odd_repo(tools: Tools) -> FixtureRepo:
    files: dict[str | bytes, Entry | str] = dict.fromkeys(ODD_NAMES, f"{ODD_MARK}\n")
    files[PLAIN_ODD] = f"{ODD_MARK}\n"
    return tools.repo(files)


@pytest.mark.parametrize("pattern", ["odd/*", "**"])
def test_p3_s2_ac8_glob_lists_odd_names_in_c_quoted_form(tools, odd_repo, pattern):
    review = tools.review(odd_repo, [("glob", {"pattern": pattern})])

    [body] = review.bodies
    listed = set(body.split("\n"))
    assert listed >= {*ODD_NAMES.values(), PLAIN_ODD}, body


@pytest.mark.parametrize(
    "call",
    [("glob", {"pattern": "odd/*"}), ("glob", {"pattern": "**"}), ("grep", {"literal": ODD_MARK})],
    ids=["glob_odd", "glob_all", "grep"],
)
def test_p3_s2_ac8_no_raw_control_character_reaches_a_result(tools, odd_repo, call):
    review = tools.review(odd_repo, [call])

    [result] = review.provider.tool_results
    unwrap(result)
    assert not RAW_CONTROL.search(result), repr(result)
    assert "\ufffd" not in result, "a name that isn't UTF-8 was decoded lossily, not quoted"


def test_p3_s2_ac8_grep_lists_the_plain_name_beside_the_odd_ones(tools, odd_repo):
    review = tools.review(odd_repo, [("grep", {"literal": ODD_MARK})])

    [body] = review.bodies
    assert (PLAIN_ODD, 1, ODD_MARK) in grep_lines(body)


ODD_READS = {
    "raw_line_feed": "odd/new\nline.py",
    "raw_escape": "odd/esc\x1bname.py",
    "raw_bell": "odd/bell\x07.py",
    **{f"quoted_{n}": quoted for n, quoted in enumerate(ODD_NAMES.values())},
}


@pytest.mark.parametrize("path", ODD_READS.values(), ids=ODD_READS.keys())
def test_p3_s2_ac8_reading_an_odd_name_is_refused_invalid_path(tools, odd_repo, path):
    review = tools.review(odd_repo, [("read", {"path": path}), READ_CHECKOUT])

    assert_refused_then_read_works(review, "invalid_path")
    assert ODD_MARK not in review.bodies[0]


# AC9: a pathological literal is linear, and a git timeout refuses only that call.


@pytest.fixture(scope="module")
def regex_bomb_repo(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepo:
    path = tmp_path_factory.mktemp("bomb") / "repo"
    line = b"a" * (AT_1_MB - 1) + b"\n"  # one line of 1,000,000 bytes with its line feed
    return build_repo(
        path, {"data/aaaa.txt": Blob(line), "data/pattern.txt": 'pattern = "(a+)+$"\n'}
    )


def test_p3_s2_ac9_catastrophic_regex_literal_completes_and_is_literal(tools, regex_bomb_repo):
    started = time.monotonic()
    review = tools.review(regex_bomb_repo, [("grep", {"literal": "(a+)+$"})])
    elapsed = time.monotonic() - started

    [body] = review.bodies
    assert grep_lines(body) == [("data/pattern.txt", 1, 'pattern = "(a+)+$"')]
    assert tools.cli.outcomes() == ["ok"]
    assert elapsed < GIT_TIMEOUT_S


async def test_p3_s2_ac9_git_timeout_refuses_that_call_and_the_next_works(
    tools, repo, hanging_git_grep
):
    executor = repo_tools(repo, timeout_s=SHORT_TIMEOUT_S)

    started = time.monotonic()
    timed_out = await executor.call("grep", {"literal": "total("})
    elapsed = time.monotonic() - started
    after = await executor.call(*READ_CHECKOUT)

    assert hanging_git_grep, "no git grep was started, so nothing timed out"
    assert_refused(unwrap(timed_out), "timeout")
    assert reason_of(executor, 0) == "timeout"
    assert elapsed < SHORT_TIMEOUT_S + BOUND_S
    assert numbered(unwrap(after)) == file_window(BASE_FILES, CHECKOUT, 10, 5)


def test_p3_s2_ac9_git_timeout_in_a_review_is_refused_and_the_review_goes_on(
    tools, repo, hanging_git_grep, monkeypatch
):
    # Seam: the CLI builds its GitRepo with git's existing timeout, GIT_TIMEOUT_S.
    for module in ("review_bot.git", "review_bot.cli"):
        monkeypatch.setattr(f"{module}.GIT_TIMEOUT_S", SHORT_TIMEOUT_S, raising=False)

    review = tools.review(repo, [("grep", {"literal": "total("}), READ_CHECKOUT])

    assert hanging_git_grep, "no git grep was started, so nothing timed out"
    assert_refused_then_read_works(review, "timeout")
    assert tools.reasons() == ["timeout", None]


# AC10: grep's arguments end with -e literal, the commit, and a bare --.


def grep_argv(processes: Sequence[Sequence[str]]) -> list[str]:
    [argv] = [list(argv) for argv in processes if "grep" in argv]
    return argv


@pytest.mark.parametrize("path_glob", [None, "billing/*.py", "*.py", "**/checkout.py"])
async def test_p3_s2_ac10_grep_argv_is_literal_commit_then_bare_double_dash(
    tools, repo, git_processes, path_glob
):
    arguments: dict[str, object] = {"literal": "total("}
    if path_glob is not None:
        arguments["path_glob"] = path_glob
    executor = repo_tools(repo)
    git_processes.clear()

    await executor.call("grep", arguments)

    argv = grep_argv(git_processes)
    e = argv.index("-e")
    assert argv[e + 1 :] == ["total(", repo.commit, "--"], argv
    if path_glob is not None:
        assert not [arg for arg in argv if path_glob in arg], argv


@pytest.mark.parametrize(
    ("path_glob", "paths"),
    [("billing/*.py", [CHECKOUT, INVOICE]), ("checkout.py", [CHECKOUT]), ("lib/*.py", [])],
)
def test_p3_s2_ac10_path_glob_is_applied_to_the_results_in_our_code(
    tools, repo, git_processes, path_glob, paths
):
    review = tools.review(repo, [("grep", {"literal": "total(", "path_glob": path_glob})])

    [body] = review.bodies
    assert {path for path, _, _ in grep_lines(body)} == set(paths)
    assert grep_argv(git_processes)[-1] == "--"


# AC11: option-like literals are searched as plain text.


OPTION_LITERALS = ["-Ofoo", "--open-files-in-pager=x", "-f/etc/passwd", "--no-index"]
OUTSIDE_MARK = "OUTSIDE-COMMIT-6e1f"


@dataclass(frozen=True, slots=True)
class OptionFixture:
    repo: FixtureRepo
    canary_literal: str
    canary_trace: Path
    lines: dict[str, str]  # literal -> the fixture line holding it


@pytest.fixture
def option_fixture(tools: Tools, tmp_path: Path) -> OptionFixture:
    trace = tmp_path / "canary_ran"
    canary = tmp_path / "canary.sh"
    canary.write_bytes(f"#!/bin/sh\necho ran > '{trace.as_posix()}'\n".encode())
    canary.chmod(0o755)
    patterns = tmp_path / "patterns.txt"
    patterns.write_bytes(f"{OUTSIDE_MARK}\n".encode())
    canary_literal = f"-O{canary.as_posix()}"
    pattern_literal = f"-f{patterns.as_posix()}"
    literals = [*OPTION_LITERALS, canary_literal, pattern_literal]
    lines = {literal: f"flag {literal} here" for literal in literals}
    repo = tools.repo(
        {
            "data/flags.txt": "".join(f"{line}\n" for line in lines.values()),
            "data/outside.txt": f"{OUTSIDE_MARK}\n",
        }
    )
    # An untracked file: only --no-index, searching the working tree, would find it.
    (repo.path / "untracked.txt").write_bytes(
        "".join(f"untracked {literal}\n" for literal in literals).encode()
    )
    return OptionFixture(repo, canary_literal, trace, lines)


def literal_ids(literals: Sequence[str]) -> list[str]:
    return [re.sub(r"\W+", "_", literal).strip("_") for literal in literals]


@pytest.mark.parametrize("literal", OPTION_LITERALS, ids=literal_ids(OPTION_LITERALS))
def test_p3_s2_ac11_option_like_literal_is_found_as_plain_text(tools, option_fixture, literal):
    review = tools.review(option_fixture.repo, [("grep", {"literal": literal})])

    [body] = review.bodies
    assert grep_lines(body) == [
        ("data/flags.txt", line_number(option_fixture, literal), option_fixture.lines[literal])
    ]


def line_number(fixture: OptionFixture, literal: str) -> int:
    return list(fixture.lines).index(literal) + 1


def test_p3_s2_ac11_pager_option_runs_no_program(tools, option_fixture):
    literal = option_fixture.canary_literal
    assert len(literal) <= 200, "the canary literal is over the 200-character cap"

    review = tools.review(option_fixture.repo, [("grep", {"literal": literal})])

    [body] = review.bodies
    assert grep_lines(body) == [
        ("data/flags.txt", line_number(option_fixture, literal), option_fixture.lines[literal])
    ]
    assert not option_fixture.canary_trace.exists(), "the canary named by -O ran"


def test_p3_s2_ac11_pattern_file_option_reads_no_file_outside_the_commit(tools, option_fixture):
    literal = next(
        item for item in option_fixture.lines if item.startswith("-f") and "patterns" in item
    )
    assert len(literal) <= 200, "the pattern-file literal is over the 200-character cap"

    review = tools.review(option_fixture.repo, [("grep", {"literal": literal})])

    [body] = review.bodies
    # Had git read the file as patterns, data/outside.txt's line would match.
    assert grep_lines(body) == [
        ("data/flags.txt", line_number(option_fixture, literal), option_fixture.lines[literal])
    ]


def test_p3_s2_ac11_no_index_searches_only_the_commit(tools, option_fixture):
    review = tools.review(option_fixture.repo, [("grep", {"literal": "--no-index"})])

    [body] = review.bodies
    assert "untracked.txt" not in body
    assert {path for path, _, _ in grep_lines(body)} == {"data/flags.txt"}


# AC12: the tree entry's path must equal the requested path exactly.


NFC_NAME = unicodedata.normalize("NFC", "docs/café.txt")
NFD_NAME = unicodedata.normalize("NFD", "docs/café.txt")


@pytest.fixture
def exact_repo(tools: Tools) -> FixtureRepo:
    repo = tools.repo({NFC_NAME: "accented\n"})
    # As git sets on Windows and macOS; tree lookups must stay exact regardless.
    for key in ("core.ignorecase", "core.precomposeunicode"):
        subprocess.run(  # noqa: S603 - fixed git arguments, test-owned repository
            ["git", "-C", str(repo.path), "config", key, "true"],  # noqa: S607 - git from PATH
            check=True,
            capture_output=True,
        )
    return repo


@pytest.mark.parametrize(
    "path",
    [
        "billing/Invoice.py",
        "BILLING/invoice.py",
        "billing/invoice.PY",
        "billing/invoice.py ",
        " billing/invoice.py",
        "billing/invoice",
        "billing/invoice.py.",
        NFD_NAME,
    ],
    ids=[
        "file_case",
        "dir_case",
        "suffix_case",
        "trailing_space",
        "leading_space",
        "prefix",
        "trailing_dot",
        "nfd",
    ],
)
def test_p3_s2_ac12_path_differing_from_the_tree_entry_is_not_found(tools, exact_repo, path):
    review = tools.review(exact_repo, [("read", {"path": path}), READ_CHECKOUT])

    assert_refused_then_read_works(review, "not_found")
    assert lines_of(INVOICE_TEXT)[0] not in review.bodies[0]


def test_p3_s2_ac12_exact_path_is_read(tools, exact_repo):
    review = tools.review(exact_repo, [("read", {"path": NFC_NAME})])

    [body] = review.bodies
    assert numbered(body) == [(1, "accented")]


# AC13: a PEM private key block can't be read in slices.


PEM_FILE = "deploy/notes.txt"
PEM_LINE_15 = "Zm9vYmFyUEVNbGluZTE1dW5pcXVlVG9rZW5Yeg"
PEM_BEGIN = "-----BEGIN " + "RSA PRIVATE KEY-----"  # split, so this file holds no key block
PEM_END = "-----END " + "RSA PRIVATE KEY-----"
PEM_BODY = [
    "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0AHB7MaZxQ2r6Hn7E4mW",
    "q1vR2kUeT1mX0d5xGg9h4n3CwYkq0aRr8Wq7Q1fYxJpZ3m2N7bVvT6uUeLs9KcA",
    "a8fK2rM1pQwX7vY3nBzT5hLdC6gJ4eRuS0iO9yPkW2qVxE1tZ8mNjHcGb3oFlDs",
    "Hx4Rk9Tq2Wn7Lm5Bv8Cz1Xs6Yd3Fg0Jp4Ua7Ie2Ko9Ow5Qr8Ty1Ui3Pa6Sd0Gh",
    PEM_LINE_15,
    "Jk2Lm4Nb6Vc8Xz0As2Df4Gh6Jk8Lq0We2Rt4Yu6Io8Pa0Sd2Fg4Hj6Kl8Zx0Cv",
    "Bn2Mq4Wr6Et8Yi0Up2As4Dg6Fh8Jl0Kz2Xc4Vb6Nm8Qw0Er2Ty4Ui6Op8As0Df",
    "Gh2Jk4Ll6Zx8Cv0Bn2Mq4Ws6Ed8Rf0Tg2Yh4Uj6Ik8Ol0Pq2Aw4Se6Dr8Ft0Gy",
    "Hu2Ji4Ko6Lp8Za0Xs2Cd4Vf6Bg8Nh0Mj2Qk4Wl6Ep8Rz0Tx2Yc4Uv6Ib8On0Pm",
]
PEM_LINES = [
    *(f"note {n}" for n in range(1, 10)),
    PEM_BEGIN,
    *PEM_BODY,
    PEM_END,
    *(f"after {n}" for n in range(21, 26)),
]
PEM_TEXT = "".join(f"{line}\n" for line in PEM_LINES)
OTHER_FILE = "docs/other.txt"


@pytest.fixture
def pem_repo(tools: Tools) -> FixtureRepo:
    assert PEM_LINES[9] == PEM_BEGIN and PEM_LINES[14] == PEM_LINE_15 and PEM_LINES[19] == PEM_END
    return tools.repo({PEM_FILE: PEM_TEXT, OTHER_FILE: f"token {PEM_LINE_15}\n"})


@pytest.mark.parametrize(
    ("start_line", "line_count"),
    [(1, 12), (15, 1), (18, 13), (10, 1), (20, 1), (None, None)],
    ids=["1_to_12", "15_to_15", "18_to_30", "begin_line", "end_line", "whole_file"],
)
def test_p3_s2_ac13_window_overlapping_the_block_is_refused_possible_secret(
    tools, pem_repo, start_line, line_count
):
    arguments: dict[str, object] = {"path": PEM_FILE}
    if start_line is not None:
        arguments |= {"start_line": start_line, "line_count": line_count}

    review = tools.review(pem_repo, [("read", arguments), READ_CHECKOUT])

    assert_refused_then_read_works(review, "possible_secret")
    refused = review.bodies[0]
    assert not [line for line in PEM_BODY if line in refused]


def test_p3_s2_ac13_lines_1_to_9_are_returned(tools, pem_repo):
    review = tools.review(pem_repo, [("read", {"path": PEM_FILE, "line_count": 9})])

    [body] = review.bodies
    assert numbered(body) == file_window({PEM_FILE: PEM_TEXT}, PEM_FILE, 1, 9)


def test_p3_s2_ac13_grep_for_a_line_inside_the_block_returns_nothing_from_it(tools, pem_repo):
    review = tools.review(pem_repo, [("grep", {"literal": PEM_LINE_15})])

    [body] = review.bodies
    assert grep_lines(body) == [(OTHER_FILE, 1, f"token {PEM_LINE_15}")]


@pytest.mark.parametrize("literal", ["PRIVATE KEY", "-----"])
def test_p3_s2_ac13_grep_for_the_block_markers_returns_nothing_from_it(tools, pem_repo, literal):
    review = tools.review(pem_repo, [("grep", {"literal": literal})])

    [body] = review.bodies
    assert not [entry for entry in grep_lines(body) if entry[0] == PEM_FILE], body
    assert not [line for line in PEM_BODY if line in body]


def test_p3_s2_ac13_block_is_in_no_request_audit_record_or_report(tools, pem_repo):
    calls = [
        ("read", {"path": PEM_FILE, "start_line": 15, "line_count": 1}),
        ("read", {"path": PEM_FILE, "start_line": 18, "line_count": 13}),
        ("grep", {"literal": PEM_LINE_15}),
        ("grep", {"literal": "Jk2Lm4Nb6Vc8"}),
    ]

    review = tools.review(pem_repo, calls, "--format", "json")

    sent = "\n".join(review.provider.tool_results)
    log = tools.cli.audit_log.read_text(encoding="utf-8")
    # PEM_LINE_15 is left out: docs/other.txt holds it too, and that line may be returned.
    for line in [line for line in PEM_BODY if line != PEM_LINE_15]:
        assert line not in sent
        assert line not in log
        assert line not in review.stdout


# AC14: a path in which the secret scan finds a key is never shown.


SECRET_PATH = f"cfg_{AWS_KEY}.txt"
SECRET_DIR_PATH = f"{AWS_KEY}/notes.txt"
PATH_MARK = "PATH-SECRET-MARK-0a9d"
PATH_CONTROL = "docs/visible.txt"


@pytest.fixture
def secret_path_repo(tools: Tools) -> FixtureRepo:
    return tools.repo(
        {
            SECRET_PATH: f"{PATH_MARK} in the file\n",
            SECRET_DIR_PATH: f"{PATH_MARK} in the directory\n",
            PATH_CONTROL: f"{PATH_MARK} visible\n",
        }
    )


@pytest.mark.parametrize("pattern", ["**", "*.txt", "cfg_*", "**/notes.txt"])
def test_p3_s2_ac14_glob_leaves_out_a_path_holding_a_secret(tools, secret_path_repo, pattern):
    review = tools.review(secret_path_repo, [("glob", {"pattern": pattern})])

    [result] = review.provider.tool_results
    body = unwrap(result)
    assert AWS_KEY not in result
    assert not [line for line in body.split("\n") if "AKIA" in line], body


def test_p3_s2_ac14_glob_double_star_lists_exactly_the_other_files(tools, secret_path_repo):
    review = tools.review(secret_path_repo, [("glob", {"pattern": "**"})])

    [body] = review.bodies
    assert body.split("\n") == sorted([*BASE_FILES, PATH_CONTROL])


def test_p3_s2_ac14_grep_leaves_out_a_path_holding_a_secret(tools, secret_path_repo):
    review = tools.review(secret_path_repo, [("grep", {"literal": PATH_MARK})])

    [result] = review.provider.tool_results
    assert grep_lines(unwrap(result)) == [(PATH_CONTROL, 1, f"{PATH_MARK} visible")]
    assert AWS_KEY not in result


@pytest.mark.parametrize("path", [SECRET_PATH, SECRET_DIR_PATH], ids=["file", "directory"])
def test_p3_s2_ac14_read_is_refused_possible_secret_without_echoing_the_path(
    tools, secret_path_repo, path
):
    review = tools.review(secret_path_repo, [("read", {"path": path}), READ_CHECKOUT])

    assert_refused_then_read_works(review, "possible_secret")
    refused = review.provider.tool_results[0]
    assert AWS_KEY not in refused
    assert PATH_MARK not in refused


def test_p3_s2_ac14_path_and_key_are_in_no_result_report_or_audit(tools, secret_path_repo):
    calls = [
        ("glob", {"pattern": "**"}),
        ("grep", {"literal": PATH_MARK}),
        ("grep", {"literal": "cfg_"}),
        ("read", {"path": SECRET_PATH}),
        ("read", {"path": SECRET_DIR_PATH}),
        ("read", {"path": PATH_CONTROL}),
    ]

    review = tools.review(secret_path_repo, calls, "--format", "json")

    assert review.exit_code == 1, review.stderr
    request = review.request
    sent = [request.system_prompt, request.user_prompt, *review.provider.tool_results]
    log = tools.cli.audit_log.read_text(encoding="utf-8")
    for leaked in (AWS_KEY, SECRET_PATH, SECRET_DIR_PATH):
        assert not [part for part in sent if leaked in part], leaked
        assert leaked not in log, leaked
        assert leaked not in review.stdout, leaked
        assert leaked not in review.stderr, leaked
    files_read = str(report_of(review)["context"]["files_read"])
    assert PATH_CONTROL in files_read
    assert "AKIA" not in files_read


# AC15: one grep call reads at most 100 candidate files.


MAX_CANDIDATES = 100
LATIN1_COUNT = 20_000
CANDIDATE_MARK = "CANDIDATE-MARK-5c3e"
UTF8_CANDIDATE = "a_utf8.txt"  # sorts before every Latin-1 file
LAST_UTF8_CANDIDATE = "z_utf8.txt"  # sorts after every Latin-1 file
# "Bounded by the 100 files, not by the repository": a few git processes per file read, plus a
# few for the listing and the search, stays far below one per file of the 20,000.
GIT_PROCESSES_PER_FILE = 5
FIXED_GIT_PROCESSES = 10


@pytest.fixture(scope="module")
def candidates_repo(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepo:
    """20,000 distinct Latin-1 files holding the literal, and a UTF-8 file before and after."""
    path = tmp_path_factory.mktemp("candidates") / "repo"
    repo = PlumbingRepo(path)
    old = repo.commit(as_entries({MAIN: BASE_FILES[MAIN]}))
    latin1 = [f"{CANDIDATE_MARK} {n} caf".encode() + b"\xe9\n" for n in range(LATIN1_COUNT)]
    repo.write_blobs(latin1)
    entries: dict[bytes, Entry] = {
        f"l{n:05}.txt".encode(): Blob(data) for n, data in enumerate(latin1)
    }
    entries[UTF8_CANDIDATE.encode()] = text(f"{CANDIDATE_MARK} café\n")
    entries[LAST_UTF8_CANDIDATE.encode()] = text(f"{CANDIDATE_MARK} last\n")
    return FixtureRepo(path, repo.commit(entries, parent=old), old)


@pytest.fixture
def read_file_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Paths passed to `GitRepo.read_file`, if the tools read files through it."""
    paths: list[str] = []
    original = GitRepo.read_file

    def read_file(self: GitRepo, commit: str, path: str, *, max_bytes: int) -> bytes | None:
        paths.append(path)
        return original(self, commit, path, max_bytes=max_bytes)

    monkeypatch.setattr(GitRepo, "read_file", read_file)
    return paths


def notes_of(body: str) -> list[str]:
    return [line for line in body.split("\n") if line.strip() and line not in grep_text(body)]


async def test_p3_s2_ac15_one_grep_starts_git_bounded_by_100_files(
    candidates_repo, git_processes, read_file_calls
):
    executor = repo_tools(candidates_repo)
    git_processes.clear()

    started = time.monotonic()
    result = await executor.call("grep", {"literal": CANDIDATE_MARK})
    elapsed = time.monotonic() - started

    unwrap(result)
    bound = MAX_CANDIDATES * GIT_PROCESSES_PER_FILE + FIXED_GIT_PROCESSES
    assert len(git_processes) <= bound, len(git_processes)
    assert len(read_file_calls) <= MAX_CANDIDATES, len(read_file_calls)
    assert len(set(read_file_calls)) == len(read_file_calls), "a file was read twice"
    assert elapsed < BOUND_S


async def test_p3_s2_ac15_grep_over_100_candidates_is_truncated_with_a_note(candidates_repo):
    executor = repo_tools(candidates_repo)

    body = unwrap(await executor.call("grep", {"literal": CANDIDATE_MARK}))

    assert grep_lines(body) == [(UTF8_CANDIDATE, 1, f"{CANDIDATE_MARK} café")]
    assert executor.ledger[-1].outcome == "truncated"
    assert notes_of(body), f"the truncated result has no note: {body!r}"


async def test_p3_s2_ac15_withheld_count_covers_only_the_files_read(candidates_repo):
    executor = repo_tools(candidates_repo)

    body = unwrap(await executor.call("grep", {"literal": CANDIDATE_MARK}))

    notes = " ".join(notes_of(body)).replace(",", "")
    counts = [int(number) for number in re.findall(r"\d+", notes)]
    assert all(count <= MAX_CANDIDATES for count in counts), notes


async def test_p3_s2_ac15_candidates_are_counted_after_path_glob(candidates_repo):
    executor = repo_tools(candidates_repo)

    body = unwrap(
        await executor.call("grep", {"literal": CANDIDATE_MARK, "path_glob": UTF8_CANDIDATE})
    )

    assert grep_lines(body) == [(UTF8_CANDIDATE, 1, f"{CANDIDATE_MARK} café")]


async def test_p3_s2_ac15_file_past_100_candidates_is_found_when_path_glob_names_it(
    candidates_repo,
):
    # Counted before path_glob, the call would stop after 100 Latin-1 files and never reach it.
    executor = repo_tools(candidates_repo)

    body = unwrap(
        await executor.call("grep", {"literal": CANDIDATE_MARK, "path_glob": LAST_UTF8_CANDIDATE})
    )

    assert grep_lines(body) == [(LAST_UTF8_CANDIDATE, 1, f"{CANDIDATE_MARK} last")]


async def test_p3_s2_ac15_path_glob_with_few_candidates_reads_few_files(
    candidates_repo, git_processes
):
    # l0000*.txt matches 10 Latin-1 files, each refused as not UTF-8.
    executor = repo_tools(candidates_repo)
    git_processes.clear()

    body = unwrap(
        await executor.call("grep", {"literal": CANDIDATE_MARK, "path_glob": "l0000*.txt"})
    )

    assert grep_lines(body) == []
    bound = 10 * GIT_PROCESSES_PER_FILE + FIXED_GIT_PROCESSES
    assert len(git_processes) <= bound, len(git_processes)


# Limits: the secret scan is linear, and a large commit stays bounded.


def test_p3_s2_limits_secret_scan_of_a_near_1_mb_blob_is_bounded(tools):
    # Many near-miss key prefixes and PEM-like headers, to stress a backtracking scan.
    near_misses = ("AKIA" + "Q7XW3RT9" + " -----BEGIN RSA PRIVATE\n") * 25_000
    assert len(near_misses.encode()) < AT_1_MB
    repo = tools.repo({"data/noisy.txt": near_misses})

    started = time.monotonic()
    review = tools.review(
        repo,
        [("read", {"path": "data/noisy.txt", "line_count": 2}), ("grep", {"literal": "AKIA"})],
    )
    elapsed = time.monotonic() - started

    assert len(review.bodies) == 2
    assert elapsed < BOUND_S


def test_p3_s2_limits_digest_of_inputs_is_recorded_not_the_paths(tools, settings_repo):
    # ADR 0005: the audit log holds the input's SHA-256, never the model's arguments as text.
    tools.review(settings_repo, [("read", {"path": "../" + SETTINGS})])

    [data] = tools.cli.tool_call_records()
    assert data["reason"] == "invalid_path"
    assert re.fullmatch(r"[0-9a-f]{64}", data["input_sha256"])
    assert "../app/settings.py" not in tools.cli.audit_log.read_text(encoding="utf-8")
