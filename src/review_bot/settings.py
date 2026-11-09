"""Typed settings, resolved from command-line flags, then the environment, then defaults."""

import os
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_bot.audit import DEFAULT_AUDIT_LOG_PATH
from review_bot.errors import ConfigError
from review_bot.findings import Severity

__all__ = ["GITHUB_TOKEN_VARIABLE", "Settings", "in_ci", "load_settings", "take_github_token"]

# Set by GitHub Actions and most CI services.
_CI_VARIABLE: Final = "CI"
GITHUB_TOKEN_VARIABLE: Final = "GITHUB_TOKEN"  # noqa: S105 - the variable's name, not a token
# Fields only a flag sets, so an environment can't turn on prompting (Phase 1 design, Settings).
_FLAG_ONLY_FIELDS: Final = frozenset({"triage"})


class Settings(BaseModel):
    """Validated settings for one run. A field ``max_diff_bytes`` is flag ``--max-diff-bytes``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["mock", "claude"] = "claude"
    # An allowlist, because the name is printed in the stderr summary line. No leading hyphen,
    # because a later provider may pass it to a command line.
    model: str = Field(default="claude-opus-5-5", pattern=r"^[\w.:/@\[\]][\w.:/@\[\]-]*$")
    threshold: Severity = Severity.LOW
    # The upper bound keeps the bounded read's up-front buffer allocatable.
    max_diff_bytes: int = Field(default=400_000, gt=0, le=100_000_000)
    # None means no cap: the bytes of the files sent to the model, after skipping (ADR 0012).
    max_review_bytes: int | None = Field(default=None, gt=0)
    # None means the Claude CLI's own limit.
    max_output_tokens: int | None = Field(default=None, gt=0)
    max_retries: int = Field(default=2, ge=0)
    api_timeout_ms: int = Field(default=90_000, gt=0)
    # Finite, because an infinite deadline would remove the only wall-clock bound.
    deadline_s: float = Field(default=300, gt=0, allow_inf_nan=False)
    audit_log: Path = DEFAULT_AUDIT_LOG_PATH
    format: Literal["text", "json", "markdown"] = "text"
    triage: bool = False
    # Context mode (Phase 3 design, Spend and caps): None means diff mode, with no tools.
    context_repo: Path | None = None
    context_rev: str = Field(default="HEAD", min_length=1)
    max_tool_calls: int = Field(default=12, ge=1, le=30)
    max_tool_result_bytes: int = Field(default=16_000, ge=1_000, le=64_000)
    max_tool_total_bytes: int = Field(default=96_000, gt=0, le=400_000)
    # None means no cap: the CLI's budget for one review (ADR 0015). At least a cent, so rounding
    # it down to the cent never gives 0; at most $1,000, so that rounding is exact.
    max_cost_usd: Decimal | None = Field(default=None, ge=Decimal("0.01"), le=Decimal(1000))


def in_ci(environ: Mapping[str, str]) -> bool:
    """Whether ``environ`` is a CI run's: ``CI`` is set, whatever its value."""
    return _CI_VARIABLE in environ


def take_github_token() -> str | None:
    """Remove ``GITHUB_TOKEN`` from this process's environment, and return it, or None if unset.

    The Claude CLI subprocess inherits this process's environment, and the SDK can only add to
    it, so every entry point calls this before any provider is built: the model's process never
    holds the token (Phase 2 design, Safety on the PR path). Only comment mode uses the value.
    """
    return os.environ.pop(GITHUB_TOKEN_VARIABLE, None)


def load_settings(flags: Mapping[str, str | bool | None]) -> Settings:
    """Build the settings, reading each field's ``REVIEW_BOT_*`` variable if it has no flag.

    ``triage`` has no variable: only its flag sets it.

    Args:
        flags: Flag values by field name; None means the flag wasn't given.

    Raises:
        ConfigError: If a value is invalid; the message names the flag or variable it came from.
    """
    # Keys that aren't fields stay in, even when None, so ``extra="forbid"`` catches a typo.
    values = {
        name: value
        for name, value in flags.items()
        if value is not None or name not in Settings.model_fields
    }
    sources = {name: f"--{name.replace('_', '-')}" for name in values}
    for field in Settings.model_fields:
        env_var = f"REVIEW_BOT_{field.upper()}"
        if field not in values and field not in _FLAG_ONLY_FIELDS and env_var in os.environ:
            values[field] = os.environ[env_var]
            sources[field] = env_var

    try:
        return Settings.model_validate(values)
    except ValidationError as exc:
        error = exc.errors()[0]
        source = sources[str(error["loc"][0])]
        raise ConfigError(f"Invalid value for {source}: {error['msg']}.") from exc
