"""Decide which reported findings become comments, and why the others are withheld.

Pure and deterministic: the model's findings never post anything themselves (Phase 2 design,
Comment state). With no comments on the pull request yet, every finding is a create or withheld.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from review_bot.diff_parser import ParsedDiff
from review_bot.findings import ReviewedFinding
from review_bot.fingerprint import anchor_for
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


class Action(StrEnum):
    """What the bot would do with a comment."""

    CREATE = "create"


class WithholdReason(StrEnum):
    """Why a reported finding gets no comment."""

    OVER_LIMIT = "over_limit"
    POSSIBLE_SECRET = "possible_secret"  # noqa: S105 - a reason's name, not a secret
    NOT_COMMENTABLE = "not_commentable"


@dataclass(frozen=True, slots=True)
class PlannedComment:
    """A comment the bot would post on its finding's line.

    ``anchor`` is the first 16 hex digits of the anchored line's hash (``anchor_for``).
    """

    action: Action
    reviewed: ReviewedFinding
    anchor: str


@dataclass(frozen=True, slots=True)
class WithheldFinding:
    """A reported finding that gets no comment, and why."""

    reviewed: ReviewedFinding
    reason: WithholdReason


@dataclass(frozen=True, slots=True)
class CommentPlan:
    """The planned comments and the withheld findings, each in comment order."""

    comments: tuple[PlannedComment, ...] = ()
    withheld: tuple[WithheldFinding, ...] = ()


def plan_comments(
    findings: Sequence[ReviewedFinding], *, pr_diff: ParsedDiff, max_open_comments: int
) -> CommentPlan:
    """Plan a comment for each finding, in comment order, until ``max_open_comments`` are planned.

    Comment order is severity, highest first, then file, then line. A finding is withheld
    ``not_commentable`` if its path isn't valid UTF-8, which GitHub can't address, or its line
    isn't an added or context line of ``pr_diff``; ``possible_secret`` if ``scan_secrets`` finds
    one in its rationale; and ``over_limit`` once the cap is reached. The rationale is scanned
    rather than the rendered body, because Markdown escaping splits a token such as ``ghp_...``
    so that the scan would miss it in the body, though GitHub would show it whole.
    """
    commentable = _commentable_lines(pr_diff)
    comments: list[PlannedComment] = []
    withheld: list[WithheldFinding] = []
    for reviewed in sorted(findings, key=_comment_order):
        finding = reviewed.finding
        anchor_text = commentable.get(finding.file, {}).get(finding.line)
        if anchor_text is None or has_lone_surrogate(finding.file):
            withheld.append(WithheldFinding(reviewed, WithholdReason.NOT_COMMENTABLE))
        elif scan_secrets(finding.rationale):
            withheld.append(WithheldFinding(reviewed, WithholdReason.POSSIBLE_SECRET))
        elif len(comments) >= max_open_comments:
            withheld.append(WithheldFinding(reviewed, WithholdReason.OVER_LIMIT))
        else:
            comments.append(PlannedComment(Action.CREATE, reviewed, anchor_for(anchor_text)))
    return CommentPlan(tuple(comments), tuple(withheld))


def _comment_order(reviewed: ReviewedFinding) -> tuple[int, str, int]:
    finding = reviewed.finding
    return (-finding.severity.rank, finding.file, finding.line)


def _commentable_lines(pr_diff: ParsedDiff) -> dict[str, dict[int, str]]:
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
