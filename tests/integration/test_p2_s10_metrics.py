r"""P2-S10: one offline command turns downloaded reports and labels into the gate's numbers.

Written from the spec's criteria, design.md ("How the gate is evidenced") and ADRs 0010 and 0011
while the developer built the slice, before any of its code was read.

Inputs are made by the real commands wherever one can make them:

- reports by `run` (`review_bot.pr.cli.main`, a scripted `MockProvider` with token usage, a git
  repository in `tmp_path`), in shadow mode, or in comment mode against the developer's
  `FakeGitHubClient`;
- shadow labels by P2-S4's `label`, driven by `Oracle`, which answers each question from the PR
  its description names, so the verdicts don't depend on the order of the questions;
- reaction and resolution labels by P2-S8's `collect`, on the same `FakeGitHubClient` the
  comment-mode `run` posted to, so their fingerprints are the report's own.

A built file is edited (`rewrite`) only where a criterion needs a value no command writes: exact
timestamps (AC2, AC11), `origin` `backfill` (AC1, AC2), a second judgement of one unit with a
chosen `recorded_at` (AC8), and malformed or oversized files (AC12, Limits).

The seam, assumed until the developer confirms it: `main(["metrics", "--reports", DIR,
"--labels", FILE, FILE, ..., *flags])` in process, stdout and stderr read with `capsys`. Only
`metrics()` knows the argv; only the "metrics/v1 reading" section knows the JSON's key names.
"""

import importlib
import json
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_p1_s4_audit_log import aws_key
from test_p2_s2_review_pr_event import UPDATED_AT, GitRepo, event
from test_p2_s4_label_shadow_findings import DESCRIPTION, REASON_PROMPT, Labeller
from test_p2_s8_reactions_as_labels import ClosedPr, collect

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.providers.base import TokenUsage
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

REPO_NAME = "octo/review-bot"  # the repo `event` writes
PR, OTHER_PR, THIRD_PR = 7, 8, 9
APP = "src/app.py"
USAGE_EXIT = 2
EVENT_AT = datetime.fromisoformat(UPDATED_AT)
# Generous: it catches a hang or quadratic work, not a slow CI machine.
BOUND_S = 60.0
TEN_MIB = 10 * 1024 * 1024
PRICES = ("--price-input-usd-per-mtok", "15", "--price-output-usd-per-mtok", "75")

# Twenty distinct head lines, so every finding has its own anchor and is its own unit.
HEAD_LINES = [f"value_{i:02d} = compute({i})" for i in range(1, 21)]
HEAD = "".join(f"{text}\n" for text in HEAD_LINES)
SECRET_HEAD = HEAD + f'key = "{aws_key(seed=5)}"\n'


def finding(line: int) -> Finding:
    return Finding(
        file=APP, line=line, severity="high", category="logic", rationale=f"Note L{line} here.",
        confidence=0.9,
    )  # fmt: skip


def load(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_bytes())
    return document


def rewrite(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    """Edit a built file in place, for a value no command writes (see the docstring)."""
    document = load(path)
    change(document)
    path.write_text(json.dumps(document), encoding="utf-8")


def utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def timed(*, event_at: datetime, review_s: float, total_s: float) -> Callable[[dict], None]:
    """Set a report's times: `finished_at - event_at` is `total_s`, `- started_at` `review_s`."""
    finished = event_at + timedelta(seconds=total_s)

    def change(document: dict[str, Any]) -> None:
        document["event_at"] = utc(event_at)
        document["started_at"] = utc(finished - timedelta(seconds=review_s))
        document["finished_at"] = utc(finished)

    return change


# Reports, built by `run`.


@dataclass
class Builder:
    """A clone with a base and head commits, and the `run` reports written under one `DIR`."""

    root: Path
    clone: GitRepo
    base: str
    heads: dict[str, str]
    events: Path
    runs: int = 0

    def run(  # noqa: PLR0913 - keyword-only, one per input a run can vary
        self,
        lines: Sequence[int],
        where: str,
        *,
        number: int = PR,
        head: str = "first",
        usage: tuple[int, int] = (0, 0),
        mode: str = "shadow",
        github: Any = None,
        provider: Any = None,
        flags: Sequence[str] = (),
    ) -> Path:
        """`run` with one finding per line, writing `DIR/<where>/shadow.json`; returns it."""
        self.runs += 1
        payload = event(self.base, self.heads[head])
        payload["number"] = payload["pull_request"]["number"] = number
        event_path = self.events / f"event{self.runs}.json"
        event_path.write_text(json.dumps(payload), encoding="utf-8")
        out = self.root / where
        if provider is None:
            provider = MockProvider(
                findings=[finding(line) for line in lines], usage=TokenUsage(*usage)
            )
        extra = {} if github is None else {"github": github}
        importlib.import_module("review_bot.pr.cli").main(
            [
                "run", "--event", str(event_path), "--repo", str(self.clone.path),
                "--out", str(out), "--provider", "mock", "--mode", mode, *flags,
            ],
            provider=provider,
            **extra,
        )  # fmt: skip
        report = out / "shadow.json"
        assert report.is_file(), f"precondition: `run` wrote no report in {out}"
        return report

    def reviewed(self, lines: Sequence[int], where: str, **kwargs: Any) -> Path:
        report = self.run(lines, where, **kwargs)
        document = load(report)
        assert document["outcome"] == "reviewed", document["outcome"]
        creates = [c for c in document["comments"] if c["action"] == "create"]
        assert len(creates) == len(lines), document["comments"]
        return report


def make_builder(root: Path) -> Builder:
    clone = GitRepo(root / "clone")
    base = clone.commit({APP: "base = 0\n"}, "base")
    clone.branch("feature")
    first = clone.commit({APP: HEAD}, "head")
    secret = clone.commit({APP: SECRET_HEAD}, "secret")
    clone.checkout("main")
    events = root / "events"
    events.mkdir()
    return Builder(root / "reports", clone, base, {"first": first, "secret": secret}, events)


@pytest.fixture
def builder(tmp_path: Path) -> Builder:
    return make_builder(tmp_path)


# Shadow labels, by `label`.


@dataclass
class Oracle:
    """`label`'s terminal user: each verdict comes from a per-PR plan, whatever the order.

    A plan item is `accept`, `skip` or a reject reason. Called at each stdin read, it reads the
    simulated stderr written so far.
    """

    plans: dict[int, list[str]]
    pending: str | None = None
    letters: dict[str, str] = field(
        default_factory=lambda: {
            "wrong": "w", "not_worth_it": "n", "wrong_line": "l", "duplicate": "d",
        }
    )  # fmt: skip

    def __call__(self) -> str:
        shown: str = sys.stderr.text  # type: ignore[attr-defined] - P1-S12's FakeStream
        if shown.endswith(REASON_PROMPT):
            assert self.pending is not None, "asked for a reason after no reject"
            answer, self.pending = self.pending, None
            return answer
        matches = [m for line in shown.splitlines() if (m := DESCRIPTION.search(line))]
        assert matches, f"no would-be comment described before a verdict: {shown[-300:]!r}"
        verdict = self.plans[int(matches[-1]["pr"])].pop(0)
        if verdict in ("accept", "skip"):
            return verdict[0]
        self.pending = self.letters[verdict]
        return "r"

    def answers(self) -> list[Any]:
        return [self] * (2 * sum(len(plan) for plan in self.plans.values()))


def judge(reports_dir: Path, labels: Path, plans: dict[int, list[str]]) -> list[dict[str, Any]]:
    """Label every would-be comment under `reports_dir` by `plans`; returns the records."""
    oracle = Oracle({pr: list(plan) for pr, plan in plans.items()})
    with pytest.MonkeyPatch.context() as patch:
        labeller = Labeller(patch, reports_dir, labels)
        run = labeller.label(oracle.answers())
        assert run.exit_code == 0, run.stderr
        records = labeller.records()
    assert all(not plan for plan in oracle.plans.values()), f"plans left: {oracle.plans}"
    return records


def plan(accept: int, *rejects: str, skip: int = 0) -> list[str]:
    return ["accept"] * accept + list(rejects) + ["skip"] * skip


# The AC6 data set: 34 units over two PRs, 17 each, two reports per PR. 32 judged: 21 accept, 9
# reject for other reasons (4 wrong, 3 not worth it, 2 wrong line), 2 duplicate. Each PR has 17
# units, 16 judged and 15 non-duplicate verdicts, so the largest PR's share is 50% read any way.
GATE_PLANS = {
    PR: plan(11, "wrong", "wrong", "not_worth_it", "wrong_line", "duplicate", skip=1),
    OTHER_PR: plan(
        10, "wrong", "wrong", "not_worth_it", "not_worth_it", "wrong_line", "duplicate", skip=1
    ),
}
FIRST_HALF, SECOND_HALF = range(1, 10), range(10, 18)


@dataclass(frozen=True, slots=True)
class GateData:
    reports: Path
    labels: Path
    records: list[dict[str, Any]]


@pytest.fixture(scope="module")
def gate_data(tmp_path_factory: pytest.TempPathFactory) -> GateData:
    """Built once: four shadow reports by `run` and 32 shadow labels by `label`."""
    root = tmp_path_factory.mktemp("gate")
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("CI", raising=False)
        made = make_builder(root)
        for number in (PR, OTHER_PR):
            made.reviewed(FIRST_HALF, f"pr{number}/a", number=number)
            made.reviewed(SECOND_HALF, f"pr{number}/b", number=number)
    labels = root / "feedback.jsonl"
    records = judge(made.root, labels, GATE_PLANS)
    verdicts = sorted((r["label"], r["reason"] or "") for r in records)
    assert verdicts == sorted(
        [("accept", "")] * 21
        + [("reject", "wrong")] * 4
        + [("reject", "not_worth_it")] * 3
        + [("reject", "wrong_line")] * 2
        + [("reject", "duplicate")] * 2
    ), f"precondition: {verdicts}"
    return GateData(made.root, labels, records)


@pytest.fixture
def gate_copy(gate_data: GateData, tmp_path: Path) -> GateData:
    """A test's own copy of the AC6 data set, to add or edit files in."""
    reports = tmp_path / "reports"
    shutil.copytree(gate_data.reports, reports)
    labels = tmp_path / "feedback.jsonl"
    shutil.copyfile(gate_data.labels, labels)
    return GateData(reports, labels, gate_data.records)


def write_labels(path: Path, records: Sequence[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


# Running `metrics`: the only code that knows its argv.


@dataclass(frozen=True, slots=True)
class Metrics:
    exit_code: int
    stdout: str
    stderr: str
    elapsed_s: float

    @property
    def doc(self) -> dict[str, Any]:
        assert self.exit_code == 0, self.stderr
        document: dict[str, Any] = json.loads(self.stdout)
        return document

    def stderr_lines(self, needle: str) -> list[str]:
        return [line for line in self.stderr.splitlines() if needle in line]


def metrics(
    capsys: pytest.CaptureFixture[str],
    reports: Path,
    *labels: Path,
    flags: Sequence[str] = (),
    json_format: bool = True,
) -> Metrics:
    argv = ["metrics", "--reports", str(reports)]
    if labels:
        argv += ["--labels", *map(str, labels)]
    argv += [*flags, "--format", "json" if json_format else "text"]
    pr_cli = importlib.import_module("review_bot.pr.cli")
    capsys.readouterr()
    started = time.monotonic()
    exit_code = pr_cli.main(argv)
    elapsed = time.monotonic() - started
    captured = capsys.readouterr()
    return Metrics(exit_code, captured.out, captured.err, elapsed)


# metrics/v1 reading: the only code that knows the JSON's key names (assumed; see the docstring).


def pct(value: Any) -> float | None:
    """A share as a percentage to one decimal: a fraction, or a string such as `70.0%`."""
    if value is None or value == "n/a":
        return None
    if isinstance(value, str):
        return round(float(value.rstrip("%")), 1)
    return round(float(value) * 100, 1)


def outcome_counts(doc: dict[str, Any]) -> dict[tuple[str, str, str, str | None], int]:
    """(mode, origin, outcome, reason) to its count of runs."""
    return {
        (row["mode"], row["origin"], row["outcome"], row["reason"]): row["count"]
        for row in doc["runs"]["outcomes"]
    }


def latency(doc: dict[str, Any], which: str) -> tuple[float, float]:
    """`end_to_end` or `review`: (p50, p95) in seconds."""
    spread = doc["runs"]["latency_s"][which]
    return spread["p50"], spread["p95"]


def tokens(doc: dict[str, Any], kind: str) -> tuple[int, float, int]:
    """`input` or `output`: (total, mean, max)."""
    spread = doc["tokens"][kind]
    return spread["total"], spread["mean"], spread["max"]


def over_budget(doc: dict[str, Any]) -> int:
    count: int = doc["tokens"]["over_budget"]
    return count


def cost(doc: dict[str, Any]) -> tuple[str, str | None] | None:
    """(total USD, USD per accepted finding) as strings in cents, or None if not priced."""
    priced = doc["cost"]
    if priced is None:
        return None
    return priced["usd"], priced["per_accepted_finding_usd"]


def precision(doc: dict[str, Any], channel: str) -> dict[str, Any]:
    """`shadow` or `comment`: accepted, rejected, duplicates, precision, wilson_low, wilson_high,
    lower_bound, judged, units, coverage, prs, largest_pr_share, conflict, resolved, splits."""
    section: dict[str, Any] = doc[f"{channel}_precision"]
    return section


def split(section: dict[str, Any], by: str) -> dict[str, dict[str, Any]]:
    """`origin`, `source`, `rule_part` or `reject_reason`: each value's numbers."""
    rows: dict[str, dict[str, Any]] = section["splits"][by]
    return rows


def orphans(doc: dict[str, Any]) -> int:
    count: int = doc["orphans"]
    return count


def assert_gate_numbers(doc: dict[str, Any]) -> None:
    """The AC6 data set's shadow numbers, unchanged."""
    shadow = precision(doc, "shadow")
    assert (shadow["accepted"], shadow["rejected"], shadow["duplicates"]) == (21, 9, 2), shadow
    assert (shadow["judged"], shadow["units"]) == (32, 34), shadow
    assert pct(shadow["precision"]) == 70.0, shadow
    assert pct(shadow["coverage"]) == 94.1, shadow


# AC1: counts per outcome and reason, split by mode and origin.


def test_p2_s10_ac1_counts_per_outcome_and_reason_split_by_mode_and_origin(builder, capsys):
    builder.reviewed([1], "shadow-1")
    builder.reviewed([2], "shadow-2", number=OTHER_PR)
    builder.reviewed([3], "comment", mode="comment", github=ClosedPr().client)
    backfilled = builder.reviewed([4], "backfill", number=THIRD_PR)
    rewrite(backfilled, lambda d: d.update(origin="backfill"))
    builder.run([], "skipped", flags=("--max-diff-bytes", "10"))
    builder.run([], "secret", head="secret")
    failing = MockProvider(error=ProviderError(ErrorCategory.AUTH, "Not logged in."))
    builder.run([], "failed", provider=failing)
    assert load(builder.root / "skipped/shadow.json")["outcome"] == "skipped", "precondition"
    assert load(builder.root / "secret/shadow.json")["outcome"] == "blocked_secret", "precondition"

    result = metrics(capsys, builder.root)

    assert outcome_counts(result.doc) == {
        ("shadow", "event", "reviewed", None): 2,
        ("comment", "event", "reviewed", None): 1,
        ("shadow", "backfill", "reviewed", None): 1,
        ("shadow", "event", "skipped", "too_large"): 1,
        ("shadow", "event", "blocked_secret", None): 1,
        ("shadow", "event", "failed", "auth"): 1,
    }


def test_p2_s10_ac1_text_names_each_outcome_reason_mode_and_origin(builder, capsys):
    builder.reviewed([1], "shadow-1")
    builder.run([], "skipped", flags=("--max-diff-bytes", "10"))
    failing = MockProvider(error=ProviderError(ErrorCategory.AUTH, "Not logged in."))
    builder.run([], "failed", provider=failing)

    result = metrics(capsys, builder.root, json_format=False)

    assert result.exit_code == 0, result.stderr
    for word in ("reviewed", "skipped", "too_large", "failed", "auth", "shadow", "event"):
        assert word in result.stdout, f"{word} not in {result.stdout}"


# AC2: end-to-end latency p50 90 s, p95 400 s, by nearest rank over reviewed event runs only.


def test_p2_s10_ac2_latency_is_nearest_rank_over_reviewed_event_runs_only(builder, capsys):
    for i, (review_s, total_s) in enumerate([(10, 60), (20, 90), (30, 120), (40, 400)]):
        report = builder.reviewed([i + 1], f"event-{i}")
        rewrite(report, timed(event_at=EVENT_AT, review_s=review_s, total_s=total_s))
    # A backfilled run's event_at is its merge time: far off, so it must not count end to end.
    # Its review time, 15 s, leaves the review p50 and p95 the same whether it counts or not.
    backfilled = builder.reviewed([5], "backfill", number=OTHER_PR)
    rewrite(backfilled, timed(event_at=EVENT_AT - timedelta(days=30), review_s=15, total_s=1e6))
    rewrite(backfilled, lambda d: d.update(origin="backfill"))
    skipped = builder.run([], "skipped", flags=("--max-diff-bytes", "10"))
    rewrite(skipped, timed(event_at=EVENT_AT, review_s=5000, total_s=9000))

    doc = metrics(capsys, builder.root).doc

    assert latency(doc, "end_to_end") == (90, 400)
    assert latency(doc, "review") == (20, 40)


# AC3: one run_id in two files counts once.


def test_p2_s10_ac3_a_report_downloaded_twice_counts_once(gate_copy, capsys):
    for report in sorted(gate_copy.reports.rglob("shadow.json")):
        copy = gate_copy.reports / "second-download" / report.relative_to(gate_copy.reports)
        copy.parent.mkdir(parents=True)
        copy.write_bytes(report.read_bytes())

    doc = metrics(capsys, gate_copy.reports, gate_copy.labels).doc

    assert outcome_counts(doc) == {("shadow", "event", "reviewed", None): 4}
    assert_gate_numbers(doc)


def test_p2_s10_ac3_a_duplicated_run_adds_no_tokens(builder, capsys):
    report = builder.reviewed([1], "one", usage=(1000, 100))
    copy = builder.root / "again" / "deeper" / "shadow.json"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(report.read_bytes())

    doc = metrics(capsys, builder.root).doc

    assert tokens(doc, "input") == (1000, 1000, 1000)
    assert tokens(doc, "output") == (100, 100, 100)


# AC4 and AC5: tokens and cost, on four reviewed runs and 9 accepted shadow labels.


@pytest.fixture
def usage_data(builder: Builder) -> tuple[Path, Path]:
    """Inputs 400k, 300k, 200k, 100k (1M); outputs 40k to 10k (100k); 35k is the budget."""
    budget = ("--max-output-tokens", "35000")
    builder.reviewed(range(1, 11), "a", usage=(400_000, 40_000), flags=budget)
    builder.reviewed([], "b", number=OTHER_PR, usage=(300_000, 30_000), flags=budget)
    builder.reviewed([], "c", number=THIRD_PR, usage=(200_000, 20_000), flags=budget)
    builder.reviewed([], "d", number=10, usage=(100_000, 10_000), flags=budget)
    labels = builder.root.parent / "feedback.jsonl"
    judge(builder.root, labels, {PR: plan(9, "wrong")})
    return builder.root, labels


def test_p2_s10_ac4_tokens_total_mean_max_and_over_budget_count(usage_data, capsys):
    doc = metrics(capsys, *usage_data).doc

    assert tokens(doc, "input") == (1_000_000, 250_000, 400_000)
    assert tokens(doc, "output") == (100_000, 25_000, 40_000)
    assert over_budget(doc) == 1


def test_p2_s10_ac5_cost_is_22_50_usd_and_2_50_per_accepted_finding(usage_data, capsys):
    doc = metrics(capsys, *usage_data, flags=PRICES).doc

    assert cost(doc) == ("22.50", "2.50")


def test_p2_s10_ac5_text_shows_the_cost_in_usd(usage_data, capsys):
    result = metrics(capsys, *usage_data, flags=PRICES, json_format=False)

    assert result.exit_code == 0, result.stderr
    assert "22.50" in result.stdout, result.stdout
    assert "2.50" in result.stdout, result.stdout
    assert "USD" in result.stdout, result.stdout


def test_p2_s10_ac5_cost_rounds_half_even_to_cents(usage_data, capsys):
    """1M input tokens at 0.125 USD per million is 0.125 USD: half-even gives 0.12, not 0.13."""
    flags = ("--price-input-usd-per-mtok", "0.125", "--price-output-usd-per-mtok", "0")

    doc = metrics(capsys, *usage_data, flags=flags).doc

    assert cost(doc) == ("0.12", "0.01")


def test_p2_s10_ac5_no_prices_shows_not_priced(usage_data, capsys):
    result = metrics(capsys, *usage_data, json_format=False)

    assert result.exit_code == 0, result.stderr
    assert "not priced" in result.stdout, result.stdout
    assert cost(metrics(capsys, *usage_data).doc) is None


@pytest.mark.parametrize(
    ("flags", "named"),
    [
        (("--price-input-usd-per-mtok", "15"), "--price-output-usd-per-mtok"),
        (("--price-output-usd-per-mtok", "75"), "--price-input-usd-per-mtok"),
        (("--price-input-usd-per-mtok=-1", "--price-output-usd-per-mtok", "75"),
         "--price-input-usd-per-mtok"),
        (("--price-input-usd-per-mtok", "15", "--price-output-usd-per-mtok=-0.01"),
         "--price-output-usd-per-mtok"),
        (("--price-input-usd-per-mtok", "abc", "--price-output-usd-per-mtok", "75"),
         "--price-input-usd-per-mtok"),
        (("--price-input-usd-per-mtok", "15", "--price-output-usd-per-mtok", ""),
         "--price-output-usd-per-mtok"),
        (("--price-input-usd-per-mtok", "nan", "--price-output-usd-per-mtok", "75"),
         "--price-input-usd-per-mtok"),
        (("--price-input-usd-per-mtok", "15", "--price-output-usd-per-mtok", "inf"),
         "--price-output-usd-per-mtok"),
    ],
    ids=["input-only", "output-only", "negative-input", "negative-output", "word", "empty",
         "nan", "infinity"],
)  # fmt: skip
def test_p2_s10_ac5_an_invalid_price_exits_2_naming_the_flag(usage_data, capsys, flags, named):
    result = metrics(capsys, *usage_data, flags=flags)

    assert result.exit_code == USAGE_EXIT, result.stderr
    assert named in result.stderr, result.stderr
    assert result.stdout == "", result.stdout
    assert "Traceback" not in result.stderr, result.stderr


# AC6: shadow precision, its interval, duplicates, the lower bound, coverage and the splits.


def test_p2_s10_ac6_precision_70_with_wilson_52_1_to_83_3(gate_data, capsys):
    shadow = precision(metrics(capsys, gate_data.reports, gate_data.labels).doc, "shadow")

    assert (shadow["accepted"], shadow["rejected"]) == (21, 9), shadow
    assert pct(shadow["precision"]) == 70.0, shadow
    assert (pct(shadow["wilson_low"]), pct(shadow["wilson_high"])) == (52.1, 83.3), shadow


def test_p2_s10_ac6_duplicates_apart_and_the_lower_bound_65_6(gate_data, capsys):
    shadow = precision(metrics(capsys, gate_data.reports, gate_data.labels).doc, "shadow")

    assert shadow["duplicates"] == 2, shadow
    assert pct(shadow["lower_bound"]) == 65.6, shadow


def test_p2_s10_ac6_coverage_94_1_of_34_units(gate_data, capsys):
    shadow = precision(metrics(capsys, gate_data.reports, gate_data.labels).doc, "shadow")

    assert (shadow["judged"], shadow["units"]) == (32, 34), shadow
    assert pct(shadow["coverage"]) == 94.1, shadow


def test_p2_s10_ac6_two_prs_and_the_largest_share_50(gate_data, capsys):
    shadow = precision(metrics(capsys, gate_data.reports, gate_data.labels).doc, "shadow")

    assert shadow["prs"] == 2, shadow
    assert pct(shadow["largest_pr_share"]) == 50.0, shadow


def test_p2_s10_ac6_splits_by_origin_source_rule_part_and_reject_reason(gate_data, capsys):
    shadow = precision(metrics(capsys, gate_data.reports, gate_data.labels).doc, "shadow")

    for by, value in (("origin", "event"), ("source", "ai"), ("rule_part", "ai:logic")):
        rows = split(shadow, by)
        assert set(rows) == {value}, rows
        assert (rows[value]["accepted"], rows[value]["rejected"]) == (21, 9), rows
    reasons = {reason: row["count"] for reason, row in split(shadow, "reject_reason").items()}
    assert reasons == {"wrong": 4, "not_worth_it": 3, "wrong_line": 2, "duplicate": 2}


def test_p2_s10_ac6_text_shows_every_gate_number(gate_data, capsys):
    result = metrics(capsys, gate_data.reports, gate_data.labels, json_format=False)

    assert result.exit_code == 0, result.stderr
    for number in ("70.0%", "52.1%", "83.3%", "65.6%", "94.1%", "50.0%"):
        assert number in result.stdout, f"{number} not in {result.stdout}"


# AC7: would-be comments in comment-mode reports never enter shadow precision or coverage.


def test_p2_s10_ac7_comment_mode_entries_add_no_shadow_units(gate_copy, capsys):
    made = make_builder(gate_copy.reports.parent / "more")
    # Same PR and lines as judged shadow units, and three new ones on another PR.
    made.reviewed(FIRST_HALF, "same", mode="comment", github=ClosedPr().client)
    made.reviewed([18, 19, 20], "new", number=THIRD_PR, mode="comment", github=ClosedPr().client)
    shutil.copytree(made.root, gate_copy.reports / "comment-mode")

    doc = metrics(capsys, gate_copy.reports, gate_copy.labels).doc

    assert_gate_numbers(doc)
    assert precision(doc, "shadow")["prs"] == 2


# AC8: the later recorded_at wins, whatever the file order.


def test_p2_s10_ac8_the_later_shadow_judgement_of_a_unit_wins(gate_copy, capsys):
    accepted = [r for r in gate_copy.records if r["label"] == "accept"]
    newest = max(datetime.fromisoformat(r["recorded_at"]) for r in gate_copy.records)
    # Later: one accept becomes `wrong`. Earlier: another accept "becomes" `wrong`, ignored.
    later = accepted[0] | {
        "label": "reject", "reason": "wrong", "recorded_at": utc(newest + timedelta(days=1)),
    }  # fmt: skip
    earlier = accepted[1] | {"label": "reject", "reason": "wrong", "recorded_at": utc(EVENT_AT)}
    rejudged = write_labels(gate_copy.reports.parent / "rejudged.jsonl", [later, earlier])

    for order in ((rejudged, gate_copy.labels), (gate_copy.labels, rejudged)):
        shadow = precision(metrics(capsys, gate_copy.reports, *order).doc, "shadow")
        assert (shadow["accepted"], shadow["rejected"]) == (20, 10), (order, shadow)
        assert pct(shadow["precision"]) == 66.7, (order, shadow)
        assert (shadow["judged"], shadow["units"]) == (32, 34), (order, shadow)


def test_p2_s10_ac8_the_later_reaction_label_wins(builder, tmp_path, capsys):
    github = ClosedPr()
    builder.reviewed([1], "comment", mode="comment", github=github.client)
    (posted,) = github.client.comments
    github.react(posted.comment_id, "+1")
    first = collect(tmp_path, capsys, github.client, name="first")
    posted.reactions.clear()
    github.react(posted.comment_id, "-1")
    second = collect(tmp_path, capsys, github.client, name="second")
    assert (first.exit_code, second.exit_code) == (0, 0), first.stderr + second.stderr
    assert [r["label"] for r in first.records + second.records] == ["accept", "reject"]
    times = [r["recorded_at"] for r in first.records + second.records]
    assert times[0] < times[1], f"precondition: {times}"

    for order in ((second.feedback, first.feedback), (first.feedback, second.feedback)):
        comment = precision(metrics(capsys, builder.root, *order).doc, "comment")
        assert (comment["accepted"], comment["rejected"]) == (0, 1), (order, comment)
        assert pct(comment["precision"]) == 0.0, (order, comment)


# AC9: reaction labels join to the comment-mode report; comment precision stands apart.


@pytest.fixture
def reacted(builder: Builder, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> list[Path]:
    """A comment-mode run posts 4 comments: one accepted, one conflict, one resolved only."""
    github = ClosedPr()
    builder.reviewed([1, 2, 3, 4], "comment", mode="comment", github=github.client)
    one, two, three, _unjudged = github.client.comments
    github.react(one.comment_id, "+1")
    github.react(two.comment_id, "+1")
    github.react(two.comment_id, "-1", "someone-else")
    github.client.resolve(three.comment_id, by="person")
    collected = collect(tmp_path, capsys, github.client)
    assert collected.exit_code == 0, collected.stderr
    assert sorted(r["label"] for r in collected.records) == ["accept", "conflict", "resolved"]
    return [collected.feedback]


def test_p2_s10_ac9_comment_precision_counts_reactions_and_their_coverage(builder, reacted, capsys):
    doc = metrics(capsys, builder.root, *reacted).doc
    comment = precision(doc, "comment")

    assert (comment["accepted"], comment["rejected"]) == (1, 0), comment
    assert pct(comment["precision"]) == 100.0, comment
    assert (comment["judged"], comment["units"]) == (1, 4), comment
    assert pct(comment["coverage"]) == 25.0, comment
    assert (comment["conflict"], comment["resolved"]) == (1, 1), comment
    assert orphans(doc) == 0


def test_p2_s10_ac9_a_reaction_label_takes_its_fields_from_the_report(builder, reacted, capsys):
    comment = precision(metrics(capsys, builder.root, *reacted).doc, "comment")

    assert set(split(comment, "source")) == {"ai"}, comment
    assert split(comment, "source")["ai"]["accepted"] == 1, comment
    assert set(split(comment, "rule_part")) == {"ai:logic"}, comment


def test_p2_s10_ac9_shadow_and_comment_precision_are_never_merged(gate_copy, reacted, capsys):
    shutil.copytree(reacted[0].parent.parent / "reports", gate_copy.reports / "comment-mode")

    doc = metrics(capsys, gate_copy.reports, gate_copy.labels, *reacted).doc

    assert_gate_numbers(doc)
    comment = precision(doc, "comment")
    assert (comment["accepted"], comment["rejected"], comment["units"]) == (1, 0, 4), comment


# AC10: no judged labels is `n/a`, not an error.


def test_p2_s10_ac10_no_labels_shows_n_a(builder, capsys):
    builder.reviewed([1, 2], "one")

    text = metrics(capsys, builder.root, json_format=False)
    doc = metrics(capsys, builder.root).doc

    assert text.exit_code == 0, text.stderr
    assert "n/a" in text.stdout, text.stdout
    assert pct(precision(doc, "shadow")["precision"]) is None
    assert pct(precision(doc, "comment")["precision"]) is None


def test_p2_s10_ac10_only_conflict_and_resolved_is_n_a(builder, tmp_path, capsys):
    github = ClosedPr()
    builder.reviewed([1, 2], "comment", mode="comment", github=github.client)
    one, two = github.client.comments
    github.react(one.comment_id, "+1")
    github.react(one.comment_id, "-1")
    github.client.resolve(two.comment_id, by="person")
    collected = collect(tmp_path, capsys, github.client)

    result = metrics(capsys, builder.root, collected.feedback)

    comment = precision(result.doc, "comment")
    assert pct(comment["precision"]) is None, comment
    assert (comment["conflict"], comment["resolved"]) == (1, 1), comment


# AC11: --since and --until keep only the runs whose event_at is in the window.


@pytest.fixture
def two_months(gate_copy: GateData) -> GateData:
    """PR 8's two reports moved to mid-October; PR 7's stay on 2026-11-08."""
    october = datetime(2026, 10, 15, 12, tzinfo=UTC)
    for report in (gate_copy.reports / f"pr{OTHER_PR}").rglob("shadow.json"):
        rewrite(report, timed(event_at=october, review_s=30, total_s=60))
    return gate_copy


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (("--since", "2026-11-01", "--until", "2026-11-30"), (11, 4, 1, 16, 17, 73.3)),
        (("--since", "2026-10-01", "--until", "2026-10-31"), (10, 5, 1, 16, 17, 66.7)),
        (("--since", "2026-11-01"), (11, 4, 1, 16, 17, 73.3)),
        (("--until", "2026-10-31"), (10, 5, 1, 16, 17, 66.7)),
    ],
    ids=["november", "october", "since-only", "until-only"],
)
def test_p2_s10_ac11_only_runs_and_units_in_the_window_count(two_months, capsys, window, expected):
    doc = metrics(capsys, two_months.reports, two_months.labels, flags=window).doc

    shadow = precision(doc, "shadow")
    numbers = (
        shadow["accepted"], shadow["rejected"], shadow["duplicates"], shadow["judged"],
        shadow["units"], pct(shadow["precision"]),
    )  # fmt: skip
    assert numbers == expected, shadow
    assert pct(shadow["coverage"]) == 94.1, shadow
    assert outcome_counts(doc) == {("shadow", "event", "reviewed", None): 2}


def test_p2_s10_ac11_an_invalid_date_exits_2(gate_data, capsys):
    result = metrics(capsys, gate_data.reports, flags=("--since", "2026-13-01"))

    assert result.exit_code == USAGE_EXIT, result.stderr
    assert "--since" in result.stderr, result.stderr


# AC12: invalid files and orphan labels: one stderr line each, out of every number.


def test_p2_s10_ac12_invalid_files_and_orphans_are_skipped_one_line_each(
    gate_copy, tmp_path, capsys
):
    bad_reports = {
        "not-json": b"{ this is not json",
        "other-schema": json.dumps({"schema": "shadow/v2"}).encode(),
        "empty": b"",
    }
    for name, content in bad_reports.items():
        (gate_copy.reports / name).mkdir()
        (gate_copy.reports / name / "shadow.json").write_bytes(content)
    bad_labels = tmp_path / "bad.jsonl"
    bad_labels.write_bytes(b"not a record\n")
    orphan_shadow = gate_copy.records[0] | {"pr": 99}
    orphan_reaction = reaction_without_report(tmp_path, capsys)
    orphan_labels = write_labels(tmp_path / "orphans.jsonl", [orphan_shadow, orphan_reaction])

    result = metrics(capsys, gate_copy.reports, gate_copy.labels, bad_labels, orphan_labels)

    assert result.exit_code == 0, result.stderr
    for name in bad_reports:
        assert len(result.stderr_lines(name)) == 1, (name, result.stderr)
    assert len(result.stderr_lines("bad.jsonl")) == 1, result.stderr
    assert len(result.stderr_lines("orphan")) == 2, result.stderr
    doc = result.doc
    assert orphans(doc) == 2
    assert_gate_numbers(doc)
    comment = precision(doc, "comment")
    assert (comment["accepted"], comment["rejected"]) == (0, 0), comment
    assert outcome_counts(doc) == {("shadow", "event", "reviewed", None): 4}


def reaction_without_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> dict:
    """A real `reaction` record from `collect`, for a comment no report under DIR created."""
    github = ClosedPr()
    github.react(github.own(), "+1")
    collected = collect(tmp_path, capsys, github.client, name="orphan")
    assert collected.exit_code == 0, collected.stderr
    (record,) = collected.records
    return record


def test_p2_s10_ac12_a_report_over_10_mib_is_skipped(gate_copy, capsys):
    padded = load(next(gate_copy.reports.rglob("shadow.json")))
    padded["run_id"] = "padded-run"
    padded["padding"] = "x" * (TEN_MIB + 1)
    target = gate_copy.reports / "huge" / "shadow.json"
    target.parent.mkdir()
    target.write_text(json.dumps(padded), encoding="utf-8")

    result = metrics(capsys, gate_copy.reports, gate_copy.labels)

    assert result.elapsed_s < BOUND_S, result.elapsed_s
    assert len(result.stderr_lines("huge")) == 1, result.stderr
    assert outcome_counts(result.doc) == {("shadow", "event", "reviewed", None): 4}


# Limits: a labels file near 10 MB of rejudgements is read in linear time; the latest wins.


def test_p2_s10_limits_nine_mb_of_rejudgements_in_time_and_the_latest_wins(gate_copy, capsys):
    accepted = next(r for r in gate_copy.records if r["label"] == "accept")
    start = datetime(2027, 1, 1, tzinfo=UTC)
    rejected = {"label": "reject", "reason": "wrong"}
    rejudgements = [
        accepted | rejected | {"recorded_at": utc(start + timedelta(seconds=i))}
        for i in range(20_000)
    ]
    rejudgements[-1] = rejudgements[-1] | {"label": "accept", "reason": None}
    rejudgements.reverse()  # the latest first in the file
    big = write_labels(gate_copy.reports.parent / "big.jsonl", rejudgements)
    assert 5_000_000 < big.stat().st_size < 10_000_000, big.stat().st_size

    result = metrics(capsys, gate_copy.reports, gate_copy.labels, big)

    assert result.elapsed_s < BOUND_S, result.elapsed_s
    assert_gate_numbers(result.doc)


# AC13: --format json is one ASCII metrics/v1 document with the text's numbers.


def test_p2_s10_ac13_json_is_one_ascii_metrics_v1_document(gate_data, capsys):
    result = metrics(capsys, gate_data.reports, gate_data.labels, flags=PRICES)

    assert result.exit_code == 0, result.stderr
    assert result.stdout.isascii(), result.stdout
    assert result.stdout.count("\n") <= 1 or result.stdout.endswith("}\n"), result.stdout
    document, end = json.JSONDecoder().raw_decode(result.stdout)
    assert result.stdout[end:].strip() == "", "more than one JSON document"
    assert document["schema"] == "metrics/v1"


def test_p2_s10_ac13_json_and_text_show_the_same_numbers(gate_data, capsys):
    doc = metrics(capsys, gate_data.reports, gate_data.labels).doc
    text = metrics(capsys, gate_data.reports, gate_data.labels, json_format=False).stdout

    shadow = precision(doc, "shadow")
    for key in ("precision", "wilson_low", "wilson_high", "lower_bound", "coverage"):
        assert f"{pct(shadow[key]):.1f}%" in text, (key, shadow[key], text)
