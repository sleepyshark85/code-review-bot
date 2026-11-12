"""Unit tests for `run_pipeline`: the secret check, and what each stage records."""

import asyncio
import dataclasses
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from audit_records import data_of, events, read_records
from diff_samples import MINIMAL_DIFF, section
from git_samples import commit, init_repo

from review_bot.audit import AuditLog
from review_bot.context.issues import IssueStatus, LinkedIssue
from review_bot.context.ledger import ToolName
from review_bot.context.tools import RepoRef
from review_bot.errors import (
    ConfigError,
    DiffNotParsableError,
    DiffTooLargeError,
    ErrorCategory,
    ProviderError,
    SecretDetectedError,
)
from review_bot.findings import Category, EvidenceSpan, Finding, RuleId, Severity, Source
from review_bot.fingerprint import fingerprint_for
from review_bot.git import GitRepo
from review_bot.governance.policy import Policy
from review_bot.prompt import CONTEXT_PROMPT_VERSION, PROMPT_VERSION
from review_bot.providers.base import (
    DeniedTool,
    LLMProvider,
    ReviewRequest,
    ReviewResponse,
    TokenUsage,
)
from review_bot.providers.mock import MockProvider
from review_bot.report import PipelineResult
from review_bot.review import ReviewContext, run_pipeline
from review_bot.rules import DomainRule
from review_bot.settings import Settings

AWS_KEY = "AK" + "IA" + "ABCDEFGH" + "23456789"
UNIQUE_TEXT = "unique-text-4f1c"


def run(
    diff_path: Path, log_path: Path, provider: LLMProvider, threshold: Severity = Severity.LOW
) -> PipelineResult:
    audit = AuditLog(log_path, clock=lambda: datetime(2026, 11, 6, tzinfo=UTC))
    settings = Settings(provider="mock", threshold=threshold)
    return asyncio.run(run_pipeline(diff_path, settings, provider, audit))


def write_diff(tmp_path: Path, text: str, name: str = "change.diff") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def review_expecting_secrets(diff_path: Path, log_path: Path) -> tuple[str, MockProvider]:
    provider = MockProvider()
    with pytest.raises(SecretDetectedError) as caught:
        run(diff_path, log_path, provider)
    return str(caught.value), provider


def test_run_pipeline_with_a_secret_names_it_without_the_path_and_calls_no_provider(
    tmp_path: Path,
) -> None:
    diff = write_diff(tmp_path, f"not a diff\n{AWS_KEY}\n", name=f"{AWS_KEY}.diff")

    message, provider = review_expecting_secrets(diff, tmp_path / "audit.jsonl")

    assert message == (
        "secret: 1 possible secret in the diff (aws-access-key at diff line 2), so it was not "
        "sent to the model. Remove it and rotate it if it is real, then run again."
    )
    assert provider.requests == []


def test_run_pipeline_with_seven_secrets_names_the_first_five_and_counts_the_rest(
    tmp_path: Path,
) -> None:
    diff = write_diff(tmp_path, f"+{AWS_KEY}\n" * 7)

    message, _ = review_expecting_secrets(diff, tmp_path / "audit.jsonl")

    named = "; ".join(f"aws-access-key at diff line {line}" for line in range(1, 6))
    assert message == (
        f"secret: 7 possible secrets in the diff ({named}; 2 more), so it was not sent to the "
        "model. Remove them and rotate any that are real, then run again."
    )


def test_run_pipeline_with_a_secret_records_kinds_and_lines_but_not_the_value(
    tmp_path: Path,
) -> None:
    log = tmp_path / "audit.jsonl"
    diff = write_diff(tmp_path, section(f"{AWS_KEY}.py", f"+{AWS_KEY}\n"), name=f"{AWS_KEY}.diff")

    review_expecting_secrets(diff, log)

    assert events(log) == ["run.start", "static.secrets", "agent.skipped"]
    assert data_of(log, "static.secrets") == {
        "hits": [
            {"kind": "aws-access-key", "diff_line": line_number} for line_number in (1, 1, 2, 3, 5)
        ]
    }
    assert data_of(log, "agent.skipped") == {"reason": "secret_detected"}
    assert AWS_KEY not in log.read_text(encoding="ascii")


def test_run_pipeline_records_the_start_with_the_diffs_hash_and_size(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    diff = write_diff(tmp_path, MINIMAL_DIFF.replace("\n", "\r\n"))

    run(diff, log, MockProvider(findings=[]))

    diff_bytes = diff.read_bytes()
    start = data_of(log, "run.start")
    assert start["provider"] == "mock"
    assert start["model"] == Settings().model
    assert start["prompt_version"] == PROMPT_VERSION
    assert start["threshold"] == "low"
    assert start["format"] == "text"
    assert start["diff_sha256"] == hashlib.sha256(diff_bytes).hexdigest()
    assert start["diff_bytes"] == len(diff_bytes)


def test_run_pipeline_records_each_stage_of_a_review_in_order(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"

    run(write_diff(tmp_path, MINIMAL_DIFF), log, MockProvider(findings=[]))

    assert events(log) == ["run.start", "parse.parsed", "static.rules", "agent.called"]
    called = data_of(log, "agent.called")
    assert called["input_tokens"] == called["output_tokens"] == 0
    assert called["duration_s"] >= 0


def test_run_pipeline_lists_a_skipped_file_and_flagged_hunks_without_header_text(
    tmp_path: Path,
) -> None:
    log = tmp_path / "audit.jsonl"
    text = (
        section("README.md")
        + "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        + "@@ -1,1 +1,1 @@\n+fine\n-gone\n"
        + "@@ -5,1 +7,9 @@ HEADER-TEXT\n+short\n"
        + "@@ -9 +nine @@ HEADER-TEXT\n+unreadable\n"
    )

    run(write_diff(tmp_path, text), log, MockProvider(findings=[]))

    assert data_of(log, "parse.parsed") == {
        "files": 2,
        "hunks": 4,
        "skipped_files": [{"file": "README.md", "reason": "docs"}],
        "skipped_files_total": 1,
        "flagged_hunks": [
            {"file": "app.py", "index": 1, "new_start": 7},
            {"file": "app.py", "index": 2, "new_start": None},
        ],
        "flagged_hunks_total": 2,
    }
    assert "HEADER-TEXT" not in log.read_text(encoding="ascii")


def test_run_pipeline_lists_the_first_50_skipped_files_and_flagged_hunks_with_totals(
    tmp_path: Path,
) -> None:
    log = tmp_path / "audit.jsonl"
    docs = "".join(section(f"docs/{index:02}.md") for index in range(51))
    hunks = "".join("@@ -1,9 +1,9 @@\n+x\n" for _ in range(51))
    text = docs + f"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n{hunks}"

    run(write_diff(tmp_path, text), log, MockProvider(findings=[]))

    parsed = data_of(log, "parse.parsed")
    assert parsed["skipped_files"] == [
        {"file": f"docs/{index:02}.md", "reason": "docs"} for index in range(50)
    ]
    assert parsed["flagged_hunks"] == [
        {"file": "app.py", "index": index, "new_start": 1} for index in range(50)
    ]
    assert (parsed["skipped_files_total"], parsed["flagged_hunks_total"]) == (51, 51)


def test_run_pipeline_with_every_file_skipped_records_why_and_calls_no_provider(
    tmp_path: Path,
) -> None:
    log = tmp_path / "audit.jsonl"
    provider = MockProvider()

    result = run(write_diff(tmp_path, section("uv.lock")), log, provider)

    assert not result.model_called
    assert provider.requests == []
    assert events(log) == ["run.start", "parse.parsed", "static.rules", "agent.skipped"]
    assert data_of(log, "agent.skipped") == {"reason": "nothing_to_review"}


def test_run_pipeline_with_a_provider_error_records_its_category(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    provider = MockProvider(error=ProviderError(ErrorCategory.RATE_LIMIT, "SENTINEL"))

    with pytest.raises(ProviderError):
        run(write_diff(tmp_path, MINIMAL_DIFF), log, provider)

    assert events(log) == ["run.start", "parse.parsed", "static.rules", "agent.failed"]
    assert data_of(log, "agent.failed") == {
        "category": "rate_limit",
        "cap": None,
        "diagnostics": [],
    }
    assert "SENTINEL" not in log.read_text(encoding="ascii")


def test_run_pipeline_with_a_cap_error_records_the_cap(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    provider = MockProvider(error=ProviderError(ErrorCategory.PERMANENT, "Capped.", cap="cost"))

    with pytest.raises(ProviderError):
        run(write_diff(tmp_path, MINIMAL_DIFF), log, provider)

    assert data_of(log, "agent.failed")["cap"] == "cost"


class ReportingProvider:
    """Answers with no findings, reporting a cost and the tools its runtime offered."""

    async def review(self, _request: ReviewRequest) -> ReviewResponse:
        return ReviewResponse(
            findings=(),
            usage=TokenUsage(0, 0),
            cost_usd=Decimal("0.1234"),
            offered_tools=("mcp__repo__read", "mcp__repo__grep"),
        )


def test_run_pipeline_returns_and_records_the_reported_cost_and_offered_tools(
    tmp_path: Path,
) -> None:
    log = tmp_path / "audit.jsonl"

    result = run(write_diff(tmp_path, MINIMAL_DIFF), log, ReportingProvider())

    assert result.cost_usd == Decimal("0.1234")
    assert events(log)[-2:] == ["agent.called", "agent.tools"]
    assert data_of(log, "agent.called")["cost_usd"] == "0.1234"
    assert data_of(log, "agent.tools") == {"tools": ["mcp__repo__read", "mcp__repo__grep"]}


def test_run_pipeline_without_a_reported_cost_records_null_and_no_tools(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"

    result = run(write_diff(tmp_path, MINIMAL_DIFF), log, MockProvider(findings=[]))

    assert result.cost_usd is None
    assert data_of(log, "agent.called")["cost_usd"] is None
    assert "agent.tools" not in events(log)


def test_run_pipeline_writes_a_path_that_is_not_utf8_in_gits_quoted_form(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    text = (
        'diff --git "a/notes\\377.md" "b/notes\\377.md"\n'
        '--- "a/notes\\377.md"\n+++ "b/notes\\377.md"\n@@ -0,0 +1,9 @@\n+x\n'
    )

    run(write_diff(tmp_path, text), log, MockProvider())

    parsed = data_of(log, "parse.parsed")
    assert log.read_bytes().isascii()
    assert parsed["skipped_files"] == [
        {"file": '"notes\\377.md"', "reason": "docs", "path_quoted": True}
    ]
    assert parsed["flagged_hunks"] == [
        {"file": '"notes\\377.md"', "index": 0, "new_start": 1, "path_quoted": True}
    ]


def test_run_pipeline_names_a_deleted_file_by_its_old_path(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    text = (
        "diff --git a/docs/old.md b/docs/old.md\ndeleted file mode 100644\n"
        "--- a/docs/old.md\n+++ /dev/null\n@@ -1,9 +0,0 @@\n-x\n" + MINIMAL_DIFF
    )

    run(write_diff(tmp_path, text), log, MockProvider(findings=[]))

    parsed = data_of(log, "parse.parsed")
    assert parsed["skipped_files"] == [{"file": "docs/old.md", "reason": "docs"}]
    assert parsed["flagged_hunks"] == [{"file": "docs/old.md", "index": 0, "new_start": 0}]


def test_run_pipeline_names_a_section_with_no_old_or_new_path_dev_null(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    text = "--- /dev/null\n+++ /dev/null\n@@ -0,0 +1,9 @@\n+x\n"

    run(write_diff(tmp_path, text), log, MockProvider(findings=[]))

    assert data_of(log, "parse.parsed")["flagged_hunks"] == [
        {"file": "/dev/null", "index": 0, "new_start": 1}
    ]


def octal(text: str) -> str:
    """Every byte of ``text`` as a three-digit octal escape, as git writes an unusual path."""
    return "".join(f"\\{byte:03o}" for byte in text.encode("utf-8"))


def quoted_docs_file(name: str) -> str:
    """A new docs file, skipped, whose quoted path spells ``name`` in octal escapes only."""
    path = f"docs/{octal(name)}.md"
    return (
        f'diff --git "a/{path}" "b/{path}"\nnew file mode 100644\n--- /dev/null\n'
        f'+++ "b/{path}"\n@@ -0,0 +1 @@\n+x\n'
    )


def test_run_pipeline_with_a_secret_spelt_in_a_quoted_path_records_and_names_its_lines(
    tmp_path: Path,
) -> None:
    log = tmp_path / "audit.jsonl"

    message, provider = review_expecting_secrets(
        write_diff(tmp_path, quoted_docs_file(AWS_KEY)), log
    )

    named = "; ".join(f"aws-access-key at diff line {line}" for line in (1, 1, 4))
    assert f"3 possible secrets in the diff ({named})" in message
    assert provider.requests == []
    assert events(log) == ["run.start", "static.secrets", "agent.skipped"]
    assert data_of(log, "agent.skipped") == {"reason": "secret_detected"}
    log_text = log.read_text(encoding="ascii")
    assert AWS_KEY not in log_text
    assert octal(AWS_KEY) not in log_text


def test_run_pipeline_with_a_raw_secret_names_only_it_and_not_a_decoded_one(
    tmp_path: Path,
) -> None:
    text = quoted_docs_file(AWS_KEY) + section("app.py", f"+{AWS_KEY}\n")

    message, _ = review_expecting_secrets(write_diff(tmp_path, text), tmp_path / "audit.jsonl")

    assert "1 possible secret in the diff (aws-access-key at diff line 11)" in message


def test_run_pipeline_names_a_decoded_secret_by_its_header_line_despite_an_escaped_line_feed(
    tmp_path: Path,
) -> None:
    text = f'diff --git a/x b/x\nrename to "x\\n{octal(AWS_KEY)}.py"\n'

    message, _ = review_expecting_secrets(write_diff(tmp_path, text), tmp_path / "audit.jsonl")

    assert "(aws-access-key at diff line 2)" in message


def test_run_pipeline_with_text_that_is_not_a_diff_redacts_only_the_path_in_its_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    diff = Path("-----END RSA " + "PRIVATE KEY-----.diff")
    diff.write_bytes(b"+x\n")

    with pytest.raises(DiffNotParsableError) as caught:
        run(diff, tmp_path / "audit.jsonl", MockProvider())

    assert str(caught.value).startswith("Diff [REDACTED].diff can't be parsed. It has no file")


def test_run_pipeline_with_provider_warnings_writes_and_records_each_in_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "audit.jsonl"
    provider = MockProvider(findings=[], warnings=["W1", "W2"])

    result = run(write_diff(tmp_path, MINIMAL_DIFF), log, provider)

    assert result.warnings == ("W1", "W2")
    assert capsys.readouterr().err == "W1\nW2\n"
    assert events(log) == [
        "run.start",
        "parse.parsed",
        "static.rules",
        "agent.called",
        "agent.warning",
        "agent.warning",
    ]
    assert [record["data"] for record in read_records(log)[-2:]] == [{"text": "W1"}, {"text": "W2"}]


def test_run_pipeline_with_no_provider_warnings_writes_and_records_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "audit.jsonl"

    result = run(write_diff(tmp_path, MINIMAL_DIFF), log, MockProvider())

    assert result.warnings == ()
    assert capsys.readouterr().err == ""
    assert "agent.warning" not in events(log)


def test_run_pipeline_records_a_warning_quoting_the_diff_scrubbed_but_shows_it_whole(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "audit.jsonl"
    warning = "the CLI said: total = compute(items)"
    diff = section("app.py", "+total = compute(items)\n")

    run(write_diff(tmp_path, diff), log, MockProvider(warnings=[warning]))

    assert capsys.readouterr().err == f"{warning}\n"
    assert data_of(log, "agent.warning") == {"text": "[removed: quotes the diff]"}


def test_run_pipeline_with_provider_diagnostics_records_them_scrubbed_and_shows_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-sentinel")
    diagnostics = ["d", "auth failed for test-key-sentinel", "echo: total = compute(items)"]
    error = ProviderError(ErrorCategory.AUTH, "m", diagnostics=diagnostics)
    diff = section("app.py", "+total = compute(items)\n")

    with pytest.raises(ProviderError):
        run(write_diff(tmp_path, diff), log, MockProvider(error=error))

    assert data_of(log, "agent.failed") == {
        "category": "auth",
        "cap": None,
        "diagnostics": ["d", "auth failed for [REDACTED]", "[removed: quotes the diff]"],
    }
    assert capsys.readouterr() == ("", "")


def make_finding(
    file: str = "app.py", severity: Severity = Severity.HIGH, rationale: str = "Why."
) -> Finding:
    return Finding(
        file=file,
        line=1,
        severity=severity,
        category=Category.LOGIC,
        rationale=rationale,
        confidence=0.9,
    )


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_run_pipeline_reports_only_findings_in_the_diff_at_or_above_the_threshold(
    tmp_path: Path,
) -> None:
    low = make_finding(severity=Severity.LOW, rationale="Minor.")
    provider = MockProvider(findings=[low, make_finding(file="gone.py"), make_finding("./app.py")])

    result = run(write_diff(tmp_path, MINIMAL_DIFF), tmp_path / "a.jsonl", provider, Severity.HIGH)

    assert [item.finding for item in result.decision.reported] == [make_finding()]
    assert [item.finding for item in result.decision.below_threshold] == [low]
    assert len(result.verification.dropped) == 1


def test_run_pipeline_records_each_proposal_then_each_kept_findings_outcome(
    tmp_path: Path,
) -> None:
    log = tmp_path / "audit.jsonl"
    low = make_finding(severity=Severity.LOW, rationale="Minor.")
    unknown = make_finding(file="gone.py")
    provider = MockProvider(findings=[make_finding(file="b/app.py"), unknown, low])

    run(write_diff(tmp_path, MINIMAL_DIFF), log, provider, Severity.HIGH)

    assert events(log)[-5:] == ["verify.checked"] * 3 + ["policy.decided"] * 2
    records = [record["data"] for record in read_records(log)[-5:]]
    kept = {
        "fingerprint": fingerprint_for(file="app.py", rule="ai:logic", anchor_text="x", ordinal=0),
        "file": "app.py",
        "line": 1,
        "severity": "high",
        "outcome": "kept",
        "reason": None,
        "duplicate_of": None,
        "rationale_sha256": sha256("Why."),
        "evidence_kept": 0,
        "evidence_dropped": 0,
        "suggestion_dropped": None,
    }
    assert records[0] == kept
    assert records[1] == {
        "fingerprint": None,
        "file_sha256": sha256("gone.py"),
        "file_length": len("gone.py"),
        "line": 1,
        "severity": "high",
        "outcome": "dropped",
        "reason": "unknown_file",
        "duplicate_of": None,
        "rationale_sha256": sha256("Why."),
        "evidence_kept": 0,
        "evidence_dropped": 0,
        "suggestion_dropped": None,
    }
    assert records[2] == kept | {
        "fingerprint": fingerprint_for(file="app.py", rule="ai:logic", anchor_text="x", ordinal=1),
        "severity": "low",
        "rationale_sha256": sha256("Minor."),
    }
    assert records[3:] == [
        {"file": "app.py", "line": 1, "outcome": "reported"},
        {"file": "app.py", "line": 1, "outcome": "below_threshold"},
    ]


def test_run_pipeline_records_no_text_of_a_proposal_that_names_no_diff_path(
    tmp_path: Path,
) -> None:
    log = tmp_path / "audit.jsonl"
    file = "SENTINEL-FILE-café.py"
    findings = [make_finding(file), make_finding(rationale="SENTINEL-RATIONALE")]

    run(write_diff(tmp_path, MINIMAL_DIFF), log, MockProvider(findings=findings))

    assert "SENTINEL" not in log.read_text(encoding="ascii")
    unknown = read_records(log)[-3]["data"]
    assert unknown["file_length"] == len(file)
    assert unknown["file_sha256"] == sha256(file)


def test_run_pipeline_with_no_proposals_records_no_verify_or_policy_stage(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"

    result = run(write_diff(tmp_path, MINIMAL_DIFF), log, MockProvider(findings=[]))

    assert result.verification.checks == ()
    assert result.decision.reported == ()
    assert not any(event.startswith(("verify.", "policy.")) for event in events(log))


SWALLOWING_DIFF = section("app.py", "+try:\n+    x()\n+except E:\n+    pass\n")


def test_run_pipeline_records_each_rule_finding_and_the_unanchored_hits(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    unanchored = (
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +x @@\n+except E:\n+    pass\n"
    )

    run(write_diff(tmp_path, SWALLOWING_DIFF + unanchored), log, MockProvider(findings=[]))

    assert data_of(log, "static.rules") == {
        "findings": [
            {
                "fingerprint": fingerprint_for(
                    file="app.py", rule="swallowed-exception", anchor_text="except E:", ordinal=0
                ),
                "rule": "swallowed-exception",
                "file": "app.py",
                "line": 3,
                "severity": "medium",
            }
        ],
        "unanchored_hits": 1,
        "unchecked_lines": 0,
    }


def test_run_pipeline_reports_rule_findings_before_model_findings(tmp_path: Path) -> None:
    provider = MockProvider(findings=[make_finding()])

    result = run(write_diff(tmp_path, SWALLOWING_DIFF), tmp_path / "a.jsonl", provider)

    assert [(item.source, item.finding.line) for item in result.decision.reported] == [
        (Source.RULE, 3),
        (Source.AI, 1),
    ]


def test_run_pipeline_records_a_duplicate_with_the_fingerprint_it_repeats(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    provider = MockProvider(findings=[make_finding(), make_finding()])

    result = run(write_diff(tmp_path, MINIMAL_DIFF), log, provider)

    [kept, duplicate] = [
        record["data"] for record in read_records(log) if record["stage"] == "verify"
    ]
    assert (duplicate["outcome"], duplicate["fingerprint"], duplicate["reason"]) == (
        "duplicate",
        None,
        None,
    )
    assert (
        duplicate["duplicate_of"] == kept["fingerprint"] == result.verification.kept[0].fingerprint
    )
    assert events(log).count("policy.decided") == 1


def review_with_cap(tmp_path: Path, diff: str, cap: int) -> tuple[PipelineResult, MockProvider]:
    provider = MockProvider(findings=[])
    audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: datetime(2026, 11, 6, tzinfo=UTC))
    settings = Settings(provider="mock", max_review_bytes=cap)
    result = asyncio.run(run_pipeline(write_diff(tmp_path, diff), settings, provider, audit))
    return result, provider


def test_run_pipeline_counts_only_files_not_skipped_against_the_review_byte_cap(
    tmp_path: Path,
) -> None:
    docs = section("README.md", "+" + "x" * 5_000 + "\n")
    code = section("app.py", "+print(1)\n")

    result, provider = review_with_cap(tmp_path, docs + code, 1_000)

    assert result.model_called
    assert len(provider.requests) == 1


def test_run_pipeline_over_the_review_byte_cap_records_the_skip_and_calls_no_provider(
    tmp_path: Path,
) -> None:
    code = section("app.py", "+" + "x" * 2_000 + "\n")

    with pytest.raises(DiffTooLargeError, match=r"over the limit of 1000\. .*--max-review-bytes"):
        review_with_cap(tmp_path, code, 1_000)

    assert data_of(tmp_path / "audit.jsonl", "agent.skipped") == {"reason": "too_large"}


def context_repo(tmp_path: Path) -> ReviewContext:
    """A commit holding `a.py`, which `MINIMAL_DIFF` doesn't change."""
    repo = init_repo(tmp_path / "repo")
    sha = commit(repo, {"a.py": f"{UNIQUE_TEXT}\n"})
    return ReviewContext(repo=RepoRef(git=GitRepo(repo, timeout_s=60), commit=sha))


def run_with_context(
    tmp_path: Path, provider: MockProvider, context: ReviewContext, **settings: int
) -> PipelineResult:
    audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: datetime(2026, 11, 9, tzinfo=UTC))
    diff = write_diff(tmp_path, MINIMAL_DIFF)
    return asyncio.run(
        run_pipeline(diff, Settings(provider="mock", **settings), provider, audit, context=context)
    )


def test_run_pipeline_with_context_sends_the_context_prompt_and_tools(tmp_path: Path) -> None:
    provider = MockProvider(findings=[], tool_calls=[("read", {"path": "a.py"})])

    result = run_with_context(tmp_path, provider, context_repo(tmp_path))

    [request] = provider.requests
    assert request.tools is not None
    assert [definition.name for definition in request.tools.definitions] == [
        "read",
        "grep",
        "glob",
    ]
    assert f"1\t{UNIQUE_TEXT}" in provider.tool_results[0]
    assert result.prompt_version == CONTEXT_PROMPT_VERSION
    assert [call.tool for call in result.ledger] == ["read"]


def test_run_pipeline_with_context_names_the_commit_and_records_the_prompt_version(
    tmp_path: Path,
) -> None:
    context = context_repo(tmp_path)

    result = run_with_context(tmp_path, MockProvider(findings=[]), context)

    assert result.context_commit == context.repo.commit
    assert (
        data_of(tmp_path / "audit.jsonl", "run.start")["prompt_version"] == CONTEXT_PROMPT_VERSION
    )


def test_run_pipeline_with_context_caps_tool_calls_from_the_settings(tmp_path: Path) -> None:
    provider = MockProvider(findings=[], tool_calls=[("glob", {"pattern": "*"})] * 2)

    result = run_with_context(tmp_path, provider, context_repo(tmp_path), max_tool_calls=1)

    assert [call.reason for call in result.ledger] == [None, "call_limit"]


def test_run_pipeline_records_each_tool_call_without_its_arguments_or_result(
    tmp_path: Path,
) -> None:
    calls = [("read", {"path": "a.py"}), ("grep", {"literal": UNIQUE_TEXT}), ("nope", {})]
    provider = MockProvider(findings=[], tool_calls=calls)

    run_with_context(tmp_path, provider, context_repo(tmp_path))

    log = tmp_path / "audit.jsonl"
    records = [r["data"] for r in read_records(log) if r["stage"] == "context"]
    assert [(r["tool"], r["outcome"], r["reason"]) for r in records] == [
        ("read", "ok", None),
        ("grep", "ok", None),
        (None, "refused", "unknown_tool"),
    ]
    assert records[0]["spans"] == [{"path": "a.py", "first_line": 1, "last_line": 1}]
    assert records[0]["spans_total"] == 1
    assert all(len(r["input_sha256"]) == 64 and r["result_bytes"] > 0 for r in records)
    assert UNIQUE_TEXT not in log.read_text(encoding="ascii")
    assert events(log)[-1] == "context.tool_call"


def test_run_pipeline_records_tool_calls_even_when_the_provider_then_fails(
    tmp_path: Path,
) -> None:
    provider = MockProvider(
        error=ProviderError(ErrorCategory.TRANSIENT, "x"), tool_calls=[("glob", {"pattern": "*"})]
    )

    with pytest.raises(ProviderError):
        run_with_context(tmp_path, provider, context_repo(tmp_path))

    assert "context.tool_call" in events(tmp_path / "audit.jsonl")


def test_run_pipeline_with_every_file_skipped_still_names_the_context_commit(
    tmp_path: Path,
) -> None:
    context = context_repo(tmp_path)
    audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: datetime(2026, 11, 9, tzinfo=UTC))
    diff = write_diff(tmp_path, section("package-lock.json"))
    settings = Settings(provider="mock")

    result = asyncio.run(run_pipeline(diff, settings, MockProvider(), audit, context=context))

    assert not result.model_called
    assert result.context_commit == context.repo.commit
    assert result.prompt_version == CONTEXT_PROMPT_VERSION


def test_run_pipeline_without_context_sends_no_tools(tmp_path: Path) -> None:
    provider = MockProvider(findings=[], tool_calls=[("read", {"path": "a.py"})])

    result = run(write_diff(tmp_path, MINIMAL_DIFF), tmp_path / "audit.jsonl", provider)

    assert provider.requests[0].tools is None
    assert result.context_commit is None
    assert result.ledger == ()
    assert result.prompt_version == PROMPT_VERSION


def citing(*spans: tuple[str, int, int]) -> Finding:
    evidence = tuple(EvidenceSpan(file=file, start_line=a, end_line=b) for file, a, b in spans)
    return make_finding().model_copy(update={"evidence": evidence})


def test_run_pipeline_with_context_keeps_cited_lines_a_tool_returned_and_counts_the_rest(
    tmp_path: Path,
) -> None:
    finding = citing(("a.py", 1, 1), ("a.py", 2, 2))
    provider = MockProvider(findings=[finding], tool_calls=[("read", {"path": "a.py"})])

    result = run_with_context(tmp_path, provider, context_repo(tmp_path))

    [kept] = result.decision.reported
    assert kept.finding.evidence == (EvidenceSpan(file="a.py", start_line=1, end_line=1),)
    assert kept.evidence_dropped == 1
    checked = data_of(tmp_path / "audit.jsonl", "verify.checked")
    assert (checked["evidence_kept"], checked["evidence_dropped"]) == (1, 1)
    assert "a.py" not in json.dumps(checked)


def head_context(tmp_path: Path, *, is_diff_head: bool) -> ReviewContext:
    """A commit holding `app.py` with three lines, of which `MINIMAL_DIFF` shows only line 1."""
    repo = init_repo(tmp_path / "repo")
    sha = commit(repo, {"app.py": "x\ny\nz\n"})
    git_repo = GitRepo(repo, timeout_s=60)
    return ReviewContext(repo=RepoRef(git=git_repo, commit=sha), is_diff_head=is_diff_head)


@pytest.mark.parametrize(("is_diff_head", "dropped"), [(True, 0), (False, 1)])
def test_run_pipeline_counts_tool_lines_of_a_changed_file_only_at_the_diffs_head(
    tmp_path: Path, is_diff_head: bool, dropped: int
) -> None:
    finding = citing(("app.py", 1, 1), ("app.py", 2, 3))
    provider = MockProvider(findings=[finding], tool_calls=[("read", {"path": "app.py"})])

    result = run_with_context(tmp_path, provider, head_context(tmp_path, is_diff_head=is_diff_head))

    assert result.decision.reported[0].evidence_dropped == dropped


def test_run_pipeline_with_context_keeps_a_suggestion_for_the_added_line(tmp_path: Path) -> None:
    finding = make_finding().model_copy(update={"suggestion": "x = 1"})

    result = run_with_context(tmp_path, MockProvider(findings=[finding]), context_repo(tmp_path))

    [kept] = result.decision.reported
    assert (kept.finding.suggestion, kept.suggestion_dropped) == ("x = 1", None)


def test_run_pipeline_without_context_drops_a_suggestion_as_not_requested(tmp_path: Path) -> None:
    finding = make_finding().model_copy(update={"suggestion": "x = 1"})
    provider = MockProvider(findings=[finding])

    result = run(write_diff(tmp_path, MINIMAL_DIFF), tmp_path / "audit.jsonl", provider)

    [kept] = result.decision.reported
    assert (kept.finding.suggestion, kept.suggestion_dropped) == (None, "not_requested")


@pytest.mark.parametrize(
    ("suggestion", "dropped"), [("x = 1", None), ("x\u202e = 1", "control_characters")]
)
def test_run_pipeline_records_why_a_suggestion_was_dropped_and_never_its_text(
    tmp_path: Path, suggestion: str, dropped: str | None
) -> None:
    finding = make_finding().model_copy(update={"suggestion": suggestion})

    run_with_context(tmp_path, MockProvider(findings=[finding]), context_repo(tmp_path))

    checked = data_of(tmp_path / "audit.jsonl", "verify.checked")
    assert checked["suggestion_dropped"] == dropped
    assert suggestion not in json.dumps(checked, ensure_ascii=False)


MONEY = DomainRule(RuleId.DECIMAL_MONEY, ("billing/**",), Severity.MEDIUM)
MONEY_DIFF = section("billing/a.py", '+price = 19.99\n+s = """\n')


def review_money(
    tmp_path: Path, provider: MockProvider, context: ReviewContext | None = None
) -> PipelineResult:
    audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: datetime(2026, 11, 9, tzinfo=UTC))
    diff = write_diff(tmp_path, MONEY_DIFF)
    return asyncio.run(
        run_pipeline(
            diff,
            Settings(provider="mock"),
            provider,
            audit,
            context=context,
            domain_rules=(MONEY,),
        )
    )


def test_run_pipeline_runs_the_domain_rules_and_records_the_lines_left_unchecked(
    tmp_path: Path,
) -> None:
    result = review_money(tmp_path, MockProvider(findings=[]))

    [reviewed] = result.decision.reported
    assert (reviewed.rule, reviewed.finding.line, reviewed.finding.severity) == (
        RuleId.DECIMAL_MONEY,
        1,
        Severity.MEDIUM,
    )
    record = data_of(tmp_path / "audit.jsonl", "static.rules")
    assert (record["findings"][0]["rule"], record["unchecked_lines"]) == ("decimal-money", 1)


def test_run_pipeline_in_diff_mode_leaves_the_prompt_without_domain_rules(tmp_path: Path) -> None:
    provider = MockProvider(findings=[])

    review_money(tmp_path, provider)

    [request] = provider.requests
    assert RuleId.DECIMAL_MONEY not in request.system_prompt


def test_run_pipeline_in_context_mode_names_the_domain_rules_in_the_prompt(
    tmp_path: Path,
) -> None:
    provider = MockProvider(findings=[])

    review_money(tmp_path, provider, context_repo(tmp_path))

    [request] = provider.requests
    assert f"- decimal-money: {MONEY.description}" in request.system_prompt


def test_run_pipeline_reports_a_model_finding_tagged_with_a_domain_rule_once_as_the_rules(
    tmp_path: Path,
) -> None:
    tagged = Finding(
        file="billing/a.py",
        line=2,
        severity=Severity.MEDIUM,
        category=Category.LOGIC,
        rationale="Float money.",
        confidence=0.9,
        rule=RuleId.DECIMAL_MONEY,
    )

    result = review_money(tmp_path, MockProvider(findings=[tagged]), context_repo(tmp_path))

    assert [(item.source, item.finding.line) for item in result.decision.reported] == [
        (Source.RULE, 1)
    ]


# Linked issues (P3-S9).

ISSUE_TEXT = "Export totals\n\nThe CSV must include the discount."
ISSUES = (
    LinkedIssue(12, IssueStatus.INCLUDED, ISSUE_TEXT),
    LinkedIssue(404, IssueStatus.NOT_FOUND),
)


def test_run_pipeline_with_linked_issues_sends_each_sent_one_after_the_diff(
    tmp_path: Path,
) -> None:
    provider = MockProvider(findings=[])
    context = dataclasses.replace(context_repo(tmp_path), read_issues=lambda: ISSUES)

    run_with_context(tmp_path, provider, context)

    [request] = provider.requests
    assert f"linked issue #12\n<<<UNTRUSTED-ISSUE-{sha256(ISSUE_TEXT)}>>>" in request.user_prompt
    assert "#404" not in request.user_prompt


def test_run_pipeline_records_each_linked_issue_with_the_bytes_sent_but_not_its_text(
    tmp_path: Path,
) -> None:
    context = dataclasses.replace(context_repo(tmp_path), read_issues=lambda: ISSUES)

    run_with_context(tmp_path, MockProvider(findings=[]), context)

    log = tmp_path / "audit.jsonl"
    records = [r["data"] for r in read_records(log) if r["event"] == "issue"]
    assert records == [
        {"number": 12, "status": "included", "bytes_sent": len(ISSUE_TEXT.encode("utf-8"))},
        {"number": 404, "status": "not_found", "bytes_sent": 0},
    ]
    assert "discount" not in log.read_text(encoding="ascii")


def test_run_pipeline_stopped_before_the_model_call_reads_no_linked_issue(tmp_path: Path) -> None:
    def read_issues() -> tuple[LinkedIssue, ...]:
        raise AssertionError("issues read before the review knew it would call the model")

    context = dataclasses.replace(context_repo(tmp_path), read_issues=read_issues)
    provider = MockProvider(findings=[])

    with pytest.raises(DiffTooLargeError):
        run_with_context(tmp_path, provider, context, max_review_bytes=1)

    assert provider.requests == []
    log = tmp_path / "audit.jsonl"
    assert [r for r in read_records(log) if r["event"] == "issue"] == []


# The policy (P4-S1).

LOCKED = Policy(
    version="locked",
    sha256="0" * 64,
    source="p.toml",
    tools=frozenset(),
    max_cost_usd=Decimal("0.60"),
)
WITHIN_LOCKED = Settings(provider="mock", max_cost_usd=Decimal("0.60"))


def run_under_locked(
    tmp_path: Path, settings: Settings, context: ReviewContext | None = None
) -> PipelineResult:
    audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: datetime(2026, 11, 6, tzinfo=UTC))
    diff = write_diff(tmp_path, MINIMAL_DIFF)
    review = run_pipeline(diff, settings, MockProvider(), audit, context=context, policy=LOCKED)
    return asyncio.run(review)


def test_run_pipeline_records_the_policy_and_returns_it(tmp_path: Path) -> None:
    result = run_under_locked(tmp_path, WITHIN_LOCKED)

    start = data_of(tmp_path / "audit.jsonl", "run.start")
    assert (start["policy_version"], start["policy_sha256"]) == ("locked", "0" * 64)
    assert result.policy == LOCKED


def test_run_pipeline_under_a_ceiling_refuses_settings_it_was_not_applied_to(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="check_settings"):
        run_under_locked(tmp_path, Settings(provider="mock"))

    assert read_records(tmp_path / "audit.jsonl") == []


def test_run_pipeline_in_context_mode_refuses_a_policy_with_no_tools(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = ReviewContext(RepoRef(GitRepo(repo, timeout_s=60), commit(repo, {"a.py": "x\n"})))

    with pytest.raises(ConfigError, match="allows no tools"):
        run_under_locked(tmp_path, WITHIN_LOCKED, context)

    assert read_records(tmp_path / "audit.jsonl") == []


class DenyingProvider:
    """Answers with no findings, reporting two tools its runtime denied, and keeps the request."""

    def __init__(self) -> None:
        self.requests: list[ReviewRequest] = []

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        self.requests.append(request)
        denied = (DeniedTool("Bash", "hook"), DeniedTool("mcp__repo__grep", "can_use_tool"))
        return ReviewResponse(findings=(), usage=TokenUsage(0, 0), denied_tools=denied)


READ_ONLY = Policy(
    version="read-only-2",
    sha256="1" * 64,
    source="p.toml",
    tools=frozenset({ToolName.READ}),
    max_cost_usd=None,
)


def test_run_pipeline_in_context_mode_sends_the_policy_and_records_each_denial(
    tmp_path: Path,
) -> None:
    provider = DenyingProvider()
    audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: datetime(2026, 11, 12, tzinfo=UTC))
    diff = write_diff(tmp_path, MINIMAL_DIFF)
    context = context_repo(tmp_path)

    asyncio.run(
        run_pipeline(
            diff, Settings(provider="mock"), provider, audit, context=context, policy=READ_ONLY
        )
    )

    [request] = provider.requests
    assert request.policy == READ_ONLY
    log = tmp_path / "audit.jsonl"
    recorded = events(log)
    called = recorded.index("agent.called")
    assert recorded[called + 1 : called + 3] == ["agent.denied", "agent.denied"]
    assert [r["data"] for r in read_records(log) if r["event"] == "denied"] == [
        {"tool": "Bash", "layer": "hook", "policy_version": "read-only-2"},
        {"tool": "mcp__repo__grep", "layer": "can_use_tool", "policy_version": "read-only-2"},
    ]
