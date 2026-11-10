import json
from pathlib import Path

import pytest
from compare_samples import HEAD_A, pr_report
from metrics_samples import key
from shadow_samples import entry, write_report

from review_bot.pr.cli import main
from review_bot.pr.reports import MAX_FILE_BYTES

LABEL = {
    "schema": "feedback/v1",
    "channel": "shadow",
    "repo": "owner/repo",
    "pr": 2,
    "fingerprint": "f" * 16,
    "file": key(1)[0],
    "rule_part": key(1)[1],
    "anchor": key(1)[2],
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


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    baseline, candidate = tmp_path / "baseline", tmp_path / "candidate"
    write_report(baseline, "2", pr_report(2, HEAD_A, entry(key(1))))
    write_report(candidate, "2", pr_report(2, HEAD_A, entry(key(1))))
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps(LABEL) + "\n", encoding="ascii")
    return baseline, candidate, labels


def args(dirs: tuple[Path, Path, Path], *extra: str) -> list[str]:
    baseline, candidate, labels = dirs
    return [
        "compare",
        "--baseline",
        str(baseline),
        "--candidate",
        str(candidate),
        "--labels",
        str(labels),
        *extra,
    ]


def test_compare_shows_text_and_exits_0(
    dirs: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(args(dirs))

    out = capsys.readouterr()
    assert exit_code == 0
    assert "compare: verdict precision=same recall=same" in out.out
    assert "compare: baseline cost=not available" in out.out
    assert out.err == ""


def test_compare_json_with_prices_estimates_the_cost(
    dirs: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        args(
            dirs,
            "--format",
            "json",
            "--price-input-usd-per-mtok",
            "15",
            "--price-output-usd-per-mtok",
            "75",
        )
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert out.isascii()
    assert json.loads(out)["baseline"]["cost"]["basis"] == "estimated"


def test_compare_with_one_price_exits_2_naming_the_other(
    dirs: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(args(dirs, "--price-input-usd-per-mtok", "15"))

    assert exit_code == 2
    assert "--price-output-usd-per-mtok" in capsys.readouterr().err


def test_compare_with_a_malformed_labels_line_exits_2_naming_it(
    dirs: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    dirs[2].write_text(json.dumps(LABEL) + "\n{not json\n", encoding="ascii")

    exit_code = main(args(dirs))

    out = capsys.readouterr()
    assert exit_code == 2
    assert "line 2 isn't a feedback/v1 record" in out.err
    assert out.out == ""


def test_compare_skips_a_report_that_isnt_shadow_v1_or_is_too_large_with_one_line_each(
    dirs: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    baseline, candidate, _ = dirs
    (baseline / "other.json").write_text('{"schema": "report/v1"}', encoding="ascii")
    (candidate / "big.json").write_bytes(b" " * (MAX_FILE_BYTES + 1))

    exit_code = main(args(dirs))

    err = capsys.readouterr().err.splitlines()
    assert exit_code == 0
    assert len(err) == 2
    assert "other.json: not a shadow/v1 report" in err[0]
    assert f"big.json: over {MAX_FILE_BYTES} bytes" in err[1]


@pytest.mark.parametrize("flag", ["--baseline", "--candidate"])
def test_compare_with_a_missing_reports_directory_exits_2_naming_its_flag(
    dirs: tuple[Path, Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag: str,
) -> None:
    argv = args(dirs)
    argv[argv.index(flag) + 1] = str(tmp_path / "missing")

    exit_code = main(argv)

    assert exit_code == 2
    assert f"{flag} " in capsys.readouterr().err
