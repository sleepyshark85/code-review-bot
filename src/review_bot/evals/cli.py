"""Command line for the evals: ``python -m review_bot.evals replay|live ...``.

Its exit codes are its own (Phase 1 design, Exit codes): 0 pass, 1 fail, 2 usage.
"""

import argparse
import asyncio
import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn, override

from review_bot.console import keep_library_logs_off_stderr, write_diagnostic, write_output
from review_bot.evals.formats import EvalInputError
from review_bot.evals.live import record_live
from review_bot.evals.render import render_live, render_replay
from review_bot.evals.replay import replay_cases, require_live
from review_bot.evals.score import CaseResult
from review_bot.secret_scan import redact
from review_bot.settings import Settings, take_github_token

if TYPE_CHECKING:
    from review_bot.providers.claude_agent import SdkCall

__all__ = ["main"]

_PASS: Final = 0
_FAIL: Final = 1
_USAGE: Final = 2

_INTERNAL_ERROR_MESSAGE: Final = (
    "review-bot evals: error: internal error. Its text isn't shown, as it can hold model or CLI "
    "output."
)


class _ArgumentParser(argparse.ArgumentParser):
    """Raises instead of exiting, so a usage error reaches ``main`` like a bad case file."""

    @override
    def error(self, message: str) -> NoReturn:
        raise EvalInputError(f"{message}. Run with --help for usage.")


def main(argv: Sequence[str] | None = None, *, sdk_call: "SdkCall | None" = None) -> int:
    """Replay or record every case under ``--cases``; return 0 pass, 1 fail, 2 usage.

    Args:
        argv: The arguments; None means the process's own.
        sdk_call: Replaces the real SDK call for ``live``, so tests stay offline. ``replay``
            never calls a model, so it never uses it.
    """
    keep_library_logs_off_stderr()
    # Before any provider is built, so the Claude CLI never inherits it.
    take_github_token()
    try:
        args = _parse_args(argv)
        results = asyncio.run(_run(args, sdk_call))
    except EvalInputError as exc:
        # Paths come from the command line and case files; redact them like the review's errors.
        write_diagnostic(f"review-bot evals: error: {redact(str(exc))}")
        return _USAGE
    # Any other exception is a bug. Its text can hold model or CLI output, so it is never shown.
    except Exception:  # noqa: BLE001 - see above
        write_diagnostic(_INTERNAL_ERROR_MESSAGE)
        return _USAGE
    if args.command == "replay" and args.require_live:
        default_model = Settings().model
        results = [require_live(result, default_model) for result in results]
    write_output(render_live(results) if args.command == "live" else render_replay(results))
    return _PASS if all(result.passed for result in results) else _FAIL


async def _run(args: argparse.Namespace, sdk_call: "SdkCall | None") -> list[CaseResult]:
    if args.command == "live":
        return await record_live(
            args.cases,
            runs=args.runs,
            max_calls=args.max_calls,
            environ=os.environ,
            sdk_call=sdk_call,
        )
    return await replay_cases(args.cases, args.recording)


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
    live = commands.add_parser(
        "live",
        help="call the real model on every case and record its findings; spends real tokens",
        allow_abbrev=False,
    )
    live.add_argument("--cases", type=Path, required=True, help="the eval cases directory")
    live.add_argument(
        "--runs", type=_positive_int, required=True, help="how many calls to make per case"
    )
    # Not required by the parser, so its absence can be refused with the planned call count.
    live.add_argument(
        "--max-calls", type=_positive_int, help="the most calls allowed in all; required"
    )
    args = parser.parse_args(argv)
    if args.command == "replay" and args.recording is not None and args.require_live:
        raise EvalInputError(
            "--recording and --require-live can't be combined: a synthetic recording is never "
            "live evidence. Leave one out."
        )
    return args


def _positive_int(text: str) -> int:
    if not (text.isascii() and text.isdigit() and int(text) >= 1):
        raise argparse.ArgumentTypeError(f"{text!r} isn't a whole number of at least 1")
    return int(text)
