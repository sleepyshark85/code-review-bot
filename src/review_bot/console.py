"""Safe writes to the standard streams, so a failing stream can't change the exit code."""

import sys
from typing import TextIO

from review_bot.render import escape_unprintable

__all__ = ["write_diagnostic", "write_output"]


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


def _write_line(stream: TextIO, text: str) -> None:
    # Encoding first means a stdout that can't show a character (Windows consoles, ASCII CI logs)
    # gets an escape instead of raising UnicodeEncodeError. ``encoding`` is None on a stream that
    # doesn't encode, such as ``io.StringIO``.
    encoding = stream.encoding or "utf-8"
    encoded = text.encode(encoding, "backslashreplace")
    stream.write(encoded.decode(encoding) + "\n")
