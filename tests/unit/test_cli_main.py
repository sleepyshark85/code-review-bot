import asyncio
import io
import logging
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TextIO, cast, override

import pytest

from review_bot.cli import build_provider, main, parse_args
from review_bot.errors import ConfigError, ErrorCategory, InputError, ProviderError
from review_bot.findings import Severity
from review_bot.providers.base import ReviewRequest, ReviewResponse, TokenUsage
from review_bot.providers.mock import MockProvider
from review_bot.settings import Settings

CANNED_LINE = (
    "mock/example.py:1 [high/logic] Canned MockProvider finding, not a real review. "
    "(confidence 1.00)\n"
)


@pytest.fixture
def diff_path(tmp_path: Path) -> Path:
    path = tmp_path / "change.diff"
    path.write_text("+print('hello')\n", encoding="utf-8")
    return path


class FailingRawStream(io.RawIOBase):
    @override
    def writable(self) -> bool:
        return True

    @override
    def write(self, _data: object) -> int:
        raise OSError("no space left")


@pytest.fixture
def failing_stream() -> TextIO:
    """Accepts buffered text and fails when it is flushed, like a full disk or closed pipe."""
    return io.TextIOWrapper(io.BufferedWriter(FailingRawStream()), encoding="utf-8")


def test_parse_args_rejects_abbreviated_flags() -> None:
    with pytest.raises(ConfigError, match="--thresh"):
        parse_args(["some.diff", "--thresh", "high"])


def test_parse_args_with_help_prints_usage_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        parse_args(["--help"])

    help_text = capsys.readouterr().out
    assert caught.value.code == 0
    assert "--max-diff-bytes" in help_text
    assert all(severity in help_text for severity in Severity)


def test_build_provider_with_mock_selected_returns_mock_provider() -> None:
    assert isinstance(build_provider(Settings(provider="mock")), MockProvider)


def test_main_with_injected_provider_bypasses_build_provider(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A None entry makes any import of the module raise ImportError, so building Claude would fail.
    monkeypatch.setitem(sys.modules, "review_bot.providers.claude_agent", None)

    exit_code = main([str(diff_path), "--provider", "claude"], provider=MockProvider())

    assert exit_code == 1


def test_main_without_argv_reads_process_arguments(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["review-bot", str(diff_path), "--provider", "mock"])

    assert main() == 1
    assert capsys.readouterr().out == CANNED_LINE


def test_python_m_review_bot_exits_with_mains_code(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["review-bot", str(diff_path), "--provider", "mock"])

    with pytest.raises(SystemExit) as caught:
        runpy.run_module("review_bot", run_name="__main__")

    assert caught.value.code == 1


@pytest.mark.parametrize(
    "argv", [[], ["some.diff", "--provider", "mock", "--nope"], ["some.diff", "--provider"]]
)
def test_main_with_bad_arguments_returns_2_with_error_line(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("review-bot: error: ")


def test_main_with_unmapped_input_error_returns_internal_error_code(
    diff_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class UnmappedError(InputError):
        pass

    class FailingProvider(MockProvider):
        @override
        async def review(self, request: ReviewRequest) -> ReviewResponse:
            raise UnmappedError("unmapped detail")

    assert main([str(diff_path)], provider=FailingProvider()) == 14
    assert "unmapped detail" in capsys.readouterr().err


def test_main_with_unmapped_provider_category_returns_internal_error_code(
    diff_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unmapped = cast(ErrorCategory, "unmapped")

    assert main([str(diff_path)], provider=MockProvider(error=ProviderError(unmapped, "x"))) == 14
    assert capsys.readouterr().err.startswith("review-bot: error: ")


class _CustomBaseError(BaseException):
    pass


@pytest.mark.parametrize(
    "error",
    [
        asyncio.CancelledError(),
        _CustomBaseError("RAW-TEXT"),
        BaseExceptionGroup("RAW-TEXT", [_CustomBaseError("RAW-TEXT")]),
    ],
    ids=["cancelled", "custom", "group"],
)
def test_main_with_base_exception_from_provider_returns_internal_error_code(
    diff_path: Path, capsys: pytest.CaptureFixture[str], error: BaseException
) -> None:
    class RaisingProvider(MockProvider):
        @override
        async def review(self, request: ReviewRequest) -> ReviewResponse:
            raise error

    assert main([str(diff_path)], provider=RaisingProvider()) == 14

    stderr = capsys.readouterr().err
    assert stderr.startswith("review-bot: error: ")
    assert "RAW-TEXT" not in stderr


class _RaisesWhenFreed:
    def __del__(self) -> None:
        raise RuntimeError("SENTINEL")


def test_main_with_built_claude_provider_that_fails_discards_later_unraisable_reports(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reports: list[object] = []
    sys.unraisablehook = reports.append
    failing = MockProvider(error=ProviderError(ErrorCategory.TRANSIENT, "timed out"))
    monkeypatch.setattr("review_bot.cli.build_provider", lambda _settings: failing)

    exit_code = main([str(diff_path), "--provider", "claude"])
    _RaisesWhenFreed()

    assert exit_code == 11
    assert reports == []
    assert capsys.readouterr().err == "review-bot: error: transient: timed out\n"


def test_main_keeps_library_log_records_off_stderr(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # pytest's own root handlers would hide ``logging.lastResort``, which is what a real run hits.
    monkeypatch.setattr(logging.getLogger(), "handlers", [])

    class LoggingProvider(MockProvider):
        @override
        async def review(self, request: ReviewRequest) -> ReviewResponse:
            logging.getLogger("claude_agent_sdk").error("RAW-CLI-OUTPUT")
            return await super().review(request)

    assert main([str(diff_path)], provider=LoggingProvider(findings=[])) == 0
    assert "RAW-CLI-OUTPUT" not in capsys.readouterr().err


def test_main_with_stdout_failing_on_flush_returns_14_and_drops_the_stream(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch, failing_stream: TextIO
) -> None:
    monkeypatch.setattr("sys.stdout", failing_stream)

    assert main([str(diff_path), "--provider", "mock"]) == 14
    # Left in place, the stream would fail again at shutdown and CPython would exit 120.
    assert sys.stdout is None


def test_main_with_help_and_stdout_failing_on_flush_returns_14_and_drops_the_stream(
    monkeypatch: pytest.MonkeyPatch, failing_stream: TextIO
) -> None:
    monkeypatch.setattr("sys.stdout", failing_stream)

    assert main(["--help"]) == 14
    assert sys.stdout is None


def test_main_with_malformed_usage_writes_only_the_error_line(
    diff_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class NoUsageProvider(MockProvider):
        @override
        async def review(self, request: ReviewRequest) -> ReviewResponse:
            return ReviewResponse(findings=(), usage=cast(TokenUsage, None))

    assert main([str(diff_path), "--provider", "mock"], provider=NoUsageProvider()) == 14

    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1


def test_main_with_forged_usage_keeps_the_summary_on_one_escaped_line(
    diff_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    forged = cast(int, "\x1b[31mCLI\nreview-bot: error: forged")

    class ForgedUsageProvider(MockProvider):
        @override
        async def review(self, request: ReviewRequest) -> ReviewResponse:
            return ReviewResponse(findings=(), usage=TokenUsage(0, forged))

    assert main([str(diff_path)], provider=ForgedUsageProvider()) == 0

    stderr = capsys.readouterr().err
    assert "\x1b" not in stderr
    assert len(stderr.splitlines()) == 1


def test_main_with_streams_that_have_no_encoding_still_writes_to_them(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("sys.stderr", stderr)

    assert main([str(diff_path), "--provider", "mock"]) == 1

    assert stdout.getvalue() == CANNED_LINE
    assert "provider=mock" in stderr.getvalue()


def test_main_with_stderr_raising_an_unforeseen_error_still_exits_by_findings(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stderr", SimpleNamespace(encoding="no-such-codec"))

    assert main([str(diff_path), "--provider", "mock"]) == 1
    assert sys.stderr is None


def test_main_with_stderr_failing_still_exits_by_findings_and_drops_the_stream(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch, failing_stream: TextIO
) -> None:
    monkeypatch.setattr("sys.stderr", failing_stream)

    assert main([str(diff_path), "--provider", "mock"]) == 1
    assert sys.stderr is None


def test_main_with_stderr_absent_keeps_the_input_error_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stderr", None)

    assert main([str(tmp_path / "missing.diff"), "--provider", "mock"]) == 3


def test_main_with_injected_provider_uses_it_instead_of_building_one(
    diff_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = MockProvider(findings=[])

    assert main([str(diff_path)], provider=provider) == 0

    assert len(provider.requests) == 1
    assert "canned" not in capsys.readouterr().err
