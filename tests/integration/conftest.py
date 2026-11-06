"""Drive the bot through its public interfaces only."""

import errno
import io
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from diff_support import FINDING_DIFF, FINDING_FIELDS
from repo_paths import REPO_ROOT

from review_bot import cli
from review_bot.findings import Finding
from review_bot.providers.claude_agent import ClaudeAgentProvider
from review_bot.providers.mock import MockProvider

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "p0_s1"


@dataclass(frozen=True, slots=True)
class Run:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def stdout_body(self) -> str:
        """Stdout without its final line terminator."""
        return self.stdout.removesuffix("\n")


class Bot:
    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._capsys = capsys

    def parse_args(self, *argv: str) -> tuple[Path, Any]:
        return cli.parse_args(list(argv))

    def mock(self, **kwargs: Any) -> Any:
        return MockProvider(**kwargs)

    def finding(self, **overrides: Any) -> Any:
        """The shared default finding; it prints only over a diff holding its anchor (P1-S6)."""
        return Finding(**(FINDING_FIELDS | overrides))

    def run(self, *argv: str, provider: Any = None) -> Run:
        """Call `main` with exactly these arguments. A `SystemExit` here fails the test."""
        self._capsys.readouterr()
        exit_code = cli.main(list(argv), provider=provider)
        captured = self._capsys.readouterr()
        return Run(exit_code, captured.out, captured.err)

    def review(self, path: Path, *flags: str, provider: Any = None) -> Run:
        """Review `path` with `--provider mock` selected, as the spec's runs do by default."""
        return self.run(str(path), "--provider", "mock", *flags, provider=provider)

    def request_for(self, path: Path, *flags: str, exit_code: int = 0) -> Any:
        """Review `path` and return the one request the provider received.

        A diff with a rule hit, such as the sample diff since P1-S7, exits 1 with no model findings.
        """
        provider = self.mock(findings=[])
        run = self.review(path, *flags, provider=provider)
        assert run.exit_code == exit_code, run.stderr
        assert len(provider.requests) == 1
        return provider.requests[0]


@pytest.fixture
def bot(capsys: pytest.CaptureFixture[str]) -> Bot:
    return Bot(capsys)


@pytest.fixture
def write_diff(tmp_path: Path) -> Callable[..., Path]:
    """Write exact bytes: a `str` is encoded as UTF-8, with no newline translation."""

    def write(content: str | bytes, name: str = "change.diff") -> Path:
        path = tmp_path / name
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return path

    return write


@pytest.fixture
def claude_argv(write_diff: Callable[..., Path]) -> Callable[..., list[str]]:
    """Arguments reviewing `FINDING_DIFF` with `--provider claude`.

    The diff holds `FINDING_FIELDS`' anchor, which `fake_sdk.HIGH_FINDING` uses.
    """
    path = write_diff(FINDING_DIFF)

    def argv(*flags: str) -> list[str]:
        return [str(path), "--provider", "claude", *flags]

    return argv


@pytest.fixture
def claude_provider(claude_argv: Callable[..., list[str]]) -> Callable[..., ClaudeAgentProvider]:
    """A provider over a fake `sdk_call`, with the settings `parse_args` gives for these flags."""

    def build(sdk_call: Any, *flags: str) -> ClaudeAgentProvider:
        _, settings = cli.parse_args(claude_argv(*flags))
        return ClaudeAgentProvider(settings, sdk_call=sdk_call)

    return build


@pytest.fixture
def claude_review(
    bot: Bot,
    claude_argv: Callable[..., list[str]],
    claude_provider: Callable[..., ClaudeAgentProvider],
) -> Callable[..., Run]:
    """Run `main` with `--provider claude` over a fake `sdk_call`."""

    def review(sdk_call: Any, *flags: str) -> Run:
        return bot.run(*claude_argv(*flags), provider=claude_provider(sdk_call, *flags))

    return review


class _FailingRaw(io.RawIOBase):
    broken = True

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        if self.broken:
            raise OSError(errno.ENOSPC, "No space left on device")
        return len(data)


@pytest.fixture
def failing_stream() -> Iterator[Callable[[str], io.TextIOWrapper]]:
    """Make a real text stream that fails on `write`, or only on `flush`, as a full disk does."""
    created: list[tuple[_FailingRaw, io.TextIOWrapper]] = []

    def make(fails_on: str) -> io.TextIOWrapper:
        raw = _FailingRaw()
        on_write = fails_on == "write"
        buffer = io.BufferedWriter(raw, buffer_size=1 if on_write else 8192)
        stream = io.TextIOWrapper(buffer, encoding="utf-8", write_through=on_write)
        created.append((raw, stream))
        return stream

    yield make
    # A stream still failing would report it from its finaliser, during some later test.
    for raw, stream in created:
        raw.broken = False
        stream.close()


@pytest.fixture
def sample_diff() -> Path:
    return REPO_ROOT / "Assignment" / "sample_diff.txt"


@pytest.fixture
def fixture_text() -> Callable[[str], str]:
    """Read a file from `tests/fixtures/p0_s1/` without newline translation."""

    def read(name: str) -> str:
        return (FIXTURES / name).read_bytes().decode("utf-8")

    return read


@pytest.fixture
def golden(fixture_text: Callable[[str], str]) -> Callable[[str], str]:
    """Read a golden file holding one line of expected output, without its terminator."""

    def read(name: str) -> str:
        return fixture_text(name).removesuffix("\n")

    return read
