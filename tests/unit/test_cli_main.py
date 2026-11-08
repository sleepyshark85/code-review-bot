import asyncio
import io
import json
import logging
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import TextIO, cast, override

import pytest
from audit_records import data_of, events, read_records
from diff_samples import FINDING_DIFF, FINDING_FIELDS, FINDING_LINE, MINIMAL_DIFF, section

from review_bot.cli import build_provider, main, parse_args
from review_bot.errors import ConfigError, ErrorCategory, InputError, ProviderError
from review_bot.findings import Finding, Severity
from review_bot.prompt import PROMPT_VERSION
from review_bot.providers.base import ReviewRequest, ReviewResponse, TokenUsage
from review_bot.providers.mock import MockProvider
from review_bot.settings import Settings

AWS_KEY = "AK" + "IA" + "ABCDEFGH" + "23456789"
NO_FINDINGS = "No findings at or above low.\n"


@pytest.fixture
def diff_path(tmp_path: Path) -> Path:
    """A diff that `FINDING_FIELDS` anchors to, so a provider returning it exits 1."""
    path = tmp_path / "change.diff"
    path.write_text(FINDING_DIFF, encoding="utf-8")
    return path


def finding_provider() -> MockProvider:
    return MockProvider(findings=[Finding(**FINDING_FIELDS)])


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

    exit_code = main([str(diff_path), "--provider", "claude"], provider=finding_provider())

    assert exit_code == 1


def test_main_without_argv_reads_process_arguments(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["review-bot", str(diff_path), "--provider", "mock"])

    # The mock's canned finding names no file in the diff, so it is dropped (P1-S6).
    assert main() == 0
    assert capsys.readouterr().out == NO_FINDINGS


def test_python_m_review_bot_exits_with_mains_code(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["review-bot", str(diff_path), "--provider", "mock"])

    with pytest.raises(SystemExit) as caught:
        runpy.run_module("review_bot", run_name="__main__")

    assert caught.value.code == 0


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

    assert main([str(diff_path), "--provider", "mock"], provider=finding_provider()) == 14
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

    assert main([str(diff_path), "--provider", "mock"], provider=finding_provider()) == 1

    assert stdout.getvalue() == f"{FINDING_LINE}\n"
    assert "provider=mock" in stderr.getvalue()


def test_main_with_stderr_raising_an_unforeseen_error_still_exits_by_findings(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stderr", SimpleNamespace(encoding="no-such-codec"))

    assert main([str(diff_path), "--provider", "mock"], provider=finding_provider()) == 1
    assert sys.stderr is None


def test_main_with_stderr_failing_still_exits_by_findings_and_drops_the_stream(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch, failing_stream: TextIO
) -> None:
    monkeypatch.setattr("sys.stderr", failing_stream)

    assert main([str(diff_path), "--provider", "mock"], provider=finding_provider()) == 1
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


def test_main_with_every_file_skipped_names_ten_paths_then_the_total(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = [f"docs/{index:02}.md" for index in range(12)]
    diff = tmp_path / "docs.diff"
    diff.write_bytes("".join(section(path) for path in paths).encode("utf-8"))
    provider = MockProvider()

    assert main([str(diff), "--provider", "mock"], provider=provider) == 0

    warning, summary = capsys.readouterr().err.splitlines()
    assert warning == (
        "review-bot: warning: nothing was reviewed: every changed file was skipped: "
        f"{', '.join(paths[:10])} (12 in total)"
    )
    assert " skipped_files=12 model_called=false run_id=" in summary
    assert provider.requests == []


def test_main_with_only_a_deleted_binary_file_names_it_as_deleted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    diff = tmp_path / "binary.diff"
    diff.write_bytes(
        b"diff --git a/x.bin b/x.bin\ndeleted file mode 100644\n"
        b"Binary files a/x.bin and /dev/null differ\n"
    )

    assert main([str(diff), "--provider", "mock"]) == 0

    warning, _summary = capsys.readouterr().err.splitlines()
    assert warning.endswith("skipped: (deleted file) (1 in total)")


def test_main_with_mock_and_every_file_skipped_omits_the_canned_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    diff = tmp_path / "lock.diff"
    diff.write_bytes(section("uv.lock").encode("utf-8"))

    assert main([str(diff), "--provider", "mock"]) == 0

    assert "canned" not in capsys.readouterr().err


def test_main_with_a_reviewed_file_reports_the_skipped_count_and_the_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    diff = tmp_path / "mixed.diff"
    diff.write_bytes((section("README.md") + MINIMAL_DIFF).encode("utf-8"))

    assert main([str(diff)], provider=MockProvider(findings=[])) == 0

    assert " skipped_files=1 model_called=true run_id=" in capsys.readouterr().err


def test_main_with_text_that_is_not_a_diff_returns_15_naming_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    diff = tmp_path / "notes.diff"
    diff.write_bytes(b"+x\n")
    provider = MockProvider()

    assert main([str(diff)], provider=provider) == 15

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"review-bot: error: Diff {diff} can't be parsed. ")
    assert provider.requests == []


def test_main_with_a_secret_in_the_diff_returns_16_with_one_error_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    diff = tmp_path / "secret.diff"
    diff.write_bytes(section("app.py", "+" + "AK" + "IA" + "ABCDEFGH23456789\n").encode("utf-8"))

    assert main([str(diff), "--threshold", "critical"], provider=MockProvider()) == 16

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("review-bot: error: secret: 1 possible secret in the diff (")
    assert captured.err.count("\n") == 1


def make_unwritable(path: Path) -> None:
    path.unlink()
    path.mkdir()


def test_main_ends_the_summary_with_the_logs_run_id_and_records_the_end(
    diff_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(diff_path), "--provider", "mock"], provider=finding_provider()) == 1

    records = read_records(audit_log_in_tmp_path)
    assert capsys.readouterr().err.endswith(f" run_id={records[0]['run_id']}\n")
    assert records[-1]["data"] == {"exit_code": 1, "error": None}


def test_main_with_a_missing_diff_records_only_the_end_with_its_code(
    tmp_path: Path, audit_log_in_tmp_path: Path
) -> None:
    assert main([str(tmp_path / "missing.diff"), "--provider", "mock"]) == 3

    assert events(audit_log_in_tmp_path) == ["run.end"]
    assert data_of(audit_log_in_tmp_path, "run.end") == {
        "exit_code": 3,
        "error": "DiffNotFoundError",
    }


def test_main_with_a_provider_error_records_its_category_at_the_end(
    diff_path: Path, audit_log_in_tmp_path: Path
) -> None:
    provider = MockProvider(error=ProviderError(ErrorCategory.AUTH, "SENTINEL"))

    assert main([str(diff_path)], provider=provider) == 8

    assert data_of(audit_log_in_tmp_path, "run.end") == {"exit_code": 8, "error": "auth"}


def test_main_with_an_unexpected_error_records_its_class_but_not_its_text(
    diff_path: Path, audit_log_in_tmp_path: Path
) -> None:
    class RaisingProvider(MockProvider):
        @override
        async def review(self, request: ReviewRequest) -> ReviewResponse:
            raise RuntimeError("SENTINEL")

    assert main([str(diff_path)], provider=RaisingProvider()) == 14

    assert data_of(audit_log_in_tmp_path, "run.end") == {
        "exit_code": 14,
        "error": "RuntimeError",
    }
    assert "SENTINEL" not in audit_log_in_tmp_path.read_text(encoding="ascii")


@pytest.mark.parametrize(
    "error", [SystemExit("SENTINEL"), KeyboardInterrupt("SENTINEL")], ids=["exit", "interrupt"]
)
def test_main_with_a_review_aborted_by_a_base_exception_records_its_class(
    diff_path: Path, audit_log_in_tmp_path: Path, error: BaseException
) -> None:
    class RaisingProvider(MockProvider):
        @override
        async def review(self, request: ReviewRequest) -> ReviewResponse:
            raise error

    assert main([str(diff_path)], provider=RaisingProvider()) == 14

    assert data_of(audit_log_in_tmp_path, "run.end") == {
        "exit_code": 14,
        "error": type(error).__name__,
    }
    assert "SENTINEL" not in audit_log_in_tmp_path.read_text(encoding="ascii")


def test_main_with_an_unwritable_log_returns_17_before_reading_the_diff_or_calling_the_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = MockProvider()

    exit_code = main(
        [str(tmp_path / "missing.diff"), "--audit-log", str(tmp_path)], provider=provider
    )

    captured = capsys.readouterr()
    assert exit_code == 17
    assert captured.out == ""
    [error] = captured.err.splitlines()
    assert error.startswith(f"review-bot: error: Can't write the audit log {tmp_path}: ")
    assert "--audit-log" in error
    assert provider.requests == []


def test_main_with_the_log_failing_before_stdout_returns_17_with_stdout_empty(
    diff_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class SabotagingProvider(MockProvider):
        @override
        async def review(self, request: ReviewRequest) -> ReviewResponse:
            make_unwritable(audit_log_in_tmp_path)
            return await super().review(request)

    assert main([str(diff_path)], provider=SabotagingProvider()) == 17

    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1


def test_main_with_the_end_failing_after_stdout_returns_17_with_stdout_intact(
    diff_path: Path, audit_log_in_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SabotagingStdout(io.StringIO):
        @override
        def write(self, text: str) -> int:
            make_unwritable(audit_log_in_tmp_path)
            return super().write(text)

    stdout = SabotagingStdout()
    monkeypatch.setattr("sys.stdout", stdout)

    assert main([str(diff_path), "--provider", "mock"], provider=finding_provider()) == 17
    assert stdout.getvalue() == f"{FINDING_LINE}\n"


def test_main_with_a_failed_review_whose_end_cannot_be_recorded_keeps_its_code_and_warns(
    diff_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class SabotagingProvider(MockProvider):
        @override
        async def review(self, request: ReviewRequest) -> ReviewResponse:
            make_unwritable(audit_log_in_tmp_path)
            raise RuntimeError("SENTINEL")

    assert main([str(diff_path)], provider=SabotagingProvider()) == 14

    error, warning = capsys.readouterr().err.splitlines()
    assert error.startswith("review-bot: error: ")
    assert warning.startswith("review-bot: warning: Can't write the audit log ")


@pytest.mark.parametrize(
    ("argv", "exit_code"),
    [
        (["x.diff", f"tmp/{AWS_KEY}.diff"], 2),
        ([f"tmp/{AWS_KEY}.diff", "--provider", "mock"], 3),
    ],
    ids=["stray-argument", "missing-diff"],
)
def test_main_redacts_a_secret_in_a_path_on_the_error_line(
    argv: list[str], exit_code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == exit_code

    err = capsys.readouterr().err
    assert "[REDACTED].diff" in err
    assert AWS_KEY not in err


def test_main_with_every_file_skipped_redacts_each_named_path_on_its_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    end_marker = "-----END RSA " + "PRIVATE KEY-----"
    diff = tmp_path / "docs.diff"
    diff.write_bytes((section(f"docs/{end_marker}.md") + section("notes.md")).encode("utf-8"))

    assert main([str(diff), "--provider", "mock"]) == 0

    warning, _summary = capsys.readouterr().err.splitlines()
    assert warning == (
        "review-bot: warning: nothing was reviewed: every changed file was skipped: "
        "[REDACTED].md, notes.md (2 in total)"
    )


def test_main_with_a_missing_diff_named_by_a_key_marker_keeps_the_whole_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    name = "-----END RSA " + "PRIVATE KEY-----.diff"

    assert main([name, "--provider", "mock"]) == 3

    assert capsys.readouterr().err == (
        "review-bot: error: Diff not found: [REDACTED].diff. Check the path.\n"
    )


def stderr_lines_of_review(
    diff_path: Path, capsys: pytest.CaptureFixture[str], *findings: Finding
) -> list[str]:
    main([str(diff_path)], provider=MockProvider(findings=findings))
    return capsys.readouterr().err.splitlines()


def test_main_with_its_one_finding_dropped_warns_in_the_singular(
    diff_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = stderr_lines_of_review(diff_path, capsys, Finding(**FINDING_FIELDS | {"line": 8}))

    assert lines[0] == (
        "review-bot: warning: the 1 model finding was dropped: its file or line isn't in the diff."
    )
    assert " rule_findings=0 dropped=1 deduped=0 skipped_files=0 " in lines[1]


def test_main_with_some_findings_kept_does_not_warn_about_dropping(
    diff_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dropped = Finding(**FINDING_FIELDS | {"file": "elsewhere.py"})

    lines = stderr_lines_of_review(diff_path, capsys, dropped, Finding(**FINDING_FIELDS))

    assert len(lines) == 1
    assert " rule_findings=0 dropped=1 deduped=0 skipped_files=0 " in lines[0]


def test_main_with_no_findings_proposed_does_not_warn_about_dropping(
    diff_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = stderr_lines_of_review(diff_path, capsys)

    assert len(lines) == 1
    assert " rule_findings=0 dropped=0 deduped=0 skipped_files=0 " in lines[0]


def test_main_counts_rule_findings_and_duplicates_in_the_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    diff = tmp_path / "change.diff"
    diff.write_text(FINDING_DIFF + section("rules.py", "+except E:\n+    pass\n"), encoding="utf-8")
    finding = Finding(**FINDING_FIELDS)

    assert main([str(diff)], provider=MockProvider(findings=[finding, finding])) == 1

    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "rules.py:1 [medium/reliability] Exception swallowed: the handler discards it, so "
        "failures pass silently. Handle it, log it or re-raise it. (confidence 1.00)",
        FINDING_LINE,
    ]
    assert " rule_findings=1 dropped=0 deduped=1 skipped_files=0 " in err


def test_main_with_json_format_writes_one_report_line_whose_run_id_matches_summary_and_log(
    diff_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(diff_path), "--format", "json"], provider=finding_provider()) == 1

    out, err = capsys.readouterr()
    [line] = out.splitlines()
    report = json.loads(line)
    [finding] = report["findings"]
    run_id = read_records(audit_log_in_tmp_path)[0]["run_id"]
    assert report["schema"] == "report/v1"
    assert (finding["file"], finding["line"], finding["source"]) == ("src/app.py", 7, "ai")
    assert report["run_id"] == run_id
    assert err.endswith(f" run_id={run_id}\n")


def test_main_with_json_format_and_no_findings_exits_0_with_an_empty_list(
    diff_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(diff_path), "--format", "json"], provider=MockProvider(findings=[])) == 0

    assert json.loads(capsys.readouterr().out)["findings"] == []


def test_main_with_json_format_and_a_provider_error_writes_nothing_to_stdout(
    diff_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class FailingProvider(MockProvider):
        @override
        async def review(self, request: ReviewRequest) -> ReviewResponse:
            raise ProviderError(ErrorCategory.AUTH, "Not logged in.")

    assert main([str(diff_path), "--format", "json"], provider=FailingProvider()) == 8

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "review-bot: error: auth: Not logged in.\n"


def test_main_with_json_format_and_stdout_failing_returns_14(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch, failing_stream: TextIO
) -> None:
    monkeypatch.setattr("sys.stdout", failing_stream)

    assert main([str(diff_path), "--format", "json"], provider=finding_provider()) == 14


def test_main_with_markdown_format_writes_the_markdown_report(
    diff_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(diff_path), "--format", "markdown"], provider=finding_provider()) == 1

    out = capsys.readouterr().out
    assert out.startswith("## Review\n\n1 reported, ")
    assert "`src/app.py:7`" in out
    assert out.endswith("(confidence 0.90, source: ai)\n")


def test_main_with_markdown_format_and_no_findings_exits_0(
    diff_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(diff_path), "--format", "markdown"], provider=MockProvider(findings=[])) == 0

    assert capsys.readouterr().out == "## Review\n\nNo findings at or above low.\n"


def two_findings_provider() -> MockProvider:
    first = Finding(**{**FINDING_FIELDS, "line": 6, "rationale": "First."})
    second = Finding(**{**FINDING_FIELDS, "rationale": "Second."})
    return MockProvider(findings=[first, second])


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Make stdin and stderr pass as terminals, and return a setter for what stdin will answer."""
    monkeypatch.setattr("review_bot.cli.is_terminal", lambda _stream: True)

    def answer(text: str) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(text))

    return answer


@pytest.mark.parametrize("stream_name", ["stdin", "stderr"])
def test_main_with_triage_and_no_terminal_exits_2_before_reading_the_diff(
    tmp_path: Path,
    audit_log_in_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stream_name: str,
) -> None:
    monkeypatch.setattr(
        "review_bot.cli.is_terminal", lambda stream: stream is not getattr(sys, stream_name)
    )

    exit_code = main([str(tmp_path / "missing.diff"), "--triage"], provider=finding_provider())

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == (
        "review-bot: error: --triage needs a terminal: stdin and stderr must both be attached to "
        "one. Run it from an interactive shell, or leave out --triage.\n"
    )
    assert not audit_log_in_tmp_path.exists()


def test_main_with_triage_reports_only_findings_not_rejected_after_the_prompts(
    diff_path: Path, terminal: Callable[[str], None], capsys: pytest.CaptureFixture[str]
) -> None:
    terminal("a\nr\n")

    exit_code = main([str(diff_path), "--triage"], provider=two_findings_provider())

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == "src/app.py:6 [high/logic] First. (confidence 0.90)\n"
    assert captured.err.startswith(
        "Finding 1 of 2: src/app.py:6 [high/logic] First. (source: ai)\n[a]ccept [r]eject [e]dit: "
    )


def test_main_with_triage_and_every_finding_rejected_exits_0_with_no_findings(
    diff_path: Path, terminal: Callable[[str], None], capsys: pytest.CaptureFixture[str]
) -> None:
    terminal("r\nr\n")

    exit_code = main([str(diff_path), "--triage"], provider=two_findings_provider())

    assert exit_code == 0
    assert capsys.readouterr().out == NO_FINDINGS


def test_main_with_triage_ended_early_exits_1_with_the_rest_undecided(
    diff_path: Path, terminal: Callable[[str], None], capsys: pytest.CaptureFixture[str]
) -> None:
    terminal("r\n")

    exit_code = main(
        [str(diff_path), "--triage", "--format", "json"], provider=two_findings_provider()
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert [finding["triage"] for finding in report["findings"]] == ["reject", "undecided"]
    assert report["counts"]["undecided"] == 1


def test_main_with_triage_logs_one_label_per_decision_without_its_text(
    diff_path: Path, terminal: Callable[[str], None], audit_log_in_tmp_path: Path
) -> None:
    terminal("a\n")

    main([str(diff_path), "--triage", "--provider", "mock"], provider=two_findings_provider())

    records = read_records(audit_log_in_tmp_path)
    first_fingerprint = next(r["data"]["fingerprint"] for r in records if r["stage"] == "verify")
    labels = [record["data"] for record in records if record["stage"] == "triage"]
    assert labels == [
        {
            "fingerprint": first_fingerprint,
            "label": "accept",
            "severity_before": "high",
            "severity_after": "high",
            "rationale_changed": False,
            "source": "ai",
            "model": "claude-opus-5-5",
            "prompt_version": PROMPT_VERSION,
        }
    ]
    assert events(audit_log_in_tmp_path)[-2:] == ["triage.labelled", "run.end"]
    assert "First." not in audit_log_in_tmp_path.read_text(encoding="ascii")


def test_main_without_triage_never_reads_stdin(
    diff_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin = io.StringIO("r\n")
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setenv("REVIEW_BOT_TRIAGE", "true")

    exit_code = main([str(diff_path), "--format", "json"], provider=finding_provider())

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert stdin.tell() == 0
    assert [finding["triage"] for finding in report["findings"]] == [None]


def test_parse_args_help_says_triage_has_no_variable(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--help"])

    help_text = " ".join(capsys.readouterr().out.split())
    assert "--triage" in help_text
    assert "Each option except --triage can also be set with REVIEW_BOT_<OPTION>" in help_text


def test_main_with_triage_and_a_failing_stdout_still_logs_the_labels(
    diff_path: Path,
    terminal: Callable[[str], None],
    audit_log_in_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_stream: TextIO,
) -> None:
    terminal("a\nr\n")
    monkeypatch.setattr("sys.stdout", failing_stream)

    exit_code = main([str(diff_path), "--triage"], provider=two_findings_provider())

    assert exit_code == 14
    labels = [
        r["data"]["label"] for r in read_records(audit_log_in_tmp_path) if r["stage"] == "triage"
    ]
    assert labels == ["accept", "reject"]


def test_main_with_a_diff_that_is_not_utf8_returns_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "latin1.diff"
    path.write_bytes(MINIMAL_DIFF.encode("latin-1") + "+café\n".encode("latin-1"))

    assert main([str(path)], provider=MockProvider(findings=[])) == 4
    assert "is not valid UTF-8" in capsys.readouterr().err


def test_main_over_the_review_byte_cap_exits_6_without_a_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    diff_path = tmp_path / "change.diff"
    diff_path.write_text(MINIMAL_DIFF, encoding="utf-8")
    provider = MockProvider()

    exit_code = main([str(diff_path), "--max-review-bytes", "1"], provider=provider)

    assert exit_code == 6
    assert "--max-review-bytes" in capsys.readouterr().err
    assert provider.requests == []
