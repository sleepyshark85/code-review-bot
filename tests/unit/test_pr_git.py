import subprocess
import sys
from pathlib import Path

import pytest
from git_samples import commit, git, init_repo

from review_bot.errors import (
    DiffEmptyError,
    DiffNotUtf8Error,
    DiffTooLargeError,
    GitError,
    NotARegularFileError,
)
from review_bot.pr.git import GitRepo

LIMIT = 100_000


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def test_diff_is_the_change_from_the_merge_base_to_head(repo: Path) -> None:
    base = commit(repo, {"app.py": "one\n"})
    git(repo, "checkout", "-q", "-b", "feature")
    head = commit(repo, {"app.py": "one\ntwo\n"})
    git(repo, "checkout", "-q", "main")
    later_base = commit(repo, {"other.py": "main only\n"})

    diff = GitRepo(repo, timeout_s=60).diff(later_base, head, max_bytes=LIMIT)

    assert diff.startswith("diff --git a/app.py b/app.py\n")
    assert "+two\n" in diff
    assert "other.py" not in diff
    assert base != later_base


def test_diff_detects_renames(repo: Path) -> None:
    base = commit(repo, {"old.py": "".join(f"line {n}\n" for n in range(20))})
    git(repo, "mv", "old.py", "new.py")
    head = commit(repo, {})

    diff = GitRepo(repo, timeout_s=60).diff(base, head, max_bytes=LIMIT)

    assert "rename from old.py\nrename to new.py\n" in diff


def test_diff_runs_no_textconv_or_external_diff_driver(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    script = tmp_path / "driver.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nprint('converted')\n",
        encoding="utf-8",
    )
    program = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'
    git(repo, "config", "diff.evil.textconv", program)
    git(repo, "config", "diff.evil.command", program)
    git(repo, "config", "diff.external", program)
    base = commit(repo, {".gitattributes": "*.py diff=evil\n", "app.py": "one\n"})
    head = commit(repo, {"app.py": "two\n"})

    diff = GitRepo(repo, timeout_s=60).diff(base, head, max_bytes=LIMIT)

    assert not marker.exists()
    assert "-one\n+two\n" in diff


def test_diff_with_a_missing_commit_names_it(repo: Path) -> None:
    base = commit(repo, {"app.py": "one\n"})
    missing = "c" * 40

    with pytest.raises(GitError, match=f"Commit {missing} isn't in --repo"):
        GitRepo(repo, timeout_s=60).diff(base, missing, max_bytes=LIMIT)


def test_diff_outside_a_repository_says_git_failed(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="git rev-parse failed with exit code 128"):
        GitRepo(tmp_path / "nowhere", timeout_s=60).diff("a" * 40, "b" * 40, max_bytes=LIMIT)


def test_diff_over_the_limit_is_too_large(repo: Path) -> None:
    base = commit(repo, {"app.py": "one\n"})
    # Far more than a pipe's buffer, so git is still writing when it is killed.
    head = commit(repo, {"app.py": "x\n" * 100_000})

    with pytest.raises(DiffTooLargeError, match="over the limit of 1000 bytes"):
        GitRepo(repo, timeout_s=60).diff(base, head, max_bytes=1_000)


def test_diff_ignores_a_no_prefix_setting_in_the_clone(repo: Path) -> None:
    git(repo, "config", "diff.noprefix", "true")
    base = commit(repo, {"app.py": "one\n"})
    head = commit(repo, {"app.py": "two\n"})

    diff = GitRepo(repo, timeout_s=60).diff(base, head, max_bytes=LIMIT)

    assert "--- a/app.py\n+++ b/app.py\n" in diff


def test_diff_in_a_shallow_clone_without_the_merge_base_says_to_fetch_it(tmp_path: Path) -> None:
    origin = init_repo(tmp_path / "origin")
    commit(origin, {"app.py": "one\n"})
    git(origin, "checkout", "-q", "-b", "feature")
    head = commit(origin, {"app.py": "one\ntwo\n"})
    git(origin, "checkout", "-q", "main")
    base = commit(origin, {"other.py": "main only\n"})
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--depth", "1", origin.as_uri(), str(clone))
    git(clone, "fetch", "-q", "--depth", "1", "origin", "feature")

    with pytest.raises(GitError, match=r"no merge base .*shallow.*fetch-depth: 0"):
        GitRepo(clone, timeout_s=60).diff(base, head, max_bytes=LIMIT)


def test_diff_at_the_limit_is_returned(repo: Path) -> None:
    base = commit(repo, {"app.py": "one\n"})
    head = commit(repo, {"app.py": "two\n"})
    size = len(GitRepo(repo, timeout_s=60).diff(base, head, max_bytes=LIMIT).encode())

    assert GitRepo(repo, timeout_s=60).diff(base, head, max_bytes=size)


def test_diff_with_no_changes_is_empty(repo: Path) -> None:
    base = commit(repo, {"app.py": "one\n"})

    with pytest.raises(DiffEmptyError, match="changes nothing since its merge base"):
        GitRepo(repo, timeout_s=60).diff(base, base, max_bytes=LIMIT)


def test_diff_that_isnt_utf8_is_unreadable(repo: Path) -> None:
    git(repo, "config", "core.quotePath", "false")
    base = commit(repo, {".gitattributes": "*.txt diff\n", "a.txt": b"caf\xe9\n"})
    head = commit(repo, {"a.txt": b"caf\xe9!\n"})

    with pytest.raises(DiffNotUtf8Error, match="isn't valid UTF-8"):
        GitRepo(repo, timeout_s=60).diff(base, head, max_bytes=LIMIT)


def test_diff_past_the_timeout_says_so(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = commit(repo, {"app.py": "one\n"})
    head = commit(repo, {"app.py": "two\n"})
    real_popen = subprocess.Popen

    def slow_git_diff(command: list[str], **kwargs: object) -> "subprocess.Popen[bytes]":
        if "diff" in command:
            command = [sys.executable, "-c", "import time; time.sleep(30)"]
        return real_popen(command, **kwargs)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(subprocess, "Popen", slow_git_diff)

    with pytest.raises(GitError, match=r"git diff took longer than 0\.5 seconds"):
        GitRepo(repo, timeout_s=0.5).diff(base, head, max_bytes=LIMIT)


def test_diff_without_git_on_path_says_how_to_fix_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")

    with pytest.raises(GitError, match=r"Couldn't run git: .*Install git"):
        GitRepo(repo, timeout_s=60).diff("a" * 40, "b" * 40, max_bytes=LIMIT)


def test_has_commit_is_true_only_for_a_commit_in_the_clone(repo: Path) -> None:
    sha = commit(repo, {"app.py": "one\n"})
    clone = GitRepo(repo, timeout_s=60)

    assert clone.has_commit(sha)
    assert not clone.has_commit("c" * 40)


def test_has_commit_outside_a_repository_says_git_failed(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="git rev-parse failed"):
        GitRepo(tmp_path, timeout_s=60).has_commit("c" * 40)


def test_is_ancestor_holds_for_an_older_commit_and_for_the_commit_itself(repo: Path) -> None:
    older = commit(repo, {"app.py": "one\n"})
    newer = commit(repo, {"app.py": "one\ntwo\n"})
    clone = GitRepo(repo, timeout_s=60)

    assert clone.is_ancestor(older, newer)
    assert clone.is_ancestor(newer, newer)
    assert not clone.is_ancestor(newer, older)


def test_is_ancestor_is_false_after_a_force_push(repo: Path) -> None:
    base = commit(repo, {"app.py": "one\n"})
    reviewed = commit(repo, {"app.py": "one\ntwo\n"})
    git(repo, "reset", "-q", "--hard", base)
    rewritten = commit(repo, {"app.py": "one\nthree\n"})

    assert not GitRepo(repo, timeout_s=60).is_ancestor(reviewed, rewritten)


def test_is_ancestor_is_false_for_a_commit_the_clone_lacks(repo: Path) -> None:
    head = commit(repo, {"app.py": "one\n"})

    assert not GitRepo(repo, timeout_s=60).is_ancestor("c" * 40, head)
    assert not GitRepo(repo, timeout_s=60).is_ancestor(head, "c" * 40)


def test_is_ancestor_raises_when_git_fails(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="git rev-parse failed"):
        GitRepo(tmp_path / "not-a-repo", timeout_s=60).is_ancestor("a" * 40, "b" * 40)


def test_read_file_returns_the_file_at_that_commit_not_a_later_one(repo: Path) -> None:
    first = commit(repo, {"config.toml": "one\n"})
    commit(repo, {"config.toml": "two\n"})

    assert GitRepo(repo, timeout_s=60).read_file(first, "config.toml", max_bytes=LIMIT) == b"one\n"


def test_read_file_returns_none_when_nothing_is_at_the_path(repo: Path) -> None:
    sha = commit(repo, {"app.py": "x\n"})

    assert GitRepo(repo, timeout_s=60).read_file(sha, "config.toml", max_bytes=LIMIT) is None


def test_read_file_reads_at_most_one_byte_over_the_limit(repo: Path) -> None:
    sha = commit(repo, {"config.toml": "x" * 50})

    assert GitRepo(repo, timeout_s=60).read_file(sha, "config.toml", max_bytes=10) == b"x" * 11


def test_read_file_refuses_a_directory(repo: Path) -> None:
    sha = commit(repo, {"config.toml/inner.txt": "x\n"})

    with pytest.raises(NotARegularFileError, match="is a directory") as caught:
        GitRepo(repo, timeout_s=60).read_file(sha, "config.toml", max_bytes=LIMIT)

    assert caught.value.kind == "directory"


def test_read_file_refuses_a_symbolic_link(repo: Path) -> None:
    # A symlink's blob, staged directly, so the test needs no symlink support on disk.
    commit(repo, {"target.toml": "x\n"})
    target = repo.parent / "link-target"
    target.write_text("target.toml", encoding="utf-8")
    link = git(repo, "hash-object", "-w", str(target))
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{link},config.toml")
    git(repo, "commit", "-q", "-m", "link")
    sha = git(repo, "rev-parse", "HEAD")

    with pytest.raises(NotARegularFileError, match="is a symbolic link"):
        GitRepo(repo, timeout_s=60).read_file(sha, "config.toml", max_bytes=LIMIT)


def test_read_file_names_a_commit_missing_from_the_clone(repo: Path) -> None:
    commit(repo, {"app.py": "x\n"})

    with pytest.raises(GitError, match="c" * 40):
        GitRepo(repo, timeout_s=60).read_file("c" * 40, "config.toml", max_bytes=LIMIT)


def test_read_file_refuses_a_submodule(repo: Path) -> None:
    # A gitlink, staged directly, so the test needs no second repository.
    other = commit(repo, {"app.py": "x\n"})
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{other},config.toml")
    git(repo, "commit", "-q", "-m", "submodule")
    sha = git(repo, "rev-parse", "HEAD")

    with pytest.raises(NotARegularFileError, match="is a submodule") as caught:
        GitRepo(repo, timeout_s=60).read_file(sha, "config.toml", max_bytes=LIMIT)

    assert caught.value.kind == "submodule"
