from collections.abc import Iterable
from typing import override

import pytest

from review_bot.findings import Category, Finding, ReviewedFinding, Severity, Source
from review_bot.report import TriageDecision
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
    assert terminal.prompts == ["[a]ccept [r]eject: "]


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
    assert terminal.lines[1:] == ["Answer a to accept the finding or r to reject it."]


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
