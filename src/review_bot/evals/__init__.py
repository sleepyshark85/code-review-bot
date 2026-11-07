"""Eval cases, replaying their recordings offline, and recording live runs (ADR 0007)."""

from review_bot.evals.cli import main
from review_bot.evals.replay import freshness_fields

__all__ = ["freshness_fields", "main"]
