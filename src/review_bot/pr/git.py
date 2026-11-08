"""Diffs between two commits of a local clone, by running ``git``.

git runs from an argument list with a timeout, and never with an external diff or a textconv
filter, so the clone's attributes and config can't make it run another program (Phase 2
design, Safety on the PR path).
"""

import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from review_bot.errors import DiffEmptyError, DiffNotUtf8Error, DiffTooLargeError, GitError
from review_bot.secret_scan import redact

__all__ = ["GitRepo"]

# Plain text in git's default shape whatever the clone's config says, with renames detected as
# the design asks.
_DIFF_OPTIONS: Final = (
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--find-renames",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "-U3",
)
# ``git rev-parse --verify --quiet`` exits 1, and only 1, when the name doesn't resolve, and
# ``git merge-base`` when the commits share no history.
_NOT_FOUND_EXIT_CODE: Final = 1


class GitRepo:
    """A local clone, read through git subprocesses that each have a timeout."""

    def __init__(self, path: Path, *, timeout_s: float) -> None:
        """Name the clone; nothing runs until a method is called.

        Args:
            path: The clone's directory.
            timeout_s: How long each git command may take.
        """
        self._path = path
        self._timeout_s = timeout_s
        self._shown_path = redact(str(path))

    def diff(self, base: str, head: str, *, max_bytes: int) -> str:
        """Return the changes from the merge base of ``base`` and ``head`` to ``head``.

        Only ``max_bytes`` plus one bytes of git's output are ever read.

        Args:
            base: The base commit's SHA.
            head: The head commit's SHA.
            max_bytes: The largest diff accepted. A larger one is rejected, never truncated.

        Raises:
            GitError: If either commit isn't in the clone, they have no merge base there, or
                git fails, times out or can't start.
            DiffTooLargeError: If the diff is over ``max_bytes``.
            DiffNotUtf8Error: If the diff isn't valid UTF-8.
            DiffEmptyError: If the diff is empty.
        """
        for commit in (base, head):
            self._require_commit(commit)
        self._require_merge_base(base, head)
        name = f"{base}...{head}"
        raw = self._read_output(["diff", *_DIFF_OPTIONS, name], max_bytes=max_bytes)
        if len(raw) > max_bytes:
            raise DiffTooLargeError(
                f"The diff {name} is over the limit of {max_bytes} bytes. Split the pull "
                "request, or raise --max-diff-bytes."
            )
        try:
            diff = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DiffNotUtf8Error(
                f"The diff {name} isn't valid UTF-8, so it can't be reviewed. Review it by hand."
            ) from exc
        if not diff.strip():
            raise DiffEmptyError(
                f"The diff {name} is empty: the pull request changes nothing since its merge base."
            )
        return diff

    def _require_commit(self, sha: str) -> None:
        """Raise ``GitError`` naming ``sha`` unless it is a commit in the clone."""
        if self._exit_code(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"]) != 0:
            raise GitError(
                f"Commit {sha} isn't in --repo {self._shown_path}. Fetch the pull request's base "
                "and head commits into the clone (fetch-depth: 0), then run again."
            )

    def _require_merge_base(self, base: str, head: str) -> None:
        """Raise ``GitError`` unless the clone holds a common ancestor of both commits.

        A shallow clone can hold both commits but not the history joining them.
        """
        if self._exit_code(["merge-base", base, head]) != 0:
            raise GitError(
                f"Commits {base} and {head} have no merge base in --repo {self._shown_path}; a "
                "shallow clone lacks it. Fetch the full history (fetch-depth: 0), then run again."
            )

    def _exit_code(self, args: Sequence[str]) -> int:
        """Run git with no output kept and return its exit code if that is 0 or 1.

        Raises:
            GitError: If git can't start, times out, or fails with any other code.
        """
        try:
            completed = subprocess.run(  # noqa: S603 - a fixed argument list, no shell
                self._command(args),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self._timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise self._timed_out(args[0]) from exc
        except OSError as exc:
            raise _not_started(exc) from exc
        if completed.returncode not in {0, _NOT_FOUND_EXIT_CODE}:
            raise self._failed(args[0], completed.returncode)
        return completed.returncode

    def _read_output(self, args: Sequence[str], *, max_bytes: int) -> bytes:
        """Run git and return at most ``max_bytes`` plus one bytes of its output.

        git is killed once that much is read, or when the timeout passes.

        Raises:
            GitError: If git can't start, times out, or fails before its output is over the
                limit.
        """
        try:
            process = subprocess.Popen(  # noqa: S603 - a fixed argument list, no shell
                self._command(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            raise _not_started(exc) from exc
        timed_out = threading.Event()

        def stop_at_timeout() -> None:
            timed_out.set()
            process.kill()

        timer = threading.Timer(self._timeout_s, stop_at_timeout)
        timer.start()
        try:
            with process:
                # ``Popen`` with ``stdout=PIPE`` always has a ``stdout``.
                assert process.stdout is not None  # noqa: S101 - see above
                output = process.stdout.read(max_bytes + 1)
                if len(output) > max_bytes:
                    process.kill()
        finally:
            timer.cancel()
        # Over the limit, git was killed on purpose, and its exit code means nothing.
        if len(output) <= max_bytes and process.returncode != 0:
            if timed_out.is_set():
                raise self._timed_out(args[0])
            raise self._failed(args[0], process.returncode)
        return output

    def _command(self, args: Sequence[str]) -> list[str]:
        return ["git", "-C", str(self._path), *args]

    def _timed_out(self, subcommand: str) -> GitError:
        return GitError(
            f"git {subcommand} took longer than {self._timeout_s:g} seconds in --repo "
            f"{self._shown_path}. Check the clone, then run again."
        )

    def _failed(self, subcommand: str, exit_code: int) -> GitError:
        return GitError(
            f"git {subcommand} failed with exit code {exit_code} in --repo {self._shown_path}. "
            "Check that --repo is a clone of the pull request's repository."
        )


def _not_started(exc: OSError) -> GitError:
    reason = exc.strerror or type(exc).__name__
    return GitError(f"Couldn't run git: {reason}. Install git and check that it is on PATH.")
