"""Ask a person to accept or reject each reported finding, and label each call for Phase 2.

The streams are injected, so this module only decides what to ask and what the answers mean.
"""

import contextlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from review_bot.findings import ReviewedFinding, Severity, Source
from review_bot.render import escape_unprintable
from review_bot.report import TriageDecision, TriagedFinding
from review_bot.secret_scan import redact

__all__ = ["Label", "LabelValue", "TriageResult", "run_triage"]

# A decision a person made, so never ``undecided``.
type LabelValue = Literal[TriageDecision.ACCEPT, TriageDecision.REJECT]

_PROMPT: Final = "[a]ccept [r]eject: "
_ANSWERS: Final[Mapping[str, LabelValue]] = {
    "a": TriageDecision.ACCEPT,
    "r": TriageDecision.REJECT,
}
_INVALID_ANSWER: Final = "Answer a to accept the finding or r to reject it."


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
    """Each reported finding with its decision, in order, and a label for each one decided."""

    findings: tuple[TriagedFinding, ...]
    labels: tuple[Label, ...]


def run_triage(
    findings: Sequence[ReviewedFinding],
    *,
    read_line: Callable[[str], str],
    write_line: Callable[[str], None],
) -> TriageResult:
    """Show each finding once, then ask until the answer is ``a`` or ``r``.

    End of input or Ctrl+C stops triage: nothing more is asked, and the findings not yet decided
    stay ``undecided``, with no label.

    Args:
        findings: The reported findings, in report order.
        read_line: Shows a prompt and returns the line answered; raises ``EOFError`` at the end
            of input.
        write_line: Writes one line where the person reads the prompts.
    """
    answers: list[LabelValue] = []
    for position, reviewed in enumerate(findings, start=1):
        try:
            write_line(_describe(reviewed, f"{position} of {len(findings)}"))
            answers.append(_ask(read_line, write_line))
        except (EOFError, KeyboardInterrupt):
            # A second Ctrl+C only loses this notice, never the decisions already made.
            with contextlib.suppress(KeyboardInterrupt):
                write_line(f"Triage stopped: {len(findings) - len(answers)} left undecided.")
            break
    decided = findings[: len(answers)]
    undecided = findings[len(answers) :]
    return TriageResult(
        findings=(
            *(TriagedFinding(item, answer) for item, answer in zip(decided, answers, strict=True)),
            *(TriagedFinding(item, TriageDecision.UNDECIDED) for item in undecided),
        ),
        labels=tuple(_label(item, answer) for item, answer in zip(decided, answers, strict=True)),
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


def _ask(read_line: Callable[[str], str], write_line: Callable[[str], None]) -> LabelValue:
    while (answer := read_line(_PROMPT).strip().lower()) not in _ANSWERS:
        write_line(_INVALID_ANSWER)
    return _ANSWERS[answer]


def _label(reviewed: ReviewedFinding, answer: LabelValue) -> Label:
    return Label(
        fingerprint=reviewed.fingerprint,
        label=answer,
        severity_before=reviewed.finding.severity,
        severity_after=reviewed.finding.severity,
        rationale_changed=False,
        source=reviewed.source,
    )
