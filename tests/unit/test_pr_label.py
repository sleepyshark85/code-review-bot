import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from shadow_samples import K1, K2, K3, entry, report, write_report

from review_bot.errors import ConfigError
from review_bot.pr.label import MAX_REPORT_BYTES, LabellingResult, run_labelling

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
VERDICT_PROMPT = "[a]ccept [r]eject [s]kip: "
REASON_PROMPT = "[w]rong [n]ot worth it [l]ine wrong [d]uplicate: "


class Terminal:
    """Answers prompts from a script, recording every prompt and line written."""

    def __init__(self, *answers: str | BaseException) -> None:
        self._answers: Iterator[str | BaseException] = iter(answers)
        self.prompts: list[str] = []
        self.lines: list[str] = []

    def read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        answer = next(self._answers, EOFError())
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def write_line(self, text: str) -> None:
        self.lines.append(text)


def label(reports: Path, labels: Path, terminal: Terminal) -> LabellingResult:
    return run_labelling(
        reports,
        labels,
        read_line=terminal.read_line,
        write_line=terminal.write_line,
        clock=lambda: NOW,
    )


def records(labels: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in labels.read_text(encoding="ascii").splitlines() if line]


@pytest.fixture
def reports(tmp_path: Path) -> Path:
    directory = tmp_path / "reports"
    directory.mkdir()
    return directory


@pytest.fixture
def labels(tmp_path: Path) -> Path:
    return tmp_path / "labels.jsonl"


# --- run_labelling: asking and appending --------------------------------------------------------


def test_run_labelling_appends_an_accept_before_asking_the_next_question(
    reports: Path, labels: Path
) -> None:
    write_report(reports, "one", report(entry(K1), entry(K2), run_id="run-1"))
    prompts: list[str] = []
    on_second_prompt: list[dict[str, Any]] = []

    def read_line(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 2:
            on_second_prompt.extend(records(labels))
        return "a"

    result = run_labelling(
        reports, labels, read_line=read_line, write_line=lambda _: None, clock=lambda: NOW
    )

    assert [record["label"] for record in on_second_prompt] == ["accept"]
    assert result == LabellingResult(judged=2, skipped=0, remaining=0, files_skipped=0)
    first = records(labels)[0]
    assert first == {
        "schema": "feedback/v1",
        "channel": "shadow",
        "repo": "owner/repo",
        "pr": 7,
        "fingerprint": "f" * 16,
        "file": "src/app.py",
        "rule_part": "ai:logic",
        "anchor": "b" * 16,
        "index": 0,
        "source": "ai",
        "severity": "medium",
        "label": "accept",
        "reason": None,
        "run_id": "run-1",
        "model": "claude-x",
        "prompt_version": "p1-v1",
        "recorded_at": "2026-11-08T12:00:00+00:00",
    }


@pytest.mark.parametrize(
    ("answer", "reason"),
    [("w", "wrong"), ("n", "not_worth_it"), ("l", "wrong_line"), (" D ", "duplicate")],
)
def test_run_labelling_reject_asks_for_a_reason_and_records_it(
    reports: Path, labels: Path, answer: str, reason: str
) -> None:
    write_report(reports, "one", report(entry(K1)))
    terminal = Terminal("r", answer)

    label(reports, labels, terminal)

    assert terminal.prompts == [VERDICT_PROMPT, REASON_PROMPT]
    assert [(r["label"], r["reason"]) for r in records(labels)] == [("reject", reason)]


def test_run_labelling_asks_again_after_any_other_answer(reports: Path, labels: Path) -> None:
    write_report(reports, "one", report(entry(K1)))
    terminal = Terminal("x", "", "r", "q", "w")

    label(reports, labels, terminal)

    assert terminal.prompts == [VERDICT_PROMPT] * 3 + [REASON_PROMPT] * 2
    assert len(terminal.lines) == 4  # the description, then one hint per wrong answer
    assert [r["reason"] for r in records(labels)] == ["wrong"]


def test_run_labelling_skip_writes_nothing_and_asks_again_next_session(
    reports: Path, labels: Path
) -> None:
    write_report(reports, "one", report(entry(K1), entry(K2)))

    first = label(reports, labels, Terminal("s", "a"))
    second = label(reports, labels, Terminal("a"))

    assert first == LabellingResult(judged=1, skipped=1, remaining=0, files_skipped=0)
    assert second == LabellingResult(judged=1, skipped=0, remaining=0, files_skipped=0)
    assert [r["rule_part"] for r in records(labels)] == ["swallowed-exception", "ai:logic"]


@pytest.mark.parametrize("stop", [EOFError(), KeyboardInterrupt()])
def test_run_labelling_stopped_part_way_keeps_what_was_appended(
    reports: Path, labels: Path, stop: BaseException
) -> None:
    write_report(reports, "one", report(entry(K1), entry(K2), entry(K3)))
    terminal = Terminal("a", "r", stop)

    result = label(reports, labels, terminal)

    assert result == LabellingResult(judged=1, skipped=0, remaining=2, files_skipped=0)
    assert len(records(labels)) == 1
    assert terminal.lines[-2:] == ["", "Labelling stopped."]


def test_run_labelling_shows_each_comment_on_one_line_with_control_characters_escaped(
    reports: Path, labels: Path
) -> None:
    write_report(reports, "one", report(entry(K1, line=4, body="bad\x1b[31m\nnext")))
    terminal = Terminal("a")

    label(reports, labels, terminal)

    assert terminal.lines[0] == (
        r"Would-be comment 1 of 1: owner/repo#7 src/app.py:4 [medium] source ai, rule "
        r"swallowed-exception, in 1 report: bad\x1b[31m\nnext"
    )


def test_run_labelling_with_nothing_to_ask_creates_no_labels_file(
    reports: Path, labels: Path
) -> None:
    result = label(reports, labels, Terminal())

    assert result == LabellingResult(judged=0, skipped=0, remaining=0, files_skipped=0)
    assert not labels.exists()


# --- run_labelling: the labels file -------------------------------------------------------------


def test_run_labelling_does_not_ask_about_units_already_labelled(
    reports: Path, labels: Path
) -> None:
    write_report(reports, "one", report(entry(K1), entry(K2)))
    label(reports, labels, Terminal("a"))
    terminal = Terminal("a")

    label(reports, labels, terminal)

    assert terminal.prompts == [VERDICT_PROMPT]
    assert [r["rule_part"] for r in records(labels)] == ["ai:logic", "swallowed-exception"]


def test_run_labelling_with_a_malformed_labels_line_names_it_and_asks_nothing(
    reports: Path, labels: Path
) -> None:
    write_report(reports, "one", report(entry(K1), entry(K2)))
    label(reports, labels, Terminal("a"))
    labels.write_text(labels.read_text(encoding="ascii") + "\n{not json\n", encoding="ascii")
    before = labels.read_bytes()
    terminal = Terminal("a")

    with pytest.raises(ConfigError, match="Line 3 of the labels file"):
        label(reports, labels, terminal)

    assert terminal.prompts == []
    assert labels.read_bytes() == before


def test_run_labelling_with_a_record_missing_a_key_names_the_key(
    reports: Path, labels: Path
) -> None:
    labels.write_text('{"schema": "feedback/v1"}\n', encoding="ascii")

    with pytest.raises(ConfigError, match=r"Line 1 .*\(channel: Field required\)"):
        label(reports, labels, Terminal())


def test_run_labelling_starts_a_new_line_after_a_last_record_with_no_line_break(
    reports: Path, labels: Path
) -> None:
    write_report(reports, "one", report(entry(K1), entry(K2)))
    label(reports, labels, Terminal("a"))
    labels.write_bytes(labels.read_bytes().rstrip(b"\n"))

    label(reports, labels, Terminal("a"))

    assert len(records(labels)) == 2


def test_run_labelling_with_an_unreadable_labels_file_raises_config_error(
    reports: Path, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match="Can't read the labels file"):
        label(reports, tmp_path, Terminal())


def test_run_labelling_with_an_unwritable_labels_file_raises_before_asking(
    reports: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_report(reports, "one", report(entry(K1)))
    terminal = Terminal("a")

    real_open = Path.open

    def refuse_appending(path: Path, mode: str = "r", **kwargs: Any) -> Any:
        if mode == "a":
            raise PermissionError(13, "Permission denied")
        return real_open(path, mode, **kwargs)

    monkeypatch.setattr(Path, "open", refuse_appending)

    with pytest.raises(ConfigError, match=r"Can't write the labels file .*Permission denied"):
        label(reports, tmp_path / "labels.jsonl", terminal)

    assert terminal.prompts == []


def test_run_labelling_with_the_labels_directory_missing_raises_before_reading_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_reports_read(*_args: object) -> None:
        raise AssertionError("the reports were read")

    monkeypatch.setattr(Path, "rglob", no_reports_read)

    with pytest.raises(ConfigError, match=r"The directory of the labels file .* doesn't exist"):
        label(tmp_path, tmp_path / "missing" / "labels.jsonl", Terminal())


def test_run_labelling_asks_about_the_same_key_on_another_pr_after_labelling_one(
    reports: Path, labels: Path
) -> None:
    write_report(reports, "seven", report(entry(K1), number=7))
    label(reports, labels, Terminal("a"))
    write_report(reports, "eight", report(entry(K1), number=8))
    terminal = Terminal("a")

    label(reports, labels, terminal)

    assert terminal.prompts == [VERDICT_PROMPT]
    assert [r["pr"] for r in records(labels)] == [7, 8]


def test_run_labelling_with_an_append_failing_raises_config_error_keeping_earlier_labels(
    reports: Path, labels: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_report(reports, "one", report(entry(K1), entry(K2)))
    real_open = Path.open

    def open_full_after_one_line(path: Path, mode: str = "r", **kwargs: Any) -> Any:
        stream = real_open(path, mode, **kwargs)
        return FullAfterOneLine(stream) if mode == "a" else stream

    monkeypatch.setattr(Path, "open", open_full_after_one_line)

    with pytest.raises(ConfigError, match=r"Can't write the labels file .*No space left"):
        label(reports, labels, Terminal("a", "a"))

    assert len(records(labels)) == 1


class FullAfterOneLine:
    """Wraps an appending stream like a disk that fills up after its first line."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lines = 0

    def write(self, text: str) -> int:
        self._lines += 1
        if self._lines > 1:
            raise OSError(28, "No space left on device")
        written: int = self._stream.write(text)
        return written

    def flush(self) -> None:
        self._stream.flush()

    def __enter__(self) -> "FullAfterOneLine":
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stream.close()


# --- run_labelling: the reports directory -------------------------------------------------------


def test_run_labelling_skips_unusable_files_with_one_line_each(reports: Path, labels: Path) -> None:
    write_report(reports, "good", report(entry(K1)))
    (reports / "broken.json").write_text("{", encoding="ascii")
    (reports / "other.json").write_text('{"schema": "report/v1"}', encoding="ascii")
    (reports / "big.json").write_bytes(b" " * (MAX_REPORT_BYTES + 1))
    (reports / "shadow.md").write_text("not a report", encoding="utf-8")
    terminal = Terminal("a")

    result = label(reports, labels, terminal)

    assert result.files_skipped == 3
    skipped = [line for line in terminal.lines if line.startswith("review-bot pr: skipped")]
    assert skipped == [
        f"review-bot pr: skipped {reports / 'big.json'}: over {MAX_REPORT_BYTES} bytes",
        f"review-bot pr: skipped {reports / 'broken.json'}: not JSON",
        f"review-bot pr: skipped {reports / 'other.json'}: not a shadow/v1 report "
        "(schema: Input should be 'shadow/v1')",
    ]
    assert result.judged == 1


def test_run_labelling_with_reports_not_a_directory_raises_config_error(
    tmp_path: Path, labels: Path
) -> None:
    with pytest.raises(ConfigError, match="isn't a directory"):
        label(tmp_path / "missing", labels, Terminal())


def test_run_labelling_ignores_a_directory_named_like_a_report(reports: Path, labels: Path) -> None:
    (reports / "artifact.json").mkdir()

    assert label(reports, labels, Terminal()).files_skipped == 0


def test_run_labelling_skips_a_report_it_cannot_read_saying_why(
    reports: Path, labels: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = write_report(reports, "locked", report(entry(K1)))
    real_open = Path.open

    def deny_the_report(path: Path, mode: str = "r", **kwargs: Any) -> Any:
        if path == locked:
            raise PermissionError(13, "Permission denied")
        return real_open(path, mode, **kwargs)

    monkeypatch.setattr(Path, "open", deny_the_report)
    terminal = Terminal()

    result = label(reports, labels, terminal)

    assert result.files_skipped == 1
    assert terminal.lines == [f"review-bot pr: skipped {locked}: can't be read (Permission denied)"]


def test_run_labelling_counts_an_artifact_downloaded_twice_once(
    reports: Path, labels: Path
) -> None:
    document = report(entry(K1, line=3), entry(K1, line=8), run_id="same")
    write_report(reports, "first", document)
    write_report(reports, "first (1)", document)
    terminal = Terminal()

    result = label(reports, labels, terminal)

    assert result.remaining == 2
    assert "in 1 report:" in terminal.lines[0]
