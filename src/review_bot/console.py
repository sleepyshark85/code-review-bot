"""The standard streams: safe writes, reading an answer, and telling whether a person is there.

A failing stream can't change the exit code.
"""

import sys
from typing import TextIO

from review_bot.render import escape_unprintable

if sys.platform == "win32":
    import ctypes
    import msvcrt

__all__ = ["is_terminal", "read_answer", "write_diagnostic", "write_output"]


def write_output(text: str) -> None:
    """Write to stdout and flush, so a failure surfaces here and not at interpreter shutdown."""
    try:
        _write_line(sys.stdout, text)
        sys.stdout.flush()
    except Exception:
        # CPython flushes the standard streams again at shutdown, and a failure there overrides
        # the exit code with 120, so the failed stream has to go.
        sys.stdout = None
        raise


def write_diagnostic(text: str) -> None:
    """Write one line to stderr. A stderr that can't be written to never changes the exit code.

    The text is escaped here because any line can hold untrusted text: a path, a flag, or token
    counts from the CLI's JSON.
    """
    try:
        _write_line(sys.stderr, escape_unprintable(text))
        sys.stderr.flush()
    # Nothing may escape ``main``, and with stderr gone there is nowhere left to report this.
    except Exception:  # noqa: BLE001 - see above
        # See ``write_output``: the shutdown flush covers stderr too.
        sys.stderr = None


def read_answer(prompt: str) -> str:
    """Write ``prompt`` to stderr, with no line break, then read one line from stdin.

    Returns:
        The line, without its line ending.

    Raises:
        EOFError: If stdin has ended, or stderr can't show the prompt, since then nobody can
            answer it.
    """
    try:
        _write(sys.stderr, escape_unprintable(prompt))
        sys.stderr.flush()
    except Exception as exc:
        # See ``write_output``: the shutdown flush covers stderr too.
        sys.stderr = None
        raise EOFError("stderr can't show the prompt") from exc
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line.rstrip("\r\n")


def is_terminal(stream: TextIO | None) -> bool:
    """Whether ``stream`` is attached to a terminal, so a person can read or answer there.

    On Windows, ``isatty`` is true for any character device, ``NUL`` included, so the console
    itself is asked too. A stream with no file descriptor, such as a test's, is taken at its word.
    """
    if stream is None or not stream.isatty():
        return False
    try:
        descriptor = stream.fileno()
    except (OSError, ValueError):  # ``io.UnsupportedOperation`` is both
        return True
    return _is_console(descriptor)


def _is_console(descriptor: int) -> bool:
    if sys.platform == "win32":
        try:
            handle = msvcrt.get_osfhandle(descriptor)
        except OSError:  # a descriptor that isn't open
            return False
        mode = ctypes.c_ulong()
        # Succeeds only for a console handle, input or output.
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    # Elsewhere ``isatty`` is reliable: ``/dev/null`` isn't a terminal.
    return True


def _write_line(stream: TextIO, text: str) -> None:
    _write(stream, text + "\n")


def _write(stream: TextIO, text: str) -> None:
    # Encoding first means a stdout that can't show a character (Windows consoles, ASCII CI logs)
    # gets an escape instead of raising UnicodeEncodeError. ``encoding`` is None on a stream that
    # doesn't encode, such as ``io.StringIO``.
    encoding = stream.encoding or "utf-8"
    encoded = text.encode(encoding, "backslashreplace")
    stream.write(encoded.decode(encoding))
