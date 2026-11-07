r"""P1-S12: accept or reject findings interactively, with labels logged.

Written from the spec's criteria and Interfaces, and design.md's Data flow, Exit codes, Settings
and Audit log sections, before the developer's code existed.

How the terminals are simulated (spec, criteria preamble): `main` runs in-process with
`sys.stdin`, `sys.stdout` and `sys.stderr` replaced by `FakeStream`s. Each says whether it is a
terminal through `isatty`, and has no descriptor, so a console check on a descriptor can't be
made on it. If the developer's terminal check needs a different seam, only `FakeStream` and
`run_bot` change. A shared timeline records every read and write in order, for AC10.
One real child process covers AC5 (`test_p1_s12_ac5_real_process_*`); on POSIX it also gives the
child a real terminal through a pseudo-terminal.

Readings of the spec that the tests rely on:
- A prompt is any stderr text holding `[a]ccept [r]eject` (Interfaces); one is shown per ask.
- "Shown with its fields" (AC1) and "on one line" (AC9): one stderr line holds the rationale, and
  the same line holds the file, line, severity, category and source.
- "One `triage` record per decision" (AC8): the log's records whose `stage` is `triage` are
  exactly the decisions, and each `data` holds exactly the Label fields plus `source`, `model` and
  `prompt_version`.
- Findings are asked about in report order, so the inputs `a` then `r` decide the report's first
  and second findings.
"""

import contextlib
import errno
import importlib
import io
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from diff_support import section
from repo_paths import REPO_ROOT
from test_p0_s3_stream_failures import assert_no_traceback
from test_p1_s4_audit_log import aws_key, octal, only, quoted_section, read_records
from test_p1_s9_dedupe import SWALLOWED_HANDLER
from test_p1_s10_json_output import report

from review_bot import cli
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding, ReviewedFinding, Source
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

TRIAGE = "--triage"
PROMPT = "[a]ccept [r]eject"
ERROR = "review-bot: error: "
JSON = ("--format", "json")
TEXT = ("--format", "text")
MARKDOWN = ("--format", "markdown")
FORMATS = {"text": TEXT, "json": JSON, "markdown": MARKDOWN}
TRIAGE_VARIABLE = "REVIEW_BOT_TRIAGE"
# The `triage` stage's one event (pinned by the main session in review round 1).
LABELLED = "labelled"
NO_FINDINGS_TEXT = "No findings at or above low.\n"
NO_FINDINGS_MARKDOWN = REPO_ROOT / "tests" / "fixtures" / "p1_s11" / "no_findings.golden.md"
LABEL_FIELDS = frozenset(
    {"fingerprint", "label", "severity_before", "severity_after", "rationale_changed"}
)
RECORD_FIELDS = LABEL_FIELDS | {"source", "model", "prompt_version"}
# AC5's bound. The in-process and child-process runs are held to it as the criterion states.
NO_TERMINAL_BOUND_S = 5.0
# Generous: it catches a hang or quadratic work, not a slow CI machine.
BOUND_S = 60.0
RAW_CONTROL = ("Cc", "Zl", "Zp")

APP = "src/app.py"
HANDLER = "src/handler.py"
APP_DIFF = section(APP, "+x\n" * 7)
RULE_DIFF = section(HANDLER, SWALLOWED_HANDLER)
# One rule finding (in `src/handler.py`) and room for AI findings on `src/app.py` lines 1 to 7.
RULE_AND_APP_DIFF = APP_DIFF + RULE_DIFF


class Eof:
    """An answer standing for end of input on stdin."""


EOF = Eof()
CTRL_C = KeyboardInterrupt
Answer = str | Eof | type[BaseException]


# Simulated terminals.


@dataclass
class FakeStream:
    """A standard stream that records into a shared timeline and says whether it's a terminal."""

    name: str
    timeline: list[tuple[str, str]]
    tty: bool
    answers: list[Answer] = field(default_factory=list)
    reads: int = 0
    broken: bool = False
    parts: list[str] = field(default_factory=list)
    encoding: str = "utf-8"
    errors: str = "strict"

    def isatty(self) -> bool:
        return self.tty

    def fileno(self) -> int:
        raise io.UnsupportedOperation("a simulated stream has no descriptor")

    def readable(self) -> bool:
        return self.name == "stdin"

    def writable(self) -> bool:
        return self.name != "stdin"

    def write(self, text: str) -> int:
        if self.broken:
            raise OSError(errno.ENOSPC, "No space left on device")
        self.timeline.append((self.name, text))
        self.parts.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def readline(self, _size: int = -1) -> str:
        self.reads += 1
        self.timeline.append(("read", ""))
        if not self.answers:
            return ""
        answer = self.answers.pop(0)
        if isinstance(answer, Eof):
            return ""
        if isinstance(answer, type):
            raise answer
        return answer + "\n"

    def read(self, _size: int = -1) -> str:
        self.reads += 1
        self.timeline.append(("read", ""))
        return ""

    def readlines(self, _hint: int = -1) -> list[str]:
        self.reads += 1
        self.timeline.append(("read", ""))
        return []

    def __iter__(self) -> Iterator[str]:
        return iter(self.readline, "")

    @property
    def text(self) -> str:
        return "".join(self.parts)


@dataclass(frozen=True, slots=True)
class TriageRun:
    exit_code: int
    stdout: str
    stderr: str
    timeline: list[tuple[str, str]]
    stdin_reads: int
    elapsed_s: float

    @property
    def prompts(self) -> int:
        return self.stderr.count(PROMPT)


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def run_bot(monkeypatch: pytest.MonkeyPatch, log_path: Path) -> Callable[..., TriageRun]:
    """Run `main` over `diff` with `--provider mock`, its log at `log_path`, under fake streams.

    By default stdin and stderr are terminals and stdout is redirected, the case AC5 allows.
    `stdin` replaces the fake stdin with a real stream, such as the null device, and `stdout`
    the fake stdout, such as one that fails.
    """

    def run(  # noqa: PLR0913 - each option is one stream's state, named at the call
        diff: Path | str,
        *flags: str,
        provider: Any,
        answers: Sequence[Answer] = (),
        triage: bool = True,
        stdin_tty: bool = True,
        stderr_tty: bool = True,
        stdout_tty: bool = False,
        stdin: Any = None,
        stderr_broken: bool = False,
        stdout: Any = None,
    ) -> TriageRun:
        timeline: list[tuple[str, str]] = []
        fake_stdin = FakeStream("stdin", timeline, stdin_tty, list(answers))
        fake_stdout = FakeStream("stdout", timeline, stdout_tty)
        stderr = FakeStream("stderr", timeline, stderr_tty, broken=stderr_broken)
        argv = [str(diff), "--provider", "mock", "--audit-log", str(log_path), *flags]
        if triage:
            argv.append(TRIAGE)
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stdin", fake_stdin if stdin is None else stdin)
            patch.setattr(sys, "stdout", fake_stdout if stdout is None else stdout)
            patch.setattr(sys, "stderr", stderr)
            start = time.monotonic()
            exit_code = cli.main(argv, provider=provider)
            elapsed = time.monotonic() - start
        return TriageRun(
            exit_code, fake_stdout.text, stderr.text, timeline, fake_stdin.reads, elapsed
        )

    return run


# Findings, providers and reading the output.


def ai(line: int, rationale: str, *, severity: str = "high", file: str = APP) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,
        category="logic",
        rationale=rationale,
        confidence=0.9,
    )


FIRST = ai(2, "First problem: the cache key ignores the tenant.")
SECOND = ai(5, "Second problem: the retry loop never sleeps.", severity="medium")
THIRD = ai(7, "Third problem: the total is rounded twice.", severity="critical")


def mock(*findings: Finding) -> MockProvider:
    return MockProvider(findings=list(findings))


def text_locations(stdout: str) -> list[str]:
    """Each text report line's leading `file:line`."""
    return [match.group(1) for match in re.finditer(r"^(\S+:\d+) \[", stdout, re.MULTILINE)]


def markdown_locations(stdout: str) -> list[str]:
    """Each Markdown finding item's `file:line` code span."""
    return re.findall(r"^- .*?`([^`]+:\d+)`", stdout, re.MULTILINE)


def locations(stdout: str, fmt: str) -> list[str]:
    if fmt == "json":
        return [f"{f['file']}:{f['line']}" for f in json.loads(stdout)["findings"]]
    return markdown_locations(stdout) if fmt == "markdown" else text_locations(stdout)


def triage_values(run: TriageRun) -> list[Any]:
    return [finding["triage"] for finding in report(run)["findings"]]


def last_run_records(log_path: Path) -> list[dict[str, Any]]:
    """The log's records from its last run: a test's baseline run shares the log."""
    records = read_records(log_path)
    return [record for record in records if record["run_id"] == records[-1]["run_id"]]


def triage_records(log_path: Path) -> list[dict[str, Any]]:
    return [record for record in last_run_records(log_path) if record["stage"] == "triage"]


def raw_controls(text: str) -> list[str]:
    return sorted({c for c in text if c != "\n" and unicodedata.category(c) in RAW_CONTROL})


def stderr_lines_holding(run: TriageRun, fragment: str) -> list[str]:
    return [line for line in run.stderr.splitlines() if fragment in line]


def has_word(line: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", line) is not None


def assert_nothing_asked(run: TriageRun) -> None:
    assert run.stdin_reads == 0, "stdin was read"
    assert PROMPT not in run.stderr, run.stderr
    assert PROMPT not in run.stdout


def triage_module() -> Any:
    """`review_bot.triage`, imported when a test needs it so the rest still collect without it."""
    return importlib.import_module("review_bot.triage")


def reviewed(finding: Finding, fingerprint: str, source: Source = Source.AI) -> ReviewedFinding:
    return ReviewedFinding(finding=finding, source=source, rule=None, fingerprint=fingerprint)


@pytest.fixture
def baseline(run_bot: Callable[..., TriageRun], write_diff: Callable[[str], Path]) -> Any:
    """The JSON report of `RULE_AND_APP_DIFF` with `FIRST` and no triage: the order asked in."""
    run = run_bot(write_diff(RULE_AND_APP_DIFF), *JSON, provider=mock(FIRST), triage=False)
    assert run.exit_code == 1, run.stderr
    findings = report(run)["findings"]
    assert [f["source"] for f in findings] == ["rule", "ai"], findings
    return findings


# AC1: each finding is shown once with its fields; only the accepted one is reported.


def test_p1_s12_ac1_each_finding_is_shown_once_with_its_fields(run_bot, write_diff, baseline):
    run = run_bot(write_diff(RULE_AND_APP_DIFF), provider=mock(FIRST), answers=["a", "r"])

    assert run.exit_code == 1, run.stderr
    assert run.prompts == 2, run.stderr
    for finding in baseline:
        shown = stderr_lines_holding(run, finding["rationale"])
        assert len(shown) == 1, f"{finding['rationale']!r} shown {len(shown)} times: {run.stderr}"
        (line,) = shown
        assert finding["file"] in line
        assert has_word(line, str(finding["line"]))
        assert has_word(line, finding["severity"])
        assert has_word(line, finding["category"])
        assert has_word(line, finding["source"])


@pytest.mark.parametrize("fmt", ["text", "markdown"])
def test_p1_s12_ac1_accept_then_reject_reports_only_the_accepted_one_and_exits_1(
    run_bot, write_diff, baseline, fmt
):
    run = run_bot(
        write_diff(RULE_AND_APP_DIFF), *FORMATS[fmt], provider=mock(FIRST), answers=["a", "r"]
    )

    assert run.exit_code == 1, run.stderr
    accepted = baseline[0]
    assert locations(run.stdout, fmt) == [f"{accepted['file']}:{accepted['line']}"], run.stdout
    assert baseline[1]["rationale"] not in run.stdout


def test_p1_s12_ac1_reject_then_accept_reports_only_the_second(run_bot, write_diff, baseline):
    run = run_bot(write_diff(RULE_AND_APP_DIFF), provider=mock(FIRST), answers=["r", "a"])

    assert run.exit_code == 1, run.stderr
    accepted = baseline[1]
    assert text_locations(run.stdout) == [f"{accepted['file']}:{accepted['line']}"]


# AC2: every finding rejected.


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        ("text", NO_FINDINGS_TEXT),
        ("markdown", NO_FINDINGS_MARKDOWN.read_bytes().decode("utf-8")),
    ],
)
def test_p1_s12_ac2_all_rejected_exits_0_with_the_no_findings_output(
    run_bot, write_diff, fmt, expected
):
    run = run_bot(
        write_diff(APP_DIFF), *FORMATS[fmt], provider=mock(FIRST, SECOND), answers=["r", "r"]
    )

    assert run.exit_code == 0, run.stderr
    assert run.stdout == expected


def test_p1_s12_ac2_all_rejected_json_keeps_them_marked_reject_with_their_rationale(
    run_bot, write_diff
):
    run = run_bot(write_diff(APP_DIFF), *JSON, provider=mock(FIRST, SECOND), answers=["r", "r"])

    assert run.exit_code == 0, run.stderr
    document = report(run)
    findings = document["findings"]
    assert [f["triage"] for f in findings] == ["reject", "reject"]
    assert [f["rationale"] for f in findings] == [FIRST.rationale, SECOND.rationale]
    assert document["counts"]["reported"] == 0
    assert document["counts"]["rejected"] == 2
    assert document["counts"]["undecided"] == 0


# AC3: `triage` in JSON.


def test_p1_s12_ac3_accepted_finding_is_marked_accept(run_bot, write_diff):
    run = run_bot(write_diff(APP_DIFF), *JSON, provider=mock(FIRST, SECOND), answers=["a", "r"])

    assert run.exit_code == 1, run.stderr
    assert triage_values(run) == ["accept", "reject"]
    document = report(run)
    assert document["counts"]["reported"] == 1
    assert document["counts"]["rejected"] == 1


def test_p1_s12_ac3_without_triage_every_finding_is_null(run_bot, write_diff):
    run = run_bot(write_diff(RULE_AND_APP_DIFF), *JSON, provider=mock(FIRST), triage=False)

    assert run.exit_code == 1, run.stderr
    assert triage_values(run) == [None, None]


@pytest.mark.parametrize(
    "answers",
    [["a", "a"], ["r", "a"], ["a", EOF], [CTRL_C]],
    ids=["all_accepted", "mixed", "eof_part_way", "ctrl_c_at_once"],
)
def test_p1_s12_ac3_with_triage_no_finding_is_null(run_bot, write_diff, answers):
    run = run_bot(write_diff(APP_DIFF), *JSON, provider=mock(FIRST, SECOND), answers=answers)

    values = triage_values(run)
    assert len(values) == 2
    assert None not in values, values
    assert set(values) <= {"accept", "reject", "undecided"}


# AC4: an invalid answer asks again and decides nothing.


@pytest.mark.parametrize("invalid", ["x", "", "  ", "ar", "accept?", "q"])
def test_p1_s12_ac4_invalid_answer_asks_again(run_bot, write_diff, invalid):
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(FIRST, SECOND), answers=[invalid, "r", "a"]
    )

    assert run.prompts == 3, run.stderr
    assert run.stdin_reads == 3
    assert triage_values(run) == ["reject", "accept"]
    assert run.exit_code == 1, run.stderr


def test_p1_s12_ac4_x_then_empty_line_still_leaves_the_first_undecided(run_bot, write_diff):
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(FIRST, SECOND), answers=["x", "", "r", "r"]
    )

    assert run.prompts == 4, run.stderr
    assert triage_values(run) == ["reject", "reject"]
    assert run.exit_code == 0, run.stderr


def test_p1_s12_ac4_x_then_eof_leaves_the_finding_undecided(run_bot, write_diff):
    run = run_bot(write_diff(APP_DIFF), *JSON, provider=mock(FIRST, SECOND), answers=["x", EOF])

    assert triage_values(run) == ["undecided", "undecided"]
    assert run.exit_code == 1, run.stderr


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (["A", " r "], ["accept", "reject"]),
        (["a\r", "R\r"], ["accept", "reject"]),
        (["\tA\t", "  R"], ["accept", "reject"]),
    ],
    ids=["upper_and_spaces", "carriage_return", "tabs_and_upper"],
)
def test_p1_s12_ac4_answers_are_matched_after_strip_and_lower(
    run_bot, write_diff, answers, expected
):
    """Pinned in review round 1: an answer is matched after `.strip().lower()`."""
    run = run_bot(write_diff(APP_DIFF), *JSON, provider=mock(FIRST, SECOND), answers=answers)

    assert run.prompts == 2, run.stderr
    assert triage_values(run) == expected


def test_p1_s12_ac4_limits_a_megabyte_answer_asks_again_within_the_bound(run_bot, write_diff):
    run = run_bot(write_diff(APP_DIFF), *JSON, provider=mock(FIRST), answers=["x" * 1_000_000, "r"])

    assert run.elapsed_s < BOUND_S
    assert triage_values(run) == ["reject"]
    assert run.exit_code == 0, run.stderr
    assert "x" * 1000 not in run.stderr, "the invalid answer was echoed back in full"


def test_p1_s12_ac4_run_triage_asks_again_with_the_prompt_each_time():
    prompts: list[str] = []
    answers = iter(["x", "", "a"])

    def read_line(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    triage_module().run_triage(
        [reviewed(FIRST, "0123456789abcdef")], read_line=read_line, write_line=lambda _: None
    )

    assert len(prompts) == 3
    assert all(PROMPT in prompt for prompt in prompts), prompts


# AC5: no terminal exits 2 at once, before reading the diff or calling the provider.


def assert_no_terminal_error(run: TriageRun) -> None:
    assert run.exit_code == 2, run.stderr
    lines = run.stderr.splitlines()
    assert len(lines) == 1, repr(run.stderr)
    assert lines[0].startswith(ERROR), lines
    assert TRIAGE in lines[0], lines
    assert "terminal" in lines[0].lower(), lines
    assert run.stdout == ""


@pytest.mark.parametrize(
    ("stdin_tty", "stderr_tty"),
    [(False, True), (True, False), (False, False)],
    ids=["stdin_not_a_terminal", "stderr_not_a_terminal", "neither"],
)
def test_p1_s12_ac5_without_a_terminal_exits_2_naming_the_flag(
    run_bot, write_diff, stdin_tty, stderr_tty
):
    provider = mock(FIRST)
    run = run_bot(
        write_diff(APP_DIFF),
        provider=provider,
        answers=["a"],
        stdin_tty=stdin_tty,
        stderr_tty=stderr_tty,
    )

    assert_no_terminal_error(run)
    assert run.stdin_reads == 0
    assert provider.requests == []
    assert run.elapsed_s < NO_TERMINAL_BOUND_S


def test_p1_s12_ac5_stdin_from_the_null_device_exits_2(run_bot, write_diff):
    """On Windows `NUL` says it's a terminal through `isatty`, so this needs the console check."""
    provider = mock(FIRST)
    with Path(os.devnull).open(encoding="utf-8") as null:
        run = run_bot(write_diff(APP_DIFF), provider=provider, stdin=null)

    assert_no_terminal_error(run)
    assert provider.requests == []
    assert run.elapsed_s < NO_TERMINAL_BOUND_S


def test_p1_s12_ac5_the_diff_is_not_read(run_bot, tmp_path):
    """A missing diff exits 3 once it's read; the terminal check comes first, so this is 2."""
    provider = mock(FIRST)
    run = run_bot(tmp_path / "missing.diff", provider=provider, stdin_tty=False)

    assert_no_terminal_error(run)
    assert provider.requests == []


@pytest.mark.parametrize("stdout_tty", [False, True], ids=["stdout_redirected", "stdout_tty"])
def test_p1_s12_ac5_terminal_stdin_and_stderr_are_enough(run_bot, write_diff, stdout_tty):
    run = run_bot(write_diff(APP_DIFF), provider=mock(FIRST), answers=["a"], stdout_tty=stdout_tty)

    assert run.exit_code == 1, run.stderr
    assert run.prompts == 1
    assert text_locations(run.stdout) == [f"{APP}:2"]


# AC5, for real: a child process whose stdin isn't a terminal.

PIPE_HELD_OPEN = "pipe_held_open"
PIPE_WITH_ANSWERS = "pipe_with_answers"
NULL_DEVICE = "null_device"
WINDOWS_NUL = "windows_nul"
on_windows = pytest.mark.skipif(sys.platform != "win32", reason="NUL exists only on Windows")
on_posix = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal")


@dataclass(frozen=True, slots=True)
class ChildRun:
    exit_code: int
    stdout: str
    stderr: str
    elapsed_s: float


@contextlib.contextmanager
def child_stdin(kind: str) -> Iterator[Any]:
    if kind in (PIPE_HELD_OPEN, PIPE_WITH_ANSWERS):
        yield subprocess.PIPE
        return
    name = "NUL" if kind == WINDOWS_NUL else os.devnull
    with Path(name).open("rb") as stream:
        yield stream


def read_terminal(master: int) -> str:
    """What a pseudo-terminal's other side has written so far, with its `\\r\\n` undone."""
    import select  # noqa: PLC0415 - POSIX only, and only these tests use it

    chunks = []
    while select.select([master], [], [], 0.5)[0]:
        try:
            chunk = os.read(master, 4096)
        except OSError:  # Linux reports EIO once the other side is closed and drained.
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")


def run_child(  # noqa: PLR0913 - the child's streams, named at the call
    diff: Path,
    log: Path,
    stdin_kind: str,
    *,
    terminal: int | None = None,
    stdin_terminal: bool = False,
    timeout_s: float = NO_TERMINAL_BOUND_S,
) -> ChildRun:
    """Run the CLI with `--triage` in a child, failing the test if it outlives `timeout_s`.

    `terminal`, a pseudo-terminal's descriptor, becomes the child's stderr, and its stdin too with
    `stdin_terminal`. Its output is then read from `master` by the caller.
    """
    command = [
        sys.executable,
        "-m",
        "review_bot",
        str(diff),
        "--provider",
        "mock",
        "--audit-log",
        str(log),
        TRIAGE,
    ]
    with child_stdin(stdin_kind) as stdin:
        start = time.monotonic()
        process = subprocess.Popen(  # noqa: S603 - fixed arguments, our own interpreter
            command,
            cwd=REPO_ROOT,
            stdin=terminal if stdin_terminal else stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if terminal is None else terminal,
        )
        try:
            if stdin_kind == PIPE_WITH_ANSWERS and process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.write(b"a\na\n")
                    process.stdin.flush()
            try:
                exit_code = process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                pytest.fail(f"--triage with stdin {stdin_kind} still ran after {timeout_s} s")
            elapsed = time.monotonic() - start
            assert process.stdout is not None
            stdout = process.stdout.read().decode("utf-8", "replace")
            stderr = "" if process.stderr is None else process.stderr.read().decode("utf-8")
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
    return ChildRun(exit_code, stdout, stderr, elapsed)


def assert_child_refused(child: ChildRun, log: Path) -> None:
    assert child.exit_code == 2, child.stderr
    assert child.elapsed_s < NO_TERMINAL_BOUND_S
    assert child.stdout == ""
    lines = child.stderr.splitlines()
    assert len(lines) == 1, repr(child.stderr)
    assert lines[0].startswith(ERROR), lines
    assert TRIAGE in lines[0]
    assert "terminal" in lines[0].lower()
    assert_no_traceback(child.stderr)
    if log.exists():
        stages = {record["stage"] for record in read_records(log)}
        assert "agent" not in stages, "the provider was reached"
        assert "parse" not in stages, "the diff was read"


@pytest.mark.parametrize(
    "stdin_kind",
    [
        PIPE_HELD_OPEN,
        PIPE_WITH_ANSWERS,
        NULL_DEVICE,
        pytest.param(WINDOWS_NUL, marks=on_windows),
    ],
)
def test_p1_s12_ac5_real_process_without_a_terminal_exits_2_within_5_s(tmp_path, stdin_kind):
    diff = tmp_path / "change.diff"
    diff.write_bytes(RULE_DIFF.encode("utf-8"))
    log = tmp_path / "audit.jsonl"

    child = run_child(diff, log, stdin_kind)

    assert_child_refused(child, log)


@on_posix
@pytest.mark.parametrize("stdin_kind", [PIPE_HELD_OPEN, NULL_DEVICE])
def test_p1_s12_ac5_real_process_with_a_terminal_stderr_but_not_stdin_exits_2(tmp_path, stdin_kind):
    """Only stdin lacks a terminal here, so this is the stdin check, not the stderr one."""
    import pty  # noqa: PLC0415 - POSIX only

    diff = tmp_path / "change.diff"
    diff.write_bytes(RULE_DIFF.encode("utf-8"))
    log = tmp_path / "audit.jsonl"
    master, terminal = pty.openpty()
    try:
        child = run_child(diff, log, stdin_kind, terminal=terminal)
        os.close(terminal)
        terminal = -1
        child = ChildRun(child.exit_code, child.stdout, read_terminal(master), child.elapsed_s)
    finally:
        if terminal >= 0:
            os.close(terminal)
        os.close(master)

    assert_child_refused(child, log)


@on_posix
def test_p1_s12_ac5_real_process_with_terminal_stdin_and_stderr_triages(tmp_path):
    """Control: with a real terminal on stdin and stderr, and stdout a pipe, triage runs."""
    import pty  # noqa: PLC0415 - POSIX only

    diff = tmp_path / "change.diff"
    diff.write_bytes(RULE_DIFF.encode("utf-8"))
    log = tmp_path / "audit.jsonl"
    master, terminal = pty.openpty()
    try:
        os.write(master, b"a\n")
        child = run_child(
            diff, log, NULL_DEVICE, terminal=terminal, stdin_terminal=True, timeout_s=BOUND_S
        )
        os.close(terminal)
        terminal = -1
        shown = read_terminal(master)
    finally:
        if terminal >= 0:
            os.close(terminal)
        os.close(master)

    assert child.exit_code == 1, shown
    assert PROMPT in shown
    assert text_locations(child.stdout) == [f"{HANDLER}:3"]


# AC6: EOF or Ctrl+C part-way stops asking and leaves the rest undecided.


@pytest.mark.parametrize("stop", [EOF, CTRL_C], ids=["eof", "ctrl_c"])
def test_p1_s12_ac6_stopping_part_way_leaves_the_rest_undecided(run_bot, write_diff, stop):
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(FIRST, SECOND, THIRD), answers=["a", stop]
    )

    assert run.exit_code == 1, run.stderr
    assert run.prompts == 2, run.stderr
    assert run.stdin_reads == 2
    document = report(run)
    assert [f["triage"] for f in document["findings"]] == ["accept", "undecided", "undecided"]
    assert document["counts"]["undecided"] == 2
    assert document["counts"]["reported"] == 3
    assert_no_traceback(run.stderr)
    assert "KeyboardInterrupt" not in run.stderr


@pytest.mark.parametrize("stop", [EOF, CTRL_C], ids=["eof", "ctrl_c"])
def test_p1_s12_ac6_undecided_findings_stay_in_the_text_report(run_bot, write_diff, stop):
    run = run_bot(write_diff(APP_DIFF), provider=mock(FIRST, SECOND, THIRD), answers=["r", stop])

    assert run.exit_code == 1, run.stderr
    assert text_locations(run.stdout) == [f"{APP}:5", f"{APP}:7"]
    assert_no_traceback(run.stderr)


@pytest.mark.parametrize("stop", [EOF, CTRL_C], ids=["eof", "ctrl_c"])
def test_p1_s12_ac6_undecided_findings_get_no_label(run_bot, write_diff, log_path, stop):
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(FIRST, SECOND, THIRD), answers=["a", stop]
    )

    assert run.exit_code == 1, run.stderr
    records = triage_records(log_path)
    assert [record["data"]["label"] for record in records] == ["accept"]
    assert records[0]["data"]["fingerprint"] == report(run)["findings"][0]["fingerprint"]


@pytest.mark.parametrize("stop", [EOF, CTRL_C], ids=["eof", "ctrl_c"])
def test_p1_s12_ac6_stopping_at_the_first_prompt_exits_1_with_all_undecided(
    run_bot, write_diff, stop
):
    run = run_bot(write_diff(APP_DIFF), *JSON, provider=mock(FIRST, SECOND), answers=[stop])

    assert run.exit_code == 1, run.stderr
    assert run.prompts == 1
    assert triage_values(run) == ["undecided", "undecided"]
    assert report(run)["counts"]["undecided"] == 2
    assert_no_traceback(run.stderr)


@pytest.mark.parametrize("stop", [EOF, CTRL_C], ids=["eof", "ctrl_c"])
def test_p1_s12_ac6_rejected_then_stopped_still_exits_1(run_bot, write_diff, stop):
    """Interfaces: exit 1 if any reported finding wasn't rejected, including one undecided."""
    run = run_bot(write_diff(APP_DIFF), provider=mock(FIRST, SECOND), answers=["r", stop])

    assert run.exit_code == 1, run.stderr


@pytest.mark.parametrize("stop", [EOF, CTRL_C], ids=["eof", "ctrl_c"])
def test_p1_s12_ac6_the_run_ends_in_the_log_with_exit_1(run_bot, write_diff, log_path, stop):
    run_bot(write_diff(APP_DIFF), provider=mock(FIRST, SECOND), answers=["a", stop])

    assert only(last_run_records(log_path), "run.end")["exit_code"] == 1


def test_p1_s12_ac6_limits_a_broken_stderr_stops_asking_and_reads_nothing(run_bot, write_diff):
    """Nobody can see a prompt on a stderr that fails, so it ends like EOF, without a read."""
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(FIRST, SECOND), answers=["a"], stderr_broken=True
    )

    assert run.exit_code == 1
    assert run.stdin_reads == 0
    assert triage_values(run) == ["undecided", "undecided"]


def test_p1_s12_ac6_run_triage_returns_on_ctrl_c_instead_of_raising():
    def read_line(_prompt: str) -> str:
        raise KeyboardInterrupt

    result = triage_module().run_triage(
        [reviewed(FIRST, "0123456789abcdef"), reviewed(SECOND, "fedcba9876543210")],
        read_line=read_line,
        write_line=lambda _: None,
    )

    assert list(result.labels) == []


# AC7: without `--triage` nothing is asked, whatever the environment says.


@pytest.mark.parametrize("fmt", sorted(FORMATS))
@pytest.mark.parametrize("variable", [None, "true", "1"], ids=["unset", "true", "one"])
def test_p1_s12_ac7_without_the_flag_nothing_reads_stdin(
    run_bot, write_diff, monkeypatch, fmt, variable
):
    if variable is not None:
        monkeypatch.setenv(TRIAGE_VARIABLE, variable)

    run = run_bot(
        write_diff(APP_DIFF), *FORMATS[fmt], provider=mock(FIRST), answers=["r"], triage=False
    )

    assert run.exit_code == 1, run.stderr
    assert_nothing_asked(run)
    assert locations(run.stdout, fmt) == [f"{APP}:2"]


def test_p1_s12_ac7_the_variable_does_not_need_a_terminal(run_bot, write_diff, monkeypatch):
    monkeypatch.setenv(TRIAGE_VARIABLE, "true")

    run = run_bot(
        write_diff(APP_DIFF),
        provider=mock(FIRST),
        triage=False,
        stdin_tty=False,
        stderr_tty=False,
    )

    assert run.exit_code == 1, run.stderr
    assert_nothing_asked(run)


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_p1_s12_ac7_no_findings_with_triage_asks_nothing(run_bot, write_diff, fmt):
    run = run_bot(write_diff(APP_DIFF), *FORMATS[fmt], provider=mock(), answers=["a"])

    assert run.exit_code == 0, run.stderr
    assert_nothing_asked(run)


# AC8: one label record per decision, without rationale text.


def test_p1_s12_ac8_one_record_per_decision_with_the_label_fields(
    run_bot, write_diff, log_path, baseline
):
    run = run_bot(write_diff(RULE_AND_APP_DIFF), provider=mock(FIRST), answers=["a", "r"])

    assert run.exit_code == 1, run.stderr
    start = only(last_run_records(log_path), "run.start")
    records = triage_records(log_path)
    assert [record["event"] for record in records] == [LABELLED, LABELLED]
    labels = [record["data"] for record in records]
    for label, finding, decision in zip(labels, baseline, ["accept", "reject"], strict=True):
        assert set(label) == RECORD_FIELDS, sorted(label)
        assert label["fingerprint"] == finding["fingerprint"]
        assert label["label"] == decision
        assert label["severity_before"] == finding["severity"]
        assert label["severity_after"] == finding["severity"]
        assert label["rationale_changed"] is False
        assert label["source"] == finding["source"]
        assert label["model"] == start["model"]
        assert label["prompt_version"] == start["prompt_version"]


def test_p1_s12_ac8_triage_records_hold_no_rationale_text(run_bot, write_diff, log_path, baseline):
    run_bot(write_diff(RULE_AND_APP_DIFF), provider=mock(FIRST), answers=["a", "r"])

    serialised = [json.dumps(record) for record in triage_records(log_path)]
    assert serialised
    for finding in baseline:
        words = [word for word in finding["rationale"].split() if len(word) > 6]
        for line in serialised:
            assert finding["rationale"] not in line
            assert not [word for word in words if word in line], (words, line)


def test_p1_s12_ac8_records_come_after_the_policy_and_before_the_end(run_bot, write_diff, log_path):
    run_bot(write_diff(APP_DIFF), provider=mock(FIRST, SECOND), answers=["a", "r"])

    stages = [record["stage"] for record in last_run_records(log_path)]
    triage_at = [index for index, stage in enumerate(stages) if stage == "triage"]
    assert triage_at, stages
    assert max(i for i, stage in enumerate(stages) if stage == "policy") < min(triage_at)
    assert stages[-1] == "run"
    assert max(triage_at) < len(stages) - 1


def closed_stream() -> io.StringIO:
    stream = io.StringIO()
    stream.close()
    return stream


@pytest.mark.parametrize("fails_on", ["write", "flush", "closed"])
def test_p1_s12_ac8_labels_are_logged_even_when_stdout_fails(
    run_bot, write_diff, log_path, failing_stream, fails_on
):
    """Labels are recorded right after triage, before the report, so a lost report keeps them."""
    stdout = closed_stream() if fails_on == "closed" else failing_stream(fails_on)
    run = run_bot(write_diff(APP_DIFF), provider=mock(FIRST), answers=["a"], stdout=stdout)

    assert run.exit_code == 14, run.stderr
    assert_no_traceback(run.stderr)
    records = triage_records(log_path)
    assert [(record["event"], record["data"]["label"]) for record in records] == [
        (LABELLED, "accept")
    ]
    assert only(last_run_records(log_path), "run.end")["exit_code"] == 14


def test_p1_s12_ac8_run_triage_returns_a_label_per_decision():
    answers = iter(["a", "r"])
    result = triage_module().run_triage(
        [reviewed(FIRST, "0123456789abcdef"), reviewed(SECOND, "fedcba9876543210")],
        read_line=lambda _: next(answers),
        write_line=lambda _: None,
    )

    labels = list(result.labels)
    assert [label.fingerprint for label in labels] == ["0123456789abcdef", "fedcba9876543210"]
    assert [str(label.label) for label in labels] == ["accept", "reject"]
    assert [str(label.severity_before) for label in labels] == ["high", "medium"]
    assert [str(label.severity_after) for label in labels] == ["high", "medium"]
    assert [label.rationale_changed for label in labels] == [False, False]
    assert [str(label.source) for label in labels] == ["ai", "ai"]


# AC9: hostile text is shown on one line with no raw control characters.

HOSTILE = "\x1b[31m red\nnext"
HOSTILE_PATH = f"src/a{HOSTILE}.py"


def assert_one_clean_line(run: TriageRun, start: str, end: str) -> None:
    assert not raw_controls(run.stderr), raw_controls(run.stderr)
    shown = stderr_lines_holding(run, start)
    assert shown, run.stderr
    assert all(end in line for line in shown), shown


def test_p1_s12_ac9_hostile_rationale_is_shown_on_one_clean_line(run_bot, write_diff):
    finding = ai(2, f"StartMark {HOSTILE} EndMark")
    run = run_bot(write_diff(APP_DIFF), provider=mock(finding), answers=["a"])

    assert run.exit_code == 1, run.stderr
    assert run.prompts == 1
    assert_one_clean_line(run, "StartMark", "EndMark")


def test_p1_s12_ac9_hostile_path_is_shown_on_one_clean_line(run_bot, write_diff):
    diff = write_diff(quoted_section(octal(HOSTILE_PATH), "+x\n"))
    finding = ai(1, "Path rationale.", file=HOSTILE_PATH)
    run = run_bot(diff, provider=mock(finding), answers=["a"])

    assert run.exit_code == 1, run.stderr
    assert run.prompts == 1
    assert_one_clean_line(run, "src/a", ".py")


@pytest.mark.parametrize(
    "hostile",
    ["\r", "\x07\x08", chr(0x2028), chr(0x2029), "\x85", "\x00", "\x7f", "\x1b]0;title\x07"],
    ids=["cr", "bell_backspace", "line_sep", "para_sep", "nel", "nul", "del", "osc_title"],
)
def test_p1_s12_ac9_limits_other_controls_in_a_rationale_stay_inert(run_bot, write_diff, hostile):
    finding = ai(2, f"StartMark {hostile} EndMark")
    run = run_bot(write_diff(APP_DIFF), provider=mock(finding), answers=["a"])

    assert run.exit_code == 1, run.stderr
    assert_one_clean_line(run, "StartMark", "EndMark")


def test_p1_s12_ac9_limits_a_long_rationale_stays_one_line_within_the_bound(run_bot, write_diff):
    rationale = "StartMark " + "\x1b[31m\n" * 20_000 + " EndMark"
    run = run_bot(write_diff(APP_DIFF), provider=mock(ai(2, rationale)), answers=["a"])

    assert run.elapsed_s < BOUND_S
    assert run.exit_code == 1, run.stderr
    assert_one_clean_line(run, "StartMark", "EndMark")


def test_p1_s12_ac9_run_triage_writes_and_prompts_single_clean_lines():
    shown: list[str] = []

    def read_line(prompt: str) -> str:
        shown.append(prompt)
        return "a"

    hostile = Finding.model_construct(
        file=HOSTILE_PATH,
        line=1,
        severity="high",
        category="logic",
        rationale=f"StartMark {HOSTILE} EndMark",
        confidence=0.9,
        rule=None,
    )
    triage_module().run_triage(
        [reviewed(hostile, "0123456789abcdef")], read_line=read_line, write_line=shown.append
    )

    assert shown
    for text in shown:
        assert "\n" not in text.rstrip("\n"), repr(text)
        assert not raw_controls(text.rstrip("\n")), repr(text)


# AC10: prompts on stderr only; stdout holds only the report, after the last decision.


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_p1_s12_ac10_prompts_go_to_stderr_and_the_report_follows_the_last_answer(
    run_bot, write_diff, fmt
):
    run = run_bot(
        write_diff(APP_DIFF), *FORMATS[fmt], provider=mock(FIRST, SECOND), answers=["a", "a"]
    )

    assert run.exit_code == 1, run.stderr
    assert run.prompts == 2
    assert PROMPT not in run.stdout
    last_read = max(i for i, (kind, _) in enumerate(run.timeline) if kind == "read")
    stdout_writes = [i for i, (kind, _) in enumerate(run.timeline) if kind == "stdout"]
    assert stdout_writes, "nothing was written to stdout"
    assert min(stdout_writes) > last_read, "stdout was written before the last decision"


@pytest.mark.parametrize("fmt", ["text", "markdown"])
def test_p1_s12_ac10_all_accepted_stdout_is_the_report_without_triage(run_bot, write_diff, fmt):
    diff = write_diff(RULE_AND_APP_DIFF)
    plain = run_bot(diff, *FORMATS[fmt], provider=mock(FIRST), triage=False)
    triaged = run_bot(diff, *FORMATS[fmt], provider=mock(FIRST), answers=["a", "a"])

    assert triaged.exit_code == plain.exit_code == 1
    assert triaged.stdout == plain.stdout


def test_p1_s12_ac10_json_stdout_is_one_document(run_bot, write_diff):
    run = run_bot(write_diff(APP_DIFF), *JSON, provider=mock(FIRST, SECOND), answers=["a", "r"])

    assert report(run)["findings"]
    assert PROMPT not in run.stdout


def test_p1_s12_ac10_stdout_is_untouched_when_stopped_by_ctrl_c(run_bot, write_diff):
    run = run_bot(write_diff(APP_DIFF), provider=mock(FIRST, SECOND), answers=[CTRL_C])

    assert PROMPT not in run.stdout
    assert text_locations(run.stdout) == [f"{APP}:2", f"{APP}:5"]


# AC11: an earlier failure keeps its own exit code and asks nothing.


def test_p1_s12_ac11_secret_hit_exits_16_and_asks_nothing(run_bot, write_diff):
    provider = mock(FIRST)
    diff = write_diff(section(APP, f'+key = "{aws_key()}"\n'))
    run = run_bot(diff, provider=provider, answers=["a"])

    assert run.exit_code == 16, run.stderr
    assert_nothing_asked(run)
    assert provider.requests == []
    assert run.stdout == ""


def test_p1_s12_ac11_invalid_setting_exits_2_and_asks_nothing(run_bot, write_diff):
    run = run_bot(write_diff(APP_DIFF), "--threshold", "bogus", provider=mock(FIRST), answers=["a"])

    assert run.exit_code == 2, run.stderr
    assert_nothing_asked(run)
    assert "--threshold" in run.stderr


@pytest.mark.parametrize(
    ("category", "code"),
    [(ErrorCategory.AUTH, 8), (ErrorCategory.REFUSAL, 10), (ErrorCategory.INVALID_OUTPUT, 13)],
)
def test_p1_s12_ac11_provider_failure_keeps_its_code_and_asks_nothing(
    run_bot, write_diff, category, code
):
    provider = MockProvider(error=ProviderError(category, "scripted failure"))
    run = run_bot(write_diff(APP_DIFF), provider=provider, answers=["a"])

    assert run.exit_code == code, run.stderr
    assert_nothing_asked(run)
    assert run.stdout == ""


# AC12: findings below the threshold aren't asked about.


def test_p1_s12_ac12_below_threshold_findings_are_not_asked_about(run_bot, write_diff):
    low = ai(5, "Low problem: a name shadows a builtin.", severity="low")
    run = run_bot(
        write_diff(APP_DIFF),
        "--threshold",
        "high",
        *JSON,
        provider=mock(FIRST, low),
        answers=["r", "a"],
    )

    assert run.prompts == 1, run.stderr
    assert run.stdin_reads == 1
    assert low.rationale not in run.stderr
    document = report(run)
    assert [f["triage"] for f in document["findings"]] == ["reject"]
    assert document["counts"]["below_threshold"] == 1
    assert run.exit_code == 0, run.stderr


def test_p1_s12_ac12_only_below_threshold_findings_asks_nothing(run_bot, write_diff):
    low = ai(5, "Low problem: a name shadows a builtin.", severity="low")
    run = run_bot(write_diff(APP_DIFF), "--threshold", "high", provider=mock(low), answers=["a"])

    assert run.exit_code == 0, run.stderr
    assert_nothing_asked(run)


# Attacks beyond the criteria' own examples.


def test_p1_s12_limits_hundreds_of_findings_are_each_asked_once_within_the_bound(
    run_bot, write_diff
):
    count = 300
    diff = write_diff(section(APP, "+x\n" * count))
    findings = [ai(line, f"Problem number {line}.") for line in range(1, count + 1)]
    run = run_bot(diff, *JSON, provider=mock(*findings), answers=["r"] * count)

    assert run.elapsed_s < BOUND_S
    assert run.prompts == count
    assert report(run)["counts"]["rejected"] == count
    assert run.exit_code == 0, run.stderr


def test_p1_s12_limits_answers_after_the_last_finding_are_not_read(run_bot, write_diff):
    run = run_bot(write_diff(APP_DIFF), provider=mock(FIRST), answers=["a", "r", "r"])

    assert run.stdin_reads == 1
    assert run.exit_code == 1, run.stderr
