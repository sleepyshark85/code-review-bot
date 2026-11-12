"""The eval reports: one row per bug, each source's recall, and what a person should read.

``replay`` scores past responses; ``live`` adds each case's run-to-run figures and the totals,
and from its dollar budget, each case's tool calls, cost and cap-hit runs, and the spend.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from statistics import fmean
from typing import TYPE_CHECKING, Final

from review_bot.errors import ErrorCategory
from review_bot.evals.formats import (
    INTERNAL_ERROR,
    RecordedRun,
    RecordedToolCall,
    Recording,
    RunErrorCategory,
)
from review_bot.evals.metrics import LiveMetrics, live_metrics
from review_bot.evals.score import (
    BugScore,
    CaseResult,
    CaseScore,
    Credit,
    RunFindings,
    RunOutcome,
    RunScore,
)
from review_bot.findings import Source
from review_bot.render import escape_unprintable
from review_bot.spend import DollarBudget

if TYPE_CHECKING:
    from review_bot.evals.live import LiveRun

__all__ = [
    "LIVE_NOTICE",
    "REPLAY_DISCLAIMER",
    "count_noun",
    "render_live",
    "render_replay",
]

REPLAY_DISCLAIMER: Final = (
    "Replay checks the pipeline against a past model response. It is not a new measurement of "
    "the model."
)
LIVE_NOTICE: Final = (
    "Live run: each case was just recorded from the model. The results cover this model, this "
    "prompt and these cases only, and a person confirms each credit."
)
# Counted even at 0, as the live run reports their rates (Phase 1 design, Known limits).
_ALWAYS_COUNTED_FAILURES: Final = (ErrorCategory.INVALID_OUTPUT, ErrorCategory.REFUSAL)
_RUN_ERROR_CATEGORIES: Final[tuple[RunErrorCategory, ...]] = (*ErrorCategory, INTERNAL_ERROR)


def render_replay(results: Sequence[CaseResult]) -> str:
    """The report for every case, after the disclaimer, ending with the overall result."""
    lines = [REPLAY_DISCLAIMER]
    for result in results:
        lines += ["", *_case_lines(result)]
    lines += ["", _overall(results)]
    return "\n".join(lines)


def render_live(run: "LiveRun") -> str:
    """The report of each case just recorded, with its run-to-run figures, then the totals.

    Each case adds its spend figures, and the report ends with what was spent of ``--max-usd``,
    after ``stopped: budget`` if the budget stopped the run.
    """
    lines = [LIVE_NOTICE]
    measured: list[LiveMetrics] = []
    for result in run.results:
        figures: list[str] = []
        if result.score is not None and result.recording is not None:
            metrics = live_metrics(result.score, result.recording)
            measured.append(metrics)
            figures = _metrics_lines(metrics) + _spend_lines(result.recording, run.budget)
        lines += ["", *_case_lines(result, figures)]
    lines += ["", _totals_line(measured), _result_line(passed=run.passed)]
    if run.stopped_by_budget:
        lines.append("stopped: budget")
    lines.append(f"spent=${run.spent_usd} of --max-usd ${run.budget.max_usd}")
    return "\n".join(lines)


def _overall(results: Sequence[CaseResult]) -> str:
    return _result_line(passed=all(result.passed for result in results))


def _result_line(*, passed: bool) -> str:
    return f"result: {'pass' if passed else 'fail'}"


def _spend_lines(recording: Recording, budget: DollarBudget) -> list[str]:
    """A case's tool calls per run in context mode, its cost towards --max-usd, and cap-hit runs."""
    runs = recording.runs
    lines = []
    if recording.mode == "context":
        lines.append(f"  tool calls per run: {_tool_call_figures(runs)}")
    counted = sum((budget.charge(run.cost_usd) for run in runs), Decimal(0))
    reported = [run.cost_usd for run in runs if run.cost_usd is not None]
    if len(reported) == len(runs):
        lines.append(f"  cost: ${counted}, reported by every run")
    else:
        lines.append(
            f"  cost: ${counted}, of which ${sum(reported, Decimal(0))} reported by "
            f"{len(reported)} of {len(runs)} runs, and {len(runs) - len(reported)} unreported "
            f"counted at --max-cost-usd ${budget.max_cost_usd} each"
        )
    cap_hit = [f"run {number} ({run.cap})" for number, run in enumerate(runs, 1) if run.cap]
    lines.append(f"  cap-hit runs: {', '.join(cap_hit) or 'none'}")
    return lines


def _tool_call_figures(runs: Sequence[RecordedRun]) -> str:
    """The mean and largest tool call count of the runs that returned.

    Failed runs are left out, as live records none of their calls.
    """
    counts = [len(run.tool_calls) for run in runs if run.error is None]
    if not counts:
        return "not measured, no run returned"
    returned = count_noun(len(counts), "returned run")
    return f"mean {fmean(counts):.1f}, largest {max(counts)}, over {returned}"


def _case_lines(result: CaseResult, figures: Sequence[str] = ()) -> list[str]:
    lines = [f"case {result.case_id}: {_recording_summary(result.recording)}"]
    if result.score is not None:
        lines += _score_lines(result.score, result.recording)
        lines += _unmeasured_lines(result.score, result.recording)
        lines += _context_run_lines(result.score, result.recording)
    lines += figures
    lines += [f"  FAIL: {problem}" for problem in result.all_problems]
    return lines


def _metrics_lines(metrics: LiveMetrics) -> list[str]:
    per_run = ", ".join(
        "no review" if count is None else str(count) for count in metrics.findings_per_run
    )
    returned = count_noun(metrics.returned, "returned finding")
    recurrence = (
        f"{metrics.recurring_fingerprints} of {count_noun(metrics.fingerprints, 'fingerprint')} "
        "kept in at least 2 runs"
    )
    drift = [
        f"  drift: {bug_id} credited on {_lines(bug_lines)}"
        for bug_id, bug_lines in metrics.lines_by_bug
    ]
    runs = len(metrics.findings_per_run)
    return [
        f"  findings per run: {per_run}",
        f"  anchor drops: {metrics.dropped} of {returned}",
        f"  rule tags: {metrics.rule_tagged} of {returned}; deduped: {metrics.deduped}",
        f"  fingerprint recurrence: {recurrence}",
        *drift,
        f"  failed runs: {_failures(metrics.failures, runs)}",
        f"  tokens: {_tokens(metrics.input_tokens, metrics.output_tokens)}",
    ]


def _totals_line(measured: Sequence[LiveMetrics]) -> str:
    failures: Counter[RunErrorCategory] = Counter()
    for metrics in measured:
        failures.update(metrics.failures)
    calls = sum(len(metrics.findings_per_run) for metrics in measured)
    tokens = _tokens(
        sum(metrics.input_tokens for metrics in measured),
        sum(metrics.output_tokens for metrics in measured),
    )
    failed = _failures(failures, calls)
    return f"total: {count_noun(calls, 'call')}, failed {failed}; tokens: {tokens}"


def _failures(failures: Mapping[RunErrorCategory, int], runs: int) -> str:
    """How many runs failed, by category: ``invalid_output`` and ``refusal`` always."""
    others = [
        category
        for category in _RUN_ERROR_CATEGORIES
        if category not in _ALWAYS_COUNTED_FAILURES and failures.get(category)
    ]
    shown = [*_ALWAYS_COUNTED_FAILURES, *others]
    by_category = ", ".join(f"{category} {failures.get(category, 0)}" for category in shown)
    return f"{sum(failures.values())} of {runs} ({by_category})"


def _lines(numbers: Sequence[int]) -> str:
    if not numbers:
        return "no line"
    return f"line {numbers[0]}" if len(numbers) == 1 else f"lines {', '.join(map(str, numbers))}"


def _tokens(input_tokens: int, output_tokens: int) -> str:
    return f"input {input_tokens}, output {output_tokens}"


def _recording_summary(recording: Recording | None) -> str:
    if recording is None:
        return "no recording, so the model's findings are not measured"
    kind = recording.kind if recording.mode == "diff" else f"{recording.kind} context"
    return (
        f"{kind} recording of {recording.model}, recorded "
        f"{recording.recorded_at.date().isoformat()}, {len(recording.runs)} runs"
    )


def _context_run_lines(score: CaseScore, recording: Recording | None) -> list[str]:
    """For a context recording, each run's tool calls, cost, cap and evidence, run by run."""
    if recording is None or recording.mode != "context":
        return []
    lines = ["  context runs:"]
    for number, (recorded, replayed) in enumerate(
        zip(recording.runs, score.evidence.runs, strict=True), start=1
    ):
        lines.append(f"    run {number}: {_context_run_summary(recorded, replayed)}")
        lines += [f"      {_tool_call_text(call)}" for call in recorded.tool_calls]
    return lines


def _context_run_summary(recorded: RecordedRun, replayed: RunFindings) -> str:
    """A run's tool call count, its cost and cap if recorded, and its evidence spans' check."""
    parts = [count_noun(len(recorded.tool_calls), "tool call")]
    if recorded.cost_usd is not None:
        parts.append(f"cost ${recorded.cost_usd}")
    if recorded.cap is not None:
        parts.append(f"cap-hit ({recorded.cap}), a miss for every bug")
    parts += [
        f"evidence kept: {replayed.evidence_kept}",
        f"evidence dropped: {replayed.evidence_dropped}",
    ]
    return ", ".join(parts)


def _tool_call_text(call: RecordedToolCall) -> str:
    """What one tool call returned: its spans, or how many paths it listed, and its size."""
    text = f"{call.tool or 'unknown tool'} {call.outcome}"
    if call.reason is not None:
        text += f" ({call.reason})"
    if call.spans:
        spans = (_span_text(span.path, span.first_line, span.last_line) for span in call.spans)
        text += f": {', '.join(spans)}"
    if call.listed:
        text += f": {count_noun(len(call.listed), 'path')} listed"
    return f"{text}, {count_noun(call.result_bytes, 'byte')}"


def _span_text(path: str, first_line: int, last_line: int) -> str:
    """``path:12`` for one line, or ``path:10-14``, the path escaped as recorded text."""
    lines = str(first_line) if first_line == last_line else f"{first_line}-{last_line}"
    return f"{escape_unprintable(path)}:{lines}"


def _score_lines(score: CaseScore, recording: Recording | None) -> list[str]:
    lines: list[str] = []
    for bug_score in score.bugs:
        lines.append(f"  {_bug_row(bug_score, recording)}")
        lines += [f"    {_credit_line(credit)}" for credit in bug_score.credits]
    lines.append(f"  recall: rules {_recall(score, Source.RULE)}, ai {_recall(score, Source.AI)}")
    lines += _noise_lines(score)
    lines += _run_lines(score)
    if score.dropped:
        lines.append("  dropped findings:")
        lines += [f"    {_credit_line(credit)}" for credit in score.dropped]
    if score.unmatched:
        lines.append("  unmatched findings:")
        lines += [f"    {_credit_line(credit)}" for credit in score.unmatched]
    return lines


def _noise_lines(score: CaseScore) -> list[str]:
    budget = score.case.noise_budget
    if budget is None:
        return []
    return [
        f"  noise budget: at most {count_noun(budget.max_findings, 'model finding')} per run, none "
        f"above {budget.max_severity}",
        f"  rule noise: {count_noun(len(score.rule_noise), 'finding')}, needs 0",
        *(() if score.runs else ["  model noise: not measured"]),
    ]


def _run_lines(score: CaseScore) -> list[str]:
    """How many runs passed, then each run that didn't, by outcome, and what a person must read."""
    if not score.runs:
        return []
    lines = [
        f"  passing runs: {score.passing_runs} of {len(score.runs)}, "
        f"needs {score.case.min_passing_runs}"
    ]
    if no_review := _with_outcome(score, RunOutcome.NO_REVIEW):
        runs = ", ".join(f"run {run.number} ({run.error})" for run in no_review)
        lines.append(f"  no review: {runs}")
    if followed := _with_outcome(score, RunOutcome.FOLLOWED):
        lines.append("  followed:")
        lines += [
            f"    {_credit_line(Credit(run.number, finding))}"
            for run in followed
            for finding in run.followed
        ]
    if suppressed := _with_outcome(score, RunOutcome.SUPPRESSED):
        lines.append(f"  suppressed: {', '.join(f'run {run.number}' for run in suppressed)}")
    if over_budget := _with_outcome(score, RunOutcome.OVER_BUDGET):
        runs = ", ".join(f"run {run.number} ({_noise_summary(run)})" for run in over_budget)
        lines.append(f"  over budget: {runs}")
    if to_review := [
        Credit(run.number, finding) for run in score.runs for finding in run.needs_human_review
    ]:
        lines.append("  needs human review:")
        lines += [f"    {_credit_line(credit)}" for credit in to_review]
    return lines


def _with_outcome(score: CaseScore, outcome: RunOutcome) -> list[RunScore]:
    return [run for run in score.runs if run.outcome is outcome]


def _noise_summary(run: RunScore) -> str:
    """An over-budget run's noise count and highest severity; it has at least one noise finding."""
    highest = max((finding.severity for finding in run.noise), key=lambda severity: severity.rank)
    return f"{count_noun(len(run.noise), 'finding')}, highest {highest}"


def count_noun(number: int, noun: str) -> str:
    """``number`` and ``noun``, made plural by an ``s`` unless ``number`` is 1."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _unmeasured_lines(score: CaseScore, recording: Recording | None) -> list[str]:
    """What only a live recording measures, for a case scored on anything else."""
    if recording is not None and recording.kind == "live":
        return []
    lines = []
    if score.case.noise_budget is not None:
        lines.append("  noise budget compliance: not measured")
    if score.case.injection is not None:
        lines.append("  injection resistance: not measured")
    return lines


def _bug_row(score: BugScore, recording: Recording | None) -> str:
    bug = score.bug
    if recording is None and bug.source is Source.AI:
        return f"{bug.id}: ai, not measured"
    verdict = "pass" if score.passed else "fail"
    row = (
        f"{bug.id}: {bug.source}, caught {score.caught}/{score.out_of}, "
        f"final {score.caught_final}/{score.out_of}, needs {score.needed}, {verdict}"
    )
    if recording is None or bug.source is Source.RULE:
        return f"{row}, evidence=rules"
    return (
        f"{row}, evidence={recording.kind}, model={recording.model}, "
        f"recorded={recording.recorded_at.date().isoformat()}"
    )


def _recall(score: CaseScore, source: Source) -> str:
    recall = score.recall(source)
    if recall is None:
        return "not measured"
    passed, total = recall
    return f"{passed}/{total}"


def _credit_line(credit: Credit) -> str:
    finding = credit.finding
    origin = "rules" if credit.run is None else f"run {credit.run}"
    # The rationale is the model's text, so it can't be allowed to forge lines.
    return (
        f"{origin}: {escape_unprintable(finding.file)}:{finding.line} [{finding.category}] "
        f"{escape_unprintable(finding.rationale)}"
    )
