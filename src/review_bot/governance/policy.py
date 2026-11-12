"""The policy file, ``policy/v1``: which tools a review may use, and a ceiling on each call's cost.

Pure: the command line reads the file and passes its bytes in (ADR 0018). Each slice adds the
section it enforces, so any key this module doesn't know is invalid. Without a file, a run uses
``BUILTIN_POLICY``, which allows what Phase 3 did.
"""

import hashlib
import re
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainValidator, ValidationError
from pydantic_core import ErrorDetails

from review_bot.context.ledger import ToolName
from review_bot.errors import ConfigError

if TYPE_CHECKING:
    # Only for its type: ``settings`` imports ``report``, which imports this module.
    from review_bot.settings import Settings

__all__ = [
    "BUILTIN_POLICY",
    "BUILTIN_POLICY_TEXT",
    "MAX_POLICY_BYTES",
    "Policy",
    "PolicyError",
    "check_context_allowed",
    "check_settings",
    "parse_policy",
]

MAX_POLICY_BYTES: Final = 64 * 1024
# The built-in policy as a file would state it; its SHA-256 is the built-in policy's.
BUILTIN_POLICY_TEXT: Final = (
    b'version = "builtin-v1"\n\n[tools]\nallowed = ["read", "grep", "glob"]\n'
)
_BUILTIN_SOURCE: Final = "builtin"
_VERSION: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")
# Digits with an optional fraction, so a sign, an exponent, nan or inf is refused.
_DECIMAL: Final = re.compile(r"[0-9]+(\.[0-9]+)?")
_MIN_COST_USD: Final = Decimal("0.01")
_MAX_COST_USD: Final = Decimal(1000)
# Bounds a key or value named in a message, since either can be as long as the file.
_MAX_SHOWN_CHARS: Final = 100
_KNOWN_KEYS: Final = "version, [tools] allowed and [cost] max_cost_usd"


class PolicyError(ConfigError):
    """The policy file can't be read or is invalid; the message names the file and the problem."""


@dataclass(frozen=True, slots=True)
class Policy:
    """What a run may do, and which policy said so.

    ``version`` is the file's own label; ``sha256`` is the SHA-256 of the file's bytes, or of
    ``BUILTIN_POLICY_TEXT`` for the built-in policy; ``source`` is ``builtin``, or the file's
    path as given. ``tools`` are the tools a context review may use. ``max_cost_usd`` is the
    ceiling on every model call's cost cap, or None for no ceiling.
    """

    version: str
    sha256: str
    source: str
    tools: frozenset[ToolName]
    max_cost_usd: Decimal | None


BUILTIN_POLICY: Final = Policy(
    version="builtin-v1",
    sha256=hashlib.sha256(BUILTIN_POLICY_TEXT).hexdigest(),
    source=_BUILTIN_SOURCE,
    tools=frozenset(ToolName),
    max_cost_usd=None,
)


def _shown(text: str) -> str:
    """At most 100 characters of ``text``, with every character that isn't printable ASCII escaped.

    Not ``render.escape_unprintable``: ``render`` imports this module, through ``report``.
    """
    return text[:_MAX_SHOWN_CHARS].encode("unicode_escape").decode("ascii")


def _version(value: object) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError("it must be a string of 1 to 64 letters, digits, '.', '_' or '-'")
    return value


def _tool_name(value: object) -> ToolName:
    if not isinstance(value, str) or value not in ToolName:
        shown = _shown(value) if isinstance(value, str) else type(value).__name__
        raise ValueError(f"'{shown}' isn't a tool; the tools are read, grep and glob")
    return ToolName(value)


def _no_repeats(tools: list[ToolName]) -> list[ToolName]:
    repeated = next((tool for index, tool in enumerate(tools) if tool in tools[:index]), None)
    if repeated is not None:
        raise ValueError(f"it lists '{repeated}' more than once")
    return tools


def _usd(value: object) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise ValueError('it must be a decimal string, such as "0.60"')
    amount = Decimal(value)
    if not _MIN_COST_USD <= amount <= _MAX_COST_USD:
        raise ValueError(f"'{_shown(value)}' is outside {_MIN_COST_USD} to {_MAX_COST_USD}")
    return amount


class _Tools(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: Annotated[
        list[Annotated[ToolName, PlainValidator(_tool_name)]], AfterValidator(_no_repeats)
    ] = Field(default_factory=lambda: list(ToolName))


class _Cost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_cost_usd: Annotated[Decimal, PlainValidator(_usd)] | None = None


class _PolicyFile(BaseModel):
    """The file's schema: any other key, or a value of another type, is invalid.

    A section left out allows what the built-in policy does.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Annotated[str, PlainValidator(_version)]
    tools: _Tools = _Tools()
    cost: _Cost = _Cost()


def parse_policy(data: bytes, *, source: str) -> Policy:
    """Parse a policy file's bytes, read from ``source``, into the policy they state.

    ``data`` may be longer than ``MAX_POLICY_BYTES``, so that an oversized file is reported.

    Raises:
        PolicyError: If the file is over ``MAX_POLICY_BYTES``, isn't UTF-8 or TOML, has a key
            other than ``version``, ``[tools] allowed`` and ``[cost] max_cost_usd``, has no
            ``version`` or one that isn't 1 to 64 of ``[A-Za-z0-9._-]``, lists a tool other
            than ``read``, ``grep`` and ``glob`` or one tool twice, or has a ``max_cost_usd``
            that isn't a decimal string from 0.01 to 1000. The message names ``source`` and the
            key or the problem.
    """
    if len(data) > MAX_POLICY_BYTES:
        raise _invalid(source, f"it is over the limit of {MAX_POLICY_BYTES} bytes")
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise _invalid(source, "it isn't valid UTF-8") from exc
    except tomllib.TOMLDecodeError as exc:
        raise _invalid(source, f"it isn't valid TOML: {_shown(str(exc))}") from exc
    except RecursionError as exc:
        # Nesting deeper than the parser can recurse makes the file as unusable as bad syntax.
        raise _invalid(source, "it is nested too deeply") from exc
    try:
        parsed = _PolicyFile.model_validate(document)
    except ValidationError as exc:
        raise _invalid(source, _problem(exc.errors()[0])) from exc
    return Policy(
        version=parsed.version,
        sha256=hashlib.sha256(data).hexdigest(),
        source=source,
        tools=frozenset(parsed.tools.allowed),
        max_cost_usd=parsed.cost.max_cost_usd,
    )


def check_settings(settings: "Settings", policy: Policy) -> "Settings":
    """``settings`` within ``policy``: each call's cost cap is at most its ceiling.

    With no cap set, the ceiling is the cap. Context mode, from ``context`` or ``context_repo``,
    needs a tool the policy allows (``check_context_allowed``).

    Raises:
        ConfigError: If the cap is above the ceiling, naming both and the policy, or context
            mode is asked for and the policy allows no tool.
    """
    if settings.context == "repo" or settings.context_repo is not None:
        check_context_allowed(policy)
    ceiling, cap = policy.max_cost_usd, settings.max_cost_usd
    if ceiling is None:
        return settings
    if cap is None:
        return settings.model_copy(update={"max_cost_usd": ceiling})
    if cap > ceiling:
        raise ConfigError(
            f"--max-cost-usd (or REVIEW_BOT_MAX_COST_USD) {cap} is above the ceiling of "
            f"{ceiling} USD per call in the policy `{policy.version}` ({policy.source}). Pass "
            f"at most {ceiling}, or leave it out to use the ceiling."
        )
    return settings


def check_context_allowed(policy: Policy) -> None:
    """Check that ``policy`` lets context mode run: it allows at least one tool.

    Raises:
        ConfigError: If it allows none.
    """
    if not policy.tools:
        raise ConfigError(
            f"The policy `{policy.version}` allows no tools, so context mode can't run; use "
            "diff mode."
        )


def _invalid(source: str, problem: str) -> PolicyError:
    return PolicyError(
        f"The policy file {source} is invalid: {problem}. Fix it, or leave out --policy and "
        "REVIEW_BOT_POLICY to use the built-in policy."
    )


def _problem(error: ErrorDetails) -> str:
    """What one schema error says is wrong, naming its key, such as ``tools.allowed[1]``."""
    key = "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" if index else part
        for index, part in enumerate(error["loc"])
    )
    shown = _shown(key)
    if error["type"] == "extra_forbidden":
        return f"unknown key '{shown}'. A policy may set only {_KNOWN_KEYS}"
    if error["type"] == "missing":
        return f"'{shown}' is missing"
    reason = error["ctx"]["error"] if error["type"] == "value_error" else error["msg"]
    return f"invalid value for '{shown}': {reason}"
