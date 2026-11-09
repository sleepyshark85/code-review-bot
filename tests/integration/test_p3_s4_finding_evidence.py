"""P3-S4: each finding shows the lines it relied on, and only lines the model was shown.

Written from the spec's criteria, Limits and Interfaces, and from Phase 3 design.md ("Interfaces",
"Prompt versions", "Report keys, appended only"), while the developer built the slice and before
any of its code was read.

Three seams drive the slice, all offline:

- **The CLI**, as P3-S1's suite drives it: `review_bot.cli.main([SAMPLE, "--provider", "mock",
  "--audit-log", LOG, ("--context-repo", DIR,) *FLAGS], provider=MockProvider(tool_calls=[...],
  raw_output=JSON))`. The model's answer is given as raw JSON so it goes through the same strict
  parse as a real model's (`parse_findings`), which is where AC5's schema lives. The repository is
  P3-S1's `make_repo` (commit `C`, holding `billing/invoice.py`, 50 lines, and
  `billing/checkout.py`, 30 lines). The sample diff changes `billing/invoice.py` only: new-side
  lines 12, 13 and 14 (two context lines and one added line), then 30 to 41 (context, added, one
  blank context line at 37, added). Lines 15 to 29 and 42 onwards aren't in the diff.
- **`review_diff` directly** for PR mode (AC4), as the spec's Interfaces say: `ReviewContext(repo=
  RepoRef(git=GitRepo(DIR, timeout_s=T), commit=C), is_diff_head=True)`. Kept spans are read from
  each reported finding's `Finding.evidence` (`file`, `start_line`, `end_line`), and the drop count
  from the audit log's `verify` `checked` record (AC8).
- **`review-bot-pr run`**, as P3-S11's suite drives it (its `make_pr` and `run_pr`, in shadow and
  comment mode), for AC7's comment bodies. Until S8 wires context mode into PR runs, a PR run is a
  diff-mode run, so a span is kept when its lines are in the PR's diff (AC6).

Each finding's `evidence_dropped` and `evidence` are read from the JSON report (AC6) or the
`shadow/v1` comment entries (spec Interfaces). The `Context:` line is read by parsing that line as
CommonMark and taking its code spans, so a separator between spans isn't assumed.
"""

import asyncio
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_p1_s4_audit_log import read_records
from test_p1_s11_markdown_output import PARSER, inline_children, visible
from test_p2_s5_comment_sync import GitHub
from test_p3_s1_local_review_with_repo_tools import (
    CHARGED_LINE,
    CHECKOUT,
    GOLDEN,
    INVOICE,
    MISSING,
    PHASE_1_PROMPT_VERSION,
    READ_MARKER,
    SAMPLE,
    UTIL,
    ContextRepo,
    grep_lines,
    make_repo,
    numbered,
    unwrap,
)
from test_p3_s11_codeowners_routing import code, make_pr, reviewed, run_pr

from review_bot.audit import AuditLog
from review_bot.context.tools import RepoRef
from review_bot.findings import Finding
from review_bot.git import GitRepo as BotGitRepo
from review_bot.providers.mock import MockProvider
from review_bot.settings import Settings

pytestmark = pytest.mark.integration

FINDINGS_EXIT = 1
INVALID_OUTPUT_EXIT = 13
MAX_SPANS = 5
# AC9: main holds `p3-v1` (P3-S1); P3-S4 takes the next free number. If another slice that bumps
# the context prompt merges first, this moves on by one (design.md, Prompt versions).
CONTEXT_PROMPT_VERSION = "p3-v2"
# Generous: it catches a line-by-line walk of a hostile span, not a slow CI machine.
BOUND_S = 60.0
HUGE_LINE = 10**12
GIT_TIMEOUT_S = 30.0

SPAN_KEYS = frozenset({"file", "start_line", "end_line"})
RATIONALE = "Tax is added here and again at checkout, so it is charged twice."
# A path no tool returned and no diff holds: it must never be shown or logged (Limits, AC8).
UNSEEN = "unseen/model_written_path_8c41e2.py"
# The sample diff's new-side lines of billing/invoice.py.
DIFF_LINES = frozenset({12, 13, 14, *range(30, 42)})

READ_CHECKOUT_ALL = ("read", {"path": CHECKOUT, "start_line": 1, "line_count": 30})
READ_INVOICE_ALL = ("read", {"path": INVOICE, "start_line": 1, "line_count": 50})
GREP_LINE_12 = ("grep", {"literal": "(1 + invoice.tax_rate)"})


# The model's answer.


def span(file: str, start: int, end: int | None = None) -> dict[str, Any]:
    return {"file": file, "start_line": start, "end_line": start if end is None else end}


def ai_finding(
    evidence: Sequence[Mapping[str, Any]] | None = None,
    *,
    file: str = INVOICE,
    line: int = 14,
    rationale: str = RATIONALE,
) -> dict[str, Any]:
    """The model's finding on the sample's added line 14; `evidence` left out when None."""
    entry: dict[str, Any] = {
        "file": file,
        "line": line,
        "severity": "high",
        "category": "logic",
        "rationale": rationale,
        "confidence": 0.8,
    }
    if evidence is not None:
        entry["evidence"] = [dict(item) for item in evidence]
    return entry


def answer(*findings: Mapping[str, Any]) -> str:
    return json.dumps({"findings": [dict(item) for item in findings]})


def spans_of(entries: Sequence[Mapping[str, Any]]) -> list[tuple[str, int, int]]:
    return [(item["file"], item["start_line"], item["end_line"]) for item in entries]


# Running the CLI.


@dataclass(frozen=True, slots=True)
class Review:
    exit_code: int
    stdout: str
    stderr: str
    provider: MockProvider
    audit_log: Path

    @property
    def report(self) -> dict[str, Any]:
        document = json.loads(self.stdout)
        assert isinstance(document, dict)
        assert document["schema"] == "report/v1"
        return document

    @property
    def ai_entries(self) -> list[dict[str, Any]]:
        return [entry for entry in self.report["findings"] if entry["source"] == "ai"]

    @property
    def ai_entry(self) -> dict[str, Any]:
        (entry,) = self.ai_entries
        return entry

    @property
    def kept(self) -> list[tuple[str, int, int]]:
        return spans_of(self.ai_entry["evidence"])

    @property
    def dropped(self) -> int:
        dropped: int = self.ai_entry["evidence_dropped"]
        return dropped

    @property
    def bodies(self) -> list[str]:
        return [unwrap(result) for result in self.provider.tool_results]

    @property
    def log_text(self) -> str:
        return self.audit_log.read_text(encoding="utf-8")

    def records(self, stage: str, event: str) -> list[dict[str, Any]]:
        return [
            record["data"]
            for record in read_records(self.audit_log)
            if record["stage"] == stage and record["event"] == event
        ]

    @property
    def outcomes(self) -> list[str]:
        return [data["outcome"] for data in self.records("context", "tool_call")]


class Harness:
    def __init__(self, bot: Any, tmp_path: Path) -> None:
        self._bot = bot
        self._tmp_path = tmp_path
        self._runs = 0

    def repo(self, extra: Mapping[str, str] | None = None) -> ContextRepo:
        return make_repo(self._tmp_path / "repo", extra)

    def run(
        self,
        raw: str,
        *argv: str,
        tool_calls: Sequence[tuple[str, Mapping[str, object]]] = (),
    ) -> Review:
        self._runs += 1
        audit_log = self._tmp_path / f"audit-{self._runs}.jsonl"
        provider = MockProvider(
            raw_output=raw, tool_calls=[(name, dict(arguments)) for name, arguments in tool_calls]
        )
        run = self._bot.run(
            str(SAMPLE),
            "--provider",
            "mock",
            "--audit-log",
            str(audit_log),
            "--format",
            "json",
            *argv,
            provider=provider,
        )
        return Review(run.exit_code, run.stdout, run.stderr, provider, audit_log)

    def context(
        self,
        repo: ContextRepo,
        tool_calls: Sequence[tuple[str, Mapping[str, object]]],
        evidence: Sequence[Mapping[str, Any]] | None,
        *flags: str,
    ) -> Review:
        """A context review (`--context-repo`) whose one finding cites `evidence`."""
        return self.run(
            answer(ai_finding(evidence)),
            "--context-repo",
            str(repo.path),
            *flags,
            tool_calls=tool_calls,
        )

    def diff_mode(self, evidence: Sequence[Mapping[str, Any]] | None) -> Review:
        return self.run(answer(ai_finding(evidence)))


@pytest.fixture
def harness(bot: Any, tmp_path: Path) -> Harness:
    return Harness(bot, tmp_path)


@pytest.fixture
def repo(harness: Harness) -> ContextRepo:
    return harness.repo()


def assert_kept_finding(review: Review) -> None:
    """The review reported the one model finding, whatever happened to its spans."""
    assert review.exit_code == FINDINGS_EXIT, review.stderr
    entry = review.ai_entry
    assert (entry["file"], entry["line"]) == (INVOICE, 14), entry


# Running review_diff in PR mode.


@dataclass(frozen=True, slots=True)
class PipelineRun:
    kept: list[tuple[str, int, int]]
    dropped: int


def review_with_context(
    tmp_path: Path,
    repo: ContextRepo,
    tool_calls: Sequence[tuple[str, Mapping[str, object]]],
    evidence: Sequence[Mapping[str, Any]],
    *,
    is_diff_head: bool | None,
) -> PipelineRun:
    """`review_diff` over the sample diff with tools over `C`; `is_diff_head` None: the default."""
    from review_bot.review import ReviewContext, review_diff  # noqa: PLC0415 - the slice's seam

    audit_log = tmp_path / f"pipeline-{len(list(tmp_path.glob('pipeline-*.jsonl')))}.jsonl"
    ref = RepoRef(git=BotGitRepo(repo.path, timeout_s=GIT_TIMEOUT_S), commit=repo.commit)
    head = {} if is_diff_head is None else {"is_diff_head": is_diff_head}
    context = ReviewContext(repo=ref, **head)
    provider = MockProvider(
        raw_output=answer(ai_finding(evidence)),
        tool_calls=[(name, dict(arguments)) for name, arguments in tool_calls],
    )
    audit = AuditLog(audit_log, clock=lambda: datetime.now(UTC))
    result = asyncio.run(
        review_diff(
            SAMPLE.read_text(encoding="utf-8"),
            Settings(provider="mock", audit_log=audit_log),
            provider,
            audit,
            name="sample_diff.txt",
            context=context,
        )
    )
    (reviewed_finding,) = [r for r in result.decision.reported if str(r.source) == "ai"]
    kept = [(s.file, s.start_line, s.end_line) for s in reviewed_finding.finding.evidence]
    (check,) = [
        record["data"]
        for record in read_records(audit_log)
        if record["stage"] == "verify"
        and record["event"] == "checked"
        and record["data"].get("line") == 14
        and record["data"]["outcome"] == "kept"
    ]
    return PipelineRun(kept, check["evidence_dropped"])


# AC1: a span inside a read is kept.


def test_p3_s4_ac1_span_inside_a_read_of_lines_1_to_30_is_kept(harness, repo):
    review = harness.context(repo, [READ_CHECKOUT_ALL], [span(CHECKOUT, 12)])

    assert_kept_finding(review)
    assert review.kept == [(CHECKOUT, 12, 12)]
    assert review.dropped == 0


def test_p3_s4_ac1_span_of_the_whole_read_is_kept(harness, repo):
    review = harness.context(repo, [READ_CHECKOUT_ALL], [span(CHECKOUT, 1, 30)])

    assert review.kept == [(CHECKOUT, 1, 30)]
    assert review.dropped == 0


def test_p3_s4_ac1_span_shown_by_two_adjoining_reads_is_kept(harness, repo):
    """Every line of 5 to 20 was shown, by one read or the other."""
    reads = [
        ("read", {"path": CHECKOUT, "start_line": 1, "line_count": 10}),
        ("read", {"path": CHECKOUT, "start_line": 11, "line_count": 20}),
    ]

    review = harness.context(repo, reads, [span(CHECKOUT, 5, 20)])

    assert review.kept == [(CHECKOUT, 5, 20)]
    assert review.dropped == 0


# AC2: a grep match shows exactly its own line.


def test_p3_s4_ac2_grep_match_precondition_is_line_12_only(harness, repo):
    review = harness.context(repo, [GREP_LINE_12], [])

    assert grep_lines(review.bodies[0]) == [(CHECKOUT, 12, CHARGED_LINE)]


def test_p3_s4_ac2_span_of_the_grep_matched_line_is_kept(harness, repo):
    review = harness.context(repo, [GREP_LINE_12], [span(CHECKOUT, 12)])

    assert_kept_finding(review)
    assert review.kept == [(CHECKOUT, 12, 12)]
    assert review.dropped == 0


@pytest.mark.parametrize(("start", "end"), [(11, 13), (11, 12), (12, 13), (11, 11), (13, 13)])
def test_p3_s4_ac2_span_beyond_the_grep_matched_line_is_dropped(harness, repo, start, end):
    review = harness.context(repo, [GREP_LINE_12], [span(CHECKOUT, start, end)])

    assert_kept_finding(review)
    assert review.kept == []
    assert review.dropped == 1


def test_p3_s4_ac2_kept_and_dropped_spans_in_one_finding(harness, repo):
    review = harness.context(repo, [GREP_LINE_12], [span(CHECKOUT, 12), span(CHECKOUT, 11, 13)])

    assert review.kept == [(CHECKOUT, 12, 12)]
    assert review.dropped == 1


# AC3: spans the model wasn't shown are dropped; the finding stays.


def test_p3_s4_ac3_span_of_a_file_no_tool_returned_is_dropped(harness, repo):
    review = harness.context(repo, [READ_CHECKOUT_ALL], [span(UTIL, 1)])

    assert_kept_finding(review)
    assert review.kept == []
    assert review.dropped == 1


def test_p3_s4_ac3_span_of_a_file_only_listed_by_glob_is_dropped(harness, repo):
    """`glob` shows a path, not its lines."""
    review = harness.context(repo, [("glob", {"pattern": "billing/*.py"})], [span(CHECKOUT, 1)])

    assert CHECKOUT in review.bodies[0].split("\n"), "precondition: glob listed the file"
    assert_kept_finding(review)
    assert review.kept == []
    assert review.dropped == 1


@pytest.mark.parametrize(("start", "end"), [(14, 15), (15, 15), (9, 10), (1, 30)])
def test_p3_s4_ac3_span_past_the_lines_returned_is_dropped(harness, repo, start, end):
    read = ("read", {"path": CHECKOUT, "start_line": 10, "line_count": 5})

    review = harness.context(repo, [read], [span(CHECKOUT, start, end)])

    assert [n for n, _ in numbered(review.bodies[0])] == [10, 11, 12, 13, 14], "precondition"
    assert_kept_finding(review)
    assert review.kept == []
    assert review.dropped == 1


def test_p3_s4_ac3_span_past_the_end_of_the_file_is_dropped(harness, repo):
    review = harness.context(repo, [READ_CHECKOUT_ALL], [span(CHECKOUT, 30, 31)])

    assert review.kept == []
    assert review.dropped == 1


def test_p3_s4_ac3_span_of_a_refused_not_found_read_is_dropped(harness, repo):
    review = harness.context(repo, [("read", {"path": MISSING})], [span(MISSING, 1)])

    assert review.outcomes == ["refused"], "precondition"
    assert_kept_finding(review)
    assert review.kept == []
    assert review.dropped == 1


def test_p3_s4_ac3_span_of_a_read_refused_over_the_call_limit_is_dropped(harness, repo):
    calls = [("glob", {"pattern": "**/*.py"}), READ_CHECKOUT_ALL]

    review = harness.context(repo, calls, [span(CHECKOUT, 12)], "--max-tool-calls", "1")

    assert review.outcomes == ["ok", "refused"], "precondition"
    assert_kept_finding(review)
    assert review.kept == []
    assert review.dropped == 1


@pytest.fixture
def truncated_review(harness: Harness) -> tuple[Harness, ContextRepo, int]:
    """A repository with 400 lines of about 78 bytes, and the last line a 400-line read returns."""
    long_text = "".join(f"line_{n:03d} = '{'x' * 64}'\n" for n in range(1, 401))
    repo = harness.repo({"data/long.py": long_text})
    probe = harness.context(repo, [("read", {"path": "data/long.py", "line_count": 400})], [])
    assert probe.outcomes == ["truncated"], "precondition: the read is cut at the byte cap"
    returned = [n for n, _ in numbered(probe.bodies[0])]
    assert returned == list(range(1, len(returned) + 1)), "precondition: a prefix is returned"
    assert 1 < len(returned) < 400, returned
    return harness, repo, returned[-1]


def test_p3_s4_ac3_truncated_read_keeps_its_last_returned_line(truncated_review):
    harness, repo, last = truncated_review
    read = ("read", {"path": "data/long.py", "line_count": 400})

    review = harness.context(repo, [read], [span("data/long.py", 1, last)])

    assert review.kept == [("data/long.py", 1, last)]
    assert review.dropped == 0


@pytest.mark.parametrize("offsets", [(1, 1), (0, 1), (-1, 1), (1, 400)], ids=str)
def test_p3_s4_ac3_truncated_read_counts_only_the_lines_it_returned(truncated_review, offsets):
    harness, repo, last = truncated_review
    read = ("read", {"path": "data/long.py", "line_count": 400})
    start, end = last + offsets[0], min(last + offsets[1], 400)

    review = harness.context(repo, [read], [span("data/long.py", start, end)])

    assert_kept_finding(review)
    assert review.kept == []
    assert review.dropped == 1


# AC4: files the diff changes, by whether the tools read the diff's head.


def test_p3_s4_ac4_cli_context_repo_ignores_ledger_lines_of_a_changed_file(harness, repo):
    """`review-bot --context-repo`: a read of all 50 lines doesn't vouch for line 20."""
    review = harness.context(repo, [READ_INVOICE_ALL], [span(INVOICE, 20)])

    assert_kept_finding(review)
    assert review.kept == []
    assert review.dropped == 1


@pytest.mark.parametrize(("start", "end"), [(12, 14), (14, 14), (30, 41), (37, 37)])
def test_p3_s4_ac4_cli_context_repo_keeps_added_and_context_lines(harness, repo, start, end):
    review = harness.context(repo, [], [span(INVOICE, start, end)])

    assert review.kept == [(INVOICE, start, end)]
    assert review.dropped == 0


@pytest.mark.parametrize(("start", "end"), [(13, 15), (14, 30), (11, 12), (41, 42)])
def test_p3_s4_ac4_cli_context_repo_drops_a_span_leaving_the_diff_even_if_read(
    harness, repo, start, end
):
    review = harness.context(repo, [READ_INVOICE_ALL], [span(INVOICE, start, end)])

    assert review.kept == []
    assert review.dropped == 1


def test_p3_s4_ac4_cli_context_repo_ignores_ledger_lines_even_at_head(harness, repo):
    """`--context-rev HEAD` is still not known to be the diff's head."""
    review = harness.context(repo, [READ_INVOICE_ALL], [span(INVOICE, 20)], "--context-rev", "HEAD")

    assert review.kept == []
    assert review.dropped == 1


READ_INVOICE_15_TO_20 = ("read", {"path": INVOICE, "start_line": 15, "line_count": 6})


@pytest.mark.parametrize(
    ("start", "end"),
    [(14, 16), (12, 20), (15, 20), (13, 13), (30, 41)],
    ids=["diff_then_read", "whole_union", "read_only", "context_line", "diff_only"],
)
def test_p3_s4_ac4_pr_mode_counts_diff_lines_and_lines_a_tool_returned(tmp_path, repo, start, end):
    run = review_with_context(
        tmp_path, repo, [READ_INVOICE_15_TO_20], [span(INVOICE, start, end)], is_diff_head=True
    )

    assert run.kept == [(INVOICE, start, end)]
    assert run.dropped == 0


@pytest.mark.parametrize(("start", "end"), [(21, 21), (20, 21), (25, 29), (14, 30)])
def test_p3_s4_ac4_pr_mode_drops_lines_neither_in_the_diff_nor_returned(tmp_path, repo, start, end):
    run = review_with_context(
        tmp_path, repo, [READ_INVOICE_15_TO_20], [span(INVOICE, start, end)], is_diff_head=True
    )

    assert run.kept == []
    assert run.dropped == 1


@pytest.mark.parametrize("is_diff_head", [False, None], ids=["false", "default"])
def test_p3_s4_ac4_otherwise_ledger_lines_of_a_changed_file_dont_count(
    tmp_path, repo, is_diff_head
):
    evidence = [span(INVOICE, 14, 16), span(INVOICE, 15), span(INVOICE, 12, 14)]

    run = review_with_context(
        tmp_path, repo, [READ_INVOICE_15_TO_20], evidence, is_diff_head=is_diff_head
    )

    assert run.kept == [(INVOICE, 12, 14)]
    assert run.dropped == 2


def test_p3_s4_ac4_pr_mode_leaves_ac1_to_ac3_unchanged_for_other_files(tmp_path, repo):
    evidence = [span(CHECKOUT, 12), span(CHECKOUT, 30, 31), span(UTIL, 1), span(MISSING, 1)]
    calls = [READ_CHECKOUT_ALL, ("read", {"path": MISSING})]

    run = review_with_context(tmp_path, repo, calls, evidence, is_diff_head=True)

    assert run.kept == [(CHECKOUT, 12, 12)]
    assert run.dropped == 3


# AC5: a malformed span rejects the whole output.


VALID = span(CHECKOUT, 12)
AC5_INVALID = {
    "six_spans": [span(UNSEEN, n) for n in range(1, 7)],
    "start_after_end": [span(INVOICE, 14, 13)],
    "start_line_0": [span(INVOICE, 0, 14)],
    "end_line_0": [{"file": INVOICE, "start_line": 1, "end_line": 0}],
    "negative_line": [span(INVOICE, -1, 14)],
    "four_good_and_one_bad": [*[span(INVOICE, 14)] * 4, span(INVOICE, 14, 13)],
    "empty_file": [span("", 14)],
    "missing_end_line": [{"file": INVOICE, "start_line": 14}],
    "extra_key": [VALID | {"text": "quoted line"}],
    "line_as_string": [{"file": INVOICE, "start_line": "14", "end_line": 14}],
    "not_a_list": "billing/invoice.py:14",
}


@pytest.mark.parametrize("evidence", AC5_INVALID.values(), ids=AC5_INVALID.keys())
def test_p3_s4_ac5_malformed_evidence_in_diff_mode_exits_13(harness, evidence):
    entry = ai_finding() | {"evidence": evidence}

    review = harness.run(answer(entry))

    assert review.exit_code == INVALID_OUTPUT_EXIT, review.stderr
    assert review.stdout == ""


@pytest.mark.parametrize("case", ["six_spans", "start_after_end", "start_line_0", "end_line_0"])
def test_p3_s4_ac5_malformed_evidence_in_context_mode_exits_13(harness, repo, case):
    entry = ai_finding() | {"evidence": AC5_INVALID[case]}

    review = harness.run(
        answer(entry), "--context-repo", str(repo.path), tool_calls=[READ_CHECKOUT_ALL]
    )

    assert review.exit_code == INVALID_OUTPUT_EXIT, review.stderr
    assert review.stdout == ""


def test_p3_s4_ac5_one_bad_finding_rejects_the_good_ones_too(harness):
    good = ai_finding([span(INVOICE, 14)])
    bad = ai_finding([span(INVOICE, 13, 12)], line=34, rationale="Swallowed exception.")

    review = harness.run(answer(good, bad))

    assert review.exit_code == INVALID_OUTPUT_EXIT, review.stderr
    assert review.stdout == ""


@pytest.mark.parametrize(
    "evidence",
    [None, [], [span(INVOICE, 14)] * MAX_SPANS, [span(INVOICE, 1, 1)]],
    ids=["absent", "empty", "five_spans", "line_1"],
)
def test_p3_s4_ac5_valid_evidence_is_accepted(harness, evidence):
    review = harness.diff_mode(evidence)

    assert_kept_finding(review)


# AC6: the JSON report.


def test_p3_s4_ac6_report_keeps_spans_in_the_models_order(harness, repo):
    evidence = [
        span(CHECKOUT, 20),
        span(INVOICE, 14),
        span(CHECKOUT, 1, 3),
        span(UTIL, 1),
        span(CHECKOUT, 12),
    ]

    review = harness.context(repo, [READ_CHECKOUT_ALL], evidence)

    assert review.kept == [
        (CHECKOUT, 20, 20),
        (INVOICE, 14, 14),
        (CHECKOUT, 1, 3),
        (CHECKOUT, 12, 12),
    ]
    assert review.dropped == 1


def test_p3_s4_ac6_each_span_has_exactly_file_start_line_and_end_line(harness, repo):
    review = harness.context(repo, [READ_CHECKOUT_ALL], [span(CHECKOUT, 10, 14)])

    (entry,) = review.ai_entry["evidence"]
    assert set(entry) == SPAN_KEYS, sorted(entry)
    assert type(entry["start_line"]) is int
    assert type(entry["end_line"]) is int
    assert type(review.ai_entry["evidence_dropped"]) is int


def test_p3_s4_ac6_every_finding_has_evidence_and_evidence_dropped(harness, repo):
    """The sample diff's two rule findings too, which cite nothing (reading flagged)."""
    review = harness.context(repo, [READ_CHECKOUT_ALL], [span(CHECKOUT, 12)])

    findings = review.report["findings"]
    assert {entry["source"] for entry in findings} == {"rule", "ai"}
    for entry in findings:
        assert "evidence" in entry, entry
        assert "evidence_dropped" in entry, entry
    for entry in findings:
        if entry["source"] == "rule":
            assert (entry["evidence"], entry["evidence_dropped"]) == ([], 0), entry


def test_p3_s4_ac6_diff_mode_checks_spans_against_the_diff_alone(harness):
    evidence = [span(INVOICE, 30, 41), span(INVOICE, 15), span(CHECKOUT, 12), span(INVOICE, 12)]

    review = harness.diff_mode(evidence)

    assert_kept_finding(review)
    assert review.kept == [(INVOICE, 30, 41), (INVOICE, 12, 12)]
    assert review.dropped == 2


@pytest.mark.parametrize("evidence", [None, []], ids=["absent", "empty"])
def test_p3_s4_ac6_diff_mode_evidence_is_empty_when_the_model_gives_none(harness, evidence):
    review = harness.diff_mode(evidence)

    assert_kept_finding(review)
    assert review.ai_entry["evidence"] == []
    assert review.dropped == 0


def test_p3_s4_ac6_context_mode_evidence_is_empty_when_the_model_gives_none(harness, repo):
    review = harness.context(repo, [READ_CHECKOUT_ALL], None)

    assert review.ai_entry["evidence"] == []
    assert review.dropped == 0


def test_p3_s4_ac6_text_output_is_unchanged_by_evidence(harness):
    """Evidence is for the report and the comment bodies; text stdout stays as it was."""
    plain = harness.run(answer(ai_finding()), "--format", "text")
    cited = harness.run(answer(ai_finding([span(INVOICE, 12, 14)])), "--format", "text")

    assert cited.exit_code == plain.exit_code == FINDINGS_EXIT
    assert cited.stdout == plain.stdout


# AC7: the `Context:` line of a comment body.


def context_lines(body: str) -> list[str]:
    return [line for line in body.split("\n") if line.startswith("Context:")]


def context_code_spans(body: str) -> list[str]:
    """The code spans of the body's one `Context: ` line, read as CommonMark."""
    (line,) = context_lines(body)
    assert line.startswith("Context: "), line
    tokens = PARSER.parse(line)
    children = list(inline_children(tokens))
    spans = [child.content for child in children if child.type == "code_inline"]
    between = "".join(c.content for c in children if c.type in ("text", "text_special"))
    separators = set(between.removeprefix("Context:"))
    assert separators <= set(" ,;"), f"text other than separators beside the spans: {line!r}"
    return spans


def assert_context_after_rationale(body: str, rationale: str) -> None:
    """The rationale is matched as a reader sees it: the body escapes Markdown punctuation."""
    lines = body.split("\n")
    (context_at,) = [n for n, line in enumerate(lines) if line.startswith("Context:")]
    rationale_at = next(
        n
        for n, line in enumerate(lines)
        if rationale in visible(list(inline_children(PARSER.parse(line))))
    )
    assert rationale_at < context_at < len(lines) - 1, body
    assert lines[-1].startswith("<!-- review-bot:v1 "), f"the marker isn't last: {lines[-1]!r}"


PR_FILE = "billing/invoice.py"
PR_RATIONALE = "The total is computed twice."


def pr_finding(
    evidence: Sequence[Mapping[str, Any]], line: int = 3, file: str = PR_FILE
) -> Finding:
    return Finding.model_validate(
        {
            "file": file,
            "line": line,
            "severity": "high",
            "category": "logic",
            "rationale": PR_RATIONALE,
            "confidence": 0.9,
            "evidence": [dict(item) for item in evidence],
        }
    )


def comment_bodies(run: Any, mode: str) -> list[str]:
    """The bodies posted in comment mode, or the shadow report's in shadow mode."""
    if mode == "shadow":
        return [entry["body"] for entry in run.shadow["comments"]]
    assert run.github is not None
    posted = [GitHub.review_of(call) for call in run.github.calls() if call.name == "post_review"]
    return [body for _, comments in posted for (_, _, _, body) in comments]


@pytest.mark.parametrize("mode", ["shadow", "comment"])
def test_p3_s4_ac7_body_has_a_context_line_of_code_spans_after_the_rationale(
    tmp_path, capsys, mode
):
    pull = make_pr(tmp_path / "clone", {PR_FILE: code(20)})
    evidence = [span(PR_FILE, 10, 14), span(PR_FILE, 12)]

    run = run_pr(pull, tmp_path, capsys, [pr_finding(evidence)], mode=mode)

    reviewed(run)
    (body,) = comment_bodies(run, mode)
    assert context_code_spans(body) == [f"{PR_FILE}:10-14", f"{PR_FILE}:12"]
    assert_context_after_rationale(body, PR_RATIONALE)


@pytest.mark.parametrize("mode", ["shadow", "comment"])
def test_p3_s4_ac7_body_without_kept_spans_has_no_context_line(tmp_path, capsys, mode):
    pull = make_pr(tmp_path / "clone", {PR_FILE: code(20)})

    run = run_pr(pull, tmp_path, capsys, [pr_finding([])], mode=mode)

    reviewed(run)
    (body,) = comment_bodies(run, mode)
    assert "Context:" not in body, body
    assert body.split("\n")[-1].startswith("<!-- review-bot:v1 "), body


def test_p3_s4_ac7_body_whose_spans_were_all_dropped_has_no_context_line(tmp_path, capsys):
    pull = make_pr(tmp_path / "clone", {PR_FILE: code(20)})

    run = run_pr(pull, tmp_path, capsys, [pr_finding([span(UNSEEN, 1), span(PR_FILE, 25)])])

    reviewed(run)
    (entry,) = run.shadow["comments"]
    assert (entry["evidence"], entry["evidence_dropped"]) == ([], 2)
    assert "Context:" not in entry["body"], entry["body"]
    assert UNSEEN not in json.dumps(run.shadow)


def test_p3_s4_ac7_dropped_spans_are_left_out_of_the_context_line(tmp_path, capsys):
    pull = make_pr(tmp_path / "clone", {PR_FILE: code(20), "billing/tax.py": code(5)})
    evidence = [span("billing/tax.py", 2, 4), span(UNSEEN, 1), span(PR_FILE, 1)]

    run = run_pr(pull, tmp_path, capsys, [pr_finding(evidence)])

    reviewed(run)
    (body,) = comment_bodies(run, "shadow")
    assert context_code_spans(body) == ["billing/tax.py:2-4", f"{PR_FILE}:1"]
    assert UNSEEN not in body


def test_p3_s4_ac7_path_with_a_backtick_stays_one_code_span(tmp_path, capsys):
    """A diff path may hold a backtick; the code span must still hold it whole."""
    odd = "billing/odd`name.py"
    pull = make_pr(tmp_path / "clone", {PR_FILE: code(20), odd: code(3)})

    run = run_pr(pull, tmp_path, capsys, [pr_finding([span(odd, 2)])])

    reviewed(run)
    (body,) = comment_bodies(run, "shadow")
    assert context_code_spans(body) == [f"{odd}:2"]


def test_p3_s4_interfaces_shadow_comment_entries_have_evidence(tmp_path, capsys):
    pull = make_pr(tmp_path / "clone", {PR_FILE: code(20)})
    evidence = [span(PR_FILE, 10, 14), span(UNSEEN, 1)]

    run = run_pr(pull, tmp_path, capsys, [pr_finding(evidence)])

    reviewed(run)
    (entry,) = run.shadow["comments"]
    assert spans_of(entry["evidence"]) == [(PR_FILE, 10, 14)]
    assert entry["evidence_dropped"] == 1
    assert set(entry["evidence"][0]) == SPAN_KEYS


# AC8: the verify record counts, and holds no span text.


def checked_records(review: Review) -> list[dict[str, Any]]:
    records = review.records("verify", "checked")
    assert records, "no verify checked record"
    return records


def ai_check(review: Review) -> dict[str, Any]:
    (record,) = [
        data
        for data in checked_records(review)
        if data.get("line") == 14 and data.get("file") == INVOICE
    ]
    return record


def test_p3_s4_ac8_checked_record_counts_kept_and_dropped_spans(harness, repo):
    evidence = [span(CHECKOUT, 12), span(CHECKOUT, 1, 30), span(UNSEEN, 3), span(INVOICE, 20)]

    review = harness.context(repo, [READ_CHECKOUT_ALL], evidence)

    record = ai_check(review)
    assert (record["evidence_kept"], record["evidence_dropped"]) == (2, 2), record


def test_p3_s4_ac8_every_checked_record_has_both_counts(harness, repo):
    review = harness.context(repo, [READ_CHECKOUT_ALL], [span(CHECKOUT, 12)])

    for record in checked_records(review):
        assert type(record["evidence_kept"]) is int, record
        assert type(record["evidence_dropped"]) is int, record


def test_p3_s4_ac8_finding_citing_nothing_counts_zero(harness):
    review = harness.diff_mode(None)

    record = ai_check(review)
    assert (record["evidence_kept"], record["evidence_dropped"]) == (0, 0), record


def test_p3_s4_ac8_diff_mode_records_have_the_counts(harness):
    review = harness.diff_mode([span(INVOICE, 14), span(CHECKOUT, 12)])

    record = ai_check(review)
    assert (record["evidence_kept"], record["evidence_dropped"]) == (1, 1), record


def test_p3_s4_ac8_no_span_text_or_unseen_path_reaches_the_log(harness, repo):
    """The model was shown line 13's marker and cited it; the log holds counts, not text."""
    evidence = [span(CHECKOUT, 12, 13), span(UNSEEN, 1)]

    review = harness.context(repo, [READ_CHECKOUT_ALL], evidence)

    assert any(READ_MARKER in body for body in review.bodies), "precondition: the model saw it"
    text = review.log_text
    assert READ_MARKER not in text
    assert CHARGED_LINE not in text
    assert UNSEEN not in text
    record = ai_check(review)
    assert "evidence" not in record, "the spans themselves are logged"


# AC9: the context prompt asks for evidence, under the next version.


def test_p3_s4_ac9_context_prompt_asks_for_evidence(harness, repo):
    review = harness.context(repo, [], None)

    # The system prompt alone: the user prompt holds the untrusted diff. Checked as labels, the way
    # P0-S1 checks each field, so a mention in prose doesn't pass.
    (request,) = review.provider.requests
    assert re.search(r"\bevidence\b\s*:", request.system_prompt), "`evidence` isn't labelled"
    for key in ("file", "start_line", "end_line"):
        assert re.search(rf'"{key}"\s*:', request.system_prompt), f"the span's {key!r} isn't shown"


def test_p3_s4_ac9_context_prompt_version_is_the_next_free_number(harness, repo):
    review = harness.context(repo, [], None)

    assert review.report["prompt_version"] == CONTEXT_PROMPT_VERSION
    (start,) = review.records("run", "start")
    assert start["prompt_version"] == CONTEXT_PROMPT_VERSION


def test_p3_s4_ac9_diff_mode_prompt_is_byte_for_byte_phase_1s(harness):
    review = harness.diff_mode(None)

    (request,) = review.provider.requests
    system = (GOLDEN / "p1_v1_system_prompt.golden.txt").read_bytes().decode("utf-8")
    user = (GOLDEN / "p1_v1_sample_user_prompt.golden.txt").read_bytes().decode("utf-8")
    assert request.system_prompt == system
    assert request.user_prompt + "\n" == user
    assert review.report["prompt_version"] == PHASE_1_PROMPT_VERSION


# Limits: spans are model input.


@pytest.mark.parametrize(
    "path",
    [
        "./billing/checkout.py",
        "billing//checkout.py",
        "Billing/checkout.py",
        "billing\\checkout.py",
        "/billing/checkout.py",
        "billing/checkout.py ",
        " billing/checkout.py",
        "billing/./checkout.py",
        "lib/../billing/checkout.py",
    ],
)
def test_p3_s4_limits_path_kept_only_if_it_equals_a_returned_path(harness, repo, path):
    review = harness.context(repo, [READ_CHECKOUT_ALL], [span(path, 12)])

    assert_kept_finding(review)
    assert review.kept == []
    assert review.dropped == 1
    assert path not in review.stdout


def test_p3_s4_limits_unseen_path_appears_in_no_output(harness, repo):
    review = harness.context(repo, [READ_CHECKOUT_ALL], [span(UNSEEN, 1)])

    assert UNSEEN not in review.stdout
    assert UNSEEN not in review.stderr
    assert UNSEEN not in review.log_text


def test_p3_s4_limits_huge_span_is_dropped_quickly_in_context_mode(harness, repo):
    grep_many = ("grep", {"literal": "_"})
    calls = [READ_CHECKOUT_ALL, *[grep_many] * 10]
    evidence = [
        span(CHECKOUT, 1, HUGE_LINE),
        span(INVOICE, 12, HUGE_LINE),
        span(CHECKOUT, HUGE_LINE - 1, HUGE_LINE),
        span(UNSEEN, 1, HUGE_LINE),
        span(CHECKOUT, 12),
    ]

    started = time.monotonic()
    review = harness.context(repo, calls, evidence, "--max-tool-calls", "11")
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, f"took {elapsed:.1f} s"
    assert review.kept == [(CHECKOUT, 12, 12)]
    assert review.dropped == 4


def test_p3_s4_limits_huge_span_is_dropped_quickly_in_diff_mode(harness):
    started = time.monotonic()
    review = harness.diff_mode([span(INVOICE, 12, HUGE_LINE), span(INVOICE, 1, HUGE_LINE)])
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, f"took {elapsed:.1f} s"
    assert review.kept == []
    assert review.dropped == 2


def test_p3_s4_limits_huge_span_is_dropped_quickly_in_pr_mode(tmp_path, repo):
    started = time.monotonic()
    run = review_with_context(
        tmp_path, repo, [READ_INVOICE_ALL], [span(INVOICE, 1, HUGE_LINE)], is_diff_head=True
    )
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, f"took {elapsed:.1f} s"
    assert run.kept == []
    assert run.dropped == 1
