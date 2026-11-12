"""Apply the severity threshold to the findings verify kept."""

from collections.abc import Sequence
from dataclasses import dataclass

from review_bot.findings import ReviewedFinding, Severity

__all__ = ["Decision", "decide"]


@dataclass(frozen=True, slots=True)
class Decision:
    """The kept findings split by the threshold, each part in the order kept."""

    reported: tuple[ReviewedFinding, ...]
    below_threshold: tuple[ReviewedFinding, ...]


def decide(kept: Sequence[ReviewedFinding], threshold: Severity) -> Decision:
    """Report each finding whose severity is at least ``threshold``."""
    reported: list[ReviewedFinding] = []
    below_threshold: list[ReviewedFinding] = []
    for reviewed in kept:
        is_reported = reviewed.finding.severity.rank >= threshold.rank
        (reported if is_reported else below_threshold).append(reviewed)
    return Decision(reported=tuple(reported), below_threshold=tuple(below_threshold))
