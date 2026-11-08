"""The tools on hostile paths and repositories: every fixture is built with git plumbing, so
links, submodules and odd names need no support from the file system.

No realistic secret may be committed, even here, so every secret is assembled at run time.
"""

import asyncio
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
from git_samples import commit, git, init_repo

from review_bot.context import commit_files
from review_bot.context.ledger import LineSpan, RefusalReason, ToolOutcome
from review_bot.context.tools import RepoRef, RepoTools, ToolCaps
from review_bot.errors import GitError, GitTimeoutError, ListingTooLargeError
from review_bot.git import GitRepo
from review_bot.secret_scan import SecretSpan

CAPS = ToolCaps(max_calls=30, max_result_bytes=16_000, max_total_bytes=400_000)
AWS_KEY = "AK" + "IA" + "ABCDEFGH" + "23456789"
PEM_BEGIN = "-----BEGIN " + "RSA PRIVATE KEY-----"
PEM_END = "-----END " + "RSA PRIVATE KEY-----"
INVOICE = "def total():\n    return 1\n"
OBJECT_TYPES = {"040000": "tree", "160000": "commit"}  # by mode; any other mode is a blob


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def make_tools(repo: Path, sha: str) -> RepoTools:
    return RepoTools(RepoRef(git=GitRepo(repo, timeout_s=60), commit=sha), caps=CAPS)


def call(tools: RepoTools, name: str, arguments: Mapping[str, object]) -> str:
    """The result's text, between its delimiter lines."""
    framed = asyncio.run(tools.call(name, arguments))
    return "\n".join(framed.split("\n")[1:-1])


def git_bytes(repo: Path, args: Sequence[str], stdin: bytes) -> str:
    completed = subprocess.run(  # noqa: S603 - a fixed argument list, no shell
        ["git", "-C", str(repo), *args],  # noqa: S607 - git from PATH, as the code under test
        input=stdin,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii").strip()


def tree_commit(repo: Path, entries: Sequence[tuple[str, str, bytes]]) -> str:
    """A commit whose root tree holds ``entries``: each a mode, an object's SHA and a raw name."""
    records = b"".join(
        f"{mode} {OBJECT_TYPES.get(mode, 'blob')} {sha}\t".encode() + name + b"\0"
        for mode, sha, name in entries
    )
    tree = git_bytes(repo, ["mktree", "-z"], records)
    return git(repo, "commit-tree", tree, "-m", "plumbing")


def blob(repo: Path, content: bytes) -> str:
    return git_bytes(repo, ["hash-object", "-w", "--stdin"], content)


def refused_reason(text: str) -> str:
    assert text.startswith("refused: "), text
    return text.split(": ")[1]


def fail_if_called(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("git ran")


@pytest.mark.parametrize(
    "path",
    [
        "../x",
        "/etc/passwd",
        "C:/x",
        "billing\\invoice.py",
        "a//b",
        "./a",
        "a/./b",
        "a/../b",
        "",
        "a\x00b",
        "a\nb",
        "a\x1bb",
        "a" * 1001,
        "billing/",
    ],
)
def test_read_of_an_invalid_path_is_refused_before_git_runs(
    repo: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    tools = make_tools(repo, commit(repo, {"a.py": "x\n"}))
    monkeypatch.setattr(GitRepo, "read_file", fail_if_called)

    assert refused_reason(call(tools, "read", {"path": path})) == "invalid_path"
    assert tools.ledger[-1].reason is RefusalReason.INVALID_PATH


@pytest.mark.parametrize("glob", ["../*", "a/../*", "/etc/*", ":(top)*", ":!x", "billing\\*"])
@pytest.mark.parametrize("tool", ["glob", "grep"])
def test_an_invalid_glob_is_refused_before_git_runs(
    repo: Path, monkeypatch: pytest.MonkeyPatch, glob: str, tool: str
) -> None:
    tools = make_tools(repo, commit(repo, {"a.py": "x\n"}))
    monkeypatch.setattr(GitRepo, "list_files", fail_if_called)
    monkeypatch.setattr(GitRepo, "grep", fail_if_called)
    arguments = {"pattern": glob} if tool == "glob" else {"literal": "x", "path_glob": glob}

    assert refused_reason(call(tools, tool, arguments)) == "invalid_path"


@pytest.mark.parametrize("path", [".git/config", "sub/.git/HEAD", ".GIT/config"])
def test_read_under_git_is_denied(repo: Path, path: str) -> None:
    tools = make_tools(repo, commit(repo, {"a.py": "x\n"}))

    assert refused_reason(call(tools, "read", {"path": path})) == "denied_path"


@pytest.fixture
def linked(repo: Path) -> RepoTools:
    """A commit with ``out`` linking outside it, ``in`` linking inside it, and a submodule."""
    inner = commit(repo, {"billing/invoice.py": INVOICE})
    billing = git(repo, "rev-parse", f"{inner}:billing")
    sha = tree_commit(
        repo,
        [
            ("040000", billing, b"billing"),
            ("120000", blob(repo, b"../../etc/passwd"), b"out"),
            ("120000", blob(repo, b"billing/invoice.py"), b"in"),
            ("160000", inner, b"sub"),
        ],
    )
    return make_tools(repo, sha)


@pytest.mark.parametrize(
    ("path", "reason"), [("out", "symlink"), ("in", "symlink"), ("sub", "submodule")]
)
def test_read_of_a_link_or_submodule_is_refused(linked: RepoTools, path: str, reason: str) -> None:
    text = call(linked, "read", {"path": path})

    assert refused_reason(text) == reason
    assert "passwd" not in text
    assert "def total" not in text


def test_links_and_submodules_are_left_out_of_grep_and_glob(linked: RepoTools) -> None:
    assert call(linked, "glob", {"pattern": "**"}) == "billing/invoice.py"
    assert call(linked, "grep", {"literal": "passwd"}) == "No line matches."
    assert call(linked, "grep", {"literal": "invoice.py"}) == "No line matches."


SECRETS_FILES = (
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
)


@pytest.fixture
def secrets_files(repo: Path) -> RepoTools:
    files = dict.fromkeys(SECRETS_FILES, "MARKER\n") | {
        "id_ed25519.pub": "MARKER public\n",
        "app.py": "MARKER app\n",
    }
    return make_tools(repo, commit(repo, files))


@pytest.mark.parametrize("path", SECRETS_FILES)
def test_read_of_a_secrets_file_is_denied(secrets_files: RepoTools, path: str) -> None:
    assert refused_reason(call(secrets_files, "read", {"path": path})) == "denied_path"


def test_secrets_files_are_left_out_of_grep_and_glob(secrets_files: RepoTools) -> None:
    assert call(secrets_files, "grep", {"literal": "MARKER"}) == (
        "app.py:1:MARKER app\nid_ed25519.pub:1:MARKER public"
    )
    assert call(secrets_files, "glob", {"pattern": "**"}) == "app.py\nid_ed25519.pub"


def test_a_public_key_is_readable(secrets_files: RepoTools) -> None:
    assert call(secrets_files, "read", {"path": "id_ed25519.pub"}) == "1\tMARKER public"


@pytest.fixture
def settings_with_key(repo: Path) -> RepoTools:
    lines = [f"line {number} hit" for number in range(1, 11)]
    lines[6] = f"KEY = '{AWS_KEY}'  # hit"
    return make_tools(repo, commit(repo, {"app/settings.py": "\n".join(lines) + "\n"}))


@pytest.mark.parametrize(("start", "count"), [(1, 200), (7, 1), (5, 5), (6, 2)])
def test_read_of_a_window_over_a_secret_is_refused(
    settings_with_key: RepoTools, start: int, count: int
) -> None:
    text = call(
        settings_with_key,
        "read",
        {"path": "app/settings.py", "start_line": start, "line_count": count},
    )

    assert refused_reason(text) == "possible_secret"
    assert AWS_KEY not in text


def test_read_of_a_window_clear_of_a_secret_is_returned(settings_with_key: RepoTools) -> None:
    text = call(settings_with_key, "read", {"path": "app/settings.py", "line_count": 5})

    assert text.split("\n") == [f"{number}\tline {number} hit" for number in range(1, 6)]


def test_grep_withholds_a_line_holding_a_secret_and_says_so(settings_with_key: RepoTools) -> None:
    text = call(settings_with_key, "grep", {"literal": "hit", "path_glob": "app/*.py"})

    lines = text.split("\n")
    assert "app/settings.py:6:line 6 hit" in lines
    assert "app/settings.py:8:line 8 hit" in lines
    assert not any(line.startswith("app/settings.py:7:") for line in lines)
    assert lines[-1] == "(1 matching line in the files read withheld: it may hold a secret)"
    assert AWS_KEY not in text
    assert LineSpan("app/settings.py", 7, 7) not in settings_with_key.ledger[-1].spans


def test_grep_with_every_match_withheld_says_only_that(settings_with_key: RepoTools) -> None:
    text = call(settings_with_key, "grep", {"literal": "KEY ="})

    assert text == "(1 matching line in the files read withheld: it may hold a secret)"
    assert settings_with_key.ledger[-1].outcome is ToolOutcome.OK


def test_grep_counts_several_withheld_lines(repo: Path) -> None:
    text = "".join(f"key{number} = {AWS_KEY}\n" for number in range(3)) + "key ok\n"
    tools = make_tools(repo, commit(repo, {"a.py": text}))

    assert call(tools, "grep", {"literal": "key"}) == (
        "a.py:4:key ok\n(3 matching lines in the files read withheld: they may hold a secret)"
    )


@pytest.fixture
def private_key(repo: Path) -> RepoTools:
    lines = [f"line {number}" for number in range(1, 31)]
    lines[9] = PEM_BEGIN
    lines[14] = "line 15 needle"
    lines[19] = PEM_END
    return make_tools(repo, commit(repo, {"keys.txt": "\n".join(lines) + "\n"}))


@pytest.mark.parametrize(("start", "count"), [(1, 12), (15, 1), (18, 13)])
def test_read_of_any_slice_of_a_private_key_block_is_refused(
    private_key: RepoTools, start: int, count: int
) -> None:
    arguments = {"path": "keys.txt", "start_line": start, "line_count": count}

    assert refused_reason(call(private_key, "read", arguments)) == "possible_secret"


def test_read_before_a_private_key_block_is_returned(private_key: RepoTools) -> None:
    text = call(private_key, "read", {"path": "keys.txt", "line_count": 9})

    assert text.split("\n")[-1] == "9\tline 9"


def test_grep_returns_nothing_from_inside_a_private_key_block(private_key: RepoTools) -> None:
    text = call(private_key, "grep", {"literal": "needle"})

    assert text == "(1 matching line in the files read withheld: it may hold a secret)"


def test_each_file_is_scanned_for_secrets_once(
    private_key: RepoTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanned: list[str] = []
    real_scan = commit_files.secret_line_spans

    def counting_scan(text: str) -> tuple[SecretSpan, ...]:
        scanned.append(text)
        return real_scan(text)

    monkeypatch.setattr(commit_files, "secret_line_spans", counting_scan)

    call(private_key, "read", {"path": "keys.txt", "line_count": 5})
    call(private_key, "read", {"path": "keys.txt", "start_line": 25})
    call(private_key, "grep", {"literal": "line 2"})

    assert len(scanned) == 1


@pytest.mark.parametrize("content", [b"text\x00more\n", b"caf\xff\n"])
def test_a_file_that_is_not_utf8_text_is_refused_and_not_searched(
    repo: Path, content: bytes
) -> None:
    tools = make_tools(repo, commit(repo, {"data.bin": content, "a.py": "more caf\n"}))

    assert refused_reason(call(tools, "read", {"path": "data.bin"})) == "binary"
    assert call(tools, "grep", {"literal": "caf"}) == "a.py:1:more caf"


def test_grep_leaves_out_a_file_over_1_mb(repo: Path) -> None:
    tools = make_tools(repo, commit(repo, {"huge.txt": "z" * 1_000_001 + "\nneedle\n"}))

    assert call(tools, "grep", {"literal": "needle"}) == "No line matches."


@pytest.mark.parametrize(
    ("tool", "arguments"), [("glob", {"pattern": "**"}), ("grep", {"literal": "x"})]
)
def test_a_listing_too_large_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: Mapping[str, object]
) -> None:
    tools = make_tools(repo, commit(repo, {"a.py": "x\n"}))

    def too_large(*_args: object, **_kwargs: object) -> None:
        raise ListingTooLargeError("Commit lists more than 50000 files.")

    monkeypatch.setattr(GitRepo, "list_files", too_large)

    assert refused_reason(call(tools, tool, arguments)) == "listing_too_large"


@pytest.fixture
def odd_names(repo: Path) -> RepoTools:
    content = blob(repo, b"odd\n")
    names = [b"line\nfeed.py", b"esc\x1b[31m.py", b"caf\xff.py", b"plain.py"]
    return make_tools(repo, tree_commit(repo, [("100644", content, name) for name in names]))


def test_glob_lists_odd_names_c_quoted(odd_names: RepoTools) -> None:
    text = call(odd_names, "glob", {"pattern": "**"})

    assert text.split("\n") == [
        '"caf\\377.py"',
        '"esc\\033[31m.py"',
        '"line\\nfeed.py"',
        "plain.py",
    ]


@pytest.mark.parametrize("path", ["line\nfeed.py", "esc\x1b[31m.py", "caf\udcff.py"])
def test_read_of_an_odd_name_is_refused_invalid_path(odd_names: RepoTools, path: str) -> None:
    assert refused_reason(call(odd_names, "read", {"path": path})) == "invalid_path"


def test_grep_shows_no_raw_control_character_from_an_odd_name(odd_names: RepoTools) -> None:
    text = call(odd_names, "grep", {"literal": "odd"})

    assert text == "plain.py:1:odd"


@pytest.mark.parametrize("path", ["billing/Invoice.py", "BILLING/invoice.py", "billing/invoice"])
def test_read_needs_the_exact_tree_entry_path(repo: Path, path: str) -> None:
    tools = make_tools(repo, commit(repo, {"billing/invoice.py": INVOICE}))

    assert refused_reason(call(tools, "read", {"path": path})) == "not_found"


@pytest.mark.parametrize(
    ("method", "error", "reason"),
    [
        ("read_file", GitTimeoutError("git ls-tree took longer than 60 seconds."), "timeout"),
        ("read_file", GitError("git ls-tree failed with exit code 128."), "git_failed"),
        ("list_files", GitTimeoutError("git ls-tree took longer than 60 seconds."), "timeout"),
        ("grep", GitTimeoutError("git grep took longer than 60 seconds."), "timeout"),
    ],
)
def test_a_git_failure_refuses_that_call_and_the_review_goes_on(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    error: GitError,
    reason: str,
) -> None:
    tools = make_tools(repo, commit(repo, {"a.py": "x\n"}))
    real: Callable[..., object] = getattr(GitRepo, method)
    calls = iter([error])

    def fails_once(*args: object, **kwargs: object) -> object:
        failure = next(calls, None)
        if failure is not None:
            raise failure
        return real(*args, **kwargs)

    monkeypatch.setattr(GitRepo, method, fails_once)
    tool, arguments = {
        "read_file": ("read", {"path": "a.py"}),
        "list_files": ("glob", {"pattern": "*.py"}),
        "grep": ("grep", {"literal": "x"}),
    }[method]

    assert refused_reason(call(tools, tool, arguments)) == reason
    assert call(tools, tool, arguments).startswith(("1\t", "a.py"))


SECRET_PATH = f"cfg_{AWS_KEY}.txt"


@pytest.fixture
def secret_named(repo: Path) -> RepoTools:
    files = {SECRET_PATH: "needle in a secret-named file\n", "app.py": "needle\n"}
    return make_tools(repo, commit(repo, files))


def test_a_path_holding_a_possible_secret_is_left_out_of_glob_and_grep(
    secret_named: RepoTools,
) -> None:
    assert call(secret_named, "glob", {"pattern": "**"}) == "app.py"
    assert call(secret_named, "grep", {"literal": "needle"}) == "app.py:1:needle"
    assert call(secret_named, "grep", {"literal": "secret-named"}) == "No line matches."


def test_read_of_a_path_holding_a_possible_secret_is_refused_without_echoing_it(
    secret_named: RepoTools,
) -> None:
    text = call(secret_named, "read", {"path": SECRET_PATH})

    assert refused_reason(text) == "possible_secret"
    assert AWS_KEY not in text
    assert "cfg_" not in text
    assert secret_named.ledger[-1].spans == ()
    assert secret_named.ledger[-1].listed == ()


def test_a_path_holding_a_possible_secret_is_in_no_ledger_entry(secret_named: RepoTools) -> None:
    call(secret_named, "read", {"path": SECRET_PATH})
    call(secret_named, "glob", {"pattern": "**"})
    call(secret_named, "grep", {"literal": "needle"})

    recorded = [
        *(span.path for entry in secret_named.ledger for span in entry.spans),
        *(path for entry in secret_named.ledger for path in entry.listed),
    ]
    assert recorded
    assert not any(AWS_KEY in path for path in recorded)


@pytest.fixture
def many_candidates(repo: Path) -> RepoTools:
    # 120 files with a match, sorted before the one that would show: a binary-free
    # Latin-1 file is a candidate that is read and then left out.
    files: dict[str, str | bytes] = {
        f"a{number:03}.txt": b"needle caf\xe9\n" for number in range(120)
    }
    files["z.txt"] = "needle\n"
    return make_tools(repo, commit(repo, files))


def test_grep_reads_at_most_100_candidate_files_and_says_it_stopped(
    many_candidates: RepoTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[str] = []
    real_read_file = GitRepo.read_file

    def counting_read_file(repo: GitRepo, commit: str, path: str, *, max_bytes: int) -> object:
        read.append(path)
        return real_read_file(repo, commit, path, max_bytes=max_bytes)

    monkeypatch.setattr(GitRepo, "read_file", counting_read_file)

    text = call(many_candidates, "grep", {"literal": "needle"})

    assert len(read) == 100
    assert text.startswith("(truncated: more lines match than are shown")
    assert many_candidates.ledger[-1].outcome is ToolOutcome.TRUNCATED


def test_grep_reads_only_candidates_that_pass_path_glob(
    many_candidates: RepoTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[str] = []
    real_read_file = GitRepo.read_file

    def counting_read_file(repo: GitRepo, commit: str, path: str, *, max_bytes: int) -> object:
        read.append(path)
        return real_read_file(repo, commit, path, max_bytes=max_bytes)

    monkeypatch.setattr(GitRepo, "read_file", counting_read_file)

    text = call(many_candidates, "grep", {"literal": "needle", "path_glob": "z.txt"})

    assert read == ["z.txt"]
    assert text == "z.txt:1:needle"


def test_the_commit_is_listed_once_per_review(
    secrets_files: RepoTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    listings: list[str] = []
    real_list_files = GitRepo.list_files

    def counting_list_files(repo: GitRepo, commit: str, *, max_entries: int) -> object:
        listings.append(commit)
        return real_list_files(repo, commit, max_entries=max_entries)

    monkeypatch.setattr(GitRepo, "list_files", counting_list_files)

    first = call(secrets_files, "glob", {"pattern": "**"})
    call(secrets_files, "grep", {"literal": "MARKER"})

    assert call(secrets_files, "glob", {"pattern": "**"}) == first
    assert len(listings) == 1
