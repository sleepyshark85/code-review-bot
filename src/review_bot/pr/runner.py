"""One pull request run: refuse it, or diff and review it, then build its shadow report.

A run comes from the pull request's event, or is backfilled once it has merged.

The collaborators come in as arguments, so tests run it on a local clone with a scripted
provider (Phase 2 design, Data flow).
"""

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Final

from review_bot.audit import AuditLog, Stage
from review_bot.pr.event import PullRequestRef
from review_bot.pr.git import GitRepo
from review_bot.pr.outcome import Reason
from review_bot.pr.shadow import (
    REPORTED_ERRORS,
    Origin,
    ReportedError,
    ShadowReport,
    build_shadow_report,
)
from review_bot.providers.base import LLMProvider
from review_bot.report import PipelineResult
from review_bot.review import review_diff
from review_bot.settings import Settings, in_ci

__all__ = ["backfill_pull_request", "run_pull_request"]

# The ``pull_request`` actions that change what a review would see.
_REVIEWED_ACTIONS: Final = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})
# In CI, no key means no review (ADR 0012).
_KEY_VARIABLE: Final = "ANTHROPIC_API_KEY"


async def run_pull_request(  # noqa: PLR0913 - keyword-only collaborators, as the design names them
    pr: PullRequestRef,
    *,
    git: GitRepo,
    make_provider: Callable[[], LLMProvider],
    settings: Settings,
    audit: AuditLog,
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
) -> ShadowReport:
    """Review ``pr``'s own changes, from its merge base to its head, and report the run.

    A fork, an action that changes nothing to review, or a Claude run in CI with no
    ``ANTHROPIC_API_KEY`` is refused: neither git nor the provider runs, and the report is
    ``skipped`` with that reason. Otherwise the provider is made, and called at most once.

    Args:
        pr: The pull request, from its event.
        git: The clone holding its base and head commits.
        make_provider: Makes the provider, only once the run isn't refused.
        settings: The run's settings.
        audit: Where the run's records go; its end is the caller's to record.
        environ: The environment, read for ``CI`` and ``ANTHROPIC_API_KEY``.
        clock: Returns the current time, timezone-aware.

    Raises:
        GitError: If a commit is missing from the clone, or git fails.
        AuditError: If a record can't be written.
    """
    return await _run(
        pr,
        _refusal(pr, settings, environ),
        Origin.EVENT,
        git=git,
        make_provider=make_provider,
        settings=settings,
        audit=audit,
        clock=clock,
    )


async def backfill_pull_request(  # noqa: PLR0913 - keyword-only collaborators, as in run_pull_request
    pr: PullRequestRef,
    *,
    git: GitRepo,
    make_provider: Callable[[], LLMProvider],
    settings: Settings,
    audit: AuditLog,
    clock: Callable[[], datetime],
) -> ShadowReport:
    """Review a merged pull request's own changes, as ``run_pull_request`` does an open one's.

    A fork, or a pull request whose base or head commit isn't in the clone, is refused: the
    provider doesn't run, and the report is ``skipped`` with that reason. The report's
    ``origin`` is ``backfill``.

    Args:
        pr: The pull request, from the pulls file.
        git: The clone that should hold its base and head commits.
        make_provider: Makes the provider, only once the run isn't refused.
        settings: The run's settings.
        audit: Where the run's records go; its end is the caller's to record.
        clock: Returns the current time, timezone-aware.

    Raises:
        GitError: If git fails.
        AuditError: If a record can't be written.
    """
    refusal = Reason.FORK if pr.is_fork else await asyncio.to_thread(_missing_commit, pr, git)
    return await _run(
        pr,
        refusal,
        Origin.BACKFILL,
        git=git,
        make_provider=make_provider,
        settings=settings,
        audit=audit,
        clock=clock,
    )


async def _run(  # noqa: PLR0913 - the run's inputs, passed through
    pr: PullRequestRef,
    refusal: Reason | None,
    origin: Origin,
    *,
    git: GitRepo,
    make_provider: Callable[[], LLMProvider],
    settings: Settings,
    audit: AuditLog,
    clock: Callable[[], datetime],
) -> ShadowReport:
    """Review the pull request unless ``refusal`` says why not, and build its report."""
    started_at = clock()
    audit.record(
        Stage.RUN,
        "pull_request",
        {"repo": pr.repo, "number": pr.number, "base_sha": pr.base_sha, "head_sha": pr.head_sha},
    )
    review: PipelineResult | ReportedError | Reason
    if refusal is None:
        review = await _review(pr, git, make_provider, settings, audit)
    else:
        audit.record(Stage.RUN, "refused", {"reason": refusal})
        review = refusal
    return build_shadow_report(
        review,
        settings=settings,
        run_id=audit.run_id,
        started_at=started_at,
        finished_at=clock(),
        origin=origin,
        pr=pr,
    )


def _refusal(pr: PullRequestRef, settings: Settings, environ: Mapping[str, str]) -> Reason | None:
    """Why the run must not go ahead, or None if it may."""
    if pr.is_fork:
        return Reason.FORK
    if pr.action not in _REVIEWED_ACTIONS:
        return Reason.ACTION
    # Outside CI, the Claude CLI may use the person's own login, as ``review-bot`` does.
    if settings.provider == "claude" and in_ci(environ) and not environ.get(_KEY_VARIABLE):
        return Reason.NO_CREDENTIAL
    return None


def _missing_commit(pr: PullRequestRef, git: GitRepo) -> Reason | None:
    """``missing_commit`` if the clone lacks the base or head commit, else None."""
    if git.has_commit(pr.base_sha) and git.has_commit(pr.head_sha):
        return None
    return Reason.MISSING_COMMIT


async def _review(
    pr: PullRequestRef,
    git: GitRepo,
    make_provider: Callable[[], LLMProvider],
    settings: Settings,
    audit: AuditLog,
) -> PipelineResult | ReportedError:
    """Diff the pull request and review it; an error that still ends in a report is returned."""
    try:
        diff = await asyncio.to_thread(
            git.diff, pr.base_sha, pr.head_sha, max_bytes=settings.max_diff_bytes
        )
        name = f"{pr.base_sha}...{pr.head_sha}"
        return await review_diff(diff, settings, make_provider(), audit, name=name)
    except REPORTED_ERRORS as exc:
        return exc
