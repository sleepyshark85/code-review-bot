import io
from pathlib import Path
from typing import override

import pytest

from review_bot.diff_input import load_diff
from review_bot.errors import (
    DiffEmptyError,
    DiffNotFoundError,
    DiffTooLargeError,
    DiffUnreadableError,
)

LIMIT = 100


def test_load_diff_with_crlf_and_braces_returns_text_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "change.diff"
    path.write_bytes("+héllo {self.id}\r\n".encode())

    assert load_diff(path, max_bytes=LIMIT) == "+héllo {self.id}\r\n"


def test_load_diff_at_exact_limit_returns_text(tmp_path: Path) -> None:
    path = tmp_path / "change.diff"
    path.write_bytes(b"x" * LIMIT)

    assert load_diff(path, max_bytes=LIMIT) == "x" * LIMIT


def test_load_diff_with_missing_path_raises_not_found_naming_path(tmp_path: Path) -> None:
    path = tmp_path / "missing.diff"

    with pytest.raises(DiffNotFoundError, match=r"missing\.diff"):
        load_diff(path, max_bytes=LIMIT)


def test_load_diff_with_file_used_as_directory_raises_not_found(tmp_path: Path) -> None:
    file = tmp_path / "file.txt"
    file.write_text("x", encoding="utf-8")

    with pytest.raises(DiffNotFoundError):
        load_diff(file / "child.diff", max_bytes=LIMIT)


def test_load_diff_with_directory_raises_unreadable(tmp_path: Path) -> None:
    with pytest.raises(DiffUnreadableError, match=tmp_path.name):
        load_diff(tmp_path, max_bytes=LIMIT)


def test_load_diff_with_invalid_utf8_raises_unreadable_naming_path(tmp_path: Path) -> None:
    path = tmp_path / "utf16.diff"
    path.write_bytes(b"\xff\xfeA\x00")

    with pytest.raises(DiffUnreadableError, match=r"utf16\.diff"):
        load_diff(path, max_bytes=LIMIT)


def test_load_diff_with_permission_error_raises_unreadable_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(_self: Path, *_args: object, **_kwargs: object) -> io.BytesIO:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "open", deny)

    with pytest.raises(DiffUnreadableError, match="Permission denied"):
        load_diff(tmp_path / "secret.diff", max_bytes=LIMIT)


def test_load_diff_of_endless_file_reads_one_byte_past_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fake, not a sparse file: on NTFS, truncate(2**40) writes real zeros to disk.
    class EndlessFile(io.BytesIO):
        @override
        def read(self, size: int | None = -1, /) -> bytes:
            if size is None or size < 0:
                raise MemoryError
            return b"x" * size

    monkeypatch.setattr(Path, "open", lambda _self, *_args, **_kwargs: EndlessFile())

    with pytest.raises(DiffTooLargeError, match=r"at least 101 bytes.*limit of 100"):
        load_diff(tmp_path / "huge.diff", max_bytes=LIMIT)


@pytest.mark.parametrize("content", [b"", b" \n\t\n", "\u00a0\r\n".encode()])
def test_load_diff_with_only_whitespace_raises_empty(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "blank.diff"
    path.write_bytes(content)

    with pytest.raises(DiffEmptyError, match=r"blank\.diff"):
        load_diff(path, max_bytes=LIMIT)


def test_load_diff_over_limit_raises_too_large_with_both_sizes(tmp_path: Path) -> None:
    path = tmp_path / "big.diff"
    path.write_bytes(b"x" * (LIMIT + 1))

    with pytest.raises(DiffTooLargeError, match=r"101 bytes.*limit of 100"):
        load_diff(path, max_bytes=LIMIT)


def test_load_diff_over_limit_and_invalid_utf8_reports_size_first(tmp_path: Path) -> None:
    path = tmp_path / "big.bin"
    path.write_bytes(b"\xff" * (LIMIT + 1))

    with pytest.raises(DiffTooLargeError):
        load_diff(path, max_bytes=LIMIT)
