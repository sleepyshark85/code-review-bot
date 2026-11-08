"""One pull request run: refuse it, or review it and report it, and in comment mode apply it.

A run comes from the pull request's event, or is backfilled once it has merged.

The collaborators come in as arguments, so tests run it on a local clone with a scripted
provider and an in-memory GitHub (Phase 2 design, Data flow).
"""

import asyncio
import dataclasses
import functools
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Final

from review_bot.audit import AuditLog, Stage
from review_bot.errors import GitHubError
from review_bot.pr.event import PullRequestRef
from review_bot.pr.git import GitRepo
from review_bot.pr.github import BotComment, GitHubClient, NewComment
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.plan import Action
from review_bot.pr.shadow import (
    REPORTED_ERRORS,
    Applied,
    Mode,
    Origin,
    ReportedError,
    ShadowComment,
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
# All of a run's new comments go in one review, so it notifies once (Phase 2 design, Volume).
_POSTED_ACTIONS: Final = frozenset({Action.CREATE, Action.MOVE})


async def run_pull_request(  # noqa: PLR0913 - keyword-only collaborators, as the design names them
    pr: PullRequestRef,
    *,
    mode: Mode,
    git: GitRepo,
    make_provider: Callable[[], LLMProvider],
    github: GitHubClient | None,
    settings: Settings,
    audit: AuditLog,
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
) -> ShadowReport:
    """Review ``pr``'s own changes, from its merge base to its head, and report the run.

    A fork, an action that changes nothing to review, a Claude run in CI with no
    ``ANTHROPIC_API_KEY``, or in comment mode a draft, is refused: neither git, the provider nor
    GitHub runs, and the report is ``skipped`` with that reason. Otherwise the provider is made,
    and called at most once.

    In comment mode, a ``reviewed`` run plans against the bot's comments on ``pr`` and applies
    the plan (``_apply``); a ``blocked_secret`` or ``failed`` one only writes its summary; a
    ``skipped`` one calls GitHub not at all. A call that fails stops the rest, and the report
    holds its error.

    Args:
        pr: The pull request, from its event.
        mode: Whether to post on the pull request, or only report.
        git: The clone holding its base and head commits.
        make_provider: Makes the provider, only once the run isn't refused.
        github: The GitHub client in comment mode; None in shadow mode.
        settings: The run's settings.
        audit: Where the run's records go; its end is the caller's to record.
        environ: The environment, read for ``CI`` and ``ANTHROPIC_API_KEY``.
        clock: Returns the current time, timezone-aware.

    Raises:
        ValueError: If ``github`` is given in shadow mode, or missing in comment mode.
        GitError: If a commit is missing from the clone, or git fails.
        GitHubError: If the bot's comments can't be listed; nothing is posted.
        AuditError: If a record can't be written.
    """
    if (github is None) != (mode is Mode.SHADOW):
        raise ValueError("A pull request run has a GitHub client exactly in comment mode.")
    return await _run(
        pr,
        _refusal(pr, mode, settings, environ),
        Origin.EVENT,
        mode=mode,
        git=git,
        make_provider=make_provider,
        github=github,
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
    ``origin`` is ``backfill``, and its mode ``shadow``.

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
        mode=Mode.SHADOW,
        git=git,
        make_provider=make_provider,
        github=None,
        settings=settings,
        audit=audit,
        clock=clock,
    )


async def _run(  # noqa: PLR0913 - the run's inputs, passed through
    pr: PullRequestRef,
    refusal: Reason | None,
    origin: Origin,
    *,
    mode: Mode,
    git: GitRepo,
    make_provider: Callable[[], LLMProvider],
    github: GitHubClient | None,
    settings: Settings,
    audit: AuditLog,
    clock: Callable[[], datetime],
) -> ShadowReport:
    """Review the pull request unless ``refusal`` says why not, then report and apply it.

    ``github`` is given exactly in comment mode, as ``run_pull_request`` checks.
    """
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
    existing: tuple[BotComment, ...] = ()
    # Only a review that reached the model has findings to pair with the bot's comments.
    if github is not None and isinstance(review, PipelineResult) and review.model_called:
        existing = await asyncio.to_thread(github.list_bot_comments, pr)
    shadow = build_shadow_report(
        review,
        mode=mode,
        existing=existing,
        settings=settings,
        run_id=audit.run_id,
        started_at=started_at,
        finished_at=clock(),
        origin=origin,
        pr=pr,
    )
    # A skip posts and edits nothing (Phase 2 design, Comment state).
    if github is None or shadow.outcome is Outcome.SKIPPED:
        return shadow
    applied = await asyncio.to_thread(_apply, shadow, pr, github)
    return dataclasses.replace(applied, finished_at=clock())


def _refusal(
    pr: PullRequestRef, mode: Mode, settings: Settings, environ: Mapping[str, str]
) -> Reason | None:
    """Why the run must not go ahead, or None if it may."""
    if pr.is_fork:
        return Reason.FORK
    if pr.action not in _REVIEWED_ACTIONS:
        return Reason.ACTION
    # Outside CI, the Claude CLI may use the person's own login, as ``review-bot`` does.
    if settings.provider == "claude" and in_ci(environ) and not environ.get(_KEY_VARIABLE):
        return Reason.NO_CREDENTIAL
    # Shadow mode reviews drafts too, for more data (Phase 2 design, Q10).
    if mode is Mode.COMMENT and pr.is_draft:
        return Reason.DRAFT
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


class _Calls:
    """Makes GitHub calls in turn until one fails, and attempts none after it."""

    def __init__(self) -> None:
        self.error: GitHubError | None = None

    def make(self, call: Callable[[], None]) -> Applied:
        """Make ``call`` unless one has failed, and say whether it was done."""
        if self.error is not None:
            return Applied.NOT_ATTEMPTED
        try:
            call()
        except GitHubError as exc:
            self.error = exc
            return Applied.FAILED
        return Applied.DONE


def _apply(shadow: ShadowReport, pr: PullRequestRef, github: GitHubClient) -> ShadowReport:
    """Apply the report's plan to the pull request, then write its summary.

    The calls go in this order, and the first that fails stops the rest of them: one review
    posting every create and move; then, in comment order, each moved comment's old thread
    resolved and each update; then each resolve. A new comment is posted before its old thread
    is resolved, so a failure can't lose it. The summary is written last even after a failure,
    so the run's result stays visible on the pull request.

    Returns:
        The report with each entry's ``applied``, and the first failed call's error, if any, in
        place of the review's.
    """
    calls = _Calls()
    posted = [
        _new_comment(comment)
        for comment in shadow.comments
        if comment.planned.action in _POSTED_ACTIONS
    ]
    review = Applied.DONE
    if posted:
        review = calls.make(functools.partial(github.post_review, pr, posted))
    comments = tuple(
        dataclasses.replace(comment, applied=_apply_comment(comment, review, calls, github))
        for comment in shadow.comments
    )
    resolves = tuple(
        dataclasses.replace(
            resolve,
            applied=calls.make(functools.partial(github.resolve_thread, resolve.comment.thread_id)),
        )
        for resolve in shadow.resolves
    )
    error = calls.error
    try:
        github.write_summary(pr, shadow.summary_body)
    except GitHubError as exc:
        error = error or exc
    return dataclasses.replace(
        shadow, comments=comments, resolves=resolves, error=error or shadow.error
    )


def _apply_comment(
    comment: ShadowComment, review: Applied, calls: _Calls, github: GitHubClient
) -> Applied:
    """Apply a planned comment after the review: ``review`` says whether it posted it."""
    planned = comment.planned
    paired = planned.paired
    if paired is None:  # a create, which only the review posts
        return review
    if planned.action is Action.MOVE:
        resolved = calls.make(functools.partial(github.resolve_thread, paired.thread_id))
        return _both(review, resolved)
    if planned.action is Action.UPDATE:
        return calls.make(functools.partial(github.update_comment, paired.comment_id, comment.body))
    return Applied.DONE  # a keep, which needs no call


def _both(first: Applied, second: Applied) -> Applied:
    """Whether two calls that make one change were both done, both not attempted, or neither."""
    if first is second and first is not Applied.FAILED:
        return first
    return Applied.FAILED


def _new_comment(comment: ShadowComment) -> NewComment:
    finding = comment.planned.reviewed.finding
    return NewComment(finding.file, finding.line, comment.body)
