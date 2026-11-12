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
from review_bot.console import write_diagnostic
from review_bot.context.codeowners import (
    CODEOWNERS_PATHS,
    MAX_CODEOWNERS_BYTES,
    NO_CODEOWNERS,
    CodeOwners,
    CodeOwnersStatus,
    parse_codeowners,
)
from review_bot.context.issues import IssueStatus, LinkedIssue, linked_issue, read_linked_issues
from review_bot.context.tools import RepoRef
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
from review_bot.findings import Severity
from review_bot.git import GitRepo
from review_bot.governance.gate import (
    NOTHING_FOUND,
    GateClass,
    GateReason,
    WholeDiffChecks,
    decide_gate,
)
from review_bot.governance.policy import Policy
from review_bot.governance.redaction import RedactionKind
from review_bot.pr.carried import (
    FRESH,
    Prior,
    carried_blockers,
    check_pr_diff,
    read_prior,
    standing_comments,
)
from review_bot.pr.config import (
    CONFIG_PATH,
    DEFAULT_REPO_CONFIG,
    MAX_CONFIG_BYTES,
    RepoConfig,
    gate_classes,
    invalid_config,
    parse_repo_config,
    with_repo_threshold,
)
from review_bot.pr.event import PullRequestRef
from review_bot.pr.github import BotComment, GitHubClient, IssueReader, NewComment
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
    with_gate,
)
from review_bot.providers.base import LLMProvider
from review_bot.report import PipelineResult
from review_bot.review import ReviewContext, review_diff
from review_bot.settings import Settings, in_ci

__all__ = ["backfill_pull_request", "record_gate", "run_pull_request"]

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
    issue_reader: IssueReader | None,
    settings: Settings,
    policy: Policy,
    audit: AuditLog,
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
    gating_on: bool = False,
) -> ShadowReport:
    """Review ``pr``'s own changes, from its merge base to its head, and report the run.

    A fork, an action that changes nothing to review, a Claude run in CI with no
    ``ANTHROPIC_API_KEY``, or in comment mode a draft, is refused: neither git, the provider nor
    GitHub runs, and the report is ``skipped`` with that reason. Otherwise the repository's
    policy is read from the base commit's ``.review-bot.toml`` (``_repo_config``): if it is
    invalid, the report is ``failed``, ``invalid_config``, with no provider call. Otherwise the
    provider is made, and called at most once. With ``settings.context`` ``repo``, the model's
    tools read the head commit through git objects, never a checkout, and the issues the pull
    request closes are read with ``issue_reader`` (``_LinkedIssues``), only once the review will
    call the model. A run that plans comments reads the base commit's CODEOWNERS too
    (``_code_owners``), to name the owners of each finding.

    In comment mode, the run reviews only what changed since the last commit the bot reviewed,
    when it can (``_review``). A ``reviewed`` run plans against the bot's comments on ``pr`` and
    applies the plan (``_apply``); a ``blocked_secret`` or ``failed`` one only writes its
    summary; a ``skipped`` one writes nothing, and reads at most the summary. A call that fails
    stops the rest, and the report holds its error.

    A repository whose config names ``gate`` classes, each one ``policy`` approves, has its
    merge gate decided (``_gated_report``; P4-S5). In comment mode, such a run first reads the
    bot's comments, its summary and who last edited each (``_read_gated``), and every such run
    writes its summary, skips included. A run that read no valid config, while gating may be on
    (``gating_on``, or a summary marker with a gate field), writes no summary (AC11).

    Args:
        pr: The pull request, from its event.
        mode: Whether to post on the pull request, or only report.
        git: The clone holding its base and head commits.
        make_provider: Makes the provider, only once the run isn't refused.
        github: The GitHub client in comment mode; None in shadow mode.
        issue_reader: Reads a context review's linked issues; None without a token, so each
            is ``no_token``.
        settings: The run's settings, within ``policy``.
        policy: The policy the run is under, which the review and the report follow.
        audit: Where the run's records go; its end is the caller's to record.
        environ: The environment, read for ``CI`` and ``ANTHROPIC_API_KEY``.
        clock: Returns the current time, timezone-aware.
        gating_on: Whether ``REVIEW_BOT_GATE`` is ``on``, as the workflow says gating is.

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
        issue_reader=issue_reader,
        settings=settings,
        policy=policy,
        audit=audit,
        clock=clock,
        gating_on=gating_on,
    )


async def backfill_pull_request(  # noqa: PLR0913 - keyword-only collaborators, as in run_pull_request
    pr: PullRequestRef,
    *,
    git: GitRepo,
    make_provider: Callable[[], LLMProvider],
    issue_reader: IssueReader | None,
    settings: Settings,
    policy: Policy,
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
        issue_reader: Reads a context review's linked issues; None without a token, so each
            is ``no_token``.
        settings: The run's settings, within ``policy``.
        policy: The policy the run is under, which the review and the report follow.
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
        issue_reader=issue_reader,
        settings=settings,
        policy=policy,
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
    issue_reader: IssueReader | None,
    settings: Settings,
    policy: Policy,
    audit: AuditLog,
    clock: Callable[[], datetime],
    gating_on: bool = False,
) -> ShadowReport:
    """Review the pull request unless ``refusal`` says why not, then report and apply it.

    A review runs under the base commit's repository config, which can set its threshold and
    the classes that gate the merge. ``github`` is given exactly in comment mode, as
    ``run_pull_request`` checks.
    """
    started_at = clock()
    audit.record(
        Stage.RUN,
        "pull_request",
        {"repo": pr.repo, "number": pr.number, "base_sha": pr.base_sha, "head_sha": pr.head_sha},
    )
    config: RepoConfig | None = None
    issues: _LinkedIssues | None = None
    gated: _Gated | None = None
    gate_may_be_on = gating_on
    if refusal is not None:
        audit.record(Stage.RUN, "refused", {"reason": refusal})
        review = _Review(refusal)
    else:
        try:
            config = await asyncio.to_thread(_repo_config, git, pr.base_sha)
            classes = gate_classes(config, policy, base_sha=pr.base_sha)
        except RepoConfigError as exc:
            # A file that parsed but gates on a class it can't is no valid config either.
            config = None
            # Kept, so the summary still records the last commit reviewed, and the next run
            # with a valid config reviews from there.
            summary = None if github is None else await asyncio.to_thread(github.read_summary, pr)
            review = _Review(exc, None if summary is None else summary.reviewed_sha)
            gate_may_be_on = gating_on or (summary is not None and summary.holds_gate_state)
        else:
            settings = with_repo_threshold(settings, config)
            if classes:
                gated = await asyncio.to_thread(_read_gated, classes, github, pr)
            context = None
            if settings.context != "off":
                issues = _LinkedIssues(pr, issue_reader)
                context = _review_context(pr, git, issues)
            review = await _review(
                pr, git, github, make_provider, settings, policy, config, context, audit, gated
            )
    existing: tuple[BotComment, ...] = ()
    codeowners: CodeOwners | None = None
    if review.plans_comments:
        codeowners = await asyncio.to_thread(_code_owners, git, pr.base_sha)
        if gated is not None:
            existing = gated.existing
        elif github is not None:
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
        policy=policy,
        since=review.since,
        incremental=review.incremental,
        codeowners=codeowners,
        issues=() if issues is None else issues.read,
    )
    if gated is not None:
        shadow = _gated_report(shadow, gated, review.whole_diff)
    record_gate(audit, shadow)
    if github is None or not _writes_summary(shadow, gate_may_be_on=gate_may_be_on):
        return shadow
    recorded = review.recorded if gated is None else gated.prior.reviewed_sha
    applied = await asyncio.to_thread(_apply, shadow, pr, github, recorded)
    return dataclasses.replace(applied, finished_at=clock())


@dataclass(frozen=True, slots=True)
class _Gated:
    """A gated run's classes, and in comment mode what it read from the PR before its review.

    ``existing`` are the bot's inline comments, and ``prior`` what its summary and comments
    say the run carries; a shadow mode run reads nothing, so it carries nothing.
    """

    classes: tuple[GateClass, ...]
    prior: Prior = FRESH
    existing: tuple[BotComment, ...] = ()


def _read_gated(
    classes: tuple[GateClass, ...], github: GitHubClient | None, pr: PullRequestRef
) -> _Gated:
    """What a gated run reads from ``pr`` before its review: nothing in shadow mode.

    Raises:
        GitHubError: If a read fails.
    """
    if github is None:
        return _Gated(classes)
    existing = github.list_bot_comments(pr)
    prior = read_prior(github.read_summary_state(pr), github.list_bot_comment_editors(pr))
    return _Gated(classes, prior, existing)


def _gated_report(
    shadow: ShadowReport, gated: _Gated, whole_diff: WholeDiffChecks | None
) -> ShadowReport:
    """``shadow`` with its merge gate decided, and the decision closing its summary.

    The model's part counts as judged if the run reviewed, or had already reviewed the head.
    The blockers carried from earlier runs follow ``gated.prior``, with the comments this run
    resolves counted as the bot's resolved (``carried_blockers``). A run that had already
    reviewed the head keeps its earlier summary's body, with only the gate section new.
    """
    resolved = {resolve.comment.comment_id for resolve in shadow.resolves}
    standing = standing_comments(gated.existing, resolved)
    checks_ran = whole_diff is not None
    carried = carried_blockers(
        gated.prior,
        classes=gated.classes,
        existing=gated.existing,
        standing=standing,
        checks_ran=checks_ran,
    )
    reported = [
        *(comment.planned.reviewed for comment in shadow.comments),
        *(item.reviewed for item in shadow.withheld),
    ]
    already_reviewed = shadow.reason is Reason.ALREADY_REVIEWED
    gate = decide_gate(
        reported,
        whole_diff=whole_diff,
        carried=carried,
        enabled=gated.classes,
        judged=shadow.outcome is Outcome.REVIEWED or already_reviewed,
        tampered=gated.prior.is_tampered(checks_ran=checks_ran),
        rebuilt=gated.prior.rebuild,
    )
    threaded = {
        *(comment.planned.reviewed.fingerprint for comment in shadow.comments),
        *(comment.fingerprint for comment in standing),
    }
    kept_body = gated.prior.body if already_reviewed else None
    return with_gate(shadow, gate, threaded=threaded, kept_body=kept_body)


def _writes_summary(shadow: ShadowReport, *, gate_may_be_on: bool) -> bool:
    """Whether a comment mode run writes its summary (P4-S5 AC11).

    A gated run always does. Otherwise a skip posts and edits nothing (Phase 2 design, Comment
    state), and a run that read no valid config writes only while gating can't be on, so it
    can't clear a gated summary's state.
    """
    if shadow.gate.enabled:
        return True
    if shadow.outcome is Outcome.SKIPPED:
        return False
    return not (shadow.gate.reason is GateReason.NO_CONFIG and gate_may_be_on)


def record_gate(audit: AuditLog, shadow: ShadowReport) -> None:
    """Record the run's gate decision as one ``policy``/``gate`` record (P4-S5 AC12).

    Raises:
        AuditError: If the record can't be written.
    """
    gate = shadow.gate
    audit.record(
        Stage.POLICY,
        "gate",
        {
            "enabled": gate.enabled,
            "decision": gate.decision,
            "reason": gate.reason,
            "classes": list(gate.classes),
            "fingerprints": [blocker.fingerprint for blocker in gate.blocking],
            "policy_version": shadow.policy.version,
        },
    )


@dataclass(frozen=True, slots=True)
class _Review:
    """A run's review, and in comment mode the commits it started from.

    ``result`` is the review, the error that ended it, or the reason it didn't run or called no
    model. ``recorded`` is the last reviewed commit the bot's summary held, if read, and
    ``incremental`` the diffs of a review of only what changed since it. ``whole_diff`` is what
    a gated run's checks found over the whole PR diff; None if they couldn't run, or the run
    isn't gated.
    """

    result: PipelineResult | ReportedError | Reason
    recorded: str | None = None
    incremental: Incremental | None = None
    whole_diff: WholeDiffChecks | None = None

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
    policy: Policy,
    config: RepoConfig,
    context: ReviewContext | None,
    audit: AuditLog,
    gated: _Gated | None,
) -> _Review:
    """Diff the pull request and review it: all of it, or only what changed since last time.

    The review runs under ``config``: files matching an ``exclude`` glob are skipped, as the
    built-in skips are, and its domain rules run with the built-in rules. ``context`` is what
    the model may look up; None reviews in diff mode.

    The PR diff comes first, so a diff that can't be reviewed, or whose files are all skipped,
    is reported with no GitHub call. Otherwise, in comment mode, the bot's summary says the
    last commit it reviewed: if that is the head, the run is refused ``already_reviewed``; if
    it is an ancestor of the head, only the PR diff's hunks changed since then are reviewed.
    Otherwise, after a force-push, in a clone without that commit, or when the diff since then
    can't be used, the whole pull request is reviewed.

    A ``gated`` run takes the last reviewed commit from its trusted marker instead, so after a
    tamper it reviews the whole pull request; and it checks the whole PR diff for rule
    findings and secrets (``_whole_diff_checks``), whatever the review then does. An empty diff
    adds nothing, so its checks find nothing; one that can't be read or parsed can't be checked.
    """
    name = f"{pr.base_sha}...{pr.head_sha}"
    max_bytes = settings.max_diff_bytes
    try:
        diff = await asyncio.to_thread(git.diff, pr.base_sha, pr.head_sha, max_bytes=max_bytes)
    except REPORTED_ERRORS as exc:
        return _Review(exc, whole_diff=NOTHING_FOUND if isinstance(exc, DiffEmptyError) else None)
    whole_diff = None
    if gated is not None:
        whole_diff = _whole_diff_checks(diff, config, settings.threshold, gated.existing)
    parsed = _reviewable(diff, config.exclude)
    review_part = functools.partial(
        _review_diff,
        make_provider=make_provider,
        settings=settings,
        policy=policy,
        config=config,
        context=context,
        audit=audit,
    )
    if github is None or parsed is None:
        return _Review(await review_part(diff, name), whole_diff=whole_diff)
    if gated is None:
        recorded = await asyncio.to_thread(_recorded_sha, github, pr)
    else:
        recorded = gated.prior.reviewed_sha
    if recorded == pr.head_sha:
        audit.record(Stage.RUN, "refused", {"reason": Reason.ALREADY_REVIEWED})
        return _Review(Reason.ALREADY_REVIEWED, recorded, whole_diff=whole_diff)
    changed = None
    if recorded is not None and await asyncio.to_thread(git.is_ancestor, recorded, pr.head_sha):
        changed = await asyncio.to_thread(_diff_since, git, recorded, pr.head_sha, max_bytes)
    if changed is None:
        return _Review(await review_part(diff, name), recorded, whole_diff=whole_diff)
    incremental = Incremental(pr_diff=parsed, changed=changed)
    hunks = changed_hunks(diff, parsed, changed)
    if not hunks:
        return _Review(Reason.NO_NEW_CHANGES, recorded, incremental, whole_diff)
    name = f"{name}, its hunks changed since {recorded}"
    result = await review_part(hunks, name)
    return _Review(result, recorded, incremental, whole_diff)


def _whole_diff_checks(
    diff: str, config: RepoConfig, threshold: Severity, existing: Sequence[BotComment]
) -> WholeDiffChecks | None:
    """The gate's checks over ``diff``, the whole PR diff, under ``config``; None if unparsable.

    Rule findings pair with the bot's ``existing`` comments (``carried.check_pr_diff``).
    """
    try:
        parsed = parse_diff(diff, exclude=config.exclude)
    except DiffNotParsableError:
        return None
    return check_pr_diff(parsed, existing, domain_rules=config.domain_rules, threshold=threshold)


class _LinkedIssues:
    """Reads the issues a pull request closes when the review asks, and keeps what it read.

    The review asks only once it will call the model, so ``read`` stays empty in a run that
    never gets there.
    """

    def __init__(self, pr: PullRequestRef, issue_reader: IssueReader | None) -> None:
        self._pr = pr
        self._issue_reader = issue_reader
        self.read: tuple[LinkedIssue, ...] = ()

    def __call__(self, kinds: frozenset[RedactionKind]) -> tuple[LinkedIssue, ...]:
        """Each issue the pull request closes, read and made safe to send, or why it isn't sent.

        The ``kinds`` of personal data are masked in what is sent (``linked_issue``).

        As many are read as ``read_linked_issues`` allows. With no ``issue_reader`` each is
        ``no_token``. An issue whose read fails is ``unavailable``, with a line on stderr saying
        why; the review goes on without it.
        """
        read_one: Callable[[int], LinkedIssue]
        if self._issue_reader is None:
            read_one = functools.partial(LinkedIssue, status=IssueStatus.NO_TOKEN)
        else:
            read_one = functools.partial(
                _read_linked_issue, self._pr.repo, self._issue_reader, kinds=kinds
            )
        self.read = read_linked_issues(self._pr.linked_issues, read_one)
        return self.read


def _review_context(pr: PullRequestRef, git: GitRepo, issues: _LinkedIssues) -> ReviewContext:
    """What the model may look up in context mode: the head commit, and the issues ``pr`` closes.

    The tools read the head, which the diff ends at, through git objects only, never a
    checkout, so no filter, hook or attribute driver from the pull request runs (ADR 0013). The
    review reads the issues through ``issues``.
    """
    return ReviewContext(
        repo=RepoRef(git=git, commit=pr.head_sha), is_diff_head=True, read_issues=issues
    )


def _read_linked_issue(
    repo: str, issue_reader: IssueReader, number: int, *, kinds: frozenset[RedactionKind]
) -> LinkedIssue:
    try:
        issue = issue_reader.read_issue(repo, number)
    except GitHubError as exc:
        write_diagnostic(
            f"review-bot pr: linked issue #{number} wasn't read, so the review goes on without "
            f"it: {exc}"
        )
        return LinkedIssue(number, IssueStatus.UNAVAILABLE)
    return linked_issue(number, issue, kinds)


async def _review_diff(  # noqa: PLR0913 - the run's inputs, keyword-only
    diff: str,
    name: str,
    *,
    make_provider: Callable[[], LLMProvider],
    settings: Settings,
    policy: Policy,
    config: RepoConfig,
    context: ReviewContext | None,
    audit: AuditLog,
) -> PipelineResult | ReportedError:
    """Review ``diff`` under ``config``, its exclusions and its domain rules, and ``policy``.

    ``context`` is the commit the model's tools read; None reviews in diff mode. An error that
    still ends in a report is returned.
    """
    try:
        return await review_diff(
            diff,
            settings,
            make_provider(),
            audit,
            name=name,
            exclude=config.exclude,
            context=context,
            domain_rules=config.domain_rules,
            policy=policy,
        )
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
    It records the run's gate too, if enabled.

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
            pr,
            shadow.summary_body,
            reviewed_sha=pr.head_sha if is_reviewed else recorded,
            gate=shadow.gate,
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
