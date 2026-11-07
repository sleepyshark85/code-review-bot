"""The eval case and recording files, read and validated (P1-S14 Interfaces).

A file that doesn't match its format stops the eval with exit 2, naming the file, so a typo can
never be scored as a pass or a fail.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    ValidationError,
    model_validator,
)

from review_bot.errors import ErrorCategory, ReviewBotError
from review_bot.findings import Category, Finding, Severity, Source

__all__ = [
    "INTERNAL_ERROR",
    "Bug",
    "EvalCase",
    "EvalInputError",
    "Injection",
    "LoadedCase",
    "NoiseBudget",
    "RecordedRun",
    "Recording",
    "RecordingKind",
    "RunErrorCategory",
    "find_case_files",
    "load_case",
    "load_recording",
]

_CASE_FILE_NAME: Final = "case.json"

type _Text = Annotated[str, Field(pattern=r"\S")]
type LineWindow = tuple[PositiveInt, PositiveInt]
type RecordingKind = Literal["live", "synthetic"]
# A failed run's category: a provider's, or ``internal`` for an unexpected exception in its call.
type RunErrorCategory = ErrorCategory | Literal["internal"]

INTERNAL_ERROR: Final = "internal"


class EvalInputError(ReviewBotError):
    """An eval case, recording or argument is invalid, so nothing is scored (exit 2)."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Bug(_Strict):
    """One planted bug, and how a finding is matched to it (``evals.match``).

    ``lines`` holds inclusive ``[start, end]`` windows, or ``"any"``. Every keyword group must
    match, through any one of its phrases. ``min_runs_caught`` is required for an ``ai`` bug and
    not allowed for a ``rule`` bug, which must be caught every time.
    """

    id: _Text
    source: Source
    file: _Text
    lines: Literal["any"] | Annotated[tuple[LineWindow, ...], Field(min_length=1)]
    categories: Annotated[tuple[Category, ...], Field(min_length=1)] | None = None
    keywords: tuple[Annotated[tuple[_Text, ...], Field(min_length=1)], ...] = ()
    min_runs_caught: PositiveInt | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.lines != "any":
            for start, end in self.lines:
                if start > end:
                    raise ValueError(f"line window [{start}, {end}] ends before it starts")
        if (self.source is Source.AI) != (self.min_runs_caught is not None):
            raise ValueError(
                "min_runs_caught is required for an ai bug, and not allowed for a rule"
            )
        return self


class NoiseBudget(_Strict):
    """How much a run may report that credits no bug.

    At most ``max_findings`` model findings per run, none more severe than ``max_severity``.
    Findings of every severity count, whatever the review threshold.
    """

    max_findings: NonNegativeInt
    max_severity: Severity


class Injection(_Strict):
    """What a diff's injection attempt asks for, so a run that obeys it can be told apart.

    ``requested_file`` is a file the attempt asks the model to report, which a run must not.
    ``canary`` is a unique string in the attempt; a rationale quoting it needs a person's review.
    """

    canary: _Text | None = None
    requested_file: _Text | None = None


class EvalCase(_Strict):
    """A diff, the bugs planted in it, the checks each run must meet, and how many runs must pass.

    ``diff`` is relative to the case file. A run passes if it didn't fail with an error and meets
    the case's ``noise_budget`` and ``injection`` checks (``evals.score``); ``min_passing_runs`` of
    a recording's runs must pass.
    """

    schema_version: Literal["eval-case/v1"] = Field(alias="schema")
    id: _Text
    diff: _Text
    bugs: tuple[Bug, ...]
    noise_budget: NoiseBudget | None = None
    injection: Injection | None = None
    min_passing_runs: NonNegativeInt = 0

    @model_validator(mode="after")
    def _check(self) -> Self:
        ids = [bug.id for bug in self.bugs]
        if len(set(ids)) != len(ids):
            raise ValueError("bug ids must be unique")
        if not self.bugs and self.noise_budget is None:
            raise ValueError("a case needs at least one bug, or a noise_budget")
        if self.injection is not None and not any(bug.source is Source.AI for bug in self.bugs):
            raise ValueError("an injection case needs the real ai bug a run must still report")
        has_run_checks = self.noise_budget is not None or self.injection is not None
        if has_run_checks and self.min_passing_runs < 1:
            raise ValueError(
                "min_passing_runs must be at least 1 for a case with a noise_budget or injection"
            )
        return self


class _RunError(_Strict):
    category: RunErrorCategory


class _Usage(_Strict):
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt


class RecordedRun(_Strict):
    """One call's result: the validated findings it returned, or the error it failed with.

    Exactly one of ``findings`` and ``error`` is set.
    """

    findings: tuple[Finding, ...] | None = None
    error: _RunError | None = None
    usage: _Usage
    warnings: tuple[str, ...]

    @model_validator(mode="after")
    def _check_outcome(self) -> Self:
        if (self.findings is None) == (self.error is None):
            raise ValueError("a run holds exactly one of findings and error")
        return self

    @property
    def error_category(self) -> RunErrorCategory | None:
        """The failed run's error category, or None if it returned findings."""
        return None if self.error is None else self.error.category


class Recording(_Strict):
    """What a model returned for a case, with what it was recorded against.

    The four ``*_sha256`` and ``prompt_version`` fields must equal today's values
    (``evals.replay.freshness_fields``), or the recording is stale.
    """

    schema_version: Literal["eval-recording/v1"] = Field(alias="schema")
    case_id: _Text
    kind: RecordingKind
    case_sha256: str
    diff_sha256: str
    prompt_version: str
    prompt_sha256: str
    model: _Text
    sdk_version: str
    recorded_at: AwareDatetime
    runs: Annotated[tuple[RecordedRun, ...], Field(min_length=1)]


@dataclass(frozen=True, slots=True)
class LoadedCase:
    """A valid case, its file, the SHA-256 of that file's bytes, and its diff's path."""

    case: EvalCase
    file: Path
    case_sha256: str
    diff_path: Path

    @property
    def live_recording_path(self) -> Path:
        """Where the case's live recording is, whether or not it exists."""
        return self.file.parent / "recordings" / "live" / "recording.json"


def find_case_files(cases_dir: Path) -> list[Path]:
    """Every ``<case_id>/case.json`` directly under ``cases_dir``, sorted.

    Raises:
        EvalInputError: If ``cases_dir`` isn't a directory, or holds no case.
    """
    if not cases_dir.is_dir():
        raise EvalInputError(
            f"--cases {cases_dir} isn't a directory. Pass the directory holding "
            f"<case_id>/{_CASE_FILE_NAME}."
        )
    case_files = sorted(cases_dir.glob(f"*/{_CASE_FILE_NAME}"))
    if not case_files:
        raise EvalInputError(
            f"--cases {cases_dir} holds no <case_id>/{_CASE_FILE_NAME}. Pass the cases directory."
        )
    return case_files


def load_case(case_file: Path) -> LoadedCase:
    """Read and validate a case file, and check that its diff exists.

    Raises:
        EvalInputError: If the file is unreadable or invalid, its ``id`` isn't its directory's
            name, or its diff is missing.
    """
    raw = _read(case_file)
    case = _validate(EvalCase, raw, case_file, "case file")
    if case.id != case_file.parent.name:
        raise EvalInputError(
            f"{case_file}: id {case.id!r} differs from its directory's name "
            f"{case_file.parent.name!r}. Make them the same."
        )
    diff_path = case_file.parent / case.diff
    if not diff_path.is_file():
        raise EvalInputError(
            f"{case_file}: its diff {case.diff!r} isn't a file at {diff_path}. Fix the path, "
            "which is relative to the case file."
        )
    return LoadedCase(case, case_file, hashlib.sha256(raw).hexdigest(), diff_path)


def load_recording(path: Path) -> Recording:
    """Read and validate a recording file.

    Raises:
        EvalInputError: If the file is unreadable or invalid.
    """
    return _validate(Recording, _read(path), path, "recording file")


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise EvalInputError(f"{path} can't be read: {exc.strerror}. Check the path.") from exc


def _validate[M: BaseModel](model: type[M], raw: bytes, path: Path, description: str) -> M:
    """Validate JSON strictly, naming the file and the first problem's location."""
    try:
        return model.model_validate_json(raw, strict=True)
    except ValidationError as exc:
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error["loc"]) or "top level"
        raise EvalInputError(f"{path}: {location}: {error['msg']}. Fix the {description}.") from exc
