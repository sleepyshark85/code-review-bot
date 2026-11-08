"""Append one JSON record per line to the audit log (ADR 0005).

Records hold decisions, identifiers and hashes, never diff lines, rationale or model text. The
exceptions are a failed call's CLI diagnostics and provider warnings, both after
``scrub_diagnostics``.
"""

import json
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from review_bot.errors import AuditError
from review_bot.report import has_lone_surrogate, path_fields
from review_bot.secret_scan import redact

__all__ = [
    "DEFAULT_AUDIT_LOG_PATH",
    "EVAL_LIVE_AUDIT_LOG_PATH",
    "AuditLog",
    "JsonValue",
    "Stage",
    "file_fields",
    "scrub_diagnostics",
    "utc_now",
]

# Relative, so it lands under the working directory.
DEFAULT_AUDIT_LOG_PATH: Final = Path(".review-bot") / "audit.jsonl"
# The live eval's log, in the same directory, whatever the review's settings say (P1-S16).
EVAL_LIVE_AUDIT_LOG_PATH: Final = DEFAULT_AUDIT_LOG_PATH.parent / "eval-live-audit.jsonl"
# The logs whose directory, when a log creates it, gets a ``.gitignore``.
_GITIGNORED_LOG_PATHS: Final = frozenset({DEFAULT_AUDIT_LOG_PATH, EVAL_LIVE_AUDIT_LOG_PATH})

_SCHEMA: Final = "audit/v1"
# Ignores everything in the default directory, itself included, in any repository.
_IGNORE_EVERYTHING: Final = "*\n"
# What ``scrub_diagnostics`` keeps: the tail of the text, after scrubbing (Phase 1 design, Q5).
_MAX_DIAGNOSTIC_LINES: Final = 20
_MAX_DIAGNOSTIC_CHARS: Final = 2000
# Shorter diff lines, such as `}` or `else:`, are so common that matching them would remove lines
# that quote nothing.
_MIN_QUOTED_DIFF_LINE_CHARS: Final = 8
_QUOTES_THE_DIFF: Final = "[removed: quotes the diff]"
_CREDENTIAL_VARIABLES: Final = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
_REDACTED: Final = "[REDACTED]"
_DIFF_MARKERS: Final = ("+", "-", " ")
# Bounds the memory a very long line's windows take: only one chunk's are held at a time.
_WINDOWS_PER_CHUNK: Final = 65_536

# A diff line's quoted forms, by their first 8 characters, then by their length.
type _QuoteIndex = dict[str, dict[int, set[str]]]

type JsonValue = str | int | float | bool | Sequence[JsonValue] | Mapping[str, JsonValue] | None


class Stage(StrEnum):
    """The pipeline stage a record comes from."""

    RUN = "run"
    STATIC = "static"
    PARSE = "parse"
    AGENT = "agent"
    VERIFY = "verify"
    POLICY = "policy"
    TRIAGE = "triage"
    CONTEXT = "context"


class AuditLog:
    """One run's records, appended to a JSONL file that may hold earlier runs."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime]) -> None:
        """Check that ``path`` can be appended to, creating its missing directories.

        Creating the default directory, for the review's log or the live eval's, also writes a
        ``.gitignore`` there, so the log is never committed by accident.

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
            if self._create_directory() and path in _GITIGNORED_LOG_PATHS:
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


def utc_now() -> datetime:
    """The current time in UTC: the clock every real audit log uses."""
    return datetime.now(UTC)


def file_fields(path: str) -> dict[str, JsonValue]:
    """Return a record's ``file`` for a diff path, as ``report.path_fields`` writes it.

    The path passes ``redact`` before quoting, so a secret is matched in its plain form.

    Raises:
        UnicodeEncodeError: If ``path`` holds a surrogate that ``surrogateescape`` can't produce.
    """
    return {**path_fields(redact(path))}  # copied to widen the type to dict[str, JsonValue]


def _redacted(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        text = redact(value)
        if has_lone_surrogate(text):
            raise ValueError(
                "An audit record can't hold a lone surrogate. Pass diff paths through file_fields."
            )
        return text
    if isinstance(value, Mapping):
        return {key: _redacted(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [_redacted(item) for item in value]
    return value


def scrub_diagnostics(
    lines: Sequence[str], *, diff_text: str, environ: Mapping[str, str]
) -> tuple[str, ...]:
    """Return the tail of ``lines`` that is safe to log: at most 20 lines and 2,000 characters.

    A lone surrogate becomes a backslash escape. Every credential variable's value in
    ``environ``, and every secret ``redact`` finds, becomes ``[REDACTED]``, and each line quoting
    a line of ``diff_text`` becomes ``[removed: quotes the diff]``. Secrets are redacted over
    every line before the line cap, and all scrubbing runs before the character cap, so no cut
    can leave part of a secret or a private key block. A diff line counts once it is 8
    characters long without its marker and outer whitespace. Best-effort: a fragment shorter
    than a diff line, a reworded diff line or an unlisted secret format passes (ADR 0005).
    """
    if not lines:
        return ()
    # The CLI's JSON can spell a lone surrogate, which ``record`` rejects, so it becomes an escape.
    text = "\n".join(lines).encode("utf-8", "backslashreplace").decode("utf-8")
    for name in _CREDENTIAL_VARIABLES:
        # An empty value would match between every pair of characters.
        if value := environ.get(name):
            text = text.replace(value, _REDACTED)
    # The whole text at once, so a private key block split over lines is found.
    tail = redact(text).split("\n")[-_MAX_DIAGNOSTIC_LINES:]
    quoted = _by_first_chars(_quoted_forms(diff_text))
    kept = [_QUOTES_THE_DIFF if _quotes_any(line, quoted) else line for line in tail]
    return _last_chars(kept, _MAX_DIAGNOSTIC_CHARS)


def _quoted_forms(diff_text: str) -> set[str]:
    """Each long enough diff line, without its marker or outer whitespace, plain and in JSON."""
    contents = {
        line[1:] if line.startswith(_DIFF_MARKERS) else line for line in diff_text.split("\n")
    }
    stripped = {content.strip() for content in contents}
    long_enough = {content for content in stripped if len(content) >= _MIN_QUOTED_DIFF_LINE_CHARS}
    # Slicing off the quotes leaves the escaped body, as the CLI's JSON would hold it.
    as_json = {json.dumps(content, ensure_ascii=False)[1:-1] for content in long_enough}
    return long_enough | as_json


def _by_first_chars(forms: Iterable[str]) -> _QuoteIndex:
    """Index ``forms``, each at least 8 characters long, by their first 8 characters and length."""
    index: _QuoteIndex = defaultdict(lambda: defaultdict(set))
    for form in forms:
        index[form[:_MIN_QUOTED_DIFF_LINE_CHARS]][len(form)].add(form)
    return index


def _quotes_any(line: str, quoted: _QuoteIndex) -> bool:
    """Whether ``line`` holds any form in ``quoted``, anywhere in it.

    Near linear in the line, however large the diff: the line's 8-character windows meet the
    index's keys in set operations, and each place a key occurs costs one set lookup per length
    of the forms it starts.
    """
    for key in _keys_in(line, quoted.keys()):
        by_length = quoted[key]
        start = line.find(key)
        while start != -1:
            if any(line[start : start + length] in forms for length, forms in by_length.items()):
                return True
            start = line.find(key, start + 1)
    return False


def _keys_in(line: str, keys: AbstractSet[str]) -> set[str]:
    """The ``keys`` that occur in ``line``, all of them 8 characters long.

    The line's windows are built in C rather than in a Python loop, one chunk at a time, and only
    the hits are kept, so memory stays at about one chunk's windows however long the line is.
    """
    width = _MIN_QUOTED_DIFF_LINE_CHARS
    starts_end = len(line) - width + 1
    hits: set[str] = set()
    for chunk in range(0, max(starts_end, 0), _WINDOWS_PER_CHUNK):
        starts = range(chunk, min(chunk + _WINDOWS_PER_CHUNK, starts_end))
        slices = map(slice, starts, range(starts.start + width, starts.stop + width))
        hits |= keys & set(map(line.__getitem__, slices))
    return hits


def _last_chars(lines: Sequence[str], limit: int) -> tuple[str, ...]:
    """The last ``limit`` characters of ``lines``, cutting the earliest line kept if need be."""
    kept: list[str] = []
    remaining = limit
    for line in reversed(lines):
        if remaining <= 0:
            break
        kept.append(line[-remaining:])
        remaining -= len(kept[-1])
    return tuple(reversed(kept))
