import io
import os
import sys
from pathlib import Path
from typing import override

import pytest

from review_bot.console import is_terminal, read_answer


class TtyStream(io.StringIO):
    """A stream that says it is a terminal and has no file descriptor, like a test's."""

    @override
    def isatty(self) -> bool:
        return True


class FailingStream(io.StringIO):
    @override
    def write(self, _text: str) -> int:
        raise OSError("closed")


def test_is_terminal_with_no_stream_is_false() -> None:
    assert not is_terminal(None)


def test_is_terminal_with_a_stream_that_is_no_tty_is_false() -> None:
    assert not is_terminal(io.StringIO())


def test_is_terminal_with_a_tty_stream_with_no_descriptor_takes_it_at_its_word() -> None:
    assert is_terminal(TtyStream())


def test_is_terminal_with_the_null_device_is_false() -> None:
    # On Windows, NUL's ``isatty`` is true, so this proves the console check.
    with Path(os.devnull).open(encoding="utf-8") as null_device:
        assert not is_terminal(null_device)


def test_is_terminal_with_a_pipe_is_false() -> None:
    read_end, write_end = os.pipe()
    os.close(write_end)
    with os.fdopen(read_end, encoding="utf-8") as pipe:
        assert not is_terminal(pipe)


def test_read_answer_writes_the_prompt_to_stderr_and_returns_the_line_without_its_ending(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("r\r\nnext\n"))

    answer = read_answer("[a]ccept: ")

    captured = capsys.readouterr()
    assert answer == "r"
    assert captured.err == "[a]ccept: "
    assert captured.out == ""


def test_read_answer_escapes_control_characters_in_the_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("a\n"))

    read_answer("x\x1b[31m: ")

    assert capsys.readouterr().err == r"x\x1b[31m: "


def test_read_answer_at_the_end_of_stdin_raises_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    with pytest.raises(EOFError):
        read_answer("? ")


def test_read_answer_with_a_failing_stderr_raises_eof_without_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = io.StringIO("a\n")
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stderr", FailingStream())

    with pytest.raises(EOFError):
        read_answer("? ")

    assert stdin.tell() == 0


class TtyStreamWithDescriptor(TtyStream):
    def __init__(self, descriptor: int) -> None:
        super().__init__()
        self._descriptor = descriptor

    @override
    def fileno(self) -> int:
        return self._descriptor


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows asks the console")
def test_is_terminal_with_a_descriptor_that_is_not_open_is_false() -> None:
    descriptor = os.open(os.devnull, os.O_RDONLY)
    os.close(descriptor)

    assert not is_terminal(TtyStreamWithDescriptor(descriptor))
