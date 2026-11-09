"""The eval case and recording files, read and validated (P1-S14 and P3-S5 Interfaces).

A file that doesn't match its format stops the eval with exit 2, naming the file, so a typo can
never be scored as a pass or a fail. A case's fixture repository is read whole here too, within
its limits, so nothing but plain files and directories can reach the repository built from it.
"""

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    ValidationError,
    model_validator,
)

from review_bot.context.ledger import LineSpan, RefusalReason, ToolCall, ToolName, ToolOutcome
from review_bot.errors import ErrorCategory, ProviderCap, ReviewBotError
from review_bot.findings import Category, Finding, Severity, Source

__all__ = [
    "INTERNAL_ERROR",
    "MAX_REPO_BYTES",
    "MAX_REPO_FILES",
    "Bug",
    "EvalCase",
    "EvalInputError",
    "EvalMode",
    "FixtureFile",
    "Injection",
    "LoadedCase",
    "NoiseBudget",
    "RecordedRun",
    "RecordedSpan",
    "RecordedToolCall",
    "Recording",
    "RecordingKind",
    "RunErrorCategory",
    "find_case_files",
    "load_case",
    "load_recording",
]

_CASE_FILE_NAME: Final = "case.json"
# Git keeps its own data under this name, so no fixture path may hold it, in any letter case.
_GIT_DIR_NAME: Final = ".git"
# A fixture repository's limits (P3-S5 Limits), and a recorded run's, as the tools bound them.
MAX_REPO_FILES: Final = 500
MAX_REPO_BYTES: Final = 2_000_000
_MAX_RECORDED_TOOL_CALLS: Final = 30
_MAX_RECORDED_SPANS: Final = 50
_MAX_RECORDED_LISTED: Final = 200

type _Text = Annotated[str, Field(pattern=r"\S")]
type LineWindow = tuple[PositiveInt, PositiveInt]
type RecordingKind = Literal["live", "synthetic"]
# How a recording's runs reviewed: one turn on the diff, or with the repository tools (Phase 3).
type EvalMode = Literal["diff", "context"]
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

    ``diff`` is relative to the case file, and so is ``repo``, the directory a context review's
    tools read, as one commit; without it they read an empty repository. A run passes if it
    didn't fail with an error and meets the case's ``noise_budget`` and ``injection`` checks
    (``evals.score``); ``min_passing_runs`` of a recording's runs must pass.
    """

    schema_version: Literal["eval-case/v1"] = Field(alias="schema")
    id: _Text
    diff: _Text
    repo: _Text | None = None
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
    cap: ProviderCap | None = None


def _decimal_from_text(value: object) -> Decimal:
    """``value``, a decimal string such as ``"0.21"``, as a ``Decimal``; a JSON number isn't one."""
    if not isinstance(value, str):
        raise ValueError('a cost must be a decimal string, such as "0.21"')  # noqa: TRY004
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{value!r} isn't a decimal number") from exc


# A dollar amount, written as a string so no float rounding reaches it.
type _DecimalText = Annotated[
    Decimal, BeforeValidator(_decimal_from_text), Field(ge=0, allow_inf_nan=False)
]


class _Usage(_Strict):
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cost_usd: _DecimalText | None = None


class RecordedSpan(_Strict):
    """Lines ``first_line`` to ``last_line`` of ``path`` that a tool call returned."""

    path: _Text
    first_line: PositiveInt
    last_line: PositiveInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.first_line > self.last_line:
            raise ValueError(f"span {self.first_line} to {self.last_line} ends before it starts")
        return self


class RecordedToolCall(_Strict):
    """One tool call of a context run: a ledger entry without its input's hash.

    ``reason`` is set exactly when the call was refused.
    """

    tool: ToolName | None
    outcome: ToolOutcome
    reason: RefusalReason | None = None
    spans: Annotated[tuple[RecordedSpan, ...], Field(max_length=_MAX_RECORDED_SPANS)] = ()
    listed: Annotated[tuple[_Text, ...], Field(max_length=_MAX_RECORDED_LISTED)] = ()
    result_bytes: NonNegativeInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.outcome is ToolOutcome.REFUSED) != (self.reason is not None):
            raise ValueError("a tool call has a reason exactly when it was refused")
        return self

    def as_ledger_entry(self) -> ToolCall:
        """The call as the ledger holds it; the input's hash isn't recorded, so it is empty."""
        return ToolCall(
            tool=self.tool,
            outcome=self.outcome,
            reason=self.reason,
            spans=tuple(
                LineSpan(span.path, span.first_line, span.last_line) for span in self.spans
            ),
            listed=self.listed,
            result_bytes=self.result_bytes,
            input_sha256="",
        )


class RecordedRun(_Strict):
    """One call's result: the validated findings it returned, or the error it failed with.

    Exactly one of ``findings`` and ``error`` is set. ``tool_calls`` are a context run's, in the
    order they finished, failed runs included.
    """

    findings: tuple[Finding, ...] | None = None
    error: _RunError | None = None
    usage: _Usage
    warnings: tuple[str, ...]
    tool_calls: Annotated[
        tuple[RecordedToolCall, ...], Field(max_length=_MAX_RECORDED_TOOL_CALLS)
    ] = ()

    @model_validator(mode="after")
    def _check_outcome(self) -> Self:
        if (self.findings is None) == (self.error is None):
            raise ValueError("a run holds exactly one of findings and error")
        return self

    @property
    def error_category(self) -> RunErrorCategory | None:
        """The failed run's error category, or None if it returned findings."""
        return None if self.error is None else self.error.category

    @property
    def cap(self) -> ProviderCap | None:
        """The cap that ended a failed run, or None if no cap did."""
        return None if self.error is None else self.error.cap

    @property
    def cost_usd(self) -> Decimal | None:
        """The run's cost as the provider reported it, or None if it wasn't recorded."""
        return self.usage.cost_usd

    @property
    def ledger(self) -> tuple[ToolCall, ...]:
        """The run's tool calls as ledger entries."""
        return tuple(call.as_ledger_entry() for call in self.tool_calls)


class Recording(_Strict):
    """What a model returned for a case, with what it was recorded against.

    The ``*_sha256`` and ``prompt_version`` fields, and a context recording's ``repo_tree``,
    must equal today's values (``evals.replay.freshness_fields``), or the recording is stale.
    Only a context recording has a ``repo_tree`` and runs with tool calls.
    """

    schema_version: Literal["eval-recording/v1"] = Field(alias="schema")
    case_id: _Text
    kind: RecordingKind
    mode: EvalMode = "diff"
    case_sha256: str
    diff_sha256: str
    prompt_version: str
    prompt_sha256: str
    repo_tree: str | None = None
    model: _Text
    sdk_version: str
    recorded_at: AwareDatetime
    runs: Annotated[tuple[RecordedRun, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _check_mode(self) -> Self:
        if (self.mode == "context") != (self.repo_tree is not None):
            raise ValueError(
                "repo_tree is required for a context recording, and not allowed for diff"
            )
        if self.mode == "diff" and any(run.tool_calls for run in self.runs):
            raise ValueError("a diff recording's runs make no tool calls")
        return self


@dataclass(frozen=True, slots=True)
class FixtureFile:
    """One file of a case's fixture repository: its ``/``-separated path, bytes and mode.

    ``is_executable`` is the owner's execute bit, which git keeps as a file's mode.
    """

    path: str
    content: bytes
    is_executable: bool = False


@dataclass(frozen=True, slots=True)
class LoadedCase:
    """A valid case, its file, the SHA-256 of that file's bytes, its diff's path, and its repo.

    ``repo_files`` are the files of the case's ``repo``, sorted by path; none without one.
    """

    case: EvalCase
    file: Path
    case_sha256: str
    diff_path: Path
    repo_files: tuple[FixtureFile, ...] = ()

    def live_recording_path(self, mode: EvalMode) -> Path:
        """Where the case's live recording of ``mode`` is, whether or not it exists."""
        directory = "live" if mode == "diff" else "live-context"
        return self.file.parent / "recordings" / directory / "recording.json"


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
    """Read and validate a case file, check that its diff exists, and read its repo.

    Raises:
        EvalInputError: If the file is unreadable or invalid, its ``id`` isn't its directory's
            name, its diff is missing, or its repo breaks a rule of ``_read_repo``.
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
    repo_files = () if case.repo is None else _read_repo(case_file, case.repo)
    return LoadedCase(case, case_file, hashlib.sha256(raw).hexdigest(), diff_path, repo_files)


def load_recording(path: Path) -> Recording:
    """Read and validate a recording file.

    Raises:
        EvalInputError: If the file is unreadable or invalid.
    """
    return _validate(Recording, _read(path), path, "recording file")


def _read_repo(case_file: Path, repo: str) -> tuple[FixtureFile, ...]:
    """Every file under the case's ``repo`` directory, sorted by path.

    Raises:
        EvalInputError: Naming the case file, if ``repo`` is absolute, resolves to the case's
            directory or outside it, isn't a directory, holds anything but regular files and
            directories, such as a symbolic link or a Windows junction, has a path part named
            ``.git``, holds more than ``MAX_REPO_FILES`` files or ``MAX_REPO_BYTES`` bytes, or
            can't be read.
    """
    case_dir = case_file.parent
    root = case_dir / repo

    def problem(text: str) -> EvalInputError:
        return EvalInputError(f"{case_file}: its repo {repo!r} {text}. Fix the case's repo.")

    # An anchored path, "/x" or on Windows also "C:x" or "\\x", isn't relative to the case file.
    if Path(repo).anchor:
        raise problem("is an absolute path; give a directory relative to the case file")
    if any(_is_git_dir_name(part) for part in Path(repo).parts):
        raise problem(f"names a {_GIT_DIR_NAME!r} directory, which git keeps for itself")
    try:
        resolved, resolved_case_dir = root.resolve(), case_dir.resolve()
        if resolved == resolved_case_dir or not resolved.is_relative_to(resolved_case_dir):
            raise problem(
                "isn't inside its case directory, after resolving '..' and links; give a "
                "directory under the case's own"
            )
        if not root.is_dir() or _is_reparse_point(root.lstat()):
            raise problem("isn't a directory, relative to the case file")
        files = _read_tree(root, problem)
    except OSError as exc:
        raise problem(f"can't be read: {exc.strerror}") from exc
    return tuple(sorted(files, key=lambda file: file.path))


def _read_tree(root: Path, problem: Callable[[str], EvalInputError]) -> list[FixtureFile]:
    """Read every file under ``root``, stopping at the first entry or limit that fails.

    Raises:
        EvalInputError: Made by ``problem``, as ``_read_repo`` says.
        OSError: If a directory or file can't be read.
    """
    files: list[FixtureFile] = []
    total_bytes = 0
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = f"{prefix}{entry.name}"
                if _is_git_dir_name(entry.name):
                    raise problem(f"holds {path!r}, a name git keeps for itself")
                status = entry.stat(follow_symlinks=False)
                if _is_plain_directory(status):
                    pending.append((Path(entry.path), f"{path}/"))
                    continue
                if _is_reparse_point(status) or not stat.S_ISREG(status.st_mode):
                    raise problem(
                        f"holds {path!r}, which is neither a regular file nor a plain directory "
                        "(a symbolic link, junction or other special file)"
                    )
                if len(files) == MAX_REPO_FILES:
                    raise problem(f"holds more than {MAX_REPO_FILES} files")
                # Read only one byte past the limit, so a huge file costs no more than that.
                with Path(entry.path).open("rb") as handle:
                    content = handle.read(MAX_REPO_BYTES - total_bytes + 1)
                total_bytes += len(content)
                if total_bytes > MAX_REPO_BYTES:
                    raise problem(f"holds more than {MAX_REPO_BYTES:,} bytes")
                is_executable = bool(status.st_mode & stat.S_IXUSR)
                files.append(FixtureFile(path, content, is_executable))
    return files


def _is_git_dir_name(name: str) -> bool:
    return name.casefold() == _GIT_DIR_NAME


def _is_plain_directory(status: os.stat_result) -> bool:
    return stat.S_ISDIR(status.st_mode) and not _is_reparse_point(status)


def _is_reparse_point(status: os.stat_result) -> bool:
    """Whether a status, read without following links, is a link or, on Windows, a junction.

    ``st_file_attributes`` exists only on Windows, where every junction and other reparse point
    carries the attribute.
    """
    attributes: int = getattr(status, "st_file_attributes", 0)
    is_reparse = bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return stat.S_ISLNK(status.st_mode) or is_reparse


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
