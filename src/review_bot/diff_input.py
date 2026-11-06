"""Read a diff file and reject anything that can't be reviewed whole."""

from pathlib import Path

from review_bot.errors import (
    DiffEmptyError,
    DiffNotFoundError,
    DiffTooLargeError,
    DiffUnreadableError,
)
from review_bot.secret_scan import redact

__all__ = ["load_diff"]


def load_diff(path: Path, *, max_bytes: int) -> str:
    """Return the diff's text unchanged, including its line endings.

    Args:
        path: The diff file.
        max_bytes: The largest accepted file size. A larger diff is rejected, never truncated.

    Raises:
        DiffNotFoundError: If the path doesn't exist.
        DiffUnreadableError: If the path can't be read, or isn't valid UTF-8.
        DiffTooLargeError: If the file is over ``max_bytes``.
        DiffEmptyError: If the file is empty or contains only whitespace.
    """
    # Every path written to stderr passes ``redact`` (Phase 1 design, Secrets).
    shown_path = redact(str(path))
    try:
        # One byte past the limit is enough to reject a file of any size without loading it.
        with path.open("rb") as diff_file:
            raw = diff_file.read(max_bytes + 1)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise DiffNotFoundError(f"Diff not found: {shown_path}. Check the path.") from exc
    except OSError as exc:
        raise DiffUnreadableError(
            f"Can't read diff {shown_path}: {exc.strerror}. Pass a readable file."
        ) from exc

    if len(raw) > max_bytes:
        raise DiffTooLargeError(
            f"Diff {shown_path} is at least {len(raw)} bytes, over the limit of {max_bytes}. "
            "Split the diff or raise --max-diff-bytes."
        )

    try:
        diff = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiffUnreadableError(
            f"Diff {shown_path} is not valid UTF-8. Re-save it as UTF-8."
        ) from exc

    if not diff.strip():
        raise DiffEmptyError(f"Diff {shown_path} is empty. Pass a diff with changes to review.")
    return diff
