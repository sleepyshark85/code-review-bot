"""P2-S12 AC6: the committed labelled set reproduces Phase 2's shadow precision offline.

Written from the spec's AC6 and the design's "How the gate is evidenced", not from `metrics`'
code. The three committed files under `tests/evals/labelled/` are read as plain JSON and the
numbers are recomputed here, independently of `review_bot.pr`:

- a unit is (repo, PR, file, rule part, anchor, index); `would-be.jsonl` holds one line per unit;
- the latest judgement per unit (by `recorded_at`) wins;
- precision is accepted / (accepted + rejected other than `duplicate`), the lower bound counts
  duplicates as rejections, coverage is judged / units, and the Wilson interval is at 95%.

The expected values are the AC2 numbers recorded in the P2-S12 section of
`docs/phases/phase-2/test-report.md`, from `metrics` over the local shadow reports on 2026-11-12.
If the labelled set changes, both the report and these constants change together.
"""

import json
import math
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from review_bot.secret_scan import scan_secrets

LABELLED = Path(__file__).resolve().parent / "labelled"
LABELS = LABELLED / "labels.jsonl"
WOULD_BE = LABELLED / "would-be.jsonl"
BASELINE = LABELLED / "baseline.json"
COMMITTED_FILES = (LABELS, WOULD_BE, BASELINE)

WOULD_BE_KEYS = {"repo", "pr", "file", "rule_part", "anchor", "index", "source", "origin"}
UNIT_KEYS = ("repo", "pr", "file", "rule_part", "anchor", "index")
FEEDBACK_KEYS = {
    "schema",
    "channel",
    "repo",
    "pr",
    "fingerprint",
    "file",
    "rule_part",
    "anchor",
    "index",
    "source",
    "severity",
    "label",
    "reason",
    "run_id",
    "model",
    "prompt_version",
    "recorded_at",
}
# Keys that would carry a finding's text, the labeller's reasoning or a person's identity.
FORBIDDEN_KEYS = {
    "body",
    "message",
    "rationale",
    "explanation",
    "comment",
    "note",
    "user",
    "username",
    "login",
    "author",
    "labeller",
    "labeler",
    "reviewer",
    "merged_by",
    "email",
}
REJECT_REASONS = ("wrong", "not_worth_it", "wrong_line", "duplicate")
Z_95 = 1.959963984540054
# The day the user turned comment mode on (2026-11-12, 04:34 UTC).
COMMENTS_ON = date(2026, 11, 12)

# AC2, as recorded in the test report.
EXPECTED_UNITS = 42
EXPECTED_ACCEPTED = 42
EXPECTED_REJECTED = 0
EXPECTED_DUPLICATES = 0
EXPECTED_PRS = 24
EXPECTED_LARGEST_PR = 3
EXPECTED_ORIGIN = {"backfill": 36, "event": 6}
EXPECTED_SOURCE = {"ai": 41, "rule": 1}
EXPECTED_RULE_PART = {
    "ai:logic": 8,
    "ai:maintainability": 3,
    "ai:reliability": 16,
    "ai:security": 6,
    "ai:testing": 8,
    "swallowed-exception": 1,
}
EXPECTED_REJECTED_SPLIT: dict[str, dict[str, int]] = {"origin": {}, "source": {}, "rule_part": {}}
EXPECTED_REJECT_REASONS = {"wrong": 0, "not_worth_it": 0, "wrong_line": 0, "duplicate": 0}
EXPECTED_COVERAGE = "100.0%"
EXPECTED_PRECISION = "100.0%"
EXPECTED_LOWER_BOUND = "100.0%"
EXPECTED_WILSON = ("91.6%", "100.0%")
EXPECTED_LARGEST_SHARE = "7.1%"

type Unit = tuple[Any, ...]
type Record = dict[str, Any]


def read_jsonl(path: Path) -> list[Record]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def unit_of(record: Record) -> Unit:
    return tuple(record[key] for key in UNIT_KEYS)


def latest_labels(labels: list[Record]) -> dict[Unit, Record]:
    latest: dict[Unit, Record] = {}
    for record in sorted(labels, key=lambda r: datetime.fromisoformat(r["recorded_at"])):
        latest[unit_of(record)] = record
    return latest


def percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def wilson_95(successes: int, trials: int) -> tuple[float, float]:
    observed, z2 = successes / trials, Z_95**2
    scale = 1 + z2 / trials
    centre = (observed + z2 / (2 * trials)) / scale
    half = Z_95 * math.sqrt(observed * (1 - observed) / trials + z2 / (4 * trials**2)) / scale
    return max(0.0, centre - half), min(1.0, centre + half)


def verdicts(judged: list[Record]) -> tuple[int, int, int]:
    accepted = sum(record["label"] == "accept" for record in judged)
    duplicates = sum(record["reason"] == "duplicate" for record in judged)
    return accepted, len(judged) - accepted - duplicates, duplicates


@pytest.fixture(scope="module")
def would_be() -> dict[Unit, Record]:
    return {unit_of(record): record for record in read_jsonl(WOULD_BE)}


@pytest.fixture(scope="module")
def judged(would_be: dict[Unit, Record]) -> list[tuple[Record, Record]]:
    """Each judged would-be comment, as (its latest shadow label, its would-be line)."""
    latest = latest_labels([r for r in read_jsonl(LABELS) if r["channel"] == "shadow"])
    return [(label, would_be[unit]) for unit, label in latest.items() if unit in would_be]


# Shape of the committed files.


def test_p2_s12_ac6_the_three_files_are_committed_together():
    for path in COMMITTED_FILES:
        assert path.is_file(), path


def test_p2_s12_ac6_would_be_has_exactly_the_eight_fields_on_every_line():
    for number, record in enumerate(read_jsonl(WOULD_BE), start=1):
        assert set(record) == WOULD_BE_KEYS, f"line {number}"
        assert isinstance(record["pr"], int) and record["pr"] >= 1, f"line {number}"
        assert isinstance(record["index"], int) and record["index"] >= 0, f"line {number}"
        assert record["source"] in {"ai", "rule"}, f"line {number}"
        assert record["origin"] in {"event", "backfill"}, f"line {number}"


def test_p2_s12_ac6_would_be_holds_one_line_per_unit():
    units = [unit_of(record) for record in read_jsonl(WOULD_BE)]

    assert len(units) == len(set(units)) == EXPECTED_UNITS


def test_p2_s12_ac6_every_label_judges_a_unit_in_would_be(would_be):
    orphans = [unit_of(r) for r in read_jsonl(LABELS) if unit_of(r) not in would_be]

    assert orphans == []


def test_p2_s12_ac6_every_label_is_an_accept_or_a_reject_with_a_known_reason():
    for number, record in enumerate(read_jsonl(LABELS), start=1):
        assert record["label"] in {"accept", "reject"}, f"line {number}"
        if record["label"] == "accept":
            assert record["reason"] is None, f"line {number}"
        else:
            assert record["reason"] in REJECT_REASONS, f"line {number}"


def test_p2_s12_ac6_labels_and_would_be_agree_on_source(judged):
    for label, line in judged:
        assert label["source"] == line["source"], unit_of(label)


# AC2's numbers, recomputed.


def test_p2_s12_ac6_count_and_coverage_reproduce_ac2(would_be, judged):
    assert len(would_be) == EXPECTED_UNITS
    assert len(judged) == EXPECTED_UNITS
    assert percent(len(judged) / len(would_be)) == EXPECTED_COVERAGE


def test_p2_s12_ac6_point_estimate_and_lower_bound_reproduce_ac2(judged):
    accepted, rejected, duplicates = verdicts([label for label, _ in judged])

    assert (accepted, rejected, duplicates) == (
        EXPECTED_ACCEPTED,
        EXPECTED_REJECTED,
        EXPECTED_DUPLICATES,
    )
    assert percent(accepted / (accepted + rejected)) == EXPECTED_PRECISION
    assert percent(accepted / (accepted + rejected + duplicates)) == EXPECTED_LOWER_BOUND


def test_p2_s12_ac6_wilson_interval_reproduces_ac2(judged):
    accepted, rejected, _ = verdicts([label for label, _ in judged])

    low, high = wilson_95(accepted, accepted + rejected)

    assert (percent(low), percent(high)) == EXPECTED_WILSON


def test_p2_s12_ac6_pr_count_and_largest_share_reproduce_ac2(judged):
    per_pr = Counter(
        (label["repo"], label["pr"]) for label, _ in judged if label["reason"] != "duplicate"
    )

    largest = max(per_pr.values())

    assert len(per_pr) == EXPECTED_PRS
    assert largest == EXPECTED_LARGEST_PR
    assert percent(largest / sum(per_pr.values())) == EXPECTED_LARGEST_SHARE


@pytest.mark.parametrize(
    ("trait", "expected"),
    [("origin", EXPECTED_ORIGIN), ("source", EXPECTED_SOURCE), ("rule_part", EXPECTED_RULE_PART)],
)
def test_p2_s12_ac6_splits_reproduce_ac2(judged, trait, expected):
    accepted = Counter(line[trait] for label, line in judged if label["label"] == "accept")
    rejected = Counter(line[trait] for label, line in judged if label["label"] == "reject")

    assert dict(accepted) == expected
    assert dict(rejected) == EXPECTED_REJECTED_SPLIT[trait]


def test_p2_s12_ac6_reject_reasons_and_wrong_line_reproduce_ac2(judged):
    reasons = Counter(label["reason"] for label, _ in judged if label["reason"] is not None)

    assert {reason: reasons[reason] for reason in REJECT_REASONS} == EXPECTED_REJECT_REASONS


# AC3's baseline, committed beside them.


def test_p2_s12_ac6_baseline_is_v1_and_ends_no_later_than_comments_on():
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert baseline["schema"] == "baseline/v1"
    assert date.fromisoformat(baseline["since"]) == date(2026, 11, 3)
    assert date.fromisoformat(baseline["until"]) <= COMMENTS_ON
    assert baseline["pr_count"] == 79
    assert baseline["open_to_merge_hours"] == {"median": 0.12, "p90": 3.94}
    assert baseline["commits_after_open"] == {"median": 0.0, "p90": 2.0}
    assert baseline["escaped_defects_per_merged_pr"] == 0.013


# No body, rationale or username in any line.


def lines_of(path: Path) -> list[Record]:
    if path.suffix == ".jsonl":
        return read_jsonl(path)
    return [json.loads(path.read_text(encoding="utf-8"))]


def keys_and_strings(value: object) -> tuple[set[str], list[str]]:
    """Every key at any depth, and every string value, of a parsed JSON value."""
    keys: set[str] = set()
    strings: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            keys.add(key)
            inner_keys, inner_strings = keys_and_strings(inner)
            keys |= inner_keys
            strings += inner_strings
    elif isinstance(value, list):
        for inner in value:
            inner_keys, inner_strings = keys_and_strings(inner)
            keys |= inner_keys
            strings += inner_strings
    elif isinstance(value, str):
        strings.append(value)
    return keys, strings


@pytest.mark.parametrize("path", COMMITTED_FILES, ids=lambda path: path.name)
def test_p2_s12_ac6_no_line_has_a_body_rationale_or_username_key(path):
    for number, record in enumerate(lines_of(path), start=1):
        keys, _ = keys_and_strings(record)

        assert keys & FORBIDDEN_KEYS == set(), f"{path.name} line {number}"


def test_p2_s12_ac6_labels_have_only_feedback_v1_keys():
    for number, record in enumerate(read_jsonl(LABELS), start=1):
        assert set(record) <= FEEDBACK_KEYS, f"line {number}"


@pytest.mark.parametrize("path", COMMITTED_FILES, ids=lambda path: path.name)
def test_p2_s12_ac6_no_value_is_prose_or_a_mention(path):
    """A comment body or a reason in words has spaces; a username is written as an @mention."""
    for number, record in enumerate(lines_of(path), start=1):
        _, strings = keys_and_strings(record)

        assert [s for s in strings if any(c.isspace() for c in s)] == [], f"line {number}"
        assert [s for s in strings if "@" in s] == [], f"line {number}"


@pytest.mark.parametrize("path", COMMITTED_FILES, ids=lambda path: path.name)
def test_p2_s12_ac6_the_owner_login_appears_only_as_the_repo_owner(path):
    for number, record in enumerate(lines_of(path), start=1):
        owner = record["repo"].split("/")[0]
        rest = {key: value for key, value in record.items() if key != "repo"}
        _, strings = keys_and_strings(rest)

        assert [s for s in strings if owner.lower() in s.lower()] == [], f"line {number}"


# The P3-S17 guard: no file under tests/ outside SECRET_TEST_FILES has a scanner hit.


@pytest.mark.parametrize("path", COMMITTED_FILES, ids=lambda path: path.name)
def test_p2_s12_ac6_committed_files_have_no_scanner_hit(path):
    assert scan_secrets(path.read_text(encoding="utf-8")) == ()


def test_p2_s12_ac6_this_file_has_no_scanner_hit():
    assert scan_secrets(Path(__file__).read_text(encoding="utf-8")) == ()
