"""Append one JSON record per line to the audit log (ADR 0005).

Records hold decisions, identifiers and hashes, never diff lines, rationale or model text.
"""

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from review_bot.errors import AuditError
from review_bot.secret_scan import redact

__all__ = ["DEFAULT_AUDIT_LOG_PATH", "AuditLog", "JsonValue", "Stage", "file_fields"]

# Relative, so it lands under the working directory.
DEFAULT_AUDIT_LOG_PATH: Final = Path(".review-bot") / "audit.jsonl"

_SCHEMA: Final = "audit/v1"
# Ignores everything in the default directory, itself included, in any repository.
_IGNORE_EVERYTHING: Final = "*\n"
# Git's C-quoting: these bytes get a short escape, other control and non-ASCII bytes an octal one.
_SHORT_ESCAPES: Final = {
    ord("\a"): r"\a",
    ord("\b"): r"\b",
    ord("\t"): r"\t",
    ord("\n"): r"\n",
    ord("\v"): r"\v",
    ord("\f"): r"\f",
    ord("\r"): r"\r",
    ord('"'): r"\"",
    ord("\\"): r"\\",
}
_PRINTABLE_ASCII: Final = range(0x20, 0x7F)

type JsonValue = str | int | float | bool | Sequence[JsonValue] | Mapping[str, JsonValue] | None


class Stage(StrEnum):
    """The pipeline stage a record comes from."""

    RUN = "run"
    STATIC = "static"
    PARSE = "parse"
    AGENT = "agent"


class AuditLog:
    """One run's records, appended to a JSONL file that may hold earlier runs."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime]) -> None:
        """Check that ``path`` can be appended to, creating its missing directories.

        Creating the default directory also writes a ``.gitignore`` there, so the log is never
        committed by accident.

        Args:
            path: The log file.
            clock: Returns the current time, timezone-aware.

        Raises:
            AuditError: If the file or its directory can't be created or written.
        """
        self.path = path
        self.run_id = str(uuid.uuid4())
        self._clock = clock
        try:
            if self._create_directory() and path == DEFAULT_AUDIT_LOG_PATH:
                (path.parent / ".gitignore").write_text(_IGNORE_EVERYTHING, encoding="utf-8")
            # Opening to append, unlike ``touch``, proves that an existing file is writable.
            with path.open("a", encoding="ascii"):
                pass
        except OSError as exc:
            raise self._unwritable(exc) from exc

    def record(self, stage: Stage, event: str, data: Mapping[str, JsonValue]) -> None:
        """Append one record. Every string in ``data`` passes ``redact`` first.

        The line is pure ASCII: anything else is written as a JSON escape.

        Raises:
            AuditError: If the record can't be appended.
            ValueError: If the clock returns a naive datetime, or a string holds a lone surrogate,
                which many JSON readers reject. ``file_fields`` writes such a path portably.
        """
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("The audit log's clock must return a timezone-aware datetime.")
        line = json.dumps(
            {
                "schema": _SCHEMA,
                "run_id": self.run_id,
                "ts": now.astimezone(UTC).isoformat(),
                "stage": stage,
                "event": event,
                "data": _redacted(data),
            },
            allow_nan=False,
        )
        try:
            with self.path.open("a", encoding="ascii", newline="\n") as log:
                log.write(line + "\n")
        except OSError as exc:
            raise self._unwritable(exc) from exc

    def _create_directory(self) -> bool:
        """Create the log's missing directories, and say whether its own directory was new."""
        try:
            self.path.parent.mkdir(parents=True)
        except FileExistsError:
            return False
        return True

    def _unwritable(self, exc: OSError) -> AuditError:
        # Every path written anywhere passes ``redact`` (Phase 1 design, Secrets).
        path = redact(str(self.path))
        reason = exc.strerror or type(exc).__name__
        return AuditError(
            f"Can't write the audit log {path}: {reason}. Pass a writable file with --audit-log "
            "or REVIEW_BOT_AUDIT_LOG."
        )


def file_fields(path: str) -> dict[str, JsonValue]:
    """Return a record's ``file`` for a diff path, plus ``path_quoted`` if it had to be quoted.

    A valid UTF-8 path is written as it is. A path holding lone surrogates, which stand for bytes
    that aren't valid UTF-8 (``surrogateescape``), is written in git's C-quoted form (Phase 1
    design, Paths in audit/v1). The path passes ``redact`` before quoting, so a secret is matched
    in its plain form.

    Raises:
        UnicodeEncodeError: If ``path`` holds a surrogate that ``surrogateescape`` can't produce.
    """
    path = redact(path)
    if _has_lone_surrogate(path):
        return {"file": _c_quoted(path.encode("utf-8", "surrogateescape")), "path_quoted": True}
    return {"file": path}


def _c_quoted(raw: bytes) -> str:
    return '"' + "".join(_c_escaped(byte) for byte in raw) + '"'


def _c_escaped(byte: int) -> str:
    if byte in _SHORT_ESCAPES:
        return _SHORT_ESCAPES[byte]
    if byte in _PRINTABLE_ASCII:
        return chr(byte)
    return f"\\{byte:03o}"


def _has_lone_surrogate(text: str) -> bool:
    """Whether ``text`` holds a lone surrogate, the only thing that stops it encoding as UTF-8."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _redacted(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        text = redact(value)
        if _has_lone_surrogate(text):
            raise ValueError(
                "An audit record can't hold a lone surrogate. Pass diff paths through file_fields."
            )
        return text
    if isinstance(value, Mapping):
        return {key: _redacted(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [_redacted(item) for item in value]
    return value
