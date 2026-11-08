"""One pull request run: refuse it, or diff and review it, then build its shadow report.

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
from review_bot.pr.shadow import REPORTED_ERRORS, ReportedError, ShadowReport, build_shadow_report
from review_bot.providers.base import LLMProvider
from review_bot.report import PipelineResult
from review_bot.review import review_diff
from review_bot.settings import Settings

__all__ = ["run_pull_request"]

# The ``pull_request`` actions that change what a review would see.
_REVIEWED_ACTIONS: Final = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})
# Set by GitHub Actions and most CI services; there, no key means no review (ADR 0012).
_CI_VARIABLE: Final = "CI"
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
    started_at = clock()
    audit.record(
        Stage.RUN,
        "pull_request",
        {"repo": pr.repo, "number": pr.number, "base_sha": pr.base_sha, "head_sha": pr.head_sha},
    )
    review: PipelineResult | ReportedError | Reason
    refusal = _refusal(pr, settings, environ)
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
        pr=pr,
    )


def _refusal(pr: PullRequestRef, settings: Settings, environ: Mapping[str, str]) -> Reason | None:
    """Why the run must not go ahead, or None if it may."""
    if pr.is_fork:
        return Reason.FORK
    if pr.action not in _REVIEWED_ACTIONS:
        return Reason.ACTION
    # Outside CI, the Claude CLI may use the person's own login, as ``review-bot`` does.
    if settings.provider == "claude" and _CI_VARIABLE in environ and not environ.get(_KEY_VARIABLE):
        return Reason.NO_CREDENTIAL
    return None


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
