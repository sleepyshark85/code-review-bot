"""Local git repositories built in a test's ``tmp_path``; nothing touches the network."""

import subprocess
from collections.abc import Mapping
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return its stripped output."""
    completed = subprocess.run(  # noqa: S603 - a fixed argument list, no shell
        ["git", "-C", str(repo), *args],  # noqa: S607 - git from PATH, as the code under test
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def init_repo(path: Path) -> Path:
    """An empty repository on branch ``main`` that can commit without global config."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    git(path, "config", "commit.gpgsign", "false")
    return path


def commit(repo: Path, files: Mapping[str, str | bytes]) -> str:
    """Write ``files`` and commit them all; return the commit's SHA."""
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    return git(repo, "rev-parse", "HEAD")
