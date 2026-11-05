"""Command line: arguments, the one event loop, printing, and errors to exit codes."""

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, NoReturn, TextIO, override

from review_bot.errors import (
    ConfigError,
    DiffEmptyError,
    DiffNotFoundError,
    DiffTooLargeError,
    DiffUnreadableError,
    InputError,
)
from review_bot.findings import Severity
from review_bot.prompt import PROMPT_VERSION
from review_bot.providers.base import LLMProvider, ReviewResponse
from review_bot.providers.mock import MockProvider
from review_bot.render import at_or_above, render_findings
from review_bot.review import review_diff
from review_bot.settings import Settings, load_settings

__all__ = ["build_provider", "main", "parse_args"]

_EXIT_CODES: Final[Mapping[type[InputError], int]] = {
    ConfigError: 2,
    DiffNotFoundError: 3,
    DiffUnreadableError: 4,
    DiffEmptyError: 5,
    DiffTooLargeError: 6,
}

_INTERNAL_ERROR_EXIT_CODE: Final = 14

_CANNED_WARNING: Final = "review-bot: warning: MockProvider output is canned, not a real review."


class _ArgumentParser(argparse.ArgumentParser):
    """Raises instead of exiting, so usage errors reach ``main`` like any other input error."""

    @override
    def error(self, message: str) -> NoReturn:
        raise ConfigError(f"{message}. Run with --help for usage.")


def parse_args(argv: Sequence[str]) -> tuple[Path, Settings]:
    """Parse the command line into the diff path and the settings.

    Raises:
        ConfigError: If an argument, or a setting from the environment, is invalid.
    """
    parser = _ArgumentParser(
        prog="review-bot",
        description="Review a diff file.",
        epilog="Each option can also be set with REVIEW_BOT_<OPTION>, e.g. REVIEW_BOT_MODEL.",
        allow_abbrev=False,
    )
    parser.add_argument("diff_path", help="the diff file to review")
    parser.add_argument("--provider", help="the provider to use: mock")
    parser.add_argument("--model", help="the model to request")
    parser.add_argument("--threshold", help=f"the lowest severity to report: {', '.join(Severity)}")
    parser.add_argument("--max-diff-bytes", help="reject a larger diff file")
    args = vars(parser.parse_args(argv))
    return Path(args.pop("diff_path")), load_settings(args)


def build_provider(settings: Settings) -> LLMProvider:
    """Create the selected provider.

    Raises:
        ConfigError: If no provider is selected: canned findings are never a silent fallback.
    """
    if settings.provider is None:
        raise ConfigError("No provider selected. Pass --provider mock for an offline run.")
    return MockProvider()


def main(argv: Sequence[str] | None = None, *, provider: LLMProvider | None = None) -> int:
    """Run one review and return the process exit code.

    Args:
        argv: The arguments; None means the process's own.
        provider: The provider to use; None means ``build_provider`` picks it from the settings.

    Returns:
        The exit code from the table in ``docs/phases/phase-0/design.md``.
    """
    try:
        diff_path, settings = parse_args(sys.argv[1:] if argv is None else argv)
        selected_provider = build_provider(settings) if provider is None else provider
        response = asyncio.run(review_diff(diff_path, settings, selected_provider))
    except InputError as exc:
        _write_line(sys.stderr, f"review-bot: error: {exc}")
        return _EXIT_CODES.get(type(exc), _INTERNAL_ERROR_EXIT_CODE)

    if settings.provider == "mock":
        _write_line(sys.stderr, _CANNED_WARNING)
    _write_line(sys.stderr, _summarise(response, settings))

    findings = at_or_above(response.findings, settings.threshold)
    _write_line(sys.stdout, render_findings(findings, settings.threshold))
    return 1 if findings else 0


def _summarise(response: ReviewResponse, settings: Settings) -> str:
    return (
        f"provider={settings.provider} model={settings.model} prompt={PROMPT_VERSION} "
        f"input_tokens={response.usage.input_tokens} output_tokens={response.usage.output_tokens}"
    )


def _write_line(stream: TextIO, text: str) -> None:
    # Encoding first means a stdout that can't show a character (Windows consoles, ASCII CI logs)
    # gets an escape instead of raising UnicodeEncodeError.
    encoded = text.encode(stream.encoding, "backslashreplace")
    stream.write(encoded.decode(stream.encoding) + "\n")
