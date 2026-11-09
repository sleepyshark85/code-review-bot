r"""P2-S4: judge would-be comments from downloaded shadow reports, and keep `feedback/v1` labels.

Written from the spec's criteria, design.md ("How the gate is evidenced", `shadow/v1`) and ADR
0011, before the developer's code was read.

Input reports are made by the real `run` subcommand (`review_bot.pr.cli.main`, a scripted
`MockProvider`, a git repository in `tmp_path`), never hand-written. A report is edited after the
fact only where a criterion needs what no built command writes yet: `mode` `comment` (S5),
`origin` `backfill` (S13), an outcome other than `reviewed` that still has entries, an `action`
other than `create`, a fingerprint that differs for one key, or a hostile body (the reports are
untrusted downloads). `rewrite` is that one helper.

The seam: `review_bot.pr.cli.main(["label", "--reports", DIR, "--labels", FILE])`, in process,
with `sys.stdin` and `sys.stderr` replaced by P1-S12's simulated terminals (`FakeStream`). If the
seam differs, only `Labeller.label` changes.

One stdin read is one answer. The stderr text written since the previous read is what that
question shows (its "segment"), so a would-be comment is identified by its body in a segment, never
by the order of questions, which the spec doesn't fix.

The exact texts were pinned by the main session after the tests were first written (2026-11-08):
the description line, the two prompts, the end-of-session counts line (`remaining` doesn't count
would-be comments skipped this session), `Labelling stopped.`, the skip lines for bad report
files, and the two exit-2 errors.
"""

import importlib
import json
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_p0_s3_stream_failures import assert_no_traceback
from test_p1_s12_triage import EOF, Eof, FakeStream, raw_controls
from test_p2_s2_review_pr_event import GitRepo, event

from review_bot.findings import Finding
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

REPO_NAME = "octo/review-bot"  # the repo `event` writes
PR = 7
OTHER_PR = 8
APP = "src/app.py"
RULE_PART = "ai:logic"
VERDICT_PROMPT = "[a]ccept [r]eject [s]kip: "
REASON_PROMPT = "[w]rong [n]ot worth it [l]ine wrong [d]uplicate: "
STOPPED = "Labelling stopped."
NO_TERMINAL = (
    "review-bot: error: label needs a terminal: stdin and stderr must both be attached to one. "
    "Run it from an interactive shell."
)
REASONS = {"w": "wrong", "n": "not_worth_it", "l": "wrong_line", "d": "duplicate"}
RECORD_KEYS = frozenset(
    {
        "schema", "channel", "repo", "pr", "fingerprint", "file", "rule_part", "anchor", "index",
        "source", "severity", "label", "reason", "run_id", "model", "prompt_version",
        "recorded_at",
    }
)  # fmt: skip
# "10 MB" read both ways: at most 10,000,000 bytes is always read; over 10 MiB is always too big.
TEN_MB = 10_000_000
TEN_MIB = 10 * 1024 * 1024
# A built report stays this far under the cap, so entries growing a little don't trip the size
# precondition.
HEADROOM = 0.9
NO_TERMINAL_BOUND_S = 5.0
# Generous: it catches a hang or quadratic work, not a slow CI machine.
BOUND_S = 60.0

# Head lines: K1, K2 and K3 on lines 1 to 3; two identical lines (one key) on 4 and 6.
HEAD_LINES = {
    1: "k1_value = compute_one()",
    2: "k2_value = compute_two()",
    3: "k3_value = compute_three()",
    4: "dup = 1",
    5: "other = 0",
    6: "dup = 1",
}
LINE_OF = {"K1": 1, "K2": 2, "K3": 3}
HEAD = "".join(f"{text}\n" for text in HEAD_LINES.values())
# The second push inserts one line on top, so every key moves down one line.
SHIFTED_HEAD = "shift = 0\n" + HEAD


def note(tag: str) -> str:
    """A rationale whose one distinctive word, `tag`, survives Markdown rendering."""
    return f"Note {tag} here."


def finding(line: int, tag: str, severity: str = "high") -> Finding:
    return Finding(
        file=APP, line=line, severity=severity, category="logic", rationale=note(tag),
        confidence=0.9,
    )  # fmt: skip


def keys(*names: str, tag: str, severity: str = "high", shift: int = 0) -> list[Finding]:
    """One finding per key name, its rationale `<key><tag>`, such as `K1r3`."""
    return [finding(LINE_OF[n] + shift, f"{n}{tag}", severity) for n in names]


# Shadow reports, built by `run`.


@dataclass
class Reports:
    """A pull request's clone, and the `run` reports written under one `DIR`."""

    root: Path
    clone: GitRepo
    base: str
    heads: dict[str, str]
    events: Path
    runs: int = 0

    def run(
        self,
        findings: Sequence[Finding],
        where: str,
        *,
        head: str = "first",
        number: int = PR,
    ) -> Path:
        """`run` with `findings`, writing `DIR/<where>/shadow.json`; returns that path."""
        self.runs += 1
        payload = event(self.base, self.heads[head])
        payload["number"] = payload["pull_request"]["number"] = number
        event_path = self.events / f"event{self.runs}.json"
        event_path.write_text(json.dumps(payload), encoding="utf-8")
        out = self.root / where
        exit_code = importlib.import_module("review_bot.pr.cli").main(
            [
                "run", "--event", str(event_path), "--repo", str(self.clone.path),
                "--out", str(out), "--provider", "mock",
            ],
            provider=MockProvider(findings=list(findings)),
        )  # fmt: skip
        assert exit_code == 0, f"precondition: `run` exited {exit_code}"
        report = out / "shadow.json"
        document = load(report)
        assert document["outcome"] == "reviewed", document["outcome"]
        assert len(document["comments"]) == len(findings), document["comments"]
        return report


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Reports:
    monkeypatch.delenv("CI", raising=False)
    clone = GitRepo(tmp_path / "clone")
    base = clone.commit({APP: "base = 0\n"}, "base")
    clone.branch("feature")
    first = clone.commit({APP: HEAD}, "head")
    second = clone.commit({APP: SHIFTED_HEAD}, "push")
    clone.checkout("main")
    events = tmp_path / "events"
    events.mkdir()
    return Reports(tmp_path / "reports", clone, base, {"first": first, "second": second}, events)


def load(report: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(report.read_bytes())
    return document


def rewrite(report: Path, change: Callable[[dict[str, Any]], None]) -> None:
    """Edit a built report in place, for a value no command writes yet (see the docstring)."""
    document = load(report)
    change(document)
    report.write_text(json.dumps(document), encoding="utf-8")


def entry(document: dict[str, Any], line: int) -> dict[str, Any]:
    (found,) = [c for c in document["comments"] if c["line"] == line]
    return found


def finished_at(report: Path) -> datetime:
    return datetime.fromisoformat(load(report)["finished_at"])


# Labelling, under simulated terminals.

Answer = str | Eof | type[BaseException] | Callable[[], Any]


class Terminal(FakeStream):
    """P1-S12's stdin, where an answer may also be a callable run at the moment of the read."""

    def readline(self, size: int = -1) -> str:
        if self.answers and callable(self.answers[0]) and not isinstance(self.answers[0], type):
            self.answers[0] = self.answers[0]()
        return super().readline(size)


@dataclass(frozen=True, slots=True)
class LabelRun:
    exit_code: int
    stdout: str
    stderr: str
    timeline: list[tuple[str, str]]
    stdin_reads: int
    elapsed_s: float

    @property
    def segments(self) -> list[str]:
        """The stderr text written before each stdin read: what that question showed."""
        shown, current = [], []
        for stream, text in self.timeline:
            if stream == "read":
                shown.append("".join(current))
                current = []
            elif stream == "stderr":
                current.append(text)
        return shown

    def asked_about(self, tag: str) -> list[str]:
        """The segments whose question showed the rationale word `tag`."""
        return [s for s in self.segments if re.search(rf"\b{re.escape(tag)}\b", s)]


class Labeller:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, reports_dir: Path, labels: Path) -> None:
        self._monkeypatch = monkeypatch
        self.reports_dir = reports_dir
        self.labels = labels

    def label(
        self,
        answers: Sequence[Answer] = (),
        *,
        stdin_tty: bool = True,
        stderr_tty: bool = True,
        stdin: Any = None,
    ) -> LabelRun:
        timeline: list[tuple[str, str]] = []
        fake_stdin = Terminal("stdin", timeline, stdin_tty, list(answers))
        stdout = FakeStream("stdout", timeline, False)
        stderr = FakeStream("stderr", timeline, stderr_tty)
        argv = ["label", "--reports", str(self.reports_dir), "--labels", str(self.labels)]
        pr_cli = importlib.import_module("review_bot.pr.cli")
        with self._monkeypatch.context() as patch:
            patch.setattr("sys.stdin", fake_stdin if stdin is None else stdin)
            patch.setattr("sys.stdout", stdout)
            patch.setattr("sys.stderr", stderr)
            started = time.monotonic()
            exit_code = pr_cli.main(argv)
            elapsed = time.monotonic() - started
        return LabelRun(exit_code, stdout.text, stderr.text, timeline, fake_stdin.reads, elapsed)

    def records(self) -> list[dict[str, Any]]:
        if not self.labels.exists():
            return []
        raw = self.labels.read_bytes()
        assert not raw or raw.endswith(b"\n"), "the labels file doesn't end in a newline"
        return [json.loads(line) for line in raw.splitlines()]


@pytest.fixture
def labeller(monkeypatch: pytest.MonkeyPatch, reports: Reports, tmp_path: Path) -> Labeller:
    return Labeller(monkeypatch, reports.root, tmp_path / "feedback.jsonl")


DESCRIPTION = re.compile(
    r"Would-be comment (?P<i>\d+) of (?P<n>\d+): (?P<repo>\S+)#(?P<pr>\d+)"
    r" (?P<file>\S+):(?P<line>\d+) \[(?P<severity>\w+)\] source (?P<source>\w+),"
    r" rule (?P<rule>\S+), in (?P<reports>\d+) (?P<noun>reports?): (?P<body>.*)"
)


def description(segment: str) -> dict[str, str]:
    """The segment's one description line, split into its fields."""
    found = [m for line in segment.splitlines() if (m := DESCRIPTION.fullmatch(line))]
    assert len(found) == 1, f"not one description line in {segment!r}"
    fields = found[0].groupdict()
    assert fields["noun"] == ("report" if fields["reports"] == "1" else "reports"), fields
    return fields


def shows_line(segment: str, line: int) -> bool:
    fields = description(segment)
    return (fields["file"], fields["line"]) == (APP, str(line))


def shows_report_count(segment: str, count: int) -> bool:
    return description(segment)["reports"] == str(count)


def assert_counts(
    run: LabelRun, *, judged: int, skipped: int, remaining: int, files_skipped: int = 0
) -> None:
    """The last stderr line ends with the session's counts (the prompt before has no newline)."""
    expected = (
        f"review-bot pr: label: judged={judged} skipped={skipped} remaining={remaining} "
        f"files_skipped={files_skipped}"
    )
    assert run.stderr.endswith("\n"), repr(run.stderr[-80:])
    last = run.stderr.splitlines()[-1]
    assert last.endswith(expected), f"{last!r} doesn't end with {expected!r}"


def only_segment(run: LabelRun, tag: str) -> str:
    shown = run.asked_about(tag)
    assert len(shown) == 1, f"{tag} asked {len(shown)} times: {run.segments}"
    return shown[0]


# AC1: three reports for one PR, keys {K1, K2}, {K1, K2}, {K1, K3}, at any depth: three questions.


@pytest.fixture
def three_reports(reports: Reports) -> list[Path]:
    """In time order; the paths sort in the reverse order, and the oldest is newest on disk."""
    made = [
        reports.run(keys("K1", "K2", tag="r1", severity="low"), "zz/deep/er"),
        reports.run(keys("K1", "K2", tag="r2", severity="medium"), "."),
        reports.run(keys("K1", "K3", tag="r3", severity="critical"), "aa"),
    ]
    times = [finished_at(report) for report in made]
    assert times == sorted(times) and len(set(times)) == 3, f"precondition: {times}"
    newest_mtime = time.time() + 3600
    os.utime(made[0], (newest_mtime, newest_mtime))
    return made


def test_p2_s4_ac1_three_reports_ask_about_k1_k2_k3_each_once(three_reports, labeller):
    run = labeller.label(["s", "s", "s"])

    assert run.exit_code == 0, run.stderr
    assert run.stdin_reads == 3, run.segments
    assert all(segment.endswith(VERDICT_PROMPT) for segment in run.segments), run.segments
    for tag in ("K1r3", "K2r2", "K3r3"):
        only_segment(run, tag)


def test_p2_s4_ac1_each_question_shows_the_latest_reports_fields(three_reports, labeller):
    run = labeller.label(["s", "s", "s"])

    assert run.exit_code == 0, run.stderr
    expected = {"K1r3": ("K1", "critical"), "K2r2": ("K2", "medium"), "K3r3": ("K3", "critical")}
    for tag, (key, severity) in expected.items():
        fields = description(only_segment(run, tag))
        shown = {k: fields[k] for k in ("n", "repo", "pr", "file", "line", "severity", "source")}
        assert shown == {
            "n": "3", "repo": REPO_NAME, "pr": str(PR), "file": APP,
            "line": str(LINE_OF[key]), "severity": severity, "source": "ai",
        }, fields  # fmt: skip
        assert fields["rule"] == RULE_PART, fields
        assert re.search(rf"\b{tag}\b", fields["body"]), fields["body"]
    assert sorted(description(segment)["i"] for segment in run.segments) == ["1", "2", "3"]
    for older in ("K1r1", "K1r2", "K2r1"):
        assert not run.asked_about(older), f"{older} is from an older report: {run.segments}"


def test_p2_s4_ac1_each_question_shows_how_many_reports_had_it(three_reports, labeller):
    run = labeller.label(["s", "s", "s"])

    for tag, count in {"K1r3": 3, "K2r2": 2, "K3r3": 1}.items():
        segment = only_segment(run, tag)
        assert shows_report_count(segment, count), f"{tag}: {count} reports not in {segment!r}"


def test_p2_s4_ac1_a_report_downloaded_twice_counts_once(reports, labeller):
    """The same artifact in two download folders is one report: it has one `run_id`."""
    report = reports.run(keys("K1", tag="r1"), "first-download")
    copy = reports.root / "second-download" / "shadow.json"
    copy.parent.mkdir()
    copy.write_bytes(report.read_bytes())

    run = labeller.label(["s"])

    assert run.exit_code == 0, run.stderr
    assert run.stdin_reads == 1, run.segments
    assert shows_report_count(only_segment(run, "K1r1"), 1)


# AC2: one key across reports is one would-be comment; two entries in one report are two.


def test_p2_s4_ac2_a_reworded_entry_on_a_shifted_line_is_one_would_be_comment(reports, labeller):
    reports.run(keys("K1", tag="r1"), "push1")
    latest = reports.run(keys("K1", tag="r2", shift=1), "push2", head="second")

    run = labeller.label(["a"])

    assert run.exit_code == 0, run.stderr
    assert run.stdin_reads == 1, run.segments
    segment = only_segment(run, "K1r2")
    assert shows_line(segment, LINE_OF["K1"] + 1), segment
    (record,) = labeller.records()
    assert record["fingerprint"] == entry(load(latest), LINE_OF["K1"] + 1)["fingerprint"]


def test_p2_s4_ac2_a_different_fingerprint_for_one_key_is_one_would_be_comment(reports, labeller):
    older = reports.run(keys("K1", tag="r1"), "one")
    latest = reports.run(keys("K1", tag="r2"), "two")
    rewrite(older, lambda d: d["comments"][0].update(fingerprint="0123456789abcdef"))

    run = labeller.label(["a"])

    assert run.exit_code == 0, run.stderr
    assert run.stdin_reads == 1, run.segments
    (record,) = labeller.records()
    assert record["fingerprint"] == load(latest)["comments"][0]["fingerprint"]
    assert record["fingerprint"] != "0123456789abcdef"


def test_p2_s4_ac2_two_identical_lines_are_two_would_be_comments_indexed_in_line_order(
    reports, labeller
):
    """The report with one entry is older, so the newer one shows both lines."""
    reports.run([finding(6, "Dold")], "one")
    latest = load(reports.run([finding(6, "Dsix"), finding(4, "Dfour")], "two"))

    run = labeller.label(["a", "a"])

    assert run.exit_code == 0, run.stderr
    assert run.stdin_reads == 2, run.segments
    assert shows_line(only_segment(run, "Dfour"), 4)
    assert shows_line(only_segment(run, "Dsix"), 6)
    assert shows_report_count(only_segment(run, "Dfour"), 2)
    by_index = {record["index"]: record["fingerprint"] for record in labeller.records()}
    assert by_index == {0: entry(latest, 4)["fingerprint"], 1: entry(latest, 6)["fingerprint"]}
    assert len({entry(latest, 4)["anchor"], entry(latest, 6)["anchor"]}) == 1


# AC3: `a` appends one accept record at once, before the next question.


def test_p2_s4_ac3_accept_appends_one_shadow_accept_line_before_the_next_question(
    reports, labeller
):
    reports.run(keys("K1", "K2", tag="r1"), "one")
    lines_at_second_question: list[int] = []

    def second_answer() -> str:
        lines_at_second_question.append(len(labeller.records()))
        return "s"

    run = labeller.label(["a", second_answer])

    assert run.exit_code == 0, run.stderr
    assert lines_at_second_question == [1]
    (record,) = labeller.records()
    assert (record["schema"], record["channel"], record["label"]) == (
        "feedback/v1",
        "shadow",
        "accept",
    )
    assert record["reason"] is None


def test_p2_s4_ac3_a_new_session_appends_to_the_existing_file(reports, labeller):
    reports.run(keys("K1", tag="r1"), "one")
    labeller.label(["a"])
    first = labeller.labels.read_bytes()
    reports.run(keys("K2", tag="r2"), "two")

    run = labeller.label(["a"])

    assert run.exit_code == 0, run.stderr
    assert labeller.labels.read_bytes().startswith(first)
    assert len(labeller.records()) == 2


# AC4: `r` asks for a reason; the record holds its value; any other answer asks again.


@pytest.mark.parametrize("key", REASONS)
def test_p2_s4_ac4_reject_asks_for_a_reason_and_records_it(reports, labeller, key):
    reports.run(keys("K1", tag="r1"), "one")

    run = labeller.label(["r", key])

    assert run.exit_code == 0, run.stderr
    assert REASON_PROMPT in run.segments[1], run.segments
    (record,) = labeller.records()
    assert (record["label"], record["reason"]) == ("reject", REASONS[key])


def test_p2_s4_ac4_any_other_reason_answer_asks_again(reports, labeller):
    reports.run(keys("K1", tag="r1"), "one")
    seen_at_retry: list[int] = []

    def retry() -> str:
        seen_at_retry.append(len(labeller.records()))
        return "d"

    run = labeller.label(["r", "x", "", retry])

    assert run.exit_code == 0, run.stderr
    assert run.stdin_reads == 4
    assert all(REASON_PROMPT in segment for segment in run.segments[1:]), run.segments
    assert seen_at_retry == [0], "a record was written before a valid reason"
    (record,) = labeller.records()
    assert (record["label"], record["reason"]) == ("reject", "duplicate")


# AC5: `s` writes nothing, and the same would-be comment is asked about next session.


def test_p2_s4_ac5_skip_writes_nothing_and_asks_again_next_session(reports, labeller):
    reports.run(keys("K1", tag="r1"), "one")

    first = labeller.label(["s"])
    assert first.exit_code == 0, first.stderr
    assert labeller.records() == []

    second = labeller.label(["s"])
    assert second.exit_code == 0, second.stderr
    only_segment(second, "K1r1")
    assert labeller.records() == []


# AC6: a labelled (repo, PR, key, index) isn't asked again; the same key on another PR is.


def test_p2_s4_ac6_a_labelled_would_be_comment_is_not_asked_again(reports, labeller):
    reports.run(keys("K1", tag="r1"), "one")
    labeller.label(["a"])
    reports.run(keys("K1", "K2", tag="r2"), "two")

    run = labeller.label(["a"])

    assert run.exit_code == 0, run.stderr
    assert run.stdin_reads == 1, run.segments
    only_segment(run, "K2r2")
    assert not run.asked_about("K1r2")


def test_p2_s4_ac6_the_same_key_on_another_pr_is_asked_about(reports, labeller):
    reports.run(keys("K1", tag="r1"), "one")
    labeller.label(["a"])
    reports.run(keys("K1", tag="p8"), "other", number=OTHER_PR)

    run = labeller.label(["a"])

    assert run.exit_code == 0, run.stderr
    assert run.stdin_reads == 1, run.segments
    assert description(only_segment(run, "K1p8"))["pr"] == str(OTHER_PR)
    first, second = labeller.records()
    assert (first["pr"], second["pr"]) == (PR, OTHER_PR)
    assert first["anchor"] == second["anchor"]


# AC7: comment-mode, unreviewed and non-create entries contribute nothing; backfill counts.


def mode_comment(document: dict[str, Any]) -> None:
    document["mode"] = "comment"


def outcome_failed(document: dict[str, Any]) -> None:
    document["outcome"], document["reason"] = "failed", "auth"


def k2_kept(document: dict[str, Any]) -> None:
    entry(document, LINE_OF["K2"])["action"] = "keep"


def k2_updated(document: dict[str, Any]) -> None:
    entry(document, LINE_OF["K2"])["action"] = "update"


@pytest.mark.parametrize(
    "change",
    [mode_comment, outcome_failed, k2_kept, k2_updated],
    ids=["mode_comment", "outcome_failed", "action_keep", "action_update"],
)
def test_p2_s4_ac7_excluded_reports_and_entries_contribute_nothing(reports, labeller, change):
    reports.run(keys("K1", tag="r1"), "kept")
    excluded = reports.run(keys("K1", "K2", tag="r2"), "excluded")
    rewrite(excluded, change)

    run = labeller.label(["s", "s"])

    assert run.exit_code == 0, run.stderr
    assert not run.asked_about("K2r2"), run.segments
    if change in (mode_comment, outcome_failed):
        segment = only_segment(run, "K1r1")  # not the excluded report's newer body
        assert shows_report_count(segment, 1), segment


def test_p2_s4_ac7_backfill_reports_count_like_event_reports(reports, labeller):
    reports.run(keys("K1", tag="r1"), "event")
    backfill = reports.run(keys("K1", "K2", tag="r2"), "backfill")
    rewrite(backfill, lambda d: d.update(origin="backfill"))

    run = labeller.label(["s", "s"])

    assert run.exit_code == 0, run.stderr
    assert run.stdin_reads == 2, run.segments
    assert shows_report_count(only_segment(run, "K1r2"), 2)
    only_segment(run, "K2r2")


# AC8: end of input or Ctrl+C keeps what was written, exits 0, and shows the counts.


def assert_stopped(run: LabelRun) -> None:
    """`Labelling stopped.` ends a line; it follows the prompt, as a fake stdin echoes nothing."""
    assert any(line.endswith(STOPPED) for line in run.stderr.splitlines()), run.stderr


@pytest.mark.parametrize("stop", [EOF, KeyboardInterrupt], ids=["end_of_input", "ctrl_c"])
def test_p2_s4_ac8_stopping_part_way_keeps_the_lines_and_exits_0(reports, labeller, stop):
    reports.run(keys("K1", "K2", "K3", tag="r1"), "one")

    run = labeller.label(["a", stop])

    assert run.exit_code == 0, run.stderr
    assert_no_traceback(run.stderr)
    assert len(labeller.records()) == 1
    assert_stopped(run)
    assert_counts(run, judged=1, skipped=0, remaining=2)


def test_p2_s4_ac8_a_finished_session_shows_its_counts(reports, labeller):
    reports.run(keys("K1", "K2", "K3", tag="r1"), "one")

    run = labeller.label(["a", "s", "r", "w"])

    assert run.exit_code == 0, run.stderr
    assert len(labeller.records()) == 2
    assert STOPPED not in run.stderr
    assert_counts(run, judged=2, skipped=1, remaining=0)


def test_p2_s4_ac8_ctrl_c_at_the_reason_prompt_writes_nothing_for_it(reports, labeller):
    reports.run(keys("K1", "K2", tag="r1"), "one")

    run = labeller.label(["a", "r", KeyboardInterrupt])

    assert run.exit_code == 0, run.stderr
    assert_no_traceback(run.stderr)
    assert [record["label"] for record in labeller.records()] == ["accept"]
    assert_stopped(run)
    assert_counts(run, judged=1, skipped=0, remaining=1)


# AC9: without a terminal on stdin or stderr, exit 2 at once, naming `label`.


def assert_no_terminal_error(run: LabelRun, labeller: Labeller) -> None:
    assert run.exit_code == 2, run.stderr
    assert run.stderr.splitlines() == [NO_TERMINAL], repr(run.stderr)
    assert run.elapsed_s < NO_TERMINAL_BOUND_S
    assert not labeller.labels.exists()


@pytest.mark.parametrize(
    ("stdin_tty", "stderr_tty"),
    [(False, True), (True, False), (False, False)],
    ids=["stdin_not_a_terminal", "stderr_not_a_terminal", "neither"],
)
def test_p2_s4_ac9_without_a_terminal_exits_2_naming_label(
    reports, labeller, stdin_tty, stderr_tty
):
    reports.run(keys("K1", tag="r1"), "one")

    run = labeller.label(["a"], stdin_tty=stdin_tty, stderr_tty=stderr_tty)

    assert_no_terminal_error(run, labeller)
    assert run.stdin_reads == 0


def test_p2_s4_ac9_stdin_from_the_null_device_exits_2(reports, labeller):
    """On Windows `NUL` says it's a terminal through `isatty`, so this needs P1-S12's check."""
    reports.run(keys("K1", tag="r1"), "one")

    with Path(os.devnull).open(encoding="utf-8") as null:
        run = labeller.label(stdin=null)

    assert_no_terminal_error(run, labeller)


# AC10: a record has exactly the listed keys, and no body, rationale or username.


def test_p2_s4_ac10_an_accept_record_has_exactly_the_keys_and_the_latest_entrys_values(
    reports, labeller
):
    reports.run(keys("K1", tag="r1", severity="low"), "one")
    latest_path = reports.run(keys("K1", tag="r2", severity="medium"), "two")
    latest = load(latest_path)
    latest_entry = latest["comments"][0]
    before = datetime.now().astimezone()

    run = labeller.label(["a"])

    assert run.exit_code == 0, run.stderr
    (record,) = labeller.records()
    assert set(record) == RECORD_KEYS, sorted(record)
    expected = {
        "schema": "feedback/v1",
        "channel": "shadow",
        "repo": REPO_NAME,
        "pr": PR,
        "fingerprint": latest_entry["fingerprint"],
        "file": APP,
        "rule_part": RULE_PART,
        "anchor": latest_entry["anchor"],
        "index": 0,
        "source": "ai",
        "severity": "medium",
        "label": "accept",
        "reason": None,
        "run_id": latest["run_id"],
        "model": latest["report"]["model"],
        "prompt_version": latest["report"]["prompt_version"],
    }
    assert {k: v for k, v in record.items() if k != "recorded_at"} == expected
    recorded_at = datetime.fromisoformat(record["recorded_at"])
    assert recorded_at.utcoffset() == timedelta(0), record["recorded_at"]
    assert before - timedelta(seconds=1) <= recorded_at <= datetime.now().astimezone()


def test_p2_s4_ac10_records_hold_no_body_or_rationale(reports, labeller):
    report = reports.run(keys("K1", "K2", tag="r1"), "one")
    bodies = [c["body"] for c in load(report)["comments"]]

    labeller.label(["a", "r", "n"])

    raw = labeller.labels.read_text(encoding="utf-8")
    for record in labeller.records():
        assert set(record) == RECORD_KEYS, sorted(record)
    for word in ("K1r1", "K2r1", "Note", "thumbs"):
        assert word not in raw, f"{word!r} is in the labels file"
    assert not any(body in raw for body in bodies)
    assert {record["reason"] for record in labeller.records()} == {None, "not_worth_it"}


# AC11: an unreadable, foreign or oversized report is skipped with one line naming it.


def write_report(reports: Reports, where: str, content: bytes) -> Path:
    path = reports.root / where / "shadow.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def oversized(valid: bytes) -> bytes:
    """A valid report, padded with JSON whitespace to just over 10 MiB."""
    return valid.rstrip(b"\n") + b" " * (TEN_MIB + 1 - len(valid.rstrip(b"\n")))


BAD_FILE_PROBLEMS = {
    "notjson": "not JSON",
    "otherschema": "not a shadow/v1 report (",
    "oversize": "over 10000000 bytes",
    "nested": "not JSON",
    "binary": "not JSON",
    "nocomments": "not a shadow/v1 report (",
}


@pytest.fixture
def bad_files(reports: Reports) -> dict[str, Path]:
    good = reports.run(keys("K1", tag="r1"), "good")
    valid = good.read_bytes()
    other_schema = json.dumps(load(good) | {"schema": "shadow/v2"}).encode()
    oversize_valid = load(reports.run(keys("K2", tag="big"), "oversize")) | {}
    return {
        "notjson": write_report(reports, "notjson", b"{not json"),
        "otherschema": write_report(reports, "otherschema", other_schema),
        "oversize": write_report(
            reports, "oversize", oversized(json.dumps(oversize_valid).encode())
        ),
        "nested": write_report(reports, "nested/deeper", b"[" * 200_000 + b"]" * 200_000),
        "binary": write_report(reports, "binary", b"\xff\xfe\x00" + valid[:100]),
        "nocomments": write_report(
            reports, "nocomments", json.dumps(load(good) | {"comments": "x"}).encode()
        ),
    }


def test_p2_s4_ac11_each_bad_file_is_skipped_with_one_line_naming_it(bad_files, labeller):
    started = time.monotonic()
    run = labeller.label(["a"])
    elapsed = time.monotonic() - started

    assert run.exit_code == 0, run.stderr
    assert_no_traceback(run.stderr)
    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    lines = run.stderr.splitlines()
    for name, path in bad_files.items():
        naming = [line for line in lines if name in line]
        assert len(naming) == 1, f"{name}: {naming}"
        prefix = f"review-bot pr: skipped {path}: "
        assert naming[0].startswith(prefix + BAD_FILE_PROBLEMS[name]), naming[0]
    assert not run.asked_about("Kbig"), "the oversized report was read"
    only_segment(run, "K1r1")
    assert len(labeller.records()) == 1


def test_p2_s4_ac11_the_count_of_skipped_files_is_shown(bad_files, labeller):
    run = labeller.label(["s"])

    assert_counts(run, judged=0, skipped=1, remaining=0, files_skipped=len(bad_files))


def test_p2_s4_ac11_a_report_of_exactly_10_000_000_bytes_is_read(reports, labeller):
    """The boundary: a report of exactly 10 MB, in the smaller (decimal) reading, is read."""
    good = reports.run(keys("K1", tag="edge"), "edge")
    raw = good.read_bytes().rstrip(b"\n")
    good.write_bytes(raw + b" " * (TEN_MB - len(raw)))

    run = labeller.label(["s"])

    assert run.exit_code == 0, run.stderr
    only_segment(run, "K1edge")


# AC12: a malformed line in the labels file exits 2 naming its line number, and changes nothing.


@pytest.mark.parametrize(
    "bad_line",
    [b"{not json", b'{"schema": "feedback/v1"}', b"[]"],
    ids=["not_json", "missing_keys", "not_an_object"],
)
def test_p2_s4_ac12_a_malformed_labels_line_exits_2_naming_it(reports, labeller, bad_line):
    reports.run(keys("K1", tag="r1"), "one")
    reports.run(keys("K2", tag="r2"), "two")
    labeller.label(["a", EOF])
    assert len(labeller.records()) == 1, "precondition: one valid record"
    labeller.labels.write_bytes(labeller.labels.read_bytes() + bad_line + b"\n")
    before = labeller.labels.read_bytes()

    run = labeller.label(["a", "a"])

    assert run.exit_code == 2, run.stderr
    assert run.stdin_reads == 0
    error = (
        rf"review-bot: error: Line 2 of the labels file --labels {re.escape(str(labeller.labels))}"
        r" isn't a feedback/v1 record \(.+\)\. Fix or remove that line, then run label again\."
    )
    assert re.fullmatch(error, run.stderr.splitlines()[-1]), run.stderr
    assert VERDICT_PROMPT not in run.stderr
    assert labeller.labels.read_bytes() == before
    assert_no_traceback(run.stderr)


# AC13: control characters in a body are neutralised before they are shown.

HOSTILE = "\x1b[31m red\nnext"


def test_p2_s4_ac13_a_hostile_rationale_shows_no_raw_control_characters(reports, labeller):
    reports.run([finding(1, f"Hstart {HOSTILE} Hend")], "one")

    run = labeller.label(["a"])

    assert run.exit_code == 0, run.stderr
    assert not raw_controls(run.stderr), raw_controls(run.stderr)
    assert "Hend" in description(only_segment(run, "Hstart"))["body"]


@pytest.mark.parametrize(
    "hostile",
    [HOSTILE, "\x1b]0;title\x07", "\x9b2J", "\rover", "\u2028sep", "\x00nul"],
    ids=["csi_newline", "osc_bel", "c1_csi", "carriage_return", "line_separator", "nul"],
)
def test_p2_s4_ac13_limits_a_tampered_body_shows_no_raw_control_characters(
    reports, labeller, hostile
):
    """Reports are untrusted downloads: the raw body itself holds the characters."""
    report = reports.run(keys("K1", tag="r1"), "one")
    rewrite(report, lambda d: d["comments"][0].update(body=f"Bstart {hostile} Bend"))

    run = labeller.label(["a"])

    assert run.exit_code == 0, run.stderr
    assert not raw_controls(run.stderr), raw_controls(run.stderr)
    body = description(only_segment(run, "Bstart"))["body"]
    assert body.startswith("Bstart ") and body.endswith(" Bend"), body


def test_p2_s4_ac13_limits_a_tampered_path_shows_no_raw_control_characters(reports, labeller):
    """A hostile `file` is either neutralised or the report is skipped; it never reaches the TTY."""
    report = reports.run(keys("K1", tag="r1"), "one")
    rewrite(report, lambda d: d["comments"][0].update(file=f"src/a{HOSTILE}.py"))

    run = labeller.label(["s"])

    assert run.exit_code == 0, run.stderr
    assert not raw_controls(run.stderr), raw_controls(run.stderr)


# Limits: a report is read in time linear in its size.


def shortened(comment: dict[str, Any]) -> dict[str, Any]:
    """`comment` with its body's middle paragraphs cut: heading, rationale and marker stay.

    20,000 real entries are about 10.5 MB since P3-S4 added `evidence`; this keeps them under
    the cap with headroom, so the limit tested stays the entry count, not the bytes.
    """
    paragraphs = comment["body"].split("\n\n")
    return {**comment, "body": "\n\n".join([*paragraphs[:2], paragraphs[-1]])}


def test_p2_s4_limits_twenty_thousand_entries_for_one_key_are_read_within_the_bound(
    reports, labeller
):
    report = reports.run(keys("K1", tag="r1"), "one")
    rewrite(report, lambda d: d.update(comments=[shortened(d["comments"][0])] * 20_000))
    assert report.stat().st_size <= HEADROOM * TEN_MB, "precondition: the report is under the cap"

    started = time.monotonic()
    run = labeller.label([EOF])
    elapsed = time.monotonic() - started

    assert run.exit_code == 0, run.stderr
    assert_no_traceback(run.stderr)
    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    assert_counts(run, judged=0, skipped=0, remaining=20_000)
