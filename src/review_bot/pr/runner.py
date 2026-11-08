"""One pull request run: refuse it, or review it and report it, and in comment mode apply it.

A run comes from the pull request's event, or is backfilled once it has merged.

The collaborators come in as arguments, so tests run it on a local clone with a scripted
provider and an in-memory GitHub (Phase 2 design, Data flow).
"""

import asyncio
import dataclasses
import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from review_bot.audit import AuditLog, Stage
from review_bot.context.codeowners import (
    CODEOWNERS_PATHS,
    MAX_CODEOWNERS_BYTES,
    NO_CODEOWNERS,
    CodeOwners,
    CodeOwnersStatus,
    parse_codeowners,
)
from review_bot.diff_parser import ParsedDiff, parse_diff
from review_bot.errors import (
    DiffEmptyError,
    DiffNotParsableError,
    DiffNotUtf8Error,
    DiffTooLargeError,
    GitHubError,
    NotARegularFileError,
    RepoConfigError,
)
from review_bot.git import GitRepo
from review_bot.pr.config import (
    CONFIG_PATH,
    DEFAULT_REPO_CONFIG,
    MAX_CONFIG_BYTES,
    RepoConfig,
    invalid_config,
    parse_repo_config,
    with_repo_threshold,
)
from review_bot.pr.event import PullRequestRef
from review_bot.pr.github import BotComment, GitHubClient, NewComment
from review_bot.pr.incremental import changed_hunks
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.plan import Action
from review_bot.pr.shadow import (
    REPORTED_ERRORS,
    Applied,
    Incremental,
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
    GitHub runs, and the report is ``skipped`` with that reason. Otherwise the repository's
    policy is read from the base commit's ``.review-bot.toml`` (``_repo_config``): if it is
    invalid, the report is ``failed``, ``invalid_config``, with no provider call. Otherwise the
    provider is made, and called at most once. A run that plans comments reads the base
    commit's CODEOWNERS too (``_code_owners``), to name the owners of each finding.

    In comment mode, the run reviews only what changed since the last commit the bot reviewed,
    when it can (``_review``). A ``reviewed`` run plans against the bot's comments on ``pr`` and
    applies the plan (``_apply``); a ``blocked_secret`` or ``failed`` one only writes its
    summary; a ``skipped`` one writes nothing, and reads at most the summary. A call that fails
    stops the rest, and the report holds its error.

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
        GitHubError: If the bot's summary or comments can't be read; nothing is posted.
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
    provider doesn't run, and the report is ``skipped`` with that reason. As in
    ``run_pull_request``, the base commit's ``.review-bot.toml`` sets the policy. The report's
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

    A review runs under the base commit's repository config, which can set its threshold.
    ``github`` is given exactly in comment mode, as ``run_pull_request`` checks.
    """
    started_at = clock()
    audit.record(
        Stage.RUN,
        "pull_request",
        {"repo": pr.repo, "number": pr.number, "base_sha": pr.base_sha, "head_sha": pr.head_sha},
    )
    config: RepoConfig | None = None
    if refusal is not None:
        audit.record(Stage.RUN, "refused", {"reason": refusal})
        review = _Review(refusal)
    else:
        try:
            config = await asyncio.to_thread(_repo_config, git, pr.base_sha)
        except RepoConfigError as exc:
            # Kept, so the summary still records the last commit reviewed, and the next run
            # with a valid config reviews from there.
            recorded = (
                None if github is None else await asyncio.to_thread(_recorded_sha, github, pr)
            )
            review = _Review(exc, recorded)
        else:
            settings = with_repo_threshold(settings, config)
            review = await _review(pr, git, github, make_provider, settings, config.exclude, audit)
    existing: tuple[BotComment, ...] = ()
    codeowners: CodeOwners | None = None
    if review.plans_comments:
        codeowners = await asyncio.to_thread(_code_owners, git, pr.base_sha)
        if github is not None:
            existing = await asyncio.to_thread(github.list_bot_comments, pr)
    shadow = build_shadow_report(
        review.result,
        mode=mode,
        existing=existing,
        settings=settings,
        config=config,
        run_id=audit.run_id,
        started_at=started_at,
        finished_at=clock(),
        origin=origin,
        pr=pr,
        since=review.since,
        incremental=review.incremental,
        codeowners=codeowners,
    )
    # A skip posts and edits nothing (Phase 2 design, Comment state).
    if github is None or shadow.outcome is Outcome.SKIPPED:
        return shadow
    applied = await asyncio.to_thread(_apply, shadow, pr, github, review.recorded)
    return dataclasses.replace(applied, finished_at=clock())


@dataclass(frozen=True, slots=True)
class _Review:
    """A run's review, and in comment mode the commits it started from.

    ``result`` is the review, the error that ended it, or the reason it didn't run or called no
    model. ``recorded`` is the last reviewed commit the bot's summary held, if read, and
    ``incremental`` the diffs of a review of only what changed since it.
    """

    result: PipelineResult | ReportedError | Reason
    recorded: str | None = None
    incremental: Incremental | None = None

    @property
    def since(self) -> str | None:
        """The recorded commit if the run started from it: the head, or an ancestor of it."""
        if self.incremental is not None or self.result is Reason.ALREADY_REVIEWED:
            return self.recorded
        return None

    @property
    def plans_comments(self) -> bool:
        """Whether the review reached the model, or found nothing new to send it."""
        result = self.result
        if isinstance(result, PipelineResult):
            return result.model_called
        return result is Reason.NO_NEW_CHANGES


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


def _repo_config(git: GitRepo, base_sha: str) -> RepoConfig:
    """The policy in ``.review-bot.toml`` at the base commit, or the defaults if it has none.

    Only the base commit's file counts, so a pull request can't change the rules it is reviewed
    under (ADR 0008).

    Raises:
        RepoConfigError: If the file is invalid, or the path holds something else.
        GitError: If the commit isn't in the clone, or git fails.
    """
    try:
        data = git.read_file(base_sha, CONFIG_PATH, max_bytes=MAX_CONFIG_BYTES)
    except NotARegularFileError as exc:
        raise invalid_config(base_sha, f"it is a {exc.kind}, not a regular file") from exc
    if data is None:
        return DEFAULT_REPO_CONFIG
    return parse_repo_config(data, base_sha=base_sha)


def _code_owners(git: GitRepo, base_sha: str) -> CodeOwners:
    """The CODEOWNERS file at the base commit: the first of GitHub's paths holding a regular file.

    Only the base commit's file counts, as for ``.review-bot.toml`` (ADR 0008). A directory or
    symbolic link at a path is passed over; if that is all there is, the result is
    ``not_a_file``, naming the first. A file that can't be used names no owner, and the run
    goes on.

    Raises:
        GitError: If the commit isn't in the clone, or git fails.
    """
    first_non_file: str | None = None
    for path in CODEOWNERS_PATHS:
        source = f"{path}@{base_sha}"
        try:
            data = git.read_file(base_sha, path, max_bytes=MAX_CODEOWNERS_BYTES)
        except NotARegularFileError:
            first_non_file = first_non_file or source
            continue
        if data is not None:
            return parse_codeowners(data, source=source)
    if first_non_file is not None:
        return CodeOwners(source=first_non_file, status=CodeOwnersStatus.NOT_A_FILE)
    return NO_CODEOWNERS


def _missing_commit(pr: PullRequestRef, git: GitRepo) -> Reason | None:
    """``missing_commit`` if the clone lacks the base or head commit, else None."""
    if git.has_commit(pr.base_sha) and git.has_commit(pr.head_sha):
        return None
    return Reason.MISSING_COMMIT


async def _review(  # noqa: PLR0913, PLR0917 - the run's inputs, passed through
    pr: PullRequestRef,
    git: GitRepo,
    github: GitHubClient | None,
    make_provider: Callable[[], LLMProvider],
    settings: Settings,
    exclude: Sequence[str],
    audit: AuditLog,
) -> _Review:
    """Diff the pull request and review it: all of it, or only what changed since last time.

    Files matching an ``exclude`` glob are skipped, as the built-in skips are.

    The PR diff comes first, so a diff that can't be reviewed, or whose files are all skipped,
    is reported with no GitHub call. Otherwise, in comment mode, the bot's summary says the
    last commit it reviewed: if that is the head, the run is refused ``already_reviewed``; if
    it is an ancestor of the head, only the PR diff's hunks changed since then are reviewed.
    Otherwise, after a force-push, in a clone without that commit, or when the diff since then
    can't be used, the whole pull request is reviewed.
    """
    name = f"{pr.base_sha}...{pr.head_sha}"
    max_bytes = settings.max_diff_bytes
    try:
        diff = await asyncio.to_thread(git.diff, pr.base_sha, pr.head_sha, max_bytes=max_bytes)
    except REPORTED_ERRORS as exc:
        return _Review(exc)
    parsed = _reviewable(diff, exclude)
    review_part = functools.partial(
        _review_diff, make_provider=make_provider, settings=settings, exclude=exclude, audit=audit
    )
    if github is None or parsed is None:
        return _Review(await review_part(diff, name))
    recorded = await asyncio.to_thread(_recorded_sha, github, pr)
    if recorded == pr.head_sha:
        audit.record(Stage.RUN, "refused", {"reason": Reason.ALREADY_REVIEWED})
        return _Review(Reason.ALREADY_REVIEWED, recorded)
    changed = None
    if recorded is not None and await asyncio.to_thread(git.is_ancestor, recorded, pr.head_sha):
        changed = await asyncio.to_thread(_diff_since, git, recorded, pr.head_sha, max_bytes)
    if changed is None:
        return _Review(await review_part(diff, name), recorded)
    incremental = Incremental(pr_diff=parsed, changed=changed)
    hunks = changed_hunks(diff, parsed, changed)
    if not hunks:
        return _Review(Reason.NO_NEW_CHANGES, recorded, incremental)
    name = f"{name}, its hunks changed since {recorded}"
    result = await review_part(hunks, name)
    return _Review(result, recorded, incremental)


async def _review_diff(  # noqa: PLR0913 - the run's inputs, keyword-only
    diff: str,
    name: str,
    *,
    make_provider: Callable[[], LLMProvider],
    settings: Settings,
    exclude: Sequence[str],
    audit: AuditLog,
) -> PipelineResult | ReportedError:
    """Review ``diff``, skipping files matching ``exclude``.

    An error that still ends in a report is returned.
    """
    try:
        return await review_diff(diff, settings, make_provider(), audit, name=name, exclude=exclude)
    except REPORTED_ERRORS as exc:
        return exc


def _reviewable(diff: str, exclude: Sequence[str]) -> ParsedDiff | None:
    """``diff`` parsed, if it parses and holds a file the review doesn't skip; else None.

    A file matching ``exclude`` is skipped, as ``review_diff`` skips it.

    With None, any part of it has nothing to review too, so the summary needn't be read.
    """
    try:
        parsed = parse_diff(diff, exclude=exclude)
    except DiffNotParsableError:
        return None
    if all(file.skip_reason is not None for file in parsed.files):
        return None
    return parsed


def _recorded_sha(github: GitHubClient, pr: PullRequestRef) -> str | None:
    """The last commit the bot's summary on ``pr`` says it reviewed, or None."""
    summary = github.read_summary(pr)
    return None if summary is None else summary.reviewed_sha


def _diff_since(git: GitRepo, since: str, head: str, max_bytes: int) -> ParsedDiff | None:
    """The diff from ``since``, an ancestor of ``head``, to ``head``; no files if it is empty.

    None if it is over ``max_bytes``, isn't UTF-8 or doesn't parse, as after merging a large
    base: the whole pull request is reviewed instead, never skipped for this diff alone. With
    ``since`` an ancestor, its merge base with ``head`` is ``since`` itself, so git's three-dot
    diff is the plain one between them.
    """
    try:
        return parse_diff(git.diff(since, head, max_bytes=max_bytes))
    except DiffEmptyError:
        return ParsedDiff(files=(), reviewable_text="", decoded_paths=())
    except (DiffTooLargeError, DiffNotUtf8Error, DiffNotParsableError):
        return None


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


def _apply(
    shadow: ShadowReport, pr: PullRequestRef, github: GitHubClient, recorded: str | None
) -> ShadowReport:
    """Apply the report's plan to the pull request, then write its summary.

    The calls go in this order, and the first that fails stops the rest of them: one review
    posting every create and move; then, in comment order, each moved comment's old thread
    resolved and each update; then each resolve. A new comment is posted before its old thread
    is resolved, so a failure can't lose it. The summary is written last even after a failure,
    so the run's result stays visible on the pull request. Its marker records the head as
    reviewed only if the run is ``reviewed`` and every call before it was done; otherwise it
    keeps ``recorded``, the commit the summary held, so the next run reviews from there again.

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
    is_reviewed = shadow.outcome is Outcome.REVIEWED and error is None
    try:
        github.write_summary(
            pr, shadow.summary_body, reviewed_sha=pr.head_sha if is_reviewed else recorded
        )
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
