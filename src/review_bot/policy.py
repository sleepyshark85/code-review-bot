"""Apply the severity threshold to the findings verify kept."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from review_bot.findings import ReviewedFinding, Severity

__all__ = ["Decision", "decide"]

_SEVERITY_RANK: Final = {severity: rank for rank, severity in enumerate(Severity)}


@dataclass(frozen=True, slots=True)
class Decision:
    """The kept findings split by the threshold, each part in the order kept."""

    reported: tuple[ReviewedFinding, ...]
    below_threshold: tuple[ReviewedFinding, ...]


def decide(kept: Sequence[ReviewedFinding], threshold: Severity) -> Decision:
    """Report each finding whose severity is at least ``threshold``."""
    minimum_rank = _SEVERITY_RANK[threshold]
    reported: list[ReviewedFinding] = []
    below_threshold: list[ReviewedFinding] = []
    for reviewed in kept:
        is_reported = _SEVERITY_RANK[reviewed.finding.severity] >= minimum_rank
        (reported if is_reported else below_threshold).append(reviewed)
    return Decision(reported=tuple(reported), below_threshold=tuple(below_threshold))
