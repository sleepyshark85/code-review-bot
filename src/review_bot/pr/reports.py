"""Read downloaded shadow reports and label files, each at most 10 MB and parsed by schema.

Both are untrusted: a file that can't be used comes back with why, never quoting its content,
and any text from it must be escaped before it is shown.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from review_bot.errors import ConfigError
from review_bot.pr.feedback import FeedbackRecord, parse_feedback_line
from review_bot.pr.units import ShadowFile
from review_bot.secret_scan import redact

__all__ = [
    "MAX_FILE_BYTES",
    "BadLabelLine",
    "parse_labels",
    "read_labels",
    "read_report",
    "read_reports",
    "skipped_line",
]

# The most bytes read from any one report or labels file.
MAX_FILE_BYTES: Final = 10_000_000


def read_reports(directory: Path) -> tuple[list[ShadowFile], list[tuple[Path, str]]]:
    """Every ``*.json`` file under ``directory``, at any depth, as a report or with why not.

    Files are taken in path order, so reports finished at the same moment come in a repeatable
    order.

    Raises:
        ConfigError: If ``directory`` isn't a directory.
    """
    if not directory.is_dir():
        raise ConfigError(
            f"--reports {redact(str(directory))} isn't a directory. Pass the directory you "
            "downloaded the shadow report artifacts to."
        )
    reports: list[ShadowFile] = []
    skipped: list[tuple[Path, str]] = []
    for path in sorted(directory.rglob("*.json")):
        if not path.is_file():
            continue
        report = read_report(path)
        if isinstance(report, ShadowFile):
            reports.append(report)
        else:
            skipped.append((path, report))
    return reports, skipped


def read_report(path: Path) -> ShadowFile | str:
    """The report at ``path``, or why it can't be used.

    It can't be when it is unreadable, over 10 MB, not JSON, or not ``shadow/v1``.
    """
    raw = _read_bounded(path)
    if isinstance(raw, str):
        return raw
    try:
        return ShadowFile.model_validate_json(raw, strict=True)
    except ValidationError as exc:
        if exc.errors()[0]["type"] == "json_invalid":
            return "not JSON"
        return f"not a shadow/v1 report ({_first_problem(exc)})"


def read_labels(path: Path) -> list[FeedbackRecord] | str:
    """The ``feedback/v1`` records in the JSONL file at ``path``, or why it can't be used.

    Blank lines are ignored. It can't be used when it is unreadable, over 10 MB, or any line
    isn't a ``feedback/v1`` record.
    """
    raw = _read_bounded(path)
    if isinstance(raw, str):
        return raw
    records = parse_labels(raw)
    if isinstance(records, BadLabelLine):
        return f"line {records.number} isn't a feedback/v1 record ({records.problem})"
    return records


@dataclass(frozen=True, slots=True)
class BadLabelLine:
    """The first line of a labels file that isn't a ``feedback/v1`` record, and what is wrong."""

    number: int
    problem: str


def parse_labels(raw: bytes) -> list[FeedbackRecord] | BadLabelLine:
    """Each line of a labels file as a ``feedback/v1`` record, ignoring blank lines.

    Returns:
        The records, or the first line that isn't one.
    """
    records = []
    for number, line in enumerate(raw.split(b"\n"), start=1):
        if not line.strip():
            continue
        try:
            records.append(parse_feedback_line(line))
        except ValidationError as exc:
            return BadLabelLine(number, _first_problem(exc))
    return records


def skipped_line(path: Path, problem: str) -> str:
    """The stderr line for a file left out, with its path redacted like every path shown."""
    return f"review-bot pr: skipped {redact(str(path))}: {problem}"


def _first_problem(exc: ValidationError) -> str:
    """Where the first problem is and what it is; pydantic's messages never quote the input."""
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"]) or "top level"
    return f"{location}: {error['msg']}"


def _read_bounded(path: Path) -> bytes | str:
    """The file's bytes, or why not: it can't be read, or it is over ``MAX_FILE_BYTES``."""
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        return f"can't be read ({exc.strerror or type(exc).__name__})"
    if len(raw) > MAX_FILE_BYTES:
        return f"over {MAX_FILE_BYTES} bytes"
    return raw
