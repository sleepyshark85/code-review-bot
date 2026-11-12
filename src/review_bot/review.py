"""Orchestrate one review: scan, parse, run the rules, call the provider, verify, decide.

Each stage's decision is recorded in the audit log.
"""

import asyncio
import hashlib
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Final

from review_bot.audit import AuditLog, JsonValue, Stage, file_fields, scrub_diagnostics
from review_bot.console import write_diagnostic
from review_bot.context.issues import LinkedIssue
from review_bot.context.ledger import LineSpan, ToolCall
from review_bot.context.tools import RepoRef, RepoTools, ToolCaps
from review_bot.diff_input import load_diff
from review_bot.diff_parser import DecodedPath, ParsedDiff, parse_diff
from review_bot.errors import (
    DiffNotParsableError,
    DiffTooLargeError,
    ProviderError,
    SecretDetectedError,
)
from review_bot.governance.policy import (
    BUILTIN_POLICY,
    Policy,
    check_context_allowed,
    check_settings,
)
from review_bot.governance.redaction import RedactionCounts, RedactionKind, add_counts, mask_diff
from review_bot.prompt import (
    CONTEXT_PROMPT_VERSION,
    PROMPT_VERSION,
    build_context_request,
    build_request,
)
from review_bot.providers.base import (
    DeniedTool,
    LLMProvider,
    ReviewRequest,
    ReviewResponse,
    TokenUsage,
)
from review_bot.report import PipelineResult, redaction_json, section_path
from review_bot.rules import DomainRule, RuleResult, run_rules
from review_bot.secret_scan import SecretHit, redact, scan_secrets
from review_bot.settings import Settings
from review_bot.threshold import Decision, decide
from review_bot.verify import ProposalCheck, VerifyResult, verify_findings

__all__ = ["ReviewContext", "review_diff", "run_pipeline", "tool_caps"]

_MAX_NAMED_SECRETS: Final = 5
# Caps each list in the parse record and each tool call's spans, so no record grows with the
# diff or the repository.
_MAX_LISTED_IN_AUDIT: Final = 50


class _AgentSkipReason(StrEnum):
    """Why the model wasn't called."""

    SECRET_DETECTED = "secret_detected"  # noqa: S105 - a reason's name, not a secret
    NOTHING_TO_REVIEW = "nothing_to_review"
    TOO_LARGE = "too_large"


class _VerifyOutcome(StrEnum):
    KEPT = "kept"
    DROPPED = "dropped"
    DUPLICATE = "duplicate"


class _PolicyOutcome(StrEnum):
    REPORTED = "reported"
    BELOW_THRESHOLD = "below_threshold"


@dataclass(frozen=True, slots=True)
class ReviewContext:
    """What a context review may look up: the commit its tools read, and the issues it closes.

    ``is_diff_head`` says that commit is the one the diff ends at, as in PR mode, so the lines
    the tools return count as evidence in the files the diff changes too. ``read_issues``
    returns the linked issues, with the redaction kinds it is given masked as ``linked_issue``
    masks them; the review calls it only once the model will be called, so a review stopped
    before then reads none. Those whose text is sent follow the diff in the prompt.
    """

    repo: RepoRef
    is_diff_head: bool = False
    read_issues: Callable[[frozenset[RedactionKind]], tuple[LinkedIssue, ...]] = lambda _: ()


async def run_pipeline(  # noqa: PLR0913 - context, domain_rules and policy are keyword-only
    diff_path: Path,
    settings: Settings,
    provider: LLMProvider,
    audit: AuditLog,
    *,
    context: ReviewContext | None = None,
    domain_rules: Sequence[DomainRule] = (),
    policy: Policy = BUILTIN_POLICY,
) -> PipelineResult:
    """Read the diff at ``diff_path``, then review it as ``review_diff`` does.

    Raises:
        InputError: If the diff file can't be read whole, or as ``review_diff`` raises it.
        ProviderError: If the provider fails.
        AuditError: If a record can't be written.
    """
    diff = await asyncio.to_thread(load_diff, diff_path, max_bytes=settings.max_diff_bytes)
    name = redact(str(diff_path))
    return await review_diff(
        diff,
        settings,
        provider,
        audit,
        name=name,
        context=context,
        domain_rules=domain_rules,
        policy=policy,
    )


async def review_diff(  # noqa: PLR0913 - name, exclude, context, domain_rules and policy are keyword-only
    diff: str,
    settings: Settings,
    provider: LLMProvider,
    audit: AuditLog,
    *,
    name: str,
    exclude: Sequence[str] = (),
    context: ReviewContext | None = None,
    domain_rules: Sequence[DomainRule] = (),
    policy: Policy = BUILTIN_POLICY,
) -> PipelineResult:
    """Review ``diff`` with ``provider``, sending only the files not skipped.

    The rules' findings come first. A model finding is reported only if its file and line are in
    the diff and it repeats no earlier one, and any finding only if its severity meets the
    threshold. A kept model finding keeps only the evidence spans the model was shown, and its
    suggestion only in context mode, whose prompt asks for one, if it may replace the line. Each
    stage's decision goes to ``audit``; the run's end is the caller's to record. The provider's
    warnings go to stderr too, before the caller writes the review.

    Args:
        diff: The diff's text.
        settings: The run's settings.
        provider: The model to call.
        audit: Where each stage's decision goes.
        name: How messages name the diff; it must already be redacted.
        exclude: Globs whose matching files are skipped too, as ``parse_diff`` matches them.
        context: The commit the model's tools read, within the settings' tool caps, and the
            linked issues sent after the diff, with the context prompt, read only once the diff
            has passed every check before the model call; None reviews in diff mode, with no
            tools.
        domain_rules: The domain rules the repository enables, which run with the built-in
            rules, and in context mode are named in the prompt too.
        policy: The policy the review runs under: its tools are the only ones offered, and
            its version and hash are recorded. Its redaction kinds are masked in the diff, the
            tool results and the linked issues sent, after the secret scan, and if it has any,
            the counts are recorded. ``settings`` must already be within it, as
            ``check_settings`` makes them, since the provider was built from them.

    Raises:
        InputError: If the diff isn't a diff, holds a possible secret, or its files to review
            are over ``max_review_bytes``; the provider is not called. ``GitError`` if git
            fails during a tool call. ``ConfigError``, before anything is recorded, if the cost
            cap is above the policy's ceiling, or ``context`` is given and the policy allows no
            tool.
        ValueError: If the policy has a ceiling and ``settings`` have no cost cap, so
            ``check_settings`` wasn't applied.
        ProviderError: If the provider fails.
        AuditError: If a record can't be written.
    """
    _require_within_policy(settings, policy, is_context=context is not None)
    prompt_version = PROMPT_VERSION if context is None else CONTEXT_PROMPT_VERSION
    context_commit = None if context is None else context.repo.commit
    audit.record(Stage.RUN, "start", _start_data(settings, diff, prompt_version, policy))
    # Before the parser, so nothing the parser misreads or drops can carry a secret past the scan.
    _stop_on_secrets(scan_secrets(diff), audit)
    try:
        parsed = parse_diff(diff, exclude=exclude)
    except DiffNotParsableError as exc:
        raise DiffNotParsableError(f"Diff {name} can't be parsed. {exc}") from exc
    # Only reached with clean raw text, so no secret is counted twice; before the parse record,
    # which names paths.
    _stop_on_secrets(_scan_decoded_paths(parsed.decoded_paths), audit)
    audit.record(Stage.PARSE, "parsed", _parsed_data(parsed))
    rule_result = run_rules(parsed, domain_rules=domain_rules)
    audit.record(Stage.STATIC, "rules", _rules_data(rule_result))

    skipped = tuple(file for file in parsed.files if file.skip_reason is not None)
    if len(skipped) == len(parsed.files):
        # The rules check only files that aren't skipped, so they found nothing here either.
        audit.record(Stage.AGENT, "skipped", {"reason": _AgentSkipReason.NOTHING_TO_REVIEW})
        return PipelineResult(
            VerifyResult(kept=(), checks=()),
            decide((), settings.threshold),
            TokenUsage(0, 0),
            skipped,
            parsed=parsed,
            model_called=False,
            prompt_version=prompt_version,
            policy=policy,
            context_commit=context_commit,
        )

    _stop_if_too_large_to_review(parsed, settings, name, audit)
    # Masked only now, after the secret scan, so a secret still stops the review (ADR 0022).
    masked_diff = mask_diff(parsed.reviewable_text, policy.redaction)
    tools: RepoTools | None = None
    issue_counts: RedactionCounts = {}
    if context is None:
        request = build_request(masked_diff.text, model=settings.model)
    else:
        issues = await asyncio.to_thread(context.read_issues, policy.redaction)
        issue_counts = add_counts(issue.redacted for issue in issues)
        tools = RepoTools(
            context.repo,
            caps=tool_caps(settings),
            allowed=policy.tools,
            redaction=policy.redaction,
        )
        request = build_context_request(
            masked_diff.text,
            model=settings.model,
            tools=tools,
            policy=policy,
            domain_rules=domain_rules,
            issues=issues,
        )
        _record_issues(issues, audit)
    try:
        response = await _call_provider(provider, request, diff, audit)
    finally:
        # Every call is recorded, even when the review then fails.
        ledger = () if tools is None else tools.ledger
        _record_tool_calls(ledger, audit)
        redaction = _by_source(
            diff=masked_diff.counts,
            tools={} if tools is None else tools.redacted,
            issues=issue_counts,
        )
        if policy.redaction:
            audit.record(Stage.STATIC, "redaction", {"counts": redaction_json(redaction)})
    # The model proposes, code disposes: nothing it returns is reported before both checks.
    verification = verify_findings(
        response.findings,
        rule_result.findings,
        parsed,
        ledger=ledger,
        is_diff_head=context is not None and context.is_diff_head,
        suggestions_requested=context is not None,
    )
    _record_verification(verification, audit)
    decision = decide(verification.kept, settings.threshold)
    _record_decision(decision, audit)
    return PipelineResult(
        verification,
        decision,
        response.usage,
        skipped,
        parsed=parsed,
        model_called=True,
        warnings=response.warnings,
        prompt_version=prompt_version,
        policy=policy,
        context_commit=context_commit,
        ledger=ledger,
        cost_usd=response.cost_usd,
        redaction=redaction,
    )


def _require_within_policy(settings: Settings, policy: Policy, *, is_context: bool) -> None:
    """Check ``settings``, and context mode if ``is_context``, are within ``policy``.

    Raises:
        ConfigError: If the cost cap is above the ceiling, or context mode needs a tool the
            policy doesn't allow.
        ValueError: If ``settings`` lack the cost cap the ceiling sets.
    """
    if is_context:
        check_context_allowed(policy)
    if policy.max_cost_usd is not None and settings.max_cost_usd is None:
        raise ValueError("A review's settings get the policy's ceiling (check_settings) first.")
    # Raises for a cost cap above the ceiling; the settings it returns are these.
    check_settings(settings, policy)


def _by_source(**counts: RedactionCounts) -> dict[str, RedactionCounts]:
    """The values masked, by the source of the text, leaving out any source with none."""
    return {source: each for source, each in counts.items() if each}


def tool_caps(settings: Settings) -> ToolCaps:
    """The tool caps a context review runs with, from ``settings``."""
    return ToolCaps(
        max_calls=settings.max_tool_calls,
        max_result_bytes=settings.max_tool_result_bytes,
        max_total_bytes=settings.max_tool_total_bytes,
    )


async def _call_provider(
    provider: LLMProvider, request: ReviewRequest, diff: str, audit: AuditLog
) -> ReviewResponse:
    """Send ``request``, record the call and the tools offered or denied, write and record warnings.

    Raises:
        ProviderError: If the provider fails; its denials and scrubbed diagnostics are recorded
            first.
        AuditError: If a record can't be written.
    """
    started = time.perf_counter()
    try:
        response = await provider.review(request)
    except ProviderError as exc:
        _record_denials(exc.denied_tools, request.policy, audit)
        diagnostics = _scrubbed(exc.diagnostics, diff)
        failure = {"category": exc.category, "cap": exc.cap, "diagnostics": diagnostics}
        audit.record(Stage.AGENT, "failed", failure)
        raise
    audit.record(
        Stage.AGENT,
        "called",
        {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cost_usd": None if response.cost_usd is None else str(response.cost_usd),
            "duration_s": round(time.perf_counter() - started, 3),
        },
    )
    if response.offered_tools is not None:
        audit.record(Stage.AGENT, "tools", {"tools": list(response.offered_tools)})
    _record_denials(response.denied_tools, request.policy, audit)
    for warning in response.warnings:
        write_diagnostic(warning)
        audit.record(Stage.AGENT, "warning", {"text": "\n".join(_scrubbed([warning], diff))})
    return response


def _record_denials(denied_tools: Iterable[DeniedTool], policy: Policy, audit: AuditLog) -> None:
    """Record each tool the provider's runtime denied, under ``policy``."""
    for denied in denied_tools:
        audit.record(
            Stage.AGENT,
            "denied",
            {"tool": denied.tool, "layer": denied.layer, "policy_version": policy.version},
        )


def _stop_if_too_large_to_review(
    parsed: ParsedDiff, settings: Settings, name: str, audit: AuditLog
) -> None:
    """Record the skip and raise if the text to send is over ``max_review_bytes``.

    Raises:
        DiffTooLargeError: If it is over the cap.
    """
    limit = settings.max_review_bytes
    review_bytes = len(parsed.reviewable_text.encode("utf-8"))
    if limit is None or review_bytes <= limit:
        return
    audit.record(Stage.AGENT, "skipped", {"reason": _AgentSkipReason.TOO_LARGE})
    raise DiffTooLargeError(
        f"Diff {name} has {review_bytes} bytes of files to review after skipping, over the "
        f"limit of {limit}. Split the change or raise --max-review-bytes."
    )


def _start_data(
    settings: Settings, diff: str, prompt_version: str, policy: Policy
) -> dict[str, JsonValue]:
    # The diff's reader decoded it as strict UTF-8, so encoding gives back its exact bytes.
    diff_bytes = diff.encode("utf-8")
    return {
        "version": version("review-bot"),
        "provider": settings.provider,
        "model": settings.model,
        "prompt_version": prompt_version,
        "threshold": settings.threshold,
        "format": settings.format,
        "diff_sha256": hashlib.sha256(diff_bytes).hexdigest(),
        "diff_bytes": len(diff_bytes),
        "policy_version": policy.version,
        "policy_sha256": policy.sha256,
    }


def _scrubbed(lines: Sequence[str], diff: str) -> list[str]:
    """``lines`` as the audit log may hold them: provider text is untrusted (ADR 0004)."""
    return list(scrub_diagnostics(lines, diff_text=diff, environ=os.environ))


def _stop_on_secrets(hits: Sequence[SecretHit], audit: AuditLog) -> None:
    """Record the hits and raise, so the diff never reaches the model, if there are any.

    Raises:
        SecretDetectedError: If ``hits`` isn't empty.
    """
    if hits:
        audit.record(Stage.STATIC, "secrets", _secrets_data(hits))
        audit.record(Stage.AGENT, "skipped", {"reason": _AgentSkipReason.SECRET_DETECTED})
        raise SecretDetectedError(_secrets_message(hits), hits=hits)


def _scan_decoded_paths(paths: Iterable[DecodedPath]) -> tuple[SecretHit, ...]:
    """Scan each path on its own, so a hit names the diff line the path was read from.

    A decoded path can hold an escaped line feed, so its own line numbers mean nothing here.
    """
    return tuple(
        SecretHit(hit.kind, path.diff_line) for path in paths for hit in scan_secrets(path.text)
    )


def _secrets_data(hits: Sequence[SecretHit]) -> dict[str, JsonValue]:
    return {"hits": [{"kind": hit.kind, "diff_line": hit.diff_line} for hit in hits]}


def _parsed_data(parsed: ParsedDiff) -> dict[str, JsonValue]:
    """Counts, then skipped files and flagged hunks in diff order, capped, with their totals.

    A hunk is named by its file, its 0-based index within the file and its declared new start,
    never by its header text, which the diff's author controls. Only listed entries are built, so
    a long path costs at most one entry per list slot.
    """
    skipped = [file for file in parsed.files if file.skip_reason is not None]
    flagged = [
        (file, index, hunk)
        for file in parsed.files
        for index, hunk in enumerate(file.hunks)
        if hunk.has_count_mismatch
    ]
    return {
        "files": len(parsed.files),
        "hunks": sum(len(file.hunks) for file in parsed.files),
        "skipped_files": [
            {**file_fields(section_path(file)), "reason": file.skip_reason}
            for file in skipped[:_MAX_LISTED_IN_AUDIT]
        ],
        "skipped_files_total": len(skipped),
        "flagged_hunks": [
            {**file_fields(section_path(file)), "index": index, "new_start": hunk.new_start}
            for file, index, hunk in flagged[:_MAX_LISTED_IN_AUDIT]
        ],
        "flagged_hunks_total": len(flagged),
    }


def _rules_data(result: RuleResult) -> dict[str, JsonValue]:
    return {
        "findings": [
            {
                "fingerprint": reviewed.fingerprint,
                "rule": reviewed.rule,
                **file_fields(reviewed.finding.file),
                "line": reviewed.finding.line,
                "severity": reviewed.finding.severity,
            }
            for reviewed in result.findings
        ],
        "unanchored_hits": result.unanchored_hits,
        "unchecked_lines": result.unchecked_lines,
    }


def _record_tool_calls(ledger: Iterable[ToolCall], audit: AuditLog) -> None:
    """Record each tool call: what it returned and how much, never its arguments or result."""
    for call in ledger:
        spans = [_span_data(span) for span in call.spans[:_MAX_LISTED_IN_AUDIT]]
        audit.record(
            Stage.CONTEXT,
            "tool_call",
            {
                "tool": call.tool,
                "outcome": call.outcome,
                "reason": call.reason,
                "spans": spans,
                "spans_total": len(call.spans),
                "result_bytes": call.result_bytes,
                "input_sha256": call.input_sha256,
            },
        )


def _record_issues(issues: Iterable[LinkedIssue], audit: AuditLog) -> None:
    """Record each linked issue's number, status and bytes of text sent, never the text."""
    for issue in issues:
        sent_bytes = len(issue.text.encode("utf-8"))
        audit.record(
            Stage.CONTEXT,
            "issue",
            {"number": issue.number, "status": issue.status, "bytes_sent": sent_bytes},
        )


def _span_data(span: LineSpan) -> JsonValue:
    """A span's path, as ``file_fields`` writes it, and its first and last lines."""
    return {
        "path": file_fields(span.path)["file"],
        "first_line": span.first_line,
        "last_line": span.last_line,
    }


def _record_verification(verification: VerifyResult, audit: AuditLog) -> None:
    for check in verification.checks:
        audit.record(Stage.VERIFY, "checked", _verify_data(check))


def _verify_data(check: ProposalCheck) -> dict[str, JsonValue]:
    """A proposal's anchor and outcome, with hashes in place of the model's text.

    A ``file`` that names no diff path is the model's own text, so only its hash and length are
    kept. ``fingerprint`` is set for a kept finding, and ``duplicate_of`` for a duplicate. Its
    evidence is only counted, so no span the model wrote is recorded, and of its suggestion only
    the reason it was dropped is.
    """
    finding = check.proposed
    if check.diff_path is None:
        file_data: dict[str, JsonValue] = {
            "file_sha256": _sha256(finding.file),
            "file_length": len(finding.file),
        }
    else:
        file_data = file_fields(check.diff_path)
    return {
        "fingerprint": check.fingerprint,
        **file_data,
        "line": finding.line,
        "severity": finding.severity,
        "outcome": _verify_outcome(check),
        "reason": check.drop_reason,
        "duplicate_of": check.duplicate_of,
        "rationale_sha256": _sha256(finding.rationale),
        "evidence_kept": len(check.evidence.kept),
        "evidence_dropped": check.evidence.dropped,
        "suggestion_dropped": check.suggestion_dropped,
    }


def _verify_outcome(check: ProposalCheck) -> _VerifyOutcome:
    if check.drop_reason is not None:
        return _VerifyOutcome.DROPPED
    if check.duplicate_of is not None:
        return _VerifyOutcome.DUPLICATE
    return _VerifyOutcome.KEPT


def _record_decision(decision: Decision, audit: AuditLog) -> None:
    """Record each kept finding's outcome: the reported ones, then those below the threshold."""
    outcomes = [(reviewed, _PolicyOutcome.REPORTED) for reviewed in decision.reported] + [
        (reviewed, _PolicyOutcome.BELOW_THRESHOLD) for reviewed in decision.below_threshold
    ]
    for reviewed, outcome in outcomes:
        finding = reviewed.finding
        data = {**file_fields(finding.file), "line": finding.line, "outcome": outcome}
        audit.record(Stage.POLICY, "decided", data)


def _sha256(text: str) -> str:
    # Model text is valid UTF-8: ``Finding`` rejects a lone surrogate.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _secrets_message(hits: Sequence[SecretHit]) -> str:
    """Say where the secrets are, without their values or the diff's path, which may hold one."""
    named = [f"{hit.kind} at diff line {hit.diff_line}" for hit in hits[:_MAX_NAMED_SECRETS]]
    if len(hits) > _MAX_NAMED_SECRETS:
        named.append(f"{len(hits) - _MAX_NAMED_SECRETS} more")
    if len(hits) == 1:
        found, advice = "1 possible secret", "Remove it and rotate it if it is real"
    else:
        found, advice = f"{len(hits)} possible secrets", "Remove them and rotate any that are real"
    return (
        f"secret: {found} in the diff ({'; '.join(named)}), so it was not sent to the model. "
        f"{advice}, then run again."
    )
