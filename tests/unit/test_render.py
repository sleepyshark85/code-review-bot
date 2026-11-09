import dataclasses
import json

import pytest
from markdown_it import MarkdownIt
from markdown_it.token import Token

from review_bot.context.ledger import LineSpan, RefusalReason, ToolCall, ToolName, ToolOutcome
from review_bot.diff_parser import FileDiff, ParsedDiff, SkipReason
from review_bot.findings import (
    Category,
    EvidenceSpan,
    Finding,
    ReviewedFinding,
    RuleId,
    Severity,
    Source,
)
from review_bot.policy import Decision
from review_bot.prompt import PROMPT_VERSION
from review_bot.providers.base import TokenUsage
from review_bot.render import (
    escape_markdown,
    escape_unprintable,
    render_findings,
    render_json,
    render_markdown,
    report_document,
)
from review_bot.report import PipelineResult, TriageDecision, TriagedFinding
from review_bot.settings import Settings
from review_bot.verify import DropReason, ProposalCheck, VerifyResult

# Assembled, so no key marker is written literally.
KEY_END_MARKER = "-" * 5 + "END RSA " + "PRIVATE KEY" + "-" * 5
NO_USAGE = TokenUsage(input_tokens=0, output_tokens=0)
UNDECODABLE_PATH = b"docs/caf\xff.py".decode("utf-8", "surrogateescape")
LINE_SEPARATOR = chr(0x2028)
BIDI_OVERRIDE = chr(0x202E)


def make_finding(
    *,
    file: str = "a.py",
    severity: Severity = Severity.HIGH,
    rationale: str = "Why.",
) -> Finding:
    return Finding(
        file=file,
        line=7,
        severity=severity,
        category=Category.LOGIC,
        rationale=rationale,
        confidence=0.876,
    )


def reviewed(
    finding: Finding | None = None,
    *,
    source: Source = Source.AI,
    rule: RuleId | None = None,
    fingerprint: str = "0123456789abcdef",
) -> ReviewedFinding:
    return ReviewedFinding(finding or make_finding(), source, rule, fingerprint)


def pipeline_result(
    *,
    reported: tuple[ReviewedFinding, ...] = (),
    below_threshold: tuple[ReviewedFinding, ...] = (),
    checks: tuple[ProposalCheck, ...] = (),
    skipped_files: tuple[FileDiff, ...] = (),
    usage: TokenUsage = NO_USAGE,
) -> PipelineResult:
    return PipelineResult(
        VerifyResult(kept=reported + below_threshold, checks=checks),
        Decision(reported=reported, below_threshold=below_threshold),
        usage,
        skipped_files,
        parsed=ParsedDiff(files=(), reviewable_text="", decoded_paths=()),
        model_called=True,
        prompt_version=PROMPT_VERSION,
    )


def render(result: PipelineResult) -> str:
    return render_json(result, Settings(provider="mock"), "run-1")


def test_render_findings_with_none_names_the_threshold() -> None:
    assert render_findings((), Severity.HIGH) == "No findings at or above high."


def test_render_findings_formats_each_finding_on_its_own_line() -> None:
    findings = [make_finding(), make_finding(file="b.py", severity=Severity.LOW)]

    assert render_findings(findings, Severity.LOW) == (
        "a.py:7 [high/logic] Why. (confidence 0.88)\nb.py:7 [low/logic] Why. (confidence 0.88)"
    )


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("x\ny", r"x\ny"),
        ("x\r\ny", r"x\r\ny"),
        ("x\ty", r"x\ty"),
        ("\x1b[31mred", r"\x1b[31mred"),
        ("x\x85y", r"x\x85y"),
        ("x\u2028y", r"x\u2028y"),
        ("x\u2029y", r"x\u2029y"),
        ("x\u202ey", r"x\u202ey"),
    ],
    ids=["newline", "crlf", "tab", "escape", "next-line", "line-sep", "para-sep", "bidi-override"],
)
def test_render_findings_escapes_unprintable_characters(raw: str, escaped: str) -> None:
    rendered_file = render_findings([make_finding(file=raw)], Severity.LOW)
    rendered_rationale = render_findings([make_finding(rationale=raw)], Severity.LOW)

    assert rendered_file.startswith(f"{escaped}:7 ")
    assert f"] {escaped} (confidence" in rendered_rationale
    assert len(rendered_file.splitlines()) == 1
    assert len(rendered_rationale.splitlines()) == 1


def test_escape_unprintable_with_undecodable_path_byte_escapes_it() -> None:
    assert escape_unprintable("bad\udcffname") == r"bad\udcffname"


def test_render_findings_keeps_printable_unicode() -> None:
    rendered = render_findings([make_finding(rationale="a → b é 日本")], Severity.LOW)

    assert "a → b é 日本" in rendered


def test_render_json_writes_every_report_field_in_a_fixed_order() -> None:
    result = pipeline_result(
        reported=(reviewed(source=Source.RULE, rule=RuleId.SWALLOWED_EXCEPTION),),
        usage=TokenUsage(input_tokens=12, output_tokens=3),
    )

    assert json.loads(render(result)) == {
        "schema": "report/v1",
        "run_id": "run-1",
        "provider": "mock",
        "model": Settings().model,
        "prompt_version": PROMPT_VERSION,
        "threshold": "low",
        "findings": [
            {
                "fingerprint": "0123456789abcdef",
                "source": "rule",
                "rule": "swallowed-exception",
                "file": "a.py",
                "line": 7,
                "severity": "high",
                "category": "logic",
                "rationale": "Why.",
                "confidence": 0.876,
                "triage": None,
                "original_rationale": None,
                "evidence": [],
                "evidence_dropped": 0,
            }
        ],
        "counts": {
            "reported": 1,
            "below_threshold": 0,
            "dropped": 0,
            "deduped": 0,
            "rejected": 0,
            "undecided": 0,
        },
        "skipped_files": [],
        "usage": {"input_tokens": 12, "output_tokens": 3},
        "mode": "diff",
        "context": None,
    }
    assert list(json.loads(render(result))) == [
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
        "mode",
        "context",
    ]


def test_render_json_with_an_untagged_ai_finding_writes_a_null_rule() -> None:
    [finding] = json.loads(render(pipeline_result(reported=(reviewed(),))))["findings"]

    assert finding["source"] == "ai"
    assert finding["rule"] is None


def test_render_json_writes_a_findings_kept_evidence_in_order_and_its_dropped_count() -> None:
    evidence = (
        EvidenceSpan(file="billing/tax.py", start_line=10, end_line=14),
        EvidenceSpan(file="a.py", start_line=3, end_line=3),
    )
    finding = make_finding().model_copy(update={"evidence": evidence})
    result = pipeline_result(reported=(dataclasses.replace(reviewed(finding), evidence_dropped=2),))

    [written] = json.loads(render(result))["findings"]

    assert written["evidence"] == [
        {"file": "billing/tax.py", "start_line": 10, "end_line": 14},
        {"file": "a.py", "start_line": 3, "end_line": 3},
    ]
    assert written["evidence_dropped"] == 2


def test_render_json_lists_only_reported_findings_and_counts_the_rest() -> None:
    proposed = make_finding()
    result = pipeline_result(
        reported=(reviewed(fingerprint="aaaaaaaaaaaaaaaa"),),
        below_threshold=(reviewed(fingerprint="bbbbbbbbbbbbbbbb"),),
        checks=(
            ProposalCheck(proposed, None, drop_reason=DropReason.UNKNOWN_FILE),
            ProposalCheck(proposed, "a.py", duplicate_of="aaaaaaaaaaaaaaaa"),
            ProposalCheck(proposed, "a.py", duplicate_of="aaaaaaaaaaaaaaaa"),
        ),
    )

    report = json.loads(render(result))

    assert [finding["fingerprint"] for finding in report["findings"]] == ["aaaaaaaaaaaaaaaa"]
    assert report["counts"] == {
        "reported": 1,
        "below_threshold": 1,
        "dropped": 1,
        "deduped": 2,
        "rejected": 0,
        "undecided": 0,
    }


def test_render_json_with_no_findings_writes_an_empty_list() -> None:
    report = json.loads(render(pipeline_result()))

    assert report["findings"] == []
    assert report["counts"]["reported"] == 0


def test_render_json_lists_skipped_files_naming_a_deleted_one_by_its_old_path() -> None:
    skipped = (
        FileDiff("README.md", "README.md", (), SkipReason.DOCS),
        FileDiff(None, "uv.lock", (), SkipReason.LOCK),
    )

    report = json.loads(render(pipeline_result(skipped_files=skipped)))

    assert report["skipped_files"] == [
        {"file": "README.md", "reason": "docs"},
        {"file": "uv.lock", "reason": "lock"},
    ]


def test_render_json_quotes_a_path_that_is_not_utf8_and_flags_it() -> None:
    finding = Finding.model_construct(**{**make_finding().model_dump(), "file": UNDECODABLE_PATH})
    skipped = (FileDiff(UNDECODABLE_PATH, None, (), SkipReason.BINARY),)

    report = json.loads(
        render(pipeline_result(reported=(reviewed(finding),), skipped_files=skipped))
    )

    assert report["findings"][0]["file"] == r'"docs/caf\377.py"'
    assert report["findings"][0]["path_quoted"] is True
    assert report["skipped_files"] == [
        {"file": r'"docs/caf\377.py"', "path_quoted": True, "reason": "binary"}
    ]


def test_render_json_with_a_valid_utf8_path_has_no_path_quoted_key() -> None:
    [finding] = json.loads(render(pipeline_result(reported=(reviewed(),))))["findings"]

    assert "path_quoted" not in finding


def test_render_json_with_hostile_text_is_one_ascii_line_that_round_trips() -> None:
    hostile = 'x\x1b[31m\ny\u2028\u2192 "q"'
    finding = make_finding(file=f"src/{hostile}.py", rationale=hostile)

    rendered = render(pipeline_result(reported=(reviewed(finding),)))

    assert rendered.isascii()
    assert len(rendered.splitlines()) == 1
    [parsed] = json.loads(rendered)["findings"]
    assert parsed["file"] == f"src/{hostile}.py"
    assert parsed["rationale"] == hostile


def test_render_json_of_two_equal_results_built_apart_is_identical() -> None:
    def build() -> PipelineResult:
        skipped = (FileDiff("README.md", None, (), SkipReason.DOCS),)
        return pipeline_result(reported=(reviewed(make_finding()),), skipped_files=skipped)

    assert render(build()) == render(build())


def test_render_json_and_text_write_a_path_holding_a_key_marker_alike() -> None:
    path = f"x{KEY_END_MARKER}.py"
    finding = make_finding(file=path)

    [parsed] = json.loads(render(pipeline_result(reported=(reviewed(finding),))))["findings"]

    assert parsed["file"] == path
    assert render_findings([finding], Severity.LOW).startswith(f"{path}:7 ")


def markdown_tokens(markdown: str) -> list[Token]:
    """Parse as CommonMark, plus autolinking bare URLs and e-mail addresses as GitHub does."""
    return MarkdownIt("commonmark", {"linkify": True}).enable("linkify").parse(markdown)


def visible_lines(markdown: str) -> list[str]:
    """Each paragraph or list item's text as a reader sees it, markup removed."""
    return [
        "".join(child.content for child in token.children or () if child.content)
        for token in markdown_tokens(markdown)
        if token.type == "inline"
    ]


def inline_types(markdown: str) -> set[str]:
    return {
        child.type
        for token in markdown_tokens(markdown)
        if token.type == "inline"
        for child in token.children or ()
    }


def test_render_markdown_writes_heading_counts_and_one_item_per_finding() -> None:
    rule_finding = make_finding(file="src/app.py", severity=Severity.MEDIUM, rationale="Swallowed.")
    result = pipeline_result(
        reported=(reviewed(rule_finding, source=Source.RULE), reviewed(make_finding())),
    )

    assert render_markdown(result, Severity.LOW) == (
        "## Review\n"
        "\n"
        "2 reported, 0 below threshold, 0 dropped, 0 deduped.\n"
        "\n"
        r"- **medium** logic at `src/app.py:7`: Swallowed\. (confidence 0.88, source: rule)"
        "\n"
        r"- **high** logic at `a.py:7`: Why\. (confidence 0.88, source: ai)"
    )


def test_render_markdown_with_nothing_to_count_is_the_heading_and_the_no_findings_line() -> None:
    assert render_markdown(pipeline_result(), Severity.MEDIUM) == (
        "## Review\n\nNo findings at or above medium."
    )


def test_render_markdown_with_only_dropped_and_deduped_findings_still_counts_them() -> None:
    proposed = make_finding()
    checks = (
        ProposalCheck(proposed, None, drop_reason=DropReason.UNKNOWN_FILE),
        ProposalCheck(proposed, "a.py", duplicate_of="aaaaaaaaaaaaaaaa"),
    )
    result = pipeline_result(below_threshold=(reviewed(),), checks=checks)

    assert render_markdown(result, Severity.CRITICAL) == (
        "## Review\n"
        "\n"
        "0 reported, 1 below threshold, 1 dropped, 1 deduped.\n"
        "\n"
        "No findings at or above critical."
    )


def test_render_markdown_lists_skipped_files_with_their_reasons() -> None:
    skipped = (
        FileDiff("README.md", "README.md", (), SkipReason.DOCS),
        FileDiff(None, "uv.lock", (), SkipReason.LOCK),
        FileDiff(UNDECODABLE_PATH, None, (), SkipReason.BINARY),
    )

    assert render_markdown(pipeline_result(skipped_files=skipped), Severity.LOW) == (
        "## Review\n"
        "\n"
        "No findings at or above low.\n"
        "\n"
        "### Skipped files\n"
        "\n"
        "- `README.md`: docs\n"
        "- `uv.lock`: lock\n"
        r'- `"docs/caf\377.py"`: binary'
    )


def test_render_markdown_quotes_a_finding_path_that_is_not_utf8() -> None:
    finding = Finding.model_construct(**{**make_finding().model_dump(), "file": UNDECODABLE_PATH})

    rendered = render_markdown(pipeline_result(reported=(reviewed(finding),)), Severity.LOW)

    assert r'`"docs/caf\377.py":7`' in rendered
    assert rendered.isascii()


def test_render_markdown_shows_an_empty_skipped_path_as_a_code_span_not_stray_backticks() -> None:
    skipped = (FileDiff("", None, (), SkipReason.DOCS),)

    rendered = render_markdown(pipeline_result(skipped_files=skipped), Severity.LOW)

    assert rendered.endswith("- `  `: docs")
    assert "code_inline" in inline_types(rendered)


@pytest.mark.parametrize(
    "rationale",
    [
        "x\n## Forged",
        "x\n\n# Forged\n\n- forged item",
        "<img src=x onerror=y>",
        "<!-- hidden -->",
        "![x](http://evil)",
        "[click](http://evil)",
        "<http://evil>",
        "```\ncode\n```",
        "`span` and `` double ``",
        "@someone",
        "Before @someone after.",
        "(@someone)",
        r"\@someone",
        "a@b.com",
        "http://evil",
        "https://evil/x",
        "see ftp://evil.com/x",
        "see FTP://evil.com/x",
        "WWW.evil.com",
        r"\[www.evil.com\]",
        r"\<https://evil/x\>",
        "| a | b |\n|---|---|",
        "*a* **b** _c_ __d__ ~~e~~",
        r"back\slash \* and &amp; &#60;",
        "x  \ny",
        "[ref]\n\n[ref]: http://evil",
    ],
)
def test_render_markdown_makes_a_hostile_rationale_inert_and_keeps_its_text(
    rationale: str,
) -> None:
    result = pipeline_result(reported=(reviewed(make_finding(rationale=rationale)),))

    rendered = render_markdown(result, Severity.LOW)

    block_types = [token.type for token in markdown_tokens(rendered)]
    assert block_types.count("heading_open") == 1
    assert block_types.count("list_item_open") == 1
    assert "html_block" not in block_types
    assert inline_types(rendered) <= {"text", "code_inline", "strong_open", "strong_close"}
    assert visible_lines(rendered)[-1] == (
        f"high logic at a.py:7: {escape_unprintable(rationale)} (confidence 0.88, source: ai)"
    )


def code_spans(markdown: str) -> list[str]:
    return [
        child.content
        for token in markdown_tokens(markdown)
        if token.type == "inline"
        for child in token.children or ()
        if child.type == "code_inline"
    ]


def test_render_markdown_puts_each_word_holding_a_mention_or_url_in_a_code_span() -> None:
    rationale = (
        "Ask (@someone) or a@b.com, see https://x/y`z, ftp://f/x, FTP://F/X and www.e.com; "
        "plain words stay."
    )
    result = pipeline_result(reported=(reviewed(make_finding(rationale=rationale)),))

    rendered = render_markdown(result, Severity.LOW)

    assert code_spans(rendered) == [
        "a.py:7",
        "(@someone)",
        "a@b.com,",
        "https://x/y`z,",
        "ftp://f/x,",
        "FTP://F/X",
        "www.e.com;",
    ]
    assert r"plain words stay\." in rendered


@pytest.mark.parametrize(
    "path",
    [
        "a`b.py",
        "a``b```c.py",
        "`a.py",
        "a.py`",
        " a.py ",
        "<img src=x>.py",
        "a\nb.py",
        f"a{LINE_SEPARATOR}## b.py",
        "[x](http://evil).py",
    ],
)
def test_render_markdown_shows_a_hostile_path_exactly_in_one_code_span(path: str) -> None:
    result = pipeline_result(
        reported=(reviewed(make_finding(file=path)),),
        skipped_files=(FileDiff(path, None, (), SkipReason.DOCS),),
    )

    rendered = render_markdown(result, Severity.LOW)

    shown = escape_unprintable(path)
    assert code_spans(rendered) == [f"{shown}:7", shown]
    assert inline_types(rendered) <= {"text", "code_inline", "strong_open", "strong_close"}


@pytest.mark.parametrize(
    "hostile", [chr(0x1B) + "[31m", LINE_SEPARATOR, chr(0x85), BIDI_OVERRIDE, chr(0x0D), chr(0)]
)
def test_render_markdown_writes_no_raw_control_character(hostile: str) -> None:
    finding = make_finding(file=f"a{hostile}.py", rationale=f"x{hostile}y")
    result = pipeline_result(
        reported=(reviewed(finding),),
        skipped_files=(FileDiff(f"b{hostile}.md", None, (), SkipReason.DOCS),),
    )

    rendered = render_markdown(result, Severity.LOW)

    assert all(char.isprintable() for line in rendered.split("\n") for char in line)


def test_render_markdown_keeps_printable_unicode() -> None:
    finding = make_finding(rationale="a → b é 日本")

    rendered = render_markdown(pipeline_result(reported=(reviewed(finding),)), Severity.LOW)

    assert "a → b é 日本" in rendered


def triaged_pair() -> tuple[PipelineResult, tuple[TriagedFinding, ...]]:
    """Three reported findings, accepted, rejected and left undecided."""
    accepted = reviewed(make_finding(rationale="Kept."), fingerprint="aaaaaaaaaaaaaaaa")
    rejected = reviewed(make_finding(rationale="Wrong."), fingerprint="bbbbbbbbbbbbbbbb")
    undecided = reviewed(make_finding(rationale="Later."), fingerprint="cccccccccccccccc")
    triaged = (
        TriagedFinding(accepted, TriageDecision.ACCEPT),
        TriagedFinding(rejected, TriageDecision.REJECT),
        TriagedFinding(undecided, TriageDecision.UNDECIDED),
    )
    return pipeline_result(reported=(accepted, rejected, undecided)), triaged


def test_render_json_with_triage_keeps_rejected_findings_marked_and_counts_them() -> None:
    result, triaged = triaged_pair()

    report = json.loads(render_json(result, Settings(provider="mock"), "run-1", triaged=triaged))

    assert [(item["rationale"], item["triage"]) for item in report["findings"]] == [
        ("Kept.", "accept"),
        ("Wrong.", "reject"),
        ("Later.", "undecided"),
    ]
    counts = report["counts"]
    assert (counts["reported"], counts["rejected"], counts["undecided"]) == (2, 1, 1)


def test_render_json_without_triage_writes_every_triage_as_null() -> None:
    result, _ = triaged_pair()

    report = json.loads(render(result))

    assert [item["triage"] for item in report["findings"]] == [None, None, None]


def test_render_markdown_with_triage_leaves_out_rejected_findings() -> None:
    result, triaged = triaged_pair()

    rendered = render_markdown(result, Severity.LOW, triaged=triaged)

    assert rendered.startswith("## Review\n\n2 reported, 0 below threshold, ")
    assert r"Kept\." in rendered
    assert r"Later\." in rendered
    assert "Wrong" not in rendered


def test_render_markdown_with_every_finding_rejected_is_the_no_findings_output() -> None:
    finding = reviewed()
    result = pipeline_result(reported=(finding,))

    rendered = render_markdown(
        result, Severity.LOW, triaged=(TriagedFinding(finding, TriageDecision.REJECT),)
    )

    assert rendered == "## Review\n\nNo findings at or above low."


def test_render_json_and_markdown_reject_triaged_findings_that_are_not_the_reported_ones() -> None:
    result, triaged = triaged_pair()
    out_of_order = (triaged[1], triaged[0], triaged[2])

    with pytest.raises(ValueError, match="reported findings, in order"):
        render_json(result, Settings(provider="mock"), "run-1", triaged=out_of_order)
    with pytest.raises(ValueError, match="reported findings, in order"):
        render_markdown(result, Severity.LOW, triaged=triaged[:2])


def edited_pair() -> tuple[PipelineResult, TriagedFinding]:
    """One reported finding, edited to a lower severity and new text."""
    original = reviewed(make_finding(severity=Severity.CRITICAL, rationale="Model text."))
    edited = reviewed(make_finding(severity=Severity.LOW, rationale="Better text."))
    triaged = TriagedFinding(edited, TriageDecision.EDIT, original_rationale="Model text.")
    return pipeline_result(reported=(original,)), triaged


def test_render_json_with_an_edit_shows_the_new_values_and_the_original_rationale() -> None:
    result, triaged = edited_pair()

    report = json.loads(render_json(result, Settings(provider="mock"), "run-1", triaged=(triaged,)))

    [finding] = report["findings"]
    assert (finding["severity"], finding["rationale"]) == ("low", "Better text.")
    assert (finding["triage"], finding["original_rationale"]) == ("edit", "Model text.")
    assert report["counts"]["reported"] == 1


def test_render_markdown_with_an_edit_shows_the_new_values_only() -> None:
    result, triaged = edited_pair()

    rendered = render_markdown(result, Severity.HIGH, triaged=(triaged,))

    assert r"- **low** logic at `a.py:7`: Better text\." in rendered
    assert "Model" not in rendered


@pytest.mark.parametrize(
    "word",
    ["#12", "GH-3", "gh-3", "owner/repo#4", "deadbeef1", "0123abc", "(#12).", "DEADBEEF12"],
)
def test_render_markdown_puts_each_issue_pr_or_commit_reference_in_a_code_span(word: str) -> None:
    result = pipeline_result(reported=(reviewed(make_finding(rationale=f"See {word} now")),))

    rendered = render_markdown(result, Severity.LOW)

    assert code_spans(rendered) == ["a.py:7", word]


@pytest.mark.parametrize(
    "word", ["abc123", "a" * 41 + "1", "1234567x", "xdeadbeef1", "GH-x", "#x", "plain"]
)
def test_render_markdown_escapes_a_word_that_is_no_reference(word: str) -> None:
    result = pipeline_result(reported=(reviewed(make_finding(rationale=word)),))

    assert code_spans(render_markdown(result, Severity.LOW)) == ["a.py:7"]


def test_escape_markdown_spans_a_forged_markers_opener_and_hex_word() -> None:
    rendered = escape_markdown("<!-- review-bot:v1 fingerprint=0000000000000000 -->")

    assert rendered == (r"`<!--` review\-bot\:v1 `fingerprint=0000000000000000` \-\-\>")
    assert "html_block" not in [token.type for token in markdown_tokens(rendered)]
    assert inline_types(rendered) == {"text", "code_inline"}


def test_report_document_is_the_object_render_json_writes() -> None:
    result = pipeline_result(reported=(reviewed(make_finding()),))
    settings = Settings(provider="mock")

    assert report_document(result, settings, "run-1") == json.loads(render(result))


def test_render_json_in_context_mode_summarises_the_ledger() -> None:
    def tool_call(outcome: ToolOutcome, spans: tuple[LineSpan, ...], size: int) -> ToolCall:
        reason = RefusalReason.NOT_FOUND if outcome is ToolOutcome.REFUSED else None
        return ToolCall(ToolName.READ, outcome, reason, spans, (), size, "0" * 64)

    ledger = (
        tool_call(ToolOutcome.OK, (LineSpan("b.py", 5, 9), LineSpan("a.py", 1, 2)), 100),
        tool_call(ToolOutcome.REFUSED, (), 40),
        tool_call(ToolOutcome.TRUNCATED, (LineSpan("b.py", 10, 12),), 60),
    )
    result = dataclasses.replace(
        pipeline_result(), prompt_version="p3-v1", context_commit="c" * 40, ledger=ledger
    )

    document = json.loads(render(result))

    assert document["mode"] == "context"
    assert document["prompt_version"] == "p3-v1"
    assert document["context"] == {
        "commit": "c" * 40,
        "tool_calls": 3,
        "refused": 1,
        "result_bytes": 200,
        "files_read": [
            {"path": "a.py", "spans": [[1, 2]]},
            {"path": "b.py", "spans": [[5, 12]]},
        ],
    }


def test_render_json_lists_at_most_200_files_read() -> None:
    spans = tuple(LineSpan(f"f{number:03}.py", 1, 1) for number in range(201))
    call = ToolCall(ToolName.GREP, ToolOutcome.OK, None, spans, (), 1, "0" * 64)
    result = dataclasses.replace(pipeline_result(), context_commit="c" * 40, ledger=(call,))

    files_read = json.loads(render(result))["context"]["files_read"]

    assert [entry["path"] for entry in files_read] == [f"f{n:03}.py" for n in range(200)]
