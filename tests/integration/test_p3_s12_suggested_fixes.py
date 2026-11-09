r"""P3-S12: a context-mode finding can carry a suggested fix for its anchored line.

Written from the spec's criteria, Limits and Interfaces, Phase 3 design.md (Q7, "Prompt versions",
"Report keys, appended only") and P2-S1 AC11, while the developer built the slice and before any
of its code was read.

Three seams drive the slice, all offline, each with a scripted `MockProvider` whose answer is raw
JSON, so the model's `suggestion` goes through the same strict parse a real model's would:

- **The CLI**, as P3-S1 and P3-S4 drive it (P3-S4's `Harness`): `review-bot SAMPLE --provider mock
  --format json|markdown (--context-repo DIR)`. Context mode is `--context-repo`; without it, a run
  is diff mode (AC5).
- **A context-mode comment body.** Until P3-S8 wires context mode into `review-bot-pr`, a PR run is
  diff mode, where every suggestion is dropped `not_requested` (AC5). So a context-mode body is
  built as P3-S8's PR mode will build it: `review_diff(..., context=ReviewContext(repo=...,
  is_diff_head=True))`, then `build_shadow_report` with the local shadow command's arguments, then
  `render_shadow_json` and `render_shadow_markdown`. A comment mode run posts each `shadow/v1`
  comment entry's `body` (P2-S5), so that body is the comment body.
- **`python -m review_bot.pr shadow`** (P2-S1's `ShadowRunner`), a diff-mode run (AC5, AC6).

The sample diff's `billing/invoice.py` new-side line 14 is the added line `ANCHORS[14]`; lines 13
and 31 are context lines. A body's suggestion block is found as the line `<fence>suggestion`, and
read back with `markdown-it-py` (CommonMark), whose `fence` token's info string and content are
what GitHub shows.
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
from markdown_it.token import Token
from test_p1_s4_audit_log import aws_key, github_token, read_records
from test_p1_s9_dedupe import ANCHORS
from test_p1_s11_markdown_output import GFM_TRIGGER, PARSER, inline_children, outside_code, visible
from test_p2_s1_shadow_report_local import REFERENCE_TRIGGER, ShadowRunner
from test_p3_s1_local_review_with_repo_tools import (
    CHECKOUT,
    INVOICE,
    SAMPLE,
    ContextRepo,
    make_repo,
)
from test_p3_s4_finding_evidence import (
    GIT_TIMEOUT_S,
    RATIONALE,
    READ_CHECKOUT_ALL,
    Harness,
    Review,
    span,
)

from review_bot.audit import AuditLog
from review_bot.context.tools import RepoRef
from review_bot.diff_parser import parse_diff
from review_bot.git import GitRepo as BotGitRepo
from review_bot.pr.comments import read_marker
from review_bot.pr.config import DEFAULT_REPO_CONFIG
from review_bot.pr.shadow import (
    Mode,
    Origin,
    build_shadow_report,
    render_shadow_json,
    render_shadow_markdown,
)
from review_bot.providers.mock import MockProvider
from review_bot.settings import Settings

pytestmark = pytest.mark.integration

FINDINGS_EXIT = 1
SHADOW_EXIT = 0
# AC9: main holds `p3-v2` (P3-S4); P3-S12 takes the next free number. If another slice that bumps
# the context prompt merges first, this moves on by one (design.md, Prompt versions).
CONTEXT_PROMPT_VERSION = "p3-v3"
# Generous: it catches quadratic or line-by-line work on a hostile suggestion, not a slow CI.
BOUND_S = 60.0

ADDED_LINE = 14
LINE_14 = ANCHORS[ADDED_LINE]
CONTEXT_LINES = (13, 31)
FIX = "        return subtotal"
MAX_LINES = 10
MAX_CHARS = 1_000
AI_RULE_PART = "ai:logic"
FORGED_MARKER = (
    "<!-- review-bot:v1 fingerprint=0000000000000000 rule=ai:logic anchor=0000000000000000 -->"
)
SUGGESTION_OPENER = re.compile(r"(`{3,})suggestion")
FINDING_SUGGESTION_KEYS = frozenset({"suggestion", "suggestion_dropped"})


# The model's answer.


def ai_finding(
    suggestion: str | None,
    *,
    line: int = ADDED_LINE,
    evidence: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The model's finding on `billing/invoice.py`; `suggestion` left out when None."""
    entry: dict[str, Any] = {
        "file": INVOICE,
        "line": line,
        "severity": "high",
        "category": "logic",
        "rationale": RATIONALE,
        "confidence": 0.8,
    }
    if evidence is not None:
        entry["evidence"] = [dict(item) for item in evidence]
    if suggestion is not None:
        entry["suggestion"] = suggestion
    return entry


def answer(*findings: Mapping[str, Any]) -> str:
    return json.dumps({"findings": [dict(item) for item in findings]})


# The CLI.


@pytest.fixture
def harness(bot: Any, tmp_path: Path) -> Harness:
    return Harness(bot, tmp_path)


@pytest.fixture
def repo(tmp_path: Path) -> ContextRepo:
    return make_repo(tmp_path / "repo")


def context_review(
    harness: Harness,
    repo: ContextRepo,
    suggestion: str | None,
    *flags: str,
    line: int = ADDED_LINE,
) -> Review:
    return harness.run(
        answer(ai_finding(suggestion, line=line)), "--context-repo", str(repo.path), *flags
    )


def diff_review(harness: Harness, suggestion: str | None, *flags: str) -> Review:
    return harness.run(answer(ai_finding(suggestion)), *flags)


def kept_entry(review: Review, line: int = ADDED_LINE) -> dict[str, Any]:
    """The one model finding, reported on `line` whatever happened to its suggestion."""
    assert review.exit_code == FINDINGS_EXIT, review.stderr
    entry = review.ai_entry
    assert (entry["file"], entry["line"]) == (INVOICE, line), entry
    return entry


# A context-mode comment body: review_diff in PR mode, then the shadow report.


@dataclass(frozen=True, slots=True)
class ContextShadow:
    document: dict[str, Any]
    markdown: str
    audit_log: Path

    @property
    def entry(self) -> dict[str, Any]:
        (entry,) = [c for c in self.document["comments"] if c["source"] == "ai"]
        return entry

    @property
    def body(self) -> str:
        body: str = self.entry["body"]
        return body

    @property
    def report_finding(self) -> dict[str, Any]:
        (finding,) = [f for f in self.document["report"]["findings"] if f["source"] == "ai"]
        return finding

    @property
    def everything(self) -> str:
        """Every output of the run: the shadow JSON, its Markdown and the audit log."""
        return "\n".join(
            (json.dumps(self.document), self.markdown, self.audit_log.read_text(encoding="utf-8"))
        )


def context_shadow(
    tmp_path: Path,
    repo: ContextRepo,
    raw: str,
    tool_calls: Sequence[tuple[str, Mapping[str, object]]] = (),
) -> ContextShadow:
    from review_bot.review import ReviewContext, review_diff  # noqa: PLC0415 - the slice's seam

    audit_log = tmp_path / f"shadow-{len(list(tmp_path.glob('shadow-*.jsonl')))}.jsonl"
    settings = Settings(provider="mock", audit_log=audit_log)
    ref = RepoRef(git=BotGitRepo(repo.path, timeout_s=GIT_TIMEOUT_S), commit=repo.commit)
    provider = MockProvider(
        raw_output=raw, tool_calls=[(name, dict(arguments)) for name, arguments in tool_calls]
    )
    audit = AuditLog(audit_log, clock=lambda: datetime.now(UTC))
    started_at = datetime.now(UTC)
    result = asyncio.run(
        review_diff(
            SAMPLE.read_text(encoding="utf-8"),
            settings,
            provider,
            audit,
            name="sample_diff.txt",
            context=ReviewContext(repo=ref, is_diff_head=True),
        )
    )
    shadow = build_shadow_report(
        result,
        mode=Mode.SHADOW,
        existing=(),
        settings=settings,
        config=DEFAULT_REPO_CONFIG,
        run_id=audit.run_id,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        origin=Origin.LOCAL,
        pr=None,
    )
    document = json.loads(render_shadow_json(shadow))
    assert document["schema"] == "shadow/v1"
    assert document["outcome"] == "reviewed", document["summary_body"]
    return ContextShadow(document, render_shadow_markdown(shadow), audit_log)


def shadow_with(tmp_path: Path, repo: ContextRepo, suggestion: str | None) -> ContextShadow:
    return context_shadow(tmp_path, repo, answer(ai_finding(suggestion)))


# Reading a body's suggestion block.


@dataclass(frozen=True, slots=True)
class Block:
    opener_at: int
    closer_at: int
    fence: str
    text: str


def suggestion_block(body: str) -> Block:
    """The first `<fence>suggestion` line, its content, and the first line closing it."""
    lines = body.split("\n")
    opener_at = next(n for n, line in enumerate(lines) if SUGGESTION_OPENER.fullmatch(line))
    opener = SUGGESTION_OPENER.fullmatch(lines[opener_at])
    assert opener is not None
    fence = opener.group(1)
    closer_at = next(n for n in range(opener_at + 1, len(lines)) if lines[n] == fence)
    return Block(opener_at, closer_at, fence, "\n".join(lines[opener_at + 1 : closer_at]))


def fences(markdown: str) -> list[Token]:
    return [token for token in PARSER.parse(markdown) if token.type == "fence"]


def assert_one_suggestion_fence(markdown: str, suggestion: str) -> None:
    """As CommonMark reads it: one fence, info `suggestion`, holding exactly the suggestion."""
    (fence,) = fences(markdown)
    assert fence.info == "suggestion", fence.info
    assert fence.content == (suggestion + "\n" if suggestion else ""), fence.content


def has_suggestion_block(markdown: str) -> bool:
    return any(SUGGESTION_OPENER.fullmatch(line.strip()) for line in markdown.split("\n")) or any(
        fence.info.strip() == "suggestion" for fence in fences(markdown)
    )


def line_index(lines: list[str], text: str) -> int:
    """The one line a reader sees holding `text`."""
    (found,) = [n for n, line in enumerate(lines) if text in visible(inline_children_of(line))]
    return found


def inline_children_of(line: str) -> list[Token]:
    return list(inline_children(PARSER.parse(line)))


def assert_inert_outside_fences(markdown: str, *, html_blocks: int | None) -> None:
    """P2-S1 AC11's check on all text outside fenced code: nothing GitHub links or hides."""
    tokens = PARSER.parse(markdown)
    children = list(inline_children(tokens))
    assert not any(c.type == "link_open" for c in children), "a link formed"
    assert not any(c.type == "html_inline" for c in children), "inline HTML formed"
    if html_blocks is not None:
        html = [t.content for t in tokens if t.type == "html_block"]
        assert len(html) == html_blocks, html
    text = outside_code(tokens)
    assert not GFM_TRIGGER.findall(text), f"{GFM_TRIGGER.findall(text)} outside code"
    assert not REFERENCE_TRIGGER.findall(text), f"{REFERENCE_TRIGGER.findall(text)} outside code"


# AC1: a kept suggestion's block, after the rationale and any Context: line, before the reaction.


def test_p3_s12_ac1_cli_context_mode_keeps_the_suggestion(harness, repo):
    review = context_review(harness, repo, FIX)

    entry = kept_entry(review)
    assert entry["suggestion"] == FIX
    assert entry["suggestion_dropped"] is None


def test_p3_s12_ac1_body_holds_a_suggestion_block_of_exactly_the_text(tmp_path, repo):
    shadow = shadow_with(tmp_path, repo, FIX)

    body = shadow.body
    block = suggestion_block(body)
    assert block.fence == "```"
    assert block.text == FIX
    assert_one_suggestion_fence(body, FIX)


def test_p3_s12_ac1_block_sits_after_the_rationale_and_before_the_reaction_request(tmp_path, repo):
    shadow = shadow_with(tmp_path, repo, FIX)

    lines = shadow.body.split("\n")
    block = suggestion_block(shadow.body)
    rationale_at = line_index(lines, RATIONALE)
    reaction_at = line_index(lines, "thumbs-up")
    assert rationale_at < block.opener_at < block.closer_at < reaction_at < len(lines) - 1
    entry = shadow.entry
    assert lines[-1] == (
        f"<!-- review-bot:v1 fingerprint={entry['fingerprint']} rule={AI_RULE_PART} "
        f"anchor={entry['anchor']} -->"
    )


def test_p3_s12_ac1_block_comes_after_the_context_line(tmp_path, repo):
    raw = answer(ai_finding(FIX, evidence=[span(CHECKOUT, 12)]))

    shadow = context_shadow(tmp_path, repo, raw, [READ_CHECKOUT_ALL])

    lines = shadow.body.split("\n")
    (context_at,) = [n for n, line in enumerate(lines) if line.startswith("Context: ")]
    block = suggestion_block(shadow.body)
    rationale_at = line_index(lines, RATIONALE)
    reaction_at = line_index(lines, "thumbs-up")
    assert rationale_at < context_at < block.opener_at < block.closer_at < reaction_at
    assert lines[-1].startswith("<!-- review-bot:v1 "), lines[-1]


def test_p3_s12_ac1_marker_is_the_last_line_and_reads_back(tmp_path, repo):
    shadow = shadow_with(tmp_path, repo, FIX)

    marker = read_marker(shadow.body)
    assert marker is not None, shadow.body
    assert (marker.fingerprint, marker.rule_part, marker.anchor) == (
        shadow.entry["fingerprint"],
        AI_RULE_PART,
        shadow.entry["anchor"],
    )


def test_p3_s12_ac1_body_without_a_suggestion_has_no_block(tmp_path, repo):
    shadow = shadow_with(tmp_path, repo, None)

    assert not has_suggestion_block(shadow.body), shadow.body
    assert fences(shadow.body) == []


@pytest.mark.parametrize(
    "suggestion",
    [
        "        return subtotal  # tax added at checkout",
        "        if self.tax_rate:\n            return subtotal\n        return subtotal",
        "\treturn subtotal",
        "        return 'naïve → total'",
    ],
    ids=["one_line", "three_lines", "tab", "non_ascii"],
)
def test_p3_s12_ac1_block_holds_each_kept_suggestion_exactly(tmp_path, repo, suggestion):
    shadow = shadow_with(tmp_path, repo, suggestion)

    assert shadow.entry["suggestion"] == suggestion
    assert suggestion_block(shadow.body).text == suggestion
    assert_one_suggestion_fence(shadow.body, suggestion)


# AC2: the fence is always longer than the longest backtick run inside.


@pytest.mark.parametrize(
    ("suggestion", "fence"),
    [
        ("        return f'{subtotal}'", "```"),
        ("        x = `a`", "```"),
        ("        x = ``a``", "```"),
        ("        x = ```a```", "````"),
        ("```", "````"),
        ("        x = 1\n```\n        y = 2", "````"),
        ("        x = ````a`` `", "`````"),
        ("        x = '" + "`" * 7 + "'", "`" * 8),
    ],
    ids=["none", "run_1", "run_2", "run_3", "only_3", "closing_line_3", "run_4", "run_7"],
)
def test_p3_s12_ac2_fence_is_longer_than_the_longest_backtick_run(
    tmp_path, repo, suggestion, fence
):
    shadow = shadow_with(tmp_path, repo, suggestion)

    block = suggestion_block(shadow.body)
    assert block.fence == fence
    assert block.text == suggestion
    assert_one_suggestion_fence(shadow.body, suggestion)


def test_p3_s12_ac2_three_backtick_run_gets_a_four_backtick_fence(tmp_path, repo):
    suggestion = "        doc = '```'"

    shadow = shadow_with(tmp_path, repo, suggestion)

    assert suggestion_block(shadow.body).fence == "````"
    assert_one_suggestion_fence(shadow.body, suggestion)
    assert shadow.body.split("\n")[-1].startswith("<!-- review-bot:v1 ")


def test_p3_s12_ac2_tilde_fence_inside_cannot_close_the_block(tmp_path, repo):
    suggestion = "~~~\n        x = 1\n~~~~"

    shadow = shadow_with(tmp_path, repo, suggestion)

    assert_one_suggestion_fence(shadow.body, suggestion)


# AC3: a suggestion dropped for a reason keeps its finding, with no block.


# AC3: bidi controls and the Unicode line and paragraph separators (Trojan Source, spec Notes).
BIDI_AND_SEPARATORS = (
    *map(chr, range(0x202A, 0x202F)),
    *map(chr, range(0x2066, 0x206A)),
    "\u200e",
    "\u200f",
    "\u2028",
    "\u2029",
)

SECRETS = {"github_token": github_token(3), "aws_key": aws_key(5)}

DROPPED = {
    "too_many_lines": ("\n".join(f"        x{n} = {n}" for n in range(MAX_LINES + 1)), "too_long"),
    "too_many_chars": ("x" * (MAX_CHARS + 1), "too_long"),
    "ten_long_lines": ("\n".join("y" * 100 for _ in range(MAX_LINES)), "too_long"),
    "same_line": (LINE_14, "unchanged"),
    "same_line_trailing_spaces": (LINE_14 + "   ", "unchanged"),
    "same_line_trailing_tab": (LINE_14 + "\t", "unchanged"),
    "nul": ("        return subtotal\x00", "control_characters"),
    "escape": ("        return \x1b[31msubtotal", "control_characters"),
    "carriage_return": ("        return subtotal\r", "control_characters"),
    "bell": ("\x07        return subtotal", "control_characters"),
    "delete": ("        return subtotal\x7f", "control_characters"),
    "c1_csi": ("        return \x9bsubtotal", "control_characters"),
    "crlf": ("        x = 1\r\n        return subtotal", "control_characters"),
    "ten_lines_and_a_trailing_line_feed": ("x\n" * MAX_LINES, "too_long"),
    **{
        f"u{ord(char):04x}": (f"        return subtotal  # {char}x", "control_characters")
        for char in BIDI_AND_SEPARATORS
    },
    "github_token": (f"        token = '{SECRETS['github_token']}'", "possible_secret"),
    "aws_key": (f"        key = '{SECRETS['aws_key']}'", "possible_secret"),
}


@pytest.mark.parametrize(("suggestion", "reason"), DROPPED.values(), ids=DROPPED.keys())
def test_p3_s12_ac3_dropped_suggestion_keeps_its_finding_with_the_reason(
    harness, repo, suggestion, reason
):
    review = context_review(harness, repo, suggestion)

    entry = kept_entry(review)
    assert entry["suggestion"] is None
    assert entry["suggestion_dropped"] == reason


@pytest.mark.parametrize("line", CONTEXT_LINES)
def test_p3_s12_ac3_suggestion_on_a_context_line_is_dropped_not_added_line(harness, repo, line):
    review = context_review(harness, repo, "        total = 0", line=line)

    entry = kept_entry(review, line)
    assert entry["suggestion"] is None
    assert entry["suggestion_dropped"] == "not_added_line"


@pytest.mark.parametrize(("suggestion", "reason"), DROPPED.values(), ids=DROPPED.keys())
def test_p3_s12_ac3_dropped_suggestion_leaves_no_block_in_the_body(
    tmp_path, repo, suggestion, reason
):
    shadow = shadow_with(tmp_path, repo, suggestion)

    assert shadow.entry["suggestion"] is None
    assert shadow.report_finding["suggestion_dropped"] == reason
    assert not has_suggestion_block(shadow.body), shadow.body
    assert not has_suggestion_block(shadow.markdown)


def test_p3_s12_ac3_context_line_suggestion_leaves_no_block_in_the_body(tmp_path, repo):
    shadow = context_shadow(tmp_path, repo, answer(ai_finding("        total = 0", line=31)))

    assert shadow.entry["line"] == 31
    assert shadow.report_finding["suggestion_dropped"] == "not_added_line"
    assert not has_suggestion_block(shadow.body), shadow.body


@pytest.mark.parametrize(
    "suggestion",
    [
        "\n".join(f"        x{n} = {n}" for n in range(MAX_LINES)),
        "x" * MAX_CHARS,
        "    " + LINE_14,
        LINE_14.strip(),
        "x\n" * (MAX_LINES - 1),
    ],
    ids=[
        "ten_lines",
        "1000_chars",
        "more_leading_space",
        "less_leading_space",
        "nine_lines_and_a_trailing_line_feed",
    ],
)
def test_p3_s12_ac3_suggestion_at_the_limits_or_differing_in_leading_space_is_kept(
    harness, repo, suggestion
):
    review = context_review(harness, repo, suggestion)

    entry = kept_entry(review)
    assert entry["suggestion"] == suggestion
    assert entry["suggestion_dropped"] is None


@pytest.mark.parametrize("name", SECRETS)
@pytest.mark.parametrize("output", ["json", "markdown", "text"])
def test_p3_s12_ac3_secret_in_a_suggestion_is_in_no_cli_output(harness, repo, name, output):
    suggestion, _ = DROPPED[name]

    review = context_review(harness, repo, suggestion, "--format", output)

    assert review.exit_code == FINDINGS_EXIT, review.stderr
    for text in (review.stdout, review.stderr, review.log_text):
        assert SECRETS[name] not in text


@pytest.mark.parametrize("name", SECRETS)
def test_p3_s12_ac3_secret_in_a_suggestion_is_in_no_shadow_output(tmp_path, repo, name):
    suggestion, _ = DROPPED[name]

    shadow = shadow_with(tmp_path, repo, suggestion)

    assert shadow.report_finding["suggestion_dropped"] == "possible_secret"
    assert SECRETS[name] not in shadow.everything


# AC3: when several reasons apply, the first in AC5's then AC3's order is the reason.


ORDERED = {
    "not_added_line_before_too_long": (13, "x\n" * (MAX_LINES + 1), "not_added_line"),
    "not_added_line_before_control": (13, "        total = 0\x00", "not_added_line"),
    "not_added_line_before_secret": (31, f"        k = '{SECRETS['aws_key']}'", "not_added_line"),
    "too_long_before_unchanged": (ADDED_LINE, LINE_14 + " " * MAX_CHARS, "too_long"),
    "too_long_before_control": (ADDED_LINE, "\x00" * (MAX_CHARS + 1), "too_long"),
    "too_long_before_secret": (
        ADDED_LINE,
        "x" * MAX_CHARS + SECRETS["github_token"],
        "too_long",
    ),
    "control_before_secret": (
        ADDED_LINE,
        f"        k = '{SECRETS['aws_key']}'\x1b",
        "control_characters",
    ),
}


@pytest.mark.parametrize(("line", "suggestion", "reason"), ORDERED.values(), ids=ORDERED.keys())
def test_p3_s12_ac3_first_reason_in_order_wins(harness, repo, line, suggestion, reason):
    review = context_review(harness, repo, suggestion, line=line)

    entry = kept_entry(review, line)
    assert entry["suggestion"] is None
    assert entry["suggestion_dropped"] == reason
    for secret in SECRETS.values():
        assert secret not in review.stdout + review.stderr + review.log_text


@pytest.mark.parametrize(
    ("line", "suggestion"),
    [
        (13, FIX),
        (ADDED_LINE, "x" * (MAX_CHARS + 1)),
        (ADDED_LINE, LINE_14),
        (ADDED_LINE, "        return subtotal\x00"),
        (ADDED_LINE, f"        k = '{SECRETS['aws_key']}'"),
    ],
    ids=["context_line", "too_long", "unchanged", "control", "secret"],
)
def test_p3_s12_ac3_not_requested_comes_first_in_diff_mode(harness, line, suggestion):
    review = harness.run(answer(ai_finding(suggestion, line=line)))

    entry = kept_entry(review, line)
    assert entry["suggestion"] is None
    assert entry["suggestion_dropped"] == "not_requested"


# AC3: "trailing whitespace" is only spaces, tabs and line feeds, so the anchored line plus a
# control or separator character is `control_characters`, not `unchanged`.


TRAILING = {
    "cr": (LINE_14 + "\r", "control_characters"),
    "crlf": (LINE_14 + "\r\n", "control_characters"),
    "line_separator": (LINE_14 + "\u2028", "control_characters"),
    "paragraph_separator": (LINE_14 + "\u2029", "control_characters"),
    "space_and_tab": (LINE_14 + " \t", "unchanged"),
}


@pytest.mark.parametrize(("suggestion", "reason"), TRAILING.values(), ids=TRAILING.keys())
def test_p3_s12_ac3_only_spaces_tabs_and_line_feeds_are_trailing_whitespace(
    harness, repo, suggestion, reason
):
    review = context_review(harness, repo, suggestion)

    entry = kept_entry(review)
    assert entry["suggestion"] is None
    assert entry["suggestion_dropped"] == reason
    assert checked_record(review, ADDED_LINE)["suggestion_dropped"] == reason


CR_FILE = "billing/crlf.py"
CR_LINE = "    total = round(subtotal, 2)\r"
# One added line whose text ends in a carriage return. The diff ends that line in CRLF, which the
# parser strips as a line ending, so a second CR stays in the line's text.
CR_DIFF = (
    f"diff --git a/{CR_FILE} b/{CR_FILE}\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    f"+++ b/{CR_FILE}\n"
    "@@ -0,0 +1 @@\n"
    f"+{CR_LINE}\r\n"
)


def test_p3_s12_ac3_suggestion_equal_to_a_line_ending_in_cr_is_control_characters(
    bot, repo, tmp_path
):
    """A control character is never `unchanged`, even when the anchored line holds it too."""
    (added,) = parse_diff(CR_DIFF).files[0].hunks[0].lines
    assert added.text == CR_LINE, "precondition: the anchored line ends in CR"
    diff = tmp_path / "crlf.diff"
    diff.write_bytes(CR_DIFF.encode("utf-8"))
    audit_log = tmp_path / "crlf-audit.jsonl"
    finding = ai_finding(CR_LINE, line=1) | {"file": CR_FILE}
    provider = MockProvider(raw_output=answer(finding))

    run = bot.run(
        str(diff),
        "--provider",
        "mock",
        "--audit-log",
        str(audit_log),
        "--format",
        "json",
        "--context-repo",
        str(repo.path),
        provider=provider,
    )

    assert run.exit_code == FINDINGS_EXIT, run.stderr
    (entry,) = [f for f in json.loads(run.stdout)["findings"] if f["source"] == "ai"]
    assert (entry["file"], entry["line"]) == (CR_FILE, 1), entry
    assert entry["suggestion"] is None
    assert entry["suggestion_dropped"] == "control_characters"
    (record,) = [
        r["data"]
        for r in read_records(audit_log)
        if (r["stage"], r["event"]) == ("verify", "checked") and r["data"].get("file") == CR_FILE
    ]
    assert record["suggestion_dropped"] == "control_characters", record


# AC5: only kept findings have their suggestion checked.


OFF_DIFF_LINE = 20


@pytest.mark.parametrize("context_mode", [True, False], ids=["context", "diff"])
def test_p3_s12_ac5_dropped_finding_has_null_suggestion_dropped(harness, repo, context_mode):
    raw = answer(ai_finding(FIX, line=OFF_DIFF_LINE))
    flags = ("--context-repo", str(repo.path)) if context_mode else ()

    review = harness.run(raw, *flags)

    assert review.ai_entries == [], review.ai_entries
    record = checked_record(review, OFF_DIFF_LINE)
    assert record["outcome"] != "kept", record
    assert record["suggestion_dropped"] is None, record


# AC10: the verify record carries the reason, never the text.


def checked_record(review: Review, line: int) -> dict[str, Any]:
    """The model finding's `verify` `checked` audit record."""
    (record,) = [
        data
        for data in review.records("verify", "checked")
        if data.get("file") == INVOICE and data.get("line") == line
    ]
    return record


def assert_no_suggestion_text(review: Review, suggestion: str) -> None:
    for record in read_records(review.audit_log):
        assert "suggestion" not in record["data"] or record["data"]["suggestion"] is None, record
    lines = [line for line in suggestion.split("\n") if len(line.strip()) >= 8]
    for line in lines:
        assert line not in review.log_text, line


@pytest.mark.parametrize(("suggestion", "reason"), DROPPED.values(), ids=DROPPED.keys())
def test_p3_s12_ac10_checked_record_has_the_ac3_reason_and_no_text(
    harness, repo, suggestion, reason
):
    review = context_review(harness, repo, f"{suggestion}")

    record = checked_record(review, ADDED_LINE)
    assert record["suggestion_dropped"] == reason, record
    assert_no_suggestion_text(review, suggestion)


@pytest.mark.parametrize("line", CONTEXT_LINES)
def test_p3_s12_ac10_checked_record_has_not_added_line(harness, repo, line):
    suggestion = "        total = SENTINEL_CONTEXT_4e1a"

    review = context_review(harness, repo, suggestion, line=line)

    assert checked_record(review, line)["suggestion_dropped"] == "not_added_line"
    assert_no_suggestion_text(review, suggestion)


def test_p3_s12_ac10_checked_record_has_not_requested_in_diff_mode(harness):
    suggestion = "        return SENTINEL_DIFF_MODE_93b2"

    review = diff_review(harness, suggestion)

    assert checked_record(review, ADDED_LINE)["suggestion_dropped"] == "not_requested"
    assert_no_suggestion_text(review, suggestion)


@pytest.mark.parametrize("suggestion", [FIX + "  # SENTINEL_KEPT_0c7d", ""], ids=["text", "empty"])
def test_p3_s12_ac10_kept_suggestion_records_null_and_no_text(harness, repo, suggestion):
    review = context_review(harness, repo, suggestion)

    assert kept_entry(review)["suggestion"] == suggestion
    record = checked_record(review, ADDED_LINE)
    assert "suggestion_dropped" in record, record
    assert record["suggestion_dropped"] is None, record
    assert_no_suggestion_text(review, suggestion)


@pytest.mark.parametrize("context_mode", [True, False], ids=["context", "diff"])
def test_p3_s12_ac10_no_suggestion_records_null(harness, repo, context_mode):
    review = context_review(harness, repo, None) if context_mode else diff_review(harness, None)

    record = checked_record(review, ADDED_LINE)
    assert "suggestion_dropped" in record, record
    assert record["suggestion_dropped"] is None, record


# AC4: an empty suggestion deletes the line.


def test_p3_s12_ac4_empty_suggestion_is_kept(harness, repo):
    review = context_review(harness, repo, "")

    entry = kept_entry(review)
    assert entry["suggestion"] == ""
    assert entry["suggestion_dropped"] is None


def test_p3_s12_ac4_empty_suggestion_renders_an_empty_block(tmp_path, repo):
    shadow = shadow_with(tmp_path, repo, "")

    block = suggestion_block(shadow.body)
    assert block.closer_at == block.opener_at + 1, shadow.body
    assert block.fence == "```"
    assert_one_suggestion_fence(shadow.body, "")
    assert shadow.entry["suggestion"] == ""


# AC5: a diff-mode suggestion is dropped `not_requested`.


@pytest.mark.parametrize("suggestion", [FIX, ""])
def test_p3_s12_ac5_diff_mode_suggestion_is_dropped_not_requested(harness, suggestion):
    review = diff_review(harness, suggestion)

    entry = kept_entry(review)
    assert entry["suggestion"] is None
    assert entry["suggestion_dropped"] == "not_requested"


def test_p3_s12_ac5_diff_mode_secret_suggestion_is_in_no_output(harness):
    secret = github_token(9)

    review = diff_review(harness, f"        token = '{secret}'")

    kept_entry(review)
    for text in (review.stdout, review.stderr, review.log_text):
        assert secret not in text


def test_p3_s12_ac5_pr_shadow_run_drops_the_suggestion_and_shows_no_block(capsys, tmp_path):
    shadow = ShadowRunner(capsys, tmp_path)

    run = shadow.run(SAMPLE, provider=MockProvider(raw_output=answer(ai_finding(FIX))))

    assert run.exit_code == SHADOW_EXIT, run.stderr
    document = run.shadow
    (entry,) = [c for c in document["comments"] if c["line"] == ADDED_LINE]
    assert entry["suggestion"] is None
    assert not has_suggestion_block(entry["body"]), entry["body"]
    (finding,) = [f for f in document["report"]["findings"] if f["line"] == ADDED_LINE]
    assert finding["suggestion_dropped"] == "not_requested"
    assert not has_suggestion_block((run.out / "shadow.md").read_text(encoding="utf-8"))


# AC6: `suggestion` and `suggestion_dropped` in report/v1, `suggestion` in shadow/v1 entries.


def test_p3_s12_ac6_every_report_finding_has_both_keys(harness, repo):
    review = context_review(harness, repo, FIX)

    findings = review.report["findings"]
    assert len(findings) == 3, findings
    for finding in findings:
        assert set(finding) >= FINDING_SUGGESTION_KEYS, finding


def test_p3_s12_ac6_rule_findings_have_null_suggestion_and_reason(harness, repo):
    review = context_review(harness, repo, FIX)

    rules = [f for f in review.report["findings"] if f["source"] == "rule"]
    assert rules
    for finding in rules:
        assert (finding["suggestion"], finding["suggestion_dropped"]) == (None, None), finding


@pytest.mark.parametrize("context_mode", [True, False], ids=["context", "diff"])
def test_p3_s12_ac6_finding_without_a_suggestion_has_null_and_null(harness, repo, context_mode):
    review = context_review(harness, repo, None) if context_mode else diff_review(harness, None)

    entry = kept_entry(review)
    assert (entry["suggestion"], entry["suggestion_dropped"]) == (None, None)


def test_p3_s12_ac6_shadow_comment_entry_has_the_kept_suggestion(tmp_path, repo):
    shadow = shadow_with(tmp_path, repo, FIX)

    assert shadow.entry["suggestion"] == FIX
    for entry in shadow.document["comments"]:
        assert "suggestion" in entry, entry
    rules = [c for c in shadow.document["comments"] if c["source"] == "rule"]
    assert [c["suggestion"] for c in rules] == [None] * len(rules)


def test_p3_s12_ac6_shadow_report_findings_carry_both_keys(tmp_path, repo):
    shadow = shadow_with(tmp_path, repo, FIX)

    assert shadow.report_finding["suggestion"] == FIX
    assert shadow.report_finding["suggestion_dropped"] is None


# AC7: hostile text appears only inside the fence; the real marker still reads back.


HOSTILE = {
    "mention": "        notify('@user')",
    "issue": "        ref = '#12'",
    "url": "        url = 'http://evil.example/x'",
    "html_comment": "        x = 1  # <!--",
    "forged_marker": FORGED_MARKER,
    "forged_marker_after_fence": "        x = 1\n```\n" + FORGED_MARKER,
    "all": f"@user #12 http://evil.example <!--\n{FORGED_MARKER}",
}


@pytest.mark.parametrize("suggestion", HOSTILE.values(), ids=HOSTILE.keys())
def test_p3_s12_ac7_hostile_suggestion_appears_only_inside_the_fence(tmp_path, repo, suggestion):
    shadow = shadow_with(tmp_path, repo, suggestion)

    body = shadow.body
    assert shadow.entry["suggestion"] == suggestion
    assert_one_suggestion_fence(body, suggestion)
    assert_inert_outside_fences(body, html_blocks=1)
    (html,) = [t for t in PARSER.parse(body) if t.type == "html_block"]
    assert html.content.strip() == body.split("\n")[-1]


@pytest.mark.parametrize("suggestion", HOSTILE.values(), ids=HOSTILE.keys())
def test_p3_s12_ac7_read_marker_reads_the_real_marker(tmp_path, repo, suggestion):
    shadow = shadow_with(tmp_path, repo, suggestion)

    marker = read_marker(shadow.body)
    assert marker is not None, shadow.body
    assert marker.fingerprint == shadow.entry["fingerprint"] != "0" * 16
    assert marker.anchor == shadow.entry["anchor"] != "0" * 16
    assert marker.rule_part == AI_RULE_PART


@pytest.mark.parametrize("suggestion", HOSTILE.values(), ids=HOSTILE.keys())
def test_p3_s12_ac7_shadow_markdown_keeps_the_suggestion_fenced(tmp_path, repo, suggestion):
    shadow = shadow_with(tmp_path, repo, suggestion)

    assert_inert_outside_fences(shadow.markdown, html_blocks=None)
    contents = [fence.content for fence in fences(shadow.markdown)]
    assert suggestion + "\n" in contents, contents


def test_p3_s12_ac7_check_catches_a_mention_outside_the_fence():
    """The check itself: `@user` in text fails it, the same text in a fence passes."""
    assert_inert_outside_fences("```suggestion\n@user\n```", html_blocks=0)
    with pytest.raises(AssertionError):
        assert_inert_outside_fences("Hi @user\n\n```suggestion\nx\n```", html_blocks=0)


# AC8: `review-bot --format markdown` shows a plain fenced code block.


MARKDOWN = ("--format", "markdown")


def test_p3_s12_ac8_markdown_shows_the_suggestion_as_a_plain_fence(harness, repo):
    review = context_review(harness, repo, FIX, *MARKDOWN)

    assert review.exit_code == FINDINGS_EXIT, review.stderr
    (fence,) = fences(review.stdout)
    assert fence.info == "", fence.info
    assert fence.content == FIX + "\n"
    assert not has_suggestion_block(review.stdout), review.stdout


def test_p3_s12_ac8_markdown_empty_suggestion_is_an_empty_plain_fence(harness, repo):
    review = context_review(harness, repo, "", *MARKDOWN)

    assert review.exit_code == FINDINGS_EXIT, review.stderr
    (fence,) = fences(review.stdout)
    assert fence.info == "", fence.info
    assert fence.content == ""


@pytest.mark.parametrize("suggestion", HOSTILE.values(), ids=HOSTILE.keys())
def test_p3_s12_ac8_markdown_hostile_suggestion_stays_in_its_fence(harness, repo, suggestion):
    review = context_review(harness, repo, suggestion, *MARKDOWN)

    assert review.exit_code == FINDINGS_EXIT, review.stderr
    (fence,) = fences(review.stdout)
    assert fence.content == suggestion + "\n"
    assert_inert_outside_fences(review.stdout, html_blocks=0)


@pytest.mark.parametrize("suggestion", ["```", "        x = ````a`` `"])
def test_p3_s12_ac8_markdown_fence_is_longer_than_the_backticks_inside(harness, repo, suggestion):
    review = context_review(harness, repo, suggestion, *MARKDOWN)

    (fence,) = fences(review.stdout)
    assert fence.content == suggestion + "\n"
    longest = max(len(run) for run in re.findall("`+", suggestion))
    assert len(fence.markup) > longest, fence.markup


def test_p3_s12_ac8_markdown_has_no_fence_for_a_dropped_suggestion(harness, repo):
    review = context_review(harness, repo, LINE_14, *MARKDOWN)

    assert review.exit_code == FINDINGS_EXIT, review.stderr
    assert fences(review.stdout) == []


# AC9: the context prompt describes `suggestion`, under the next version.


def system_prompt(review: Review) -> str:
    (request,) = review.provider.requests
    prompt: str = request.system_prompt
    return prompt


def test_p3_s12_ac9_context_prompt_labels_suggestion(harness, repo):
    review = context_review(harness, repo, None)

    # The system prompt alone: the user prompt holds the untrusted diff.
    assert re.search(r"\bsuggestion\b\W{0,2}\s*:", system_prompt(review)), "not labelled"


def test_p3_s12_ac9_context_prompt_says_optional_and_anchored_added_line_only(harness, repo):
    review = context_review(harness, repo, None)

    paragraphs = re.split(r"\n\s*\n", system_prompt(review))
    described = [p for p in paragraphs if re.search(r"\bsuggestion\b", p)]
    assert described, "the prompt doesn't describe `suggestion`"
    text = " ".join(described).lower()
    # Loose on wording: optional, only for an added line, and a replacement of that line.
    assert re.search(r"optional|\bmay\b|\bif any\b|only (?:when|if)", text), described
    assert re.search(r"\badded\b", text), described
    assert re.search(r"replac|instead of", text), described


def test_p3_s12_ac9_context_prompt_version_is_the_next_free_number(harness, repo):
    review = context_review(harness, repo, None)

    assert review.report["prompt_version"] == CONTEXT_PROMPT_VERSION
    (start,) = review.records("run", "start")
    assert start["prompt_version"] == CONTEXT_PROMPT_VERSION


def test_p3_s12_ac9_diff_mode_prompt_does_not_mention_suggestion(harness):
    review = diff_review(harness, None)

    assert "suggestion" not in system_prompt(review).lower()


# Limits: a suggestion is model text, checked in linear time and fenced so it can't close.


@pytest.mark.parametrize(
    "suggestion",
    [
        "x" * 200_000,
        "\n" * 100_000,
        "`" * 200_000,
        "\x00" * 200_000,
        ("ab" * 50_000 + "\n") * 2,
        "x" * 199_000 + github_token(1),
    ],
    ids=["long_line", "many_lines", "backticks", "nuls", "two_long_lines", "secret_at_the_end"],
)
def test_p3_s12_limits_hostile_size_is_dropped_quickly(harness, repo, suggestion):
    started = time.monotonic()
    review = context_review(harness, repo, suggestion)
    elapsed = time.monotonic() - started

    entry = kept_entry(review)
    assert entry["suggestion"] is None
    assert entry["suggestion_dropped"] in ("too_long", "control_characters", "possible_secret")
    assert elapsed < BOUND_S, elapsed
    assert github_token(1) not in review.stdout + review.log_text


def test_p3_s12_limits_longest_backtick_run_kept_is_fenced_quickly(tmp_path, repo):
    suggestion = "`" * MAX_CHARS

    started = time.monotonic()
    shadow = shadow_with(tmp_path, repo, suggestion)
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, elapsed
    block = suggestion_block(shadow.body)
    assert len(block.fence) == MAX_CHARS + 1
    assert_one_suggestion_fence(shadow.body, suggestion)


def test_p3_s12_limits_audit_log_holds_no_suggestion_text(harness, repo):
    """The audit log holds decisions, never model text (ADR 0005)."""
    marker = "SUGGESTION-UNIQUE-7f2c91"

    review = context_review(harness, repo, f"        return subtotal  # {marker}")

    kept_entry(review)
    assert marker not in review.log_text
    assert all(marker not in json.dumps(r) for r in read_records(review.audit_log))
