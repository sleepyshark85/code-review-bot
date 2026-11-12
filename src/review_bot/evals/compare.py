"""Diff mode against context mode on the same eval cases (design, Q3 and How the gate is evidenced).

Pure: ``evals.replay`` scores each case's live recording of each mode through today's pipeline,
and this compares the two, case by case and in total.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Literal

from review_bot.errors import ProviderCap
from review_bot.evals.formats import EvalMode, Recording, RunErrorCategory
from review_bot.evals.match import matches_bug
from review_bot.evals.score import BugScore, CaseReplays, CaseResult, CaseScore
from review_bot.findings import Source
from review_bot.price_flags import Prices, token_cost_usd

__all__ = [
    "BugRuns",
    "CapHit",
    "CaseComparison",
    "Comparison",
    "Cost",
    "CostBasis",
    "ModeCase",
    "ModeTotals",
    "PrecisionChange",
    "Rate",
    "Verdict",
    "compare_cases",
]

# Where a mode's cost comes from: every run's reported cost, or its tokens at the user's prices.
type CostBasis = Literal["recorded", "estimated"]
type PrecisionChange = Literal["higher", "same", "lower"]


@dataclass(frozen=True, slots=True)
class Rate:
    """``part`` over ``whole``: a share, or a mean per run, such as tokens per run."""

    part: int
    whole: int

    @property
    def value(self) -> Fraction | None:
        """``part / whole``, or None, not measured, when there is nothing to measure it on."""
        return Fraction(self.part, self.whole) if self.whole else None


@dataclass(frozen=True, slots=True)
class BugRuns:
    """An AI bug's runs that caught it in one mode, and whether that met ``min_runs_caught``."""

    bug_id: str
    caught_runs: int
    runs: int
    met: bool


@dataclass(frozen=True, slots=True)
class ModeCase:
    """A paired case in one mode: whether it passed, its run count, and each AI bug's runs."""

    passed: bool
    runs: int
    bugs: tuple[BugRuns, ...]


@dataclass(frozen=True, slots=True)
class CaseComparison:
    """A case in both modes, or, unpaired, the reason it isn't compared.

    ``modes`` is empty exactly when ``reason`` is set.
    """

    case_id: str
    reason: str | None
    modes: Mapping[EvalMode, ModeCase]

    @property
    def paired(self) -> bool:
        """Whether both modes have a fresh live recording, so the case is compared."""
        return self.reason is None


@dataclass(frozen=True, slots=True)
class Cost:
    """A mode's cost over its runs, unrounded, and where it comes from."""

    basis: CostBasis
    total_usd: Decimal
    mean_usd: Decimal


@dataclass(frozen=True, slots=True)
class CapHit:
    """A run that a cost or turn cap ended: its case, 1-based number and cap."""

    case_id: str
    run: int
    cap: ProviderCap


@dataclass(frozen=True, slots=True)
class ModeTotals:
    """One mode's figures over the paired cases, each from that mode's own runs.

    ``ai_bugs_caught``: the AI bugs that met ``min_runs_caught``, of all AI bugs.
    ``credited_runs``: the runs that caught each bug, summed over bugs, of their runs.
    ``precision``: the model's final findings, kept after dedup on cases with bugs, that credit
    a bug, of all of them. ``findings_per_run``: the final findings of cases with a noise
    budget, per run. ``injection_runs_passed``: of injection cases' runs. The token and tool
    call figures are per run. ``cost`` is None if not recorded. A failed run counts as a run with
    no findings.
    """

    runs: int
    ai_bugs_caught: Rate
    credited_runs: Rate
    precision: Rate
    findings_per_run: Rate
    injection_runs_passed: Rate
    cost: Cost | None
    input_tokens_per_run: Rate
    output_tokens_per_run: Rate
    tool_calls_per_run: Rate
    most_tool_calls: int
    failed_runs: Mapping[RunErrorCategory, int]
    cap_hits: tuple[CapHit, ...]


@dataclass(frozen=True, slots=True)
class Verdict:
    """What Q3 asks of the eval cases.

    ``precision`` is context's against diff's, or None if either has no final finding.
    ``smaller_share`` holds ``(case id, bug id)`` of each AI bug caught in a smaller share of
    runs in context mode. ``context_injection_passed`` is None without injection runs.
    """

    precision: PrecisionChange | None
    smaller_share: tuple[tuple[str, str], ...]
    context_injection_passed: bool | None


@dataclass(frozen=True, slots=True)
class Comparison:
    """Every case with a ``repo`` or a context recording, each mode's totals, and the verdict."""

    cases: tuple[CaseComparison, ...]
    totals: Mapping[EvalMode, ModeTotals]
    verdict: Verdict

    @property
    def passed(self) -> bool:
        """Whether every listed case, each with a ``repo`` or a context recording, is paired."""
        return all(case.paired for case in self.cases)


@dataclass(frozen=True, slots=True)
class _Scored:
    """A paired case's result in one mode, known to be scored on a recording."""

    case_id: str
    passed: bool
    score: CaseScore
    recording: Recording


@dataclass(frozen=True, slots=True)
class _Pair:
    """One paired case, scored in each mode."""

    diff: _Scored
    context: _Scored


def compare_cases(replays: Sequence[CaseReplays], prices: Prices | None) -> Comparison:
    """Compare each case's two modes, and total each mode over the paired cases.

    A case without a ``repo`` or a context recording is left out. ``prices`` estimate the cost
    of a mode in which some run has no reported cost.
    """
    cases: list[CaseComparison] = []
    pairs: list[_Pair] = []
    for replay in replays:
        case = replay.case
        if case.repo is None and replay.context is None:
            continue
        diff, context = _scored(replay.diff), _scored(replay.context)
        if isinstance(diff, str) or isinstance(context, str):
            reasons = [
                f"{mode}: {reason}"
                for mode, reason in (("diff", diff), ("context", context))
                if isinstance(reason, str)
            ]
            cases.append(CaseComparison(case.id, "; ".join(reasons), {}))
            continue
        pairs.append(_Pair(diff, context))
        modes: dict[EvalMode, ModeCase] = {"diff": _mode_case(diff), "context": _mode_case(context)}
        cases.append(CaseComparison(case.id, None, modes))
    totals: dict[EvalMode, ModeTotals] = {
        "diff": _totals([pair.diff for pair in pairs], prices),
        "context": _totals([pair.context for pair in pairs], prices),
    }
    return Comparison(tuple(cases), totals, _verdict(pairs, totals))


def _scored(result: CaseResult | None) -> _Scored | str:
    """``result`` if it was scored on a recording, else why the case can't be paired."""
    if result is None or result.recording is None:
        return "no live recording"
    if result.score is None:
        return " ".join(result.all_problems)
    return _Scored(result.case_id, result.passed, result.score, result.recording)


def _mode_case(scored: _Scored) -> ModeCase:
    bugs = tuple(
        BugRuns(bug.bug.id, bug.caught, bug.out_of, bug.passed) for bug in _ai_bugs(scored.score)
    )
    return ModeCase(scored.passed, len(scored.recording.runs), bugs)


def _totals(cases: Sequence[_Scored], prices: Prices | None) -> ModeTotals:
    ai_bugs = [bug for case in cases for bug in _ai_bugs(case.score)]
    noise_cases = [case.score for case in cases if case.score.case.noise_budget is not None]
    injection_cases = [case.score for case in cases if case.score.case.injection is not None]
    recorded = [run for case in cases for run in case.recording.runs]
    tool_calls = [len(run.tool_calls) for run in recorded]
    return ModeTotals(
        runs=len(recorded),
        ai_bugs_caught=Rate(sum(bug.passed for bug in ai_bugs), len(ai_bugs)),
        credited_runs=Rate(sum(bug.caught for bug in ai_bugs), sum(bug.out_of for bug in ai_bugs)),
        precision=_precision(cases),
        findings_per_run=Rate(
            sum(len(run.final) for score in noise_cases for run in score.evidence.runs),
            sum(len(score.runs) for score in noise_cases),
        ),
        injection_runs_passed=Rate(
            sum(score.passing_runs for score in injection_cases),
            sum(len(score.runs) for score in injection_cases),
        ),
        cost=_cost(cases, prices),
        input_tokens_per_run=Rate(sum(run.usage.input_tokens for run in recorded), len(recorded)),
        output_tokens_per_run=Rate(sum(run.usage.output_tokens for run in recorded), len(recorded)),
        tool_calls_per_run=Rate(sum(tool_calls), len(recorded)),
        most_tool_calls=max(tool_calls, default=0),
        failed_runs=dict(
            Counter(run.error_category for run in recorded if run.error_category is not None)
        ),
        cap_hits=tuple(
            CapHit(case.case_id, number, run.cap)
            for case in cases
            for number, run in enumerate(case.recording.runs, start=1)
            if run.cap is not None
        ),
    )


def _precision(cases: Sequence[_Scored]) -> Rate:
    """The final AI findings that credit an AI bug, of all of them, over cases with bugs."""
    credited = final = 0
    for case in cases:
        if not case.score.case.bugs:
            continue
        bugs = [score.bug for score in _ai_bugs(case.score)]
        for run in case.score.evidence.runs:
            final += len(run.final)
            credited += sum(any(matches_bug(bug, finding) for bug in bugs) for finding in run.final)
    return Rate(credited, final)


def _cost(cases: Sequence[_Scored], prices: Prices | None) -> Cost | None:
    """Reported costs if every run has one, else the tokens at ``prices``, else None."""
    runs = [run for case in cases for run in case.recording.runs]
    if not runs:
        return None
    reported = [run.cost_usd for run in runs if run.cost_usd is not None]
    if len(reported) == len(runs):
        basis: CostBasis = "recorded"
        total = sum(reported, Decimal(0))
    elif prices is not None:
        basis = "estimated"
        estimates = (
            token_cost_usd(run.usage.input_tokens, run.usage.output_tokens, prices) for run in runs
        )
        total = sum(estimates, Decimal(0))
    else:
        return None
    return Cost(basis, total, total / len(runs))


def _verdict(pairs: Sequence[_Pair], totals: Mapping[EvalMode, ModeTotals]) -> Verdict:
    """Q3 on the eval cases: precision, each AI bug's share of runs, and injection runs."""
    smaller_share = tuple(
        (pair.diff.case_id, diff_bug.bug.id)
        for pair in pairs
        for diff_bug, context_bug in zip(
            _ai_bugs(pair.diff.score), _ai_bugs(pair.context.score), strict=True
        )
        if _run_share(context_bug) < _run_share(diff_bug)
    )
    return Verdict(
        _precision_change(totals["diff"].precision.value, totals["context"].precision.value),
        smaller_share,
        _all_passed(totals["context"].injection_runs_passed),
    )


def _all_passed(rate: Rate) -> bool | None:
    """Whether every run passed, or None, not measured, without runs."""
    value = rate.value
    return None if value is None else value == 1


def _precision_change(diff: Fraction | None, context: Fraction | None) -> PrecisionChange | None:
    if diff is None or context is None:
        return None
    if context > diff:
        return "higher"
    return "same" if context == diff else "lower"


def _ai_bugs(score: CaseScore) -> list[BugScore]:
    return [bug for bug in score.bugs if bug.bug.source is Source.AI]


def _run_share(bug: BugScore) -> Fraction:
    """The share of its runs that caught the bug; a recording has at least one run."""
    return Fraction(bug.caught, bug.out_of)
