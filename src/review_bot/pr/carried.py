"""What a gated comment-mode run carries from its earlier runs, and the PR diff's blockers.

Pure (Phase 4 design, Merge gating; ADR 0019). A run reads the bot's summary and inline comments
first (``read_prior``). Its marker is trusted only if nobody but the bot last edited any of the
bot's issue comments, and it is well formed without ``tampered``; then its ``ai:`` entries
carry, or every entry when the whole diff couldn't be checked (``carried_blockers``). Otherwise
nothing is read from it, and the ``ai:`` blockers are rebuilt from the bot's own inline
comments. The rule blockers the whole diff holds
take the fingerprints of the bot's comments they pair with (``check_pr_diff``).
"""

import dataclasses
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from review_bot.diff_parser import ParsedDiff
from review_bot.findings import Severity
from review_bot.governance.gate import (
    Blocker,
    GateClass,
    WholeDiffChecks,
    check_whole_diff,
    is_ai_class,
)
from review_bot.pr.comments import (
    MarkerEntry,
    SummaryMarker,
    body_before_gate,
    is_summary,
    read_summary_marker,
)
from review_bot.pr.github import BotComment, BotIssueComment, Resolver, is_edited_by_others
from review_bot.pr.plan import pair_fingerprints
from review_bot.rules import DomainRule

__all__ = [
    "FRESH",
    "Prior",
    "carried_blockers",
    "check_pr_diff",
    "read_prior",
    "standing_comments",
]


@dataclass(frozen=True, slots=True)
class Prior:
    """What a gated run read from the pull request before its review.

    ``marker`` is the bot's summary's marker if it is trusted, and ``body`` that summary
    without its marker or gate section; both are None otherwise. ``rebuild`` says the carried
    ``ai:`` blockers are rebuilt from the bot's inline comments instead: the summary isn't
    trusted, or there is none while the bot has inline comments on the pull request. ``edited``
    says one of the bot's inline comments was last edited by someone else.
    """

    marker: SummaryMarker | None = None
    body: str | None = None
    rebuild: bool = False
    edited: bool = False

    @property
    def reviewed_sha(self) -> str | None:
        """The last commit the trusted marker says the bot reviewed, or None."""
        return None if self.marker is None else self.marker.reviewed_sha

    def is_tampered(self, *, checks_ran: bool) -> bool:
        """Whether the run must stay ``tampered``.

        It must if an inline comment was edited by someone else, or if a rebuild is needed but
        can't complete because the whole diff couldn't be checked.
        """
        return self.edited or (self.rebuild and not checks_ran)


# A pull request the bot never commented on, or a shadow mode run, which reads nothing.
FRESH: Final = Prior()


def read_prior(
    issue_comments: Sequence[BotIssueComment], editors: Mapping[int, str | None]
) -> Prior:
    """What the bot's ``issue_comments`` and inline comments' ``editors`` say for the run.

    ``issue_comments`` are oldest first, and its summary is the oldest whose last line is a
    summary marker, as ``GitHubClient.read_summary`` finds it. ``editors`` maps each inline
    comment the bot wrote to its last editor. The marker is trusted only if none of the bot's
    issue comments was last edited by someone else, so a summary whose marker was edited away
    is a tamper too, not a deletion.
    """
    edited = any(is_edited_by_others(editor) for editor in editors.values())
    summary = next((comment for comment in issue_comments if is_summary(comment.body)), None)
    issue_edited = any(is_edited_by_others(comment.last_editor) for comment in issue_comments)
    if summary is not None and not issue_edited:
        marker = read_summary_marker(summary.body)
        if marker is not None and not marker.tampered:
            return Prior(marker, body_before_gate(summary.body), edited=edited)
    rebuild = summary is not None or issue_edited or bool(editors)
    return Prior(rebuild=rebuild, edited=edited)


def check_pr_diff(
    parsed: ParsedDiff,
    existing: Sequence[BotComment],
    *,
    domain_rules: Sequence[DomainRule],
    threshold: Severity,
) -> WholeDiffChecks:
    """The whole PR diff's rule findings and secrets, each rule finding paired with a comment.

    A rule finding takes the fingerprint of the bot's comment it pairs with, as the comment plan
    pairs them (``plan.pair_fingerprints``), so an override on that thread covers it.
    """
    checks = check_whole_diff(parsed, domain_rules=domain_rules, threshold=threshold)
    paired = pair_fingerprints(checks.rule_findings, existing, pr_diff=parsed)
    return dataclasses.replace(checks, rule_findings=paired)


def standing_comments(
    existing: Sequence[BotComment], resolved: Collection[int]
) -> tuple[BotComment, ...]:
    """The bot's comments still standing: open or person-resolved, and not in ``resolved``.

    ``resolved`` holds the IDs of the comments this run resolves, as the bot.
    """
    return tuple(
        comment
        for comment in existing
        if comment.resolved_by is not Resolver.BOT and comment.comment_id not in resolved
    )


def carried_blockers(
    prior: Prior,
    *,
    classes: Sequence[GateClass],
    standing: Sequence[BotComment],
    resolved_now: Collection[str],
    checks_ran: bool,
) -> tuple[Blocker, ...]:
    """The blockers ``prior`` carries into this run, under the enabled ``classes``.

    ``standing`` are the bot's inline comments not resolved by the bot, before or in this run
    (``standing_comments``), and ``resolved_now`` the fingerprints of those this run resolves
    because their anchored lines are gone. With a trusted marker, each listed ``ai:`` entry
    carries, or every entry if ``checks_ran`` is false, unless this run resolves its comment
    (P4-S5 AC8). An entry listed after its comment was resolved, such as a withheld finding
    sharing an old comment's fingerprint, still carries. With a rebuild, each standing comment
    of an enabled ``ai:`` class whose shown severity and confidence meet the class's minimums
    is a blocker (AC9). Otherwise nothing carries.
    """
    enabled = {gate_class.rule_part: gate_class for gate_class in classes}
    if prior.marker is not None:
        entries = (
            entry
            for entry in prior.marker.entries
            if entry.gate_class in enabled and (entry.is_ai or not checks_ran)
        )
        return tuple(
            blocker
            for entry in entries
            if (blocker := _listed_blocker(entry, standing, resolved_now)) is not None
        )
    if prior.rebuild:
        return tuple(
            _comment_blocker(comment, comment.rule_part)
            for comment in standing
            if _meets_ai_class(comment, enabled)
        )
    return ()


def _listed_blocker(
    entry: MarkerEntry, standing: Sequence[BotComment], resolved_now: Collection[str]
) -> Blocker | None:
    """The blocker a marker entry carries, placed on its standing comment if it has one.

    None if this run resolves its comment. With no standing comment, it was withheld or its
    comment is gone, so it carries with no place.
    """
    own = next((comment for comment in standing if comment.fingerprint == entry.fingerprint), None)
    if own is not None:
        return _comment_blocker(own, entry.gate_class)
    if entry.fingerprint in resolved_now:
        return None
    return Blocker(entry.fingerprint, entry.gate_class, None, None, None, carried=True)


def _meets_ai_class(comment: BotComment, enabled: Mapping[str, GateClass]) -> bool:
    gate_class = enabled.get(comment.rule_part)
    return (
        is_ai_class(comment.rule_part)
        and gate_class is not None
        and comment.confidence is not None
        and gate_class.admits(comment.severity, comment.confidence)
    )


def _comment_blocker(comment: BotComment, gate_class: str) -> Blocker:
    return Blocker(
        comment.fingerprint,
        gate_class,
        comment.path,
        comment.line,
        comment.severity,
        carried=True,
    )
