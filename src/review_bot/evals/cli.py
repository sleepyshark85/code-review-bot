"""Command line for the evals: ``python -m review_bot.evals replay|live|compare ...``.

Its exit codes are its own (Phase 1 design, Exit codes): 0 pass, 1 fail, 2 usage.
"""

import argparse
import asyncio
import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn, override

from review_bot.console import keep_library_logs_off_stderr, write_diagnostic, write_output
from review_bot.errors import ConfigError
from review_bot.evals.compare import compare_cases
from review_bot.evals.compare_render import render_compare_json, render_compare_text
from review_bot.evals.formats import EvalInputError
from review_bot.evals.live import record_live
from review_bot.evals.render import render_live, render_replay
from review_bot.evals.replay import replay_both_modes, replay_cases, require_live
from review_bot.evals.score import CaseResult
from review_bot.price_flags import add_price_arguments, prices_from_flags
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
    """Replay, record or compare every case under ``--cases``; return 0 pass, 1 fail, 2 usage.

    ``compare`` passes when every case with a ``repo`` or a context recording is paired.

    Args:
        argv: The arguments; None means the process's own.
        sdk_call: Replaces the real SDK call for ``live``, so tests stay offline. ``replay``
            and ``compare`` never call a model, so they never use it.
    """
    keep_library_logs_off_stderr()
    # Before any provider is built, so the Claude CLI never inherits it.
    take_github_token()
    try:
        args = _parse_args(argv)
        output, exit_code = asyncio.run(_run(args, sdk_call))
    except (EvalInputError, ConfigError) as exc:
        # Paths come from the command line and case files; redact them like the review's errors.
        write_diagnostic(f"review-bot evals: error: {redact(str(exc))}")
        return _USAGE
    # Any other exception is a bug. Its text can hold model or CLI output, so it is never shown.
    except Exception:  # noqa: BLE001 - see above
        write_diagnostic(_INTERNAL_ERROR_MESSAGE)
        return _USAGE
    write_output(output)
    return exit_code


async def _run(args: argparse.Namespace, sdk_call: "SdkCall | None") -> tuple[str, int]:
    """The command's report and exit code."""
    if args.command == "compare":
        return await _compare(args)
    if args.command == "live":
        results = await _record(args, sdk_call)
        return render_live(results), _exit_code(results)
    return await _replay(args)


async def _replay(args: argparse.Namespace) -> tuple[str, int]:
    results = await replay_cases(args.cases, args.recording, mode=args.mode)
    if args.require_live:
        default_model = Settings().model
        results = [require_live(result, default_model, mode=args.mode) for result in results]
    return render_replay(results), _exit_code(results)


async def _compare(args: argparse.Namespace) -> tuple[str, int]:
    prices = prices_from_flags(args.price_input_usd_per_mtok, args.price_output_usd_per_mtok)
    comparison = compare_cases(await replay_both_modes(args.cases), prices)
    render = render_compare_json if args.format == "json" else render_compare_text
    return render(comparison), _PASS if comparison.passed else _FAIL


def _exit_code(results: Sequence[CaseResult]) -> int:
    return _PASS if all(result.passed for result in results) else _FAIL


async def _record(args: argparse.Namespace, sdk_call: "SdkCall | None") -> list[CaseResult]:
    return await record_live(
        args.cases,
        runs=args.runs,
        max_calls=args.max_calls,
        environ=os.environ,
        sdk_call=sdk_call,
    )


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
        "--mode",
        choices=("diff", "context"),
        default="diff",
        help="the recordings to replay: diff mode (the default), or context mode over each repo",
    )
    replay.add_argument(
        "--recording",
        type=Path,
        help="a synthetic recording of --mode to score in place of its case's live one",
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
    _add_compare_command(commands)
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


def _add_compare_command(commands: "argparse._SubParsersAction[_ArgumentParser]") -> None:
    compare = commands.add_parser(
        "compare",
        help="compare diff mode with context mode on each case's live recordings, offline",
        allow_abbrev=False,
    )
    compare.add_argument("--cases", type=Path, required=True, help="the eval cases directory")
    add_price_arguments(compare)
    compare.add_argument(
        "--format", choices=("text", "json"), default="text", help="text (default) or json"
    )
