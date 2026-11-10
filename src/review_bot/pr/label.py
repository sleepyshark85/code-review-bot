"""Judge the would-be comments in downloaded shadow reports, appending a label for each.

Reports are untrusted: ``pr.reports`` reads them, and their text is shown only escaped.
"""

import contextlib
import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, TextIO

from review_bot.errors import ConfigError
from review_bot.pr.feedback import (
    FeedbackLabel,
    FeedbackRecord,
    RejectReason,
    ShadowFeedback,
    render_feedback_line,
)
from review_bot.pr.reports import BadLabelLine, parse_labels, read_reports, skipped_line
from review_bot.pr.units import WouldBeComment, would_be_comments
from review_bot.render import escape_unprintable
from review_bot.secret_scan import redact

__all__ = ["LABELLING_STOPPED", "LabellingResult", "run_labelling"]

LABELLING_STOPPED: Final = "Labelling stopped."

_VERDICT_PROMPT: Final = "[a]ccept [r]eject [s]kip: "
_INVALID_VERDICT: Final = "Answer a to accept, r to reject, or s to skip it until next time."
_REASON_PROMPT: Final = "[w]rong [n]ot worth it [l]ine wrong [d]uplicate: "
_INVALID_REASON: Final = (
    "Answer w if it's wrong, n if it's not worth a comment, l if its line is wrong, or d if "
    "it's a duplicate."
)
_REASONS: Final[Mapping[str, RejectReason]] = {
    "w": RejectReason.WRONG,
    "n": RejectReason.NOT_WORTH_IT,
    "l": RejectReason.WRONG_LINE,
    "d": RejectReason.DUPLICATE,
}

# A verdict and, on a reject, its reason; None when the person skips.
type _Verdict = tuple[FeedbackLabel, RejectReason | None] | None


@dataclass(frozen=True, slots=True)
class LabellingResult:
    """How a session went: would-be comments judged, skipped and left, and report files skipped."""

    judged: int
    skipped: int
    remaining: int
    files_skipped: int


def run_labelling(
    reports_dir: Path,
    labels_path: Path,
    *,
    read_line: Callable[[str], str],
    write_line: Callable[[str], None],
    clock: Callable[[], datetime],
) -> LabellingResult:
    """Ask about each would-be comment in ``reports_dir`` not yet labelled in ``labels_path``.

    Each verdict is appended to ``labels_path`` before the next question. ``s`` skips one until
    the next session. End of input or Ctrl+C stops the session, keeping what was appended.

    Args:
        reports_dir: Searched at any depth for ``*.json`` shadow reports. A file that can't be
            read, is over 10 MB or isn't a ``shadow/v1`` report is skipped with one line.
        labels_path: The ``feedback/v1`` JSONL file; created when the first question is due.
            Only its ``shadow`` records mark a would-be comment as labelled; records of other
            channels are checked, then ignored.
        read_line: Shows a prompt and returns the line answered; raises ``EOFError`` at the end
            of input.
        write_line: Writes one line where the person reads the prompts.
        clock: The current time, for ``recorded_at``.

    Raises:
        ConfigError: If ``reports_dir`` isn't a directory, or ``labels_path`` is in a
            directory that doesn't exist, can't be read, holds a line that isn't a
            ``feedback/v1`` record, or can't be written. Only the last can happen once a
            question has been asked, and records appended before it stay.
    """
    existing = _read_labels(labels_path)
    labelled = {
        record.unit
        for record in _parse_labels(existing, labels_path)
        if isinstance(record, ShadowFeedback)
    }
    reports, skipped_files = read_reports(reports_dir, flag="--reports")
    for path, problem in skipped_files:
        write_line(escape_unprintable(skipped_line(path, problem)))
    comments = [comment for comment in would_be_comments(reports) if comment.unit not in labelled]
    judged = skipped = 0
    if comments:
        with _open_for_append(labels_path) as labels:
            append = functools.partial(_append, labels, labels_path)
            if existing and not existing.endswith(b"\n"):
                # Otherwise the first new record would join the last line.
                append("")
            judged, skipped = _ask_all(comments, read_line, write_line, append, clock)
    return LabellingResult(
        judged=judged,
        skipped=skipped,
        remaining=len(comments) - judged - skipped,
        files_skipped=len(skipped_files),
    )


def _read_labels(path: Path) -> bytes:
    """The labels file's bytes; none if it doesn't exist yet.

    Raises:
        ConfigError: If it exists but can't be read, or its directory doesn't exist, so it could
            never be written.
    """
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        if not path.parent.is_dir():
            raise ConfigError(
                f"The directory of the labels file --labels {redact(str(path))} doesn't exist. "
                "Create it, or pass a file in a directory that exists."
            ) from exc
        return b""
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise ConfigError(
            f"Can't read the labels file --labels {redact(str(path))}: {reason}. Pass a file you "
            "can read and write."
        ) from exc


def _parse_labels(raw: bytes, path: Path) -> list[FeedbackRecord]:
    """Parse each line of the labels file, ignoring blank ones.

    Raises:
        ConfigError: Naming the first line that isn't a ``feedback/v1`` record.
    """
    records = parse_labels(raw)
    if isinstance(records, BadLabelLine):
        raise ConfigError(
            f"Line {records.number} of the labels file --labels {redact(str(path))} isn't a "
            f"feedback/v1 record ({records.problem}). Fix or remove that line, then run label "
            "again."
        )
    return records


def _open_for_append(path: Path) -> TextIO:
    try:
        return path.open("a", encoding="ascii", newline="\n")
    except OSError as exc:
        raise _unwritable(path, exc) from exc


def _append(labels: TextIO, path: Path, line: str) -> None:
    """Write ``line`` and a line break, flushed so it survives the session ending."""
    try:
        labels.write(line + "\n")
        labels.flush()
    except OSError as exc:
        raise _unwritable(path, exc) from exc


def _unwritable(path: Path, exc: OSError) -> ConfigError:
    reason = exc.strerror or type(exc).__name__
    return ConfigError(
        f"Can't write the labels file --labels {redact(str(path))}: {reason}. Pass a file you "
        "can write to; labels already written stay."
    )


def _ask_all(
    comments: Sequence[WouldBeComment],
    read_line: Callable[[str], str],
    write_line: Callable[[str], None],
    append: Callable[[str], None],
    clock: Callable[[], datetime],
) -> tuple[int, int]:
    """Ask about each would-be comment in turn, appending each verdict at once.

    Returns:
        How many were judged, and how many skipped, before the end or an interruption.
    """
    judged = skipped = 0
    for position, comment in enumerate(comments, start=1):
        try:
            write_line(_describe(comment, f"{position} of {len(comments)}"))
            verdict = _ask(read_line, write_line)
            if verdict is None:
                skipped += 1
                continue
            label, reason = verdict
            append(render_feedback_line(_record(comment, label, reason, clock())))
            judged += 1
        except (EOFError, KeyboardInterrupt):
            # A second Ctrl+C only loses this notice, never the labels already appended.
            with contextlib.suppress(KeyboardInterrupt):
                # A terminal echoes no line break for end of input or Ctrl+C, so end the prompt's.
                write_line("")
                write_line(LABELLING_STOPPED)
            break
    return judged, skipped


def _describe(comment: WouldBeComment, position: str) -> str:
    """The would-be comment on one line, so report text can't forge more lines or drive a TTY."""
    unit, entry = comment.unit, comment.entry
    reports = "1 report" if comment.report_count == 1 else f"{comment.report_count} reports"
    # Redacted like every path written to stderr (Phase 1 design, Secrets).
    return escape_unprintable(
        f"Would-be comment {position}: {unit.repo}#{unit.pr} {redact(entry.file)}:{entry.line} "
        f"[{entry.severity}] source {entry.source}, rule {entry.rule_part}, in {reports}: "
        f"{entry.body}"
    )


def _ask(read_line: Callable[[str], str], write_line: Callable[[str], None]) -> _Verdict:
    while True:
        answer = read_line(_VERDICT_PROMPT).strip().lower()
        if answer == "a":
            return FeedbackLabel.ACCEPT, None
        if answer == "r":
            return FeedbackLabel.REJECT, _ask_reason(read_line, write_line)
        if answer == "s":
            return None
        write_line(_INVALID_VERDICT)


def _ask_reason(read_line: Callable[[str], str], write_line: Callable[[str], None]) -> RejectReason:
    while (answer := read_line(_REASON_PROMPT).strip().lower()) not in _REASONS:
        write_line(_INVALID_REASON)
    return _REASONS[answer]


def _record(
    comment: WouldBeComment,
    label: FeedbackLabel,
    reason: RejectReason | None,
    recorded_at: datetime,
) -> ShadowFeedback:
    unit, entry = comment.unit, comment.entry
    return ShadowFeedback(
        channel="shadow",
        repo=unit.repo,
        pr=unit.pr,
        fingerprint=entry.fingerprint,
        file=unit.file,
        rule_part=unit.rule_part,
        anchor=unit.anchor,
        index=unit.index,
        source=entry.source,
        severity=entry.severity,
        label=label,
        reason=reason,
        run_id=comment.run_id,
        model=comment.model,
        prompt_version=comment.prompt_version,
        recorded_at=recorded_at,
    )
