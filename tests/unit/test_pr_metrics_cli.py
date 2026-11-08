import json
from pathlib import Path
from typing import Any

import pytest
from shadow_samples import K1, entry, report, write_report

from review_bot.pr.cli import main

LABEL = {
    "schema": "feedback/v1",
    "channel": "shadow",
    "repo": "owner/repo",
    "pr": 7,
    "fingerprint": "f" * 16,
    "file": K1[0],
    "rule_part": K1[1],
    "anchor": K1[2],
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


def write_labels(path: Path, *records: dict[str, Any]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="ascii")
    return path


@pytest.fixture
def reports(tmp_path: Path) -> Path:
    directory = tmp_path / "reports"
    write_report(directory, "run-a", report(entry(K1)))
    return directory


def test_metrics_shows_text_by_default_and_exits_0(
    reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = write_labels(tmp_path / "labels.jsonl", LABEL)

    exit_code = main(["metrics", "--reports", str(reports), "--labels", str(labels)])

    out = capsys.readouterr()
    assert exit_code == 0
    assert "metrics: shadow_precision units=1 judged=1 coverage=100.0%" in out.out
    assert "metrics: cost=not priced" in out.out
    assert out.err == ""


def test_metrics_json_reads_several_labels_files_after_one_flag(
    reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = write_labels(tmp_path / "a.jsonl", LABEL)
    later = LABEL | {"label": "reject", "reason": "wrong", "recorded_at": "2026-11-09T00:00:00Z"}
    second = write_labels(tmp_path / "b.jsonl", later)
    argv = ["metrics", "--reports", str(reports), "--labels", str(first), str(second)]

    exit_code = main([*argv, "--format", "json"])

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert (document["shadow_precision"]["accepted"], document["shadow_precision"]["rejected"]) == (
        0,
        1,
    )


def test_metrics_with_both_prices_shows_the_cost(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prices = ["--price-input-usd-per-mtok", "15", "--price-output-usd-per-mtok", "75"]

    exit_code = main(["metrics", "--reports", str(reports), *prices, "--format", "json"])

    # The sample report used 1000 input and 100 output tokens.
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["cost"] == {
        "usd": "0.02",
        "per_accepted_finding_usd": None,
    }


@pytest.mark.parametrize(
    ("given", "missing"),
    [
        ("--price-input-usd-per-mtok", "--price-output-usd-per-mtok"),
        ("--price-output-usd-per-mtok", "--price-input-usd-per-mtok"),
    ],
)
def test_metrics_with_one_price_exits_2_naming_the_missing_flag(
    reports: Path, given: str, missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["metrics", "--reports", str(reports), given, "15"])

    assert exit_code == 2
    assert f"{missing} is missing" in capsys.readouterr().err


@pytest.mark.parametrize("price", ["-1", "abc", "nan", "inf", "1e3", "", chr(0x661)])
def test_metrics_with_an_invalid_price_exits_2_naming_the_flag(
    reports: Path, price: str, capsys: pytest.CaptureFixture[str]
) -> None:
    prices = ["--price-input-usd-per-mtok", price, "--price-output-usd-per-mtok", "75"]

    exit_code = main(["metrics", "--reports", str(reports), *prices])

    assert exit_code == 2
    assert "--price-input-usd-per-mtok" in capsys.readouterr().err


def test_metrics_with_until_before_since_exits_2(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    window = ["--since", "2026-11-08", "--until", "2026-11-01"]

    exit_code = main(["metrics", "--reports", str(reports), *window])

    assert exit_code == 2
    assert "--until 2026-11-01 is before --since 2026-11-08" in capsys.readouterr().err


def test_metrics_with_reports_that_isnt_a_directory_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["metrics", "--reports", str(tmp_path / "missing")])

    assert exit_code == 2
    assert "--reports" in capsys.readouterr().err


def test_metrics_skips_bad_files_and_names_orphans_one_stderr_line_each(
    reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (reports / "broken.json").write_text("{", encoding="ascii")
    bad = write_labels(tmp_path / "bad.jsonl", LABEL | {"channel": "email"})
    orphan = write_labels(tmp_path / "orphan.jsonl", LABEL | {"pr": 99})

    exit_code = main(["metrics", "--reports", str(reports), "--labels", str(bad), str(orphan)])

    err = capsys.readouterr().err.splitlines()
    assert exit_code == 0
    assert err == [
        f"review-bot pr: skipped {reports / 'broken.json'}: not JSON",
        f"review-bot pr: skipped {bad}: line 1 isn't a feedback/v1 record (channel: Input "
        "should be 'shadow', 'reaction' or 'resolution')",
        "review-bot pr: orphan shadow label for owner/repo#99, fingerprint ffffffffffffffff: no "
        "report in --reports has its finding, so no number counts it. Download that pull "
        "request's reports too.",
    ]
