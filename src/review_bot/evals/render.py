"""The replay report: one row per bug, each source's recall, and what a person should read."""

from collections.abc import Sequence
from typing import Final

from review_bot.evals.formats import Recording
from review_bot.evals.score import BugScore, CaseResult, CaseScore, Credit
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
    lines += [f"  FAIL: {problem}" for problem in result.all_problems]
    return lines


def _recording_summary(recording: Recording | None) -> str:
    if recording is None:
        return "no recording, so AI bugs are not measured"
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
    if score.failed_runs:
        failed = ", ".join(f"run {number} ({category})" for number, category in score.failed_runs)
        lines.append(f"  failed runs: {failed}")
    if score.unmatched:
        lines.append("  unmatched findings:")
        lines += [f"    {_credit_line(credit)}" for credit in score.unmatched]
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
