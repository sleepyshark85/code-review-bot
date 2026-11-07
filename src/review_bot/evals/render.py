"""The replay report: one row per bug, each source's recall, and what a person should read."""

from collections.abc import Sequence
from typing import Final

from review_bot.evals.formats import Recording
from review_bot.evals.score import BugScore, CaseResult, CaseScore, Credit, RunOutcome, RunScore
from review_bot.findings import Source
from review_bot.render import escape_unprintable

__all__ = ["REPLAY_DISCLAIMER", "render_replay"]

REPLAY_DISCLAIMER: Final = (
    "Replay checks the pipeline against a past model response. It is not a new measurement of "
    "the model."
)


def render_replay(results: Sequence[CaseResult]) -> str:
    """The report for every case, after the disclaimer, ending with the overall result."""
    lines = [REPLAY_DISCLAIMER]
    for result in results:
        lines += ["", *_case_lines(result)]
    overall = "pass" if all(result.passed for result in results) else "fail"
    lines += ["", f"result: {overall}"]
    return "\n".join(lines)


def _case_lines(result: CaseResult) -> list[str]:
    lines = [f"case {result.case_id}: {_recording_summary(result.recording)}"]
    if result.score is not None:
        lines += _score_lines(result.score, result.recording)
        lines += _unmeasured_lines(result.score, result.recording)
    lines += [f"  FAIL: {problem}" for problem in result.all_problems]
    return lines


def _recording_summary(recording: Recording | None) -> str:
    if recording is None:
        return "no recording, so the model's findings are not measured"
    return (
        f"{recording.kind} recording of {recording.model}, recorded "
        f"{recording.recorded_at.date().isoformat()}, {len(recording.runs)} runs"
    )


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
        f"  noise budget: at most {_count(budget.max_findings, 'model finding')} per run, none "
        f"above {budget.max_severity}",
        f"  rule noise: {_count(len(score.rule_noise), 'finding')}, needs 0",
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
    return f"{_count(len(run.noise), 'finding')}, highest {highest}"


def _count(number: int, noun: str) -> str:
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
