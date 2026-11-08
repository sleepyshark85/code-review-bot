"""The tools' arguments: the strict schema each call is checked against, and each definition.

Pure. Every argument comes from the model, so it is parsed here before any git runs.
"""

from collections.abc import Mapping
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_bot.context.ledger import ToolName
from review_bot.context.paths import MAX_PATH_CHARS
from review_bot.providers.base import ToolDefinition

__all__ = [
    "MAX_GLOB_PATHS",
    "MAX_GREP_MATCHES",
    "TOOL_DEFINITIONS",
    "GlobArguments",
    "GrepArguments",
    "ReadArguments",
    "ToolArguments",
    "parse_arguments",
]

MAX_GREP_MATCHES: Final = 50
MAX_GLOB_PATHS: Final = 200

# A literal reaches git's command line, which can't take a NUL. It may hold a tab, as indented
# code does, but no other control character, so no line break.
_ONE_LINE: Final = r"^[^\x00-\x08\x0a-\x1f\x7f]*$"


class ToolArguments(BaseModel):
    """The base of each tool's arguments."""

    # Strict, so ``"10"`` isn't taken for a line number, nor ``true`` for 1.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ReadArguments(ToolArguments):
    """``read``'s arguments."""

    # Any string: ``is_valid_path`` checks it, so a bad path is refused ``invalid_path``.
    path: str = Field(
        description=(
            f"The file's path from the repository's root, with / between segments, at most "
            f"{MAX_PATH_CHARS} characters."
        ),
    )
    start_line: int = Field(default=1, ge=1, description="The first line to return, from 1.")
    line_count: int = Field(default=200, ge=1, le=400, description="How many lines to return.")


class GrepArguments(ToolArguments):
    """``grep``'s arguments."""

    literal: str = Field(
        min_length=1,
        max_length=200,
        pattern=_ONE_LINE,
        description="The exact text to find, on one line: not a regular expression.",
    )
    path_glob: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description="Search only files whose path matches this glob, as glob matches it.",
    )
    ignore_case: bool = Field(default=False, description="Whether to ignore ASCII case.")


class GlobArguments(ToolArguments):
    """``glob``'s arguments."""

    pattern: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "A glob: * matches within one path segment and ** any number of segments. A pattern "
            "with no / matches file names at any depth; one with a / matches from the root."
        ),
    )


TOOL_DEFINITIONS: Final = (
    ToolDefinition(
        name=ToolName.READ,
        description=(
            "Read up to 400 numbered lines of one file at the commit, each as <line>, a tab, "
            "and the line's text."
        ),
        input_schema=ReadArguments.model_json_schema(),
    ),
    ToolDefinition(
        name=ToolName.GREP,
        description=(
            f"Find lines holding a literal string in the commit's text files, as path:line:text, "
            f"sorted by path then line, at most {MAX_GREP_MATCHES}."
        ),
        input_schema=GrepArguments.model_json_schema(),
    ),
    ToolDefinition(
        name=ToolName.GLOB,
        description=(
            f"List the commit's file paths matching a glob, sorted, one per line, at most "
            f"{MAX_GLOB_PATHS}."
        ),
        input_schema=GlobArguments.model_json_schema(),
    ),
)


def parse_arguments[T: ToolArguments](model: type[T], arguments: Mapping[str, object]) -> T | None:
    """``arguments`` as ``model``, or None if they don't fit its schema."""
    try:
        return model.model_validate(dict(arguments))
    except ValidationError:
        return None
