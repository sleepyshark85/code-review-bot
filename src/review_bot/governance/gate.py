"""Which findings block a merge, under the finding classes a policy approves (ADR 0019).

Pure. A rule class counts only the rules' own findings, and ``possible-secret`` the possible
secrets the pull request adds; both are found afresh over the whole base...head diff every run
(``check_whole_diff``). An ``ai:`` class counts this run's model findings, plus the blockers its
caller carries from earlier runs (``pr.carried``). Nothing here enforces a block: a separate
check reads the decision from the bot's summary (Phase 4 design, Merge gating).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

from review_bot.diff_parser import LineKind, ParsedDiff
from review_bot.findings import Category, ReviewedFinding, RuleId, Severity, Source
from review_bot.fingerprint import Fingerprinter
from review_bot.rules import DomainRule, run_rules
from review_bot.secret_scan import path_holds_secret, scan_secrets
from review_bot.threshold import decide

__all__ = [
    "GATE_CLASS_NAMES",
    "NOTHING_FOUND",
    "NOT_ENABLED",
    "NO_CONFIG",
    "POSSIBLE_SECRET",
    "Blocker",
    "GateClass",
    "GateDecision",
    "GateReason",
    "GateResult",
    "WholeDiffChecks",
    "check_whole_diff",
    "class_name_problem",
    "decide_gate",
    "is_ai_class",
]

POSSIBLE_SECRET: Final = "possible-secret"  # noqa: S105 - a class's name, not a secret
_AI_PREFIX: Final = "ai:"
# The rules that never report above these severities, below ``high``, so they can never block.
_NEVER_BLOCKING: Final[Mapping[str, Severity]] = {
    RuleId.SWALLOWED_EXCEPTION: Severity.MEDIUM,
    RuleId.OPEN_WITHOUT_ENCODING: Severity.LOW,
}
GATE_CLASS_NAMES: Final = frozenset(
    (
        *(rule for rule in RuleId if rule not in _NEVER_BLOCKING),
        *(f"{_AI_PREFIX}{category}" for category in Category),
        POSSIBLE_SECRET,
    )
)
# A possible secret blocks as a finding this severe and this sure would.
_SECRET_SEVERITY: Final = Severity.CRITICAL
_SECRET_CONFIDENCE: Final = 1.0


class GateDecision(StrEnum):
    """Whether the run's findings block the merge."""

    NOT_ENABLED = "not_enabled"
    PASS = "pass"  # noqa: S105 - a decision's name, not a password
    BLOCK = "block"
    # The model's part wasn't judged (the run failed or skipped), and no blocker was found.
    UNJUDGED = "unjudged"


class GateReason(StrEnum):
    """Why the decision isn't the findings' own."""

    # The bot's summary or inline comments were edited by someone else, or the summary was lost.
    TAMPERED = "tampered"
    # The run read no valid repository config, so it can't know which classes gate.
    NO_CONFIG = "no_config"


@dataclass(frozen=True, slots=True)
class GateClass:
    """A finding class a policy approves for gating, and the least a finding needs to block.

    ``rule_part`` is a rule ID, ``ai:<category>`` or ``possible-secret``.
    """

    rule_part: str
    min_severity: Severity
    min_confidence: float
    approved_by: str
    approved_on: date

    def admits(self, severity: Severity, confidence: float) -> bool:
        """Whether a finding this severe and this sure meets the class's minimums."""
        return severity.rank >= self.min_severity.rank and confidence >= self.min_confidence


@dataclass(frozen=True, slots=True)
class Blocker:
    """A finding that blocks the merge, and its class.

    ``file`` and ``line`` are None where it has none: a new path holding a possible secret, or
    a finding in such a path, has neither; a secret on a hunk whose header has no readable start
    has no line; and a carried blocker whose comment is gone has neither. ``severity`` is None
    only for such a carried blocker. ``carried`` says it came from an earlier run, not from this
    one's checks.
    """

    fingerprint: str
    gate_class: str
    file: str | None
    line: int | None
    severity: Severity | None
    carried: bool = False

    @property
    def is_ai(self) -> bool:
        """Whether it is a model finding's, whose class carries between runs."""
        return is_ai_class(self.gate_class)


@dataclass(frozen=True, slots=True)
class WholeDiffChecks:
    """What the rules and the secret scan find over a pull request's whole base...head diff.

    ``rule_findings`` are the rules' findings at or above the run's threshold, and ``secrets``
    the possible secrets the diff adds, each a ``possible-secret`` blocker.
    """

    rule_findings: tuple[ReviewedFinding, ...]
    secrets: tuple[Blocker, ...]


NOTHING_FOUND: Final = WholeDiffChecks(rule_findings=(), secrets=())


@dataclass(frozen=True, slots=True)
class GateResult:
    """A run's gate decision, as ``shadow/v1`` records it in ``gate``.

    ``enabled`` is None when the run read no valid config, so it can't tell. ``classes`` are the
    classes the repository gates on. ``blocking`` holds every blocker, ``ai:`` ones first, then
    the most severe first. ``rebuilt`` says the run found a tamper, and rebuilt the carried
    blockers from the bot's own inline comments.
    """

    enabled: bool | None
    decision: GateDecision
    reason: GateReason | None
    classes: tuple[str, ...]
    blocking: tuple[Blocker, ...] = ()
    rebuilt: bool = False


NOT_ENABLED: Final = GateResult(
    enabled=False, decision=GateDecision.NOT_ENABLED, reason=None, classes=()
)
NO_CONFIG: Final = GateResult(
    enabled=None, decision=GateDecision.UNJUDGED, reason=GateReason.NO_CONFIG, classes=()
)


def class_name_problem(name: str) -> str | None:
    """Why ``name`` can't be a gate class, or None if it can.

    The reason doesn't quote ``name``, so the caller can show it in its own way.
    """
    if name in GATE_CLASS_NAMES:
        return None
    severity = _NEVER_BLOCKING.get(name)
    if severity is not None:
        return f"its rule reports only {severity}, below high, so it can never block"
    return (
        "it isn't a finding class; a class is a domain rule such as decimal-money, "
        "ai:<category> such as ai:security, or possible-secret"
    )


def check_whole_diff(
    parsed: ParsedDiff, *, domain_rules: Sequence[DomainRule], threshold: Severity
) -> WholeDiffChecks:
    """Run the rules and the secret scan over ``parsed``, a pull request's whole diff.

    The rules run as a review runs them, with ``domain_rules``, and only their findings at or
    above ``threshold`` count. Every file is scanned for the secrets it adds, skipped ones
    too, since a secret in a skipped file is still added. Linear in the diff's size.
    """
    rule_findings = run_rules(parsed, domain_rules=domain_rules).findings
    reported = decide(rule_findings, threshold).reported
    return WholeDiffChecks(rule_findings=reported, secrets=_added_secrets(parsed))


def _added_secrets(parsed: ParsedDiff) -> tuple[Blocker, ...]:
    """A blocker for each added line holding a possible secret, and each new path holding one.

    A new path holding one is a single blocker with no file or line, whose fingerprint hashes
    the path, and its lines add none. A line's blocker has its new path and line, and its
    fingerprint hashes the line, as a finding's does. Neither holds the value. A removed or
    context line, an old path and a deleted file add nothing.
    """
    fingerprinter = Fingerprinter()
    blockers: list[Blocker] = []
    for file in parsed.files:
        path = file.path
        if path is None:
            continue
        if path_holds_secret(path):
            fingerprint = fingerprinter.fingerprint(
                file=path, rule=POSSIBLE_SECRET, anchor_text=path
            )
            blockers.append(_secret(fingerprint, None, None))
            continue
        for hunk in file.hunks:
            for line in hunk.lines:
                if line.kind is LineKind.ADDED and scan_secrets(line.text):
                    fingerprint = fingerprinter.fingerprint(
                        file=path, rule=POSSIBLE_SECRET, anchor_text=line.text
                    )
                    blockers.append(_secret(fingerprint, path, line.new_line))
    return tuple(blockers)


def _secret(fingerprint: str, file: str | None, line: int | None) -> Blocker:
    return Blocker(fingerprint, POSSIBLE_SECRET, file, line, _SECRET_SEVERITY)


def decide_gate(  # noqa: PLR0913 - keyword-only, each a separate input of the decision
    reported: Iterable[ReviewedFinding],
    *,
    whole_diff: WholeDiffChecks | None,
    carried: Iterable[Blocker],
    enabled: Sequence[GateClass],
    judged: bool,
    tampered: bool = False,
    rebuilt: bool = False,
) -> GateResult:
    """Decide which findings block the merge under the ``enabled`` classes.

    Args:
        reported: This run's reported findings, each with the fingerprint its comment has. Only
            the model's own, whose rule part is ``ai:<category>``, count, under that class.
        whole_diff: The whole diff's rule findings, which count under their rule's class, and
            possible secrets, under ``possible-secret``; None if the checks couldn't run.
        carried: Blockers carried from earlier runs; those of a class not enabled are dropped.
        enabled: The classes the repository gates on. A finding blocks only if it meets its
            class's minimum severity and confidence; a possible secret is ``critical`` with
            confidence 1.0.
        judged: Whether the model's part was judged: the run reviewed the pull request, or
            had already reviewed its head.
        tampered: Whether the run found a tamper it couldn't rebuild from.
        rebuilt: Whether the run rebuilt its carried blockers after a tamper.

    Returns:
        ``block`` if ``tampered`` or any finding blocks, else ``pass`` if ``judged``, else
        ``unjudged``; ``not_enabled`` with no class enabled. A blocker found twice, by its
        fingerprint, is kept once, this run's over a carried one. A finding in a path holding a
        possible secret blocks with no file or line, so no report shows the path.
    """
    if not enabled:
        return NOT_ENABLED
    by_name = {gate_class.rule_part: gate_class for gate_class in enabled}
    model_findings = [
        reviewed
        for reviewed in reported
        if reviewed.source is Source.AI and is_ai_class(reviewed.rule_part)
    ]
    rule_findings = [] if whole_diff is None else whole_diff.rule_findings
    found = [
        *_finding_blockers((*model_findings, *rule_findings), by_name),
        *_secret_blockers(() if whole_diff is None else whole_diff.secrets, by_name),
        *(blocker for blocker in carried if blocker.gate_class in by_name),
    ]
    unique: dict[str, Blocker] = {}
    for blocker in found:
        unique.setdefault(blocker.fingerprint, blocker)
    blocking = tuple(sorted(unique.values(), key=_marker_order))
    if tampered:
        decision, reason = GateDecision.BLOCK, GateReason.TAMPERED
    elif blocking:
        decision, reason = GateDecision.BLOCK, None
    else:
        decision, reason = (GateDecision.PASS if judged else GateDecision.UNJUDGED), None
    classes = tuple(by_name)
    return GateResult(True, decision, reason, classes, blocking, rebuilt)


def is_ai_class(name: str) -> bool:
    """Whether ``name`` is a model finding's class, ``ai:<category>``, which carries."""
    return name.startswith(_AI_PREFIX)


def _finding_blockers(
    findings: Iterable[ReviewedFinding], by_name: Mapping[str, GateClass]
) -> list[Blocker]:
    """The findings that meet their class's minimums, each as a blocker.

    A finding in a file whose path holds a possible secret is placed nowhere, so its blocker
    never shows the path.
    """
    blockers: list[Blocker] = []
    for reviewed in findings:
        finding = reviewed.finding
        gate_class = by_name.get(reviewed.rule_part)
        if gate_class is None or not gate_class.admits(finding.severity, finding.confidence):
            continue
        file, line = (
            (None, None) if path_holds_secret(finding.file) else (finding.file, finding.line)
        )
        blockers.append(
            Blocker(reviewed.fingerprint, reviewed.rule_part, file, line, finding.severity)
        )
    return blockers


def _secret_blockers(secrets: Sequence[Blocker], by_name: Mapping[str, GateClass]) -> list[Blocker]:
    gate_class = by_name.get(POSSIBLE_SECRET)
    if gate_class is None or not gate_class.admits(_SECRET_SEVERITY, _SECRET_CONFIDENCE):
        return []
    return list(secrets)


def _marker_order(blocker: Blocker) -> tuple[bool, int, str, int, str]:
    """``ai:`` blockers first, then the most severe first (unknown last), then by place."""
    rank = -1 if blocker.severity is None else blocker.severity.rank
    return (not blocker.is_ai, -rank, blocker.file or "", blocker.line or 0, blocker.fingerprint)
