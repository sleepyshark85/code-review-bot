from collections.abc import Iterable
from typing import override

import pytest

from review_bot.findings import Category, Finding, ReviewedFinding, RuleId, Severity, Source
from review_bot.report import TriageDecision, TriagedFinding
from review_bot.triage import Label, TriageResult, run_triage

# Assembled, so no key marker is written literally.
AWS_KEY = "AK" + "IA" + "ABCDEFGH" + "23456789"


def reviewed(
    fingerprint: str = "0123456789abcdef",
    *,
    file: str = "a.py",
    rationale: str = "Why.",
    source: Source = Source.AI,
) -> ReviewedFinding:
    finding = Finding(
        file=file,
        line=7,
        severity=Severity.HIGH,
        category=Category.LOGIC,
        rationale=rationale,
        confidence=0.9,
    )
    return ReviewedFinding(finding, source, None, fingerprint)


class Terminal:
    """Scripted answers, and a transcript of every prompt and line written."""

    def __init__(self, answers: Iterable[str | type[BaseException]]) -> None:
        self._answers = iter(answers)
        self.prompts: list[str] = []
        self.lines: list[str] = []

    def read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        answer = next(self._answers, EOFError)
        if isinstance(answer, str):
            return answer
        raise answer

    def write_line(self, text: str) -> None:
        self.lines.append(text)


def triage(findings: tuple[ReviewedFinding, ...], terminal: Terminal) -> TriageResult:
    return run_triage(findings, read_line=terminal.read_line, write_line=terminal.write_line)


def decisions(result: TriageResult) -> list[TriageDecision]:
    return [item.decision for item in result.findings]


def test_run_triage_records_each_answer_as_the_findings_decision() -> None:
    findings = (reviewed("aaaaaaaaaaaaaaaa"), reviewed("bbbbbbbbbbbbbbbb"))

    result = triage(findings, Terminal(["a", "r"]))

    assert [item.reviewed for item in result.findings] == list(findings)
    assert decisions(result) == [TriageDecision.ACCEPT, TriageDecision.REJECT]


def test_run_triage_shows_each_finding_once_with_its_fields() -> None:
    terminal = Terminal(["a"])

    triage((reviewed(source=Source.RULE),), terminal)

    assert terminal.lines == ["Finding 1 of 1: a.py:7 [high/logic] Why. (source: rule)"]
    assert terminal.prompts == ["[a]ccept [r]eject [e]dit: "]


@pytest.mark.parametrize(
    ("answer", "decision"),
    [("A", TriageDecision.ACCEPT), (" r ", TriageDecision.REJECT), ("a\r", TriageDecision.ACCEPT)],
)
def test_run_triage_ignores_case_and_surrounding_whitespace(
    answer: str, decision: TriageDecision
) -> None:
    result = triage((reviewed(),), Terminal([answer]))

    assert decisions(result) == [decision]


@pytest.mark.parametrize("invalid", ["x", "", "accept"])
def test_run_triage_with_an_invalid_answer_asks_again_without_showing_the_finding_again(
    invalid: str,
) -> None:
    terminal = Terminal([invalid, "r"])

    result = triage((reviewed(),), terminal)

    assert decisions(result) == [TriageDecision.REJECT]
    assert len(terminal.prompts) == 2
    assert terminal.lines[1:] == [
        "Answer a to accept the finding, r to reject it, or e to edit it."
    ]


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_run_triage_interrupted_stops_asking_and_leaves_the_rest_undecided(
    interruption: type[BaseException],
) -> None:
    findings = (reviewed("aaaaaaaaaaaaaaaa"), reviewed("bbbbbbbbbbbbbbbb"), reviewed("c" * 16))
    terminal = Terminal(["a", interruption, "r"])

    result = triage(findings, terminal)

    assert decisions(result) == [
        TriageDecision.ACCEPT,
        TriageDecision.UNDECIDED,
        TriageDecision.UNDECIDED,
    ]
    assert len(terminal.prompts) == 2
    assert terminal.lines[-1] == "Triage stopped: 2 left undecided."
    assert [label.fingerprint for label in result.labels] == ["aaaaaaaaaaaaaaaa"]


def test_run_triage_interrupted_while_showing_a_finding_leaves_it_undecided() -> None:
    lines: list[str] = []

    def write_line(text: str) -> None:
        if text.startswith("Finding"):
            raise KeyboardInterrupt
        lines.append(text)

    result = run_triage((reviewed(),), read_line=lambda _prompt: "a", write_line=write_line)

    assert decisions(result) == [TriageDecision.UNDECIDED]
    assert lines == ["Triage stopped: 1 left undecided."]


def test_run_triage_with_no_findings_asks_nothing() -> None:
    terminal = Terminal(["a"])

    result = triage((), terminal)

    assert result == TriageResult(findings=(), labels=())
    assert terminal.prompts == []
    assert terminal.lines == []


def test_run_triage_labels_each_decision_with_its_severity_unchanged() -> None:
    findings = (
        reviewed("aaaaaaaaaaaaaaaa", source=Source.RULE),
        reviewed("bbbbbbbbbbbbbbbb"),
    )

    result = triage(findings, Terminal(["r", "a"]))

    assert result.labels == (
        Label(
            "aaaaaaaaaaaaaaaa",
            TriageDecision.REJECT,
            Severity.HIGH,
            Severity.HIGH,
            False,
            Source.RULE,
        ),
        Label(
            "bbbbbbbbbbbbbbbb",
            TriageDecision.ACCEPT,
            Severity.HIGH,
            Severity.HIGH,
            False,
            Source.AI,
        ),
    )


def test_run_triage_shows_control_characters_escaped_on_one_line() -> None:
    hostile = "x\x1b[31m\ny"
    terminal = Terminal(["a"])

    triage((reviewed(file=f"{hostile}.py", rationale=hostile),), terminal)

    [shown] = terminal.lines
    assert shown == r"Finding 1 of 1: x\x1b[31m\ny.py:7 [high/logic] x\x1b[31m\ny (source: ai)"


def test_run_triage_redacts_a_secret_in_the_path_it_shows() -> None:
    terminal = Terminal(["a"])

    triage((reviewed(file=f"keys/{AWS_KEY}.py"),), terminal)

    assert AWS_KEY not in terminal.lines[0]
    assert "keys/[REDACTED].py:7" in terminal.lines[0]


def test_run_triage_with_a_second_ctrl_c_on_the_stop_notice_keeps_the_decisions() -> None:
    class InterruptedTwice(Terminal):
        @override
        def write_line(self, text: str) -> None:
            if text.startswith("Triage stopped"):
                raise KeyboardInterrupt
            super().write_line(text)

    findings = (reviewed("aaaaaaaaaaaaaaaa"), reviewed("bbbbbbbbbbbbbbbb"))

    result = triage(findings, InterruptedTwice(["a", KeyboardInterrupt]))

    assert decisions(result) == [TriageDecision.ACCEPT, TriageDecision.UNDECIDED]


def test_run_triage_edit_replaces_severity_and_rationale_and_keeps_the_original_text() -> None:
    finding = reviewed()

    result = triage((finding,), Terminal(["e", "low", "Better text."]))

    [edited] = result.findings
    assert edited.decision is TriageDecision.EDIT
    assert edited.reviewed.finding.severity is Severity.LOW
    assert edited.reviewed.finding.rationale == "Better text."
    assert edited.original_rationale == "Why."
    assert result.labels == (
        Label(
            "0123456789abcdef", TriageDecision.EDIT, Severity.HIGH, Severity.LOW, True, Source.AI
        ),
    )


def test_run_triage_edit_asks_for_severity_then_rationale() -> None:
    terminal = Terminal(["e", "", ""])

    triage((reviewed(),), terminal)

    assert terminal.prompts == [
        "[a]ccept [r]eject [e]dit: ",
        "Severity (low, medium, high, critical; Enter keeps high): ",
        "Rationale (Enter keeps it): ",
    ]


@pytest.mark.parametrize("kept", ["", "   "])
def test_run_triage_edit_with_empty_answers_keeps_the_values_but_is_still_an_edit(
    kept: str,
) -> None:
    finding = reviewed()

    result = triage((finding,), Terminal(["e", kept, kept]))

    [edited] = result.findings
    assert edited == TriagedFinding(finding, TriageDecision.EDIT, original_rationale=None)
    assert result.labels == (
        Label(
            "0123456789abcdef", TriageDecision.EDIT, Severity.HIGH, Severity.HIGH, False, Source.AI
        ),
    )


def test_run_triage_edit_with_the_same_text_does_not_count_as_a_changed_rationale() -> None:
    result = triage((reviewed(),), Terminal(["e", "", " Why. "]))

    assert result.findings[0].original_rationale is None
    assert result.labels[0].rationale_changed is False


def test_run_triage_edit_with_only_new_text_keeps_the_severity() -> None:
    result = triage((reviewed(),), Terminal(["e", "", "New."]))

    [edited] = result.findings
    assert edited.reviewed.finding.severity is Severity.HIGH
    assert edited.reviewed.finding.rationale == "New."
    assert result.labels[0].severity_after is Severity.HIGH
    assert result.labels[0].rationale_changed is True


def test_run_triage_edit_with_an_unknown_severity_asks_again_then_goes_on() -> None:
    terminal = Terminal(["e", "urgent", " Medium ", "New."])

    result = triage((reviewed(),), terminal)

    assert result.findings[0].reviewed.finding.severity is Severity.MEDIUM
    assert result.findings[0].reviewed.finding.rationale == "New."
    assert terminal.prompts[1] == terminal.prompts[2]
    assert terminal.lines[1:] == [
        "Answer one of low, medium, high, critical, or press Enter to keep high."
    ]


def test_run_triage_edit_never_changes_the_anchor_or_identity() -> None:
    finding = ReviewedFinding(
        reviewed().finding.model_copy(update={"rule": RuleId.SWALLOWED_EXCEPTION}),
        Source.RULE,
        RuleId.SWALLOWED_EXCEPTION,
        "0123456789abcdef",
    )

    result = triage((finding,), Terminal(["e", "critical", "New."]))

    edited = result.findings[0].reviewed
    assert edited.fingerprint == finding.fingerprint
    assert edited.source is finding.source
    assert edited.rule is finding.rule
    assert edited.finding.model_dump(exclude={"severity", "rationale"}) == (
        finding.finding.model_dump(exclude={"severity", "rationale"})
    )


def test_run_triage_edit_redacts_a_secret_in_the_new_rationale() -> None:
    result = triage((reviewed(),), Terminal(["e", "", f"Key {AWS_KEY} leaked."]))

    assert result.findings[0].reviewed.finding.rationale == "Key [REDACTED] leaked."


@pytest.mark.parametrize(
    "answers",
    [
        ["e", EOFError],
        ["e", KeyboardInterrupt],
        ["e", "low", EOFError],
        ["e", "low", KeyboardInterrupt],
    ],
    ids=["eof-at-severity", "ctrl-c-at-severity", "eof-at-rationale", "ctrl-c-at-rationale"],
)
def test_run_triage_interrupted_part_way_through_an_edit_leaves_the_finding_undecided_and_unedited(
    answers: list[str | type[BaseException]],
) -> None:
    finding = reviewed()

    result = triage((finding,), Terminal(answers))

    assert result == TriageResult(
        findings=(TriagedFinding(finding, TriageDecision.UNDECIDED),), labels=()
    )
