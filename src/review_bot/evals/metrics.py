"""What a live run's report adds to a case's scores: spread, drift, tags, failures and tokens.

Pure: it reads a case's score, the evidence it was scored on, and its recording.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Final

from review_bot.evals.formats import Recording, RunErrorCategory
from review_bot.evals.score import CaseScore

__all__ = ["LiveMetrics", "live_metrics"]

# A fingerprint recurs if it was kept in at least this many runs (P1-S16 AC9).
_RECURRING_RUNS: Final = 2


@dataclass(frozen=True, slots=True)
class LiveMetrics:
    """A case's run-to-run figures (Phase 1 design, Known limits).

    ``findings_per_run`` counts each run's findings as returned, or is None for a failed run.
    ``returned``, ``dropped`` and ``rule_tagged`` count the model's findings over all runs: as
    returned, failing the anchor check, and carrying a ``rule`` tag. ``deduped`` counts those
    removed as duplicates. ``lines_by_bug`` holds each bug's id and the distinct lines its credits
    used, as drift. ``recurring_fingerprints`` of the distinct ``fingerprints`` kept were kept in
    at least 2 runs. ``failures`` counts failed runs by category.
    """

    findings_per_run: tuple[int | None, ...]
    returned: int
    dropped: int
    rule_tagged: int
    deduped: int
    lines_by_bug: tuple[tuple[str, tuple[int, ...]], ...]
    fingerprints: int
    recurring_fingerprints: int
    failures: dict[RunErrorCategory, int]
    input_tokens: int
    output_tokens: int


def live_metrics(score: CaseScore, recording: Recording) -> LiveMetrics:
    """The run-to-run figures for a case scored on ``recording``."""
    runs = score.evidence.runs
    runs_by_fingerprint = Counter(
        fingerprint for run in runs for fingerprint in set(run.fingerprints)
    )
    return LiveMetrics(
        findings_per_run=tuple(
            None if run.error is not None else len(run.returned) for run in runs
        ),
        returned=sum(len(run.returned) for run in runs),
        dropped=sum(len(run.dropped) for run in runs),
        rule_tagged=sum(finding.rule is not None for run in runs for finding in run.returned),
        deduped=sum(len(run.anchor_valid) - len(run.final) for run in runs),
        lines_by_bug=tuple(
            (bug.bug.id, tuple(sorted({credit.finding.line for credit in bug.credits})))
            for bug in score.bugs
        ),
        fingerprints=len(runs_by_fingerprint),
        recurring_fingerprints=sum(
            count >= _RECURRING_RUNS for count in runs_by_fingerprint.values()
        ),
        failures=dict(Counter(run.error for run in runs if run.error is not None)),
        input_tokens=sum(run.usage.input_tokens for run in recording.runs),
        output_tokens=sum(run.usage.output_tokens for run in recording.runs),
    )
