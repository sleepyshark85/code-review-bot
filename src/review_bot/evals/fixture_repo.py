"""Build a case's fixture repository as one git commit, the same on every machine (P3-S5).

The commit's author, committer, date and message are fixed, git reads no user or system config,
and every file is written with its bytes as read and git's mode for it, so two builds of the same
files give the same tree and commit IDs. Each git command has the usual timeout.
"""

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from review_bot.context.tools import RepoRef
from review_bot.errors import GitError, GitTimeoutError
from review_bot.evals.formats import FixtureFile
from review_bot.git import GIT_TIMEOUT_S, GitRepo
from review_bot.path_quoting import c_quoted
from review_bot.render import escape_unprintable

__all__ = ["FixtureRepo", "build_fixture_repo"]

_REF: Final = "refs/heads/main"
# 2026-01-01T00:00:00Z, in fast-import's raw date format.
_IDENTITY: Final = b"review-bot evals <evals@review-bot.invalid> 1767225600 +0000"
_MESSAGE: Final = b"Eval fixture repository\n"


@dataclass(frozen=True, slots=True)
class FixtureRepo:
    """The built repository's one commit, for the tools to read, and that commit's tree ID."""

    repo: RepoRef
    tree: str


def build_fixture_repo(files: Sequence[FixtureFile], directory: Path) -> FixtureRepo:
    """Create a git repository at ``directory`` whose one commit holds exactly ``files``.

    No files give a commit of the empty tree. Its branch ``main`` and ``HEAD`` name the commit.
    ``directory`` must not exist yet, or be empty.

    Raises:
        GitError: If git can't start, fails, or takes longer than its timeout.
    """
    init = ["init", "--quiet", "--object-format=sha1", "--initial-branch=main", str(directory)]
    _run_git(directory.parent, init)
    _run_git(directory, ["fast-import", "--quiet"], stdin=_fast_import_stream(files))
    output = _run_git(directory, ["rev-parse", _REF, f"{_REF}^{{tree}}"])
    commit, tree = output.decode("ascii").split()
    git = GitRepo(directory, timeout_s=GIT_TIMEOUT_S, flag="the case's repo")
    return FixtureRepo(RepoRef(git=git, commit=commit), tree)


def _fast_import_stream(files: Sequence[FixtureFile]) -> bytes:
    """One commit of ``files``, in ``git fast-import``'s format, each path C-quoted."""
    parts = [
        b"commit " + _REF.encode("ascii") + b"\n",
        b"author " + _IDENTITY + b"\n",
        b"committer " + _IDENTITY + b"\n",
        _data(_MESSAGE),
    ]
    for file in files:
        quoted = c_quoted(os.fsencode(file.path)).encode("ascii")
        mode = b"100755" if file.is_executable else b"100644"
        parts += [b"M " + mode + b" inline " + quoted + b"\n", _data(file.content)]
    return b"".join(parts)


def _data(content: bytes) -> bytes:
    return b"data " + str(len(content)).encode("ascii") + b"\n" + content + b"\n"


def _run_git(directory: Path, args: Sequence[str], *, stdin: bytes = b"") -> bytes:
    """Run git in ``directory`` with ``args``, whose first is the subcommand, and return its output.

    Raises:
        GitError: If git can't start or exits with an error.
        GitTimeoutError: If it takes longer than ``GIT_TIMEOUT_S``.
    """
    subcommand = args[0]
    try:
        completed = subprocess.run(  # noqa: S603 - a fixed argument list, no shell
            ["git", "-C", str(directory), *args],  # noqa: S607 - git from PATH, as in review_bot.git
            input=stdin,
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            env=_isolated_environment(os.environ),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeoutError(
            f"git {subcommand} took longer than {GIT_TIMEOUT_S} seconds building an eval fixture "
            "repository. Check the case's repo, then run again."
        ) from exc
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise GitError(
            f"Couldn't run git: {reason}. Install git and check that it is on PATH."
        ) from exc
    if completed.returncode != 0:
        raise GitError(
            f"git {subcommand} failed with exit code {completed.returncode} building an eval "
            f"fixture repository{_last_line(completed.stderr)}. Check the case's repo, then run "
            "again."
        )
    return completed.stdout


def _last_line(stderr: bytes) -> str:
    """``: `` and git's last line on stderr, escaped so it can't forge output; empty if none."""
    lines = stderr.decode("utf-8", errors="replace").strip().splitlines()
    return f": {escape_unprintable(lines[-1])}" if lines else ""


def _isolated_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` without git's own variables, reading no user or system config.

    So neither a hook's ``GIT_DIR`` nor a user's config, such as ``init.defaultObjectFormat``, can
    change which repository is written or what its IDs are.
    """
    kept = {name: value for name, value in environ.items() if not name.startswith("GIT_")}
    return kept | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
