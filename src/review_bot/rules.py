"""Checks that need no model, on the added lines of reviewable Python files.

A hunk is a fragment of a file, so each check reads the lines it shows, line by line, rather
than parsing Python. A hit counts only if the hunk adds a line it rests on.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from review_bot.diff_parser import DiffLine, FileDiff, Hunk, LineKind, ParsedDiff
from review_bot.findings import Category, Finding, ReviewedFinding, RuleId, Severity, Source
from review_bot.fingerprint import Fingerprinter

__all__ = ["RuleResult", "run_rules"]

_PYTHON_SUFFIX: Final = ".py"
# An ``except`` clause's line, bare or naming exceptions, and whatever follows its colon.
# ``[^:]*`` stops at the first colon, so a clause holding a slice or a ``lambda`` is missed.
_EXCEPT_CLAUSE: Final = re.compile(r"\s*except\b[^:]*:(?P<after_colon>.*)")
# A statement that does nothing, with an optional trailing comment.
_DOES_NOTHING: Final = re.compile(r"(?:pass|\.\.\.)\s*(?:#.*)?")
_SWALLOWED_EXCEPTION_RATIONALE: Final = (
    "Exception swallowed: the handler discards it, so failures pass silently. "
    "Handle it, log it or re-raise it."
)


@dataclass(frozen=True, slots=True)
class RuleResult:
    """The rules' findings, by file and line, and how many hits had no line number to anchor to.

    A hit in a hunk whose header has no readable new start is counted, not reported (ADR 0006).
    """

    findings: tuple[ReviewedFinding, ...]
    unanchored_hits: int


def run_rules(parsed: ParsedDiff) -> RuleResult:
    """Find swallowed exceptions in the reviewable ``.py`` files of ``parsed``."""
    anchored: list[tuple[str, int, str]] = []
    unanchored_hits = 0
    for file in parsed.files:
        path = _reviewable_python_path(file)
        if path is None:
            continue
        for hunk in file.hunks:
            for clause in _swallowing_except_clauses(hunk):
                # A header declaring new start 0 numbers its first line 0, which can't anchor.
                if clause.new_line is None or clause.new_line < 1:
                    unanchored_hits += 1
                else:
                    anchored.append((path, clause.new_line, clause.text))
    # The one place rule findings are ordered: verify keeps them as given. Sorted before
    # fingerprinting, so ordinals follow that order.
    anchored.sort(key=lambda hit: hit[:2])
    fingerprinter = Fingerprinter()
    findings = tuple(
        _swallowed_exception(path, line, text, fingerprinter) for path, line, text in anchored
    )
    return RuleResult(findings=findings, unanchored_hits=unanchored_hits)


def _reviewable_python_path(file: FileDiff) -> str | None:
    """The file's new path if the rules check it: not skipped, not deleted, and ending ``.py``."""
    if file.path is None or file.skip_reason is not None:
        return None
    return file.path if file.path.endswith(_PYTHON_SUFFIX) else None


def _swallowing_except_clauses(hunk: Hunk) -> Iterator[DiffLine]:
    """Yield each ``except`` line whose handler's first statement is ``pass`` or ``...``.

    Only the new version's lines are read. The clause or that statement must be added, so a
    handler the hunk only shows as context isn't reported.
    """
    new_version = [line for line in hunk.lines if line.kind is not LineKind.REMOVED]
    for index, line in enumerate(new_version):
        clause = _EXCEPT_CLAUSE.fullmatch(line.text)
        if clause is None:
            continue
        statement = _first_statement(new_version, index, clause["after_colon"])
        if statement is None:
            continue
        text, statement_line = statement
        is_added = LineKind.ADDED in (line.kind, statement_line.kind)
        if is_added and _DOES_NOTHING.fullmatch(text):
            yield line


def _first_statement(
    lines: Sequence[DiffLine], clause_index: int, after_colon: str
) -> tuple[str, DiffLine] | None:
    """The handler's first statement, stripped, and the line holding it.

    That is the text after the colon, in a one-line handler, or else the first line after
    ``lines[clause_index]`` that isn't blank or only a comment. None if the hunk ends before it.
    Indexed, not sliced or skipped through, so each clause costs only the lines it reads.
    """
    inline = after_colon.strip()
    if inline and not inline.startswith("#"):
        return inline, lines[clause_index]
    for index in range(clause_index + 1, len(lines)):
        line = lines[index]
        text = line.text.strip()
        if text and not text.startswith("#"):
            return text, line
    return None


def _swallowed_exception(
    path: str, line: int, text: str, fingerprinter: Fingerprinter
) -> ReviewedFinding:
    rule = RuleId.SWALLOWED_EXCEPTION
    # Not validated, because a diff path may hold lone surrogates, standing for bytes that aren't
    # UTF-8, which the model's schema rejects. Every other field is a constant or a line >= 1.
    finding = Finding.model_construct(
        file=path,
        line=line,
        severity=Severity.MEDIUM,
        category=Category.RELIABILITY,
        rationale=_SWALLOWED_EXCEPTION_RATIONALE,
        confidence=1.0,
    )
    fingerprint = fingerprinter.fingerprint(file=path, rule=rule, anchor_text=text)
    return ReviewedFinding(finding, Source.RULE, rule, fingerprint)
