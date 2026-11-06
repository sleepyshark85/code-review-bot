r"""P1-S11: get the review as Markdown.

Written from the spec's criteria and Notes, and design.md's Settings, Audit log ("Paths in
`audit/v1`") and Compatibility sections, before the developer's code was read. Runs go through
`main` with an injected `MockProvider`; the stdout failure cases run a real process, as P0-S3
AC4's and P1-S10 AC8's do.

Stdout is read by `markdown-it-py` as CommonMark plus GitHub's table and autolink (linkify)
extensions, because GitHub renders both, and `|` and bare URLs are among AC4's payloads. "Inert"
(AC4, AC5) is read as: the same block structure and the same count of every inline structure
(HTML, image, link, emphasis) as a control run with a plain value; the same number of finding list
items; each whitespace-separated word of the hostile value appearing, in order, in its item's
visible text, so a line break may show as a space or as an escape; and the spec's code-span rule,
checked directly, not only through the parser: no `@`, `://` (any scheme) or `www.` anywhere in
text outside code, and each word of the value holding one inside a code span. The parser alone
isn't enough, because linkify doesn't link an escaped URL such as `http\:\/\/evil`, and GitHub does.

A finding's list item is one whose visible text holds a code span ending in `:<line>` (AC1).

Over `Assignment/sample_diff.txt`, the rule findings are `swallowed-exception` (medium) on line 35
and `open-without-encoding` (low) on line 39; line 14 is an added line in the first hunk.
"""

import re
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from diff_support import MINIMAL_DIFF, SAMPLE_RULE_LINES, section
from markdown_it import MarkdownIt
from markdown_it.token import Token
from repo_paths import REPO_ROOT
from stream_support import CLOSED, DEV_FULL, READER_GONE, needs_dev_full, run_process
from test_p0_s3_stream_failures import (
    assert_no_traceback,
    assert_only_the_error_line,
    bot_command,
    injected_command,
)
from test_p1_s4_audit_log import LONE_SURROGATE, audit_error, octal, quoted_section
from test_p1_s9_dedupe import SWALLOWED_HANDLER
from test_p1_s10_json_output import (
    FAILURE_CASES,
    GOLDEN_CASES,
    INVOICE,
    OPEN_RULE,
    SWALLOWED,
    ai_finding,
    failures,
    golden_text,
    rationale_of,
    sample_plus,
    without_run_id,
)

from review_bot import audit
from review_bot.findings import Finding

pytestmark = pytest.mark.integration

MARKDOWN = ("--format", "markdown")
TEXT = ("--format", "text")
FORMAT_VARIABLE = "REVIEW_BOT_FORMAT"
AUDIT_FLAG = "--audit-log"
ERROR = "review-bot: error: "
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "p1_s11"
# Generous: it catches a hang or quadratic work, not a slow CI machine.
BOUND_S = 60.0

PARSER = MarkdownIt("commonmark", {"linkify": True}).enable(["table", "linkify"])
# Inline structures whose count a hostile value mustn't change.
INLINE_STRUCTURES = ("html_inline", "image", "link_open", "em_open", "strong_open", "s_open")
# What GitHub can turn into a mention or an autolink outside code (spec Interfaces).
GFM_TRIGGER = re.compile(r"@|://|www\.", re.IGNORECASE)
LINE_SUFFIX = re.compile(r":(\d+)$")


# Reading the document.


def check_stdout(stdout: str) -> None:
    """Stdout can always be encoded, and holds no raw control or separator character (AC6)."""
    assert not LONE_SURROGATE.search(stdout), "stdout holds a lone surrogate"
    stdout.encode("utf-8")
    raw = sorted({c for c in stdout if c != "\n" and is_raw_control(c)})
    assert not raw, f"stdout holds raw control characters {raw!r}"


def is_raw_control(character: str) -> bool:
    return unicodedata.category(character) in ("Cc", "Zl", "Zp")


def parse(run: Any) -> list[Token]:
    check_stdout(run.stdout)
    assert run.stdout.strip(), "stdout is empty"
    return PARSER.parse(run.stdout)


def inline_children(tokens: list[Token]) -> Iterator[Token]:
    for token in tokens:
        if token.type == "inline":
            yield from token.children or ()


def visible(children: list[Token] | tuple[Token, ...]) -> str:
    """What a reader sees: text and code span contents, a line break as a newline."""
    parts = []
    for child in children:
        if child.type in ("text", "text_special", "code_inline"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append("\n")
        elif child.type == "image":
            parts.append(visible(child.children or ()))
    return "".join(parts)


def outside_code(tokens: list[Token]) -> str:
    """Text a renderer could still turn into a mention or an autolink: not in code."""
    parts = []
    for token in tokens:
        if token.type in ("fence", "code_block"):
            parts.append("\n")
            continue
        for child in token.children or () if token.type == "inline" else ():
            parts.append(child.content if child.type in ("text", "text_special") else " ")
        parts.append("\n")
    return "".join(parts)


def document_text(tokens: list[Token]) -> str:
    return "\n".join(visible(t.children or ()) for t in tokens if t.type == "inline")


@dataclass(frozen=True, slots=True)
class Item:
    text: str
    code_spans: tuple[str, ...]


def list_items(tokens: list[Token]) -> list[Item]:
    """Every list item, outermost level only, with its visible text and its code spans."""
    items: list[Item] = []
    depth = 0
    inlines: list[Token] = []
    for token in tokens:
        if token.type == "list_item_open":
            depth += 1
            if depth == 1:
                inlines = []
        elif token.type == "list_item_close":
            depth -= 1
            if depth == 0:
                children = [c for t in inlines for c in t.children or ()]
                spans = tuple(c.content for c in children if c.type == "code_inline")
                items.append(Item(" ".join(visible(t.children or ()) for t in inlines), spans))
        elif depth and token.type == "inline":
            inlines.append(token)
    return items


def finding_items(tokens: list[Token]) -> list[Item]:
    return [
        item for item in list_items(tokens) if any(LINE_SUFFIX.search(s) for s in item.code_spans)
    ]


def location(item: Item) -> str:
    (span,) = [s for s in item.code_spans if LINE_SUFFIX.search(s)]
    return span


def block_shape(tokens: list[Token]) -> list[tuple[str, str, int]]:
    return [(t.type, t.tag, t.level) for t in tokens if t.type != "inline"]


def inline_counts(tokens: list[Token]) -> dict[str, int]:
    children = list(inline_children(tokens))
    return {name: sum(c.type == name for c in children) for name in INLINE_STRUCTURES}


def headings(tokens: list[Token]) -> list[tuple[str, str]]:
    return [
        (opening.tag, content.content)
        for opening, content in pairwise(tokens)
        if opening.type == "heading_open"
    ]


def words_in_order(value: str, text: str) -> bool:
    """Each whitespace-separated word of `value` appears in `text`, in order."""
    position = 0
    for word in value.split():
        found = text.find(word, position)
        if found < 0:
            return False
        position = found + len(word)
    return True


def assert_inert(hostile: list[Token], control: list[Token], value: str, expected_items: int):
    assert block_shape(hostile) == block_shape(control), "the block structure changed"
    assert inline_counts(hostile) == inline_counts(control), "an inline structure was added"
    for kind in ("html_block", "html_inline", "image", "link_open"):
        hostile_count = sum(t.type == kind for t in [*hostile, *inline_children(hostile)])
        control_count = sum(t.type == kind for t in [*control, *inline_children(control)])
        assert hostile_count == control_count, f"a new {kind}"
    assert headings(hostile) == headings(control), headings(hostile)
    assert_code_span_rule(hostile, value)
    items = finding_items(hostile)
    assert len(items) == len(finding_items(control)) == expected_items
    assert any(words_in_order(value, item.text) for item in items), (
        f"{value!r} isn't visible in any finding: {[i.text for i in items]}"
    )


def assert_code_span_rule(tokens: list[Token], value: str) -> None:
    """No `@` or URL is in text outside code; each word of `value` holding one is in a code span."""
    triggers = GFM_TRIGGER.findall(outside_code(tokens))
    assert not triggers, f"{triggers} outside a code span"
    spans = [c.content for c in inline_children(tokens) if c.type == "code_inline"]
    for word in value.split():
        if GFM_TRIGGER.search(word):
            assert any(word in span for span in spans), f"{word!r} isn't in a code span: {spans}"


# Runs.


@pytest.fixture
def review_md(bot: Any, tmp_path: Path) -> Callable[..., Any]:
    """Review `diff` with `--provider mock --format markdown`, logging under `tmp_path`."""

    def run(diff: Path, *flags: str, provider: Any = None) -> Any:
        log = tmp_path / "audit.jsonl"
        return bot.review(diff, *MARKDOWN, AUDIT_FLAG, str(log), *flags, provider=provider)

    return run


@pytest.fixture
def sample_with(review_md: Callable[..., Any], sample_diff: Path, bot: Any) -> Callable[..., Any]:
    """The sample diff's two rule findings plus these AI findings, as Markdown."""

    def run(*findings: Finding, flags: tuple[str, ...] = ()) -> Any:
        return review_md(sample_diff, *flags, provider=bot.mock(findings=list(findings)))

    return run


def golden(name: str) -> str:
    path = FIXTURES / name
    assert path.exists(), f"golden {path.name} isn't captured yet"
    return path.read_bytes().decode("utf-8")


# AC1: the sample's two rule findings plus one AI finding, as a heading, counts and a list.

RULE_RATIONALES = {35: rationale_of(SAMPLE_RULE_LINES[0]), 39: rationale_of(SAMPLE_RULE_LINES[1])}


def test_p1_s11_ac1_sample_with_one_ai_finding_matches_the_golden_and_exits_1(sample_with):
    run = sample_with(ai_finding())

    assert run.exit_code == 1, run.stderr
    assert run.stdout == golden("sample.golden.md")


def test_p1_s11_ac1_starts_with_the_review_heading_then_counts_then_the_findings(sample_with):
    tokens = parse(sample_with(ai_finding()))

    assert (tokens[0].type, tokens[0].tag, tokens[1].content) == ("heading_open", "h2", "Review")
    assert headings(tokens)[0] == ("h2", "Review")
    first_list = next(i for i, t in enumerate(tokens) if t.type.endswith("_list_open"))
    between = document_text(tokens[3:first_list])
    assert re.search(r"(?<![\d.])3(?![\d.])", between), (
        f"no reported count before the list: {between!r}"
    )


def test_p1_s11_ac1_one_list_item_per_finding_rules_first_with_location_in_a_code_span(
    sample_with,
):
    items = finding_items(parse(sample_with(ai_finding())))

    assert [location(item) for item in items] == [
        f"{INVOICE}:35",
        f"{INVOICE}:39",
        f"{INVOICE}:14",
    ]


@dataclass(frozen=True, slots=True)
class Shown:
    """What one finding's item must show; `confidence` is a pattern for its number."""

    severity: str
    category: str
    rationale: str
    confidence: str
    source: str


SHOWN = [
    Shown("medium", "reliability", RULE_RATIONALES[35], r"1(?:\.0{1,2})?|100 ?%", "rule"),
    Shown("low", "reliability", RULE_RATIONALES[39], r"1(?:\.0{1,2})?|100 ?%", "rule"),
    Shown("high", "logic", ai_finding().rationale, r"0\.80?|80 ?%", "ai"),
]


@pytest.mark.parametrize("index", range(3), ids=["rule_35", "rule_39", "ai_14"])
def test_p1_s11_ac1_each_item_shows_severity_category_rationale_confidence_and_source(
    sample_with, index
):
    item = finding_items(parse(sample_with(ai_finding())))[index]
    shown = SHOWN[index]

    text = item.text
    assert re.search(rf"\b{shown.severity}\b", text, re.IGNORECASE), text
    assert re.search(rf"\b{shown.category}\b", text, re.IGNORECASE), text
    assert shown.rationale in text, text
    number = rf"confidence\W{{0,3}}(?:{shown.confidence})(?![\d.])"
    assert re.search(number, text, re.IGNORECASE), text
    other = "ai" if shown.source == "rule" else "rule"
    assert re.search(rf"\b{shown.source}\b", text, re.IGNORECASE), text
    assert not re.search(rf"\b{other}\b", text, re.IGNORECASE), text


def test_p1_s11_ac1_rule_ids_dont_change_the_sources(sample_with):
    """A tagged AI finding stays `ai`; only the rule check's own findings are `rule`."""
    items = finding_items(parse(sample_with(ai_finding(rule=OPEN_RULE))))

    assert len(items) == 3
    assert re.search(r"\bai\b", items[2].text, re.IGNORECASE), items[2].text


# AC2: no findings is the heading and one sentence, exit 0.


def test_p1_s11_ac2_no_findings_is_the_heading_and_the_sentence_and_exits_0(
    review_md, write_diff, bot
):
    run = review_md(write_diff(MINIMAL_DIFF), provider=bot.mock(findings=[]))

    assert run.exit_code == 0, run.stderr
    tokens = parse(run)
    assert [(t.type, t.tag) for t in tokens if t.type != "inline"] == [
        ("heading_open", "h2"),
        ("heading_close", "h2"),
        ("paragraph_open", "p"),
        ("paragraph_close", "p"),
    ]
    assert document_text(tokens) == "Review\nNo findings at or above low."


def test_p1_s11_ac2_no_findings_matches_the_golden(review_md, write_diff, bot):
    run = review_md(write_diff(MINIMAL_DIFF), provider=bot.mock(findings=[]))

    assert run.stdout == golden("no_findings.golden.md")


def test_p1_s11_ac2_sentence_names_the_threshold_in_use(sample_with):
    run = sample_with(flags=("--threshold", "critical"))

    assert run.exit_code == 0, run.stderr
    tokens = parse(run)
    assert "No findings at or above critical." in document_text(tokens)
    assert finding_items(tokens) == []


# AC3: skipped files are listed with their reasons; dropped and deduped findings are counted.


def test_p1_s11_ac3_skipped_files_section_lists_each_file_with_its_reason(
    review_md, write_diff, bot
):
    diff = write_diff(MINIMAL_DIFF + section("README.md") + section("uv.lock"))

    run = review_md(diff, provider=bot.mock(findings=[]))

    assert run.exit_code == 0, run.stderr
    tokens = parse(run)
    text = document_text(tokens)
    assert "Skipped files" in text, text
    skipped = {path: item for item in list_items(tokens) for path in ("README.md", "uv.lock")
               if path in item.text}  # fmt: skip
    assert set(skipped) == {"README.md", "uv.lock"}, [i.text for i in list_items(tokens)]
    assert re.search(r"\bdocs\b", skipped["README.md"].text.replace("README.md", "")), skipped
    assert re.search(r"\block\b", skipped["uv.lock"].text.replace("uv.lock", "")), skipped
    assert text.index("Skipped files") < text.index("README.md")


def test_p1_s11_ac3_no_skipped_files_section_without_skipped_files(sample_with):
    assert "Skipped files" not in sample_with(ai_finding()).stdout


def test_p1_s11_ac3_all_files_skipped_still_gives_the_heading_and_the_section(
    review_md, write_diff, bot
):
    provider = bot.mock(findings=[])

    run = review_md(write_diff(section("README.md")), provider=provider)

    assert run.exit_code == 0, run.stderr
    assert provider.requests == []
    tokens = parse(run)
    assert headings(tokens)[0] == ("h2", "Review")
    assert "Skipped files" in document_text(tokens)
    assert any("README.md" in item.text for item in list_items(tokens))


def counted(text: str, number: int, word: str) -> bool:
    n = rf"(?<![\d.]){number}(?![\d.])"
    return bool(re.search(rf"{n}\W{{0,3}}{word}|{word}\W{{0,3}}{n}", text, re.IGNORECASE))


@pytest.fixture
def counts_run(review_md: Callable[..., Any], write_diff: Callable[..., Path], bot: Any) -> Any:
    """Three dropped findings, one deduped, one below the threshold, and one skipped file."""
    diff = write_diff(sample_plus(section("README.md", "+Docs only.\n")))
    dropped = [ai_finding(line=997 + n, rationale=f"Dropped {n}.") for n in range(3)]
    deduped = ai_finding(line=35, severity="medium", category="reliability", rule=SWALLOWED)
    return review_md(diff, "--threshold", "medium", provider=bot.mock(findings=[*dropped, deduped]))


def test_p1_s11_ac3_counts_say_how_many_were_dropped_and_deduped(counts_run):
    assert counts_run.exit_code == 1, counts_run.stderr
    text = document_text(parse(counts_run))

    assert counted(text, 3, "dropped"), text
    assert counted(text, 1, "deduped"), text


def test_p1_s11_ac3_dropped_and_deduped_findings_arent_listed(counts_run):
    tokens = parse(counts_run)

    assert [location(item) for item in finding_items(tokens)] == [f"{INVOICE}:35"]
    assert "Dropped 0." not in counts_run.stdout


def test_p1_s11_ac3_matches_the_golden(counts_run):
    assert counts_run.stdout == golden("counts_and_skipped.golden.md")


# AC4: a hostile rationale is inert, as CommonMark and as GitHub-flavoured Markdown.

PLAIN = "Plain words only"
RATIONALE_PAYLOADS = {
    # The spec's list.
    "newline_heading": "\n## Forged",
    "img_onerror": "<img src=x onerror=y>",
    "image": "![x](http://evil)",
    "link": "[click](http://evil)",
    "fence": "\n```\nfenced\n```\n",
    "inline_backticks": "``` x ``` `y`",
    "mention": "@someone",
    "pipe": "a | b | c",
    "star": "*emphasis* **strong** * item",
    # Beyond it, as the task asks.
    "hash_first": "# Forged",
    "quote": "> quoted",
    "newline_quote": "\n> quoted",
    "dash_item": "- item",
    "newline_dash_item": "\n- item\n- another",
    "numbered_item": "1. item",
    "newline_numbered_item": "\n1. item\n2) another",
    "script": "<script>alert(1)</script>",
    # And further: a table, setext heading, rule, indented code, autolink and entity.
    "newline_table": "\n| a | b |\n|---|---|\n| c | d |",
    "setext_heading": "\nForged\n======",
    "thematic_break": "\n---\n",
    "indented_code": "\n\n    code block",
    "autolink": "<http://evil>",
    "entity": "&lt;b&gt; &#64;someone",
    "html_comment": "<!-- hidden -->",
    "reference_link": "[x]\n\n[x]: http://evil",
    "mention_after_paren": "(@someone)",
}
# Spec AC4, changed in build: GitHub-flavoured triggers, alone and inside brackets, escaped or not.
GFM_WORDS = {
    "escaped_mention": r"\@someone",
    "email": "a@b.com",
    "http": "http://evil",
    "https": "https://evil/x",
    "www": "www.evil.com",
    # Spec Interfaces, review round 1: any scheme, not only http and https.
    "ftp": "ftp://evil",
}
GFM_FORMS = {"bare": "{}", "brackets": "[{}]", "escaped_brackets": r"\[{}\]"}
RATIONALE_PAYLOADS |= {
    f"gfm_{word}_{form}": template.format(value)
    for word, value in GFM_WORDS.items()
    for form, template in GFM_FORMS.items()
}
# Beyond the spec's list: case, trailing punctuation, a lone `@`, and a URL glued to a word.
RATIONALE_PAYLOADS |= {
    "gfm_upper_http": "HTTP://EVIL",
    "gfm_upper_ftp": "FTP://EVIL",
    "gfm_other_scheme": "git+ssh://evil/x",
    "gfm_mixed_www": "WwW.Evil.com",
    "gfm_url_then_period": "see http://evil.",
    "gfm_lone_at": "a @ b",
    "gfm_glued_url": "text,https://evil",
    "gfm_two_triggers": "@a http://b www.c @d",
}


def rationale_with(payload: str) -> str:
    return f"Before {payload} after."


@pytest.fixture
def control_tokens(sample_with: Callable[..., Any]) -> list[Token]:
    run = sample_with(ai_finding(rationale=rationale_with(PLAIN)))
    assert run.exit_code == 1, run.stderr
    return parse(run)


@pytest.mark.parametrize("payload", RATIONALE_PAYLOADS.values(), ids=RATIONALE_PAYLOADS.keys())
def test_p1_s11_ac4_hostile_rationale_is_inert(sample_with, control_tokens, payload):
    rationale = rationale_with(payload)

    run = sample_with(ai_finding(rationale=rationale))

    assert run.exit_code == 1, run.stderr
    assert_inert(parse(run), control_tokens, rationale, expected_items=3)


@pytest.mark.parametrize(
    "rationale",
    [
        "".join("`" * n + f" w{n} " for n in range(1, 61)),
        "[" * 2000 + "x" + "](" * 2000,
        "<" * 2000 + "a" + ">" * 2000,
        "\\" * 2001 + "*x*",
        "\n".join(f"{n}. item" for n in range(500)),
    ],
    ids=["backtick_runs", "nested_brackets", "angle_brackets", "backslashes", "many_list_lines"],
)
def test_p1_s11_ac4_limits_large_hostile_rationale_is_inert_and_bounded(
    sample_with, control_tokens, rationale
):
    started = time.monotonic()
    run = sample_with(ai_finding(rationale=rationale))
    elapsed = time.monotonic() - started

    assert run.exit_code == 1, run.stderr
    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    assert_inert(parse(run), control_tokens, rationale, expected_items=3)


def test_p1_s11_ac4_limits_thousands_of_findings_stay_one_item_each_and_bounded(
    review_md, write_diff, bot
):
    count = 3000
    text = section("big.js", "".join(f"+value_{n} = {n}\n" for n in range(count)))
    findings = [
        Finding(
            file="big.js",
            line=n + 1,
            severity="high",
            category="logic",
            rationale=f"Finding {n}: \n## Forged `x` <b>y</b> @someone",
            confidence=0.5,
        )
        for n in range(count)
    ]

    started = time.monotonic()
    run = review_md(write_diff(text), provider=bot.mock(findings=findings))
    elapsed = time.monotonic() - started

    assert run.exit_code == 1, run.stderr
    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    tokens = parse(run)
    items = finding_items(tokens)
    assert len(items) == count
    assert location(items[-1]) == f"big.js:{count}"
    assert [tag for tag, _ in headings(tokens)].count("h2") == 1
    assert not any(c.type == "html_inline" for c in inline_children(tokens))
    assert_code_span_rule(tokens, findings[-1].rationale)


# AC5: a hostile path from a diff header is inert in the same way.

PATH_PAYLOADS = {
    # The spec's list.
    "backtick": "src/a`b.py",
    "double_backtick": "src/a``b`.py",
    "newline_escape": "src/a\n## Forged.py",
    "less_than": "src/a<img src=x onerror=y>.py",
    # Beyond it.
    "script": "src/<script>x</script>.py",
    "link": "src/[click](http:evil).py",
    "mention": "src/@someone.py",
    "pipe_star": "src/a|b*c*.py",
    "leading_hash": "# x.py",
    "leading_backtick": "`a.py",
}


def rule_on(path: str) -> str:
    """A section on `path`, in git's quoted form, with a swallowed handler on line 4."""
    return quoted_section(octal(path), "+x\n" + SWALLOWED_HANDLER)


@pytest.fixture
def path_run(review_md: Callable[..., Any], write_diff: Callable[..., Path], bot: Any):
    def run(path: str) -> Any:
        return review_md(write_diff(rule_on(path)), provider=bot.mock(findings=[]))

    return run


@pytest.mark.parametrize("path", PATH_PAYLOADS.values(), ids=PATH_PAYLOADS.keys())
def test_p1_s11_ac5_hostile_path_of_a_finding_is_inert(path_run, path):
    control = path_run("src/plain.py")
    hostile = path_run(path)

    assert (control.exit_code, hostile.exit_code) == (1, 1), hostile.stderr
    assert_inert(parse(hostile), parse(control), path, expected_items=1)


@pytest.mark.parametrize("path", PATH_PAYLOADS.values(), ids=PATH_PAYLOADS.keys())
def test_p1_s11_ac5_hostile_path_of_a_skipped_file_is_inert(review_md, write_diff, bot, path):
    """The same payloads under `docs/`, ending `.md`, so the file is skipped."""
    hostile_path = "docs/" + path.removeprefix("src/").removesuffix(".py") + ".md"

    def run(skipped: str) -> Any:
        text = MINIMAL_DIFF + quoted_section(octal(skipped))
        return review_md(write_diff(text), provider=bot.mock(findings=[]))

    control, hostile = run("docs/plain.md"), run(hostile_path)

    assert (control.exit_code, hostile.exit_code) == (0, 0), hostile.stderr
    hostile_tokens, control_tokens = parse(hostile), parse(control)
    assert block_shape(hostile_tokens) == block_shape(control_tokens)
    assert inline_counts(hostile_tokens) == inline_counts(control_tokens)
    assert headings(hostile_tokens) == headings(control_tokens)
    assert_code_span_rule(hostile_tokens, hostile_path)
    items = list_items(hostile_tokens)
    assert len(items) == len(list_items(control_tokens))
    assert any(words_in_order(hostile_path, item.text) for item in items), [i.text for i in items]


def test_p1_s11_ac5_plain_path_is_shown_exactly_in_its_code_span(path_run):
    (item,) = finding_items(parse(path_run("src/plain.py")))

    assert location(item) == "src/plain.py:4"


# AC6: no raw control character, from any field.

CONTROLS = {"esc": "\x1b[31m", "line_separator": "\u2028"}
# Beyond the spec's two: other C0, DEL, C1 and the paragraph separator.
MORE_CONTROLS = {"nul": "\x00", "bell": "\x07", "cr": "\r", "tab": "\t", "del": "\x7f",
                 "nel": "\x85", "csi": "\x9b", "paragraph_separator": "\u2029"}  # fmt: skip


@pytest.mark.parametrize(
    "control", [*CONTROLS.values(), *MORE_CONTROLS.values()], ids=[*CONTROLS, *MORE_CONTROLS]
)
def test_p1_s11_ac6_control_in_a_rationale_isnt_raw_on_stdout(sample_with, control):
    run = sample_with(ai_finding(rationale=f"Before {control} after."))

    assert run.exit_code == 1, run.stderr
    check_stdout(run.stdout)
    assert len(finding_items(parse(run))) == 3


@pytest.mark.parametrize("control", CONTROLS.values(), ids=CONTROLS.keys())
def test_p1_s11_ac6_control_in_a_finding_path_isnt_raw_on_stdout(path_run, control):
    run = path_run(f"src/a{control}b.py")

    assert run.exit_code == 1, run.stderr
    check_stdout(run.stdout)
    assert len(finding_items(parse(run))) == 1


@pytest.mark.parametrize("control", CONTROLS.values(), ids=CONTROLS.keys())
def test_p1_s11_ac6_control_in_an_ai_finding_path_isnt_raw_on_stdout(
    review_md, write_diff, bot, control
):
    path = f"src/a{control}b.py"
    ai = Finding(file=path, line=1, severity="high", category="logic", rationale="R.", confidence=1)

    run = review_md(write_diff(quoted_section(octal(path))), provider=bot.mock(findings=[ai]))

    assert run.exit_code == 1, run.stderr
    check_stdout(run.stdout)
    assert len(finding_items(parse(run))) == 1


@pytest.mark.parametrize("control", CONTROLS.values(), ids=CONTROLS.keys())
def test_p1_s11_ac6_control_in_a_skipped_path_isnt_raw_on_stdout(
    review_md, write_diff, bot, control
):
    text = MINIMAL_DIFF + quoted_section(octal(f"docs/a{control}b.md"))

    run = review_md(write_diff(text), provider=bot.mock(findings=[]))

    assert run.exit_code == 0, run.stderr
    check_stdout(run.stdout)


# Spec Notes: a path that isn't valid UTF-8 is shown in git's quoted form, and exits 1, not 14.

NON_UTF8_PY = "caf\\377.py"
NON_UTF8_DOC = "docs/caf\\377.md"
QUOTED_PY = '"caf\\377.py"'
QUOTED_DOC = '"docs/caf\\377.md"'


def test_p1_s11_notes_non_utf8_finding_and_skipped_paths_are_shown_quoted_and_exit_1(
    review_md, write_diff, bot
):
    text = quoted_section(NON_UTF8_PY, SWALLOWED_HANDLER) + quoted_section(NON_UTF8_DOC)

    run = review_md(write_diff(text), provider=bot.mock(findings=[]))

    assert run.exit_code == 1, f"expected exit 1 (not 14): {run.stderr}"
    tokens = parse(run)
    (item,) = finding_items(tokens)
    assert location(item) == f"{QUOTED_PY}:3"
    assert re.search(r"\brule\b", item.text, re.IGNORECASE), item.text
    assert any(QUOTED_DOC in i.text for i in list_items(tokens)), [
        i.text for i in list_items(tokens)
    ]


def test_p1_s11_notes_only_file_skipped_on_a_non_utf8_path_exits_0(review_md, write_diff, bot):
    run = review_md(write_diff(quoted_section(NON_UTF8_DOC)), provider=bot.mock())

    assert run.exit_code == 0, f"expected exit 0 (not 14): {run.stderr}"
    assert QUOTED_DOC in document_text(parse(run))


def test_p1_s11_notes_non_utf8_path_is_written_through_a_real_stdout(tmp_path):
    """A child's stdout is a real stream, so an unencodable path would fail the write (14)."""
    diff = tmp_path / "non_utf8.diff"
    diff.write_bytes(quoted_section(NON_UTF8_PY, SWALLOWED_HANDLER).encode())
    log = tmp_path / "audit.jsonl"

    completed = run_process(bot_command(*MARKDOWN, AUDIT_FLAG, str(log), diff=str(diff)))

    assert completed.returncode == 1, f"expected exit 1 (not 14): {completed.stderr}"
    assert f"{QUOTED_PY}:3" in completed.stdout


# AC7: text is unchanged; any failure leaves stdout empty, in markdown too (the handover).


# Text by default, and by the flag over `REVIEW_BOT_FORMAT=markdown`.
TEXT_SELECTIONS = {"default": ((), None), "flag_over_variable": (TEXT, "markdown")}


@pytest.mark.parametrize("selection", TEXT_SELECTIONS)
@pytest.mark.parametrize("case", GOLDEN_CASES)
def test_p1_s11_ac7_text_stdout_is_byte_identical_to_the_golden(
    bot, write_diff, monkeypatch, case, selection
):
    flags, variable = TEXT_SELECTIONS[selection]
    golden_case = GOLDEN_CASES[case]
    if variable:
        monkeypatch.setenv(FORMAT_VARIABLE, variable)

    run = bot.review(
        write_diff(golden_case.diff_text()),
        *golden_case.flags,
        *flags,
        provider=bot.mock(findings=golden_case.findings()),
    )

    assert run.exit_code in (0, 1), run.stderr
    assert run.stdout == golden_text(golden_case.stdout)


def test_p1_s11_ac7_markdown_summary_line_is_the_text_summary(bot, sample_diff):
    def run(*flags: str) -> Any:
        return bot.review(sample_diff, *flags, provider=bot.mock(findings=[ai_finding()]))

    text, markdown = run(), run(*MARKDOWN)

    assert (text.exit_code, markdown.exit_code) == (1, 1), markdown.stderr
    assert without_run_id(markdown.stderr) == without_run_id(text.stderr)


@pytest.mark.parametrize("case", FAILURE_CASES)
def test_p1_s11_ac7_failure_leaves_stdout_empty_in_markdown_with_stderr_as_text(
    bot, tmp_path, case
):
    failure = failures(tmp_path, bot)[case]

    def run(*format_flags: str) -> Any:
        return bot.review(failure.diff, *format_flags, *failure.flags, provider=failure.provider())

    default, markdown = run(), run(*MARKDOWN)

    for result in (default, markdown):
        assert result.exit_code == failure.code, result.stderr
        assert result.stdout == ""
    assert markdown.stderr.splitlines()[0].startswith(ERROR), markdown.stderr
    assert without_run_id(markdown.stderr) == without_run_id(default.stderr)


@pytest.fixture
def fail_audit_from(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Make `AuditLog.record` fail, as a full disk would, from one `stage.event` on."""

    def install(failing: str) -> None:
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


@pytest.mark.parametrize("failing", ["run.start", "parse.parsed", "agent.called"])
def test_p1_s11_ac7_log_failing_before_the_report_exits_17_with_stdout_empty(
    sample_with, fail_audit_from, failing
):
    fail_audit_from(failing)

    run = sample_with(ai_finding())

    assert run.exit_code == 17, run.stderr
    assert run.stdout == ""


def test_p1_s11_ac7_exception_log_failing_after_the_report_keeps_the_whole_report(
    sample_with, fail_audit_from
):
    """design.md Compatibility: exit 17 following a delivered report keeps that report."""
    fail_audit_from("run.end")

    run = sample_with(ai_finding())

    assert run.exit_code == 17, run.stderr
    assert len(finding_items(parse(run))) == 3


MARKDOWN_ON_STDOUT_FAILURE = [
    pytest.param(CLOSED, bot_command(*MARKDOWN), id="closed"),
    pytest.param(READER_GONE, bot_command(*MARKDOWN), id="reader_gone"),
    pytest.param(DEV_FULL, bot_command(*MARKDOWN), marks=needs_dev_full, id="dev_full"),
    pytest.param(
        READER_GONE, [*injected_command("many_findings"), *MARKDOWN], id="reader_gone_on_write"
    ),
]


def test_p1_s11_ac7_control_the_markdown_command_delivers_a_report():
    completed = run_process(bot_command(*MARKDOWN))

    assert completed.returncode == 1, completed.stderr
    check_stdout(completed.stdout)
    assert headings(PARSER.parse(completed.stdout))[0] == ("h2", "Review")


@pytest.mark.parametrize(("kind", "command"), MARKDOWN_ON_STDOUT_FAILURE)
def test_p1_s11_ac7_stdout_failure_exits_14_with_one_error_line(kind, command):
    completed = run_process(command, stdout=kind)

    assert completed.returncode == 14, completed.stderr
    assert_no_traceback(completed.stderr)
    assert_only_the_error_line(completed.stderr)


# design.md Settings: markdown is chosen by the flag or the variable, the flag first.


def test_p1_s11_settings_variable_alone_selects_markdown(bot, sample_diff, monkeypatch, tmp_path):
    monkeypatch.setenv(FORMAT_VARIABLE, "markdown")

    run = bot.review(
        sample_diff, AUDIT_FLAG, str(tmp_path / "a.jsonl"), provider=bot.mock(findings=[])
    )

    assert run.exit_code == 1, run.stderr
    assert headings(parse(run))[0] == ("h2", "Review")


def test_p1_s11_settings_markdown_flag_beats_a_json_variable(sample_with, monkeypatch):
    monkeypatch.setenv(FORMAT_VARIABLE, "json")

    run = sample_with()

    assert run.exit_code == 1, run.stderr
    assert not run.stdout.lstrip().startswith("{")
    assert [location(item) for item in finding_items(parse(run))] == [
        f"{INVOICE}:35",
        f"{INVOICE}:39",
    ]
