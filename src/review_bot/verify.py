"""Check each model finding's anchor against the parsed diff, drop any that miss, and dedupe.

A model finding is kept only if its ``file`` names a reviewable new path and its ``line`` is an
added or context line in it. A near miss is dropped, never moved to a nearby line (ADR 0006). A
finding that passes is a duplicate if it repeats a nearby rule finding at no higher severity, or
an earlier model finding's file, rule part, line and rationale (ADR 0003). Each finding's cited
evidence is checked too: only the spans the model was shown are kept (``context.evidence``).
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from review_bot.context.evidence import EvidenceCheck, ShownLines, verify_evidence
from review_bot.context.ledger import ToolCall
from review_bot.diff_parser import Hunk, ParsedDiff
from review_bot.findings import Finding, ReviewedFinding, RuleId, Source
from review_bot.fingerprint import Fingerprinter

__all__ = ["DropReason", "ProposalCheck", "VerifyResult", "diff_path_named", "verify_findings"]

# A finding's ``file`` may carry one of these before a diff path (Phase 1 design, Line anchors).
_PATH_PREFIXES: Final = ("./", "a/", "b/")
# How many new-version lines apart a model finding and the rule finding it repeats may be.
_NEARBY_LINES: Final = 3
_NO_EVIDENCE: Final = EvidenceCheck(kept=(), dropped=0)


@dataclass(frozen=True, slots=True)
class _AnchorLine:
    """An added or context line's text, and the hunk showing it."""

    text: str
    hunk: Hunk


type _AnchorLines = Mapping[str, Mapping[int, _AnchorLine]]
type _RuleFindingsByLine = Mapping[
    tuple[str, RuleId | None], Mapping[int, Sequence[ReviewedFinding]]
]


class DropReason(StrEnum):
    """Why a model finding was dropped."""

    UNKNOWN_FILE = "unknown_file"
    SKIPPED_FILE = "skipped_file"
    LINE_NOT_IN_DIFF = "line_not_in_diff"


@dataclass(frozen=True, slots=True)
class ProposalCheck:
    """One proposed finding, and what verify decided about it.

    ``diff_path`` is the diff's new path that the finding's ``file`` names, or None if it names
    none. Exactly one of ``drop_reason``, ``duplicate_of`` and ``fingerprint`` is set:
    ``drop_reason`` if its anchor isn't in the diff, ``duplicate_of`` (the kept finding's
    fingerprint) if it repeats a kept finding, or else ``fingerprint``, its own, as it was kept.
    ``evidence`` is the check of its cited spans, whatever the outcome.
    """

    proposed: Finding
    diff_path: str | None
    drop_reason: DropReason | None = None
    duplicate_of: str | None = None
    fingerprint: str | None = None
    evidence: EvidenceCheck = _NO_EVIDENCE

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
    proposed, each with ``file`` set to the diff path it names and ``evidence`` to its kept spans.
    """

    kept: tuple[ReviewedFinding, ...]
    checks: tuple[ProposalCheck, ...]

    @property
    def dropped(self) -> tuple[ProposalCheck, ...]:
        """The checks that dropped their finding, in the order proposed."""
        return tuple(check for check in self.checks if check.drop_reason is not None)

    @property
    def ai_anchor_valid(self) -> tuple[ProposalCheck, ...]:
        """The checks whose anchor is in the diff, duplicates included, in the order proposed.

        So the model's findings that passed the anchor check, before dedup.
        """
        return tuple(check for check in self.checks if check.drop_reason is None)

    @property
    def deduped(self) -> tuple[ProposalCheck, ...]:
        """The checks that found their finding a duplicate, in the order proposed."""
        return tuple(check for check in self.checks if check.duplicate_of is not None)


def verify_findings(
    proposed: Sequence[Finding],
    rule_findings: Sequence[ReviewedFinding],
    parsed: ParsedDiff,
    *,
    ledger: Iterable[ToolCall] = (),
    is_diff_head: bool = False,
) -> VerifyResult:
    """Check every proposed finding's file and line against ``parsed``, then dedupe the rest.

    Args:
        proposed: The model's findings.
        rule_findings: Findings from the rules, which anchor only to lines in the diff. They are
            kept first, in the order given, which ``run_rules`` makes file then line.
        parsed: The diff the findings are about.
        ledger: The tool calls of a context review, whose returned lines evidence may cite.
        is_diff_head: Whether the tools read the commit the diff ends at, so their lines count
            in the files the diff changes too (``context.evidence.ShownLines``).
    """
    anchor_lines = _anchor_lines(parsed)
    skipped_paths = {
        file.path for file in parsed.files if file.path is not None and file.skip_reason is not None
    }
    known_paths = anchor_lines.keys() | skipped_paths
    diff_lines = {path: anchor_lines.get(path, {}).keys() for path in known_paths}
    shown = ShownLines(diff_lines, ledger, is_diff_head=is_diff_head)
    checks = _check_all(proposed, rule_findings, anchor_lines, known_paths, shown)
    kept_ai = tuple(
        ReviewedFinding(
            check.proposed.model_copy(
                update={"file": check.diff_path, "evidence": check.evidence.kept}
            ),
            Source.AI,
            check.proposed.rule,
            check.fingerprint,
            evidence_dropped=check.evidence.dropped,
        )
        for check in checks
        if check.fingerprint is not None
    )
    return VerifyResult(kept=(*rule_findings, *kept_ai), checks=checks)


def _anchor_lines(parsed: ParsedDiff) -> dict[str, dict[int, _AnchorLine]]:
    """Each reviewable new path, with its added and context lines by number.

    A path with no such line, such as one whose hunks only remove lines, maps to an empty dict.
    When hunks sharing a path number a line twice, the first one's line is kept.
    """
    anchors: dict[str, dict[int, _AnchorLine]] = {}
    for file in parsed.files:
        if file.path is None or file.skip_reason is not None:
            continue
        numbered = anchors.setdefault(file.path, {})
        for hunk in file.hunks:
            for line in hunk.lines:
                if line.new_line is not None:
                    numbered.setdefault(line.new_line, _AnchorLine(line.text, hunk))
    return anchors


def _drop_reason(
    finding: Finding, diff_path: str | None, anchor_lines: _AnchorLines
) -> DropReason | None:
    if diff_path is None:
        return DropReason.UNKNOWN_FILE
    if diff_path not in anchor_lines:
        return DropReason.SKIPPED_FILE
    if finding.line not in anchor_lines[diff_path]:
        return DropReason.LINE_NOT_IN_DIFF
    return None


def diff_path_named(file: str, known_paths: Collection[str]) -> str | None:
    """The path in ``known_paths`` that ``file`` names, or None.

    ``file`` matches exactly, or once more without one leading ``./``, ``a/`` or ``b/``.
    """
    if file in known_paths:
        return file
    for prefix in _PATH_PREFIXES:
        if file.startswith(prefix):
            stripped = file.removeprefix(prefix)
            return stripped if stripped in known_paths else None
    return None


def _check_all(
    proposed: Sequence[Finding],
    rule_findings: Sequence[ReviewedFinding],
    anchor_lines: _AnchorLines,
    known_paths: Collection[str],
    shown: ShownLines,
) -> tuple[ProposalCheck, ...]:
    """Drop each finding whose anchor misses; mark a repeat a duplicate; fingerprint the rest.

    A finding is compared first with the rule findings (``_repeated_rule_finding``), then with the
    model findings kept before it: two are alike when their path, rule part, line and rationale
    are all the same, whatever their ordinals. Ordinals count on from the rule findings', so a
    model finding never takes a rule finding's fingerprint. Every finding's evidence is checked
    against ``shown``.
    """
    fingerprinter = Fingerprinter(taken=(reviewed.fingerprint for reviewed in rule_findings))
    rule_findings_by_line = _rule_findings_by_line(rule_findings)
    kept_fingerprints: dict[tuple[str, str, int, str], str] = {}
    checks: list[ProposalCheck] = []
    for finding in proposed:
        diff_path = diff_path_named(finding.file, known_paths)
        reason = _drop_reason(finding, diff_path, anchor_lines)
        evidence = verify_evidence(finding.evidence, shown)
        # ``diff_path is None`` always has a reason; it is tested only so mypy narrows the type.
        if reason is not None or diff_path is None:
            checks.append(ProposalCheck(finding, diff_path, drop_reason=reason, evidence=evidence))
            continue
        lines = anchor_lines[diff_path]
        rule_part = finding.rule or f"ai:{finding.category}"
        alike = (diff_path, rule_part, finding.line, finding.rationale)
        duplicate_of = _repeated_rule_finding(finding, diff_path, lines, rule_findings_by_line)
        if duplicate_of is None:
            duplicate_of = kept_fingerprints.get(alike)
        if duplicate_of is not None:
            checks.append(
                ProposalCheck(finding, diff_path, duplicate_of=duplicate_of, evidence=evidence)
            )
            continue
        fingerprint = fingerprinter.fingerprint(
            file=diff_path, rule=rule_part, anchor_text=lines[finding.line].text
        )
        kept_fingerprints[alike] = fingerprint
        checks.append(ProposalCheck(finding, diff_path, fingerprint=fingerprint, evidence=evidence))
    return tuple(checks)


def _rule_findings_by_line(rule_findings: Sequence[ReviewedFinding]) -> _RuleFindingsByLine:
    """The rule findings by file and rule, then by line, each line's in the order given."""
    by_line: dict[tuple[str, RuleId | None], dict[int, list[ReviewedFinding]]] = {}
    for reviewed in rule_findings:
        key = (reviewed.finding.file, reviewed.rule)
        by_line.setdefault(key, {}).setdefault(reviewed.finding.line, []).append(reviewed)
    return by_line


def _repeated_rule_finding(
    finding: Finding,
    diff_path: str,
    lines: Mapping[int, _AnchorLine],
    rule_findings_by_line: _RuleFindingsByLine,
) -> str | None:
    """The fingerprint of a rule finding that ``finding`` repeats, or None.

    ``finding`` sits in ``diff_path``, whose anchor lines are ``lines``. It repeats a rule finding
    of the rule it is tagged with, in the same file and hunk, at most ``_NEARBY_LINES`` lines
    away, and at least as severe as it, so a tag can't hide a more serious finding. Each line's
    hunk is the one ``lines`` holds for it. Of several, the one on the lowest line is returned,
    and on that line the first given.
    """
    if finding.rule is None:
        return None
    same_rule = rule_findings_by_line.get((diff_path, finding.rule), {})
    hunk = lines[finding.line].hunk
    for line in range(finding.line - _NEARBY_LINES, finding.line + _NEARBY_LINES + 1):
        anchor = lines.get(line)
        if anchor is None or anchor.hunk is not hunk:
            continue
        for reviewed in same_rule.get(line, ()):
            if finding.severity.rank <= reviewed.finding.severity.rank:
                return reviewed.fingerprint
    return None
