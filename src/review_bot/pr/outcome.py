"""How a run ended, as ``shadow/v1`` records it in ``outcome`` and ``reason``.

Its own module, so both the report and the summary comment can name it.
"""

from enum import StrEnum
from typing import Final

__all__ = ["OUTCOMES_WITH_FINDINGS", "Outcome", "Reason"]


class Outcome(StrEnum):
    """How a run ended. Only ``reviewed`` and ``degraded`` can plan comments.

    ``degraded``: no model was available, so only the rules' findings are reported (ADR 0021).
    """

    REVIEWED = "reviewed"
    BLOCKED_SECRET = "blocked_secret"  # noqa: S105 - an outcome's name, not a secret
    SKIPPED = "skipped"
    FAILED = "failed"
    DEGRADED = "degraded"


# The outcomes whose reports hold findings; a degraded run's are the rules' alone (P4-S8).
OUTCOMES_WITH_FINDINGS: Final = frozenset({Outcome.REVIEWED, Outcome.DEGRADED})


class Reason(StrEnum):
    """Why a run was skipped, reviewed without the model, or failed before any review.

    A run the provider failed, or that degraded, has the provider's ``ErrorCategory`` as its
    reason instead.
    """

    TOO_LARGE = "too_large"
    EMPTY = "empty"
    NOT_A_DIFF = "not_a_diff"
    UNREADABLE = "unreadable"
    NOTHING_TO_REVIEW = "nothing_to_review"
    # A pull request run refused before git or the provider ran.
    ACTION = "action"
    FORK = "fork"
    NO_CREDENTIAL = "no_credential"
    # Comment mode waits until a draft is ready for review.
    DRAFT = "draft"
    # A backfilled pull request whose base or head commit isn't in the clone.
    MISSING_COMMIT = "missing_commit"
    # Comment mode: the head is the commit the bot last reviewed.
    ALREADY_REVIEWED = "already_reviewed"
    # Not a skip: outcome ``reviewed``, with no hunk changed since the last reviewed commit, so no
    # model call.
    NO_NEW_CHANGES = "no_new_changes"
    # Failed: the base commit's ``.review-bot.toml`` is invalid, so nothing was reviewed.
    INVALID_CONFIG = "invalid_config"
