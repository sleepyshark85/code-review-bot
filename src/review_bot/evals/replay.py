"""Replay a case's recorded runs through today's pipeline, with ``MockProvider`` (ADR 0007).

Nothing here calls a model. In context mode, the case's ``repo`` is built as a git repository
for the tools (``evals.fixture_repo``). In either mode, the domain rules that a
``.review-bot.toml`` at the root of the case's ``repo`` enables run, as a pull request's base
commit's would. The repository and each pipeline run's audit log go to a temporary directory that
is deleted afterwards, so replay writes nothing that outlives it. It also holds
``check_sendable``, ``live``'s pre-flight check of each case, and what ``live`` reviews a case
with in context mode: its built repository and its domain rules.
"""

import dataclasses
import hashlib
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from review_bot.audit import AuditLog, utc_now
from review_bot.context.tools import RepoTools
from review_bot.diff_input import load_diff
from review_bot.diff_parser import parse_diff
from review_bot.errors import ConfigFileError, GitError, InputError
from review_bot.evals.fixture_repo import FixtureRepo, build_fixture_repo
from review_bot.evals.formats import (
    MODES,
    EvalInputError,
    EvalMode,
    LoadedCase,
    RecordedRun,
    Recording,
    find_case_files,
    load_case,
    load_recording,
)
from review_bot.evals.score import (
    CaseEvidence,
    CaseReplays,
    CaseResult,
    RunFindings,
    score_case,
)
from review_bot.findings import Finding, Source
from review_bot.governance.policy import (
    BUILTIN_POLICY,
    Policy,
    check_context_allowed,
    check_settings,
)
from review_bot.pr.config import CONFIG_PATH, parse_config_file
from review_bot.prompt import (
    CONTEXT_PROMPT_VERSION,
    PROMPT_VERSION,
    build_context_request,
    build_request,
)
from review_bot.providers.base import ReviewRequest
from review_bot.providers.mock import MockProvider
from review_bot.report import PipelineResult
from review_bot.review import ReviewContext, run_pipeline, tool_caps
from review_bot.rules import DomainRule
from review_bot.settings import Settings
from review_bot.verify import VerifyResult, verify_findings

__all__ = [
    "build_case_repo",
    "case_domain_rules",
    "check_sendable",
    "freshness_fields",
    "replay_both_modes",
    "replay_cases",
    "require_live",
    "score_recording",
]

_LIVE_COMMAND: Final = "python -m review_bot.evals live --cases DIR --runs N --max-calls M"
_LIVE_CONTEXT_COMMAND: Final = (
    "python -m review_bot.evals live --cases DIR --mode context --runs N --max-calls M "
    "--max-usd X --max-cost-usd Y"
)


@dataclasses.dataclass(frozen=True, slots=True)
class _CaseReview:
    """How the pipeline reviews a case, worked out once per case.

    ``context`` is the repository a context review's tools read, or None in diff mode,
    ``domain_rules`` the domain rules the case repo's ``.review-bot.toml`` enables, and
    ``policy`` the policy the review is under.
    """

    context: ReviewContext | None
    domain_rules: tuple[DomainRule, ...]
    policy: Policy


def freshness_fields(
    case_file: Path, mode: EvalMode = "diff", policy: Policy = BUILTIN_POLICY
) -> dict[str, str]:
    """Today's values of a recording's freshness fields, for the case at ``case_file``.

    In context mode, the tools offered are those ``policy`` allows.

    A recording is fresh only if its ``case_sha256``, ``diff_sha256``, ``prompt_version`` and
    ``prompt_sha256``, and in context mode its ``repo_tree``, all equal these. ``prompt_sha256``
    covers the system prompt and the user prompt built from the diff's reviewable text, and in
    context mode the case's linked issues and the tool definitions, so changing either prompt,
    the framing, an issue, a tool, or which files are skipped changes it. ``repo_tree`` is the
    tree ID of the case's built ``repo``, so changing any file under it changes it. Context mode
    builds that repository with git.

    Raises:
        EvalInputError: If the case is invalid, its diff can't be reviewed, or in context mode
            its repository can't be built or its ``.review-bot.toml`` is invalid.
    """
    loaded = load_case(case_file)
    if mode == "diff":
        return _case_freshness(loaded, None, (), policy)
    with tempfile.TemporaryDirectory(prefix="review-bot-eval-") as work_dir:
        repo = build_case_repo(loaded, Path(work_dir))
        return _case_freshness(loaded, repo, case_domain_rules(loaded), policy)


def _case_freshness(
    loaded: LoadedCase,
    repo: FixtureRepo | None,
    domain_rules: Sequence[DomainRule],
    policy: Policy,
) -> dict[str, str]:
    """``freshness_fields`` for a case already loaded: in context mode with its built ``repo``.

    ``domain_rules`` are those the case's repo enables, which only the context prompt names, and
    ``policy`` says which tools it offers.

    Raises:
        EvalInputError: If the case's diff can't be reviewed.
    """
    settings = _replay_settings(policy)
    try:
        diff = load_diff(loaded.diff_path, max_bytes=settings.max_diff_bytes)
        reviewable_text = parse_diff(diff).reviewable_text
    except InputError as exc:
        raise _unreviewable(loaded, exc) from exc
    fields = {"case_sha256": loaded.case_sha256, "diff_sha256": _sha256_text(diff)}
    # The same request ``run_pipeline`` builds; ``model`` isn't part of either prompt.
    if repo is None:
        request = build_request(reviewable_text, model=settings.model)
        prompts = json.dumps([request.system_prompt, request.user_prompt])
        return fields | {"prompt_version": PROMPT_VERSION, "prompt_sha256": _sha256_text(prompts)}
    tools = RepoTools(repo.repo, caps=tool_caps(settings), allowed=policy.tools)
    request = build_context_request(
        reviewable_text,
        model=settings.model,
        tools=tools,
        domain_rules=domain_rules,
        issues=loaded.issues,
    )
    prompts = json.dumps([request.system_prompt, request.user_prompt, _tool_definitions(request)])
    return fields | {
        "prompt_version": CONTEXT_PROMPT_VERSION,
        "prompt_sha256": _sha256_text(prompts),
        "repo_tree": repo.tree,
    }


def _tool_definitions(request: ReviewRequest) -> list[dict[str, object]]:
    tools = () if request.tools is None else request.tools.definitions
    return [
        {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
        for tool in tools
    ]


async def replay_cases(
    cases_dir: Path,
    control: Path | None,
    *,
    mode: EvalMode = "diff",
    policy: Policy = BUILTIN_POLICY,
) -> list[CaseResult]:
    """Score every case under ``cases_dir`` on its live recording of ``mode``, or on rules alone.

    ``control`` is a synthetic recording (``--recording``) of ``mode`` that replaces its case's
    live one. A stale recording fails its case without scoring it. Each case is reviewed under
    ``policy``, which must allow a tool in context mode.

    Raises:
        EvalInputError: If a case or a recording read is invalid, a recording sits under the
            wrong kind's or mode's directory, ``control`` is live, of another mode or names no
            case, a case's repo has an invalid ``.review-bot.toml``, or in context mode a case's
            repository can't be built.
        ConfigError: In context mode, before anything is read, if ``policy`` allows no tool.
    """
    if mode == "context":
        check_context_allowed(policy)
    cases = [load_case(case_file) for case_file in find_case_files(cases_dir)]
    recordings = {loaded.case.id: _live_recordings(loaded)[mode] for loaded in cases}
    for loaded in cases:
        _check_synthetic_recordings(loaded)
    if control is not None:
        recording = _control_recording(control, mode)
        if recording.case_id not in recordings:
            raise EvalInputError(
                f"--recording {control}: case_id {recording.case_id!r} names no case under "
                f"{cases_dir}. Pass a recording of one of its cases."
            )
        recordings[recording.case_id] = recording
    return [
        await score_recording(loaded, recordings[loaded.case.id], mode=mode, policy=policy)
        for loaded in cases
    ]


async def replay_both_modes(cases_dir: Path) -> list[CaseReplays]:
    """Score every case under ``cases_dir`` on its live recording of each mode, where it has one.

    A stale recording isn't scored: its result says why (``score_recording``).

    Raises:
        EvalInputError: If a case or a live recording is invalid, a recording sits under the
            wrong mode's directory, or in context mode a case's repository can't be built.
    """
    cases = [load_case(case_file) for case_file in find_case_files(cases_dir)]
    recordings = [_live_recordings(loaded) for loaded in cases]
    replays = []
    for loaded, live in zip(cases, recordings, strict=True):
        diff = await _score_if_recorded(loaded, live["diff"], mode="diff")
        context = await _score_if_recorded(loaded, live["context"], mode="context")
        replays.append(CaseReplays(loaded.case, diff, context))
    return replays


async def _score_if_recorded(
    loaded: LoadedCase, recording: Recording | None, *, mode: EvalMode
) -> CaseResult | None:
    return None if recording is None else await score_recording(loaded, recording, mode=mode)


def require_live(result: CaseResult, default_model: str, *, mode: EvalMode) -> CaseResult:
    """``result``, failed too unless it was scored on a live recording of ``default_model``.

    ``mode`` is the replay's, so the advice says how to get a live recording of that mode.
    """
    recording = result.recording
    command = _LIVE_COMMAND if mode == "diff" else _LIVE_CONTEXT_COMMAND
    advice = f"record one with {command}"
    if recording is None or recording.kind != "live":
        problem = f"--require-live needs a live recording; {advice}"
    elif recording.model != default_model:
        problem = (
            f"--require-live needs a recording of the default model {default_model}, but this "
            f"one is of {recording.model}; {advice}"
        )
    else:
        return result
    return dataclasses.replace(result, problems=(*result.problems, problem))


async def check_sendable(loaded: LoadedCase) -> None:
    """Check that a review of the case's diff would call the model.

    The diff runs through the pipeline with ``MockProvider``, so it meets the pipeline's own
    checks, such as its secret scan, and nothing calls a real model.

    Raises:
        EvalInputError: If the diff can't be reviewed or holds a possible secret, or the case's
            repo has an invalid ``.review-bot.toml``, naming the case file, or if every file in
            it is skipped, naming the case.
    """
    with tempfile.TemporaryDirectory(prefix="review-bot-eval-") as audit_dir:
        review = _CaseReview(
            context=None, domain_rules=case_domain_rules(loaded), policy=BUILTIN_POLICY
        )
        result = await _review(loaded, (), Path(audit_dir) / "audit.jsonl", review)
    if not result.model_called:
        raise EvalInputError(
            f"case {loaded.case.id}: nothing to review: every file is skipped, so no call would "
            "reach the model. Give the case a diff with a file that isn't skipped."
        )


async def score_recording(
    loaded: LoadedCase,
    recording: Recording | None,
    *,
    mode: EvalMode,
    policy: Policy = BUILTIN_POLICY,
) -> CaseResult:
    """Score the case in ``mode`` on ``recording`` through today's pipeline, or on rules alone.

    The pipeline runs under ``policy``. A stale recording fails the case without being scored.

    Raises:
        EvalInputError: If the case's diff can't be reviewed, its repo has an invalid
            ``.review-bot.toml``, or in context mode its repository can't be built.
    """
    case_id = loaded.case.id
    domain_rules = case_domain_rules(loaded)
    with tempfile.TemporaryDirectory(prefix="review-bot-eval-") as work_dir:
        repo = None if mode == "diff" else build_case_repo(loaded, Path(work_dir))
        if recording is not None:
            today = _case_freshness(loaded, repo, domain_rules, policy)
            stale = [name for name, value in today.items() if getattr(recording, name) != value]
            if stale:
                verb = "differs" if len(stale) == 1 else "differ"
                problem = (
                    f"the recording is stale: its {', '.join(stale)} {verb} from today's, so it "
                    f"wasn't scored. {_refresh_advice(recording)}"
                )
                return CaseResult(case_id, recording, None, (problem,))
        # As ``review-bot --context-repo`` reviews: the repo isn't known to be the diff's head.
        context = (
            None
            if repo is None
            else ReviewContext(repo=repo.repo, read_issues=lambda: loaded.issues)
        )
        review = _CaseReview(context=context, domain_rules=domain_rules, policy=policy)
        evidence = await _replay_case(loaded, recording, review, Path(work_dir) / "audit.jsonl")
    return CaseResult(case_id, recording, score_case(loaded.case, evidence))


def build_case_repo(loaded: LoadedCase, work_dir: Path) -> FixtureRepo:
    """Build the case's ``repo``, or an empty repository without one, under ``work_dir``.

    Raises:
        EvalInputError: If git can't build it.
    """
    try:
        return build_fixture_repo(loaded.repo_files, work_dir / "repo")
    except GitError as exc:
        raise EvalInputError(f"{loaded.file}: its repo can't be built: {exc}") from exc


def _refresh_advice(recording: Recording) -> str:
    """How to make a stale ``recording`` fresh. The message names the stale fields already."""
    if recording.mode == "context":
        if recording.kind == "live":
            return f"Record it again with {_LIVE_CONTEXT_COMMAND}"
        return (
            "Set its five freshness fields to today's values, from "
            'review_bot.evals.freshness_fields(case.json, "context")'
        )
    live_advice = f"record a live run with {_LIVE_COMMAND}"
    if recording.kind == "live":
        return f"Record it again: {live_advice}"
    # ``live`` can't remake a hand-written control, so updating its fields comes first.
    return (
        "Set its four freshness fields to today's values, from "
        f"review_bot.evals.freshness_fields(case.json), or {live_advice}"
    )


def _live_recordings(loaded: LoadedCase) -> dict[EvalMode, Recording | None]:
    """The case's live recording of each mode, or None where it has none.

    Raises:
        EvalInputError: If one is invalid, synthetic, of another case or of the other mode.
    """
    return {mode: _live_recording(loaded, mode) for mode in MODES}


def _live_recording(loaded: LoadedCase, mode: EvalMode) -> Recording | None:
    path = loaded.live_recording_path(mode)
    if not path.is_file():
        return None
    recording = load_recording(path)
    if recording.kind != "live":
        raise EvalInputError(
            f"{path} is a {recording.kind} recording under {_live_directory(mode)}. Move it to "
            "recordings/synthetic/: only --recording reads a synthetic one."
        )
    if recording.mode != mode:
        raise EvalInputError(
            f"{path} is a {recording.mode}-mode recording under {_live_directory(mode)}, which "
            f"holds {mode}-mode ones. Move it to {_live_directory(recording.mode)}."
        )
    if recording.case_id != loaded.case.id:
        raise EvalInputError(
            f"{path}: case_id {recording.case_id!r} isn't its case's id {loaded.case.id!r}. "
            "Record it again."
        )
    return recording


def _live_directory(mode: EvalMode) -> str:
    return "recordings/live/" if mode == "diff" else "recordings/live-context/"


def _check_synthetic_recordings(loaded: LoadedCase) -> None:
    for path in sorted((loaded.file.parent / "recordings" / "synthetic").glob("*.json")):
        recording = load_recording(path)
        if recording.kind != "synthetic":
            raise EvalInputError(
                f"{path} is a {recording.kind} recording under recordings/synthetic/. Move it "
                f"to {_live_directory(recording.mode)}recording.json."
            )


def _control_recording(path: Path, mode: EvalMode) -> Recording:
    recording = load_recording(path)
    if recording.kind != "synthetic":
        raise EvalInputError(
            f"--recording {path} is a {recording.kind} recording, but --recording reads only "
            "synthetic ones. Replay reads each case's live recording itself."
        )
    if recording.mode != mode:
        raise EvalInputError(
            f"--recording {path} is a {recording.mode}-mode recording, but replay runs in "
            f"{mode} mode. Pass --mode {recording.mode}, or a {mode}-mode recording."
        )
    return recording


async def _replay_case(
    loaded: LoadedCase,
    recording: Recording | None,
    review: _CaseReview,
    audit_path: Path,
) -> CaseEvidence:
    """Run the case's diff through the pipeline once for the rules, then once per recorded run.

    A recorded run's findings are what ``MockProvider`` returns, so they meet today's verify and
    dedup. A failed run isn't replayed: it found nothing. With no recording, only the rules run.
    Each review runs as ``review`` says.

    Raises:
        EvalInputError: If the case's diff can't be reviewed.
    """
    rules_only = await _review(loaded, (), audit_path, review)
    rule_findings = tuple(
        reviewed.finding
        for reviewed in rules_only.verification.kept
        if reviewed.source is Source.RULE
    )
    recorded_runs = () if recording is None else recording.runs
    runs = tuple([await _replay_run(loaded, run, audit_path, review) for run in recorded_runs])
    return CaseEvidence(rule_findings, runs)


async def _replay_run(
    loaded: LoadedCase, run: RecordedRun, audit_path: Path, review: _CaseReview
) -> RunFindings:
    if run.error_category is not None:
        return RunFindings(
            returned=(), anchor_valid=(), dropped=(), final=(), error=run.error_category
        )
    # ``findings`` is set whenever ``error`` isn't (``RecordedRun``'s check).
    returned = run.findings or ()
    result = await _review(loaded, returned, audit_path, review)
    context = review.context
    verification = (
        result.verification
        if context is None
        else _verify_against_recorded_tools(result, returned, run, context)
    )
    kept_ai = [reviewed for reviewed in verification.kept if reviewed.source is Source.AI]
    return RunFindings(
        returned=returned,
        anchor_valid=tuple(
            check.proposed.model_copy(update={"file": check.diff_path})
            for check in verification.ai_anchor_valid
        ),
        dropped=tuple(check.proposed for check in verification.dropped),
        final=tuple(reviewed.finding for reviewed in kept_ai),
        fingerprints=tuple(reviewed.fingerprint for reviewed in kept_ai),
        evidence_kept=sum(len(check.evidence.kept) for check in verification.checks),
        evidence_dropped=sum(check.evidence.dropped for check in verification.checks),
    )


def _verify_against_recorded_tools(
    result: PipelineResult, returned: Sequence[Finding], run: RecordedRun, context: ReviewContext
) -> VerifyResult:
    """Verify ``returned`` again, as the pipeline did, with the run's recorded tool calls.

    ``MockProvider`` calls no tool, so the pipeline's own check of the evidence saw an empty
    ledger. The recorded calls are the ledger the live review had.
    """
    rule_findings = [
        reviewed for reviewed in result.verification.kept if reviewed.source is Source.RULE
    ]
    return verify_findings(
        returned,
        rule_findings,
        result.parsed,
        ledger=run.ledger,
        is_diff_head=context.is_diff_head,
    )


async def _review(
    loaded: LoadedCase,
    findings: Sequence[Finding],
    audit_path: Path,
    review: _CaseReview,
) -> PipelineResult:
    audit = AuditLog(audit_path, clock=utc_now)
    provider = MockProvider(findings=findings)
    try:
        return await run_pipeline(
            loaded.diff_path,
            _replay_settings(review.policy),
            provider,
            audit,
            context=review.context,
            domain_rules=review.domain_rules,
            policy=review.policy,
        )
    except InputError as exc:
        raise _unreviewable(loaded, exc) from exc


def case_domain_rules(loaded: LoadedCase) -> tuple[DomainRule, ...]:
    """The domain rules the ``.review-bot.toml`` at the root of the case's ``repo`` enables.

    Empty without a ``repo``, or without that file in it.

    Raises:
        EvalInputError: If the file is invalid, naming the case file and the problem.
    """
    config_file = next((file for file in loaded.repo_files if file.path == CONFIG_PATH), None)
    if config_file is None:
        return ()
    try:
        config = parse_config_file(config_file.content, source=CONFIG_PATH)
    except ConfigFileError as exc:
        raise EvalInputError(
            f"{loaded.file}: its repo's {CONFIG_PATH} is invalid: {exc}. Fix the case's repo."
        ) from exc
    return config.domain_rules


def _replay_settings(policy: Policy) -> Settings:
    # Built here, not from the environment: replay always scores at the lowest threshold. No
    # cost cap is set, so the policy's ceiling, if any, is the cap, and never exceeded.
    return check_settings(Settings(provider="mock"), policy)


def _unreviewable(loaded: LoadedCase, exc: InputError) -> EvalInputError:
    return EvalInputError(f"{loaded.file}: its diff can't be reviewed: {exc}")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
