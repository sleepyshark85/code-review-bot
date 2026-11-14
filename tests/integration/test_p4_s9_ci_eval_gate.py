"""P4-S9: CI runs the eval gate under this repository's policy file.

Written from the spec's AC8 and AC9 and the Phase 4 design ("Versioned prompts and the eval
gate"). Both files are read as the spec says: `ci.yml` with PyYAML, the policy with `tomllib` and
with the bot's own `parse_policy`, and `tests/evals/gate.toml` with `tomllib`. The `evals gate`
command CI runs is then run in process, offline, on the committed cases.

AC8's second half (once P4-S10 passes the policy to the review runs, their findings are unchanged
and `caps` shows the ceiling) needs S10's wiring and is S10's to test.
"""

import shlex
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from review_bot.evals import main as evals_main
from review_bot.governance.policy import parse_policy
from review_bot.settings import Settings

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
POLICY_PATH = ".github/review-bot-policy.toml"
POLICY = REPO_ROOT / POLICY_PATH
GATE_TOML = REPO_ROOT / "tests" / "evals" / "gate.toml"
DEFAULT_MODEL = Settings.model_fields["model"].default


def gate_job_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["gate"]["steps"]
    assert isinstance(steps, list)
    return steps


def evals_gate_argv() -> list[str]:
    """The one `review_bot.evals gate` command in the `gate` job, split as a shell would."""
    found = [
        words[words.index("review_bot.evals") :]
        for step in gate_job_steps()
        for line in str(step.get("run", "")).replace("\\\n", " ").splitlines()
        if "review_bot.evals" in (words := shlex.split(line, comments=True))
        and words[words.index("review_bot.evals") + 1 : words.index("review_bot.evals") + 2]
        == ["gate"]
    ]
    assert len(found) == 1, f"expected one `review_bot.evals gate` in the gate job: {found}"
    return found[0][2:]


def option(argv: list[str], name: str) -> str:
    values = [argv[i + 1] for i, word in enumerate(argv[:-1]) if word == name]
    values += [word.split("=", 1)[1] for word in argv if word.startswith(f"{name}=")]
    assert len(values) == 1, (name, argv)
    return values[0]


def policy_data() -> dict[str, Any]:
    return tomllib.loads(POLICY.read_text(encoding="utf-8"))


# --- AC8: CI's gate job runs `evals gate` under the repository's policy ---------------------------


def test_p4_s9_ac8_gate_job_runs_evals_gate_with_the_repository_policy() -> None:
    argv = evals_gate_argv()

    assert option(argv, "--policy") == POLICY_PATH
    assert option(argv, "--cases") == "tests/evals/cases"
    assert option(argv, "--suites") == "tests/evals/gate.toml"


def test_p4_s9_ac8_policy_has_only_version_tools_cost_and_models() -> None:
    assert set(policy_data()) == {"version", "tools", "cost", "models"}


def test_p4_s9_ac8_policy_allows_the_three_tools() -> None:
    tools = policy_data()["tools"]

    assert set(tools) == {"allowed"}
    assert sorted(tools["allowed"]) == ["glob", "grep", "read"]


def test_p4_s9_ac8_policy_cost_ceiling_is_ci_context_cap() -> None:
    assert policy_data()["cost"] == {"max_cost_usd": "0.60"}


def test_p4_s9_ac8_policy_has_one_model_entry_the_default_claude_model() -> None:
    [entry] = policy_data()["models"]

    assert (entry["provider"], entry["model"]) == ("claude", DEFAULT_MODEL)
    assert "diff" in entry["modes"]


def test_p4_s9_ac8_bot_parses_the_policy_as_stated() -> None:
    policy = parse_policy(POLICY.read_bytes(), source=POLICY_PATH)

    assert policy.version == policy_data()["version"]
    assert policy.tools == frozenset({"read", "grep", "glob"})
    assert policy.max_cost_usd == Decimal("0.60")
    assert [entry.route for entry in policy.models] == [f"claude:{DEFAULT_MODEL}"]
    assert policy.fallback is None


def test_p4_s9_ac8_ci_command_passes_offline_on_the_committed_files(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """What CI's step does, from the repository root, in process."""
    monkeypatch.chdir(REPO_ROOT)
    argv = evals_gate_argv()

    exit_code = evals_main(["gate", *argv])
    output = "".join(capsys.readouterr())

    assert exit_code == 0, output


# --- AC9: the policy lists `context` only together with a Claude context suite --------------------


def committed_suites() -> list[dict[str, Any]]:
    suites: list[dict[str, Any]] = tomllib.loads(GATE_TOML.read_text(encoding="utf-8"))["suite"]
    return suites


def has_suite(provider: str, model: str, mode: str) -> bool:
    return any(
        (suite["provider"], suite["model"], suite["mode"]) == (provider, model, mode)
        for suite in committed_suites()
    )


def test_p4_s9_ac9_every_mode_the_policy_lists_has_a_suite() -> None:
    for entry in policy_data()["models"]:
        for mode in entry["modes"]:
            assert has_suite(entry["provider"], entry["model"], mode), (entry, mode)


def test_p4_s9_ac9_context_in_the_policy_comes_with_a_claude_context_suite() -> None:
    [entry] = policy_data()["models"]

    if "context" in entry["modes"]:
        assert has_suite("claude", DEFAULT_MODEL, "context")
    else:
        assert entry["modes"] == ["diff"]


def test_p4_s9_ac9_p3_s15_has_merged_so_context_and_its_suite_are_both_in() -> None:
    """Out of scope: whichever of S9 and P3-S15 merges second adds them; P3-S15 merged first."""
    [entry] = policy_data()["models"]

    assert sorted(entry["modes"]) == ["context", "diff"]
    assert has_suite("claude", DEFAULT_MODEL, "context")
