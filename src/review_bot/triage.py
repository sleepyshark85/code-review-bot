"""Ask a person to accept, reject or edit each reported finding, and label each call for Phase 2.

The streams are injected, so this module only decides what to ask and what the answers mean.
"""

import contextlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal

from review_bot.findings import Finding, ReviewedFinding, Severity, Source
from review_bot.render import escape_unprintable
from review_bot.report import TriageDecision, TriagedFinding
from review_bot.secret_scan import redact

__all__ = ["Label", "LabelValue", "TriageResult", "run_triage"]

# A decision a person made, so never ``undecided``.
type LabelValue = Literal[TriageDecision.ACCEPT, TriageDecision.REJECT, TriageDecision.EDIT]

_PROMPT: Final = "[a]ccept [r]eject [e]dit: "
_ANSWERS: Final[Mapping[str, LabelValue]] = {
    "a": TriageDecision.ACCEPT,
    "r": TriageDecision.REJECT,
    "e": TriageDecision.EDIT,
}
_INVALID_ANSWER: Final = "Answer a to accept the finding, r to reject it, or e to edit it."
_SEVERITY_NAMES: Final = ", ".join(Severity)
_RATIONALE_PROMPT: Final = "Rationale (Enter keeps it): "


@dataclass(frozen=True, slots=True)
class Label:
    """One decision on one finding: the feedback Phase 2 learns from. Never holds its text.

    ``source`` is the finding's, so its audit record needs nothing else from the finding.
    """

    fingerprint: str
    label: LabelValue
    severity_before: Severity
    severity_after: Severity
    rationale_changed: bool
    source: Source


@dataclass(frozen=True, slots=True)
class TriageResult:
    """Each reported finding in order, with its decision and any edit, and each decision's label."""

    findings: tuple[TriagedFinding, ...]
    labels: tuple[Label, ...]


def run_triage(
    findings: Sequence[ReviewedFinding],
    *,
    read_line: Callable[[str], str],
    write_line: Callable[[str], None],
) -> TriageResult:
    """Show each finding once, then ask until the answer is ``a``, ``r`` or ``e``.

    ``e`` then asks for a new severity and a new rationale, where an empty answer keeps the
    current value. Nothing else about the finding can be edited. A new rationale is redacted.

    End of input or Ctrl+C stops triage: nothing more is asked, and the findings not yet decided,
    including one part-way through an edit, stay ``undecided`` and unedited, with no label.

    Args:
        findings: The reported findings, in report order.
        read_line: Shows a prompt and returns the line answered; raises ``EOFError`` at the end
            of input.
        write_line: Writes one line where the person reads the prompts.
    """
    decided: list[tuple[TriagedFinding, Label]] = []
    for position, reviewed in enumerate(findings, start=1):
        try:
            write_line(_describe(reviewed, f"{position} of {len(findings)}"))
            decided.append(_decide(reviewed, read_line, write_line))
        except (EOFError, KeyboardInterrupt):
            # A second Ctrl+C only loses this notice, never the decisions already made.
            with contextlib.suppress(KeyboardInterrupt):
                write_line(f"Triage stopped: {len(findings) - len(decided)} left undecided.")
            break
    undecided = findings[len(decided) :]
    return TriageResult(
        findings=(
            *(triaged for triaged, _ in decided),
            *(TriagedFinding(item, TriageDecision.UNDECIDED) for item in undecided),
        ),
        labels=tuple(label for _, label in decided),
    )


def _describe(reviewed: ReviewedFinding, position: str) -> str:
    """The finding on one line, so text from the diff or the model can't forge more lines."""
    finding = reviewed.finding
    # Redacted like every path written to stderr (Phase 1 design, Secrets).
    path = escape_unprintable(redact(finding.file))
    return (
        f"Finding {position}: {path}:{finding.line} [{finding.severity}/{finding.category}] "
        f"{escape_unprintable(finding.rationale)} (source: {reviewed.source})"
    )


def _decide(
    reviewed: ReviewedFinding,
    read_line: Callable[[str], str],
    write_line: Callable[[str], None],
) -> tuple[TriagedFinding, Label]:
    """Ask for the person's call on one finding, and apply any edit."""
    answer = _ask(read_line, write_line)
    before = reviewed.finding
    after = _edit(before, read_line, write_line) if answer is TriageDecision.EDIT else before
    rationale_changed = after.rationale != before.rationale
    triaged = TriagedFinding(
        replace(reviewed, finding=after),
        answer,
        original_rationale=before.rationale if rationale_changed else None,
    )
    label = Label(
        fingerprint=reviewed.fingerprint,
        label=answer,
        severity_before=before.severity,
        severity_after=after.severity,
        rationale_changed=rationale_changed,
        source=reviewed.source,
    )
    return triaged, label


def _ask(read_line: Callable[[str], str], write_line: Callable[[str], None]) -> LabelValue:
    while (answer := read_line(_PROMPT).strip().lower()) not in _ANSWERS:
        write_line(_INVALID_ANSWER)
    return _ANSWERS[answer]


def _edit(
    finding: Finding, read_line: Callable[[str], str], write_line: Callable[[str], None]
) -> Finding:
    """The finding with the severity and rationale the person gives, each kept if left empty."""
    severity = _ask_severity(finding.severity, read_line, write_line)
    # Redacted, so a secret typed here never reaches the report.
    rationale = redact(read_line(_RATIONALE_PROMPT).strip()) or finding.rationale
    # ``model_copy`` doesn't validate, but both values already are: a ``Severity``, and stripped
    # text that isn't empty.
    return finding.model_copy(update={"severity": severity, "rationale": rationale})


def _ask_severity(
    current: Severity, read_line: Callable[[str], str], write_line: Callable[[str], None]
) -> Severity:
    prompt = f"Severity ({_SEVERITY_NAMES}; Enter keeps {current}): "
    while (answer := read_line(prompt).strip().lower()) not in {"", *Severity}:
        write_line(f"Answer one of {_SEVERITY_NAMES}, or press Enter to keep {current}.")
    return Severity(answer) if answer else current
