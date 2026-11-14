"""The eval gate: each prompt version pinned to its text, and each allowed model on its suite.

It passes only if every current prompt version's text matches its pin in
``prompt.PROMPT_FINGERPRINTS``; if every model and mode the policy allows (with no models listed,
the default model in diff mode) has a suite in the suites file with at least the cases of the
default model's suite for that mode; and if every suite's cases pass on fresh live recordings by
that suite's provider and model, each miss accepted (Phase 4 design, Versioned prompts and the
eval gate; ADR 0007, ADR 0021). Nothing here calls a model: recordings are replayed as
``replay`` replays them.
"""

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_bot import prompt
from review_bot.evals.formats import (
    EvalInputError,
    EvalMode,
    LoadedCase,
    find_case_files,
    live_recording_dir,
    load_case,
)
from review_bot.evals.replay import read_live_recording, score_recording
from review_bot.governance.policy import ModelProvider, Policy, route_name
from review_bot.settings import Settings

__all__ = [
    "MAX_SUITES_BYTES",
    "CaseOutcome",
    "GateResult",
    "Suite",
    "SuiteResult",
    "check_coverage",
    "check_prompt_pins",
    "load_suites",
    "run_gate",
]

MAX_SUITES_BYTES: Final = 64 * 1024
_DEFAULT_PROVIDER: Final[ModelProvider] = "claude"


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _AcceptedMiss(_Strict):
    case: str
    bug: str
    approved_by: Annotated[str, Field(pattern=r"\S", max_length=100)]
    approved_on: date


class _SuiteEntry(_Strict):
    provider: ModelProvider
    model: Annotated[str, Field(pattern=r"^\S+$", max_length=200)]
    mode: EvalMode
    cases: Annotated[list[str], Field(min_length=1)]
    accepted_miss: list[_AcceptedMiss] = Field(default_factory=list)


class _SuitesFile(_Strict):
    suite: Annotated[list[_SuiteEntry], Field(min_length=1)]


@dataclass(frozen=True, slots=True)
class Suite:
    """One model's eval cases in one mode, and the misses its owner accepted, as (case, bug)."""

    provider: ModelProvider
    model: str
    mode: EvalMode
    cases: tuple[str, ...]
    accepted_misses: frozenset[tuple[str, str]] = frozenset()

    @property
    def name(self) -> str:
        """Such as ``claude:claude-opus-5-5 diff``."""
        return f"{route_name(self.provider, self.model)} {self.mode}"


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """One suite case, and why it fails, if it does.

    ``is_fresh`` says its live recording is fresh and by the suite's model, and
    ``accepted_misses`` counts its misses the suite accepts.
    """

    case_id: str
    is_fresh: bool
    accepted_misses: int = 0
    problems: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """Whether nothing fails the case."""
        return not self.problems


@dataclass(frozen=True, slots=True)
class SuiteResult:
    """A suite and the outcome of each of its cases, in its order."""

    suite: Suite
    cases: tuple[CaseOutcome, ...]

    @property
    def fresh(self) -> int:
        """How many cases have a fresh live recording by the suite's model."""
        return sum(case.is_fresh for case in self.cases)

    @property
    def passed_cases(self) -> int:
        """How many cases passed."""
        return sum(case.passed for case in self.cases)

    @property
    def accepted_misses(self) -> int:
        """How many misses were accepted, over every case."""
        return sum(case.accepted_misses for case in self.cases)

    @property
    def passed(self) -> bool:
        """Whether every case passed."""
        return self.passed_cases == len(self.cases)


@dataclass(frozen=True, slots=True)
class GateResult:
    """What the gate found: prompt pins that fail, models the suites don't cover, each suite."""

    prompt_problems: tuple[str, ...]
    coverage_problems: tuple[str, ...]
    suites: tuple[SuiteResult, ...]

    @property
    def passed(self) -> bool:
        """Whether every check passed."""
        return (
            not self.prompt_problems
            and not self.coverage_problems
            and all(suite.passed for suite in self.suites)
        )


async def run_gate(cases_dir: Path, suites_file: Path, policy: Policy) -> GateResult:
    """Check the prompt pins, the suites' coverage of ``policy``'s models, and every suite.

    Each case is scored under ``policy``, as ``replay`` scores it.

    Raises:
        EvalInputError: If a case, the suites file or a live recording is invalid.
    """
    cases = {loaded.case.id: loaded for loaded in map(load_case, find_case_files(cases_dir))}
    suites = load_suites(suites_file, cases)
    prompt_problems = check_prompt_pins(prompt.PROMPT_FINGERPRINTS, prompt.prompt_fingerprints())
    results = [
        SuiteResult(
            suite,
            tuple([await _run_case(suite, cases[case_id], policy) for case_id in suite.cases]),
        )
        for suite in suites
    ]
    return GateResult(tuple(prompt_problems), tuple(check_coverage(suites, policy)), tuple(results))


def load_suites(path: Path, cases: Mapping[str, LoadedCase]) -> tuple[Suite, ...]:
    """Read the suites file at ``path``, strictly, checking it against the known ``cases``.

    Raises:
        EvalInputError: Naming the file and the key or case, if it can't be read, is over
            ``MAX_SUITES_BYTES``, isn't UTF-8 TOML, has a key the format doesn't, misses one or
            has a value of the wrong type; or if two suites share a provider, model and mode, a
            suite lists a case twice or one not in ``cases``, or an accepted miss names a case
            its suite doesn't list or a bug its case doesn't have.
    """
    entries = _parse(path, _read(path)).suite
    suites = tuple(_suite(path, entry, cases) for entry in entries)
    names = [suite.name for suite in suites]
    if repeated := next((name for name in names if names.count(name) > 1), None):
        raise _invalid(path, f"two suites are for {repeated}; merge them into one")
    return suites


def _read(path: Path) -> bytes:
    try:
        with path.open("rb") as file:
            data = file.read(MAX_SUITES_BYTES + 1)
    except OSError as exc:
        raise EvalInputError(
            f"{path} can't be read: {exc.strerror}. Pass the suites file with --suites."
        ) from exc
    if len(data) > MAX_SUITES_BYTES:
        raise _invalid(path, f"it is over the limit of 64 KB ({MAX_SUITES_BYTES} bytes)")
    return data


def _parse(path: Path, data: bytes) -> _SuitesFile:
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise _invalid(path, "it isn't valid UTF-8") from exc
    except tomllib.TOMLDecodeError as exc:
        raise _invalid(path, f"it isn't valid TOML: {exc}") from exc
    except RecursionError as exc:
        raise _invalid(path, "it is nested too deeply") from exc
    try:
        return _SuitesFile.model_validate(document, strict=True)
    except ValidationError as exc:
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error["loc"]) or "top level"
        raise _invalid(path, f"{location}: {error['msg']}") from exc


def _suite(path: Path, entry: _SuiteEntry, cases: Mapping[str, LoadedCase]) -> Suite:
    """``entry`` as a suite, once its cases and accepted misses are checked against ``cases``."""
    suite = Suite(
        entry.provider,
        entry.model,
        entry.mode,
        tuple(entry.cases),
        frozenset((miss.case, miss.bug) for miss in entry.accepted_miss),
    )
    for case_id in suite.cases:
        if case_id not in cases:
            raise _invalid(
                path, f"the suite {suite.name} lists {case_id!r}, which isn't a case under --cases"
            )
        if suite.cases.count(case_id) > 1:
            raise _invalid(path, f"the suite {suite.name} lists {case_id!r} more than once")
    for case_id, bug_id in sorted(suite.accepted_misses):
        if case_id not in suite.cases:
            raise _invalid(
                path,
                f"the suite {suite.name} accepts a miss in {case_id!r}, which isn't one of its "
                "cases",
            )
        if bug_id not in {bug.id for bug in cases[case_id].case.bugs}:
            raise _invalid(
                path,
                f"the suite {suite.name} accepts a miss of {bug_id!r}, which isn't a bug of case "
                f"{case_id!r}",
            )
    return suite


def _invalid(path: Path, problem: str) -> EvalInputError:
    return EvalInputError(f"{path}: {problem}. Fix the suites file.")


def check_prompt_pins(pinned: Mapping[str, str], today: Mapping[str, str]) -> list[str]:
    """Why each prompt version in ``today`` doesn't match its pin in ``pinned``, if it doesn't.

    ``today`` maps each current version to the fingerprint of its text today.
    """
    problems = []
    for version, fingerprint in today.items():
        pin = pinned.get(version)
        if pin is None:
            problems.append(
                f"prompt {version}: no fingerprint is pinned for it; once its evals are "
                f"recorded, pin {fingerprint} in PROMPT_FINGERPRINTS"
            )
        elif pin != fingerprint:
            problems.append(
                f"prompt {version}: its text changed; give it a new version, record its evals "
                f"again, and pin {fingerprint} in PROMPT_FINGERPRINTS"
            )
    return problems


def check_coverage(suites: Sequence[Suite], policy: Policy) -> list[str]:
    """Why a model and mode ``policy`` allows isn't covered by ``suites``, for each that isn't.

    Each needs a suite with every case of the default model's suite in that mode, if there is
    one. A policy that lists no models allows any, so then only the default model in diff mode
    is checked.
    """
    default_model = Settings().model
    allowed = [
        (entry.provider, entry.model, mode)
        for entry in policy.models
        for mode in sorted(entry.modes, reverse=True)
    ] or [(_DEFAULT_PROVIDER, default_model, "diff")]
    by_key = {(suite.provider, suite.model, suite.mode): suite for suite in suites}
    problems = []
    for provider, model, mode in allowed:
        route = route_name(provider, model)
        suite = by_key.get((provider, model, mode))
        if suite is None:
            problems.append(
                f"no suite for {route} in {mode} mode; add one, or leave it out of the policy"
            )
            continue
        default = by_key.get((_DEFAULT_PROVIDER, default_model, mode))
        missing = [] if default is None else [c for c in default.cases if c not in suite.cases]
        if missing:
            problems.append(
                f"the suite {suite.name} lacks cases of the default model's {mode} suite: "
                f"{', '.join(missing)}; add them"
            )
    return problems


async def _run_case(suite: Suite, loaded: LoadedCase, policy: Policy) -> CaseOutcome:
    """Score the case on its live recording by the suite's provider and model, under ``policy``.

    Raises:
        EvalInputError: If the recording is invalid, or the case can't be reviewed.
    """
    case_id = loaded.case.id
    recording = read_live_recording(loaded, suite.mode, suite.provider)
    advice = (
        f"record it with python -m review_bot.evals live --provider {suite.provider} --model "
        f"{suite.model} --mode {suite.mode} --case {case_id}"
    )
    if recording is None:
        directory = live_recording_dir(suite.mode, suite.provider)
        missing = f"its live recording {directory}recording.json is missing; {advice}"
        return CaseOutcome(case_id, is_fresh=False, problems=(missing,))
    if recording.model != suite.model:
        other_model = f"its live recording is by another model, {recording.model}; {advice}"
        return CaseOutcome(case_id, is_fresh=False, problems=(other_model,))
    result = await score_recording(loaded, recording, mode=suite.mode, policy=policy)
    if result.score is None:
        # Stale: ``problems`` names the fields that differ.
        return CaseOutcome(case_id, is_fresh=False, problems=result.problems)
    misses = result.score.misses
    unaccepted = [miss for miss in misses if (case_id, miss.bug.id) not in suite.accepted_misses]
    problems = (
        *(f"{miss.problem}, and it isn't an accepted miss" for miss in unaccepted),
        *result.score.problems_besides_misses,
    )
    return CaseOutcome(
        case_id,
        is_fresh=True,
        accepted_misses=len(misses) - len(unaccepted),
        problems=problems,
    )
