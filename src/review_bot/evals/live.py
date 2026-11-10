"""Record each case's runs against the real model, within a call and a dollar budget (ADR 0007).

The only eval code that calls a model (P1-S16, P3-S6). Before any call it refuses to run in CI,
except from a manual dispatch given ``--allow-dispatch`` (ADR 0015), without a budget that covers
every call, or with a case whose diff can't be sent, such as one holding a secret or one whose
files are all skipped. With a dollar budget, a call starts only while what was spent leaves room
for one more at its cap (ADR 0015). Context mode reviews each case over its ``repo``, built as
replay builds it. Each call's outcome goes to stderr as it ends.
"""

import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Final

from review_bot.audit import EVAL_LIVE_AUDIT_LOG_PATH, AuditLog, Stage, utc_now
from review_bot.console import discard_unraisable_reports, write_diagnostic
from review_bot.context.ledger import ToolCall
from review_bot.errors import AuditError, ConfigError, InputError, ProviderError
from review_bot.evals.formats import (
    INTERNAL_ERROR,
    EvalInputError,
    EvalMode,
    LoadedCase,
    RecordedRun,
    Recording,
    find_case_files,
    load_case,
    load_recording,
)
from review_bot.evals.render import count_noun
from review_bot.evals.replay import (
    build_case_repo,
    case_domain_rules,
    check_sendable,
    freshness_fields,
    score_recording,
)
from review_bot.evals.score import CaseResult
from review_bot.findings import Finding
from review_bot.providers.base import LLMProvider
from review_bot.review import ReviewContext, run_pipeline
from review_bot.rules import DomainRule
from review_bot.secret_scan import redact, scan_secrets
from review_bot.settings import Settings, live_run_refused, load_settings
from review_bot.spend import DollarBudget

if TYPE_CHECKING:
    from review_bot.providers.claude_agent import SdkCall

__all__ = ["LiveRequest", "LiveRun", "record_live"]

_RECORDING_SCHEMA: Final = "eval-recording/v1"
_REDACTED: Final = "[REDACTED]"
_NO_USAGE: Final = {"input_tokens": 0, "output_tokens": 0}


@dataclass(frozen=True, slots=True)
class LiveRequest:
    """What ``live`` is asked to record, from its flags.

    ``case_ids`` picks the cases to run; empty runs every case under ``cases_dir``. ``max_calls``
    is the most calls allowed in all; None refuses, as no budget was given. ``max_usd`` is the
    dollar allowance and ``max_cost_usd`` each call's cap: context mode needs both, diff mode
    both or neither. ``allow_dispatch`` is ``--allow-dispatch``.
    """

    cases_dir: Path
    runs: int
    max_calls: int | None
    mode: EvalMode = "diff"
    case_ids: tuple[str, ...] = ()
    max_usd: Decimal | None = None
    max_cost_usd: Decimal | None = None
    allow_dispatch: bool = False


@dataclass(frozen=True, slots=True)
class LiveRun:
    """What ``live`` did: each case it recorded, scored, and with a dollar budget, its spend.

    ``spent_usd`` counts each call's reported cost, or its cap if it reported none; it is 0
    without a budget. ``stopped_by_budget`` says the budget left too little for another call, so
    the planned cases after the last of ``results`` weren't recorded.
    """

    results: tuple[CaseResult, ...]
    budget: DollarBudget | None
    spent_usd: Decimal
    stopped_by_budget: bool

    @property
    def passed(self) -> bool:
        """Whether every planned case was recorded, and passed."""
        return not self.stopped_by_budget and all(result.passed for result in self.results)


class _OutOfBudgetError(Exception):
    """No call may start: ``spent_usd`` leaves less than one call's cap of the allowance."""

    def __init__(self, budget: DollarBudget, spent_usd: Decimal) -> None:
        """Record the budget and what was spent of it."""
        super().__init__("out of budget")
        self.budget = budget
        self.spent_usd = spent_usd


@dataclass(slots=True)
class _Spend:
    """What the calls so far count towards ``budget``; with no budget, nothing is counted."""

    budget: DollarBudget | None
    spent_usd: Decimal = Decimal(0)

    def check_room(self) -> None:
        """Check that another call may start.

        Raises:
            _OutOfBudgetError: If one more call at its cap wouldn't fit in the budget.
        """
        if self.budget is not None and not self.budget.has_room(self.spent_usd):
            raise _OutOfBudgetError(self.budget, self.spent_usd)

    def charge(self, run: RecordedRun) -> None:
        """Count ``run``'s call: its reported cost, or its cap if it reported none."""
        if self.budget is not None:
            self.spent_usd += self.budget.charge(run.cost_usd)


@dataclass(frozen=True, slots=True)
class _Reviewer:
    """How each call reviews a case's diff: with these settings and provider.

    In context mode, its tools read the repository ``context`` names, and the domain rules that
    repository enables run too.
    """

    settings: Settings
    provider: LLMProvider
    context: ReviewContext | None = None
    domain_rules: tuple[DomainRule, ...] = ()


@dataclass(frozen=True, slots=True)
class _Plan:
    """The calls a run would make: ``runs`` for each of ``case_count`` cases."""

    case_count: int
    runs: int

    @property
    def calls(self) -> int:
        """How many calls in all."""
        return self.case_count * self.runs

    def __str__(self) -> str:
        """Such as ``6 calls (2 cases x 3 runs)``."""
        per_case = f"{count_noun(self.case_count, 'case')} x {count_noun(self.runs, 'run')}"
        return f"{count_noun(self.calls, 'call')} ({per_case})"


async def record_live(
    request: LiveRequest, *, environ: Mapping[str, str], sdk_call: "SdkCall | None" = None
) -> LiveRun:
    """Call the model ``runs`` times per case, write each case's live recording, and score it.

    Each call is one attempt, with the configured model and deadline, and appends to its own
    ``run_id`` in the live audit log. A case's recording of the request's mode replaces the old
    one only once all its runs are done, failed runs included. Once the dollar budget can't
    cover another call, none starts: the case cut short, and any after it, keep their old
    recordings, and stderr names them. Once the calls end, ``sys.unraisablehook`` reports are
    discarded, so the SDK's shutdown can't add to stderr (P1-S18).

    Args:
        request: What to record, from the flags.
        environ: The process environment, read for ``CI`` and ``GITHUB_EVENT_NAME``.
        sdk_call: Replaces the real SDK call, so tests stay offline.

    Raises:
        EvalInputError: Before any call, if ``CI`` is set and the run isn't a
            ``workflow_dispatch`` given ``allow_dispatch``, a ``--case`` names no case, the
            planned calls exceed ``max_calls`` or need a dollar budget they lack or don't fit, a
            case is invalid, its diff can't be reviewed or has nothing to review, or a setting
            is invalid. During the run, if a case's repo can't be built, or the audit log or a
            recording can't be written.
    """
    if live_run_refused(environ, allow_dispatch=request.allow_dispatch):
        raise EvalInputError(
            "live runs in CI only from a workflow_dispatch run given --allow-dispatch, and this "
            "CI run (CI is set) isn't one. Run it by hand, outside CI, or dispatch live.yml."
        )
    cases = [
        load_case(case_file)
        for case_file in _selected_case_files(request.cases_dir, request.case_ids)
    ]
    plan = _Plan(len(cases), request.runs)
    _check_call_budget(plan, request.max_calls)
    spend = _Spend(_dollar_budget(request, plan))
    for loaded in cases:
        await check_sendable(loaded)
    settings = _live_settings(request.max_cost_usd)
    provider = _claude(settings, sdk_call)
    reviewer = _Reviewer(settings, provider)
    results: list[CaseResult] = []
    try:
        for loaded in cases:
            results.append(await _record_case(loaded, request, reviewer, spend))
    except _OutOfBudgetError as stop:
        write_diagnostic(_budget_stop_line(stop, cases[len(results) :]))
    finally:
        discard_unraisable_reports()
    return LiveRun(
        tuple(results),
        spend.budget,
        spend.spent_usd,
        stopped_by_budget=len(results) < len(cases),
    )


def _selected_case_files(cases_dir: Path, case_ids: Sequence[str]) -> list[Path]:
    """Every case file under ``cases_dir``, or only those of ``case_ids`` if any are given.

    Raises:
        EvalInputError: If ``cases_dir`` holds no case, or an ID names none of its cases.
    """
    case_files = find_case_files(cases_dir)
    if not case_ids:
        return case_files
    known = {case_file.parent.name for case_file in case_files}
    if unknown := [case_id for case_id in case_ids if case_id not in known]:
        raise EvalInputError(
            f"--case {', '.join(unknown)}: no such case under --cases {cases_dir}. Pass the name "
            "of a directory there that holds a case.json."
        )
    return [case_file for case_file in case_files if case_file.parent.name in case_ids]


def _check_call_budget(plan: _Plan, max_calls: int | None) -> None:
    if max_calls is None:
        raise EvalInputError(
            f"live needs --max-calls: this run would make {plan}. Pass --max-calls {plan.calls} "
            "if that budget is approved."
        )
    if plan.calls > max_calls:
        raise EvalInputError(
            f"live would make {plan}, more than --max-calls {max_calls}. Lower --runs, or pass "
            f"--max-calls {plan.calls} if that budget is approved."
        )


def _dollar_budget(request: LiveRequest, plan: _Plan) -> DollarBudget | None:
    """The dollar budget the calls are charged to, or None in diff mode given neither amount.

    Raises:
        EvalInputError: If context mode lacks either amount, diff mode has only one, or the
            planned calls, each at its cap, could cost more than ``--max-usd``.
    """
    max_usd, max_cost_usd = request.max_usd, request.max_cost_usd
    if request.mode == "diff" and max_usd is None and max_cost_usd is None:
        return None
    if max_usd is None or max_cost_usd is None:
        given = "--max-usd" if max_usd is not None else "--max-cost-usd"
        reason = "--mode context" if request.mode == "context" else given
        raise EvalInputError(
            f"live with {reason} needs both --max-usd and --max-cost-usd: this run would make "
            f"{plan}. Pass --max-cost-usd, the most one call may cost, and --max-usd, at least "
            f"{plan.calls} times that, if that budget is approved."
        )
    budget = DollarBudget(max_usd=max_usd, max_cost_usd=max_cost_usd)
    if not budget.covers(plan.calls):
        planned_usd = budget.planned_usd(plan.calls)
        raise EvalInputError(
            f"live would make {plan} of up to --max-cost-usd {max_cost_usd} each, {planned_usd} "
            f"USD in all, more than --max-usd {max_usd}. Lower --runs, or pick fewer cases with "
            f"--case, or pass --max-usd {planned_usd} if that budget is approved."
        )
    return budget


def _budget_stop_line(stop: _OutOfBudgetError, unrecorded: Sequence[LoadedCase]) -> str:
    budget = stop.budget
    cases = ", ".join(loaded.case.id for loaded in unrecorded)
    return (
        f"live: stopped: {stop.spent_usd} USD spent leaves less than --max-cost-usd "
        f"{budget.max_cost_usd} of --max-usd {budget.max_usd}, so no further call started. Not "
        f"recorded, so each keeps its old recording: {cases}. Record them with --case and an "
        "approved budget."
    )


def _live_settings(max_cost_usd: Decimal | None) -> Settings:
    """The configured model, deadline and API timeout, with one attempt per call.

    The fields in ``fixed`` take no value from their REVIEW_BOT_* variable: a call sends the diff
    replay hashes, at replay's size limit, its tools run at replay's caps, and the audit log goes
    to live's own path. Each call's cost cap is ``max_cost_usd`` (``--max-cost-usd``), or none,
    whatever REVIEW_BOT_MAX_COST_USD says.

    Raises:
        EvalInputError: If the model, deadline or API timeout configured is invalid,
            ``max_cost_usd`` is outside the cost cap's range, or without it,
            REVIEW_BOT_MAX_COST_USD is invalid.
    """
    replay_defaults = Settings()
    fixed = {
        "provider": "claude",
        "max_retries": "0",
        "audit_log": str(EVAL_LIVE_AUDIT_LOG_PATH),
        "threshold": str(replay_defaults.threshold),
        "max_diff_bytes": str(replay_defaults.max_diff_bytes),
        "format": replay_defaults.format,
        "max_tool_calls": str(replay_defaults.max_tool_calls),
        "max_tool_result_bytes": str(replay_defaults.max_tool_result_bytes),
        "max_tool_total_bytes": str(replay_defaults.max_tool_total_bytes),
        "max_cost_usd": None if max_cost_usd is None else str(max_cost_usd),
    }
    try:
        settings = load_settings(fixed)
    except ConfigError as exc:
        raise EvalInputError(str(exc)) from exc
    # Given None, ``load_settings`` reads REVIEW_BOT_MAX_COST_USD, which this then drops.
    return settings.model_copy(update={"max_cost_usd": max_cost_usd})


def _claude(settings: Settings, sdk_call: "SdkCall | None") -> LLMProvider:
    # Deferred, so replay never loads the SDK.
    from review_bot.providers.claude_agent import ClaudeAgentProvider  # noqa: PLC0415 - see above

    return ClaudeAgentProvider(settings, sdk_call=sdk_call)


async def _record_case(
    loaded: LoadedCase, request: LiveRequest, reviewer: _Reviewer, spend: _Spend
) -> CaseResult:
    """Make the case's calls in the request's mode, write its recording, and score it as read.

    In context mode, the case's ``repo`` is built in a temporary directory for its calls.

    Raises:
        _OutOfBudgetError: Before a call the budget can't cover; the recording isn't written.
    """
    mode = request.mode
    fields = freshness_fields(loaded.file, mode)
    if mode == "diff":
        recorded_runs = await _record_runs(loaded, request.runs, reviewer, spend)
    else:
        with tempfile.TemporaryDirectory(prefix="review-bot-eval-") as work_dir:
            repo = build_case_repo(loaded, Path(work_dir))
            # As replay reviews it: the repo is the diff's base, so not its head.
            in_repo = replace(
                reviewer,
                context=ReviewContext(repo=repo.repo),
                domain_rules=case_domain_rules(loaded),
            )
            recorded_runs = await _record_runs(loaded, request.runs, in_repo, spend)
    recording = Recording.model_validate(
        {
            "schema": _RECORDING_SCHEMA,
            "case_id": loaded.case.id,
            "kind": "live",
            "mode": mode,
            **fields,
            "model": reviewer.settings.model,
            "sdk_version": version("claude-agent-sdk"),
            "recorded_at": utc_now(),
            "runs": recorded_runs,
        }
    )
    path = loaded.live_recording_path(mode)
    _write_recording(path, recording)
    return await score_recording(loaded, load_recording(path), mode=mode)


async def _record_runs(
    loaded: LoadedCase, runs: int, reviewer: _Reviewer, spend: _Spend
) -> list[RecordedRun]:
    """Make the case's ``runs`` calls one by one, each charged to ``spend`` once it ends.

    Raises:
        _OutOfBudgetError: Before a call the budget can't cover.
    """
    recorded_runs = []
    for number in range(1, runs + 1):
        spend.check_room()
        run = await _record_run(loaded, reviewer)
        spend.charge(run)
        outcome = "ok" if run.error_category is None else f"failed ({run.error_category})"
        write_diagnostic(redact(f"live: case {loaded.case.id} run {number}/{runs} ... {outcome}"))
        recorded_runs.append(run)
    return recorded_runs


async def _record_run(loaded: LoadedCase, reviewer: _Reviewer) -> RecordedRun:
    """One call through the pipeline: what it returned, redacted, or its error category.

    A run that returned keeps its findings, its tool calls without their content, and its
    reported cost. A failed run keeps its error's category, and the cap that ended it if one did.
    An unexpected exception is recorded as a failed run with category ``internal``, so the runs
    already paid for are kept. The audit log gets its class name; its text, which can hold the
    model's or the CLI's output, goes nowhere.

    Raises:
        EvalInputError: If the audit log can't be written, or the diff can't be reviewed.
    """
    settings = reviewer.settings
    try:
        audit = AuditLog(settings.audit_log, clock=utc_now)
    except AuditError as exc:
        raise _unwritable_audit_log(settings) from exc
    try:
        result = await run_pipeline(
            loaded.diff_path,
            settings,
            reviewer.provider,
            audit,
            context=reviewer.context,
            domain_rules=reviewer.domain_rules,
        )
    except ProviderError as exc:
        return _failed_run({"category": exc.category, "cap": exc.cap})
    except AuditError as exc:
        raise _unwritable_audit_log(settings) from exc
    except InputError as exc:
        raise EvalInputError(f"{loaded.file}: its diff can't be reviewed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - recorded as a failed run, as the docstring says
        audit.record(
            Stage.AGENT, "failed", {"category": INTERNAL_ERROR, "error": type(exc).__name__}
        )
        return _failed_run({"category": INTERNAL_ERROR})
    # One check per proposal, in the order returned, so this is the model's answer before verify.
    returned = tuple(_scrubbed_finding(check.proposed) for check in result.verification.checks)
    usage = {
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "cost_usd": None if result.cost_usd is None else str(result.cost_usd),
    }
    return RecordedRun.model_validate(
        {
            "findings": returned,
            "usage": usage,
            "warnings": tuple(_scrubbed(warning) for warning in result.warnings),
            "tool_calls": tuple(_recorded_tool_call(call) for call in result.ledger),
        }
    )


def _recorded_tool_call(call: ToolCall) -> dict[str, object]:
    """``call`` as a recording holds it: paths, spans, outcome and size, never file text."""
    return {
        "tool": call.tool,
        "outcome": call.outcome,
        "reason": call.reason,
        "spans": [
            {"path": span.path, "first_line": span.first_line, "last_line": span.last_line}
            for span in call.spans
        ],
        "listed": call.listed,
        "result_bytes": call.result_bytes,
    }


def _failed_run(error: Mapping[str, object]) -> RecordedRun:
    return RecordedRun.model_validate({"error": error, "usage": _NO_USAGE, "warnings": ()})


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
