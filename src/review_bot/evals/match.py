"""Whether a finding reports a planted bug: by file, line window, category and keyword groups.

Matching is approximate, so a person confirms each credit (ADR 0007).
"""

import re

from review_bot.evals.formats import Bug
from review_bot.findings import Finding

__all__ = ["matches_bug"]


def matches_bug(bug: Bug, finding: Finding) -> bool:
    """Whether ``finding`` is in the bug's file and lines, of its categories, with its keywords.

    Every keyword group must match the rationale through at least one of its phrases.
    """
    return (
        finding.file == bug.file
        and _in_lines(finding.line, bug)
        and (bug.categories is None or finding.category in bug.categories)
        and all(
            any(_mentions(finding.rationale, phrase) for phrase in group) for group in bug.keywords
        )
    )


def _in_lines(line: int, bug: Bug) -> bool:
    if bug.lines == "any":
        return True
    return any(start <= line <= end for start, end in bug.lines)


def _mentions(text: str, phrase: str) -> bool:
    """Whether ``phrase`` is in ``text``, ignoring case, with no letter or digit either side.

    ``_`` counts as a boundary, so ``export`` and ``csv`` are both in ``export_csv``.
    """
    # ``[^\W_]`` is one letter or digit, in any script.
    pattern = rf"(?<![^\W_]){re.escape(phrase)}(?![^\W_])"
    return re.search(pattern, text, re.IGNORECASE) is not None
