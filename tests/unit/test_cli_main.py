import runpy
from pathlib import Path
from typing import override

import pytest

from review_bot.cli import build_provider, main, parse_args
from review_bot.errors import ConfigError, InputError
from review_bot.findings import Severity
from review_bot.providers.base import ReviewRequest, ReviewResponse
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


def test_main_with_injected_provider_needs_no_selection(
    diff_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = MockProvider(findings=[])

    assert main([str(diff_path)], provider=provider) == 0

    assert len(provider.requests) == 1
    assert "canned" not in capsys.readouterr().err
