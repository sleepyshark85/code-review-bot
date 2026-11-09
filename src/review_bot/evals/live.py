"""Record each case's runs against the real model, within a call budget (P1-S16, ADR 0007).

The only eval code that calls a model. Before any call it refuses to run in CI, without a budget
that covers every call, or with a case whose diff can't be sent, such as one holding a secret or
one whose files are all skipped. Each call's outcome goes to stderr as it ends.
"""

import json
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Final

from review_bot.audit import EVAL_LIVE_AUDIT_LOG_PATH, AuditLog, Stage, utc_now
from review_bot.console import discard_unraisable_reports, write_diagnostic
from review_bot.errors import AuditError, ConfigError, InputError, ProviderError
from review_bot.evals.formats import (
    INTERNAL_ERROR,
    EvalInputError,
    LoadedCase,
    RecordedRun,
    Recording,
    RunErrorCategory,
    find_case_files,
    load_case,
    load_recording,
)
from review_bot.evals.render import count_noun
from review_bot.evals.replay import check_sendable, freshness_fields, score_recording
from review_bot.evals.score import CaseResult
from review_bot.findings import Finding
from review_bot.providers.base import LLMProvider
from review_bot.review import run_pipeline
from review_bot.secret_scan import redact, scan_secrets
from review_bot.settings import Settings, in_ci, load_settings

if TYPE_CHECKING:
    from review_bot.providers.claude_agent import SdkCall

__all__ = ["record_live"]

_RECORDING_SCHEMA: Final = "eval-recording/v1"
_REDACTED: Final = "[REDACTED]"
_NO_USAGE: Final = {"input_tokens": 0, "output_tokens": 0}


async def record_live(
    cases_dir: Path,
    *,
    runs: int,
    max_calls: int | None,
    environ: Mapping[str, str],
    sdk_call: "SdkCall | None" = None,
) -> list[CaseResult]:
    """Call the model ``runs`` times per case, write each case's live recording, and score it.

    Each call is one attempt, with the configured model and deadline, and appends to its own
    ``run_id`` in the live audit log. A case's recording replaces the old one only once all its
    runs are done, failed runs included. Once the calls end, ``sys.unraisablehook`` reports are
    discarded, so the SDK's shutdown can't add to stderr (P1-S18).

    Args:
        cases_dir: The eval cases directory.
        runs: How many calls to make per case.
        max_calls: The most calls allowed in all; None refuses, as no budget was given.
        environ: The process environment, read for ``CI``.
        sdk_call: Replaces the real SDK call, so tests stay offline.

    Raises:
        EvalInputError: Before any call, if ``CI`` is set, the planned calls exceed
            ``max_calls``, a case is invalid, its diff can't be reviewed or has nothing to
            review, or a setting is invalid. During the run, if the audit log or a recording
            can't be written.
    """
    if in_ci(environ):
        raise EvalInputError(
            "live never runs in CI, and CI is set in the environment. Run it by hand, outside CI."
        )
    cases = [load_case(case_file) for case_file in find_case_files(cases_dir)]
    _check_budget(len(cases), runs, max_calls)
    for loaded in cases:
        await check_sendable(loaded)
    settings = _live_settings()
    provider = _claude(settings, sdk_call)
    try:
        return [await _record_case(loaded, runs, settings, provider) for loaded in cases]
    finally:
        discard_unraisable_reports()


def _check_budget(case_count: int, runs: int, max_calls: int | None) -> None:
    planned = case_count * runs
    per_case = f"{count_noun(case_count, 'case')} x {count_noun(runs, 'run')}"
    plan = f"{count_noun(planned, 'call')} ({per_case})"
    if max_calls is None:
        raise EvalInputError(
            f"live needs --max-calls: this run would make {plan}. Pass --max-calls {planned} if "
            "that budget is approved."
        )
    if planned > max_calls:
        raise EvalInputError(
            f"live would make {plan}, more than --max-calls {max_calls}. Lower --runs, or pass "
            f"--max-calls {planned} if that budget is approved."
        )


def _live_settings() -> Settings:
    """The configured model, deadline and API timeout, with one attempt per call.

    Every other field is given as a flag, so its REVIEW_BOT_* variable is never read: a call sends
    the diff replay hashes, at replay's size limit, and the audit log goes to live's own path.

    Raises:
        EvalInputError: If the model, deadline or API timeout configured is invalid.
    """
    replay_defaults = Settings()
    fixed = {
        "provider": "claude",
        "max_retries": "0",
        "audit_log": str(EVAL_LIVE_AUDIT_LOG_PATH),
        "threshold": str(replay_defaults.threshold),
        "max_diff_bytes": str(replay_defaults.max_diff_bytes),
        "format": replay_defaults.format,
    }
    try:
        return load_settings(fixed)
    except ConfigError as exc:
        raise EvalInputError(str(exc)) from exc


def _claude(settings: Settings, sdk_call: "SdkCall | None") -> LLMProvider:
    # Deferred, so replay never loads the SDK.
    from review_bot.providers.claude_agent import ClaudeAgentProvider  # noqa: PLC0415 - see above

    return ClaudeAgentProvider(settings, sdk_call=sdk_call)


async def _record_case(
    loaded: LoadedCase, runs: int, settings: Settings, provider: LLMProvider
) -> CaseResult:
    """Make the case's calls, write its recording, and score the file as replay reads it."""
    fields = freshness_fields(loaded.file)
    recorded_runs = []
    for number in range(1, runs + 1):
        run = await _record_run(loaded, settings, provider)
        outcome = "ok" if run.error_category is None else f"failed ({run.error_category})"
        write_diagnostic(redact(f"live: case {loaded.case.id} run {number}/{runs} ... {outcome}"))
        recorded_runs.append(run)
    recording = Recording.model_validate(
        {
            "schema": _RECORDING_SCHEMA,
            "case_id": loaded.case.id,
            "kind": "live",
            **fields,
            "model": settings.model,
            "sdk_version": version("claude-agent-sdk"),
            "recorded_at": utc_now(),
            "runs": recorded_runs,
        }
    )
    _write_recording(loaded.live_recording_path, recording)
    return await score_recording(loaded, load_recording(loaded.live_recording_path))


async def _record_run(loaded: LoadedCase, settings: Settings, provider: LLMProvider) -> RecordedRun:
    """One call through the pipeline: the findings as returned, redacted, or its error category.

    An unexpected exception is recorded as a failed run with category ``internal``, so the runs
    already paid for are kept. The audit log gets its class name; its text, which can hold the
    model's or the CLI's output, goes nowhere.

    Raises:
        EvalInputError: If the audit log can't be written, or the diff can't be reviewed.
    """
    try:
        audit = AuditLog(settings.audit_log, clock=utc_now)
    except AuditError as exc:
        raise _unwritable_audit_log(settings) from exc
    try:
        result = await run_pipeline(loaded.diff_path, settings, provider, audit)
    except ProviderError as exc:
        return _failed_run(exc.category)
    except AuditError as exc:
        raise _unwritable_audit_log(settings) from exc
    except InputError as exc:
        raise EvalInputError(f"{loaded.file}: its diff can't be reviewed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - recorded as a failed run, as the docstring says
        audit.record(
            Stage.AGENT, "failed", {"category": INTERNAL_ERROR, "error": type(exc).__name__}
        )
        return _failed_run(INTERNAL_ERROR)
    # One check per proposal, in the order returned, so this is the model's answer before verify.
    returned = tuple(_scrubbed_finding(check.proposed) for check in result.verification.checks)
    return RecordedRun.model_validate(
        {
            "findings": returned,
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
            },
            "warnings": tuple(_scrubbed(warning) for warning in result.warnings),
        }
    )


def _failed_run(category: RunErrorCategory) -> RecordedRun:
    return RecordedRun.model_validate(
        {"error": {"category": category}, "usage": _NO_USAGE, "warnings": ()}
    )


def _unwritable_audit_log(settings: Settings) -> EvalInputError:
    return EvalInputError(
        f"live can't write its audit log {settings.audit_log}, under the working directory. "
        "Run it from a directory where that file can be created and written."
    )


def _scrubbed_finding(finding: Finding) -> Finding:
    suggestion = None if finding.suggestion is None else _scrubbed(finding.suggestion)
    return finding.model_copy(
        update={
            "file": _scrubbed(finding.file),
            "rationale": _scrubbed(finding.rationale),
            "suggestion": suggestion,
        }
    )


def _scrubbed(text: str) -> str:
    """``text`` with each secret value as ``[REDACTED]``, or all of it if that isn't enough.

    The file holds the JSON form, where a line feed becomes a two-character escape, joining lines
    the scan saw apart, so that form is scanned too.
    """
    redacted = redact(text)
    return _REDACTED if scan_secrets(json.dumps(redacted)) else redacted


def _write_recording(path: Path, recording: Recording) -> None:
    """Write ``recording`` to ``path`` atomically, so an interruption leaves the old file intact.

    Raises:
        EvalInputError: If the file would hold a possible secret, or can't be written.
    """
    data = recording.model_dump(mode="json", by_alias=True, exclude_none=True)
    text = json.dumps(data, indent=2) + "\n"
    if scan_secrets(text):
        raise EvalInputError(
            f"{path} wasn't written: it would hold a possible secret that redaction missed. Check "
            "the case's id, then run again."
        )
    partial = path.with_name(f"{path.name}.partial")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial.write_text(text, encoding="utf-8", newline="\n")
        partial.replace(path)
    except OSError as exc:
        raise EvalInputError(f"{path} can't be written: {exc.strerror}. Check the path.") from exc
