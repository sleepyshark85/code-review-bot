"""Unit tests for building a case's fixture repository: one commit, the same on every build."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from review_bot.errors import GitError, GitTimeoutError
from review_bot.evals import fixture_repo
from review_bot.evals.fixture_repo import build_fixture_repo
from review_bot.evals.formats import FixtureFile

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
FILES = (
    FixtureFile("billing/checkout.py", b"def charge(total):\n    return total\n"),
    FixtureFile("bin/run.sh", b"echo hi\n", is_executable=True),
    FixtureFile('odd "name"\twith\\tab.txt', b"odd\n"),
)


def git(directory: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed git arguments in a test
        ["git", "-C", str(directory), *args],  # noqa: S607 - git from PATH
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    )
    return completed.stdout.strip()


def test_build_fixture_repo_commits_exactly_the_files_with_their_modes(tmp_path: Path) -> None:
    built = build_fixture_repo(FILES, tmp_path / "repo")

    entries = built.repo.git.list_files(built.repo.commit, max_entries=10)

    assert [(entry.path, entry.mode) for entry in entries] == [
        ("billing/checkout.py", "100644"),
        ("bin/run.sh", "100755"),
        ('odd "name"\twith\\tab.txt', "100644"),
    ]
    assert built.repo.git.read_file(built.repo.commit, "billing/checkout.py", max_bytes=100) == (
        FILES[0].content
    )
    assert git(tmp_path / "repo", "rev-parse", f"{built.repo.commit}^{{tree}}") == built.tree
    assert git(tmp_path / "repo", "rev-list", "--count", "--all") == "1"


def test_build_fixture_repo_gives_the_same_ids_on_every_build(tmp_path: Path) -> None:
    first = build_fixture_repo(FILES, tmp_path / "first")
    second = build_fixture_repo(tuple(reversed(FILES)), tmp_path / "second")

    assert (first.repo.commit, first.tree) == (second.repo.commit, second.tree)


def test_build_fixture_repo_ids_change_with_a_files_bytes_or_mode(tmp_path: Path) -> None:
    built = build_fixture_repo(FILES[:1], tmp_path / "a")
    edited = FixtureFile(FILES[0].path, FILES[0].content + b"\n")
    executable = FixtureFile(FILES[0].path, FILES[0].content, is_executable=True)

    trees = {
        built.tree,
        build_fixture_repo([edited], tmp_path / "b").tree,
        build_fixture_repo([executable], tmp_path / "c").tree,
    }

    assert len(trees) == 3


def test_build_fixture_repo_has_a_fixed_author_committer_and_date(tmp_path: Path) -> None:
    built = build_fixture_repo(FILES, tmp_path / "repo")

    shown = git(tmp_path / "repo", "log", "-1", "--format=%an <%ae> %at|%cn <%ce> %ct")

    identity = "review-bot evals <evals@review-bot.invalid> 1767225600"
    assert shown == f"{identity}|{identity}"
    assert built.repo.commit == git(tmp_path / "repo", "rev-parse", "HEAD")


def test_build_fixture_repo_of_no_files_commits_the_empty_tree(tmp_path: Path) -> None:
    built = build_fixture_repo((), tmp_path / "repo")

    assert built.tree == EMPTY_TREE
    assert built.repo.git.list_files(built.repo.commit, max_entries=10) == ()


def test_build_fixture_repo_ignores_the_users_git_config_and_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = build_fixture_repo(FILES, tmp_path / "plain")
    config = tmp_path / "gitconfig"
    config.write_text("[init]\n\tdefaultObjectFormat = sha256\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Someone Else")

    built = build_fixture_repo(FILES, tmp_path / "configured")

    assert (built.repo.commit, built.tree) == (expected.repo.commit, expected.tree)
    assert not (tmp_path / "elsewhere").exists()


def _fake_run(outcome: Any) -> Any:
    def run(*_args: Any, **_kwargs: Any) -> Any:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return run


@pytest.mark.parametrize(
    ("outcome", "error", "expected"),
    [
        (
            subprocess.TimeoutExpired(["git"], 60),
            GitTimeoutError,
            "git init took longer than 60 seconds",
        ),
        (FileNotFoundError(2, "No such file or directory"), GitError, "Couldn't run git"),
        (
            subprocess.CompletedProcess(["git"], 128, b"", b""),
            GitError,
            "exit code 128 building an eval fixture repository. Check",
        ),
        (
            subprocess.CompletedProcess(["git"], 128, b"", b"warning: one\nfatal: bad\x1b[2J\n\n"),
            GitError,
            r"exit code 128 building an eval fixture repository: fatal: bad\\x1b\[2J\. Check",
        ),
    ],
    ids=["timeout", "no_git", "fails", "fails_saying_why"],
)
def test_build_fixture_repo_raises_a_git_error_saying_what_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Any,
    error: type[GitError],
    expected: str,
) -> None:
    monkeypatch.setattr(fixture_repo.subprocess, "run", _fake_run(outcome))

    with pytest.raises(error, match=expected):
        build_fixture_repo(FILES, tmp_path / "repo")
