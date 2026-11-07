"""Replay a case's recorded runs through today's pipeline, with ``MockProvider`` (ADR 0007).

Nothing here calls a model. Each pipeline run's audit log goes to a temporary directory that is
deleted afterwards, so replay writes nothing that outlives it.
"""

import dataclasses
import hashlib
import json
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from review_bot.audit import AuditLog
from review_bot.diff_input import load_diff
from review_bot.diff_parser import parse_diff
from review_bot.errors import InputError
from review_bot.evals.formats import (
    EvalInputError,
    LoadedCase,
    RecordedRun,
    Recording,
    find_case_files,
    load_case,
    load_recording,
)
from review_bot.evals.score import CaseEvidence, CaseResult, RunFindings, score_case
from review_bot.findings import Finding, Source
from review_bot.prompt import PROMPT_VERSION, build_request
from review_bot.providers.mock import MockProvider
from review_bot.report import PipelineResult
from review_bot.review import run_pipeline
from review_bot.settings import Settings

__all__ = ["freshness_fields", "replay_cases", "require_live"]

_LIVE_COMMAND: Final = "python -m review_bot.evals live --cases DIR --runs N --max-calls M"


def freshness_fields(case_file: Path) -> dict[str, str]:
    """Today's values of a recording's freshness fields, for the case at ``case_file``.

    A recording is fresh only if its ``case_sha256``, ``diff_sha256``, ``prompt_version`` and
    ``prompt_sha256`` all equal these. ``prompt_sha256`` covers the system prompt and the user
    prompt built from the diff's reviewable text, so changing either prompt, the framing, or
    which files are skipped changes it.

    Raises:
        EvalInputError: If the case is invalid, or its diff can't be reviewed.
    """
    return _case_freshness(load_case(case_file))


def _case_freshness(loaded: LoadedCase) -> dict[str, str]:
    """``freshness_fields`` for a case already loaded.

    Raises:
        EvalInputError: If the case's diff can't be reviewed.
    """
    settings = _replay_settings()
    try:
        diff = load_diff(loaded.diff_path, max_bytes=settings.max_diff_bytes)
        reviewable_text = parse_diff(diff).reviewable_text
    except InputError as exc:
        raise _unreviewable(loaded, exc) from exc
    # The same request ``run_pipeline`` builds; ``model`` isn't part of either prompt.
    request = build_request(reviewable_text, model=settings.model)
    prompts = json.dumps([request.system_prompt, request.user_prompt])
    return {
        "case_sha256": loaded.case_sha256,
        "diff_sha256": _sha256(diff.encode("utf-8")),
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": _sha256(prompts.encode("utf-8")),
    }


async def replay_cases(cases_dir: Path, control: Path | None) -> list[CaseResult]:
    """Score every case under ``cases_dir`` on its live recording, or on rules alone without one.

    ``control`` is a synthetic recording (``--recording``) that replaces its case's live one. A
    stale recording fails its case without scoring it.

    Raises:
        EvalInputError: If a case or a recording read is invalid, a recording sits under the
            wrong kind's directory, or ``control`` is live or names no case.
    """
    cases = [load_case(case_file) for case_file in find_case_files(cases_dir)]
    recordings = {loaded.case.id: _live_recording(loaded) for loaded in cases}
    for loaded in cases:
        _check_synthetic_recordings(loaded)
    if control is not None:
        recording = _control_recording(control)
        if recording.case_id not in recordings:
            raise EvalInputError(
                f"--recording {control}: case_id {recording.case_id!r} names no case under "
                f"{cases_dir}. Pass a recording of one of its cases."
            )
        recordings[recording.case_id] = recording
    return [await _replay_scored(loaded, recordings[loaded.case.id]) for loaded in cases]


def require_live(result: CaseResult, default_model: str) -> CaseResult:
    """``result``, failed too unless it was scored on a live recording of ``default_model``."""
    recording = result.recording
    if recording is None or recording.kind != "live":
        problem = f"--require-live needs a live recording; record one with {_LIVE_COMMAND}"
    elif recording.model != default_model:
        problem = (
            f"--require-live needs a recording of the default model {default_model}, but this "
            f"one is of {recording.model}; record one with {_LIVE_COMMAND}"
        )
    else:
        return result
    return dataclasses.replace(result, problems=(*result.problems, problem))


async def _replay_scored(loaded: LoadedCase, recording: Recording | None) -> CaseResult:
    case_id = loaded.case.id
    if recording is not None:
        today = _case_freshness(loaded)
        stale = [name for name, value in today.items() if getattr(recording, name) != value]
        if stale:
            verb = "differs" if len(stale) == 1 else "differ"
            problem = (
                f"the recording is stale: its {', '.join(stale)} {verb} from today's, so it "
                f"wasn't scored. {_refresh_advice(recording)}"
            )
            return CaseResult(case_id, recording, None, (problem,))
    evidence = await _replay_case(loaded, recording)
    return CaseResult(case_id, recording, score_case(loaded.case, evidence))


def _refresh_advice(recording: Recording) -> str:
    live_advice = f"record a live run with {_LIVE_COMMAND}"
    if recording.kind == "live":
        return f"Record it again: {live_advice}"
    # ``live`` can't remake a hand-written control, so updating its fields comes first. The
    # stale fields are named earlier in the message, so this advice doesn't list them.
    return (
        "Set its four freshness fields to today's values, from "
        f"review_bot.evals.freshness_fields(case.json), or {live_advice}"
    )


def _live_recording(loaded: LoadedCase) -> Recording | None:
    path = loaded.file.parent / "recordings" / "live" / "recording.json"
    if not path.is_file():
        return None
    recording = load_recording(path)
    if recording.kind != "live":
        raise EvalInputError(
            f"{path} is a {recording.kind} recording under recordings/live/. Move it to "
            "recordings/synthetic/: only --recording reads a synthetic one."
        )
    if recording.case_id != loaded.case.id:
        raise EvalInputError(
            f"{path}: case_id {recording.case_id!r} isn't its case's id {loaded.case.id!r}. "
            "Record it again."
        )
    return recording


def _check_synthetic_recordings(loaded: LoadedCase) -> None:
    for path in sorted((loaded.file.parent / "recordings" / "synthetic").glob("*.json")):
        recording = load_recording(path)
        if recording.kind != "synthetic":
            raise EvalInputError(
                f"{path} is a {recording.kind} recording under recordings/synthetic/. Move it "
                "to recordings/live/recording.json."
            )


def _control_recording(path: Path) -> Recording:
    recording = load_recording(path)
    if recording.kind != "synthetic":
        raise EvalInputError(
            f"--recording {path} is a {recording.kind} recording, but --recording reads only "
            "synthetic ones. Replay reads each case's live recording itself."
        )
    return recording


async def _replay_case(loaded: LoadedCase, recording: Recording | None) -> CaseEvidence:
    """Run the case's diff through the pipeline once for the rules, then once per recorded run.

    A recorded run's findings are what ``MockProvider`` returns, so they meet today's verify and
    dedup. A failed run isn't replayed: it found nothing. With no recording, only the rules run.

    Raises:
        EvalInputError: If the case's diff can't be reviewed.
    """
    with tempfile.TemporaryDirectory(prefix="review-bot-eval-") as audit_dir:
        audit_path = Path(audit_dir) / "audit.jsonl"
        rules_only = await _review(loaded, (), audit_path)
        rule_findings = tuple(
            reviewed.finding
            for reviewed in rules_only.verification.kept
            if reviewed.source is Source.RULE
        )
        recorded_runs = () if recording is None else recording.runs
        runs = tuple([await _replay_run(loaded, run, audit_path) for run in recorded_runs])
    return CaseEvidence(rule_findings, runs)


async def _replay_run(loaded: LoadedCase, run: RecordedRun, audit_path: Path) -> RunFindings:
    if run.error_category is not None:
        return RunFindings(anchor_valid=(), final=(), error=run.error_category)
    # ``findings`` is set whenever ``error`` isn't (``RecordedRun``'s check).
    result = await _review(loaded, run.findings or (), audit_path)
    verification = result.verification
    return RunFindings(
        anchor_valid=tuple(
            check.proposed.model_copy(update={"file": check.diff_path})
            for check in verification.ai_anchor_valid
        ),
        final=tuple(
            reviewed.finding for reviewed in verification.kept if reviewed.source is Source.AI
        ),
    )


async def _review(
    loaded: LoadedCase, findings: Sequence[Finding], audit_path: Path
) -> PipelineResult:
    audit = AuditLog(audit_path, clock=lambda: datetime.now(UTC))
    provider = MockProvider(findings=findings)
    try:
        return await run_pipeline(loaded.diff_path, _replay_settings(), provider, audit)
    except InputError as exc:
        raise _unreviewable(loaded, exc) from exc


def _replay_settings() -> Settings:
    # Built here, not from the environment: replay always scores at the lowest threshold.
    return Settings(provider="mock")


def _unreviewable(loaded: LoadedCase, exc: InputError) -> EvalInputError:
    return EvalInputError(f"{loaded.file}: its diff can't be reviewed: {exc}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
