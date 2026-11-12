"""P1-S10: get the review as JSON.

Written from the spec's criteria and Notes, and design.md's Settings, Audit log ("Paths in
`audit/v1`") and Compatibility sections, before the developer's code existed. Runs go through
`main` with an injected `MockProvider`, or a small provider reporting token usage; the stdout
failure cases (AC8) run a real process, as P0-S3 AC4's do.

`report/v1`'s top-level keys and `counts` keys are the spec's Interfaces list. Its findings' keys
are AC1's list, plus `path_quoted` only on a path that isn't valid UTF-8. `usage` holds
`input_tokens` and `output_tokens`, and the document is one line followed by a single `\\n`
(the spec's "Changed in build" block).

Expected fingerprints come from design.md's Encoding, by P1-S9's `expected_fingerprint`, which is
itself checked against P1-S7's golden file; `sample_report.golden.json` holds them precomputed.

Over `Assignment/sample_diff.txt`, the rule findings are `swallowed-exception` (medium) on line 35
and `open-without-encoding` (low) on line 39; line 14 is an added line in the first hunk.

`--format markdown` belongs to P1-S11, so AC7's "every format" is run here for `text` and `json`.
"""

import importlib
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from diff_support import FINDING_DIFF, FINDING_FIELDS, MINIMAL_DIFF, SAMPLE_RULE_LINES, section
from repo_paths import REPO_ROOT
from stream_support import CLOSED, DEV_FULL, READER_GONE, needs_dev_full, run_process
from test_p0_s3_stream_failures import (
    assert_no_traceback,
    assert_only_the_error_line,
    bot_command,
    injected_command,
)
from test_p1_s4_audit_log import (
    LONE_SURROGATE,
    PROVIDER_EXITS,
    RaisingProvider,
    audit_error,
    aws_key,
    deleted_section,
    end_marker,
    input_failures,
    octal,
    only,
    quoted_section,
    read_records,
    scalars,
)
from test_p1_s9_dedupe import ANCHORS, RULE_35, RULE_39, SWALLOWED_HANDLER, expected_fingerprint

from review_bot.errors import ProviderError
from review_bot.findings import Finding
from review_bot.providers.base import ReviewRequest, ReviewResponse, TokenUsage

pytestmark = pytest.mark.integration

SCHEMA = "report/v1"
REPORT_KEYS = (
    "schema",
    "run_id",
    "provider",
    "model",
    "prompt_version",
    "threshold",
    "findings",
    "counts",
    "skipped_files",
    "usage",
    # Appended by P3-S1 (AC10, AC11): `mode` is `diff` and `context` null without --context-repo.
    "mode",
    "context",
    # Appended by P4-S1 (AC1): the policy the run used, the built-in one without --policy.
    "policy",
)
COUNT_KEYS = ("reported", "below_threshold", "dropped", "deduped", "rejected", "undecided")
FINDING_KEYS = frozenset(
    {
        "fingerprint",
        "source",
        "rule",
        "file",
        "line",
        "severity",
        "category",
        "rationale",
        "confidence",
        "triage",
        "original_rationale",
        # Appended by P3-S4 (AC6): every finding has `evidence`, the kept spans (`[]` for a rule
        # finding, or a model finding citing none), and `evidence_dropped`.
        "evidence",
        "evidence_dropped",
        # Appended by P3-S12 (AC6): the kept suggestion or null, and the reason it was dropped or
        # null.
        "suggestion",
        "suggestion_dropped",
    }
)
ZERO_COUNTS = dict.fromkeys(COUNT_KEYS, 0)

JSON = ("--format", "json")
TEXT = ("--format", "text")
FORMAT_VARIABLE = "REVIEW_BOT_FORMAT"
AUDIT_FLAG = "--audit-log"
ERROR = "review-bot: error: "

SAMPLE = REPO_ROOT / "Assignment" / "sample_diff.txt"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
SAMPLE_GOLDEN = FIXTURES / "p1_s10" / "sample_report.golden.json"
INVOICE = "billing/invoice.py"
SWALLOWED = "swallowed-exception"
OPEN_RULE = "open-without-encoding"
RUN_ID_FIELD = re.compile(r"run_id=\S+")
# Generous: it catches a hang or quadratic work, not a slow CI machine.
BOUND_S = 60.0


# Reading the report.


def reject_constant(name: str) -> Any:
    raise AssertionError(f"the report holds {name}, which isn't JSON")


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    assert len(keys) == len(set(keys)), f"duplicate keys in {keys}"
    return dict(pairs)


def report(run: Any) -> dict[str, Any]:
    """Parse stdout as exactly one `report/v1` document: pure ASCII, one line, strict JSON."""
    assert run.stdout.isascii(), "stdout isn't pure ASCII"
    assert run.stdout.endswith("\n"), "the document isn't followed by a newline"
    body = run.stdout.removesuffix("\n")
    assert body, "stdout is empty"
    assert "\n" not in body, "stdout isn't one line"
    assert "\r" not in body, "stdout holds a carriage return"
    document = json.loads(
        body, parse_constant=reject_constant, object_pairs_hook=reject_duplicate_keys
    )
    assert isinstance(document, dict), "the document isn't a JSON object"
    assert set(document) == set(REPORT_KEYS), sorted(document)
    # Report keys are appended only (Phase 3 design, "Report keys, appended only").
    assert tuple(document)[-3:] == ("mode", "context", "policy"), tuple(document)
    assert document["schema"] == SCHEMA
    assert isinstance(document["run_id"], str)
    assert document["run_id"]
    assert set(document["counts"]) == set(COUNT_KEYS), document["counts"]
    assert isinstance(document["findings"], list)
    assert isinstance(document["skipped_files"], list)
    assert isinstance(document["usage"], dict)
    lone = [s for s in scalars(document) if isinstance(s, str) and LONE_SURROGATE.search(s)]
    assert not lone, f"the document holds a lone surrogate: {lone!r}"
    return document


def without(document: dict[str, Any], *names: str) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key not in names}


def key_order(value: Any) -> Any:
    """Every object's keys in document order, through objects and lists."""
    if isinstance(value, dict):
        return [(key, key_order(nested)) for key, nested in value.items()]
    if isinstance(value, list):
        return [key_order(nested) for nested in value]
    return None


def summary_line(stderr: str) -> str:
    return stderr.splitlines()[-1]


def summary_run_id(stderr: str) -> str:
    match = RUN_ID_FIELD.search(summary_line(stderr))
    assert match, stderr
    return match.group().removeprefix("run_id=")


def without_run_id(stderr: str) -> str:
    return RUN_ID_FIELD.sub("run_id=<run_id>", stderr)


def rationale_of(rendered: str) -> str:
    """A text line's rationale: between `] ` and ` (confidence`."""
    return rendered.split("] ", 1)[1].rsplit(" (confidence", 1)[0]


def rule_entry(line: int) -> dict[str, Any]:
    """The expected `findings` entry for the sample's rule finding on `line`."""
    rule, severity, fingerprint, rendered = {
        35: (SWALLOWED, "medium", RULE_35, SAMPLE_RULE_LINES[0]),
        39: (OPEN_RULE, "low", RULE_39, SAMPLE_RULE_LINES[1]),
    }[line]
    return {
        "fingerprint": fingerprint,
        "source": "rule",
        "rule": rule,
        "file": INVOICE,
        "line": line,
        "severity": severity,
        "category": "reliability",
        "rationale": rationale_of(rendered),
        "confidence": 1.0,
        "triage": None,
        "original_rationale": None,
        # P3-S4 (AC6): a rule finding cites nothing.
        "evidence": [],
        "evidence_dropped": 0,
        # P3-S12 (AC6): a rule finding suggests nothing.
        "suggestion": None,
        "suggestion_dropped": None,
    }


# Providers, findings and diffs.


class UsageProvider:
    """Answers with `findings` and a non-zero token usage, so `usage` zeros mean something."""

    def __init__(self, findings: Sequence[Finding] = (), usage: TokenUsage | None = None) -> None:
        self.requests: list[ReviewRequest] = []
        self._findings = tuple(findings)
        self._usage = usage or TokenUsage(input_tokens=1234, output_tokens=567)

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        self.requests.append(request)
        return ReviewResponse(findings=self._findings, usage=self._usage)


def ai_finding(**overrides: Any) -> Finding:
    """An untagged AI finding on the sample's added line 14."""
    fields = {
        "file": INVOICE,
        "line": 14,
        "severity": "high",
        "category": "logic",
        "rationale": "Tax is applied to the discounted total twice.",
        "confidence": 0.8,
    }
    return Finding(**(fields | overrides))


def sample_text() -> str:
    return SAMPLE.read_bytes().decode("utf-8")


def sample_plus(*sections: str) -> str:
    text = sample_text()
    return (text if text.endswith("\n") else text + "\n") + "".join(sections)


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def review(bot: Any, log_path: Path) -> Callable[..., Any]:
    """Review `diff` with `--provider mock`, the log at `log_path`, and these flags."""

    def run(diff: Path, *flags: str, provider: Any = None) -> Any:
        return bot.review(diff, AUDIT_FLAG, str(log_path), *flags, provider=provider)

    return run


@pytest.fixture
def review_json(review: Callable[..., Any]) -> Callable[..., Any]:
    def run(diff: Path, *flags: str, provider: Any = None) -> Any:
        return review(diff, *JSON, *flags, provider=provider)

    return run


@pytest.fixture
def fail_audit_from(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Make `AuditLog.record` fail, as a full disk would, from one `stage.event` on."""

    def install(failing: str) -> None:
        audit = importlib.import_module("review_bot.audit")
        error = audit_error()
        real_record = audit.AuditLog.record
        failed = False

        def record(self: Any, stage: Any, event: str, data: Any) -> None:
            nonlocal failed
            if failed or f"{getattr(stage, 'value', stage)}.{event}" == failing:
                failed = True
                raise error
            real_record(self, stage, event, data)

        monkeypatch.setattr(audit.AuditLog, "record", record)

    return install


# The golden file agrees with the independent fingerprint encoding, so it can stand for AC1.


def test_p1_s10_golden_fingerprints_match_the_designs_encoding():
    golden = json.loads(SAMPLE_GOLDEN.read_text(encoding="utf-8"))

    fingerprints = [entry["fingerprint"] for entry in golden["findings"]]

    assert fingerprints == [RULE_35, RULE_39]
    assert golden["findings"] == [rule_entry(35), rule_entry(39)]


# AC1: the sample diff with no AI findings is one report/v1 document of its two rule findings.


def test_p1_s10_ac1_sample_with_no_ai_findings_matches_the_golden_report_and_exits_1(
    review_json, sample_diff, bot
):
    run = review_json(sample_diff, provider=bot.mock(findings=[]))

    assert run.exit_code == 1, run.stderr
    document = report(run)
    golden = json.loads(SAMPLE_GOLDEN.read_text(encoding="utf-8"))
    assert without(document, "run_id", "usage") == golden


def test_p1_s10_ac1_each_rule_finding_has_exactly_the_criterions_keys(
    review_json, sample_diff, bot
):
    run = review_json(sample_diff, provider=bot.mock(findings=[]))

    findings = report(run)["findings"]
    assert len(findings) == 2
    for entry in findings:
        assert set(entry) == FINDING_KEYS, sorted(entry)
        assert entry["source"] == "rule"
        assert entry["triage"] is None
        assert entry["original_rationale"] is None
        assert type(entry["line"]) is int
        assert isinstance(entry["confidence"], float | int)


def test_p1_s10_ac1_stdout_holds_nothing_but_the_document(review_json, sample_diff, bot):
    run = review_json(sample_diff, provider=bot.mock(findings=[]))

    assert run.stdout.strip().startswith("{")
    assert run.stdout.strip().endswith("}")
    for rendered in SAMPLE_RULE_LINES:
        assert rendered not in run.stdout
    assert "No findings" not in run.stdout


def test_p1_s10_ac1_top_level_settings_are_the_runs(review_json, sample_diff, bot):
    run = review_json(
        sample_diff, "--model", "flag-model", "--threshold", "low", provider=bot.mock(findings=[])
    )

    document = report(run)
    assert document["provider"] == "mock"
    assert document["model"] == "flag-model"
    assert document["prompt_version"] == "p1-v1"
    assert document["threshold"] == "low"


# AC2: a valid AI finding follows the rule findings, with source `ai`.


def test_p1_s10_ac2_untagged_ai_finding_follows_the_rules_with_rule_null(
    review_json, sample_diff, bot
):
    ai = ai_finding()

    run = review_json(sample_diff, provider=bot.mock(findings=[ai]))

    assert run.exit_code == 1, run.stderr
    findings = report(run)["findings"]
    assert findings[:2] == [rule_entry(35), rule_entry(39)]
    assert findings[2:] == [
        {
            "fingerprint": expected_fingerprint(INVOICE, "ai:logic", ANCHORS[14], 0),
            "source": "ai",
            "rule": None,
            "file": INVOICE,
            "line": 14,
            "severity": "high",
            "category": "logic",
            "rationale": ai.rationale,
            "confidence": 0.8,
            "triage": None,
            "original_rationale": None,
            # P3-S4 (AC6): a model finding citing nothing.
            "evidence": [],
            "evidence_dropped": 0,
            # P3-S12 (AC6): a model finding suggesting nothing.
            "suggestion": None,
            "suggestion_dropped": None,
        }
    ]


def test_p1_s10_ac2_tagged_ai_finding_carries_its_rule_and_rule_fingerprint(
    review_json, sample_diff, bot
):
    """Tagged `open-without-encoding` on line 14, far from the rule's line 39: not a duplicate."""
    run = review_json(sample_diff, provider=bot.mock(findings=[ai_finding(rule=OPEN_RULE)]))

    assert run.exit_code == 1, run.stderr
    (entry,) = report(run)["findings"][2:]
    assert entry["source"] == "ai"
    assert entry["rule"] == OPEN_RULE
    assert entry["fingerprint"] == expected_fingerprint(INVOICE, OPEN_RULE, ANCHORS[14], 0)


def test_p1_s10_ac2_ai_findings_keep_the_order_the_model_gave(review_json, sample_diff, bot):
    later = ai_finding(line=30, rationale="Second as proposed.")
    earlier = ai_finding(line=14, rationale="First line, proposed last.")

    run = review_json(sample_diff, provider=bot.mock(findings=[later, earlier]))

    findings = report(run)["findings"]
    assert [entry["source"] for entry in findings] == ["rule", "rule", "ai", "ai"]
    assert [entry["line"] for entry in findings[2:]] == [30, 14]


# AC3: dropped, deduped and below-threshold findings are counted, not listed; skipped files are.


def test_p1_s10_ac3_counts_and_skipped_files_with_one_of_each_outcome(review_json, write_diff, bot):
    diff = write_diff(sample_plus(section("README.md", "+Docs only.\n")))
    dropped = ai_finding(line=999, rationale="Not on any line of the diff.")
    deduped = ai_finding(line=35, severity="medium", category="reliability", rule=SWALLOWED)

    # `--threshold medium` puts the `open-without-encoding` rule finding (low) below it.
    run = review_json(diff, "--threshold", "medium", provider=bot.mock(findings=[dropped, deduped]))

    assert run.exit_code == 1, run.stderr
    document = report(run)
    assert document["counts"] == {
        "reported": 1,
        "below_threshold": 1,
        "dropped": 1,
        "deduped": 1,
        "rejected": 0,
        "undecided": 0,
    }
    assert document["findings"] == [rule_entry(35)]
    assert document["skipped_files"] == [{"file": "README.md", "reason": "docs"}]


def test_p1_s10_ac3_reported_counts_the_listed_findings(review_json, sample_diff, bot):
    run = review_json(sample_diff, provider=bot.mock(findings=[ai_finding()]))

    document = report(run)
    assert document["counts"]["reported"] == len(document["findings"]) == 3


@pytest.mark.parametrize(
    ("path", "reason"),
    [("README.md", "docs"), ("uv.lock", "lock")],
    ids=["docs", "lock"],
)
def test_p1_s10_ac3_each_skipped_file_is_listed_with_its_reason(
    review_json, write_diff, bot, path, reason
):
    run = review_json(write_diff(MINIMAL_DIFF + section(path)), provider=bot.mock(findings=[]))

    assert report(run)["skipped_files"] == [{"file": path, "reason": reason}]


# AC4: no findings, or every file skipped, still give a complete document.


def test_p1_s10_ac4_no_findings_gives_an_empty_list_a_complete_document_and_exit_0(
    review_json, write_diff, bot
):
    run = review_json(write_diff(MINIMAL_DIFF), provider=bot.mock(findings=[]))

    assert run.exit_code == 0, run.stderr
    document = report(run)
    assert document["findings"] == []
    assert document["counts"] == ZERO_COUNTS
    assert document["skipped_files"] == []
    assert (document["provider"], document["model"]) == ("mock", "claude-opus-5-5")
    assert (document["prompt_version"], document["threshold"]) == ("p1-v1", "low")


def test_p1_s10_ac4_only_below_threshold_findings_exit_0_with_none_listed(
    review_json, sample_diff, bot
):
    run = review_json(sample_diff, "--threshold", "critical", provider=bot.mock(findings=[]))

    assert run.exit_code == 0, run.stderr
    document = report(run)
    assert document["findings"] == []
    assert document["counts"]["below_threshold"] == 2
    assert document["counts"]["reported"] == 0


def test_p1_s10_ac4_called_provider_usage_is_reported(review_json, write_diff):
    provider = UsageProvider(usage=TokenUsage(input_tokens=1234, output_tokens=567))

    run = review_json(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 0, run.stderr
    assert len(provider.requests) == 1
    assert report(run)["usage"] == {"input_tokens": 1234, "output_tokens": 567, "cost_usd": None}


def test_p1_s10_ac4_all_files_skipped_gives_a_complete_document_with_usage_zeros(
    review_json, write_diff
):
    provider = UsageProvider()

    run = review_json(write_diff(section("README.md") + section("uv.lock")), provider=provider)

    assert run.exit_code == 0, run.stderr
    assert provider.requests == []
    document = report(run)
    assert document["findings"] == []
    assert document["counts"] == ZERO_COUNTS
    assert document["skipped_files"] == [
        {"file": "README.md", "reason": "docs"},
        {"file": "uv.lock", "reason": "lock"},
    ]
    assert document["usage"] == {"input_tokens": 0, "output_tokens": 0, "cost_usd": None}


# AC5: hostile text in a rationale and in a path stays one ASCII line and round-trips.

HOSTILE = '\x1b[31m red\nnext\u2028sep \u2192 "quoted"'
HOSTILE_PATH = f"src/a{HOSTILE}.py"


@pytest.fixture
def hostile_run(review_json: Callable[..., Any], write_diff: Callable[..., Path], bot: Any) -> Any:
    """A rule finding and an AI finding on `HOSTILE_PATH`, the AI one with a hostile rationale."""
    text = quoted_section(octal(HOSTILE_PATH), "+x\n" + SWALLOWED_HANDLER)
    ai = Finding(
        file=HOSTILE_PATH,
        line=1,
        severity="high",
        category="security",
        rationale=f"Rationale {HOSTILE} end.",
        confidence=0.9,
    )
    return review_json(write_diff(text), provider=bot.mock(findings=[ai]))


def test_p1_s10_ac5_hostile_rationale_and_path_give_one_ascii_line(hostile_run):
    assert hostile_run.exit_code == 1, hostile_run.stderr
    assert hostile_run.stdout.isascii()
    assert hostile_run.stdout.removesuffix("\n").splitlines() == [
        hostile_run.stdout.removesuffix("\n")
    ]
    for raw in ("\x1b", "\u2028", "\u2192", "\r"):
        assert raw not in hostile_run.stdout


def test_p1_s10_ac5_parsed_values_equal_the_originals(hostile_run):
    findings = report(hostile_run)["findings"]

    by_source = {entry["source"]: entry for entry in findings}
    assert set(by_source) == {"rule", "ai"}, findings
    assert by_source["ai"]["rationale"] == f"Rationale {HOSTILE} end."
    assert by_source["ai"]["file"] == HOSTILE_PATH
    assert by_source["rule"]["file"] == HOSTILE_PATH
    assert "path_quoted" not in by_source["ai"], "a valid UTF-8 path isn't quoted"
    assert "path_quoted" not in by_source["rule"], "a valid UTF-8 path isn't quoted"


def test_p1_s10_ac5_hostile_skipped_path_round_trips(review_json, write_diff, bot):
    path = f"docs/{HOSTILE}.md"
    text = MINIMAL_DIFF + quoted_section(octal(path))

    run = review_json(write_diff(text), provider=bot.mock(findings=[]))

    assert run.exit_code == 0, run.stderr
    assert report(run)["skipped_files"] == [{"file": path, "reason": "docs"}]


@pytest.mark.parametrize(
    "character",
    ["\x00", "\x7f", "\x85", "\u2029", "\ufeff", "\U0001f600", "\\"],
    ids=["nul", "del", "nel", "paragraph_separator", "bom", "astral", "backslash"],
)
def test_p1_s10_ac5_limits_other_awkward_characters_in_a_rationale_round_trip(
    review_json, sample_diff, bot, character
):
    rationale = f"Before {character} after."

    run = review_json(sample_diff, provider=bot.mock(findings=[ai_finding(rationale=rationale)]))

    assert run.exit_code == 1, run.stderr
    assert report(run)["findings"][2]["rationale"] == rationale


def test_p1_s10_ac5_limits_thousands_of_findings_stay_one_line_and_bounded(
    review_json, write_diff, bot
):
    count = 3000
    text = section("big.js", "".join(f"+value_{n} = {n}\n" for n in range(count)))
    findings = [
        Finding(
            file="big.js",
            line=n + 1,
            severity="high",
            category="logic",
            rationale=f"Finding {n}: {HOSTILE}",
            confidence=0.5,
        )
        for n in range(count)
    ]

    started = time.monotonic()
    run = review_json(write_diff(text), provider=bot.mock(findings=findings))
    elapsed = time.monotonic() - started

    assert run.exit_code == 1, run.stderr
    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    document = report(run)
    assert len(document["findings"]) == count
    assert document["findings"][-1]["rationale"] == f"Finding {count - 1}: {HOSTILE}"


# Spec Notes: a path that isn't valid UTF-8 is written in git's quoted form with path_quoted.


NON_UTF8_PY = "caf\\377.py"
NON_UTF8_DOC = "docs/caf\\377.md"
QUOTED_PY = '"caf\\377.py"'
QUOTED_DOC = '"docs/caf\\377.md"'
DECODED_PY = "caf\udcff.py"


def test_p1_s10_notes_rule_finding_and_skipped_file_on_non_utf8_paths_are_quoted(
    review_json, write_diff, bot
):
    text = quoted_section(NON_UTF8_PY, SWALLOWED_HANDLER) + quoted_section(NON_UTF8_DOC)

    run = review_json(write_diff(text), provider=bot.mock(findings=[]))

    assert run.exit_code == 1, f"expected exit 1 (not 14): {run.stderr}"
    assert run.stdout.isascii()
    document = report(run)
    (entry,) = document["findings"]
    assert len(QUOTED_PY) == 12
    assert entry["file"] == QUOTED_PY
    assert entry["path_quoted"] is True
    assert set(entry) == FINDING_KEYS | {"path_quoted"}, sorted(entry)
    assert (entry["source"], entry["rule"], entry["line"]) == ("rule", SWALLOWED, 3)
    assert document["skipped_files"] == [
        {"file": QUOTED_DOC, "reason": "docs", "path_quoted": True}
    ]


def test_p1_s10_notes_non_utf8_rule_finding_keeps_its_fingerprint(
    review_json, write_diff, bot, log_path
):
    """The fingerprint is over the decoded diff path (design.md Encoding), not the quoted form."""
    run = review_json(
        write_diff(quoted_section(NON_UTF8_PY, SWALLOWED_HANDLER)), provider=bot.mock(findings=[])
    )

    (entry,) = report(run)["findings"]
    expected = expected_fingerprint(DECODED_PY, SWALLOWED, "except Exception:", 0)
    assert entry["fingerprint"] == expected
    assert expected in json.dumps(only(read_records(log_path), "static.rules"))


def test_p1_s10_notes_only_the_object_on_a_non_utf8_path_is_flagged(review_json, write_diff, bot):
    """`Finding` rejects a lone surrogate, so an AI finding is always on a valid UTF-8 path."""
    text = quoted_section(NON_UTF8_PY, SWALLOWED_HANDLER) + FINDING_DIFF

    run = review_json(write_diff(text), provider=bot.mock(findings=[Finding(**FINDING_FIELDS)]))

    assert run.exit_code == 1, f"expected exit 1 (not 14): {run.stderr}"
    rule, ai = report(run)["findings"]
    assert (rule["file"], rule["path_quoted"]) == (QUOTED_PY, True)
    assert ai["file"] == FINDING_FIELDS["file"]
    assert "path_quoted" not in ai


def test_p1_s10_notes_only_file_skipped_on_a_non_utf8_path_exits_0(review_json, write_diff, bot):
    run = review_json(write_diff(quoted_section(NON_UTF8_DOC)), provider=bot.mock())

    assert run.exit_code == 0, f"expected exit 0 (not 14): {run.stderr}"
    assert report(run)["skipped_files"] == [
        {"file": QUOTED_DOC, "reason": "docs", "path_quoted": True}
    ]


def test_p1_s10_notes_skipped_deleted_file_is_listed_by_its_old_path(review_json, write_diff, bot):
    """design.md "Paths in `audit/v1`": a deleted file's old path, never null."""
    text = deleted_section("docs/old.md", "@@ -1,2 +0,0 @@", "-a\n-b\n") + MINIMAL_DIFF

    run = review_json(write_diff(text), provider=bot.mock(findings=[]))

    assert run.exit_code == 0, run.stderr
    assert report(run)["skipped_files"] == [{"file": "docs/old.md", "reason": "docs"}]


def test_p1_s10_notes_skipped_deleted_file_with_a_non_utf8_old_path_is_quoted(
    review_json, write_diff, bot
):
    text = (
        f'diff --git "a/{NON_UTF8_DOC}" "b/{NON_UTF8_DOC}"\ndeleted file mode 100644\n'
        f'--- "a/{NON_UTF8_DOC}"\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-a\n' + MINIMAL_DIFF
    )

    run = review_json(write_diff(text), provider=bot.mock(findings=[]))

    assert run.exit_code == 0, f"expected exit 0 (not 14): {run.stderr}"
    assert report(run)["skipped_files"] == [
        {"file": QUOTED_DOC, "reason": "docs", "path_quoted": True}
    ]


def test_p1_s10_notes_path_holding_an_end_marker_is_written_as_the_real_path(
    bot, write_diff, review_json
):
    """Review round 1: JSON paths aren't redacted, so `file` is the diff path, as in text."""
    path = f"x{end_marker()}.py"
    diff = write_diff(section(path, SWALLOWED_HANDLER))

    as_json = review_json(diff, provider=bot.mock(findings=[]))
    as_text = bot.review(diff, provider=bot.mock(findings=[]))

    assert (as_json.exit_code, as_text.exit_code) == (1, 1), as_json.stderr + as_text.stderr
    (entry,) = report(as_json)["findings"]
    assert entry["file"] == path
    assert "path_quoted" not in entry
    assert as_text.stdout.startswith(f"{entry['file']}:{entry['line']} "), as_text.stdout


# AC6: the same run twice gives the same document, run_id aside, with stable key order.


def test_p1_s10_ac6_same_run_twice_gives_identical_documents(review_json, write_diff, bot):
    diff = write_diff(sample_plus(section("README.md")))

    def once() -> Any:
        provider = bot.mock(findings=[ai_finding(), ai_finding(line=999)])
        return review_json(diff, provider=provider)

    first, second = once(), once()

    one, two = report(first), report(second)
    assert one["run_id"] != two["run_id"]
    assert without(one, "run_id") == without(two, "run_id")
    assert key_order(one) == key_order(two)
    assert first.stdout.replace(one["run_id"], "<run_id>") == second.stdout.replace(
        two["run_id"], "<run_id>"
    )


def test_p1_s10_ac6_top_level_key_order_is_the_same_for_every_outcome(
    review_json, write_diff, sample_diff, bot
):
    with_findings = report(review_json(sample_diff, provider=bot.mock(findings=[ai_finding()])))
    no_findings = report(review_json(write_diff(MINIMAL_DIFF), provider=bot.mock(findings=[])))
    all_skipped = report(review_json(write_diff(section("README.md")), provider=bot.mock()))

    orders = {tuple(document) for document in (with_findings, no_findings, all_skipped)}
    assert len(orders) == 1, orders
    count_orders = {tuple(d["counts"]) for d in (with_findings, no_findings, all_skipped)}
    assert len(count_orders) == 1, count_orders
    finding_orders = {tuple(entry) for entry in with_findings["findings"]}
    assert len(finding_orders) == 1, finding_orders


# AC7: a failure from 2 to 17 leaves stdout empty in every format, with stderr as with text.


@dataclass(frozen=True, slots=True)
class Failure:
    code: int
    diff: Path
    flags: tuple[str, ...]
    provider: Callable[[], Any]


def failures(tmp_path: Path, bot: Any) -> dict[str, Failure]:
    """Each non-report exit, triggered the way the earlier slices' tests trigger it."""
    minimal = tmp_path / "minimal.diff"
    minimal.write_bytes(MINIMAL_DIFF.encode())
    secret = tmp_path / "secret.diff"
    secret.write_bytes(section("app.py", f'+k = "{aws_key(seed=21)}"\n').encode())
    log_directory = tmp_path / "log-is-a-directory"
    log_directory.mkdir()

    def mock(**kwargs: Any) -> Callable[[], Any]:
        return lambda: bot.mock(**kwargs)

    cases = {
        "2_bad_threshold": Failure(2, minimal, ("--threshold", "urgent"), mock()),
        "2_unknown_flag": Failure(2, minimal, ("--no-such-flag",), mock()),
        "13_not_json": Failure(13, minimal, (), mock(raw_output="not json")),
        "14_provider_raises": Failure(
            14, minimal, (), lambda: RaisingProvider(RuntimeError("SENTINEL"))
        ),
        "16_secret": Failure(16, secret, (), mock()),
        "17_unwritable_log": Failure(17, minimal, (AUDIT_FLAG, str(log_directory)), mock()),
    }
    for name, (code, diff, flags) in input_failures(tmp_path).items():
        cases[name] = Failure(code, diff, flags, mock())
    for category, code in PROVIDER_EXITS.items():
        error = ProviderError(category, "detail")
        cases[f"{code}_{category.value}"] = Failure(code, minimal, (), mock(error=error))
    return cases


FAILURE_CASES = [
    "2_bad_threshold",
    "2_unknown_flag",
    "3_missing",
    "4_directory",
    "4_not_utf8",
    "5_empty",
    "6_too_large",
    "7_unavailable",
    "8_auth",
    "9_rate_limit",
    "10_refusal",
    "11_transient",
    "12_permanent",
    "13_invalid_output",
    "13_not_json",
    "14_provider_raises",
    "15_not_a_diff",
    "16_secret",
    "17_unwritable_log",
]


def test_p1_s10_ac7_cases_cover_every_code_from_2_to_17():
    assert {int(name.split("_", 1)[0]) for name in FAILURE_CASES} == set(range(2, 18))


@pytest.mark.parametrize("case", FAILURE_CASES)
def test_p1_s10_ac7_failure_leaves_stdout_empty_in_every_format_with_stderr_as_text(
    bot, tmp_path, case
):
    failure = failures(tmp_path, bot)[case]

    def run(*format_flags: str) -> Any:
        return bot.review(failure.diff, *format_flags, *failure.flags, provider=failure.provider())

    default, text, as_json = run(), run(*TEXT), run(*JSON)

    for result in (default, text, as_json):
        assert result.exit_code == failure.code, result.stderr
        assert result.stdout == ""
    assert as_json.stderr.splitlines()[0].startswith(ERROR), as_json.stderr
    assert without_run_id(as_json.stderr) == without_run_id(default.stderr)
    assert without_run_id(text.stderr) == without_run_id(default.stderr)


@pytest.mark.parametrize("failing", ["run.start", "parse.parsed", "agent.called"])
def test_p1_s10_ac7_log_failing_before_the_report_exits_17_with_stdout_empty(
    review_json, write_diff, bot, fail_audit_from, failing
):
    fail_audit_from(failing)

    run = review_json(write_diff(MINIMAL_DIFF), provider=bot.mock(findings=[]))

    assert run.exit_code == 17, run.stderr
    assert run.stdout == ""


@pytest.mark.parametrize("findings", [[], [FINDING_FIELDS]], ids=["no_findings", "findings"])
def test_p1_s10_ac7_exception_log_failing_after_the_report_keeps_the_whole_document(
    review_json, write_diff, bot, fail_audit_from, findings
):
    """design.md Compatibility: exit 17 following a delivered report keeps that report."""
    provider = bot.mock(findings=[Finding(**fields) for fields in findings])
    fail_audit_from("run.end")

    run = review_json(write_diff(FINDING_DIFF), provider=provider)

    assert run.exit_code == 17, run.stderr
    document = report(run)
    assert len(document["findings"]) == len(findings)
    assert document["counts"]["reported"] == len(findings)


# AC8: stdout closed or failing exits 14 with one error line (P0-S3 AC4), in a real process.

JSON_ON_STDOUT_FAILURE = [
    pytest.param(CLOSED, bot_command(*JSON), id="closed"),
    pytest.param(READER_GONE, bot_command(*JSON), id="reader_gone"),
    pytest.param(DEV_FULL, bot_command(*JSON), marks=needs_dev_full, id="dev_full"),
    pytest.param(
        READER_GONE, [*injected_command("many_findings"), *JSON], id="reader_gone_on_write"
    ),
    pytest.param(
        DEV_FULL,
        [*injected_command("many_findings"), *JSON],
        marks=needs_dev_full,
        id="dev_full_on_write",
    ),
]


def test_p1_s10_ac8_control_the_json_command_delivers_a_report():
    completed = run_process(bot_command(*JSON))

    assert completed.returncode == 1, completed.stderr
    assert completed.stdout.isascii()
    document = json.loads(completed.stdout)
    assert document["schema"] == SCHEMA
    assert len(document["findings"]) == 2


def test_p1_s10_ac8_control_many_findings_outgrow_the_stream_buffers():
    completed = run_process([*injected_command("many_findings"), *JSON])

    assert completed.returncode == 1, completed.stderr
    assert len(completed.stdout) > 10 * 8192
    assert len(json.loads(completed.stdout)["findings"]) == 5002


@pytest.mark.parametrize(("kind", "command"), JSON_ON_STDOUT_FAILURE)
def test_p1_s10_ac8_stdout_failure_exits_14_with_one_error_line(kind, command):
    completed = run_process(command, stdout=kind)

    assert completed.returncode == 14, completed.stderr
    assert_no_traceback(completed.stderr)
    assert_only_the_error_line(completed.stderr)


# AC9: text output is byte-identical to the goldens, and the summary doesn't change with format.


def golden_text(relative: str) -> str:
    return (FIXTURES / relative).read_bytes().decode("utf-8")


def high_on_line_14() -> list[Finding]:
    return [ai_finding(severity="high")]


def rendered_finding() -> list[Finding]:
    fields = {"line": 17, "severity": "critical", "category": "security"}
    rationale = {"rationale": "Tax is applied twice.", "confidence": 0.5}
    return [ai_finding(file=INVOICE, **fields, **rationale)]


def sentinel_finding() -> list[Finding]:
    return [
        Finding(
            file="app.py",
            line=1,
            severity="high",
            category="logic",
            rationale="SENTINEL-RATIONALE: the print is wrong.",
            confidence=0.9,
        )
    ]


SENTINEL_DIFF = section("app.py", "+print('SENTINEL-DIFF')\n")


@dataclass(frozen=True, slots=True)
class GoldenCase:
    diff_text: Callable[[], str]
    flags: tuple[str, ...]
    findings: Callable[[], list[Finding] | None]
    stdout: str
    stderr: str | None = None


GOLDEN_CASES = {
    "p0_s1_no_findings_critical": GoldenCase(
        sample_text, ("--threshold", "critical"), high_on_line_14,
        "p0_s1/no_findings_critical.golden.txt",
    ),
    "p0_s1_rendered_finding": GoldenCase(
        lambda: section(INVOICE, "+x\n" * 17), (), rendered_finding,
        "p0_s1/rendered_finding.golden.txt",
    ),
    "p0_s2_high_finding": GoldenCase(
        lambda: FINDING_DIFF, (), lambda: [Finding(**FINDING_FIELDS)],
        "p0_s2/high_finding.golden.txt",
    ),
    "p1_s4_findings": GoldenCase(
        lambda: SENTINEL_DIFF, (), sentinel_finding,
        "p1_s4/findings.stdout.golden.txt", "p1_s4/findings.stderr.golden.txt",
    ),
    "p1_s4_no_findings": GoldenCase(
        lambda: SENTINEL_DIFF, (), list,
        "p1_s4/no_findings.stdout.golden.txt", "p1_s4/no_findings.stderr.golden.txt",
    ),
    "p1_s4_all_skipped": GoldenCase(
        lambda: section("README.md", "+SENTINEL-DIFF\n"), (), lambda: None,
        "p1_s4/all_skipped.stdout.golden.txt", "p1_s4/all_skipped.stderr.golden.txt",
    ),
    "p1_s6_kept_findings": GoldenCase(
        lambda: golden_text("p1_s6/anchors.diff"), (),
        lambda: [ai_finding(line=n, rationale="Rationale text.", confidence=0.9)
                 for n in (12, 14, 30, 37, 41)],
        "p1_s6/kept_findings.golden.txt",
    ),
}  # fmt: skip
# How text is selected: by default, by the flag, by the variable, and by the flag over it.
TEXT_SELECTIONS = {
    "default": ((), {}),
    "flag": (TEXT, {}),
    "variable": ((), {FORMAT_VARIABLE: "text"}),
    "flag_over_json_variable": (TEXT, {FORMAT_VARIABLE: "json"}),
}


@pytest.fixture
def golden_run(
    bot: Any, write_diff: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Any]:
    def run(case: GoldenCase, flags: Sequence[str], environment: dict[str, str]) -> Any:
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        provider = bot.mock(findings=case.findings())
        return bot.review(write_diff(case.diff_text()), *case.flags, *flags, provider=provider)

    return run


@pytest.mark.parametrize("selection", TEXT_SELECTIONS)
@pytest.mark.parametrize("case", GOLDEN_CASES)
def test_p1_s10_ac9_text_stdout_is_byte_identical_to_the_golden(golden_run, case, selection):
    golden_case = GOLDEN_CASES[case]
    flags, environment = TEXT_SELECTIONS[selection]

    run = golden_run(golden_case, flags, environment)

    assert run.exit_code in (0, 1), run.stderr
    assert run.stdout == golden_text(golden_case.stdout)


@pytest.mark.parametrize("selection", TEXT_SELECTIONS)
@pytest.mark.parametrize("case", [name for name, c in GOLDEN_CASES.items() if c.stderr])
def test_p1_s10_ac9_text_stderr_is_the_golden_plus_run_id(golden_run, case, selection):
    golden_case = GOLDEN_CASES[case]
    flags, environment = TEXT_SELECTIONS[selection]
    assert golden_case.stderr

    run = golden_run(golden_case, flags, environment)

    expected = golden_text(golden_case.stderr).splitlines()
    expected[-1] = f"{expected[-1]} run_id={summary_run_id(run.stderr)}"
    assert run.stderr.splitlines() == expected


@pytest.mark.parametrize("case", [name for name, c in GOLDEN_CASES.items() if c.stderr])
def test_p1_s10_ac9_json_summary_line_is_the_text_summary(golden_run, case):
    golden_case = GOLDEN_CASES[case]

    run = golden_run(golden_case, JSON, {})

    assert run.exit_code in (0, 1), run.stderr
    report(run)
    expected = golden_text(golden_case.stderr).splitlines()[-1]
    assert summary_line(run.stderr) == f"{expected} run_id={summary_run_id(run.stderr)}"


def test_p1_s10_ac9_json_and_text_stderr_match_on_the_sample(bot, sample_diff):
    def run(*flags: str) -> Any:
        return bot.review(sample_diff, *flags, provider=bot.mock(findings=[ai_finding()]))

    text, as_json = run(), run(*JSON)

    assert (text.exit_code, as_json.exit_code) == (1, 1)
    assert without_run_id(summary_line(as_json.stderr)) == without_run_id(summary_line(text.stderr))


# AC10: an unknown format exits 2 and names its source; the flag beats the variable.


@pytest.mark.parametrize("value", ["yaml", "", "JSON ", "markdown\n"], ids=repr)
def test_p1_s10_ac10_unknown_format_flag_exits_2_naming_the_flag(bot, write_diff, value):
    provider = bot.mock()

    run = bot.review(write_diff(MINIMAL_DIFF), "--format", value, provider=provider)

    assert run.exit_code == 2, run.stderr
    assert "--format" in run.stderr
    assert run.stdout == ""
    assert provider.requests == []


def test_p1_s10_ac10_unknown_format_variable_exits_2_naming_the_variable(
    bot, write_diff, monkeypatch
):
    monkeypatch.setenv(FORMAT_VARIABLE, "yaml")
    provider = bot.mock()

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 2, run.stderr
    assert FORMAT_VARIABLE in run.stderr
    assert run.stdout == ""
    assert provider.requests == []


def test_p1_s10_ac10_unknown_format_error_is_one_safe_line(bot, write_diff, monkeypatch):
    monkeypatch.setenv(FORMAT_VARIABLE, "yaml\x1b[31m\nINJECTED")

    run = bot.review(write_diff(MINIMAL_DIFF), provider=bot.mock())

    assert run.exit_code == 2, run.stderr
    assert "\x1b" not in run.stderr
    assert not any(line.startswith("INJECTED") for line in run.stderr.splitlines())


def test_p1_s10_ac10_variable_alone_selects_json(review, sample_diff, bot, monkeypatch):
    monkeypatch.setenv(FORMAT_VARIABLE, "json")

    run = review(sample_diff, provider=bot.mock(findings=[]))

    assert run.exit_code == 1, run.stderr
    assert report(run)["findings"] == [rule_entry(35), rule_entry(39)]


def test_p1_s10_ac10_flag_beats_an_invalid_variable(review_json, sample_diff, bot, monkeypatch):
    monkeypatch.setenv(FORMAT_VARIABLE, "yaml")

    run = review_json(sample_diff, provider=bot.mock(findings=[]))

    assert run.exit_code == 1, run.stderr
    assert report(run)["schema"] == SCHEMA


def test_p1_s10_ac10_invalid_flag_over_a_valid_variable_names_the_flag(
    bot, write_diff, monkeypatch
):
    monkeypatch.setenv(FORMAT_VARIABLE, "json")

    run = bot.review(write_diff(MINIMAL_DIFF), "--format", "yaml", provider=bot.mock())

    assert run.exit_code == 2, run.stderr
    assert "--format" in run.stderr
    assert run.stdout == ""


def test_p1_s10_ac10_format_appears_in_the_logs_run_start(review_json, sample_diff, bot, log_path):
    """design.md Audit log: `run.start` records the format."""
    review_json(sample_diff, provider=bot.mock(findings=[]))

    assert "json" in scalars(only(read_records(log_path), "run.start"))


# AC11: the document's run_id is the summary's and the log's.


@pytest.mark.parametrize(
    "text", [MINIMAL_DIFF, section("README.md")], ids=["reviewed", "all_skipped"]
)
def test_p1_s10_ac11_run_id_matches_the_summary_and_every_log_record(
    review_json, write_diff, bot, log_path, text
):
    run = review_json(write_diff(text), provider=bot.mock(findings=[]))

    assert run.exit_code == 0, run.stderr
    run_id = report(run)["run_id"]
    assert run_id == summary_run_id(run.stderr)
    assert {record["run_id"] for record in read_records(log_path)} == {run_id}


def test_p1_s10_ac11_each_run_has_its_own_run_id(review_json, sample_diff, bot):
    first = report(review_json(sample_diff, provider=bot.mock(findings=[])))
    second = report(review_json(sample_diff, provider=bot.mock(findings=[])))

    assert first["run_id"] != second["run_id"]


def test_p1_s10_ac11_child_process_run_id_matches_its_summary():
    completed = run_process(bot_command(*JSON))

    assert completed.returncode == 1, completed.stderr
    assert json.loads(completed.stdout)["run_id"] == summary_run_id(completed.stderr)
