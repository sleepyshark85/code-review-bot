"""Command line for the evals: ``python -m review_bot.evals replay ...``.

Its exit codes are its own (Phase 1 design, Exit codes): 0 pass, 1 fail, 2 usage.
"""

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Final, NoReturn, override

from review_bot.console import write_diagnostic, write_output
from review_bot.evals.formats import EvalInputError
from review_bot.evals.render import render_replay
from review_bot.evals.replay import replay_cases, require_live
from review_bot.secret_scan import redact
from review_bot.settings import Settings

__all__ = ["main"]

_PASS: Final = 0
_FAIL: Final = 1
_USAGE: Final = 2


class _ArgumentParser(argparse.ArgumentParser):
    """Raises instead of exiting, so a usage error reaches ``main`` like a bad case file."""

    @override
    def error(self, message: str) -> NoReturn:
        raise EvalInputError(f"{message}. Run with --help for usage.")


def main(argv: Sequence[str] | None = None) -> int:
    """Replay every case under ``--cases`` and return the exit code: 0 pass, 1 fail, 2 usage."""
    try:
        args = _parse_args(argv)
        results = asyncio.run(replay_cases(args.cases, args.recording))
    except EvalInputError as exc:
        # Paths come from the command line and case files; redact them like the review's errors.
        write_diagnostic(f"review-bot evals: error: {redact(str(exc))}")
        return _USAGE
    if args.require_live:
        default_model = Settings().model
        results = [require_live(result, default_model) for result in results]
    write_output(render_replay(results))
    return _PASS if all(result.passed for result in results) else _FAIL


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _ArgumentParser(prog="python -m review_bot.evals", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser(
        "replay",
        help="score recorded model responses through today's pipeline, offline",
        allow_abbrev=False,
    )
    replay.add_argument("--cases", type=Path, required=True, help="the eval cases directory")
    replay.add_argument(
        "--recording",
        type=Path,
        help="a synthetic recording to score in place of its case's live one",
    )
    replay.add_argument(
        "--require-live",
        action="store_true",
        help="fail unless every case has a live recording of the default model",
    )
    args = parser.parse_args(argv)
    if args.recording is not None and args.require_live:
        raise EvalInputError(
            "--recording and --require-live can't be combined: a synthetic recording is never "
            "live evidence. Leave one out."
        )
    return args
