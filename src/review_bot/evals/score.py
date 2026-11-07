"""Score one case: each planted bug per run, each source's recall, and why the case fails.

Pure: ``evals.replay`` runs the pipeline and hands over what it found.
"""

from dataclasses import dataclass

from review_bot.errors import ErrorCategory
from review_bot.evals.formats import Bug, EvalCase, Recording
from review_bot.evals.match import matches_bug
from review_bot.findings import Finding, Source

__all__ = [
    "BugScore",
    "CaseEvidence",
    "CaseResult",
    "CaseScore",
    "Credit",
    "RunFindings",
    "score_case",
]


@dataclass(frozen=True, slots=True)
class RunFindings:
    """One recorded run through today's pipeline, or the error it was recorded with.

    ``anchor_valid`` holds the model's findings whose anchor is in the diff, before dedup, each
    with ``file`` set to the diff path it names. ``final`` holds the model's findings kept after
    dedup. Both are empty for a failed run.
    """

    anchor_valid: tuple[Finding, ...]
    final: tuple[Finding, ...]
    error: ErrorCategory | None = None


@dataclass(frozen=True, slots=True)
class CaseEvidence:
    """The rules' findings on a case's diff, and each recorded run, or no runs if unrecorded."""

    rule_findings: tuple[Finding, ...]
    runs: tuple[RunFindings, ...]


@dataclass(frozen=True, slots=True)
class Credit:
    """A finding, and the 1-based run it came from, or None for a rule's finding."""

    run: int | None
    finding: Finding


@dataclass(frozen=True, slots=True)
class BugScore:
    """How often a bug was caught.

    A rule bug is scored once, as the rules are deterministic; an AI bug once per run, and isn't
    measured (``out_of`` is 0) without a recording. ``caught`` counts matches before dedup and
    ``caught_final`` after it. ``credits`` are the matching findings, before dedup.
    """

    bug: Bug
    caught: int
    caught_final: int
    out_of: int
    credits: tuple[Credit, ...]

    @property
    def is_measured(self) -> bool:
        """Whether there was anything to score it on."""
        return self.out_of > 0

    @property
    def needed(self) -> int:
        """How many times it must be caught: every time for a rule bug."""
        return self.bug.min_runs_caught or self.out_of

    @property
    def passed(self) -> bool:
        """Whether it was measured, and caught at least as often as needed."""
        return self.is_measured and self.caught >= self.needed


@dataclass(frozen=True, slots=True)
class CaseScore:
    """A case's bug scores in case order, its failed runs, unmatched findings and problems.

    ``failed_runs`` pairs each failed run's 1-based number with its error category.
    ``unmatched`` holds the findings that credit no bug of their source. The case passes when
    ``problems`` is empty.
    """

    bugs: tuple[BugScore, ...]
    failed_runs: tuple[tuple[int, ErrorCategory], ...]
    unmatched: tuple[Credit, ...]
    problems: tuple[str, ...]

    def recall(self, source: Source) -> tuple[int, int] | None:
        """The bugs of ``source`` that passed, and how many there are; None if unmeasured."""
        scores = [score for score in self.bugs if score.bug.source is source]
        if not any(score.is_measured for score in scores):
            return None
        return sum(score.passed for score in scores), len(scores)


@dataclass(frozen=True, slots=True)
class CaseResult:
    """A case's outcome: the recording it was scored on, its score, and what else fails it.

    ``score`` is None if the case wasn't scored, such as for a stale recording; ``problems``
    then says why.
    """

    case_id: str
    recording: Recording | None
    score: CaseScore | None
    problems: tuple[str, ...] = ()

    @property
    def all_problems(self) -> tuple[str, ...]:
        """Every reason the case fails: its own, then its score's."""
        return self.problems + (() if self.score is None else self.score.problems)

    @property
    def passed(self) -> bool:
        """Whether nothing fails the case."""
        return not self.all_problems


def score_case(case: EvalCase, evidence: CaseEvidence) -> CaseScore:
    """Score every bug of ``case`` on ``evidence``, and list what makes the case fail.

    Rule bugs are matched against the rules' findings only, and AI bugs against the model's, so
    each source's recall is its own. A finding may credit several bugs if it matches each.
    """
    bugs = tuple(
        _score_rule_bug(bug, evidence.rule_findings)
        if bug.source is Source.RULE
        else _score_ai_bug(bug, evidence.runs)
        for bug in case.bugs
    )
    failed_runs = tuple(
        (number, run.error)
        for number, run in enumerate(evidence.runs, start=1)
        if run.error is not None
    )
    problems = [_bug_problem(score) for score in bugs if score.is_measured and not score.passed]
    passing_runs = len(evidence.runs) - len(failed_runs)
    if evidence.runs and passing_runs < case.min_passing_runs:
        problems.append(
            f"{passing_runs} of {len(evidence.runs)} runs passed; the case needs "
            f"{case.min_passing_runs}"
        )
    return CaseScore(bugs, failed_runs, _unmatched(case, evidence), tuple(problems))


def _score_rule_bug(bug: Bug, rule_findings: tuple[Finding, ...]) -> BugScore:
    matched = tuple(Credit(None, finding) for finding in rule_findings if matches_bug(bug, finding))
    caught = int(bool(matched))
    return BugScore(bug, caught, caught, 1, matched)


def _score_ai_bug(bug: Bug, runs: tuple[RunFindings, ...]) -> BugScore:
    matched = tuple(
        Credit(number, finding)
        for number, run in enumerate(runs, start=1)
        for finding in run.anchor_valid
        if matches_bug(bug, finding)
    )
    caught_final = sum(any(matches_bug(bug, finding) for finding in run.final) for run in runs)
    caught = len({credit.run for credit in matched})
    return BugScore(bug, caught, caught_final, len(runs), matched)


def _bug_problem(score: BugScore) -> str:
    if score.bug.source is Source.RULE:
        return f"rule bug {score.bug.id} wasn't caught"
    return (
        f"ai bug {score.bug.id} was caught in {score.caught} of {score.out_of} runs; "
        f"it needs {score.needed}"
    )


def _unmatched(case: EvalCase, evidence: CaseEvidence) -> tuple[Credit, ...]:
    rule_bugs = [bug for bug in case.bugs if bug.source is Source.RULE]
    ai_bugs = [bug for bug in case.bugs if bug.source is Source.AI]
    from_rules = [
        Credit(None, finding)
        for finding in evidence.rule_findings
        if not any(matches_bug(bug, finding) for bug in rule_bugs)
    ]
    from_model = [
        Credit(number, finding)
        for number, run in enumerate(evidence.runs, start=1)
        for finding in run.anchor_valid
        if not any(matches_bug(bug, finding) for bug in ai_bugs)
    ]
    return (*from_rules, *from_model)
