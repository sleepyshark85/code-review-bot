"""Check each model finding's anchor against the parsed diff, drop any that miss, and dedupe.

A model finding is kept only if its ``file`` names a reviewable new path and its ``line`` is an
added or context line in it. A near miss is dropped, never moved to a nearby line (ADR 0006). A
kept finding repeating an earlier one's file, category, line and rationale is a duplicate
(ADR 0003).
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from review_bot.diff_parser import ParsedDiff
from review_bot.findings import Finding, ReviewedFinding, Source
from review_bot.fingerprint import Fingerprinter

__all__ = ["DropReason", "ProposalCheck", "VerifyResult", "verify_findings"]

# A finding's ``file`` may carry one of these before a diff path (Phase 1 design, Line anchors).
_PATH_PREFIXES: Final = ("./", "a/", "b/")

type _LineTexts = Mapping[str, Mapping[int, str]]


class DropReason(StrEnum):
    """Why a model finding was dropped."""

    UNKNOWN_FILE = "unknown_file"
    SKIPPED_FILE = "skipped_file"
    LINE_NOT_IN_DIFF = "line_not_in_diff"


@dataclass(frozen=True, slots=True)
class ProposalCheck:
    """One proposed finding, and what verify decided about it.

    ``diff_path`` is the diff's new path that the finding's ``file`` names, or None if it names
    none. Exactly one of the last three fields is set: ``drop_reason`` if its anchor isn't in the
    diff, ``duplicate_of`` (the kept finding's fingerprint) if it repeats a kept finding, or else
    ``fingerprint``, its own, as it was kept.
    """

    proposed: Finding
    diff_path: str | None
    drop_reason: DropReason | None = None
    duplicate_of: str | None = None
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        """Raise ``ValueError`` unless exactly one outcome is set."""
        outcomes = (self.drop_reason, self.duplicate_of, self.fingerprint)
        if sum(outcome is not None for outcome in outcomes) != 1:
            raise ValueError(
                "A proposal check needs exactly one of drop_reason, duplicate_of and fingerprint."
            )


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """What verify kept, and its check of each proposed finding, in the order proposed.

    ``kept`` holds the rule findings in the order given, then the kept model findings in the order
    proposed, each with ``file`` set to the diff path it names.
    """

    kept: tuple[ReviewedFinding, ...]
    checks: tuple[ProposalCheck, ...]

    @property
    def dropped(self) -> tuple[ProposalCheck, ...]:
        """The checks that dropped their finding, in the order proposed."""
        return tuple(check for check in self.checks if check.drop_reason is not None)

    @property
    def deduped(self) -> tuple[ProposalCheck, ...]:
        """The checks that found their finding a duplicate, in the order proposed."""
        return tuple(check for check in self.checks if check.duplicate_of is not None)


def verify_findings(
    proposed: Sequence[Finding], rule_findings: Sequence[ReviewedFinding], parsed: ParsedDiff
) -> VerifyResult:
    """Check every proposed finding's file and line against ``parsed``, then dedupe the rest.

    Args:
        proposed: The model's findings.
        rule_findings: Findings from the rules, which anchor only to lines in the diff. They are
            kept first, in the order given, which ``run_rules`` makes file then line.
        parsed: The diff the findings are about.
    """
    line_texts = _anchor_line_texts(parsed)
    skipped_paths = {
        file.path for file in parsed.files if file.path is not None and file.skip_reason is not None
    }
    known_paths = line_texts.keys() | skipped_paths
    checks = _check_all(proposed, line_texts, known_paths)
    kept_ai = tuple(
        ReviewedFinding(
            check.proposed.model_copy(update={"file": check.diff_path}),
            Source.AI,
            None,
            check.fingerprint,
        )
        for check in checks
        if check.fingerprint is not None
    )
    return VerifyResult(kept=(*rule_findings, *kept_ai), checks=checks)


def _anchor_line_texts(parsed: ParsedDiff) -> dict[str, dict[int, str]]:
    """Each reviewable new path, with the text of its added and context lines by number.

    A path with no such line, such as one whose hunks only remove lines, maps to an empty dict.
    When sections sharing a path number a line twice, the first one's text is kept.
    """
    texts: dict[str, dict[int, str]] = {}
    for file in parsed.files:
        if file.path is None or file.skip_reason is not None:
            continue
        numbered = texts.setdefault(file.path, {})
        for hunk in file.hunks:
            for line in hunk.lines:
                if line.new_line is not None:
                    numbered.setdefault(line.new_line, line.text)
    return texts


def _drop_reason(
    finding: Finding, diff_path: str | None, line_texts: _LineTexts
) -> DropReason | None:
    if diff_path is None:
        return DropReason.UNKNOWN_FILE
    if diff_path not in line_texts:
        return DropReason.SKIPPED_FILE
    if finding.line not in line_texts[diff_path]:
        return DropReason.LINE_NOT_IN_DIFF
    return None


def _diff_path_named(file: str, known_paths: Collection[str]) -> str | None:
    """Match ``file`` exactly, then once more without one leading ``./``, ``a/`` or ``b/``."""
    if file in known_paths:
        return file
    for prefix in _PATH_PREFIXES:
        if file.startswith(prefix):
            stripped = file.removeprefix(prefix)
            return stripped if stripped in known_paths else None
    return None


def _check_all(
    proposed: Sequence[Finding], line_texts: _LineTexts, known_paths: Collection[str]
) -> tuple[ProposalCheck, ...]:
    """Drop each finding whose anchor misses; fingerprint the rest, or mark a repeat a duplicate.

    Two findings are alike when their path, rule part, line and rationale are all the same; the
    ordinal isn't compared. A rule finding's rule part is a ``RuleId``, never ``ai:``, so no rule
    finding shares a model finding's parts, and ordinals are counted among model findings only.
    """
    fingerprinter = Fingerprinter()
    kept_fingerprints: dict[tuple[str, str, int, str], str] = {}
    checks: list[ProposalCheck] = []
    for finding in proposed:
        diff_path = _diff_path_named(finding.file, known_paths)
        reason = _drop_reason(finding, diff_path, line_texts)
        # ``diff_path is None`` always has a reason; it is tested only so mypy narrows the type.
        if reason is not None or diff_path is None:
            checks.append(ProposalCheck(finding, diff_path, drop_reason=reason))
            continue
        rule_part = f"ai:{finding.category}"
        alike = (diff_path, rule_part, finding.line, finding.rationale)
        if alike in kept_fingerprints:
            checks.append(ProposalCheck(finding, diff_path, duplicate_of=kept_fingerprints[alike]))
            continue
        fingerprint = fingerprinter.fingerprint(
            file=diff_path, rule=rule_part, anchor_text=line_texts[diff_path][finding.line]
        )
        kept_fingerprints[alike] = fingerprint
        checks.append(ProposalCheck(finding, diff_path, fingerprint=fingerprint))
    return tuple(checks)
