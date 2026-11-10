"""P3-S10 AC9: the `domain-money` eval case scores its rule bug from the case repository's config.

Written from the spec's AC9, design.md ("Spend and caps": the 9 new context cases, each `repo`
being the diff's base) and P3-S5's case format, while the developer built the slice and before any
of its code was read. Runs go through `review_bot.evals.main(["replay", ...])` in process, on a
temporary copy of the cases, through P3-S5's `replay` fixture and `copy_cases`.

Reading: "from the case repository's config" means replay reads `.review-bot.toml` from the case's
`repo` directory, in either mode, as `run` reads it from the base commit. So the same case with no
config, or with a config whose paths don't match, misses the rule bug.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from test_p3_s5_context_eval_cases import (
    CASES,
    PHASE1_CASES,
    Replay,
    case_json,
    copy_cases,
    diff_text,
)
from test_p3_s15_close_phase_3 import ACCEPTED_DIFF_MISSES

from review_bot.evals import main

pytestmark = pytest.mark.integration

CASE = "domain-money"
RULE = "decimal-money"
CONFIG = ".review-bot.toml"
SHIPPING = "billing/shipping.py"
FLOAT_LINE = "PER_KG_RATE = 0.85"
MODES = ("diff", "context")


@pytest.fixture
def replay(capsys: pytest.CaptureFixture[str]) -> Callable[..., Replay]:
    """`replay ARGS` in process, as P3-S5's fixture runs it, with stdout and stderr together."""

    def run(*args: str | Path) -> Replay:
        capsys.readouterr()
        try:
            exit_code = main(["replay", *map(str, args)])
        except SystemExit as exc:  # argparse reports usage errors this way
            exit_code = exc.code if isinstance(exc.code, int) else 2
        captured = capsys.readouterr()
        return Replay(exit_code, captured.out + captured.err)

    return run


def case_repo(root: Path = CASES) -> Path:
    return root / CASE / str(case_json(CASE, root)["repo"])


# The case.


def test_p3_s10_ac9_case_has_a_repo_and_one_rule_bug_on_the_float_line() -> None:
    case = case_json(CASE)

    assert case["repo"] == "repo"
    (bug,) = case["bugs"]
    assert (bug["id"], bug["source"], bug["file"]) == (RULE, "rule", SHIPPING)
    (window,) = bug["lines"]
    added = [line for line in diff_text(CASE).splitlines() if line.startswith("+")]
    assert f"+{FLOAT_LINE}" in added
    assert window[0] == window[1]


def test_p3_s10_ac9_case_repo_config_switches_the_rule_on_for_billing() -> None:
    config = (case_repo() / CONFIG).read_text(encoding="utf-8")

    assert "[[domain_rules]]" in config
    assert f'rule = "{RULE}"' in config
    assert '"billing/**"' in config


def test_p3_s10_ac9_case_repo_is_the_diffs_base() -> None:
    """The repo holds the file before the change: not the added float line."""
    base = (case_repo() / SHIPPING).read_text(encoding="utf-8")

    assert FLOAT_LINE not in base
    removed = [line[1:] for line in diff_text(CASE).splitlines() if line.startswith("-")]
    assert all(line in base for line in removed if not line.startswith("--"))


def test_p3_s10_ac9_case_is_listed_in_the_committed_cases() -> None:
    found = {path.parent.name for path in CASES.glob("*/case.json")}

    assert CASE in found


# Replay, in either mode.


@pytest.mark.parametrize("mode", MODES)
def test_p3_s10_ac9_replay_scores_the_rule_bug_caught(
    replay: Callable[..., Replay], tmp_path: Path, mode: str
) -> None:
    result = replay("--cases", copy_cases(tmp_path, CASE), "--mode", mode)

    assert result.exit_code == 0, result.output
    assert result.recall("rules") == "1/1"
    assert not result.failures()


@pytest.mark.parametrize("mode", MODES)
def test_p3_s10_ac9_without_the_repo_config_the_rule_bug_is_missed(
    replay: Callable[..., Replay], tmp_path: Path, mode: str
) -> None:
    """The rule comes from the case's config, not from replay switching it on for every case."""
    root = copy_cases(tmp_path, CASE)
    (case_repo(root) / CONFIG).unlink()

    result = replay("--cases", root, "--mode", mode)

    assert result.exit_code == 1, result.output
    assert result.recall("rules") == "0/1"
    assert {case for case, _ in result.failures()} == {CASE}, result.output


@pytest.mark.parametrize("mode", MODES)
def test_p3_s10_ac9_config_for_other_paths_misses_the_rule_bug(
    replay: Callable[..., Replay], tmp_path: Path, mode: str
) -> None:
    root = copy_cases(tmp_path, CASE)
    config = case_repo(root) / CONFIG
    config.write_text(
        config.read_text(encoding="utf-8").replace('"billing/**"', '"payments/**"'),
        encoding="utf-8",
    )

    result = replay("--cases", root, "--mode", mode)

    assert result.exit_code == 1, result.output
    assert result.recall("rules") == "0/1"


@pytest.mark.parametrize("mode", MODES)
def test_p3_s10_ac9_rule_isnt_switched_on_by_a_config_beside_the_case(
    replay: Callable[..., Replay], tmp_path: Path, mode: str
) -> None:
    """Only the case's `repo` holds its config: not the case directory or the cases root."""
    root = copy_cases(tmp_path, CASE)
    config = case_repo(root) / CONFIG
    text = config.read_text(encoding="utf-8")
    config.unlink()
    (root / CASE / CONFIG).write_text(text, encoding="utf-8")
    (root / CONFIG).write_text(text, encoding="utf-8")

    result = replay("--cases", root, "--mode", mode)

    assert result.exit_code == 1, result.output
    assert result.recall("rules") == "0/1"


# An invalid case config: no case scored, exit 2, naming the case file, the config and the problem.

INVALID_CASE_CONFIGS = {
    # name: (the config's content, a pattern naming the problem)
    "unknown_rule": (
        '[[domain_rules]]\nrule = "float-money"\npaths = ["billing/**"]\n',
        r"domain_rules\[0\].*rule|rule.*float-money",
    ),
    "not_toml": ('[[domain_rules]]\nrule = "decimal-money\n', r"TOML"),
    "over_64_kb": (
        "# padding\n" * 6554 + '[[domain_rules]]\nrule = "decimal-money"\n',
        r"(?i)65,?536|64 ?KB",
    ),
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("content", "problem"), list(INVALID_CASE_CONFIGS.values()), ids=list(INVALID_CASE_CONFIGS)
)
def test_p3_s10_ac9_invalid_case_config_exits_2_scoring_nothing(
    replay: Callable[..., Replay], tmp_path: Path, mode: str, content: str, problem: str
) -> None:
    root = copy_cases(tmp_path, CASE)
    (case_repo(root) / CONFIG).write_text(content, encoding="utf-8")

    result = replay("--cases", root, "--mode", mode)

    assert result.exit_code == 2, result.output
    output = result.output
    assert f"{CASE}/case.json" in output.replace("\\", "/"), output
    assert CONFIG in output, output
    assert re.search(problem, output), output
    assert not re.search(r"\brules \d+/\d+|\bcaught\b|^case \S+:", output, re.MULTILINE), output


# Phase 1's cases are unaffected.


@pytest.mark.parametrize("mode", MODES)
def test_p3_s10_ac9_phase_1_cases_score_as_before(
    replay: Callable[..., Replay], tmp_path: Path, mode: str
) -> None:
    """With no recordings, Phase 1's rule bugs are all caught and nothing fails."""
    result = replay("--cases", copy_cases(tmp_path, *PHASE1_CASES), "--mode", mode)

    assert result.exit_code == 0, result.output
    assert not result.failures(), result.output


def test_p3_s10_ac9_committed_cases_still_exit_1_only_for_the_accepted_misses(
    replay: Callable[..., Replay],
) -> None:
    """P3-S5 AC5's check, on the committed cases, with `domain-money` among them.

    `domain-money` isn't among the misses: its rule bug is caught from the case's config.
    """
    result = replay("--cases", CASES)

    assert result.exit_code == 1, result.output
    assert result.failures() == ACCEPTED_DIFF_MISSES, result.output


def test_p3_s10_ac9_phase_1_cases_name_no_domain_rule() -> None:
    """No Phase 1 case gained a `repo` or a config, so their rule scores can't move."""
    for case_id in PHASE1_CASES:
        case = case_json(case_id)
        assert "repo" not in case, case_id
        assert not any(bug["id"] == RULE for bug in case["bugs"]), case_id
        assert not list((CASES / case_id).rglob(CONFIG)), case_id
    assert json.dumps(case_json(CASE)).count(RULE) == 1
