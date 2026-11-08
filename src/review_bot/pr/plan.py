"""Decide what to do with each reported finding and each of the bot's comments on the PR.

Pure and deterministic: the model's findings never post anything themselves. Each finding pairs
with at most one of the bot's comments, by key rather than by fingerprint ordinal, and a comment
nobody found again is resolved only when its anchored line is gone (Phase 2 design, Comment
state; ADR 0009).
"""

import dataclasses
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from review_bot.diff_parser import ParsedDiff
from review_bot.findings import ReviewedFinding
from review_bot.fingerprint import Fingerprinter, anchor_for
from review_bot.pr.github import BotComment, Resolver
from review_bot.report import has_lone_surrogate
from review_bot.secret_scan import scan_secrets

__all__ = [
    "DEFAULT_MAX_OPEN_COMMENTS",
    "Action",
    "CommentPlan",
    "PlannedComment",
    "WithheldFinding",
    "WithholdReason",
    "plan_comments",
]

DEFAULT_MAX_OPEN_COMMENTS: Final = 10

# A comment's key: its path, rule part and anchor.
type _Key = tuple[str, str, str]
# Each path's added and context lines' text, by line number.
type _Lines = Mapping[str, Mapping[int, str]]


class Action(StrEnum):
    """What the bot does with a comment."""

    CREATE = "create"
    KEEP = "keep"
    UPDATE = "update"
    MOVE = "move"
    RESOLVE = "resolve"


class WithholdReason(StrEnum):
    """Why a reported finding gets no comment."""

    OVER_LIMIT = "over_limit"
    POSSIBLE_SECRET = "possible_secret"  # noqa: S105 - a reason's name, not a secret
    NOT_COMMENTABLE = "not_commentable"


@dataclass(frozen=True, slots=True)
class PlannedComment:
    """What the bot does about one finding's comment: create, keep, update or move it.

    ``paired`` is the bot's comment the finding paired with, None exactly for a create. A
    paired comment keeps its posted fingerprint, so ``reviewed`` carries that fingerprint, and
    a body rendered from it keeps it too; a create carries a fingerprint no comment holds yet.
    ``anchor`` is the first 16 hex digits of the finding's line's hash (``anchor_for``).

    Raises:
        ValueError: If ``paired`` is given for a create, or missing for any other action.
    """

    action: Action
    reviewed: ReviewedFinding
    anchor: str
    paired: BotComment | None = None

    def __post_init__(self) -> None:
        """Check that exactly a create has no paired comment."""
        if (self.paired is None) != (self.action is Action.CREATE):
            raise ValueError("A planned comment has a paired comment exactly when it isn't new.")


@dataclass(frozen=True, slots=True)
class WithheldFinding:
    """A reported finding that gets no comment, and why."""

    reviewed: ReviewedFinding
    reason: WithholdReason


@dataclass(frozen=True, slots=True)
class CommentPlan:
    """The planned comments and withheld findings, each in comment order, and the resolves.

    ``resolved`` are the bot's open comments to resolve, oldest first.
    """

    comments: tuple[PlannedComment, ...] = ()
    withheld: tuple[WithheldFinding, ...] = ()
    resolved: tuple[BotComment, ...] = ()


@dataclass(frozen=True, slots=True)
class _Pairable:
    """A finding that may pair: its line is in the PR diff and its rationale holds no secret."""

    reviewed: ReviewedFinding
    anchor_text: str
    anchor: str

    @property
    def key(self) -> _Key:
        return (self.reviewed.finding.file, self.reviewed.rule_part, self.anchor)


def plan_comments(
    findings: Sequence[ReviewedFinding],
    existing: Sequence[BotComment],
    *,
    pr_diff: ParsedDiff,
    max_open_comments: int,
) -> CommentPlan:
    """Plan each finding's comment against the bot's ``existing`` comments.

    Comment order is severity, highest first, then file, then line. A finding is withheld
    ``not_commentable`` if its line isn't an added or context line of ``pr_diff``, or its path
    isn't valid UTF-8, which GitHub can't address; and ``possible_secret`` if ``scan_secrets``
    finds one in its rationale. Neither pairs. The rationale is scanned rather than the rendered
    body, because Markdown escaping splits a token such as ``ghp_...`` so that the scan would
    miss it in the body, though GitHub would show it whole.

    The other findings pair with the bot's comments (``_pair``). A paired comment a person
    resolved is kept as it is. An open one is kept, or updated if its severity changed, or
    moved to the finding's line if GitHub marks it outdated. An unpaired finding is created, in
    comment order, while fewer than ``max_open_comments`` of the bot's comments would be open,
    and withheld ``over_limit`` after that. A created comment never takes a fingerprint that an
    open or person-resolved comment already holds (``_unheld``), so no two open comments share
    one in a settled state.

    Of two open comments with one fingerprint, the one that isn't outdated, else the older, is
    kept and the other resolved. An unpaired open comment is resolved if its anchored line is
    gone (``_is_gone``).
    """
    lines = _new_side_lines(pr_diff)
    renamed = _renamed_paths(pr_diff)
    pairable, withheld = _pairable_findings(findings, lines)
    duplicates = _duplicates(existing)
    duplicate_ids = {comment.comment_id for comment in duplicates}
    # A thread the bot resolved is never a candidate, so it can't capture a finding.
    candidates = [
        comment
        for comment in existing
        if comment.resolved_by is not Resolver.BOT and comment.comment_id not in duplicate_ids
    ]
    pairs = _pair(pairable, candidates, renamed)
    paired_ids = {comment.comment_id for comment in pairs.values()}
    gone = [
        comment
        for comment in candidates
        if comment.resolved_by is None
        and comment.comment_id not in paired_ids
        and _is_gone(comment, lines, renamed)
    ]
    resolved = sorted((*duplicates, *gone), key=lambda comment: comment.comment_id)
    still_open = sum(comment.resolved_by is None for comment in existing) - len(resolved)
    open_slots = max_open_comments - still_open
    held = {comment.fingerprint for comment in existing if comment.resolved_by is not Resolver.BOT}
    # Every fingerprint in use is taken, so a new one is unique on the PR and in this run.
    fingerprinter = Fingerprinter(taken=held | {reviewed.fingerprint for reviewed in findings})
    planned: list[PlannedComment] = []
    for position, finding in enumerate(pairable):
        paired = pairs.get(position)
        if paired is not None:
            planned.append(_plan_paired(finding, paired))
        elif open_slots > 0:
            open_slots -= 1
            reviewed = _unheld(finding, held, fingerprinter)
            planned.append(PlannedComment(Action.CREATE, reviewed, finding.anchor))
        else:
            withheld.append(WithheldFinding(finding.reviewed, WithholdReason.OVER_LIMIT))
    withheld.sort(key=lambda item: _comment_order(item.reviewed))
    return CommentPlan(tuple(planned), tuple(withheld), tuple(resolved))


def _pairable_findings(
    findings: Iterable[ReviewedFinding], lines: _Lines
) -> tuple[list[_Pairable], list[WithheldFinding]]:
    """The findings that may pair, in comment order, and the ones withheld before pairing."""
    pairable: list[_Pairable] = []
    withheld: list[WithheldFinding] = []
    for reviewed in sorted(findings, key=_comment_order):
        finding = reviewed.finding
        anchor_text = lines.get(finding.file, {}).get(finding.line)
        if anchor_text is None or has_lone_surrogate(finding.file):
            withheld.append(WithheldFinding(reviewed, WithholdReason.NOT_COMMENTABLE))
        elif scan_secrets(finding.rationale):
            withheld.append(WithheldFinding(reviewed, WithholdReason.POSSIBLE_SECRET))
        else:
            pairable.append(_Pairable(reviewed, anchor_text, anchor_for(anchor_text)))
    return pairable, withheld


def _unheld(
    finding: _Pairable, held: Collection[str], fingerprinter: Fingerprinter
) -> ReviewedFinding:
    """The finding, with the next free ordinal's fingerprint if a comment already holds its own.

    An identical line added above a commented one takes that comment's ordinal, so without this
    the new comment would share the kept comment's fingerprint, and the duplicates rule would
    resolve and re-post it on every run.
    """
    reviewed = finding.reviewed
    if reviewed.fingerprint not in held:
        return reviewed
    fingerprint = fingerprinter.fingerprint(
        file=reviewed.finding.file, rule=reviewed.rule_part, anchor_text=finding.anchor_text
    )
    return dataclasses.replace(reviewed, fingerprint=fingerprint)


def _plan_paired(finding: _Pairable, paired: BotComment) -> PlannedComment:
    """What the bot does about the comment ``finding`` paired with."""
    reviewed = dataclasses.replace(finding.reviewed, fingerprint=paired.fingerprint)
    if paired.resolved_by is Resolver.PERSON:
        action = Action.KEEP
    elif paired.line is None:
        action = Action.MOVE
    elif paired.severity != reviewed.finding.severity:
        action = Action.UPDATE
    else:
        action = Action.KEEP
    return PlannedComment(action, reviewed, finding.anchor, paired)


def _pair(
    findings: Sequence[_Pairable], candidates: Iterable[BotComment], renamed: Mapping[str, str]
) -> dict[int, BotComment]:
    """Pair findings with candidates of their key, one to one, by the finding's position.

    A candidate's key follows ``renamed``. Pairs are taken best first: a candidate with the
    finding's fingerprint on the finding's own line; then the candidate nearest the finding's
    line (outdated ones last), then the one with the finding's severity, then an open thread
    over a person-resolved one, then the oldest comment. The fingerprint counts only on the
    finding's own line because an identical line added above shifts the ordinals, so it would
    pair a comment with the new line instead of its own.
    """
    by_key: defaultdict[_Key, list[BotComment]] = defaultdict(list)
    for comment in candidates:
        by_key[_comment_key(comment, renamed)].append(comment)
    ranked = sorted(
        (_pair_rank(finding, comment), position, comment)
        for position, finding in enumerate(findings)
        for comment in by_key.get(finding.key, ())
    )
    pairs: dict[int, BotComment] = {}
    taken: set[int] = set()
    for _, position, comment in ranked:
        if position not in pairs and comment.comment_id not in taken:
            pairs[position] = comment
            taken.add(comment.comment_id)
    return pairs


def _pair_rank(finding: _Pairable, comment: BotComment) -> tuple[bool, bool, int, bool, bool, int]:
    """How good a pair is, best lowest; the comment ID makes every rank distinct."""
    reviewed = finding.reviewed
    is_same_line = comment.line == reviewed.finding.line
    distance = 0 if comment.line is None else abs(comment.line - reviewed.finding.line)
    return (
        not (is_same_line and comment.fingerprint == reviewed.fingerprint),
        comment.line is None,
        distance,
        comment.severity != reviewed.finding.severity,
        comment.resolved_by is Resolver.PERSON,
        comment.comment_id,
    )


def _comment_key(comment: BotComment, renamed: Mapping[str, str]) -> _Key:
    return (renamed.get(comment.path, comment.path), comment.rule_part, comment.anchor)


def _duplicates(existing: Iterable[BotComment]) -> list[BotComment]:
    """Each open comment that shares its fingerprint with a better one, to be resolved.

    The best is the one that isn't outdated, else the older.
    """
    by_fingerprint: defaultdict[str, list[BotComment]] = defaultdict(list)
    for comment in existing:
        if comment.resolved_by is None:
            by_fingerprint[comment.fingerprint].append(comment)
    duplicates: list[BotComment] = []
    for group in by_fingerprint.values():
        _, *worse = sorted(group, key=lambda comment: (comment.line is None, comment.comment_id))
        duplicates.extend(worse)
    return duplicates


def _is_gone(comment: BotComment, lines: _Lines, renamed: Mapping[str, str]) -> bool:
    """Whether a comment's anchored line is gone from the PR diff.

    A comment GitHub doesn't mark outdated is gone if its line isn't in its file's PR diff, or
    no longer has its anchor. An outdated one is gone if its anchor is on no line of its file's
    PR diff. Its path follows ``renamed``.
    """
    file_lines = lines.get(renamed.get(comment.path, comment.path), {})
    if comment.line is None:
        return all(anchor_for(text) != comment.anchor for text in file_lines.values())
    text = file_lines.get(comment.line)
    return text is None or anchor_for(text) != comment.anchor


def _comment_order(reviewed: ReviewedFinding) -> tuple[int, str, int]:
    finding = reviewed.finding
    return (-finding.severity.rank, finding.file, finding.line)


def _new_side_lines(pr_diff: ParsedDiff) -> dict[str, dict[int, str]]:
    """Each new path's added and context lines' text, by line number.

    When hunks sharing a path number a line twice, the first one's text is kept, as verify keeps
    it for the fingerprint.
    """
    lines: dict[str, dict[int, str]] = {}
    for file in pr_diff.files:
        if file.path is None:
            continue
        numbered = lines.setdefault(file.path, {})
        for hunk in file.hunks:
            for line in hunk.lines:
                if line.new_line is not None:
                    numbered.setdefault(line.new_line, line.text)
    return lines


def _renamed_paths(pr_diff: ParsedDiff) -> dict[str, str]:
    """Each path the PR diff renames, mapped to its new path."""
    return {
        file.old_path: file.path
        for file in pr_diff.files
        if file.old_path is not None and file.path is not None and file.old_path != file.path
    }
