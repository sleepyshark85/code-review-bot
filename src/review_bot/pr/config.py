"""A repository's review policy, from the ``.review-bot.toml`` at a pull request's base commit.

Pure: the runner reads the file through git and passes its bytes in. The file sets only the
threshold, the excluded paths, the comment limit, the domain rules (P3-S10) and the finding
classes that gate a merge (P4-S5); the mode and the caps stay in the workflow, so a key for them
is unknown, and invalid (P2-S9; Phase 2 design, CI settings and caps).
"""

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Final

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainValidator,
    ValidationError,
)
from pydantic_core import ErrorDetails

from review_bot.errors import ConfigFileError, RepoConfigError
from review_bot.findings import RuleId, Severity
from review_bot.governance.gate import GATE_CLASS_NAMES, GateClass, class_name_problem
from review_bot.governance.policy import Policy
from review_bot.pr.plan import DEFAULT_MAX_OPEN_COMMENTS
from review_bot.render import escape_unprintable
from review_bot.rules import DEFAULT_DOMAIN_RULE_SEVERITY, DOMAIN_RULE_IDS, DomainRule
from review_bot.settings import Settings

__all__ = [
    "CONFIG_PATH",
    "DEFAULT_REPO_CONFIG",
    "MAX_CONFIG_BYTES",
    "RepoConfig",
    "gate_classes",
    "invalid_config",
    "parse_config_file",
    "parse_repo_config",
    "with_repo_threshold",
]

CONFIG_PATH: Final = ".review-bot.toml"
MAX_CONFIG_BYTES: Final = 64 * 1024
_MAX_PATTERNS: Final = 50
_MAX_PATTERN_CHARS: Final = 200
_MAX_DOMAIN_RULES: Final = 20
_MAX_DOMAIN_RULE_PATTERNS: Final = 20
# Bounds a key named in a message, since an unknown key can be as long as the file.
_MAX_SHOWN_KEY_CHARS: Final = 100
_DEFAULT_SOURCE: Final = "default"


@dataclass(frozen=True, slots=True)
class RepoConfig:
    """The policy a pull request is reviewed under, and where it came from.

    ``source`` is ``default`` when the base commit has no file, else where the file was read:
    ``.review-bot.toml@<SHA>`` for a base commit's.
    ``threshold`` is None when the file doesn't set one; ``exclude`` holds globs on a file's
    path, as ``parse_diff`` matches them. ``domain_rules`` are the domain rules it enables, and
    ``gate`` the names of the finding classes it gates a merge on (``gate_classes``).
    """

    source: str
    threshold: Severity | None = None
    exclude: tuple[str, ...] = ()
    max_open_comments: int = DEFAULT_MAX_OPEN_COMMENTS
    domain_rules: tuple[DomainRule, ...] = ()
    gate: tuple[str, ...] = ()


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


def _domain_rule_id(name: object) -> RuleId:
    """The catalogue's rule named ``name``, which must be a string."""
    if not isinstance(name, str) or name not in DOMAIN_RULE_IDS:
        known = ", ".join(sorted(DOMAIN_RULE_IDS))
        raise ValueError(f"it names no domain rule; the domain rules are: {known}")
    return RuleId(name)


def _gate_class_name(name: object) -> str:
    """``name``, if it is a finding class's, which must be a string."""
    if not isinstance(name, str) or name not in GATE_CLASS_NAMES:
        text = name if isinstance(name, str) else ""
        shown = escape_unprintable(text[:_MAX_SHOWN_KEY_CHARS])
        raise ValueError(f"the class '{shown}' can't gate: {class_name_problem(text)}")
    return name


def _no_repeated_names(names: list[str]) -> list[str]:
    repeated = next((name for index, name in enumerate(names) if name in names[:index]), None)
    if repeated is not None:
        raise ValueError(f"it lists '{repeated}' more than once")
    return names


class _DomainRuleEntry(BaseModel):
    """One ``[[domain_rules]]`` entry: any other key, or a value of another type, is invalid."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: Annotated[RuleId, PlainValidator(_domain_rule_id)]
    paths: list[_Pattern] = Field(min_length=1, max_length=_MAX_DOMAIN_RULE_PATTERNS)
    severity: Severity = DEFAULT_DOMAIN_RULE_SEVERITY


class _ConfigFile(BaseModel):
    """The file's schema: any other key, or a value of another type, is invalid."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    threshold: Severity | None = None
    exclude: list[_Pattern] = Field(default_factory=list, max_length=_MAX_PATTERNS)
    # Strict, so neither "3", 3.0 nor true passes for 3.
    max_open_comments: int = Field(default=DEFAULT_MAX_OPEN_COMMENTS, strict=True, ge=1, le=25)
    domain_rules: list[_DomainRuleEntry] = Field(default_factory=list, max_length=_MAX_DOMAIN_RULES)
    gate: Annotated[
        list[Annotated[str, PlainValidator(_gate_class_name)]], AfterValidator(_no_repeated_names)
    ] = Field(default_factory=list)


def parse_repo_config(data: bytes, *, base_sha: str) -> RepoConfig:
    """Parse the file's bytes, read from the commit ``base_sha``, into the repository's policy.

    ``data`` may be longer than ``MAX_CONFIG_BYTES``, so that an oversized file is reported.

    Raises:
        RepoConfigError: If the file is invalid, as ``parse_config_file`` says, naming the
            commit and the key or the problem.
    """
    try:
        return parse_config_file(data, source=f"{CONFIG_PATH}@{base_sha}")
    except ConfigFileError as exc:
        raise invalid_config(base_sha, str(exc)) from exc


def parse_config_file(data: bytes, *, source: str) -> RepoConfig:
    """Parse a ``.review-bot.toml``'s bytes, read from ``source``, into a review policy.

    ``data`` may be longer than ``MAX_CONFIG_BYTES``, so that an oversized file is reported.

    Raises:
        ConfigFileError: If the file is over ``MAX_CONFIG_BYTES``, isn't UTF-8 or TOML, has a
            key other than ``threshold``, ``exclude``, ``max_open_comments``,
            ``domain_rules`` and ``gate``, an entry of ``domain_rules`` with a key other than
            ``rule``, ``paths`` and ``severity``, a value out of its type or range, a rule not
            in the catalogue, a pattern with an empty segment, or a ``gate`` entry that isn't a
            finding class or is listed twice. The message names the key, with its entry, or
            the problem, but not ``source``: the caller says where the file is.
    """
    if len(data) > MAX_CONFIG_BYTES:
        raise ConfigFileError(f"it is over the limit of {MAX_CONFIG_BYTES} bytes")
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigFileError("it isn't valid UTF-8") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigFileError(f"it isn't valid TOML: {exc}") from exc
    except RecursionError as exc:
        # Nesting deeper than the parser can recurse makes the file as unusable as bad syntax.
        raise ConfigFileError("it is nested too deeply") from exc
    try:
        parsed = _ConfigFile.model_validate(document)
    except ValidationError as exc:
        raise ConfigFileError(_problem(exc.errors()[0])) from exc
    return RepoConfig(
        source=source,
        threshold=parsed.threshold,
        exclude=tuple(parsed.exclude),
        max_open_comments=parsed.max_open_comments,
        domain_rules=tuple(
            DomainRule(entry.rule, tuple(entry.paths), entry.severity)
            for entry in parsed.domain_rules
        ),
        gate=tuple(parsed.gate),
    )


def gate_classes(config: RepoConfig, policy: Policy, *, base_sha: str) -> tuple[GateClass, ...]:
    """The classes ``config`` gates on, as ``policy`` approves them, in ``config``'s order.

    A domain rule's class needs a ``[[domain_rules]]`` entry for that rule whose severity meets
    the class's minimum, or it could never block.

    Raises:
        RepoConfigError: If ``policy`` doesn't approve a class, or a domain rule's class has no
            entry, or only entries below its minimum severity; the message names the class,
            the reason and the policy's version.
    """
    approved = {gate_class.rule_part: gate_class for gate_class in policy.gate_classes}
    classes: list[GateClass] = []
    for name in config.gate:
        gate_class = approved.get(name)
        problem = _gate_class_problem(gate_class, config.domain_rules)
        if gate_class is None or problem is not None:
            raise invalid_config(
                base_sha,
                f"gate names the class {name}, but {problem}, so it can't gate under the policy "
                f"`{policy.version}`",
            )
        classes.append(gate_class)
    return tuple(classes)


def _gate_class_problem(
    gate_class: GateClass | None, domain_rules: Sequence[DomainRule]
) -> str | None:
    """Why a class could never block, or None if it can.

    It can't if the policy doesn't approve it (``gate_class`` is None), or if it is a domain
    rule's class and ``domain_rules`` has no entry for that rule at its minimum severity.
    """
    if gate_class is None:
        return "the policy doesn't approve it"
    if gate_class.rule_part not in DOMAIN_RULE_IDS:
        return None
    severities = [rule.severity for rule in domain_rules if rule.rule == gate_class.rule_part]
    if not severities:
        return "no [[domain_rules]] entry enables that rule"
    minimum = gate_class.min_severity
    if all(severity.rank < minimum.rank for severity in severities):
        return f"every [[domain_rules]] entry for that rule is below the class's {minimum}"
    return None


def invalid_config(base_sha: str, problem: str) -> RepoConfigError:
    """The error for the file at ``base_sha``, saying what is wrong with it and what to do."""
    return RepoConfigError(
        f"{CONFIG_PATH} at the base commit {base_sha} is invalid: {problem}. Fix it on the base "
        "branch, then run again."
    )


def _problem(error: ErrorDetails) -> str:
    """What one schema error says is wrong, naming its key, such as ``exclude[3]``.

    A key in a domain rule is named with its entry, such as ``domain_rules[0].paths``.
    """
    location = error["loc"]
    key = "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" if index else part
        for index, part in enumerate(location)
    )
    shown = escape_unprintable(key[:_MAX_SHOWN_KEY_CHARS])
    if error["type"] != "extra_forbidden":
        return f"invalid value for '{shown}': {error['msg']}"
    if location[0] == "domain_rules":
        return f"unknown key '{shown}'. A domain rule may set only rule, paths and severity"
    return (
        f"unknown key '{shown}'. It may set only threshold, exclude, max_open_comments, "
        "domain_rules and gate; the mode and the caps are set in the workflow"
    )


def with_repo_threshold(settings: Settings, config: RepoConfig) -> Settings:
    """``settings`` with the file's threshold, unless a flag or variable gave one.

    The order is flag, then environment, then file, then default. A field given when
    ``settings`` was built, as ``load_settings`` gives a flag's or variable's value, counts as
    given.
    """
    if config.threshold is None or "threshold" in settings.model_fields_set:
        return settings
    return settings.model_copy(update={"threshold": config.threshold})
