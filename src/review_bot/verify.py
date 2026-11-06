"""Check each model finding's anchor against the parsed diff, and drop any that miss.

A model finding is kept only if its ``file`` names a reviewable new path and its ``line`` is an
added or context line in it. A near miss is dropped, never moved to a nearby line (ADR 0006).
"""

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from review_bot.diff_parser import ParsedDiff
from review_bot.findings import Finding, ReviewedFinding, Source

__all__ = ["AnchorCheck", "DropReason", "VerifyResult", "verify_findings"]

# A finding's ``file`` may carry one of these before a diff path (Phase 1 design, Line anchors).
_PATH_PREFIXES: Final = ("./", "a/", "b/")


class DropReason(StrEnum):
    """Why a model finding was dropped."""

    UNKNOWN_FILE = "unknown_file"
    SKIPPED_FILE = "skipped_file"
    LINE_NOT_IN_DIFF = "line_not_in_diff"


@dataclass(frozen=True, slots=True)
class AnchorCheck:
    """One proposed finding, and what verify decided about it.

    ``diff_path`` is the diff's new path that the finding's ``file`` names, or None if it names
    none. ``drop_reason`` is None for a kept finding.
    """

    proposed: Finding
    diff_path: str | None
    drop_reason: DropReason | None


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """What verify kept, and its check of each proposed finding, in the order proposed.

    ``kept`` holds the rule findings by file and line, then the kept model findings in the order
    proposed, each with ``file`` set to the diff path it names.
    """

    kept: tuple[ReviewedFinding, ...]
    checks: tuple[AnchorCheck, ...]

    @property
    def dropped(self) -> tuple[AnchorCheck, ...]:
        """The checks that dropped their finding, in the order proposed."""
        return tuple(check for check in self.checks if check.drop_reason is not None)


def verify_findings(
    proposed: Sequence[Finding], rule_findings: Sequence[ReviewedFinding], parsed: ParsedDiff
) -> VerifyResult:
    """Check every proposed finding's file and line against ``parsed``.

    Args:
        proposed: The model's findings.
        rule_findings: Findings from the rules, which anchor only to lines in the diff.
        parsed: The diff the findings are about.
    """
    anchor_lines = _anchor_lines(parsed)
    skipped_paths = {
        file.path for file in parsed.files if file.path is not None and file.skip_reason is not None
    }
    known_paths = anchor_lines.keys() | skipped_paths
    checks = tuple(_check(finding, anchor_lines, known_paths) for finding in proposed)
    kept_ai = tuple(
        ReviewedFinding(check.proposed.model_copy(update={"file": check.diff_path}), Source.AI)
        for check in checks
        if check.drop_reason is None
    )
    by_file_and_line = sorted(
        rule_findings, key=lambda reviewed: (reviewed.finding.file, reviewed.finding.line)
    )
    return VerifyResult(kept=(*by_file_and_line, *kept_ai), checks=checks)


def _anchor_lines(parsed: ParsedDiff) -> dict[str, set[int]]:
    """Each reviewable new path, with the new-version numbers of its added and context lines.

    A path with no such line, such as one whose hunks only remove lines, maps to an empty set.
    """
    lines: defaultdict[str, set[int]] = defaultdict(set)
    for file in parsed.files:
        if file.path is None or file.skip_reason is not None:
            continue
        numbers = lines[file.path]
        for hunk in file.hunks:
            numbers.update(line.new_line for line in hunk.lines if line.new_line is not None)
    return dict(lines)


def _check(
    finding: Finding, anchor_lines: Mapping[str, set[int]], known_paths: Collection[str]
) -> AnchorCheck:
    diff_path = _diff_path_named(finding.file, known_paths)
    if diff_path is None:
        reason: DropReason | None = DropReason.UNKNOWN_FILE
    elif diff_path not in anchor_lines:
        reason = DropReason.SKIPPED_FILE
    elif finding.line not in anchor_lines[diff_path]:
        reason = DropReason.LINE_NOT_IN_DIFF
    else:
        reason = None
    return AnchorCheck(finding, diff_path, reason)


def _diff_path_named(file: str, known_paths: Collection[str]) -> str | None:
    """Match ``file`` exactly, then once more without one leading ``./``, ``a/`` or ``b/``."""
    if file in known_paths:
        return file
    for prefix in _PATH_PREFIXES:
        if file.startswith(prefix):
            stripped = file.removeprefix(prefix)
            return stripped if stripped in known_paths else None
    return None
