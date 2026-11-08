"""Orchestrate one review: scan, parse, run the rules, call the provider, verify, decide.

Each stage's decision is recorded in the audit log.
"""

import asyncio
import hashlib
import os
import time
from collections.abc import Iterable, Sequence
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Final

from review_bot.audit import AuditLog, JsonValue, Stage, file_fields, scrub_diagnostics
from review_bot.console import write_diagnostic
from review_bot.diff_input import load_diff
from review_bot.diff_parser import DecodedPath, ParsedDiff, parse_diff
from review_bot.errors import DiffNotParsableError, ProviderError, SecretDetectedError
from review_bot.policy import Decision, decide
from review_bot.prompt import PROMPT_VERSION, build_request
from review_bot.providers.base import LLMProvider, ReviewResponse, TokenUsage
from review_bot.report import PipelineResult, section_path
from review_bot.rules import RuleResult, run_rules
from review_bot.secret_scan import SecretHit, redact, scan_secrets
from review_bot.settings import Settings
from review_bot.verify import ProposalCheck, VerifyResult, verify_findings

__all__ = ["run_pipeline"]

_MAX_NAMED_SECRETS: Final = 5
# Caps each list in the parse record, so its size doesn't grow with the diff.
_MAX_LISTED_IN_AUDIT: Final = 50


class _AgentSkipReason(StrEnum):
    """Why the model wasn't called."""

    SECRET_DETECTED = "secret_detected"  # noqa: S105 - a reason's name, not a secret
    NOTHING_TO_REVIEW = "nothing_to_review"


class _VerifyOutcome(StrEnum):
    KEPT = "kept"
    DROPPED = "dropped"
    DUPLICATE = "duplicate"


class _PolicyOutcome(StrEnum):
    REPORTED = "reported"
    BELOW_THRESHOLD = "below_threshold"


async def run_pipeline(
    diff_path: Path, settings: Settings, provider: LLMProvider, audit: AuditLog
) -> PipelineResult:
    """Review the diff at ``diff_path`` with ``provider``, sending only the files not skipped.

    The rules' findings come first. A model finding is reported only if its file and line are in
    the diff and it repeats no earlier one, and any finding only if its severity meets the
    threshold. Each stage's decision goes to ``audit``; the run's end is the caller's to record.
    The provider's warnings go to stderr too, before the caller writes the review.

    Raises:
        InputError: If the diff can't be reviewed, or holds a possible secret; the provider is
            not called.
        ProviderError: If the provider fails.
        AuditError: If a record can't be written.
    """
    diff = await asyncio.to_thread(load_diff, diff_path, max_bytes=settings.max_diff_bytes)
    audit.record(Stage.RUN, "start", _start_data(settings, diff))
    # Before the parser, so nothing the parser misreads or drops can carry a secret past the scan.
    _stop_on_secrets(scan_secrets(diff), audit)
    try:
        parsed = parse_diff(diff)
    except DiffNotParsableError as exc:
        shown_path = redact(str(diff_path))
        raise DiffNotParsableError(f"Diff {shown_path} can't be parsed. {exc}") from exc
    # Only reached with clean raw text, so no secret is counted twice; before the parse record,
    # which names paths.
    _stop_on_secrets(_scan_decoded_paths(parsed.decoded_paths), audit)
    audit.record(Stage.PARSE, "parsed", _parsed_data(parsed))
    rule_result = run_rules(parsed)
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
        )

    response = await _call_provider(provider, parsed, settings, diff, audit)
    # The model proposes, code disposes: nothing it returns is reported before both checks.
    verification = verify_findings(response.findings, rule_result.findings, parsed)
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
    )


async def _call_provider(
    provider: LLMProvider, parsed: ParsedDiff, settings: Settings, diff: str, audit: AuditLog
) -> ReviewResponse:
    """Send the reviewable text, record the call, and write and record its warnings.

    Raises:
        ProviderError: If the provider fails; its scrubbed diagnostics are recorded first.
        AuditError: If a record can't be written.
    """
    started = time.perf_counter()
    try:
        response = await provider.review(
            build_request(parsed.reviewable_text, model=settings.model)
        )
    except ProviderError as exc:
        diagnostics = _scrubbed(exc.diagnostics, diff)
        audit.record(Stage.AGENT, "failed", {"category": exc.category, "diagnostics": diagnostics})
        raise
    audit.record(
        Stage.AGENT,
        "called",
        {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "duration_s": round(time.perf_counter() - started, 3),
        },
    )
    for warning in response.warnings:
        write_diagnostic(warning)
        audit.record(Stage.AGENT, "warning", {"text": "\n".join(_scrubbed([warning], diff))})
    return response


def _start_data(settings: Settings, diff: str) -> dict[str, JsonValue]:
    # ``load_diff`` decoded the file as strict UTF-8, so encoding gives back its exact bytes.
    diff_bytes = diff.encode("utf-8")
    return {
        "version": version("review-bot"),
        "provider": settings.provider,
        "model": settings.model,
        "prompt_version": PROMPT_VERSION,
        "threshold": settings.threshold,
        "format": settings.format,
        "diff_sha256": hashlib.sha256(diff_bytes).hexdigest(),
        "diff_bytes": len(diff_bytes),
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
    }


def _record_verification(verification: VerifyResult, audit: AuditLog) -> None:
    for check in verification.checks:
        audit.record(Stage.VERIFY, "checked", _verify_data(check))


def _verify_data(check: ProposalCheck) -> dict[str, JsonValue]:
    """A proposal's anchor and outcome, with hashes in place of the model's text.

    A ``file`` that names no diff path is the model's own text, so only its hash and length are
    kept. ``fingerprint`` is set for a kept finding, and ``duplicate_of`` for a duplicate.
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
