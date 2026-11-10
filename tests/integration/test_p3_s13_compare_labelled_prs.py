"""P3-S13: compare Phase 3 against Phase 2 on the labelled pull requests, offline.

Written from the spec's criteria, Limits and Interfaces, Phase 2's "How the gate is evidenced",
Phase 3's Q3 and ADR 0016, while the developer built the slice and before any of its code was
read. No model is called: every input is a file written here.

Inputs are built in `tmp_path`:
- `shadow/v1` reports, shaped as `pr.shadow` writes them today (`Side.report`). A baseline is a
  diff-mode run (`p1-v1`, `context` null); a candidate is a context-mode backfill (`p3-v5`, with
  `context.tool_calls`). `phase2_shape=True` drops every key Phase 3 added, as the real Phase 2
  artifacts lack them.
- `feedback/v1` shadow labels, one per judged unit, keyed as `pr label` writes them.

The seam, assumed: `review_bot.pr.cli.main(["compare", "--baseline", DIR, "--candidate", DIR,
"--labels", FILE, *flags])` in process; stdout and stderr read with `capsys`.

Readings of `compare/v1` (Interfaces names the top-level and per-side keys, not the inner ones),
written before the code and since matched by the developer's output. Each is used in one reader
below, the only code that knows inner key names:
- `prs.compared[*]` and `prs.excluded[*]` name their PR as `number` or `pr`; a compared entry
  holds its head SHA among its values; an excluded one its `reason`.
- `<side>.units`: `accepted`, `unjudged`, rejections by reason (`rejected` as a mapping, or a
  separate mapping), `coverage`.
- `<side>.precision`: a point estimate, a Wilson interval (`wilson_low`/`wilson_high`, or a pair)
  and `lower_bound`.
- `verdict`: `precision` and `recall` words, or `not decidable: coverage` as that string or as
  `decidable` false with `reason` `coverage`.
- A share is read by `as_share`: a fraction, a percentage string, a pair or `{n, d}` mapping.
- Money is read from every decimal value under `<side>.cost`; other numbers from every number
  under `<side>.tokens`, `.tool_calls` and `.latency`, so a value's key name isn't assumed.
"""

import hashlib
import importlib
import json
import math
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

REPO = "octo/review-bot"
APP = "src/app.py"
OTHER_FILE = "src/other.py"
MODEL = "claude-test-model"
USAGE_EXIT = 2
TEN_MB = 10_000_000
# Generous: it catches quadratic work or a hang, not a slow CI machine.
BOUND_S = 60.0
PRICES = ("--price-input-usd-per-mtok", "15", "--price-output-usd-per-mtok", "75")
T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
LABELLED_AT = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
VERDICT_WORDS = ("higher", "same", "lower")
REASONS = ("wrong", "not_worth_it", "wrong_line", "duplicate")

# Text that must never reach the output (AC8): a comment body, a rationale and usernames.
BODY = "BODYMARK-7f3 says @bodyuser-91 should fix this"
RATIONALE = "RATIONALEMARK-c41 the total is wrong"
SUMMARY = "SUMMARYMARK-2be cc @summaryuser-55"
FORBIDDEN = ("BODYMARK", "RATIONALEMARK", "SUMMARYMARK", "bodyuser", "summaryuser")


def sha40(tag: str) -> str:
    return hashlib.sha1(tag.encode(), usedforsecurity=False).hexdigest()


def hex16(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


HEAD_A, HEAD_B, HEAD_C, HEAD_D, HEAD_E, HEAD_F = (sha40(tag) for tag in "ABCDEF")
BASE_SHA = sha40("base")


def anchor_of(file: str, line: int) -> str:
    """The anchor of a line: two lines with the same number in one file share it."""
    return hex16(f"anchor|{file}|{line}")


def entry(
    line: int, rule: str = "ai:logic", *, file: str = APP, anchor: str | None = None
) -> dict[str, Any]:
    """One `create` entry of a shadow report, as `pr.shadow` writes it."""
    anchor = anchor or anchor_of(file, line)
    return {
        "action": "create",
        "fingerprint": hex16(f"fp|{file}|{rule}|{line}|{anchor}"),
        "rule_part": rule,
        "anchor": anchor,
        "source": "ai" if rule.startswith("ai:") else "rule",
        "severity": "medium",
        "file": file,
        "line": line,
        "body": BODY,
        "applied": None,
        "owners": [],
        "evidence": None,
        "evidence_dropped": None,
        "suggestion": None,
    }


def utc(moment: datetime) -> str:
    return moment.isoformat()


# Fixture files.


@dataclass(frozen=True, slots=True)
class Result:
    exit_code: int
    stdout: str
    stderr: str
    elapsed_s: float

    @property
    def document(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.stdout)
        return document


@dataclass
class Fixture:
    """A `--baseline` and a `--candidate` directory of reports, and a `--labels` file."""

    root: Path
    label_lines: list[str] = field(default_factory=list)
    reports_written: int = 0

    @property
    def baseline(self) -> Path:
        return self.root / "baseline"

    @property
    def candidate(self) -> Path:
        return self.root / "candidate"

    @property
    def labels(self) -> Path:
        return self.root / "labels.jsonl"

    def report(  # noqa: PLR0913 - keyword-only, one per field a criterion varies
        self,
        side: str,
        pr: int,
        head: str,
        entries: Sequence[dict[str, Any]] = (),
        *,
        mode: str = "shadow",
        outcome: str = "reviewed",
        usage: tuple[int, int] = (1000, 100),
        cost_usd: str | None = None,
        tool_calls: int = 2,
        review_s: float = 30.0,
        run_id: str | None = None,
        phase2_shape: bool = False,
        name: str | None = None,
        repo: str = REPO,
    ) -> Path:
        """Write one `shadow/v1` report under the side's directory; returns its path."""
        self.reports_written += 1
        number = self.reports_written
        document = shadow_document(
            side=side,
            pr=pr,
            head=head,
            entries=entries,
            mode=mode,
            outcome=outcome,
            usage=usage,
            cost_usd=cost_usd,
            tool_calls=tool_calls,
            started=T0 + timedelta(minutes=number),
            review_s=review_s,
            run_id=run_id or f"run-{side}-{pr}-{head[:6]}-{number}",
            phase2_shape=phase2_shape,
            repo=repo,
        )
        directory = self.root / side / (name or f"artifact-{number:04d}")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "shadow.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def label(  # noqa: PLR0913 - keyword-only, one per field a criterion varies
        self,
        pr: int,
        unit_entry: Mapping[str, Any],
        verdict: str,
        *,
        index: int = 0,
        recorded_at: datetime = LABELLED_AT,
        fingerprint: str | None = None,
        repo: str = REPO,
    ) -> None:
        """Append a shadow label: `accept`, or a reject reason."""
        record = {
            "schema": "feedback/v1",
            "channel": "shadow",
            "repo": repo,
            "pr": pr,
            "fingerprint": fingerprint or unit_entry["fingerprint"],
            "file": unit_entry["file"],
            "rule_part": unit_entry["rule_part"],
            "anchor": unit_entry["anchor"],
            "index": index,
            "source": unit_entry["source"],
            "severity": unit_entry["severity"],
            "label": "accept" if verdict == "accept" else "reject",
            "reason": None if verdict == "accept" else verdict,
            "run_id": "run-labelled",
            "model": MODEL,
            "prompt_version": "p1-v1",
            "recorded_at": utc(recorded_at),
        }
        self.label_lines.append(json.dumps(record))

    def judged(
        self,
        side: str,
        pr: int,
        head: str,
        items: Sequence[tuple[int, str, str | None]],
        **report_fields: Any,
    ) -> Path:
        """One report with a unit per `(line, rule, verdict)`, each labelled unless None."""
        entries = [entry(line, rule) for line, rule, _ in items]
        path = self.report(side, pr, head, entries, **report_fields)
        for unit_entry, (_, _, verdict) in zip(entries, items, strict=True):
            if verdict is not None:
                self.label(pr, unit_entry, verdict)
        return path

    def compare(
        self,
        capsys: pytest.CaptureFixture[str],
        *flags: str,
        json_format: bool = True,
        write_labels: bool = True,
    ) -> Result:
        self.baseline.mkdir(parents=True, exist_ok=True)
        self.candidate.mkdir(parents=True, exist_ok=True)
        if write_labels:
            self.labels.write_text("".join(f"{line}\n" for line in self.label_lines), "utf-8")
        argv = [
            "compare", "--baseline", str(self.baseline), "--candidate", str(self.candidate),
            "--labels", str(self.labels), *flags,
        ]  # fmt: skip
        if json_format:
            argv += ["--format", "json"]
        pr_cli = importlib.import_module("review_bot.pr.cli")
        capsys.readouterr()
        started = time.monotonic()
        exit_code = pr_cli.main(argv)
        elapsed = time.monotonic() - started
        captured = capsys.readouterr()
        return Result(exit_code, captured.out, captured.err, elapsed)

    def compared(self, capsys: pytest.CaptureFixture[str], *flags: str) -> dict[str, Any]:
        """Run `compare --format json`, check it exits 0, and return its document."""
        result = self.compare(capsys, *flags)
        assert result.exit_code == 0, result.stderr
        return result.document


def shadow_document(  # noqa: PLR0913 - keyword-only, one per field of the report
    *,
    side: str,
    pr: int,
    head: str,
    entries: Sequence[dict[str, Any]],
    mode: str,
    outcome: str,
    usage: tuple[int, int],
    cost_usd: str | None,
    tool_calls: int,
    started: datetime,
    review_s: float,
    run_id: str,
    repo: str,
    phase2_shape: bool,
) -> dict[str, Any]:
    reviewed = outcome == "reviewed"
    is_candidate = side == "candidate"
    context = (
        {
            "commit": head, "tool_calls": tool_calls, "refused": 0,
            "result_bytes": 100 * tool_calls, "files_read": [],
        }
        if is_candidate and reviewed
        else None
    )  # fmt: skip
    report: dict[str, Any] | None = None
    if reviewed:
        report = {
            "schema": "report/v1",
            "run_id": run_id,
            "provider": "claude",
            "model": MODEL,
            "prompt_version": "p3-v5" if is_candidate else "p1-v1",
            "threshold": "low",
            "findings": [
                {
                    "file": item["file"], "line": item["line"], "severity": item["severity"],
                    "category": "logic", "rationale": RATIONALE, "confidence": 0.9,
                }
                for item in entries
            ],
            "counts": {"reported": len(entries), "below_threshold": 0, "dropped": 0,
                       "deduped": 0, "rejected": 0, "undecided": 0},
            "skipped_files": [],
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1], "cost_usd": cost_usd},
            "mode": "context" if is_candidate else "diff",
            "context": context,
        }  # fmt: skip
    document: dict[str, Any] = {
        "schema": "shadow/v1",
        "run_id": run_id,
        "mode": mode,
        "origin": "backfill" if is_candidate else "event",
        "outcome": outcome,
        "reason": None if reviewed else "permanent",
        "pr": {"repo": repo, "number": pr, "base_sha": BASE_SHA, "head_sha": head},
        "event_at": utc(T0),
        "started_at": utc(started),
        "finished_at": utc(started + timedelta(seconds=review_s)),
        "report": report,
        "summary_body": SUMMARY,
        "comments": list(entries) if reviewed else [],
        "withheld": [],
        "caps": {"max_findings": 10},
        "over_budget": False,
        "scope": "full",
        "since": None,
        "config": None,
        "codeowners": None,
        "context": context,
        "issues": [],
    }
    if phase2_shape:
        for key in ("codeowners", "context", "issues"):
            del document[key]
        if report is not None:
            for key in ("mode", "context"):
                del report[key]
            del report["usage"]["cost_usd"]
    return document


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


# compare/v1 reading: the only code that knows inner key names (assumed; see the docstring).


def leaves(value: Any) -> Iterator[Any]:
    if isinstance(value, Mapping):
        for item in value.values():
            yield from leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from leaves(item)
    else:
        yield value


def numbers(value: Any) -> list[float]:
    return [
        float(item)
        for item in leaves(value)
        if isinstance(item, int | float) and not isinstance(item, bool)
    ]


def decimals(value: Any) -> set[Decimal]:
    found = set()
    for item in leaves(value):
        if isinstance(item, bool) or item is None:
            continue
        try:
            found.add(Decimal(str(item)))
        except InvalidOperation:
            continue
    return found


def strings(value: Any) -> list[str]:
    return [item for item in leaves(value) if isinstance(item, str)]


def as_share(value: Any) -> float | None:
    """A share as a fraction: a number, `70.0%`, `[n, d]`, `{n, d}` or `{share: ...}`."""
    if value is None or value == "n/a":
        return None
    assert not isinstance(value, bool), f"not a share: {value!r}"
    if isinstance(value, int | float):
        return float(value) / 100 if value > 1 else float(value)
    if isinstance(value, str):
        return float(value.rstrip("%")) / 100 if value.endswith("%") else float(value)
    if isinstance(value, list) and len(value) == 2:
        return None if value[1] == 0 else value[0] / value[1]
    if isinstance(value, Mapping):
        for key in ("share", "value", "fraction", "point", "estimate", "recall", "precision"):
            if key in value and not isinstance(value[key], Mapping):
                return as_share(value[key])
        for top, bottom in (("issues", "pool"), ("numerator", "denominator"), ("found", "of")):
            if top in value and bottom in value:
                return None if value[bottom] == 0 else value[top] / value[bottom]
    raise AssertionError(f"not a share: {value!r}")


def close(actual: float | None, expected: float, places: int = 3) -> bool:
    """Equal to a value rounded to `places` decimals (JSON) or a percentage to one (text)."""
    return actual is not None and abs(actual - expected) <= 0.5 * 10**-places + 1e-9


def pr_of(item: Mapping[str, Any]) -> int:
    for key in ("number", "pr"):
        value = item.get(key)
        if isinstance(value, Mapping):
            value = value.get("number")
        if isinstance(value, int):
            return value
    raise AssertionError(f"no PR number in {item!r}")


def compared_prs(document: Mapping[str, Any]) -> dict[int, list[Any]]:
    """Each compared PR with the values its entry holds (its head SHA among them)."""
    return {pr_of(item): list(leaves(item)) for item in document["prs"]["compared"]}


def excluded_prs(document: Mapping[str, Any]) -> dict[int, str]:
    return {pr_of(item): item["reason"] for item in document["prs"]["excluded"]}


def side_units(document: Mapping[str, Any], side: str) -> Mapping[str, Any]:
    units: Mapping[str, Any] = document[side]["units"]
    return units


def accepted(document: Mapping[str, Any], side: str) -> int:
    count: int = side_units(document, side)["accepted"]
    return count


def unjudged(document: Mapping[str, Any], side: str) -> int:
    count: int = side_units(document, side)["unjudged"]
    return count


def rejected_by_reason(document: Mapping[str, Any], side: str) -> dict[str, int]:
    units = side_units(document, side)
    for key in ("rejected", "rejected_by_reason", "reject_reasons", "by_reason", "reasons"):
        if isinstance(units.get(key), Mapping):
            by_reason = units[key]
            return {reason: by_reason.get(reason, 0) for reason in REASONS}
    raise AssertionError(f"no rejections by reason in {units!r}")


def coverage(document: Mapping[str, Any], side: str) -> float | None:
    units = side_units(document, side)
    if "coverage" in units:
        return as_share(units["coverage"])
    return as_share(document[side]["precision"]["coverage"])


def precision_point(document: Mapping[str, Any], side: str) -> float | None:
    return as_share(document[side]["precision"])


def wilson(document: Mapping[str, Any], side: str) -> tuple[float | None, float | None]:
    precision = document[side]["precision"]
    if "wilson_low" in precision:
        return as_share(precision["wilson_low"]), as_share(precision["wilson_high"])
    for key in ("wilson_95", "wilson", "interval"):
        if key in precision:
            interval = precision[key]
            if isinstance(interval, Mapping):
                return as_share(interval["low"]), as_share(interval["high"])
            return as_share(interval[0]), as_share(interval[1])
    raise AssertionError(f"no Wilson interval in {precision!r}")


def lower_bound(document: Mapping[str, Any], side: str) -> float | None:
    precision = document[side]["precision"]
    return as_share(precision["lower_bound"])


def recall(document: Mapping[str, Any], side: str) -> float | None:
    return as_share(document[side]["recall"])


def pool_size(document: Mapping[str, Any]) -> int:
    pool = document["pool"]
    if isinstance(pool, int):
        return pool
    for key in ("issues", "size", "count", "total"):
        if isinstance(pool.get(key), int):
            size: int = pool[key]
            return size
    raise AssertionError(f"no pool size in {pool!r}")


def found_only_by(document: Mapping[str, Any], side: str) -> int:
    pool = document["pool"]
    places = [pool, pool.get("only", {}), document[side], document[side].get("recall", {})]
    keys = (f"{side}_only", f"only_{side}", side, "only", "issues_only", "found_only")
    for place in places:
        if not isinstance(place, Mapping):
            continue
        for key in keys:
            if isinstance(place.get(key), int) and not isinstance(place.get(key), bool):
                count: int = place[key]
                return count
    raise AssertionError(f"no count of issues found only by {side}")


def side_cost(document: Mapping[str, Any], side: str) -> Any:
    return document[side]["cost"]


def verdict_words(document: Mapping[str, Any]) -> tuple[str, str]:
    verdict = document["verdict"]
    assert isinstance(verdict, Mapping), verdict
    return verdict["precision"], verdict["recall"]


def is_not_decidable(document: Mapping[str, Any]) -> bool:
    """`not decidable: coverage`, as that string or as `decidable` false for `coverage`."""
    verdict = document["verdict"]
    if isinstance(verdict, str):
        return verdict == "not decidable: coverage"
    return verdict.get("decidable") is False and verdict.get("reason") == "coverage"


# Independent statistics.


def wilson_95(successes: int, trials: int) -> tuple[float, float]:
    z = 1.959963984540054
    share = successes / trials
    denominator = 1 + z * z / trials
    centre = (share + z * z / (2 * trials)) / denominator
    half = z / denominator * math.sqrt(share * (1 - share) / trials + z * z / (4 * trials**2))
    return centre - half, centre + half


def test_p3_s13_selfcheck_wilson_matches_phase_2s_worked_example() -> None:
    low, high = wilson_95(21, 30)
    assert (round(low, 3), round(high, 3)) == (0.521, 0.833)


def test_p3_s13_selfcheck_fixtures_are_valid_phase_2_inputs(fx: Fixture) -> None:
    # Phase 2's own readers accept every report and label written here, in both shapes.
    reports_module = importlib.import_module("review_bot.pr.reports")
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "accept")], phase2_shape=True)
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "wrong"), (3, "ai:logic", None)],
              cost_usd="0.40")  # fmt: skip
    fx.report("baseline", 6, HEAD_C, [entry(4)], outcome="failed")
    fx.label_lines.append(json.dumps(json.loads(fx.label_lines[0]) | {"index": 0}))

    paths = sorted(fx.root.rglob("shadow.json"))
    assert len(paths) == 3
    for path in paths:
        report = reports_module.read_report(path)
        assert not isinstance(report, str), (path, report)
    labels = b"\n".join(line.encode() for line in fx.label_lines)
    assert len(reports_module.parse_labels(labels)) == 3


# AC1: pairing by head SHA.


def pairing_fixture(fx: Fixture) -> dict[str, dict[str, Any]]:
    """PR 2: baseline at A and B, candidate at B. PR 3: candidate at C, baseline at D. PR 4:
    candidate only. PR 5: baseline only. One accepted unit in each report."""
    units = {
        "base_2a": entry(1),
        "base_2b": entry(2),
        "cand_2b": entry(3),
        "cand_3c": entry(4),
        "base_3d": entry(5),
        "cand_4e": entry(6),
        "base_5f": entry(7),
    }
    fx.report("baseline", 2, HEAD_A, [units["base_2a"]])
    fx.report("baseline", 2, HEAD_B, [units["base_2b"]])
    fx.report("candidate", 2, HEAD_B, [units["cand_2b"]])
    fx.report("candidate", 3, HEAD_C, [units["cand_3c"]])
    fx.report("baseline", 3, HEAD_D, [units["base_3d"]])
    fx.report("candidate", 4, HEAD_E, [units["cand_4e"]])
    fx.report("baseline", 5, HEAD_F, [units["base_5f"]])
    for key, unit_entry in units.items():
        fx.label(int(key.split("_")[1][0]), unit_entry, "accept")
    return units


def test_p3_s13_ac1_only_the_same_head_reports_of_pr_2_are_compared(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    pairing_fixture(fx)

    document = fx.compared(capsys)

    compared = compared_prs(document)
    assert set(compared) == {2}
    assert HEAD_B in compared[2]
    assert HEAD_A not in compared[2]


@pytest.mark.parametrize(
    ("pr", "reason"),
    [(3, "no_same_head"), (4, "no_same_head"), (5, "no_candidate")],
    ids=["candidate-at-another-head", "candidate-without-baseline", "baseline-without-candidate"],
)
def test_p3_s13_ac1_unpaired_prs_are_excluded_with_their_reason(
    fx: Fixture, capsys: pytest.CaptureFixture[str], pr: int, reason: str
) -> None:
    pairing_fixture(fx)

    document = fx.compared(capsys)

    assert excluded_prs(document).get(pr) == reason
    assert pr not in compared_prs(document)


def test_p3_s13_ac1_only_units_of_compared_reports_count(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # Each side's one unit is in PR 2's B reports; A's, PR 3's, PR 4's and PR 5's don't count.
    pairing_fixture(fx)

    document = fx.compared(capsys)

    for side in ("baseline", "candidate"):
        assert accepted(document, side) == 1, side
        assert unjudged(document, side) == 0, side
        assert sum(rejected_by_reason(document, side).values()) == 0, side
    assert pool_size(document) == 2


@pytest.mark.parametrize(
    ("side", "change"),
    [
        ("baseline", {"outcome": "failed"}),
        ("baseline", {"mode": "comment"}),
        ("candidate", {"outcome": "failed"}),
        ("candidate", {"mode": "comment"}),
    ],
    ids=["baseline-failed", "baseline-comment-mode", "candidate-failed", "candidate-comment-mode"],
)
def test_p3_s13_ac1_a_report_that_is_not_reviewed_in_shadow_mode_does_not_pair(
    fx: Fixture, capsys: pytest.CaptureFixture[str], side: str, change: dict[str, str]
) -> None:
    # PR 2 pairs normally; PR 6's pair at head C has one side not `reviewed` in shadow mode.
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "accept")])
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")])
    sides: dict[str, dict[str, str]] = {"baseline": {}, "candidate": {}} | {side: change}
    fx.judged("baseline", 6, HEAD_C, [(3, "ai:logic", "accept")], **sides["baseline"])
    fx.judged("candidate", 6, HEAD_C, [(4, "ai:logic", "accept")], **sides["candidate"])

    document = fx.compared(capsys)

    assert set(compared_prs(document)) == {2}
    assert 6 in excluded_prs(document)
    assert accepted(document, "baseline") == accepted(document, "candidate") == 1


def test_p3_s13_ac1_a_candidate_at_another_head_of_a_compared_pr_does_not_count(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # Candidate reports for PR 2 at A and B, a baseline only at B: only B's units count.
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "accept")])
    fx.judged("candidate", 2, HEAD_A, [(2, "ai:logic", "wrong"), (3, "ai:logic", "accept")])
    fx.judged("candidate", 2, HEAD_B, [(4, "ai:logic", "accept")])

    document = fx.compared(capsys)

    assert HEAD_B in compared_prs(document)[2]
    assert HEAD_A not in compared_prs(document)[2]
    assert accepted(document, "candidate") == 1
    assert rejected_by_reason(document, "candidate")["wrong"] == 0
    assert pool_size(document) == 2


def test_p3_s13_ac1_the_same_pr_number_in_another_repo_does_not_pair(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "accept")], repo="octo/other")
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")])

    document = fx.compared(capsys)

    assert compared_prs(document) == {}
    assert accepted(document, "baseline") == accepted(document, "candidate") == 0
    # Both are PR 2, so read the list itself, not `excluded_prs` keyed by number.
    reasons = sorted(item["reason"] for item in document["prs"]["excluded"])
    assert reasons == ["no_candidate", "no_same_head"]


def test_p3_s13_ac1_phase_2_shaped_baseline_reports_are_compared(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # The real baseline is Phase 2's artifacts, written before Phase 3 added `context`,
    # `issues`, `codeowners`, `report.mode` and `usage.cost_usd`.
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "accept")], phase2_shape=True)
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")])

    result = fx.compare(capsys)

    assert result.exit_code == 0, result.stderr
    assert set(compared_prs(result.document)) == {2}
    assert accepted(result.document, "baseline") == 1
    assert "skipped" not in result.stderr


# AC2: units, verdicts and precision per side.


# 34 baseline units over PRs 2 and 6: 21 accept, 4 wrong, 3 not worth it, 2 wrong line,
# 2 duplicate, 2 unjudged (Phase 2's worked example). 13 candidate units: 10 accept, 1 wrong,
# 1 duplicate, 1 unjudged.
def plan(*parts: tuple[str | None, int]) -> list[str | None]:
    """Verdicts in order, each repeated: `None` leaves a unit unjudged."""
    return [verdict for verdict, count in parts for _ in range(count)]


BASELINE_PLANS = {
    2: plan(("accept", 11), ("wrong", 2), ("not_worth_it", 2), ("wrong_line", 1),
            ("duplicate", 1), (None, 1)),
    6: plan(("accept", 10), ("wrong", 2), ("not_worth_it", 1), ("wrong_line", 1),
            ("duplicate", 1), (None, 1)),
}  # fmt: skip
CANDIDATE_PLANS = {
    2: plan(("accept", 6), ("wrong", 1), ("duplicate", 1), (None, 1)),
    6: plan(("accept", 4)),
}
HEADS = {2: HEAD_B, 6: HEAD_C}


def precision_fixture(fx: Fixture) -> None:
    for side, plans, rule in (
        ("baseline", BASELINE_PLANS, "ai:logic"),
        ("candidate", CANDIDATE_PLANS, "ai:reliability"),
    ):
        for pr, plan in plans.items():
            items = [(line, rule, verdict) for line, verdict in enumerate(plan, start=1)]
            fx.judged(side, pr, HEADS[pr], items)


@pytest.mark.parametrize(
    ("side", "expected"),
    [
        ("baseline", {"accepted": 21, "unjudged": 2,
                      "rejected": {"wrong": 4, "not_worth_it": 3, "wrong_line": 2,
                                   "duplicate": 2}}),
        ("candidate", {"accepted": 10, "unjudged": 1,
                       "rejected": {"wrong": 1, "not_worth_it": 0, "wrong_line": 0,
                                    "duplicate": 1}}),
    ],
)  # fmt: skip
def test_p3_s13_ac2_each_side_shows_accepted_rejected_by_reason_and_unjudged(
    fx: Fixture, capsys: pytest.CaptureFixture[str], side: str, expected: dict[str, Any]
) -> None:
    precision_fixture(fx)

    document = fx.compared(capsys)

    assert accepted(document, side) == expected["accepted"]
    assert unjudged(document, side) == expected["unjudged"]
    assert rejected_by_reason(document, side) == expected["rejected"]


@pytest.mark.parametrize(
    ("side", "counted", "judged", "units"),
    [("baseline", (21, 30), (21, 32), (32, 34)), ("candidate", (10, 11), (10, 12), (12, 13))],
)
def test_p3_s13_ac2_each_side_shows_precision_wilson_lower_bound_and_coverage(  # noqa: PLR0913, PLR0917 - parametrised
    fx: Fixture,
    capsys: pytest.CaptureFixture[str],
    side: str,
    counted: tuple[int, int],
    judged: tuple[int, int],
    units: tuple[int, int],
) -> None:
    precision_fixture(fx)

    document = fx.compared(capsys)

    low, high = wilson_95(*counted)
    assert close(precision_point(document, side), counted[0] / counted[1])
    actual_low, actual_high = wilson(document, side)
    assert close(actual_low, low)
    assert close(actual_high, high)
    assert close(lower_bound(document, side), judged[0] / judged[1])
    assert close(coverage(document, side), units[0] / units[1])


def test_p3_s13_ac2_phase_2s_worked_example_reads_as_its_spec_states(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    precision_fixture(fx)

    document = fx.compared(capsys)

    assert close(precision_point(document, "baseline"), 0.700)
    low, high = wilson(document, "baseline")
    assert close(low, 0.521, places=2)  # rounding the exact 0.5212 either way
    assert close(high, 0.833, places=2)
    assert close(lower_bound(document, "baseline"), 0.656)
    assert close(coverage(document, "baseline"), 0.941)


def test_p3_s13_ac2_two_entries_of_one_key_are_two_units_joined_by_index(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two identical lines (one anchor) in one report: units index 0 and 1, in line order.
    anchor = anchor_of(APP, 10)
    first, second = entry(10, anchor=anchor), entry(20, anchor=anchor)
    fx.report("baseline", 2, HEAD_B, [second, first])
    fx.label(2, first, "accept", index=0)
    fx.label(2, second, "wrong", index=1)
    fx.judged("candidate", 2, HEAD_B, [(30, "ai:logic", "accept")])

    document = fx.compared(capsys)

    assert accepted(document, "baseline") == 1
    assert rejected_by_reason(document, "baseline")["wrong"] == 1
    assert unjudged(document, "baseline") == 0


def test_p3_s13_ac2_label_joins_whatever_its_fingerprint(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # A rewording changes the fingerprint, not the unit: the join is repo, PR, file, rule
    # part, anchor and index.
    unit_entry = entry(1)
    fx.report("baseline", 2, HEAD_B, [unit_entry])
    fx.label(2, unit_entry, "accept", fingerprint="0123456789abcdef")
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")])

    document = fx.compared(capsys)

    assert accepted(document, "baseline") == 1
    assert unjudged(document, "baseline") == 0


@pytest.mark.parametrize(
    "mismatch",
    [{"rule_part": "ai:security"}, {"file": OTHER_FILE}, {"anchor": "ffffffffffffffff"},
     {"index": 1}, {"pr": 9}],
    ids=["rule-part", "file", "anchor", "index", "pr"],
)  # fmt: skip
def test_p3_s13_ac2_label_differing_in_one_key_field_does_not_join(
    fx: Fixture, capsys: pytest.CaptureFixture[str], mismatch: dict[str, Any]
) -> None:
    unit_entry = entry(1)
    fx.report("baseline", 2, HEAD_B, [unit_entry])
    index = mismatch.pop("index", 0)
    pr = mismatch.pop("pr", 2)
    fx.label(pr, unit_entry | mismatch, "accept", index=index)
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")])

    document = fx.compared(capsys)

    assert accepted(document, "baseline") == 0
    assert unjudged(document, "baseline") == 1


def test_p3_s13_ac2_a_unit_on_both_sides_takes_its_one_label_on_each(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    shared = entry(1)
    fx.report("baseline", 2, HEAD_B, [shared])
    fx.report("candidate", 2, HEAD_B, [shared])
    fx.label(2, shared, "accept")

    document = fx.compared(capsys)

    assert accepted(document, "baseline") == accepted(document, "candidate") == 1


def test_p3_s13_ac2_a_report_downloaded_twice_counts_once(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # `pr.units` groups by run: the same run_id in two artifacts is one run.
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "accept")])
    items = [(2, "ai:logic", "accept"), (3, "ai:logic", "wrong")]
    fx.judged("candidate", 2, HEAD_B, items, run_id="run-twice")
    fx.judged("candidate", 2, HEAD_B, items, run_id="run-twice")

    document = fx.compared(capsys)

    assert accepted(document, "candidate") == 1
    assert rejected_by_reason(document, "candidate")["wrong"] == 1


# AC3: pooled recall over accepted issues.


def test_p3_s13_ac3_different_rule_parts_on_one_anchored_line_are_one_issue_found_by_both(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "accept")])
    fx.judged("candidate", 2, HEAD_B, [(1, "ai:reliability", "accept")])

    document = fx.compared(capsys)

    assert pool_size(document) == 1
    assert close(recall(document, "baseline"), 1.0)
    assert close(recall(document, "candidate"), 1.0)
    assert found_only_by(document, "baseline") == found_only_by(document, "candidate") == 0


def pool_fixture(fx: Fixture) -> None:
    """Six issues: one found by both, three by the baseline only, two by the candidate only.

    Rejected and unjudged units add none; two accepted baseline units on one line are one; the
    same anchor in another file, or on another PR, is another issue.
    """
    baseline_2 = [entry(1, "ai:logic"), entry(2, "ai:security"), entry(3, "ai:logic"),
                  entry(5, "ai:style"), entry(5, "ai:naming"),
                  entry(9, "ai:logic", file=OTHER_FILE, anchor=anchor_of(APP, 4)),
                  entry(7, "ai:logic")]  # fmt: skip
    candidate_2 = [entry(1, "ai:reliability"), entry(2, "ai:testing"), entry(4, "ai:logic"),
                   entry(8, "ai:logic")]  # fmt: skip
    verdicts_2: dict[str, list[str | None]] = {
        "baseline": ["accept", "accept", "wrong", "accept", "accept", "accept", None],
        "candidate": ["accept", "wrong", "accept", "duplicate"],
    }
    for side, entries in (("baseline", baseline_2), ("candidate", candidate_2)):
        fx.report(side, 2, HEAD_B, entries)
        for unit_entry, verdict in zip(entries, verdicts_2[side], strict=True):
            if verdict is not None:
                fx.label(2, unit_entry, verdict)
    fx.report("baseline", 6, HEAD_C, [])
    fx.judged("candidate", 6, HEAD_C, [(1, "ai:logic", "accept")])


def test_p3_s13_ac3_pool_is_every_issue_either_side_accepted(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    pool_fixture(fx)

    document = fx.compared(capsys)

    assert pool_size(document) == 6


@pytest.mark.parametrize(("side", "found", "only"), [("baseline", 4, 3), ("candidate", 3, 2)])
def test_p3_s13_ac3_each_side_shows_recall_and_issues_only_it_found(
    fx: Fixture, capsys: pytest.CaptureFixture[str], side: str, found: int, only: int
) -> None:
    pool_fixture(fx)

    document = fx.compared(capsys)

    assert close(recall(document, side), found / 6)
    assert found_only_by(document, side) == only


def test_p3_s13_ac3_no_accepted_unit_gives_an_empty_pool_without_error(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "wrong")])
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "wrong")])

    document = fx.compared(capsys)

    assert pool_size(document) == 0
    assert precision_point(document, "candidate") == 0.0


# AC4: cost, tokens, tool calls and review time.


def cost_fixture(fx: Fixture, *, costed: bool) -> None:
    """Three compared PRs. Baseline costs 0.10, 0.20, 0.30 with 4 accepted units; candidate
    costs 0.25, 0.35, 0.60 with 6. Tokens, tool calls and review times differ per PR."""
    rows = [
        # pr, head, baseline (cost, usage, review_s, accepted), candidate (..., tool calls)
        (2, HEAD_B, ("0.10", (1000, 100), 5.0, 2), ("0.25", (4000, 400), 10.0, 2, 2)),
        (6, HEAD_C, ("0.20", (2000, 200), 6.0, 1), ("0.35", (5000, 500), 20.0, 2, 3)),
        (7, HEAD_D, ("0.30", (6000, 600), 7.0, 1), ("0.60", (9000, 900), 300.0, 2, 7)),
    ]
    line = 0
    for pr, head, baseline, candidate in rows:
        for side, (cost, usage, review_s, accepted_units, *calls) in (
            ("baseline", baseline),
            ("candidate", candidate),
        ):
            items = []
            for _ in range(accepted_units):
                line += 1
                items.append((line, "ai:logic", "accept"))
            fx.judged(
                side, pr, head, items, cost_usd=cost if costed else None, usage=usage,
                review_s=review_s, tool_calls=calls[0] if calls else 0,
            )  # fmt: skip


@pytest.mark.parametrize(
    ("side", "total", "mean", "per_accepted"),
    [("baseline", "0.60", "0.20", "0.15"), ("candidate", "1.20", "0.40", "0.20")],
)
def test_p3_s13_ac4_reported_costs_give_total_mean_and_cost_per_accepted_unit(  # noqa: PLR0913, PLR0917 - parametrised
    fx: Fixture,
    capsys: pytest.CaptureFixture[str],
    side: str,
    total: str,
    mean: str,
    per_accepted: str,
) -> None:
    cost_fixture(fx, costed=True)

    document = fx.compared(capsys)

    cost = side_cost(document, side)
    assert {Decimal(total), Decimal(mean), Decimal(per_accepted)} <= decimals(cost), cost
    assert "estimated" not in json.dumps(cost)


def test_p3_s13_ac4_reported_costs_win_over_prices(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    cost_fixture(fx, costed=True)

    document = fx.compared(capsys, *PRICES)

    cost = side_cost(document, "candidate")
    assert Decimal("1.20") in decimals(cost)
    assert "estimated" not in json.dumps(cost)


def test_p3_s13_ac4_a_report_that_is_not_compared_does_not_force_an_estimate(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # "Every compared report": an uncosted baseline at another head isn't compared.
    cost_fixture(fx, costed=True)
    fx.judged("baseline", 2, HEAD_A, [(99, "ai:logic", "accept")], cost_usd=None)

    document = fx.compared(capsys, *PRICES)

    assert "estimated" not in json.dumps(side_cost(document, "baseline"))
    assert Decimal("0.60") in decimals(side_cost(document, "baseline"))


def test_p3_s13_ac4_without_costs_both_prices_estimate_from_tokens_in_decimal(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1,000,000 input and 100,000 output tokens at 15 and 75 USD per Mtok is 22.50 (P2-S10 AC5);
    # a second PR with no tokens halves the mean; 9 accepted units give 2.50 each.
    accepted_lines = [(line, "ai:logic", "accept") for line in range(1, 10)]
    fx.judged("baseline", 2, HEAD_B, accepted_lines, usage=(1_000_000, 100_000))
    fx.judged("baseline", 6, HEAD_C, [], usage=(0, 0))
    fx.judged("candidate", 2, HEAD_B, [(20, "ai:logic", "accept")])
    fx.judged("candidate", 6, HEAD_C, [(21, "ai:logic", "accept")])

    result = fx.compare(capsys, *PRICES)

    assert result.exit_code == 0, result.stderr
    cost = side_cost(result.document, "baseline")
    assert "estimated" in json.dumps(cost)
    assert {Decimal("22.50"), Decimal("11.25"), Decimal("2.50")} <= decimals(cost), cost
    assert "estimated" in fx.compare(capsys, *PRICES, json_format=False).stdout


def test_p3_s13_ac4_baseline_without_costs_is_estimated_beside_a_costed_candidate(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # The real case: Phase 2's reports have no `cost_usd`; context runs report theirs.
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "accept")], phase2_shape=True,
              usage=(1_000_000, 100_000))  # fmt: skip
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")], cost_usd="0.40")

    document = fx.compared(capsys, *PRICES)

    cost = side_cost(document, "baseline")
    assert "estimated" in json.dumps(cost)
    assert Decimal("22.50") in decimals(cost)


def test_p3_s13_ac4_without_costs_or_prices_cost_is_not_available(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    cost_fixture(fx, costed=False)

    document = fx.compared(capsys)
    text = fx.compare(capsys, json_format=False)

    for side in ("baseline", "candidate"):
        cost = side_cost(document, side)
        assert cost is None or "not available" in strings(cost), cost
        assert not decimals(cost) - {Decimal(0)}, f"a cost without costs or prices: {cost}"
    assert "not available" in text.stdout


@pytest.mark.parametrize(
    "costing", ["reported", "estimated", "not-available"], ids=lambda costing: costing
)
def test_p3_s13_ac4_tokens_tool_calls_and_review_time_are_always_shown(
    fx: Fixture, capsys: pytest.CaptureFixture[str], costing: str
) -> None:
    cost_fixture(fx, costed=costing == "reported")
    flags = PRICES if costing == "estimated" else ()

    document = fx.compared(capsys, *flags)

    # Baseline tokens per PR: input 1000, 2000, 6000 (mean 3000), output 100, 200, 600 (300).
    tokens = numbers(document["baseline"]["tokens"])
    assert {3000.0, 300.0} <= set(tokens) or 3300.0 in tokens, tokens
    # Candidate tool calls 2, 3 and 7: mean 4, largest 7.
    assert {4.0, 7.0} <= set(numbers(document["candidate"]["tool_calls"]))
    # Candidate review times 10, 20 and 300 s: p50 20 and p95 300 (nearest rank).
    assert {20.0, 300.0} <= set(numbers(document["candidate"]["latency"]))
    assert {6.0, 7.0} <= set(numbers(document["baseline"]["latency"]))


def test_p3_s13_ac4_no_accepted_unit_gives_no_cost_per_accepted_unit_without_error(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "wrong")], cost_usd="0.10")
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "wrong")], cost_usd="0.30")

    result = fx.compare(capsys)

    assert result.exit_code == 0, result.stderr
    cost = side_cost(result.document, "candidate")
    assert decimals(cost) == {Decimal("0.30")}, cost  # total and mean; no per-accepted value


# AC5: the verdict.


@pytest.mark.parametrize(
    ("baseline", "candidate", "expected"),
    [
        # precision 2/4 against 1/1; recall 2/2 against 1/2
        ([(1, "accept"), (2, "accept"), (3, "wrong"), (4, "wrong")], [(1, "accept")],
         ("higher", "lower")),
        # precision 1/2 each; recall 1/1 each
        ([(1, "accept"), (2, "wrong")], [(1, "accept"), (3, "wrong")], ("same", "same")),
        # equal at 100%
        ([(1, "accept")], [(1, "accept")], ("same", "same")),
        # precision 1/1 against 2/3; recall 1/2 against 2/2
        ([(1, "accept")], [(1, "accept"), (2, "accept"), (3, "not_worth_it")],
         ("lower", "higher")),
        # duplicates don't lower precision: 1/1 against 1/1 with a duplicate
        ([(1, "accept")], [(1, "accept"), (5, "duplicate")], ("same", "same")),
    ],
    ids=["higher-lower", "same-same", "same-at-100", "lower-higher", "duplicate-ignored"],
)  # fmt: skip
def test_p3_s13_ac5_verdict_states_precision_and_recall_for_the_candidate(
    fx: Fixture,
    capsys: pytest.CaptureFixture[str],
    baseline: list[tuple[int, str]],
    candidate: list[tuple[int, str]],
    expected: tuple[str, str],
) -> None:
    # Different rule parts, so no unit is shared; a shared line is a shared issue.
    fx.judged("baseline", 2, HEAD_B, [(line, "ai:logic", v) for line, v in baseline])
    fx.judged("candidate", 2, HEAD_B, [(line, "ai:reliability", v) for line, v in candidate])

    document = fx.compared(capsys)
    text = fx.compare(capsys, json_format=False).stdout

    assert verdict_words(document) == expected
    assert not is_not_decidable(document)
    for kind, word in zip(("precision", "recall"), expected, strict=True):
        assert any(kind in line and word in line for line in text.splitlines()), (kind, text)


def coverage_fixture(fx: Fixture, baseline_judged: int, candidate_judged: int) -> None:
    """10 baseline units and 9 candidate units, the first N of each accepted, the rest not."""
    for side, rule, units, judged in (
        ("baseline", "ai:logic", 10, baseline_judged),
        ("candidate", "ai:reliability", 9, candidate_judged),
    ):
        items = [(line, rule, "accept" if line <= judged else None) for line in range(1, units + 1)]
        fx.judged(side, 2, HEAD_B, items)


def test_p3_s13_ac5_coverage_of_exactly_90_percent_is_decidable(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    coverage_fixture(fx, baseline_judged=9, candidate_judged=9)  # 90% and 100%

    document = fx.compared(capsys)

    assert not is_not_decidable(document)
    assert all(word in VERDICT_WORDS for word in verdict_words(document))


@pytest.mark.parametrize(
    ("baseline_judged", "candidate_judged", "shares"),
    [(8, 9, ("80.0%", "100.0%")), (10, 8, ("100.0%", "88.9%")), (0, 0, ("0.0%", "0.0%"))],
    ids=["baseline-80", "candidate-88.9", "nothing-judged"],
)
def test_p3_s13_ac5_coverage_below_90_percent_is_not_decidable_with_each_sides_coverage(
    fx: Fixture,
    capsys: pytest.CaptureFixture[str],
    baseline_judged: int,
    candidate_judged: int,
    shares: tuple[str, str],
) -> None:
    coverage_fixture(fx, baseline_judged, candidate_judged)

    document = fx.compared(capsys)
    text = fx.compare(capsys, json_format=False).stdout

    assert is_not_decidable(document), document["verdict"]
    assert not {"higher", "same", "lower"} & set(strings(document["verdict"]))
    expected = (baseline_judged / 10, candidate_judged / 9)
    assert close(coverage(document, "baseline"), expected[0])
    assert close(coverage(document, "candidate"), expected[1])
    verdict_numbers = numbers(document["verdict"])
    assert all(any(close(n, share) for n in verdict_numbers) for share in expected)
    assert "not decidable: coverage" in text
    for share in shares:
        assert share in text, (share, text)


# AC6: which labels count.


def test_p3_s13_ac6_labels_for_units_on_prs_not_compared_are_ignored(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    pairing_fixture(fx)
    # Rejections of the excluded PRs' units, recorded later, would show if they counted.
    for pr, unit_entry in ((3, entry(4)), (4, entry(6)), (5, entry(7))):
        fx.label(pr, unit_entry, "wrong", recorded_at=LABELLED_AT + timedelta(days=1))

    result = fx.compare(capsys)

    assert result.exit_code == 0, result.stderr
    for side in ("baseline", "candidate"):
        assert accepted(result.document, side) == 1
        assert sum(rejected_by_reason(result.document, side).values()) == 0


@pytest.mark.parametrize("later_first", [True, False], ids=["later-line-first", "later-line-last"])
def test_p3_s13_ac6_the_latest_recorded_at_wins_whatever_the_file_order(
    fx: Fixture, capsys: pytest.CaptureFixture[str], later_first: bool
) -> None:
    unit_entry = entry(1)
    fx.report("baseline", 2, HEAD_B, [unit_entry])
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")])
    later = (unit_entry, "accept", LABELLED_AT + timedelta(hours=1))
    earlier = (unit_entry, "wrong_line", LABELLED_AT)
    for judged_entry, verdict, at in (later, earlier) if later_first else (earlier, later):
        fx.label(2, judged_entry, verdict, recorded_at=at)

    document = fx.compared(capsys)

    assert accepted(document, "baseline") == 1
    assert rejected_by_reason(document, "baseline")["wrong_line"] == 0


def test_p3_s13_ac6_the_latest_label_wins_across_utc_offsets(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # 10:00+02:00 is 08:00 UTC, earlier than 09:00Z, though it sorts later as text.
    unit_entry = entry(1)
    fx.report("baseline", 2, HEAD_B, [unit_entry])
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")])
    fx.label(2, unit_entry, "accept", recorded_at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC))
    fx.label(
        2,
        unit_entry,
        "wrong",
        recorded_at=datetime.fromisoformat("2026-11-05T10:00:00+02:00"),
    )

    document = fx.compared(capsys)

    assert accepted(document, "baseline") == 1
    assert rejected_by_reason(document, "baseline")["wrong"] == 0


# AC7: malformed and skipped inputs.


@pytest.mark.parametrize(
    "bad_line",
    ["{not json", '{"schema": "feedback/v2"}', "[]"],
    ids=["not-json", "other-schema", "not-an-object"],
)
def test_p3_s13_ac7_malformed_labels_line_exits_2_naming_the_line(
    fx: Fixture, capsys: pytest.CaptureFixture[str], bad_line: str
) -> None:
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "accept")])
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")])
    fx.label_lines.insert(1, bad_line)  # line 2

    result = fx.compare(capsys)

    assert result.exit_code == USAGE_EXIT
    assert "line 2" in result.stderr, result.stderr
    assert result.stdout == ""


def test_p3_s13_ac7_label_missing_a_key_exits_2_naming_the_line(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "accept"), (2, "ai:logic", "accept")])
    fx.judged("candidate", 2, HEAD_B, [(3, "ai:logic", "accept")])
    record = json.loads(fx.label_lines[2])
    del record["anchor"]
    fx.label_lines[2] = json.dumps(record)  # line 3

    result = fx.compare(capsys)

    assert result.exit_code == USAGE_EXIT
    assert "line 3" in result.stderr, result.stderr


def bad_report(path: Path, kind: str) -> None:
    """Overwrite a valid report so it can't be used, keeping its unit had it been read."""
    if kind == "not-json":
        path.write_text("{not json", encoding="utf-8")
    elif kind == "other-schema":
        document = json.loads(path.read_text(encoding="utf-8"))
        document["schema"] = "shadow/v2"
        path.write_text(json.dumps(document), encoding="utf-8")
    else:
        pad_to(path, TEN_MB + 1)


def pad_to(path: Path, size_bytes: int) -> None:
    """Grow a valid report to exactly `size_bytes` with trailing whitespace."""
    raw = path.read_bytes()
    path.write_bytes(raw + b" " * (size_bytes - len(raw)))


@pytest.mark.parametrize("side", ["baseline", "candidate"])
@pytest.mark.parametrize("kind", ["not-json", "other-schema", "over-10-mb"])
def test_p3_s13_ac7_unusable_report_is_skipped_with_one_stderr_line(
    fx: Fixture, capsys: pytest.CaptureFixture[str], side: str, kind: str
) -> None:
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "accept")])
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")])
    # A second pair on PR 6: the bad file is one of its reports.
    pair = {
        "baseline": fx.judged("baseline", 6, HEAD_C, [(3, "ai:logic", "accept")],
                              name="unusable-report"),
        "candidate": fx.judged("candidate", 6, HEAD_C, [(4, "ai:logic", "accept")],
                               name="unusable-report"),
    }  # fmt: skip
    bad_report(pair[side], kind)

    result = fx.compare(capsys)

    assert result.exit_code == 0, result.stderr
    named = [line for line in result.stderr.splitlines() if "unusable-report" in line]
    assert len(named) == 1, result.stderr
    assert 6 not in compared_prs(result.document)
    assert set(compared_prs(result.document)) == {2}
    assert accepted(result.document, side) == 1


def test_p3_s13_ac7_report_of_exactly_10_mb_is_read(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "accept")])
    big = fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")])
    pad_to(big, TEN_MB)  # at most 10 MB, as `label` reads

    result = fx.compare(capsys)

    assert result.exit_code == 0, result.stderr
    assert result.stderr.strip() == "" or "skipped" not in result.stderr
    assert accepted(result.document, "candidate") == 1
    assert result.elapsed_s < BOUND_S


def test_p3_s13_ac7_clean_inputs_exit_0_with_nothing_skipped(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    pairing_fixture(fx)

    result = fx.compare(capsys)

    assert result.exit_code == 0
    assert "skipped" not in result.stderr


# AC8: the JSON document and what neither format shows.


def test_p3_s13_ac8_json_is_one_ascii_compare_v1_document_with_the_interface_keys(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    pairing_fixture(fx)

    result = fx.compare(capsys)

    assert result.exit_code == 0, result.stderr
    assert result.stdout.isascii()
    assert len(result.stdout.strip().splitlines()) == 1
    document = result.document
    assert document["schema"] == "compare/v1"
    assert {"schema", "prs", "baseline", "candidate", "pool", "verdict"} <= set(document)
    assert {"compared", "excluded"} <= set(document["prs"])
    side_keys = {"units", "precision", "recall", "cost", "tokens", "tool_calls", "latency"}
    for side in ("baseline", "candidate"):
        assert side_keys <= set(document[side]), side


def test_p3_s13_ac8_non_ascii_path_keeps_json_ascii(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    path = "src/caf\u00e9\u202e.py"
    fx.report("baseline", 2, HEAD_B, [entry(1, file=path)])
    fx.label(2, entry(1, file=path), "accept")
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")])

    result = fx.compare(capsys)

    assert result.exit_code == 0, result.stderr
    assert result.stdout.isascii()
    assert accepted(result.document, "baseline") == 1


def test_p3_s13_ac8_json_holds_what_the_text_shows(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    pairing_fixture(fx)
    fx.judged("baseline", 6, HEAD_C, [(10, "ai:logic", "accept"), (11, "ai:logic", None)])
    fx.judged("candidate", 6, HEAD_C, [(12, "ai:logic", "wrong")])

    document = fx.compared(capsys)
    text = fx.compare(capsys, json_format=False)

    assert text.exit_code == 0
    assert not text.stdout.lstrip().startswith("{"), "text format printed JSON"
    for pr, reason in excluded_prs(document).items():
        assert any(str(pr) in line and reason in line for line in text.stdout.splitlines()), (
            pr,
            reason,
        )
    if is_not_decidable(document):
        assert "not decidable: coverage" in text.stdout
    else:
        for kind, word in zip(("precision", "recall"), verdict_words(document), strict=True):
            assert any(kind in line and word in line for line in text.stdout.splitlines())
    for pr in compared_prs(document):
        head = HEAD_B if pr == 2 else HEAD_C
        assert head[:7] in text.stdout, (pr, head)


NUMBER_IN_TEXT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(%?)(?![\w.])")


def test_p3_s13_ac8_every_number_the_text_shows_is_in_the_json(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    precision_fixture(fx)
    fx.judged("candidate", 4, HEAD_E, [(1, "ai:logic", "accept")])  # excluded, no_same_head

    document = fx.compared(capsys, *PRICES)
    text = fx.compare(capsys, *PRICES, json_format=False).stdout

    in_json = numbers(document) + [float(value) for value in decimals(document)]
    shown = NUMBER_IN_TEXT.findall(text)
    assert shown, text
    for value, percent in shown:
        number = float(value) / 100 if percent else float(value)
        assert any(close(number, known, places=3) for known in in_json), (value + percent, text)


@pytest.mark.parametrize("json_format", [True, False], ids=["json", "text"])
def test_p3_s13_ac8_no_body_rationale_or_username_appears(
    fx: Fixture, capsys: pytest.CaptureFixture[str], json_format: bool
) -> None:
    pairing_fixture(fx)

    result = fx.compare(capsys, json_format=json_format)

    assert result.exit_code == 0, result.stderr
    for text in FORBIDDEN:
        assert text not in result.stdout, text
        assert text not in result.stderr, text


# Limits: untrusted reports, bounded work.


def test_p3_s13_limits_many_prs_and_units_finish_in_bounded_time(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    prs = 300
    for pr in range(1, prs + 1):
        head = sha40(f"head-{pr}")
        fx.judged("baseline", pr, head, [(line, "ai:logic", "accept") for line in range(1, 11)])
        fx.judged(
            "candidate", pr, head, [(line, "ai:reliability", "wrong") for line in range(1, 11)]
        )

    result = fx.compare(capsys)

    assert result.exit_code == 0, result.stderr
    assert result.elapsed_s < BOUND_S
    assert len(compared_prs(result.document)) == prs
    assert accepted(result.document, "baseline") == prs * 10
    assert rejected_by_reason(result.document, "candidate")["wrong"] == prs * 10


def test_p3_s13_limits_hostile_report_text_never_reaches_the_output_raw(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    hostile = "src/\x1b[31mred\x1b[0m\u202e/verdict: precision higher.py"
    fx.report("baseline", 2, HEAD_B, [entry(1, file=hostile)])
    fx.label(2, entry(1, file=hostile), "accept")
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")])

    for json_format in (True, False):
        result = fx.compare(capsys, json_format=json_format)
        assert result.exit_code == 0, result.stderr
        assert "\x1b" not in result.stdout + result.stderr
        assert "\u202e" not in result.stdout + result.stderr


# Interfaces: exit code 2 for invalid input (a reading: the spec names the code, not the cases).


@pytest.mark.parametrize(
    ("flags", "named"),
    [
        (("--price-input-usd-per-mtok", "15"), "--price-output-usd-per-mtok"),
        (("--price-input-usd-per-mtok", "15", "--price-output-usd-per-mtok", "abc"),
         "--price-output-usd-per-mtok"),
        (("--baseline", "no-such-directory"), "--baseline"),
        (("--labels", "no-such-file.jsonl"), "--labels"),
    ],
    ids=["lone-price", "non-numeric-price", "missing-baseline", "missing-labels"],
)  # fmt: skip
def test_p3_s13_interfaces_invalid_input_exits_2_naming_the_flag(
    fx: Fixture, capsys: pytest.CaptureFixture[str], flags: tuple[str, ...], named: str
) -> None:
    # A later flag wins in argparse, so these override the fixture's own paths.
    fx.judged("baseline", 2, HEAD_B, [(1, "ai:logic", "accept")])
    fx.judged("candidate", 2, HEAD_B, [(2, "ai:logic", "accept")])

    result = fx.compare(capsys, *flags)

    assert result.exit_code == USAGE_EXIT
    assert named in result.stderr, result.stderr
    assert result.stdout == ""
