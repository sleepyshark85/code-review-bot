"""A local clone's diffs, files, listings, searches and commits, read by git.

git runs from an argument list with a timeout, and never with an external diff or a textconv
filter, so the clone's attributes and config can't make it run another program (Phase 2
design, Safety on the PR path).
"""

import os
import subprocess
import threading
from collections.abc import Callable, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import IO, Final

from review_bot.errors import (
    DiffEmptyError,
    DiffNotUtf8Error,
    DiffTooLargeError,
    GitError,
    GitTimeoutError,
    ListingTooLargeError,
    NotARegularFileError,
)
from review_bot.git_output import (
    REGULAR_FILE_MODES,
    GrepReader,
    GrepResult,
    TreeEntry,
    find_tree_entry,
    parse_tree_entry,
)
from review_bot.secret_scan import redact

__all__ = ["GIT_TIMEOUT_S", "GitRepo"]

# How long each git command may take.
GIT_TIMEOUT_S: Final = 60

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
# ``git merge-base`` when the commits share no history or, with ``--is-ancestor``, when the first
# isn't an ancestor of the second.
_NOT_FOUND_EXIT_CODE: Final = 1
# ``git ls-tree`` modes: a regular file, plain or executable, and what else a path can name.
_OTHER_ENTRY_KINDS: Final = {"120000": "symbolic link", "160000": "submodule"}
# One ``ls-tree`` entry: mode, type, a SHA of up to 64 hex digits, a tab and the path.
_MAX_TREE_ENTRY_BYTES: Final = 4096
# A whole listing's byte cap, per entry allowed: an entry is about 80 bytes plus its path. Lower
# than ``_MAX_TREE_ENTRY_BYTES``, because a read allocates its whole cap up front.
_MAX_LISTING_BYTES_PER_ENTRY: Final = 512
_SUCCESS: Final = frozenset({0})
_SUCCESS_OR_NOT_FOUND: Final = frozenset({0, _NOT_FOUND_EXIT_CODE})
# What git exits with on any fatal error, such as being run outside a repository.
_FATAL_EXIT_CODE: Final = 128
_SUCCESS_OR_FATAL: Final = frozenset({0, _FATAL_EXIT_CODE})


class GitRepo:
    """A local clone, read through git subprocesses that each have a timeout."""

    def __init__(self, path: Path, *, timeout_s: float, flag: str = "--repo") -> None:
        """Name the clone; nothing runs until a method is called.

        Args:
            path: The clone's directory.
            timeout_s: How long each git command may take.
            flag: The command-line flag that named the clone, which error messages name.
        """
        self._path = path
        self._timeout_s = timeout_s
        self._flag = flag
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

    def read_file(self, commit: str, path: str, *, max_bytes: int) -> bytes | None:
        """Return the regular file at ``path`` in ``commit``, or None if nothing is there.

        Only the tree entry whose path is exactly ``path`` is read: ``path`` is never a pattern,
        and the clone's case folding doesn't apply. Only ``max_bytes`` plus one bytes of it are
        ever read, so a longer result means the file is over ``max_bytes``. Its bytes are git's,
        with no filter or textconv applied.

        Args:
            commit: The commit's SHA.
            path: The path from the repository's root, with ``/`` between segments.
            max_bytes: The most bytes wanted.

        Raises:
            GitError: If the commit isn't in the clone, git's listing can't be read, or git
                fails, times out or can't start.
            NotARegularFileError: If ``path`` is a directory, symbolic link or submodule.
        """
        self._require_commit(commit)
        listing = self._read_output(
            ["ls-tree", "-z", "--full-tree", commit, "--", path], max_bytes=_MAX_TREE_ENTRY_BYTES
        )
        entry = find_tree_entry(listing, path, max_bytes=_MAX_TREE_ENTRY_BYTES)
        if entry is None:
            return None
        mode, kind, object_id = entry
        if mode not in REGULAR_FILE_MODES:
            what = "directory" if kind == "tree" else _OTHER_ENTRY_KINDS.get(mode, "special entry")
            raise NotARegularFileError(
                f"{path} in commit {commit} is a {what}, not a regular file.", kind=what
            )
        return self._read_output(["cat-file", "blob", object_id], max_bytes=max_bytes)

    def list_files(self, commit: str, *, max_entries: int) -> tuple[TreeEntry, ...]:
        """Return the entries of ``commit``'s tree at any depth, without its directories.

        Args:
            commit: The commit's SHA.
            max_entries: The most entries accepted. A larger listing is rejected, never cut.

        Raises:
            ListingTooLargeError: If the listing is over ``max_entries`` entries, or over 512
                bytes per entry allowed.
            GitError: If git's output can't be read, or git fails, times out or can't start.
        """
        max_bytes = max_entries * _MAX_LISTING_BYTES_PER_ENTRY
        raw = self._read_output(["ls-tree", "-r", "-z", "-l", commit], max_bytes=max_bytes)
        records = raw.split(b"\0")[:-1]  # git ends every record with a NUL
        if len(raw) > max_bytes or len(records) > max_entries:
            raise ListingTooLargeError(
                f"Commit {commit} in {self._shown_path} lists more than {max_entries} files, or "
                f"more than {max_bytes} bytes of them: too many to list."
            )
        return tuple(parse_tree_entry(record) for record in records)

    def grep(
        self,
        commit: str,
        literal: str,
        *,
        ignore_case: bool,
        keep: Callable[[str], bool],
        max_bytes: int,
    ) -> GrepResult:
        """Return git's matches of ``literal`` in the files of ``commit`` that ``keep`` accepts.

        ``literal`` is a fixed string, never a pattern or an option, and no pathspec is passed:
        ``keep`` is asked about each match's path as git's output streams by. Matches come by
        path, then line. Once the matches kept are over ``max_bytes`` of git's output, git is
        stopped and the result is cut; the bytes of matches not kept don't count.

        Raises:
            GitError: If git's output can't be read, or git fails, times out or can't start.
        """
        case_options = ["-i"] if ignore_case else []
        options = ["-F", "-n", "-z", "-I", "--no-textconv", "--no-color", "--no-column"]
        args = ["grep", *options, *case_options, "-e", literal, commit, "--"]
        reader = GrepReader(commit, keep=keep, max_bytes=max_bytes)
        # ``git grep`` exits 1 when nothing matches.
        return self._run_git(args, reader.read, exit_codes=_SUCCESS_OR_NOT_FOUND)

    def resolve_commit(self, revision: str) -> str | None:
        """Return the SHA of the commit ``revision`` names, or None if it names none.

        Raises:
            GitError: If the directory isn't in a git repository, or git fails, times out or
                can't start.
        """
        output = self._read_output(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}"],
            max_bytes=_MAX_TREE_ENTRY_BYTES,
            exit_codes=_SUCCESS_OR_NOT_FOUND,
        )
        return output.decode("ascii").strip() or None

    def top_level(self) -> Path | None:
        """Return the root of the working tree the directory is in, or None if it isn't in one.

        A directory that doesn't exist isn't in one either.

        Raises:
            GitError: If git times out or can't start.
        """
        output = self._read_output(
            ["rev-parse", "--show-toplevel"],
            max_bytes=_MAX_TREE_ENTRY_BYTES,
            exit_codes=_SUCCESS_OR_FATAL,
        )
        # Outside a working tree, git exits 128 and writes nothing to stdout.
        return Path(os.fsdecode(output.rstrip(b"\n"))) if output else None

    def has_commit(self, sha: str) -> bool:
        """Whether ``sha`` is a commit in the clone.

        Raises:
            GitError: If git fails, times out or can't start.
        """
        return self._exit_code(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"]) == 0

    def is_ancestor(self, older: str, newer: str) -> bool:
        """Whether ``older`` is ``newer`` or one of its ancestors, both commits in the clone.

        False if either isn't a commit in the clone, or a shallow clone lacks the history
        joining them.

        Raises:
            GitError: If git fails, times out or can't start.
        """
        if not (self.has_commit(older) and self.has_commit(newer)):
            return False
        return self._exit_code(["merge-base", "--is-ancestor", older, newer]) == 0

    def _require_commit(self, sha: str) -> None:
        """Raise ``GitError`` naming ``sha`` unless it is a commit in the clone."""
        if not self.has_commit(sha):
            raise GitError(
                f"Commit {sha} isn't in {self._flag} {self._shown_path}. Fetch it into the clone "
                "(fetch-depth: 0 fetches every commit), then run again."
            )

    def _require_merge_base(self, base: str, head: str) -> None:
        """Raise ``GitError`` unless the clone holds a common ancestor of both commits.

        A shallow clone can hold both commits but not the history joining them.
        """
        if self._exit_code(["merge-base", base, head]) != 0:
            raise GitError(
                f"Commits {base} and {head} have no merge base in {self._flag} "
                f"{self._shown_path}; a shallow clone lacks it. Fetch the full history "
                "(fetch-depth: 0), then run again."
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

    def _read_output(
        self, args: Sequence[str], *, max_bytes: int, exit_codes: AbstractSet[int] = _SUCCESS
    ) -> bytes:
        """Run git and return at most ``max_bytes`` plus one bytes of its output.

        git is killed once that much is read, or when the timeout passes.

        Raises:
            GitError: If git can't start, times out, or exits with a code not in ``exit_codes``
                before its output is over the limit.
        """

        def read(stdout: IO[bytes]) -> tuple[bytes, bool]:
            output = stdout.read(max_bytes + 1)
            return output, len(output) > max_bytes

        return self._run_git(args, read, exit_codes=exit_codes)

    def _run_git[T](
        self,
        args: Sequence[str],
        read: Callable[[IO[bytes]], tuple[T, bool]],
        *,
        exit_codes: AbstractSet[int],
    ) -> T:
        """Run git and return what ``read`` makes of its output.

        ``read`` also says whether it stopped early, and git is then killed. git is killed too
        when the timeout passes.

        Raises:
            GitError: If git can't start, times out, or exits with a code not in ``exit_codes``
                before ``read`` stops early.
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
                value, is_stopped = read(process.stdout)
                if is_stopped:
                    process.kill()
        finally:
            timer.cancel()
        # Stopped early, git was killed on purpose, and its exit code means nothing.
        if is_stopped:
            return value
        # Before the exit code: a killed git can exit with one ``exit_codes`` allows, such as 1
        # on Windows, which would pass a cut-short output off as complete.
        if timed_out.is_set():
            raise self._timed_out(args[0])
        if process.returncode not in exit_codes:
            raise self._failed(args[0], process.returncode)
        return value

    def _command(self, args: Sequence[str]) -> list[str]:
        # So a path given to git is never a pattern, nor magic such as ``:(icase)``.
        return ["git", "-C", str(self._path), "--literal-pathspecs", *args]

    def _timed_out(self, subcommand: str) -> GitTimeoutError:
        return GitTimeoutError(
            f"git {subcommand} took longer than {self._timeout_s:g} seconds in {self._flag} "
            f"{self._shown_path}. Check the clone, then run again."
        )

    def _failed(self, subcommand: str, exit_code: int) -> GitError:
        return GitError(
            f"git {subcommand} failed with exit code {exit_code} in {self._flag} "
            f"{self._shown_path}. Check that {self._flag} is a git clone of the repository "
            "under review."
        )


def _not_started(exc: OSError) -> GitError:
    reason = exc.strerror or type(exc).__name__
    return GitError(f"Couldn't run git: {reason}. Install git and check that it is on PATH.")
