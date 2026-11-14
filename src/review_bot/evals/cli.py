"""Command line for the evals: ``python -m review_bot.evals replay|live|compare|gate ...``.

Its exit codes are its own (Phase 1 design, Exit codes): 0 pass, 1 fail, 2 usage.
"""

import argparse
import asyncio
import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn, override

from review_bot.cli import read_policy_option
from review_bot.console import keep_library_logs_off_stderr, write_diagnostic, write_output
from review_bot.errors import ConfigError
from review_bot.evals.compare import compare_cases
from review_bot.evals.compare_render import render_compare_json, render_compare_text
from review_bot.evals.formats import EvalInputError
from review_bot.evals.gate import run_gate
from review_bot.evals.gate_render import render_gate_json, render_gate_text
from review_bot.evals.live import LiveRequest, LiveRun, record_live
from review_bot.evals.render import render_live, render_replay
from review_bot.evals.replay import replay_both_modes, replay_cases, require_live
from review_bot.evals.score import CaseResult
from review_bot.governance.policy import BUILTIN_POLICY, Policy
from review_bot.price_flags import add_price_arguments, prices_from_flags
from review_bot.secret_scan import redact
from review_bot.settings import Settings, take_github_token, take_openai_api_key
from review_bot.spend import usd_amount

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
    """Replay, record, compare or gate the cases under ``--cases``; return 0 pass, 1 fail, 2 usage.

    ``compare`` passes when every case with a ``repo`` or a context recording is paired.
    ``gate`` passes when the prompt pins and every suite in ``--suites`` do (``evals.gate``).
    ``live`` records only the cases ``--case`` names, if given, and fails if its dollar budget
    stopped it before every case was recorded.

    Args:
        argv: The arguments; None means the process's own.
        sdk_call: Replaces the real SDK call for ``live``, so tests stay offline. ``replay``
            and ``compare`` never call a model, so they never use it.
    """
    keep_library_logs_off_stderr()
    # Before any provider is built, so the Claude CLI never inherits them.
    take_github_token()
    openai_api_key = take_openai_api_key()
    try:
        args = _parse_args(argv)
        policy = (
            BUILTIN_POLICY
            if args.command == "compare"
            else read_policy_option(args.policy, os.environ)
        )
        output, exit_code = asyncio.run(_run(args, policy, sdk_call, openai_api_key))
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


async def _run(
    args: argparse.Namespace,
    policy: Policy,
    sdk_call: "SdkCall | None",
    openai_api_key: str | None,
) -> tuple[str, int]:
    """The command's report and exit code; all but ``compare`` review under ``policy``."""
    if args.command == "compare":
        return await _compare(args)
    if args.command == "gate":
        return await _gate(args, policy)
    if args.command == "live":
        run = await _record(args, policy, sdk_call, openai_api_key)
        return render_live(run), _PASS if run.passed else _FAIL
    return await _replay(args, policy)


async def _gate(args: argparse.Namespace, policy: Policy) -> tuple[str, int]:
    result = await run_gate(args.cases, args.suites, policy)
    render = render_gate_json if args.format == "json" else render_gate_text
    return render(result), _PASS if result.passed else _FAIL


async def _replay(args: argparse.Namespace, policy: Policy) -> tuple[str, int]:
    results = await replay_cases(
        args.cases, args.recording, mode=args.mode, policy=policy, provider=args.provider
    )
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


async def _record(
    args: argparse.Namespace,
    policy: Policy,
    sdk_call: "SdkCall | None",
    openai_api_key: str | None,
) -> LiveRun:
    request = LiveRequest(
        args.cases,
        runs=args.runs,
        max_calls=args.max_calls,
        mode=args.mode,
        case_ids=tuple(args.case_ids or ()),
        max_usd=args.max_usd,
        max_cost_usd=args.max_cost_usd,
        allow_dispatch=args.allow_dispatch,
        policy=policy,
        provider=args.provider,
        model=args.model,
        prices=prices_from_flags(args.price_input_usd_per_mtok, args.price_output_usd_per_mtok),
    )
    return await record_live(
        request, environ=os.environ, sdk_call=sdk_call, openai_api_key=openai_api_key
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
    _add_provider_argument(replay, "whose live recordings to replay")
    _add_policy_argument(replay)
    live = commands.add_parser(
        "live",
        help="call the real model on every case and record its findings; spends real tokens",
        allow_abbrev=False,
    )
    live.add_argument("--cases", type=Path, required=True, help="the eval cases directory")
    live.add_argument(
        "--mode",
        choices=("diff", "context"),
        default="diff",
        help="diff mode (the default), or context mode, with the tools reading each case's repo",
    )
    live.add_argument(
        "--case",
        dest="case_ids",
        action="append",
        help="a case to run, by its id; repeat it for more; without it, every case runs",
    )
    live.add_argument(
        "--runs", type=_positive_int, required=True, help="how many calls to make per case"
    )
    # This and the two amounts aren't required by the parser, so their absence can be refused
    # with the planned call count.
    live.add_argument(
        "--max-calls", type=_positive_int, help="the most calls allowed in all; required"
    )
    live.add_argument(
        "--max-usd", type=usd_amount, help="the most all calls may cost, in USD; required"
    )
    live.add_argument(
        "--max-cost-usd",
        type=usd_amount,
        help="the most one call may cost, in USD, from 0.01 to 1000; required unless the "
        "policy sets a ceiling, which is then the default and the most it may be",
    )
    live.add_argument(
        "--allow-dispatch",
        action="store_true",
        help="in CI, run only if started by workflow_dispatch; any other CI run still refuses",
    )
    _add_provider_argument(live, "the provider to call")
    live.add_argument(
        "--model",
        help="the model to call (default: REVIEW_BOT_MODEL, else Claude's default); required "
        "for --provider openai",
    )
    add_price_arguments(live)
    _add_policy_argument(live)
    _add_compare_command(commands)
    _add_gate_command(commands)
    args = parser.parse_args(argv)
    if args.command == "replay":
        _check_replay_flags(args)
    return args


def _check_replay_flags(args: argparse.Namespace) -> None:
    if args.recording is not None and args.require_live:
        raise EvalInputError(
            "--recording and --require-live can't be combined: a synthetic recording is never "
            "live evidence. Leave one out."
        )
    if args.require_live and args.provider != "claude":
        raise EvalInputError(
            "--require-live checks for a recording of the default model, Claude's, so it can't "
            f"be combined with --provider {args.provider}. Leave it out, or check that "
            "provider's suite with python -m review_bot.evals gate."
        )


def _add_provider_argument(parser: argparse.ArgumentParser, purpose: str) -> None:
    parser.add_argument(
        "--provider",
        choices=("claude", "openai"),
        default="claude",
        help=f"{purpose} (default: claude)",
    )


def _add_policy_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--policy",
        help="the policy file the reviews are under (default: REVIEW_BOT_POLICY, else the "
        "built-in policy)",
    )


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


def _add_gate_command(commands: "argparse._SubParsersAction[_ArgumentParser]") -> None:
    gate = commands.add_parser(
        "gate",
        help="check the prompt pins, and each model the policy allows on its eval suite, offline",
        allow_abbrev=False,
    )
    gate.add_argument("--cases", type=Path, required=True, help="the eval cases directory")
    gate.add_argument(
        "--suites", type=Path, required=True, help="the suites file, such as tests/evals/gate.toml"
    )
    _add_policy_argument(gate)
    gate.add_argument(
        "--format", choices=("text", "json"), default="text", help="text (default) or json"
    )
