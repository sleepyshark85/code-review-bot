"""Command line for pull requests: ``python -m review_bot.pr shadow <diff> --out DIR``.

It uses ``review-bot``'s exit codes, except that findings aren't a failure: a written report
with outcome ``reviewed``, ``blocked_secret`` or ``skipped`` exits 0 (Phase 2 design, Commands
and exit codes).
"""

import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from review_bot.audit import AuditLog, Stage, utc_now
from review_bot.cli import (
    RaisingArgumentParser,
    add_setting_arguments,
    record_failed_end,
    report_failure,
    run_review,
)
from review_bot.console import keep_library_logs_off_stderr, write_diagnostic
from review_bot.errors import ConfigError, ProviderError
from review_bot.pr.shadow import (
    REPORTED_ERRORS,
    ReportedError,
    ShadowReport,
    build_shadow_report,
    render_shadow_json,
    render_shadow_markdown,
)
from review_bot.providers.base import LLMProvider
from review_bot.report import PipelineResult
from review_bot.secret_scan import redact
from review_bot.settings import Settings, load_settings

__all__ = ["main"]

_AUDIT_LOG_VARIABLE: Final = "REVIEW_BOT_AUDIT_LOG"
_AUDIT_LOG_NAME: Final = "audit.jsonl"
_JSON_NAME: Final = "shadow.json"
_MARKDOWN_NAME: Final = "shadow.md"


def main(argv: Sequence[str] | None = None, *, provider: LLMProvider | None = None) -> int:
    """Run one subcommand and return the process exit code.

    Args:
        argv: The arguments; None means the process's own.
        provider: The provider to use; None means ``review_bot.cli.build_provider`` picks it
            from the settings.

    Returns:
        0 for a written report that isn't ``failed``; otherwise ``review-bot``'s code for the
        failure, such as 2 for a usage error or an unwritable ``--out``, 3 for a missing diff,
        4 for one the system can't read, and 7 to 13 for a provider failure, whose report is
        still written.
    """
    keep_library_logs_off_stderr()
    try:
        diff_path, out_dir, settings = _parse_args(sys.argv[1:] if argv is None else argv)
        # Before the diff is read, so an unwritable --out costs no review.
        _require_writable_directory(out_dir)
        audit = AuditLog(settings.audit_log, clock=utc_now)
    except Exception as exc:  # noqa: BLE001 - `report_failure` maps any failure to its code
        return report_failure(exc)
    try:
        return _shadow(diff_path, out_dir, settings, provider, audit)
    except Exception as exc:  # noqa: BLE001 - `report_failure` maps any failure to its code
        exit_code = report_failure(exc)
        record_failed_end(audit, exit_code, exc)
        return exit_code


def _parse_args(argv: Sequence[str]) -> tuple[Path, Path, Settings]:
    """Parse the command line into the diff path, the output directory and the settings.

    The audit log goes in the output directory unless ``REVIEW_BOT_AUDIT_LOG`` names one.

    Raises:
        ConfigError: If an argument, or a setting from the environment, is invalid.
    """
    parser = RaisingArgumentParser(
        prog="python -m review_bot.pr",
        description="Report what the review bot would post on a pull request.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    shadow = commands.add_parser(
        "shadow",
        help="write the comments and summary the bot would post for a diff file, posting nothing",
        description="Each option except --out can also be set with REVIEW_BOT_<OPTION>.",
        allow_abbrev=False,
    )
    shadow.add_argument("diff_path", help="the diff file to review")
    shadow.add_argument(
        "--out", type=Path, required=True, help="the directory to write shadow.json and shadow.md"
    )
    add_setting_arguments(shadow)
    flags = vars(parser.parse_args(argv))
    del flags["command"]
    diff_path, out_dir = Path(flags.pop("diff_path")), flags.pop("out")
    if _AUDIT_LOG_VARIABLE not in os.environ:
        flags["audit_log"] = str(out_dir / _AUDIT_LOG_NAME)
    return diff_path, out_dir, load_settings(flags)


def _require_writable_directory(out_dir: Path) -> None:
    """Create ``out_dir`` if it is missing, and prove a file can be written in it.

    Raises:
        ConfigError: If it can't be created or written.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=out_dir):
            pass
    except OSError as exc:
        raise _unwritable(out_dir, exc) from exc


def _shadow(
    diff_path: Path,
    out_dir: Path,
    settings: Settings,
    provider: LLMProvider | None,
    audit: AuditLog,
) -> int:
    started_at = utc_now()
    review: PipelineResult | ReportedError
    try:
        review = run_review(diff_path, settings, provider, audit)
    except REPORTED_ERRORS as exc:
        review = exc
    shadow = build_shadow_report(
        review,
        settings=settings,
        run_id=audit.run_id,
        started_at=started_at,
        finished_at=utc_now(),
    )
    _write_report(shadow, out_dir)
    if isinstance(review, ProviderError):
        write_diagnostic(_written_line(shadow, out_dir))
        exit_code = report_failure(review)
        record_failed_end(audit, exit_code, review)
        return exit_code
    if not isinstance(review, PipelineResult):
        # The error's own message says what to do; it names paths only redacted.
        write_diagnostic(f"review-bot pr: {redact(str(review))}")
    write_diagnostic(_written_line(shadow, out_dir))
    audit.record(Stage.RUN, "end", {"exit_code": 0, "error": None})
    return 0


def _write_report(shadow: ShadowReport, out_dir: Path) -> None:
    """Write ``shadow.json`` and ``shadow.md`` in ``out_dir``.

    Raises:
        ConfigError: If either can't be written.
    """
    try:
        (out_dir / _JSON_NAME).write_text(
            render_shadow_json(shadow) + "\n", encoding="ascii", newline="\n"
        )
        (out_dir / _MARKDOWN_NAME).write_text(
            render_shadow_markdown(shadow) + "\n", encoding="utf-8", newline="\n"
        )
    except OSError as exc:
        raise _unwritable(out_dir, exc) from exc


def _unwritable(out_dir: Path, exc: OSError) -> ConfigError:
    reason = exc.strerror or type(exc).__name__
    return ConfigError(
        f"Can't write the shadow report in --out {redact(str(out_dir))}: {reason}. Pass a "
        "directory you can write to."
    )


def _written_line(shadow: ShadowReport, out_dir: Path) -> str:
    reason = "" if shadow.reason is None else f" reason={shadow.reason}"
    return (
        f"review-bot pr: wrote {_JSON_NAME} and {_MARKDOWN_NAME} in {redact(str(out_dir))}: "
        f"outcome={shadow.outcome}{reason} comments={len(shadow.comments)} "
        f"withheld={len(shadow.withheld)} run_id={shadow.run_id}"
    )
