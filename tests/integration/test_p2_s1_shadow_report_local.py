r"""P2-S1: get a shadow report of what the bot would post, for a local diff.

Written from the spec's criteria and design.md's Interfaces, Comment state and Safety sections,
before the developer's code was read. Every run calls `review_bot.pr.cli.main` in process, with
`["shadow", <diff>, "--out", <dir>, ...]` and a scripted `MockProvider` passed as `provider=`
(the seam is the one place to change: `ShadowRunner.run`). No run reaches a model or GitHub.

Bodies and the summary are read as GitHub-flavoured Markdown by `markdown-it-py` (CommonMark plus
the table and linkify extensions), the dev dependency P1-S11 introduced. The parser doesn't know
GitHub's references (`#12`, `GH-3`, `owner/repo#4`, commit SHAs) or mentions, so AC11 also checks
the spec's structural guarantee directly: no trigger is left in text outside code, and each word
of the hostile value holding one sits inside a code span.

Over `Assignment/sample_diff.txt`, the rule findings are `swallowed-exception` (medium) on line 35
and `open-without-encoding` (low) on line 39; the AI finding used here is `logic` (high) on line 14.
"""

import hashlib
import importlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from diff_support import MINIMAL_DIFF, section
from markdown_it.token import Token
from test_p1_s4_audit_log import aws_key, github_token, quoted_section, read_records
from test_p1_s9_dedupe import ANCHORS, RULE_35, RULE_39, SWALLOWED_HANDLER, expected_fingerprint
from test_p1_s10_json_output import INVOICE, OPEN_RULE, SWALLOWED, ai_finding
from test_p1_s11_markdown_output import (
    GFM_TRIGGER,
    PARSER,
    RULE_RATIONALES,
    document_text,
    inline_children,
    list_items,
    outside_code,
    visible,
    words_in_order,
)

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

# design.md Interfaces, in its order.
SHADOW_KEYS = (
    "schema",
    "run_id",
    "mode",
    "origin",
    "outcome",
    "reason",
    "pr",
    "event_at",
    "started_at",
    "finished_at",
    "report",
    "summary_body",
    "comments",
    "withheld",
    "caps",
    "over_budget",
    # P2-S7 (design.md, `shadow/v1`: later slices add keys, never remove them).
    "scope",
    "since",
    # P2-S9.
    "config",
    # P3-S11.
    "codeowners",
    # P3-S8 (Phase 3 design.md, Report keys: `shadow/v1` adds the same `context` as `report/v1`).
    "context",
    # P3-S9 (Phase 3 design.md, Report keys: `shadow/v1` adds `issues`).
    "issues",
)
# `applied` joined in P2-S5, `owners` in P3-S11, `evidence` and `evidence_dropped` in P3-S4, and
# `suggestion` in P3-S12 (design.md, `shadow/v1`: later slices add keys, never remove them).
COMMENT_KEYS = frozenset(
    {
        "action", "fingerprint", "rule_part", "anchor", "source", "severity", "file", "line",
        "body", "applied", "owners", "evidence", "evidence_dropped", "suggestion",
    }
)  # fmt: skip
WITHHELD_KEYS = frozenset({"fingerprint", "file", "line", "reason", "owners"})
TIMESTAMPS = ("event_at", "started_at", "finished_at")
RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
SUMMARY_LIMIT = 60_000
AUDIT_VARIABLE = "REVIEW_BOT_AUDIT_LOG"
# Generous: it catches a hang or quadratic work, not a slow CI machine.
BOUND_S = 60.0

# What GitHub can turn into a mention, link, reference or HTML comment outside code (design.md
# "What a comment can't do"): `@`, URLs, `#123`, `GH-123`, `owner/repo#123`, 7-to-40-hex words.
HEX_WORD = r"(?<![0-9A-Za-z])[0-9a-fA-F]{7,40}(?![0-9A-Za-z])"
REFERENCE_TRIGGER = re.compile(rf"#\d|GH-\d|<!--|{HEX_WORD}", re.IGNORECASE)


def anchor_of(line_text: str) -> str:
    """ADR 0003's anchored-line hash, first 16 hex digits, written out independently."""
    collapsed = " ".join(line_text.split())
    return hashlib.sha256(collapsed.encode("utf-8", "surrogatepass")).hexdigest()[:16]


# Phase 1 design.md "Fingerprints and dedup": an untagged AI finding's rule part is `ai:<category>`.
AI_RULE_PART = "ai:logic"
AI_14 = expected_fingerprint(INVOICE, AI_RULE_PART, ANCHORS[14], 0)


def marker(fingerprint: str, rule_part: str, anchor: str) -> str:
    return f"<!-- review-bot:v1 fingerprint={fingerprint} rule={rule_part} anchor={anchor} -->"


# Running the command and reading what it writes.


@dataclass(frozen=True, slots=True)
class Run:
    exit_code: int
    stdout: str
    stderr: str
    out: Path

    @property
    def shadow(self) -> dict[str, Any]:
        return load_shadow(self.out)


class ShadowRunner:
    def __init__(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        self._capsys = capsys
        self._tmp_path = tmp_path

    def run(self, diff: Path, *flags: str, provider: Any = None, out: Path | None = None) -> Run:
        """`shadow <diff> --out DIR --provider mock`, in process, with `provider` injected.

        `--provider mock` matches `bot.review`, so `report.provider` agrees with AC1's reference.
        """
        pr_cli = importlib.import_module("review_bot.pr.cli")
        out = out or self._tmp_path / "out"
        self._capsys.readouterr()
        exit_code = pr_cli.main(
            ["shadow", str(diff), "--out", str(out), "--provider", "mock", *flags],
            provider=provider,
        )
        captured = self._capsys.readouterr()
        return Run(exit_code, captured.out, captured.err, out)


@pytest.fixture
def shadow(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> ShadowRunner:
    return ShadowRunner(capsys, tmp_path)


def load_shadow(out: Path) -> dict[str, Any]:
    """`DIR/shadow.json`: one ASCII line of strict JSON holding exactly design.md's keys."""
    raw = (out / "shadow.json").read_bytes()
    assert raw.isascii(), "shadow.json isn't pure ASCII"
    body = raw.removesuffix(b"\n")
    assert body, "shadow.json is empty"
    assert b"\n" not in body, "shadow.json isn't one line"
    document = json.loads(body)
    assert isinstance(document, dict)
    assert tuple(document) == SHADOW_KEYS, list(document)
    assert document["schema"] == "shadow/v1"
    for entry in document["comments"]:
        assert set(entry) == COMMENT_KEYS, sorted(entry)
    for entry in document["withheld"]:
        assert set(entry) == WITHHELD_KEYS, sorted(entry)
    return document


def everything_in(out: Path) -> bytes:
    return b"".join(path.read_bytes() for path in sorted(out.rglob("*")) if path.is_file())


def body_tokens(body: str) -> list[Token]:
    return PARSER.parse(body)


def body_text(body: str) -> str:
    """What a reader sees in a body: its inline text and code spans, not the HTML marker."""
    return document_text(body_tokens(body))


def summary_text(document: dict[str, Any]) -> str:
    return document_text(PARSER.parse(document["summary_body"]))


def last_line(body: str) -> str:
    return body.removesuffix("\n").split("\n")[-1]


@pytest.fixture
def sample_with(shadow: ShadowRunner, sample_diff: Path) -> Callable[..., Run]:
    def run(*findings: Finding, flags: tuple[str, ...] = ()) -> Run:
        return shadow.run(sample_diff, *flags, provider=MockProvider(findings=list(findings)))

    return run


def many_findings_diff(write_diff: Callable[..., Path], file: str, count: int) -> Path:
    return write_diff(section(file, "".join(f"+value_{n} = {n}\n" for n in range(count))))


def finding_at(file: str, line: int, severity: str = "high") -> Finding:
    return Finding(
        file=file, line=line, severity=severity, category="logic", rationale="R.", confidence=0.9
    )


# AC1: the sample diff and one AI finding: one shadow/v1 line, three ordered `create` comments.


def test_p2_s1_ac1_sample_with_one_ai_finding_is_a_local_reviewed_shadow_line_and_exits_0(
    sample_with,
):
    run = sample_with(ai_finding())

    assert run.exit_code == 0, run.stderr
    document = run.shadow
    assert (document["mode"], document["origin"], document["outcome"]) == (
        "shadow",
        "local",
        "reviewed",
    )
    assert document["pr"] is None
    assert document["reason"] is None
    assert (run.out / "shadow.md").is_file()


def test_p2_s1_ac1_report_is_review_bots_json_document_for_the_same_run(
    sample_with, bot, sample_diff
):
    run = sample_with(ai_finding())
    reference = bot.review(
        sample_diff, "--format", "json", provider=MockProvider(findings=[ai_finding()])
    )

    assert reference.exit_code == 1, reference.stderr
    expected = json.loads(reference.stdout)
    actual = run.shadow["report"]
    assert {k: v for k, v in actual.items() if k != "run_id"} == {
        k: v for k, v in expected.items() if k != "run_id"
    }


def test_p2_s1_ac1_three_create_comments_with_their_findings_fields_in_severity_order(
    sample_with,
):
    document = sample_with(ai_finding()).shadow

    fields = [
        (c["action"], c["fingerprint"], c["rule_part"], c["anchor"], c["source"], c["severity"],
         c["file"], c["line"])
        for c in document["comments"]
    ]  # fmt: skip
    assert fields == [
        ("create", AI_14, AI_RULE_PART, anchor_of(ANCHORS[14]), "ai", "high", INVOICE, 14),
        ("create", RULE_35, SWALLOWED, anchor_of(ANCHORS[35]), "rule", "medium", INVOICE, 35),
        ("create", RULE_39, OPEN_RULE, anchor_of(ANCHORS[39]), "rule", "low", INVOICE, 39),
    ]
    reported = {f["fingerprint"] for f in document["report"]["findings"]}
    assert {c["fingerprint"] for c in document["comments"]} == reported
    assert document["withheld"] == []


def test_p2_s1_interfaces_shadow_md_is_the_summary_then_each_comment(sample_with):
    run = sample_with(ai_finding())

    markdown = (run.out / "shadow.md").read_bytes().decode("utf-8")
    document = run.shadow
    assert markdown.startswith(document["summary_body"])
    for comment in document["comments"]:
        assert words_in_order(rationale_text(comment), document_text(PARSER.parse(markdown)))


def rationale_text(comment: dict[str, Any]) -> str:
    return {14: ai_finding().rationale, **RULE_RATIONALES}[comment["line"]]


# AC2: each body shows the finding, asks for a reaction, and ends with the exact marker.


REACTION = (
    "Is this right and worth acting on? React with a thumbs-up if so, or a thumbs-down if not."
)
HEADERS = {
    14: "high logic (source ai, confidence 0.80)",
    35: f"medium reliability (source rule, rule {SWALLOWED}, confidence 1.00)",
    39: f"low reliability (source rule, rule {OPEN_RULE}, confidence 1.00)",
}


def paragraphs(body: str) -> list[str]:
    """Each top-level block's visible text; the HTML marker block as its raw line."""
    tokens = body_tokens(body)
    blocks = []
    for index, token in enumerate(tokens):
        if token.type == "html_block":
            blocks.append(token.content.strip())
        elif token.type == "paragraph_open":
            blocks.append(visible(tokens[index + 1].children or ()))
    assert [t.type for t in tokens if t.level == 0 and not t.type.endswith("_close")] == [
        "paragraph_open", "paragraph_open", "paragraph_open", "html_block"
    ], [t.type for t in tokens]  # fmt: skip
    return blocks


@pytest.mark.parametrize("line", [14, 35, 39], ids=["ai_14", "rule_35", "rule_39"])
def test_p2_s1_ac2_body_is_the_finding_then_its_rationale_then_the_reaction_then_the_marker(
    sample_with, line
):
    (comment,) = [c for c in sample_with(ai_finding()).shadow["comments"] if c["line"] == line]

    header, rationale, reaction, last = paragraphs(comment["body"])
    assert header == HEADERS[line]
    assert rationale == rationale_text(comment)
    assert reaction == REACTION
    assert last == last_line(comment["body"])


def test_p2_s1_ac2_last_line_is_exactly_the_marker_with_the_adr_0003_anchor(sample_with):
    for comment in sample_with(ai_finding()).shadow["comments"]:
        expected = marker(
            comment["fingerprint"], comment["rule_part"], anchor_of(ANCHORS[comment["line"]])
        )
        assert last_line(comment["body"]) == expected


# AC3: no findings.


def test_p2_s1_ac3_no_findings_has_no_comments_and_the_clean_sentence(shadow, write_diff):
    run = shadow.run(write_diff(MINIMAL_DIFF), provider=MockProvider(findings=[]))

    assert run.exit_code == 0, run.stderr
    document = run.shadow
    assert document["outcome"] == "reviewed"
    assert document["comments"] == []
    assert document["withheld"] == []
    assert summary_text(document) == "Review bot: reviewed. No findings at or above low."


# AC4: a secret in the diff blocks the review and names only the kind and line.


def test_p2_s1_ac4_aws_key_blocks_the_review_without_a_call_or_the_value(shadow, write_diff):
    key = aws_key(seed=3)
    diff = write_diff(section("app.py", f'+x = 1\n+key = "{key}"\n'))  # the key is diff line 6
    provider = MockProvider(findings=[])

    run = shadow.run(diff, provider=provider)

    assert run.exit_code == 0, run.stderr
    document = run.shadow
    assert (document["outcome"], document["report"]) == ("blocked_secret", None)
    assert provider.requests == []
    text = summary_text(document)
    assert "aws-access-key" in text, text
    assert re.search(r"line\W{0,3}6(?!\d)", text, re.IGNORECASE), text
    assert key.encode() not in everything_in(run.out)
    assert key not in run.stderr + run.stdout


# AC5: inputs that can't be reviewed are skipped with a reason; a missing file is exit 3.


SKIPS = {
    "too_large": (MINIMAL_DIFF.encode(), ("--max-diff-bytes", "10")),
    "empty": (b"", ()),
    "not_a_diff": (b"+x\n", ()),
    "unreadable": (MINIMAL_DIFF.encode() + b"+caf\xe9\n", ()),
}


@pytest.mark.parametrize("reason", SKIPS)
def test_p2_s1_ac5_unreviewable_diff_is_skipped_with_its_reason_and_no_call(
    shadow, write_diff, reason
):
    content, flags = SKIPS[reason]
    provider = MockProvider(findings=[])

    run = shadow.run(write_diff(content), *flags, provider=provider)

    assert run.exit_code == 0, run.stderr
    document = run.shadow
    assert (document["outcome"], document["reason"]) == ("skipped", reason)
    assert provider.requests == []
    assert document["comments"] == []


def test_p2_s1_ac5_whitespace_only_diff_is_empty(shadow, write_diff):
    run = shadow.run(write_diff(b"  \n\n"), provider=MockProvider(findings=[]))

    assert run.exit_code == 0, run.stderr
    assert (run.shadow["outcome"], run.shadow["reason"]) == ("skipped", "empty")


def test_p2_s1_ac5_missing_diff_exits_3_with_no_report(shadow, tmp_path):
    provider = MockProvider(findings=[])

    run = shadow.run(tmp_path / "missing.diff", provider=provider)

    assert run.exit_code == 3, run.stderr
    assert not (run.out / "shadow.json").exists()
    assert not (run.out / "shadow.md").exists()
    assert provider.requests == []


# AC6: a provider failure is a written report with outcome `failed`, and its own exit code.


def test_p2_s1_ac6_auth_failure_is_outcome_failed_reason_auth_exit_8(shadow, write_diff):
    provider = MockProvider(error=ProviderError(ErrorCategory.AUTH, "Not logged in."))

    run = shadow.run(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 8, run.stderr
    document = run.shadow
    assert (document["outcome"], document["reason"]) == ("failed", "auth")
    assert document["comments"] == []


# AC7: over the default limit of 10, the rest are withheld `over_limit` and listed in the summary.

SEVERITIES_12 = {
    ("src/a.py", 1): "low", ("src/a.py", 2): "high", ("src/a.py", 3): "medium",
    ("src/a.py", 4): "critical", ("src/a.py", 5): "low", ("src/a.py", 6): "high",
    ("src/b.py", 1): "high", ("src/b.py", 2): "low", ("src/b.py", 3): "critical",
    ("src/b.py", 4): "medium", ("src/b.py", 5): "high", ("src/b.py", 6): "low",
}  # fmt: skip


@pytest.fixture
def twelve(shadow: ShadowRunner, write_diff: Callable[..., Path]) -> Run:
    body = "".join(f"+line_{n} = {n}\n" for n in range(1, 7))
    diff = write_diff(section("src/b.py", body) + section("src/a.py", body))
    findings = [
        finding_at(file, line, severity) for (file, line), severity in SEVERITIES_12.items()
    ]
    return shadow.run(diff, provider=MockProvider(findings=findings))


def test_p2_s1_ac7_first_10_in_order_are_created(twelve):
    assert twelve.exit_code == 0, twelve.stderr
    comments = twelve.shadow["comments"]

    expected = sorted(SEVERITIES_12, key=lambda k: (-RANK[SEVERITIES_12[k]], k[0], k[1]))[:10]
    assert [(c["file"], c["line"]) for c in comments] == expected
    assert {c["action"] for c in comments} == {"create"}


def test_p2_s1_ac7_last_2_are_withheld_over_limit_and_listed_by_file_and_line(twelve):
    document = twelve.shadow

    withheld = [(w["file"], w["line"], w["reason"]) for w in document["withheld"]]
    assert sorted(withheld) == [("src/b.py", 2, "over_limit"), ("src/b.py", 6, "over_limit")]
    text = summary_text(document)
    assert text.startswith("Review bot: reviewed. 10 comments, 2 withheld.\nWithheld findings:"), (
        text
    )
    items = [item.text for item in list_items(PARSER.parse(document["summary_body"]))]
    assert items == ["src/b.py:2: over_limit", "src/b.py:6: over_limit"]


# AC8: 1,000 findings: 20 listed, then "and 970 more", at most 60,000 characters.


def test_p2_s1_ac8_thousand_findings_list_20_withheld_then_the_rest_as_a_count(shadow, write_diff):
    diff = many_findings_diff(write_diff, "big.py", 1000)
    findings = [finding_at("big.py", n) for n in range(1, 1001)]

    started = time.monotonic()
    run = shadow.run(diff, provider=MockProvider(findings=findings))
    elapsed = time.monotonic() - started

    assert run.exit_code == 0, run.stderr
    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    document = run.shadow
    assert len(document["comments"]) == 10
    assert len(document["withheld"]) == 990
    items = [item.text for item in list_items(PARSER.parse(document["summary_body"]))]
    first_20 = document["withheld"][:20]
    assert items == [f"{w['file']}:{w['line']}: over_limit" for w in first_20] + ["and 970 more"]
    assert len(document["summary_body"]) <= SUMMARY_LIMIT


def test_p2_s1_ac8_limits_long_paths_still_keep_the_summary_under_60000(shadow, write_diff):
    """20 withheld entries on 3,500-character paths would exceed the cap if listed in full."""
    paths = ["abc/" * 875 + f"f{n:02}.py" for n in range(25)]
    diff = write_diff(
        "".join(section(path, "".join(f"+v{n}\n" for n in range(40))) for path in paths)
    )
    findings = [finding_at(path, line) for path in paths for line in range(1, 41)]

    run = shadow.run(diff, "--max-diff-bytes", "2000000", provider=MockProvider(findings=findings))

    assert run.exit_code == 0, run.stderr
    document = run.shadow
    assert len(document["withheld"]) == 990
    assert len(document["summary_body"]) <= SUMMARY_LIMIT


# AC9: a secret in a rationale withholds that finding, and the value is nowhere under DIR.


def test_p2_s1_ac9_precondition_scan_secrets_detects_the_token():
    secret_scan = importlib.import_module("review_bot.secret_scan")

    assert secret_scan.scan_secrets(f"+x = {github_token(seed=5)}\n")


def test_p2_s1_ac9_token_in_a_rationale_is_withheld_possible_secret_and_written_nowhere(
    sample_with,
):
    token = github_token(seed=5)

    run = sample_with(ai_finding(rationale=f"Leaked {token} in the tax code."))

    assert run.exit_code == 0, run.stderr
    document = run.shadow
    assert AI_14 not in {c["fingerprint"] for c in document["comments"]}
    assert [c["line"] for c in document["comments"]] == [35, 39]
    assert [(w["fingerprint"], w["reason"]) for w in document["withheld"]] == [
        (AI_14, "possible_secret")
    ]
    assert token.encode() not in everything_in(run.out)


def test_p2_s1_ac9_token_is_not_in_the_default_audit_log_under_dir(sample_with, monkeypatch):
    """Without `REVIEW_BOT_AUDIT_LOG` the log is `DIR/audit.jsonl` (design.md Safety), so in DIR."""
    monkeypatch.delenv(AUDIT_VARIABLE)
    token = github_token(seed=6)

    run = sample_with(ai_finding(rationale=f"Leaked {token} in the tax code."))

    assert run.exit_code == 0, run.stderr
    assert (run.out / "audit.jsonl").is_file()
    assert token.encode() not in everything_in(run.out)


# AC10: a rule finding on a non-UTF-8 path is withheld `not_commentable`, listed quoted.


def test_p2_s1_ac10_rule_finding_on_a_non_utf8_path_is_withheld_and_listed_quoted(
    shadow, write_diff
):
    diff = write_diff(quoted_section("caf\\377.py", SWALLOWED_HANDLER))

    run = shadow.run(diff, provider=MockProvider(findings=[]))

    assert run.exit_code == 0, run.stderr
    document = run.shadow
    assert document["comments"] == []
    assert [(w["line"], w["reason"]) for w in document["withheld"]] == [(3, "not_commentable")]
    assert '"caf\\377.py"' in summary_text(document), summary_text(document)


# AC11: references, mentions, links and comments in a rationale are inert, in code spans.

INERT_TOKENS = {
    "issue": "#12",
    "gh_issue": "GH-3",
    "cross_repo": "owner/repo#4",
    "sha_9": "deadbeef1",
    "sha_7": "0123abc",
    "forged_marker": "<!-- review-bot:v1 fingerprint=0000000000000000 -->",
    "mention": "@x",
    "url": "http://evil",
}


def trigger_words(value: str) -> list[str]:
    return [w for w in value.split() if GFM_TRIGGER.search(w) or REFERENCE_TRIGGER.search(w)]


def assert_inert_on_github(tokens: list[Token], value: str, *, markers: int) -> None:
    """No link, mention, reference or HTML comment forms; each trigger word is in a code span."""
    children = list(inline_children(tokens))
    assert not any(c.type == "link_open" for c in children), "a link formed"
    html = [t for t in [*tokens, *children] if t.type in ("html_block", "html_inline")]
    assert len(html) == markers, [t.content for t in html]
    text = outside_code(tokens)
    assert not GFM_TRIGGER.findall(text), f"{GFM_TRIGGER.findall(text)} outside a code span"
    assert not REFERENCE_TRIGGER.findall(text), (
        f"{REFERENCE_TRIGGER.findall(text)} outside a code span"
    )
    spans = [c.content for c in children if c.type == "code_inline"]
    for word in trigger_words(value):
        assert any(word in span for span in spans), f"{word!r} isn't in a code span: {spans}"


@pytest.mark.parametrize("value", INERT_TOKENS.values(), ids=INERT_TOKENS.keys())
def test_p2_s1_ac11_rationale_token_is_inert_in_the_comment_body(sample_with, value):
    run = sample_with(ai_finding(rationale=f"Before {value} after."))

    assert run.exit_code == 0, run.stderr
    (comment,) = [c for c in run.shadow["comments"] if c["line"] == 14]
    tokens = body_tokens(comment["body"])
    assert_inert_on_github(tokens, value, markers=1)
    (html,) = [t for t in tokens if t.type == "html_block"]
    assert html.content.strip() == last_line(comment["body"])
    assert last_line(comment["body"]) == marker(AI_14, AI_RULE_PART, anchor_of(ANCHORS[14]))
    assert words_in_order(value, body_text(comment["body"]))


@pytest.mark.parametrize("value", INERT_TOKENS.values(), ids=INERT_TOKENS.keys())
def test_p2_s1_ac11_rationale_token_is_inert_in_review_bot_markdown(bot, sample_diff, value):
    """Fixes issue #37: the same rule for `review-bot --format markdown`."""
    provider = MockProvider(findings=[ai_finding(rationale=f"Before {value} after.")])

    run = bot.review(sample_diff, "--format", "markdown", provider=provider)

    assert run.exit_code == 1, run.stderr
    assert_inert_on_github(PARSER.parse(run.stdout), value, markers=0)


def test_p2_s1_ac11_check_catches_a_raw_reference():
    """The check itself: a raw `#12` outside code fails it, the same value in a span passes."""
    assert_inert_on_github(PARSER.parse("Before `#12` after."), "#12", markers=0)
    with pytest.raises(AssertionError):
        assert_inert_on_github(PARSER.parse("Before \\#12 after."), "#12", markers=0)


# AC12: a long rationale is cut in the body and kept whole in the report.


def test_p2_s1_ac12_rationale_over_2000_is_cut_in_the_body_and_whole_in_the_report(sample_with):
    # Spaces at positions 4, 14, 24, ...; character 2,000 (index 1,999) is an `x`.
    rationale = "".join(" " if n % 10 == 4 else "x" for n in range(5000))

    run = sample_with(ai_finding(rationale=rationale))

    assert run.exit_code == 0, run.stderr
    document = run.shadow
    (comment,) = [c for c in document["comments"] if c["line"] == 14]
    text = body_text(comment["body"])
    assert re.search(re.escape(rationale[:2000]) + r"\s*\(cut\)", text), text[-200:]
    assert rationale[:2001] not in text
    (reported,) = [f for f in document["report"]["findings"] if f["line"] == 14]
    assert reported["rationale"] == rationale


# AC13: `--out` is created if missing, and checked before the diff is read.


def test_p2_s1_ac13_missing_out_directory_is_created(shadow, write_diff, tmp_path):
    out = tmp_path / "new" / "nested"

    run = shadow.run(write_diff(MINIMAL_DIFF), provider=MockProvider(findings=[]), out=out)

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"


@pytest.mark.parametrize("where", ["a_file", "under_a_file"])
def test_p2_s1_ac13_unwritable_out_exits_2_naming_out_before_reading_the_diff(
    shadow, tmp_path, where
):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    out = blocker if where == "a_file" else blocker / "out"
    provider = MockProvider(findings=[])

    # The diff is missing: reading it first would exit 3, not 2.
    run = shadow.run(tmp_path / "missing.diff", provider=provider, out=out)

    assert run.exit_code == 2, run.stderr
    assert "--out" in run.stderr
    assert provider.requests == []


# AC14: two identical runs differ only in `run_id` and the timestamps; `run_id` is the log's.


def test_p2_s1_ac14_same_run_twice_differs_only_in_run_id_and_timestamps(
    shadow, sample_diff, tmp_path, monkeypatch
):
    documents, logs = [], []
    for n in range(2):
        log = tmp_path / f"audit{n}.jsonl"
        monkeypatch.setenv(AUDIT_VARIABLE, str(log))
        run = shadow.run(
            sample_diff, provider=MockProvider(findings=[ai_finding()]), out=tmp_path / f"o{n}"
        )
        assert run.exit_code == 0, run.stderr
        documents.append(run.shadow)
        logs.append(log)

    def stable(document: dict[str, Any]) -> dict[str, Any]:
        kept = {k: v for k, v in document.items() if k not in ("run_id", *TIMESTAMPS)}
        kept["report"] = {k: v for k, v in kept["report"].items() if k != "run_id"}
        return kept

    assert stable(documents[0]) == stable(documents[1])
    assert documents[0]["run_id"] != documents[1]["run_id"]
    for document, log in zip(documents, logs, strict=True):
        assert {record["run_id"] for record in read_records(log)} == {document["run_id"]}
