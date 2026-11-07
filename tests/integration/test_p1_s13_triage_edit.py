r"""P1-S13: edit a finding during triage.

Written from the spec's criteria, P1-S12's Interfaces and design.md's open question 9, and checked
against pre-slice `HEAD`. Runs go through `main` with `MockProvider` and the simulated terminals of
`test_p1_s12_triage` (its `run_bot` fixture), and a few call `run_triage` directly.

Readings of the spec that the tests rely on:
- After `e`, exactly two more answers are read for a finding (severity, then rationale), plus one
  per invalid severity (AC3, AC4). The severity prompt names "severity"; the rationale one names
  "rationale". Neither names a field AC4 freezes.
- Answers are matched as P1-S12 pinned: after `.strip().lower()`, so ` LOW ` is `low`. A
  rationale is kept as typed bar surrounding whitespace, then redacted; one that is blank after
  stripping is an empty answer and keeps the current text, since `Finding.rationale` must hold a
  non-space.
- "The report shows that rationale" (AC1): text and Markdown show the new text and not the old.
  The old text survives only in JSON's `original_rationale` (open question 9).
- An edited finding keeps its `fingerprint`, so a finding is found by it, not by report position:
  a new severity may move it.
"""

import io
import json
import time
from typing import Any

import pytest
import test_p1_s12_triage as s12
from test_p0_s3_stream_failures import assert_no_traceback
from test_p1_s4_audit_log import aws_key, github_token
from test_p1_s10_json_output import report
from test_p1_s11_markdown_output import check_stdout, finding_items, headings, parse
from test_p1_s12_triage import (
    APP,
    APP_DIFF,
    BOUND_S,
    CTRL_C,
    EOF,
    FIRST,
    FORMATS,
    JSON,
    LABELLED,
    MARKDOWN,
    RECORD_FIELDS,
    RULE_AND_APP_DIFF,
    SECOND,
    THIRD,
    TriageRun,
    ai,
    has_word,
    last_run_records,
    mock,
    raw_controls,
    reviewed,
    text_locations,
    triage_module,
    triage_records,
    triage_values,
)

pytestmark = pytest.mark.integration

# P1-S12's fixtures, shared by name: the simulated terminals, the log and the untriaged baseline.
run_bot = s12.run_bot
log_path = s12.log_path
baseline = s12.baseline

PROMPT = "[a]ccept [r]eject [e]dit"
NEW_TEXT = "Better text."
REDACTED = "[REDACTED]"
# Fields an edit can't change (AC4), as named in `report/v1`.
FROZEN_FIELDS = ("file", "line", "source", "rule", "category", "confidence", "fingerprint")
# Words an edit prompt mustn't offer (AC4). `line` is left out: "one line" is fair wording.
FROZEN_WORDS = ("file", "source", "rule", "category", "confidence", "fingerprint", "anchor")


# Reading the output.


def by_fingerprint(run: TriageRun) -> dict[str, dict[str, Any]]:
    return {finding["fingerprint"]: finding for finding in report(run)["findings"]}


def text_line(run: TriageRun, location: str) -> str:
    (line,) = [line for line in run.stdout.splitlines() if line.startswith(f"{location} [")]
    return line


def markdown_item(run: TriageRun, location: str) -> str:
    tokens = parse(run)
    (item,) = [item for item in finding_items(tokens) if location in item.code_spans]
    return item.text


def shown_in(run: TriageRun, fmt: str, location: str) -> str:
    """The one report entry for `location`, as a reader sees it in text or Markdown."""
    return markdown_item(run, location) if fmt == "markdown" else text_line(run, location)


def severity_of(entry: str, fmt: str) -> str:
    """A text line's `[severity/category]`, or a Markdown item's leading bold severity."""
    if fmt == "markdown":
        return entry.split(maxsplit=1)[0]
    return entry.split(" [", 1)[1].split("/", 1)[0]


class TerminalText(io.StringIO):
    """A real line-splitting stdin that says it's a terminal: each `readline` is one line."""

    def isatty(self) -> bool:
        return True


def scripted(answers: list[str]) -> Any:
    """A `read_line` giving each answer in turn, whatever the prompt."""
    remaining = iter(answers)
    return lambda _prompt: next(remaining)


def edit_once(*, severity: str = "", rationale: str = "") -> list[str]:
    return ["e", severity, rationale]


# AC1: `e`, `low`, `Better text.` shows severity low and that rationale, in every format.


@pytest.mark.parametrize("fmt", ["text", "markdown"])
def test_p1_s13_ac1_edited_ai_finding_shows_the_new_severity_and_rationale(
    run_bot, write_diff, fmt
):
    run = run_bot(
        write_diff(APP_DIFF),
        *FORMATS[fmt],
        provider=mock(FIRST),
        answers=edit_once(severity="low", rationale=NEW_TEXT),
    )

    assert run.exit_code == 1, run.stderr
    entry = shown_in(run, fmt, f"{APP}:2")
    assert severity_of(entry, fmt) == "low", entry
    assert NEW_TEXT in entry, entry
    assert FIRST.rationale not in run.stdout


@pytest.mark.parametrize("fmt", ["text", "markdown"])
def test_p1_s13_ac1_edited_rule_finding_shows_the_new_severity_and_rationale(
    run_bot, write_diff, baseline, fmt
):
    rule = baseline[0]
    run = run_bot(
        write_diff(RULE_AND_APP_DIFF),
        *FORMATS[fmt],
        provider=mock(FIRST),
        answers=[*edit_once(severity="low", rationale=NEW_TEXT), "a"],
    )

    assert run.exit_code == 1, run.stderr
    entry = shown_in(run, fmt, f"{rule['file']}:{rule['line']}")
    assert severity_of(entry, fmt) == "low", entry
    assert NEW_TEXT in entry, entry
    assert NEW_TEXT not in shown_in(run, fmt, f"{APP}:2")


def test_p1_s13_ac1_json_marks_edit_and_keeps_the_models_text(run_bot, write_diff):
    run = run_bot(
        write_diff(APP_DIFF),
        *JSON,
        provider=mock(FIRST),
        answers=edit_once(severity="low", rationale=NEW_TEXT),
    )

    assert run.exit_code == 1, run.stderr
    (finding,) = report(run)["findings"]
    assert finding["triage"] == "edit"
    assert finding["severity"] == "low"
    assert finding["rationale"] == NEW_TEXT
    assert finding["original_rationale"] == FIRST.rationale


def test_p1_s13_ac1_json_keeps_the_rules_text_and_leaves_the_accepted_one_alone(
    run_bot, write_diff, baseline
):
    rule, model = baseline
    run = run_bot(
        write_diff(RULE_AND_APP_DIFF),
        *JSON,
        provider=mock(FIRST),
        answers=[*edit_once(severity="low", rationale=NEW_TEXT), "a"],
    )

    assert run.exit_code == 1, run.stderr
    findings = by_fingerprint(run)
    edited, accepted = findings[rule["fingerprint"]], findings[model["fingerprint"]]
    assert (edited["triage"], edited["severity"], edited["rationale"]) == ("edit", "low", NEW_TEXT)
    assert edited["original_rationale"] == rule["rationale"]
    assert accepted["triage"] == "accept"
    assert accepted["rationale"] == model["rationale"]
    assert accepted["original_rationale"] is None
    assert report(run)["counts"]["reported"] == 2


def test_p1_s13_ac1_the_prompt_offers_edit(run_bot, write_diff):
    run = run_bot(write_diff(APP_DIFF), provider=mock(FIRST), answers=["a"])

    assert run.stderr.count(PROMPT) == 1, run.stderr


@pytest.mark.parametrize("answer", ["E", " e ", "e\r"], ids=["upper", "spaces", "carriage_return"])
def test_p1_s13_ac1_e_is_matched_like_the_other_answers(run_bot, write_diff, answer):
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(FIRST), answers=[answer, "low", NEW_TEXT]
    )

    assert triage_values(run) == ["edit"]


@pytest.mark.parametrize("severity", ["LOW", " low ", "Low\r"], ids=["upper", "spaces", "cr"])
def test_p1_s13_ac1_severity_is_matched_after_strip_and_lower(run_bot, write_diff, severity):
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(FIRST), answers=["e", severity, NEW_TEXT]
    )

    assert run.stdin_reads == 3, "a valid severity was asked again"
    (finding,) = report(run)["findings"]
    assert finding["severity"] == "low"


@pytest.mark.parametrize("severity", ["low", "medium", "high", "critical"])
def test_p1_s13_ac1_every_severity_can_be_chosen(run_bot, write_diff, severity):
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(SECOND), answers=edit_once(severity=severity)
    )

    (finding,) = report(run)["findings"]
    assert (finding["triage"], finding["severity"]) == ("edit", severity)


def test_p1_s13_ac1_unicode_rationale_is_kept_exactly(run_bot, write_diff):
    text = 'Ünïcode → "quoted" text, café.'
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(FIRST), answers=edit_once(rationale=text)
    )

    (finding,) = report(run)["findings"]
    assert finding["rationale"] == text


def test_p1_s13_ac1_the_next_finding_is_asked_as_usual_after_an_edit(run_bot, write_diff):
    run = run_bot(
        write_diff(APP_DIFF),
        *JSON,
        provider=mock(FIRST, SECOND),
        answers=[*edit_once(severity="low", rationale=NEW_TEXT), "r"],
    )

    assert run.stdin_reads == 4
    assert run.stderr.count(PROMPT) == 2, run.stderr
    assert triage_values(run) == ["edit", "reject"]
    assert report(run)["counts"]["rejected"] == 1


# AC2: two empty answers keep the values, and it's still an edit.


def test_p1_s13_ac2_two_empty_answers_keep_the_values_with_label_edit(run_bot, write_diff):
    run = run_bot(write_diff(APP_DIFF), *JSON, provider=mock(FIRST), answers=edit_once())

    assert run.exit_code == 1, run.stderr
    (finding,) = report(run)["findings"]
    assert finding["triage"] == "edit"
    assert finding["severity"] == FIRST.severity
    assert finding["rationale"] == FIRST.rationale
    assert finding["original_rationale"] is None


def test_p1_s13_ac2_two_empty_answers_log_an_unchanged_edit(run_bot, write_diff, log_path):
    run_bot(write_diff(APP_DIFF), provider=mock(FIRST), answers=edit_once())

    (record,) = triage_records(log_path)
    assert record["data"]["label"] == "edit"
    assert record["data"]["severity_before"] == record["data"]["severity_after"] == "high"
    assert record["data"]["rationale_changed"] is False


@pytest.mark.parametrize("fmt", ["text", "markdown"])
def test_p1_s13_ac2_an_unchanged_edit_renders_like_an_accept(run_bot, write_diff, fmt):
    diff = write_diff(APP_DIFF)
    accepted = run_bot(diff, *FORMATS[fmt], provider=mock(FIRST), answers=["a"])
    edited = run_bot(diff, *FORMATS[fmt], provider=mock(FIRST), answers=edit_once())

    assert edited.exit_code == accepted.exit_code == 1
    assert edited.stdin_reads == 3, "the edit wasn't one prompt plus two empty answers"
    assert edited.stdout == accepted.stdout


@pytest.mark.parametrize("blank", ["   ", "\t"], ids=["spaces", "tab"])
def test_p1_s13_ac2_a_blank_rationale_counts_as_empty(run_bot, write_diff, log_path, blank):
    run = run_bot(write_diff(APP_DIFF), *JSON, provider=mock(FIRST), answers=["e", "", blank])

    (finding,) = report(run)["findings"]
    assert finding["rationale"] == FIRST.rationale
    assert finding["original_rationale"] is None
    (record,) = triage_records(log_path)
    assert record["data"]["rationale_changed"] is False


def test_p1_s13_ac2_severity_only_edit_keeps_the_rationale(run_bot, write_diff, log_path):
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(FIRST), answers=edit_once(severity="low")
    )

    (finding,) = report(run)["findings"]
    assert (finding["severity"], finding["rationale"]) == ("low", FIRST.rationale)
    assert finding["original_rationale"] is None
    (record,) = triage_records(log_path)
    assert record["data"]["rationale_changed"] is False
    assert (record["data"]["severity_before"], record["data"]["severity_after"]) == ("high", "low")


def test_p1_s13_ac2_rationale_only_edit_keeps_the_severity(run_bot, write_diff, log_path):
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(FIRST), answers=edit_once(rationale=NEW_TEXT)
    )

    (finding,) = report(run)["findings"]
    assert (finding["severity"], finding["rationale"]) == ("high", NEW_TEXT)
    assert finding["original_rationale"] == FIRST.rationale
    (record,) = triage_records(log_path)
    assert record["data"]["rationale_changed"] is True
    assert record["data"]["severity_before"] == record["data"]["severity_after"] == "high"


# AC3: an unknown severity asks again, with the rest unaffected.


@pytest.mark.parametrize("invalid", ["urgent", "info", "crit", "a", "e", "low!"])
def test_p1_s13_ac3_unknown_severity_asks_again(run_bot, write_diff, invalid):
    run = run_bot(
        write_diff(APP_DIFF),
        *JSON,
        provider=mock(FIRST),
        answers=["e", invalid, "low", NEW_TEXT],
    )

    assert run.stdin_reads == 4, "the severity wasn't asked again exactly once"
    (finding,) = report(run)["findings"]
    assert (finding["triage"], finding["severity"], finding["rationale"]) == (
        "edit",
        "low",
        NEW_TEXT,
    )


def test_p1_s13_ac3_unknown_severity_leaves_the_other_findings_alone(run_bot, write_diff):
    run = run_bot(
        write_diff(APP_DIFF),
        *JSON,
        provider=mock(FIRST, SECOND),
        answers=["e", "urgent", "urgent", "", "", "a"],
    )

    assert run.stdin_reads == 6
    assert run.stderr.count(PROMPT) == 2, run.stderr
    findings = report(run)["findings"]
    assert [f["triage"] for f in findings] == ["edit", "accept"]
    assert [f["severity"] for f in findings] == ["high", "medium"]
    assert [f["rationale"] for f in findings] == [FIRST.rationale, SECOND.rationale]


def test_p1_s13_ac3_unknown_severity_isnt_shown_back_in_full(run_bot, write_diff):
    huge = "u" * 1_000_000
    run = run_bot(write_diff(APP_DIFF), *JSON, provider=mock(FIRST), answers=["e", huge, "low", ""])

    assert run.elapsed_s < BOUND_S
    assert "u" * 1000 not in run.stderr
    (finding,) = report(run)["findings"]
    assert finding["severity"] == "low"


def test_p1_s13_ac3_run_triage_asks_for_severity_again():
    prompts: list[str] = []
    answers = iter(["e", "urgent", "low", NEW_TEXT])

    def read_line(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    result = triage_module().run_triage(
        [reviewed(FIRST, "0123456789abcdef")], read_line=read_line, write_line=lambda _: None
    )

    assert len(prompts) == 4
    assert "severity" in prompts[1].lower(), prompts
    assert "severity" in prompts[2].lower(), prompts
    (label,) = result.labels
    assert str(label.severity_after) == "low"


# AC4: an edit can't change, or offer, the anchor and identity fields.


@pytest.mark.parametrize("target", [0, 1], ids=["rule", "ai"])
def test_p1_s13_ac4_edit_keeps_the_frozen_fields(run_bot, write_diff, baseline, target):
    answers = ["a", "a"]
    answers[target : target + 1] = edit_once(severity="critical", rationale=NEW_TEXT)
    run = run_bot(write_diff(RULE_AND_APP_DIFF), *JSON, provider=mock(FIRST), answers=answers)

    assert run.exit_code == 1, run.stderr
    before = baseline[target]
    after = by_fingerprint(run).get(before["fingerprint"])
    assert after is not None, "the edited finding's fingerprint changed"
    assert after["triage"] == "edit"
    assert {key: after[key] for key in FROZEN_FIELDS} == {key: before[key] for key in FROZEN_FIELDS}


def test_p1_s13_ac4_an_edit_reads_only_severity_and_rationale(run_bot, write_diff):
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(FIRST), answers=[*edit_once(), "x", "y"]
    )

    assert run.stdin_reads == 3, "an edit read more than a severity and a rationale"


def test_p1_s13_ac4_edit_prompts_ask_for_severity_then_rationale_only():
    prompts: list[str] = []
    answers = iter(edit_once(severity="low", rationale=NEW_TEXT))

    def read_line(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    triage_module().run_triage(
        [reviewed(FIRST, "0123456789abcdef")], read_line=read_line, write_line=lambda _: None
    )

    assert len(prompts) == 3, prompts
    severity_prompt, rationale_prompt = prompts[1].lower(), prompts[2].lower()
    assert "severity" in severity_prompt
    assert "rationale" in rationale_prompt
    for prompt in (severity_prompt, rationale_prompt):
        offered = [word for word in FROZEN_WORDS if has_word(prompt, word)]
        assert not offered, (offered, prompt)


def test_p1_s13_ac4_run_triage_label_keeps_fingerprint_and_source():
    result = triage_module().run_triage(
        [reviewed(FIRST, "0123456789abcdef")],
        read_line=scripted(edit_once(severity="low", rationale=NEW_TEXT)),
        write_line=lambda _: None,
    )

    (label,) = result.labels
    assert label.fingerprint == "0123456789abcdef"
    assert str(label.source) == "ai"
    assert str(label.label) == "edit"


# AC5: a human's lowered severity outranks the threshold, which ran before triage.


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_p1_s13_ac5_critical_lowered_to_low_stays_with_threshold_high(run_bot, write_diff, fmt):
    run = run_bot(
        write_diff(APP_DIFF),
        "--threshold",
        "high",
        *FORMATS[fmt],
        provider=mock(THIRD),
        answers=edit_once(severity="low"),
    )

    assert run.exit_code == 1, run.stderr
    if fmt == "json":
        document = report(run)
        (finding,) = document["findings"]
        assert (finding["severity"], finding["triage"]) == ("low", "edit")
        assert document["counts"]["reported"] == 1
        assert document["counts"]["below_threshold"] == 0
    else:
        assert severity_of(shown_in(run, fmt, f"{APP}:7"), fmt) == "low"


def test_p1_s13_ac5_a_finding_below_the_threshold_still_isnt_asked(run_bot, write_diff):
    low = ai(5, "Low problem: a name shadows a builtin.", severity="low")
    run = run_bot(
        write_diff(APP_DIFF),
        "--threshold",
        "high",
        *JSON,
        provider=mock(THIRD, low),
        answers=[*edit_once(severity="low"), "a"],
    )

    assert run.stdin_reads == 3
    document = report(run)
    assert [f["line"] for f in document["findings"]] == [7]
    assert document["counts"]["below_threshold"] == 1


# AC6: the label records the severities and whether the text changed, not the text.


def test_p1_s13_ac6_triage_record_holds_the_change_but_not_the_text(
    run_bot, write_diff, log_path, baseline
):
    unusual_text = "Zanzibarite overflow in quuxification"
    run = run_bot(
        write_diff(RULE_AND_APP_DIFF),
        provider=mock(FIRST),
        answers=["a", *edit_once(severity="low", rationale=unusual_text)],
    )

    assert run.exit_code == 1, run.stderr
    records = triage_records(log_path)
    assert [record["event"] for record in records] == [LABELLED, LABELLED]
    edited = records[1]["data"]
    assert set(edited) == RECORD_FIELDS, sorted(edited)
    assert edited["fingerprint"] == baseline[1]["fingerprint"]
    assert edited["label"] == "edit"
    assert (edited["severity_before"], edited["severity_after"]) == ("high", "low")
    assert edited["rationale_changed"] is True
    for record in last_run_records(log_path):
        serialised = json.dumps(record)
        for word in [word for word in unusual_text.split() if len(word) > 6]:
            assert word not in serialised, (word, record)


def test_p1_s13_ac6_run_triage_label_records_the_change():
    result = triage_module().run_triage(
        [reviewed(THIRD, "0123456789abcdef")],
        read_line=scripted(edit_once(severity="medium", rationale=NEW_TEXT)),
        write_line=lambda _: None,
    )

    (label,) = result.labels
    assert str(label.severity_before) == "critical"
    assert str(label.severity_after) == "medium"
    assert label.rationale_changed is True
    assert NEW_TEXT not in repr(label)


# AC7: a newline or `\x1b` in the new text can't break a line, and renders safely.


def test_p1_s13_ac7_a_newline_ends_the_answer_by_construction(run_bot, write_diff):
    """A real stdin splits lines: the text after the newline is the next answer, not rationale."""
    stdin = TerminalText("e\n\nStartMark\n## Forged EndMark\n")
    run = run_bot(write_diff(APP_DIFF), *MARKDOWN, provider=mock(FIRST, SECOND), stdin=stdin)

    assert run.exit_code == 1, run.stderr
    tokens = parse(run)
    assert headings(tokens) == [("h2", "Review")], headings(tokens)
    items = finding_items(tokens)
    assert len(items) == 2
    assert any("StartMark" in item.text for item in items)
    assert "Forged" not in run.stdout


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_p1_s13_ac7_escape_in_new_text_renders_safely(run_bot, write_diff, fmt):
    text = "StartMark \x1b[31m red \x1b]0;title\x07 EndMark"
    run = run_bot(
        write_diff(APP_DIFF), *FORMATS[fmt], provider=mock(FIRST), answers=edit_once(rationale=text)
    )

    assert run.exit_code == 1, run.stderr
    assert not raw_controls(run.stdout), raw_controls(run.stdout)
    if fmt == "json":
        (finding,) = report(run)["findings"]
        assert finding["rationale"] == text
    elif fmt == "markdown":
        check_stdout(run.stdout)
        (item,) = finding_items(parse(run))
        assert "StartMark" in item.text
        assert "EndMark" in item.text
    else:
        assert "EndMark" in text_line(run, f"{APP}:2")


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_p1_s13_ac7_limits_a_line_that_still_holds_a_newline_renders_safely(
    run_bot, write_diff, fmt
):
    """A stream that breaks the line contract: P1-S10 and P1-S11's rules still hold the line."""
    text = "StartMark \x1b[31m red\n## Forged\r" + chr(0x2028) + " EndMark"
    run = run_bot(
        write_diff(APP_DIFF),
        *FORMATS[fmt],
        provider=mock(FIRST, SECOND),
        answers=[*edit_once(rationale=text), "a"],
    )

    assert run.exit_code == 1, run.stderr
    assert not raw_controls(run.stdout), raw_controls(run.stdout)
    if fmt == "json":
        findings = report(run)["findings"]
        assert [f["triage"] for f in findings] == ["edit", "accept"]
        assert findings[0]["rationale"].startswith("StartMark")
    elif fmt == "markdown":
        tokens = parse(run)
        assert headings(tokens) == [("h2", "Review")], headings(tokens)
        items = finding_items(tokens)
        assert len(items) == 2
        assert "StartMark" in markdown_item(run, f"{APP}:2")
    else:
        assert text_locations(run.stdout) == [f"{APP}:2", f"{APP}:5"]
        assert "EndMark" in text_line(run, f"{APP}:2")


def test_p1_s13_ac7_escape_in_new_text_isnt_echoed_raw_to_stderr(run_bot, write_diff):
    text = "StartMark \x1b[31m red EndMark"
    run = run_bot(write_diff(APP_DIFF), provider=mock(FIRST), answers=edit_once(rationale=text))

    assert run.stderr.count(PROMPT) == 1, run.stderr
    assert "EndMark" in text_line(run, f"{APP}:2")
    assert not raw_controls(run.stderr), raw_controls(run.stderr)


@pytest.mark.parametrize("payload", ["\n## Forged", "<img src=x onerror=y>", "[x](http://evil)"])
def test_p1_s13_ac7_markdown_in_new_text_is_inert(run_bot, write_diff, payload):
    run = run_bot(
        write_diff(APP_DIFF),
        *MARKDOWN,
        provider=mock(FIRST),
        answers=edit_once(rationale=f"StartMark {payload} EndMark"),
    )

    tokens = parse(run)
    assert headings(tokens) == [("h2", "Review")]
    children = [c for t in tokens if t.type == "inline" for c in t.children or ()]
    assert not [c.type for c in children if c.type in ("html_inline", "link_open", "image")]
    (item,) = finding_items(tokens)
    assert "StartMark" in item.text
    assert "EndMark" in item.text


# AC8: an assembled secret in the new text is shown as `[REDACTED]`.


@pytest.mark.parametrize("fmt", sorted(FORMATS))
@pytest.mark.parametrize("secret", [aws_key(3), github_token(5)], ids=["aws", "github"])
def test_p1_s13_ac8_secret_in_new_text_is_redacted_in_the_report(run_bot, write_diff, fmt, secret):
    run = run_bot(
        write_diff(APP_DIFF),
        *FORMATS[fmt],
        provider=mock(FIRST),
        answers=edit_once(rationale=f"StartMark key {secret} EndMark"),
    )

    assert run.exit_code == 1, run.stderr
    assert secret not in run.stdout
    if fmt == "json":
        (finding,) = report(run)["findings"]
        assert REDACTED in finding["rationale"]
        assert finding["rationale"].startswith("StartMark")
    elif fmt == "markdown":
        (item,) = finding_items(parse(run))
        assert REDACTED in item.text
    else:
        assert REDACTED in text_line(run, f"{APP}:2")


def test_p1_s13_ac8_secret_in_new_text_reaches_neither_stderr_nor_the_log(
    run_bot, write_diff, log_path
):
    secret = aws_key(11)
    run = run_bot(
        write_diff(APP_DIFF), provider=mock(FIRST), answers=edit_once(rationale=f"key {secret}")
    )

    assert run.stderr.count(PROMPT) == 1, run.stderr
    assert REDACTED in text_line(run, f"{APP}:2")
    assert secret not in run.stderr
    assert secret not in log_path.read_text(encoding="utf-8")


def test_p1_s13_ac8_secret_in_new_text_doesnt_stop_the_report(run_bot, write_diff):
    """The diff was already scanned; a secret typed in triage is redacted, not a 16."""
    run = run_bot(
        write_diff(APP_DIFF),
        *JSON,
        provider=mock(FIRST),
        answers=edit_once(rationale=f"key {aws_key(2)}"),
    )

    assert run.exit_code == 1, run.stderr
    assert triage_values(run) == ["edit"]


# AC9: EOF or Ctrl+C part-way through an edit leaves the finding undecided and unchanged.


@pytest.mark.parametrize("stop", [EOF, CTRL_C], ids=["eof", "ctrl_c"])
@pytest.mark.parametrize(
    "before_stop",
    [["e"], ["e", "low"], ["e", "urgent"]],
    ids=["at_severity", "at_rationale", "after_invalid_severity"],
)
def test_p1_s13_ac9_stopping_part_way_leaves_it_undecided_and_unchanged(
    run_bot, write_diff, log_path, stop, before_stop
):
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(FIRST, SECOND), answers=[*before_stop, stop]
    )

    assert run.exit_code == 1, run.stderr
    assert run.stderr.count(PROMPT) == 1, "`e` didn't start an edit, or asking went on"
    assert run.stdin_reads == len(before_stop) + 1
    assert_no_traceback(run.stderr)
    assert "KeyboardInterrupt" not in run.stderr
    document = report(run)
    findings = document["findings"]
    assert [f["triage"] for f in findings] == ["undecided", "undecided"]
    assert [f["severity"] for f in findings] == ["high", "medium"]
    assert [f["rationale"] for f in findings] == [FIRST.rationale, SECOND.rationale]
    assert [f["original_rationale"] for f in findings] == [None, None]
    assert document["counts"]["undecided"] == 2
    assert triage_records(log_path) == []


@pytest.mark.parametrize("stop", [EOF, CTRL_C], ids=["eof", "ctrl_c"])
def test_p1_s13_ac9_earlier_decisions_stand_when_an_edit_is_cut_short(run_bot, write_diff, stop):
    run = run_bot(
        write_diff(APP_DIFF),
        *JSON,
        provider=mock(FIRST, SECOND, THIRD),
        answers=[*edit_once(severity="low", rationale=NEW_TEXT), "e", "medium", stop],
    )

    assert run.exit_code == 1, run.stderr
    findings = report(run)["findings"]
    assert [f["triage"] for f in findings] == ["edit", "undecided", "undecided"]
    assert [f["severity"] for f in findings] == ["low", "medium", "critical"]
    assert findings[0]["rationale"] == NEW_TEXT


@pytest.mark.parametrize("stop", [EOF, CTRL_C], ids=["eof", "ctrl_c"])
def test_p1_s13_ac9_cut_short_edit_shows_the_original_in_text(run_bot, write_diff, stop):
    run = run_bot(write_diff(APP_DIFF), provider=mock(FIRST), answers=["e", "low", stop])

    assert run.exit_code == 1, run.stderr
    assert run.stderr.count(PROMPT) == 1, run.stderr
    line = text_line(run, f"{APP}:2")
    assert severity_of(line, "text") == "high"
    assert FIRST.rationale in line


def test_p1_s13_ac9_run_triage_drops_a_cut_short_edit():
    prompts: list[str] = []

    def read_line(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "e"
        raise KeyboardInterrupt

    result = triage_module().run_triage(
        [reviewed(FIRST, "0123456789abcdef")], read_line=read_line, write_line=lambda _: None
    )

    assert len(prompts) == 2, prompts
    assert PROMPT in prompts[0]
    assert PROMPT not in prompts[1], "`e` was refused and the decision asked again"
    assert list(result.labels) == []


# Attacks beyond the criteria' own examples.


def test_p1_s13_limits_a_megabyte_rationale_finishes_within_the_bound(run_bot, write_diff):
    text = "StartMark " + "w" * 1_000_000 + " EndMark"
    run = run_bot(
        write_diff(APP_DIFF), *JSON, provider=mock(FIRST), answers=edit_once(rationale=text)
    )

    assert run.elapsed_s < BOUND_S
    assert run.exit_code == 1, run.stderr
    (finding,) = report(run)["findings"]
    assert finding["triage"] == "edit"
    assert "w" * 1000 not in run.stderr, "the new rationale was echoed back in full"


def test_p1_s13_limits_hundreds_of_edits_within_the_bound(run_bot, write_diff, log_path):
    from diff_support import section  # noqa: PLC0415 - only this test needs a long diff

    count = 200
    diff = write_diff(section(APP, "+x\n" * count))
    findings = [ai(line, f"Problem number {line}.") for line in range(1, count + 1)]
    answers = [
        answer
        for line in range(count)
        for answer in edit_once(severity="low", rationale=f"Edited {line}.")
    ]
    start = time.monotonic()
    run = run_bot(diff, *JSON, provider=mock(*findings), answers=answers)

    assert time.monotonic() - start < BOUND_S
    document = report(run)
    assert {f["triage"] for f in document["findings"]} == {"edit"}
    assert {f["severity"] for f in document["findings"]} == {"low"}
    assert len(triage_records(log_path)) == count


def test_p1_s13_limits_edit_then_stdout_failure_still_logs_the_label(
    run_bot, write_diff, log_path, failing_stream
):
    run = run_bot(
        write_diff(APP_DIFF),
        provider=mock(FIRST),
        answers=edit_once(severity="low", rationale=NEW_TEXT),
        stdout=failing_stream("write"),
    )

    assert run.exit_code == 14, run.stderr
    (record,) = triage_records(log_path)
    assert record["data"]["label"] == "edit"
