"""The policy file, ``policy/v1``: the tools, cost ceiling, masking and gate classes allowed.

Pure: the command line reads the file and passes its bytes in (ADR 0018). Each slice adds the
section it enforces, so any key this module doesn't know is invalid. Without a file, a run uses
``BUILTIN_POLICY``, which allows what Phase 3 did.
"""

import hashlib
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainValidator, ValidationError
from pydantic_core import ErrorDetails

from review_bot.context.ledger import ToolName
from review_bot.errors import ConfigError
from review_bot.findings import Severity
from review_bot.governance.gate import GATE_CLASS_NAMES, GateClass, class_name_problem
from review_bot.governance.redaction import RedactionKind

if TYPE_CHECKING:
    # Only for its type: ``settings`` imports ``audit``, which imports ``report``, which imports
    # this module.
    from review_bot.settings import Settings

__all__ = [
    "BUILTIN_POLICY",
    "BUILTIN_POLICY_TEXT",
    "MAX_POLICY_BYTES",
    "Policy",
    "PolicyError",
    "allows_tool",
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
_KNOWN_KEYS: Final = (
    "version, [tools] allowed, [cost] max_cost_usd, [redaction] kinds and [[gate.classes]] "
    "rule, min_severity, min_confidence, approved_by and approved_on"
)
_GATE_SEVERITIES: Final = (Severity.HIGH, Severity.CRITICAL)
_DEFAULT_MIN_SEVERITY: Final = Severity.CRITICAL
_MIN_CONFIDENCE_RANGE: Final = (0.5, 1.0)
_DEFAULT_MIN_CONFIDENCE: Final = 0.9


class PolicyError(ConfigError):
    """The policy file can't be read or is invalid; the message names the file and the problem."""


@dataclass(frozen=True, slots=True)
class Policy:
    """What a run may do, and which policy said so.

    ``version`` is the file's own label; ``sha256`` is the SHA-256 of the file's bytes, or of
    ``BUILTIN_POLICY_TEXT`` for the built-in policy; ``source`` is ``builtin``, or the file's
    path as given. ``tools`` are the tools a context review may use. ``max_cost_usd`` is the
    ceiling on every model call's cost cap, or None for no ceiling. ``redaction`` are the kinds
    of personal data masked in everything a request sends; none masks nothing.
    ``gate_classes`` are the finding classes a repository may gate a merge on; none gates
    nothing.
    """

    version: str
    sha256: str
    source: str
    tools: frozenset[ToolName]
    max_cost_usd: Decimal | None
    redaction: frozenset[RedactionKind] = frozenset()
    gate_classes: tuple[GateClass, ...] = ()


BUILTIN_POLICY: Final = Policy(
    version="builtin-v1",
    sha256=hashlib.sha256(BUILTIN_POLICY_TEXT).hexdigest(),
    source=_BUILTIN_SOURCE,
    tools=frozenset(ToolName),
    max_cost_usd=None,
)


def _shown(text: str) -> str:
    """At most 100 characters of ``text``, with each unprintable character escaped.

    As ``render.escape_unprintable`` escapes, which can't be imported here: ``render`` imports
    ``report``, which imports this module.
    """
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in text[:_MAX_SHOWN_CHARS]
    )


def _version(value: object) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError("it must be a string of 1 to 64 letters, digits, '.', '_' or '-'")
    return value


def _tool_name(value: object) -> ToolName:
    if not isinstance(value, str) or value not in ToolName:
        shown = _shown(value) if isinstance(value, str) else type(value).__name__
        raise ValueError(f"'{shown}' isn't a tool; the tools are read, grep and glob")
    return ToolName(value)


def _redaction_kind(value: object) -> RedactionKind:
    if not isinstance(value, str) or value not in RedactionKind:
        shown = _shown(value) if isinstance(value, str) else type(value).__name__
        raise ValueError(f"'{shown}' isn't a redaction kind; the kinds are email, phone and ipv4")
    return RedactionKind(value)


def _gate_class_name(value: object) -> str:
    if not isinstance(value, str) or value not in GATE_CLASS_NAMES:
        name = value if isinstance(value, str) else ""
        raise ValueError(f"the class '{_shown(name)}' can't gate: {class_name_problem(name)}")
    return value


def _min_severity(value: object) -> Severity:
    if not isinstance(value, str) or value not in _GATE_SEVERITIES:
        raise ValueError("it must be high or critical")
    return Severity(value)


def _min_confidence(value: object) -> float:
    low, high = _MIN_CONFIDENCE_RANGE
    # A bool is an int, so it is refused by name.
    if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
        raise ValueError(f"it must be a number from {low} to {high}")
    return float(value)


def _approver(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_SHOWN_CHARS:
        raise ValueError(
            f"it must name who approved the class, in 1 to {_MAX_SHOWN_CHARS} characters"
        )
    return value


def _approval_date(value: object) -> date:
    # TOML reads 2026-12-01 as a date; a datetime is a date too, so it is refused by name.
    if type(value) is not date:
        raise ValueError("it must be a TOML date, such as 2026-12-01")
    return value


def _no_repeats[T](items: list[T]) -> list[T]:
    repeated = next((item for index, item in enumerate(items) if item in items[:index]), None)
    if repeated is not None:
        raise ValueError(f"it lists '{repeated}' more than once")
    return items


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


class _Redaction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kinds: Annotated[
        list[Annotated[RedactionKind, PlainValidator(_redaction_kind)]],
        AfterValidator(_no_repeats),
    ] = Field(default_factory=list)


class _GateClassEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: Annotated[str, PlainValidator(_gate_class_name)]
    min_severity: Annotated[Severity, PlainValidator(_min_severity)] = _DEFAULT_MIN_SEVERITY
    min_confidence: Annotated[float, PlainValidator(_min_confidence)] = _DEFAULT_MIN_CONFIDENCE
    approved_by: Annotated[str, PlainValidator(_approver)]
    approved_on: Annotated[date, PlainValidator(_approval_date)]


def _no_repeated_classes(entries: list[_GateClassEntry]) -> list[_GateClassEntry]:
    _no_repeats([entry.rule for entry in entries])
    return entries


class _Gate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    classes: Annotated[list[_GateClassEntry], AfterValidator(_no_repeated_classes)] = Field(
        default_factory=list
    )


class _PolicyFile(BaseModel):
    """The file's schema: any other key, or a value of another type, is invalid.

    A section left out allows what the built-in policy does.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Annotated[str, PlainValidator(_version)]
    tools: _Tools = _Tools()
    cost: _Cost = _Cost()
    redaction: _Redaction = _Redaction()
    gate: _Gate = _Gate()


def parse_policy(data: bytes, *, source: str) -> Policy:
    """Parse a policy file's bytes, read from ``source``, into the policy they state.

    ``data`` may be longer than ``MAX_POLICY_BYTES``, so that an oversized file is reported.

    Raises:
        PolicyError: If the file is over ``MAX_POLICY_BYTES``, isn't UTF-8 or TOML, has a key
            other than ``version``, ``[tools] allowed``, ``[cost] max_cost_usd``,
            ``[redaction] kinds`` and ``[[gate.classes]]``'s ``rule``, ``min_severity``,
            ``min_confidence``, ``approved_by`` and ``approved_on``, has no ``version`` or one
            that isn't 1 to 64 of
            ``[A-Za-z0-9._-]``, lists a tool other than ``read``, ``grep`` and ``glob`` or one
            tool twice, has a ``max_cost_usd`` that isn't a decimal string from 0.01 to 1000,
            or lists a redaction kind other than ``email``, ``phone`` and ``ipv4`` or one kind
            twice, or has a gate class that isn't a class (``class_name_problem``), names one
            class twice, has a ``min_severity`` other than ``high`` or ``critical``, a
            ``min_confidence`` outside 0.5 to 1.0, no ``approved_by`` or no ``approved_on``
            date. The message names ``source`` and the key or the problem.
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
        redaction=frozenset(parsed.redaction.kinds),
        gate_classes=tuple(
            GateClass(
                entry.rule,
                entry.min_severity,
                entry.min_confidence,
                entry.approved_by,
                entry.approved_on,
            )
            for entry in parsed.gate.classes
        ),
    )


def allows_tool(allowed: frozenset[ToolName], name: str) -> bool:
    """Whether a policy allowing ``allowed`` lets a review call the tool ``name``.

    The one tool decision: the executor and Claude's hook and ``can_use_tool`` all make it here.
    ``name`` is the model's untrusted text, so it must equal a tool's name exactly.
    """
    return name in allowed


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
    if error["type"] == "model_type":
        return f"invalid value for '{shown}': it must be a table, such as [{shown}]"
    reason = error["ctx"]["error"] if error["type"] == "value_error" else error["msg"]
    return f"invalid value for '{shown}': {reason}"
