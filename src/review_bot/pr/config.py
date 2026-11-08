"""A repository's review policy, from the ``.review-bot.toml`` at a pull request's base commit.

Pure: the runner reads the file through git and passes its bytes in. The file sets only the
threshold, the excluded paths and the comment limit; the mode and the caps stay in the workflow,
so a key for them is unknown, and invalid (P2-S9; Phase 2 design, CI settings and caps).
"""

import tomllib
from dataclasses import dataclass
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import ErrorDetails

from review_bot.errors import RepoConfigError
from review_bot.findings import Severity
from review_bot.pr.plan import DEFAULT_MAX_OPEN_COMMENTS
from review_bot.render import escape_unprintable
from review_bot.settings import Settings

__all__ = [
    "CONFIG_PATH",
    "DEFAULT_REPO_CONFIG",
    "MAX_CONFIG_BYTES",
    "RepoConfig",
    "invalid_config",
    "parse_repo_config",
    "with_repo_threshold",
]

CONFIG_PATH: Final = ".review-bot.toml"
MAX_CONFIG_BYTES: Final = 64 * 1024
_MAX_PATTERNS: Final = 50
_MAX_PATTERN_CHARS: Final = 200
# Bounds a key named in a message, since an unknown key can be as long as the file.
_MAX_SHOWN_KEY_CHARS: Final = 100
_DEFAULT_SOURCE: Final = "default"


@dataclass(frozen=True, slots=True)
class RepoConfig:
    """The policy a pull request is reviewed under, and where it came from.

    ``source`` is ``default`` when the base commit has no file, else ``.review-bot.toml@<SHA>``.
    ``threshold`` is None when the file doesn't set one; ``exclude`` holds globs on a file's
    path, as ``parse_diff`` matches them.
    """

    source: str
    threshold: Severity | None = None
    exclude: tuple[str, ...] = ()
    max_open_comments: int = DEFAULT_MAX_OPEN_COMMENTS


DEFAULT_REPO_CONFIG: Final = RepoConfig(source=_DEFAULT_SOURCE)


def _relative_pattern(pattern: str) -> str:
    """``pattern`` if it has no empty segment, which no path has, so it could match nothing."""
    if "" in pattern.split("/"):
        raise ValueError(
            f"the pattern '{escape_unprintable(pattern)}' has an empty segment; patterns are "
            "relative paths, without a leading or trailing slash or '//'"
        )
    return pattern


type _Pattern = Annotated[
    str, Field(strict=True, max_length=_MAX_PATTERN_CHARS), AfterValidator(_relative_pattern)
]


class _ConfigFile(BaseModel):
    """The file's schema: any other key, or a value of another type, is invalid."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    threshold: Severity | None = None
    exclude: list[_Pattern] = Field(default_factory=list, max_length=_MAX_PATTERNS)
    # Strict, so neither "3", 3.0 nor true passes for 3.
    max_open_comments: int = Field(default=DEFAULT_MAX_OPEN_COMMENTS, strict=True, ge=1, le=25)


def parse_repo_config(data: bytes, *, base_sha: str) -> RepoConfig:
    """Parse the file's bytes, read from the commit ``base_sha``, into the repository's policy.

    ``data`` may be longer than ``MAX_CONFIG_BYTES``, so that an oversized file is reported.

    Raises:
        RepoConfigError: If the file is over ``MAX_CONFIG_BYTES``, isn't UTF-8 or TOML, has a
            key other than ``threshold``, ``exclude`` and ``max_open_comments``, a value out of
            its type or range, or an ``exclude`` pattern with an empty segment; the message
            names the key or the problem.
    """
    if len(data) > MAX_CONFIG_BYTES:
        raise invalid_config(base_sha, f"it is over the limit of {MAX_CONFIG_BYTES} bytes")
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise invalid_config(base_sha, "it isn't valid UTF-8") from exc
    except tomllib.TOMLDecodeError as exc:
        raise invalid_config(base_sha, f"it isn't valid TOML: {exc}") from exc
    except RecursionError as exc:
        # Nesting deeper than the parser can recurse makes the file as unusable as bad syntax.
        raise invalid_config(base_sha, "it is nested too deeply") from exc
    try:
        parsed = _ConfigFile.model_validate(document)
    except ValidationError as exc:
        raise invalid_config(base_sha, _problem(exc.errors()[0])) from exc
    return RepoConfig(
        source=f"{CONFIG_PATH}@{base_sha}",
        threshold=parsed.threshold,
        exclude=tuple(parsed.exclude),
        max_open_comments=parsed.max_open_comments,
    )


def invalid_config(base_sha: str, problem: str) -> RepoConfigError:
    """The error for the file at ``base_sha``, saying what is wrong with it and what to do."""
    return RepoConfigError(
        f"{CONFIG_PATH} at the base commit {base_sha} is invalid: {problem}. Fix it on the base "
        "branch, then run again."
    )


def _problem(error: ErrorDetails) -> str:
    """What one schema error says is wrong, naming its key, such as ``exclude[3]``."""
    key = "".join(part if isinstance(part, str) else f"[{part}]" for part in error["loc"])
    shown = escape_unprintable(key[:_MAX_SHOWN_KEY_CHARS])
    if error["type"] == "extra_forbidden":
        return (
            f"unknown key '{shown}'. It may set only threshold, exclude and max_open_comments; "
            "the mode and the caps are set in the workflow"
        )
    return f"invalid value for '{shown}': {error['msg']}"


def with_repo_threshold(settings: Settings, config: RepoConfig) -> Settings:
    """``settings`` with the file's threshold, unless a flag or variable gave one.

    The order is flag, then environment, then file, then default. A field given when
    ``settings`` was built, as ``load_settings`` gives a flag's or variable's value, counts as
    given.
    """
    if config.threshold is None or "threshold" in settings.model_fields_set:
        return settings
    return settings.model_copy(update={"threshold": config.threshold})
