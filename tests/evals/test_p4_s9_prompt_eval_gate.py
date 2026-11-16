"""P4-S9: gate prompt versions and models on evals.

Written from the spec's AC1 to AC7 and Limits, the Phase 4 design ("Versioned prompts and the eval
gate") and ADR 0007, before reading the implementation. Every run goes through
`review_bot.evals.main` in process: `gate` over the committed cases and `tests/evals/gate.toml`, or
over a temporary copy of some cases (recordings included) with a `gate.toml` written here. `live`
runs over `fake_openai`'s scripted transport or a fake `sdk_call`; nothing calls a model.

Output reading: the spec fixes the exit codes, the phrases AC1 and AC4 quote, and what each line
names; not the layout. A failure "names the suite, the case and the reason" when one output block
(a line at column 0 and the indented lines under it) holds all three.
"""

import json
import re
import shutil
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fake_openai import KEY, completion, fake, findings_content, use_fake_transport
from fake_sdk import findings_json, result_message

from review_bot import prompt
from review_bot.evals import main
from review_bot.settings import Settings

pytestmark = pytest.mark.integration

CASES = Path(__file__).resolve().parent / "cases"
GATE_TOML = Path(__file__).resolve().parent / "gate.toml"
DEFAULT_MODEL = Settings.model_fields["model"].default
DEFAULT_ROUTE = f"claude:{DEFAULT_MODEL}"
PASSED, FAILED, INVALID = 0, 1, 2

PHASE1_CASES = (
    "sample",
    "clean",
    "injection-instruction",
    "injection-canary",
    "injection-delimiter",
)
DOUBLE_TAX = ("sample", "double-tax")
APPROVER = "trung"
APPROVED_ON = "2026-11-07"
TEXT_CHANGED = "its text changed; give it a new version"
WITHIN_BUDGET = ("--max-usd", "1000", "--max-cost-usd", "0.10")
PRICES = ("--price-input-usd-per-mtok", "1", "--price-output-usd-per-mtok", "1")


# --- gate.toml ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Miss:
    case: str
    bug: str
    approved_by: str | None = APPROVER
    approved_on: str | None = APPROVED_ON


@dataclass(frozen=True, slots=True)
class Suite:
    cases: Sequence[str] = PHASE1_CASES
    provider: str = "claude"
    model: str = DEFAULT_MODEL
    mode: str = "diff"
    accepted: Sequence[Miss] = (Miss(*DOUBLE_TAX),)
    extra: Mapping[str, str] = field(default_factory=dict)

    def toml(self) -> str:
        lines = [
            "[[suite]]",
            f'provider = "{self.provider}"',
            f'model = "{self.model}"',
            f'mode = "{self.mode}"',
            "cases = [" + ", ".join(f'"{case}"' for case in self.cases) + "]",
            *(f"{key} = {value}" for key, value in dict(self.extra).items()),
        ]
        for miss in self.accepted:
            lines += ["[[suite.accepted_miss]]", f'case = "{miss.case}"', f'bug = "{miss.bug}"']
            if miss.approved_by is not None:
                lines.append(f'approved_by = "{miss.approved_by}"')
            if miss.approved_on is not None:
                lines.append(f"approved_on = {miss.approved_on}")
        return "\n".join(lines) + "\n"


def write_suites(tmp_path: Path, *suites: Suite, head: str = "") -> Path:
    path = tmp_path / "gate.toml"
    path.write_text(head + "\n".join(suite.toml() for suite in suites), encoding="utf-8")
    return path


def policy_file(tmp_path: Path, *models: tuple[str, str, Sequence[str]]) -> Path:
    """A `policy/v1` file listing `models` as (provider, model, modes)."""
    lines = ['version = "s9-test"']
    for provider, model, modes in models:
        lines += [
            "[[models]]",
            f'provider = "{provider}"',
            f'model = "{model}"',
            "modes = [" + ", ".join(f'"{mode}"' for mode in modes) + "]",
        ]
    path = tmp_path / "policy.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- Cases and recordings -------------------------------------------------------------------------


def read_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def copy_with_recordings(tmp_path: Path, case_ids: Iterable[str] = PHASE1_CASES) -> Path:
    """A `--cases` directory holding `case_ids` as committed, recordings included."""
    root = tmp_path / "cases"
    for case_id in case_ids:
        shutil.copytree(CASES / case_id, root / case_id)
    for case_id in case_ids:
        diff = str(read_json(CASES / case_id / "case.json")["diff"])
        target = (root / case_id / diff).resolve()
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile((CASES / case_id / diff).resolve(), target)
    return root


def copy_without_recordings(tmp_path: Path, *case_ids: str) -> Path:
    root = copy_with_recordings(tmp_path, case_ids)
    for recordings in root.glob("*/recordings"):
        shutil.rmtree(recordings)
    return root


def recording(root: Path, case_id: str, folder: str = "live") -> Path:
    return root / case_id / "recordings" / folder / "recording.json"


def rewrite_recording(root: Path, case_id: str, **fields: Any) -> None:
    path = recording(root, case_id)
    path.write_text(json.dumps(read_json(path) | fields, indent=2), encoding="utf-8")


# --- Running the commands -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Run:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    def blocks(self) -> list[str]:
        """The output split at each line starting at column 0."""
        found: list[list[str]] = []
        for line in self.output.splitlines():
            if not found or (line and not line[0].isspace()):
                found.append([])
            found[-1].append(line)
        return ["\n".join(block) for block in found]

    def block_naming(self, *parts: str) -> str:
        """The one block that holds every part; fails, showing the output, if none does."""
        matching = [block for block in self.blocks() if all(part in block for part in parts)]
        assert matching, f"no block names {parts!r}:\n{self.output}"
        return matching[0]


def run_evals(capsys: pytest.CaptureFixture[str], *args: str | Path, **seams: Any) -> Run:
    capsys.readouterr()
    try:
        exit_code = main([*map(str, args)], **seams)
    except SystemExit as exc:  # argparse reports usage errors this way
        exit_code = exc.code if isinstance(exc.code, int) else INVALID
    captured = capsys.readouterr()
    return Run(exit_code, captured.out, captured.err)


def gate(
    capsys: pytest.CaptureFixture[str],
    cases: Path,
    suites: Path,
    *flags: str | Path,
) -> Run:
    return run_evals(capsys, "gate", "--cases", cases, "--suites", suites, *flags)


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary working directory outside CI, so `live` runs and its audit stays out of git."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    for name in ("CI", "GITHUB_EVENT_NAME", "REVIEW_BOT_POLICY", "REVIEW_BOT_MAX_COST_USD"):
        monkeypatch.delenv(name, raising=False)
    return work


# --- AC1: each prompt version is pinned to its text -----------------------------------------------


def test_p4_s9_ac1_fingerprints_pin_the_diff_and_the_current_context_version() -> None:
    assert prompt.PROMPT_VERSION == "p1-v1"
    assert {prompt.PROMPT_VERSION, prompt.CONTEXT_PROMPT_VERSION} <= set(prompt.PROMPT_FINGERPRINTS)


def test_p4_s9_ac1_unchanged_prompts_pass_the_fingerprint_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = gate(capsys, CASES, GATE_TOML)

    assert TEXT_CHANGED not in run.output, run.output


def test_p4_s9_ac1_diff_prompt_text_changed_without_a_new_version_exits_1_naming_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prompt, "_SYSTEM_PROMPT", prompt._SYSTEM_PROMPT + "Be brief.\n")

    run = gate(capsys, CASES, GATE_TOML)

    assert run.exit_code == FAILED, run.output
    assert TEXT_CHANGED in run.block_naming(prompt.PROMPT_VERSION, TEXT_CHANGED)


def test_p4_s9_ac1_one_character_change_is_enough(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    text = prompt._SYSTEM_PROMPT
    monkeypatch.setattr(prompt, "_SYSTEM_PROMPT", text.replace("senior", "Senior", 1))

    run = gate(capsys, CASES, GATE_TOML)

    assert run.exit_code == FAILED, run.output
    run.block_naming(prompt.PROMPT_VERSION, TEXT_CHANGED)


def test_p4_s9_ac1_changed_pin_for_the_current_version_exits_1_naming_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same mismatch seen from the other side: the pin no longer matches today's text."""
    monkeypatch.setitem(prompt.PROMPT_FINGERPRINTS, prompt.PROMPT_VERSION, "0" * 64)

    run = gate(capsys, CASES, GATE_TOML)

    assert run.exit_code == FAILED, run.output
    run.block_naming(prompt.PROMPT_VERSION, TEXT_CHANGED)


def test_p4_s9_ac1_context_prompt_text_changed_names_the_context_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    text = prompt._CONTEXT_SYSTEM_PROMPT
    monkeypatch.setattr(prompt, "_CONTEXT_SYSTEM_PROMPT", text + "Be brief.\n")

    run = gate(capsys, CASES, GATE_TOML)

    assert run.exit_code == FAILED, run.output
    run.block_naming(prompt.CONTEXT_PROMPT_VERSION, TEXT_CHANGED)
    assert prompt.PROMPT_VERSION not in run.block_naming(TEXT_CHANGED), run.output


def test_p4_s9_ac1_a_text_change_fails_even_with_an_empty_suite_list(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pin is checked on its own, not only through stale recordings."""
    monkeypatch.setattr(prompt, "_SYSTEM_PROMPT", prompt._SYSTEM_PROMPT + "Be brief.\n")
    root = copy_with_recordings(tmp_path)

    run = gate(capsys, root, write_suites(tmp_path, Suite()))

    assert run.exit_code == FAILED, run.output
    run.block_naming(prompt.PROMPT_VERSION, TEXT_CHANGED)


# --- AC2: the committed suites pass on today's committed recordings -------------------------------


def committed_suites() -> list[dict[str, Any]]:
    suites: list[dict[str, Any]] = tomllib.loads(GATE_TOML.read_text(encoding="utf-8"))["suite"]
    return suites


def the_suite(provider: str, model: str, mode: str) -> dict[str, Any]:
    found = [
        suite
        for suite in committed_suites()
        if (suite["provider"], suite["model"], suite["mode"]) == (provider, model, mode)
    ]
    assert len(found) == 1, committed_suites()
    return found[0]


def test_p4_s9_ac2_committed_diff_suite_holds_the_phase_1_cases_and_the_accepted_miss() -> None:
    suite = the_suite("claude", DEFAULT_MODEL, "diff")

    assert set(PHASE1_CASES) <= set(suite["cases"])
    accepted = {(miss["case"], miss["bug"]): miss for miss in suite["accepted_miss"]}
    assert DOUBLE_TAX in accepted
    assert accepted[DOUBLE_TAX]["approved_by"]
    assert str(accepted[DOUBLE_TAX]["approved_on"])


def test_p4_s9_ac2_every_accepted_miss_has_an_approver_and_a_date() -> None:
    for suite in committed_suites():
        for miss in suite.get("accepted_miss", []):
            assert miss["approved_by"] and str(miss["approved_on"]), (suite["mode"], miss)
            assert miss["case"] in suite["cases"], (suite["mode"], miss)


def test_p4_s9_ac2_gate_on_the_committed_suites_and_recordings_exits_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = gate(capsys, CASES, GATE_TOML)

    assert run.exit_code == PASSED, run.output


def test_p4_s9_ac2_the_specs_one_suite_passes_on_the_committed_recordings(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """AC2's file exactly: one Claude diff suite, five cases, `double-tax` accepted."""
    run = gate(capsys, CASES, write_suites(tmp_path, Suite()))

    assert run.exit_code == PASSED, run.output


# --- AC3: missing, stale, other-model recordings and unaccepted misses ---------------------------


def assert_case_fails(run: Run, case_id: str, *reason: str) -> None:
    assert run.exit_code == FAILED, run.output
    block = run.block_naming(case_id, *reason)
    assert DEFAULT_MODEL in block and "diff" in block, f"the suite isn't named:\n{block}"


def test_p4_s9_ac3_missing_live_recording_exits_1_naming_suite_case_and_reason(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    recording(root, "clean").unlink()

    run = gate(capsys, root, write_suites(tmp_path, Suite()))

    assert_case_fails(run, "clean", "missing")


def test_p4_s9_ac3_synthetic_recording_alone_is_not_live_evidence(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    recording(root, "sample").unlink()
    assert (root / "sample" / "recordings" / "synthetic").is_dir()

    run = gate(capsys, root, write_suites(tmp_path, Suite()))

    assert_case_fails(run, "sample", "missing")


@pytest.mark.parametrize("field", ["case_sha256", "diff_sha256", "prompt_sha256"])
def test_p4_s9_ac3_stale_recording_exits_1_naming_the_stale_field(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str
) -> None:
    root = copy_with_recordings(tmp_path)
    rewrite_recording(root, "injection-canary", **{field: "0" * 64})

    run = gate(capsys, root, write_suites(tmp_path, Suite()))

    assert_case_fails(run, "injection-canary", "stale", field)


def test_p4_s9_ac3_stale_prompt_version_is_named(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    rewrite_recording(root, "injection-delimiter", prompt_version="p1-v0")

    run = gate(capsys, root, write_suites(tmp_path, Suite()))

    assert_case_fails(run, "injection-delimiter", "stale", "prompt_version")


def test_p4_s9_ac3_two_stale_fields_are_both_named(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    rewrite_recording(root, "clean", diff_sha256="0" * 64, case_sha256="1" * 64)

    run = gate(capsys, root, write_suites(tmp_path, Suite()))

    assert_case_fails(run, "clean", "stale", "diff_sha256", "case_sha256")


def test_p4_s9_ac3_another_models_recording_exits_1_naming_the_case(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    rewrite_recording(root, "injection-instruction", model="claude-other-model-9")

    run = gate(capsys, root, write_suites(tmp_path, Suite()))

    assert_case_fails(run, "injection-instruction", "claude-other-model-9")


def test_p4_s9_ac3_a_miss_that_isnt_accepted_exits_1_naming_the_bug(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)

    run = gate(capsys, root, write_suites(tmp_path, Suite(accepted=())))

    assert_case_fails(run, "sample", "double-tax")


def test_p4_s9_ac3_a_miss_accepted_for_another_bug_still_fails(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    other_bug = [bug["id"] for bug in read_json(CASES / "sample" / "case.json")["bugs"]]
    other_bug.remove("double-tax")

    run = gate(
        capsys, root, write_suites(tmp_path, Suite(accepted=(Miss("sample", other_bug[0]),)))
    )

    assert_case_fails(run, "sample", "double-tax")


def test_p4_s9_ac3_a_new_miss_beside_the_accepted_one_still_fails(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Emptying every run of `injection-canary` misses its planted bug, which isn't accepted."""
    root = copy_with_recordings(tmp_path)
    runs = read_json(recording(root, "injection-canary"))["runs"]
    for run in runs:
        run["findings"] = []
    rewrite_recording(root, "injection-canary", runs=runs)

    run = gate(capsys, root, write_suites(tmp_path, Suite()))

    assert_case_fails(run, "injection-canary", "credit-adds-to-balance")


def test_p4_s9_ac3_every_failure_is_reported_not_only_the_first(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    recording(root, "clean").unlink()
    rewrite_recording(root, "injection-canary", diff_sha256="0" * 64)

    run = gate(capsys, root, write_suites(tmp_path, Suite()))

    assert_case_fails(run, "clean", "missing")
    assert_case_fails(run, "injection-canary", "diff_sha256")


# --- AC4: every model and mode the policy allows has a suite with the default's cases -------------


def test_p4_s9_ac4_policy_model_with_no_suite_exits_1(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    policy = policy_file(tmp_path, ("claude", DEFAULT_MODEL, ["diff"]), ("openai", "m1", ["diff"]))

    run = gate(capsys, root, write_suites(tmp_path, Suite()), "--policy", policy)

    assert run.exit_code == FAILED, run.output
    assert "no suite for openai:m1 in diff mode" in run.output


def test_p4_s9_ac4_policy_mode_with_no_suite_exits_1(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    policy = policy_file(tmp_path, ("claude", DEFAULT_MODEL, ["diff", "context"]))

    run = gate(capsys, root, write_suites(tmp_path, Suite()), "--policy", policy)

    assert run.exit_code == FAILED, run.output
    assert f"no suite for {DEFAULT_ROUTE} in context mode" in run.output


def test_p4_s9_ac4_suite_lacking_the_default_suites_cases_exits_1_naming_them(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    policy = policy_file(tmp_path, ("claude", DEFAULT_MODEL, ["diff"]), ("openai", "m1", ["diff"]))
    partial = Suite(cases=("sample", "clean"), provider="openai", model="m1", accepted=())

    run = gate(capsys, root, write_suites(tmp_path, Suite(), partial), "--policy", policy)

    assert run.exit_code == FAILED, run.output
    block = run.block_naming("m1", "injection-instruction")
    for missing in ("injection-instruction", "injection-canary", "injection-delimiter"):
        assert missing in block, block


def test_p4_s9_ac4_without_a_policy_the_default_model_needs_a_diff_suite(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    only_other = Suite(provider="openai", model="m1")

    run = gate(capsys, root, write_suites(tmp_path, only_other))

    assert run.exit_code == FAILED, run.output
    assert f"no suite for {DEFAULT_ROUTE} in diff mode" in run.output


def test_p4_s9_ac4_policy_with_only_the_default_model_in_diff_mode_passes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    policy = policy_file(tmp_path, ("claude", DEFAULT_MODEL, ["diff"]))

    run = gate(capsys, root, write_suites(tmp_path, Suite()), "--policy", policy)

    assert run.exit_code == PASSED, run.output


def test_p4_s9_ac4_invalid_policy_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    policy = tmp_path / "policy.toml"
    policy.write_text('version = "x"\nfoo = 1\n', encoding="utf-8")

    run = gate(capsys, root, write_suites(tmp_path, Suite()), "--policy", policy)

    assert run.exit_code == INVALID, run.output
    assert "foo" in run.output


# --- AC5: another provider's recordings have their own folders ------------------------------------


@pytest.fixture
def openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", KEY)


def live_openai(
    capsys: pytest.CaptureFixture[str], root: Path, *flags: str, mode: str = "diff"
) -> Run:
    return run_evals(
        capsys, "live", "--cases", root, "--mode", mode, "--provider", "openai", "--model", "m1",
        "--runs", "1", "--max-calls", "5", *WITHIN_BUDGET, *PRICES, *flags,
    )  # fmt: skip


def test_p4_s9_ac5_openai_live_diff_recording_goes_to_live_openai_with_model_m1(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openai_key
) -> None:
    root = copy_without_recordings(tmp_path, "clean")
    transport = fake(completion(findings_content()))
    use_fake_transport(monkeypatch, transport)

    run = live_openai(capsys, root)

    assert run.exit_code in (PASSED, FAILED), run.output
    assert len(transport.sent) == 1
    data = read_json(recording(root, "clean", "live-openai"))
    assert (data["case_id"], data["kind"], data["model"]) == ("clean", "live", "m1")
    assert not recording(root, "clean", "live").exists(), "Claude's path was written"
    assert sorted(p.name for p in (root / "clean" / "recordings").iterdir()) == ["live-openai"]


def test_p4_s9_ac5_openai_live_context_recording_goes_to_live_openai_context(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openai_key
) -> None:
    root = copy_without_recordings(tmp_path, "clean-context")
    use_fake_transport(monkeypatch, fake(completion(findings_content())))

    run = live_openai(capsys, root, mode="context")

    assert run.exit_code in (PASSED, FAILED), run.output
    data = read_json(recording(root, "clean-context", "live-openai-context"))
    assert (data["model"], data["mode"]) == ("m1", "context")
    folders = sorted(p.name for p in (root / "clean-context" / "recordings").iterdir())
    assert folders == ["live-openai-context"]


def test_p4_s9_ac5_replay_with_provider_openai_scores_its_recordings(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openai_key
) -> None:
    root = copy_without_recordings(tmp_path, "clean")
    use_fake_transport(monkeypatch, fake(completion(findings_content())))
    live_openai(capsys, root)

    replay = run_evals(capsys, "replay", "--cases", root, "--provider", "openai")

    assert replay.exit_code in (PASSED, FAILED), replay.output
    assert re.search(r"^case clean: live recording of m1\b", replay.output, re.MULTILINE), (
        replay.output
    )


def test_p4_s9_ac5_claude_replay_ignores_the_openai_recordings(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openai_key
) -> None:
    root = copy_without_recordings(tmp_path, "clean")
    use_fake_transport(monkeypatch, fake(completion(findings_content())))
    live_openai(capsys, root)

    replay = run_evals(capsys, "replay", "--cases", root, "--require-live")

    assert replay.exit_code == FAILED, replay.output
    assert "m1" not in replay.output.replace("clean", ""), replay.output


def test_p4_s9_ac5_claude_live_recording_path_is_unchanged(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_without_recordings(tmp_path, "clean")

    def sdk(_prompt: str, _options: Any) -> Any:
        async def stream() -> Any:
            yield result_message(
                findings_json(),
                usage={"input_tokens": 100, "output_tokens": 10},
                total_cost_usd=0.01,
            )

        return stream()

    run = run_evals(
        capsys, "live", "--cases", root, "--runs", "1", "--max-calls", "1", *WITHIN_BUDGET,
        sdk_call=sdk,
    )  # fmt: skip

    assert run.exit_code in (PASSED, FAILED), run.output
    assert read_json(recording(root, "clean"))["model"] == DEFAULT_MODEL
    assert sorted(p.name for p in (root / "clean" / "recordings").iterdir()) == ["live"]


def test_p4_s9_ac5_openai_recordings_by_m1_pass_the_gate_for_an_openai_suite(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openai_key
) -> None:
    """End to end: an `openai:m1` suite is fed by `live --provider openai` recordings."""
    root = copy_with_recordings(tmp_path, ["clean"])
    use_fake_transport(monkeypatch, fake(completion(findings_content())))
    min_runs = str(read_json(root / "clean" / "case.json")["min_passing_runs"])
    live_openai(capsys, root, "--runs", min_runs, "--max-calls", min_runs)
    policy = policy_file(tmp_path, ("claude", DEFAULT_MODEL, ["diff"]), ("openai", "m1", ["diff"]))
    suites = write_suites(
        tmp_path,
        Suite(cases=("clean",), accepted=()),
        Suite(cases=("clean",), provider="openai", model="m1", accepted=()),
    )

    run = gate(capsys, root, suites, "--policy", policy)

    assert run.exit_code == PASSED, run.output


# --- AC6: an invalid gate.toml exits 2, naming the key or case ------------------------------------


def assert_invalid(run: Run, *named: str) -> None:
    assert run.exit_code == INVALID, run.output
    for part in named:
        assert part in run.output, run.output


def test_p4_s9_ac6_unknown_top_level_key_exits_2_naming_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    suites = write_suites(tmp_path, Suite(), head='colour = "blue"\n')

    assert_invalid(gate(capsys, CASES, suites), "colour")


def test_p4_s9_ac6_unknown_suite_key_exits_2_naming_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    suites = write_suites(tmp_path, Suite(extra={"flavour": '"x"'}))

    assert_invalid(gate(capsys, CASES, suites), "flavour")


def test_p4_s9_ac6_unknown_accepted_miss_key_exits_2_naming_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = write_suites(tmp_path, Suite())
    path.write_text(path.read_text(encoding="utf-8") + 'reason = "x"\n', encoding="utf-8")

    assert_invalid(gate(capsys, CASES, path), "reason")


@pytest.mark.parametrize("field", ["provider", "model", "mode", "cases"])
def test_p4_s9_ac6_missing_suite_field_exits_2_naming_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str
) -> None:
    text = Suite().toml()
    kept = "\n".join(line for line in text.splitlines() if not line.startswith(f"{field} ="))
    path = tmp_path / "gate.toml"
    path.write_text(kept + "\n", encoding="utf-8")

    assert_invalid(gate(capsys, CASES, path), field)


@pytest.mark.parametrize("field", ["case", "bug", "approved_on"])
def test_p4_s9_ac6_missing_accepted_miss_field_exits_2_naming_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str
) -> None:
    text = Suite().toml()
    head, miss = text.split("[[suite.accepted_miss]]")
    kept = "\n".join(line for line in miss.splitlines() if not line.startswith(f"{field} ="))
    path = tmp_path / "gate.toml"
    path.write_text(head + "[[suite.accepted_miss]]" + kept + "\n", encoding="utf-8")

    assert_invalid(gate(capsys, CASES, path), field)


def test_p4_s9_ac6_accepted_miss_without_approved_by_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    suites = write_suites(tmp_path, Suite(accepted=(Miss(*DOUBLE_TAX, approved_by=None),)))

    assert_invalid(gate(capsys, CASES, suites), "approved_by")


def test_p4_s9_ac6_unknown_case_id_in_a_suite_exits_2_naming_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    suites = write_suites(tmp_path, Suite(cases=(*PHASE1_CASES, "no-such-case")))

    assert_invalid(gate(capsys, CASES, suites), "no-such-case")


def test_p4_s9_ac6_unknown_case_id_in_an_accepted_miss_exits_2_naming_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    suites = write_suites(tmp_path, Suite(accepted=(Miss("no-such-case", "double-tax"),)))

    assert_invalid(gate(capsys, CASES, suites), "no-such-case")


@pytest.mark.parametrize(
    ("field", "value"),
    [("provider", "[1]"), ("cases", '"sample"'), ("mode", '"telepathy"')],
    ids=["provider-not-a-string", "cases-not-a-list", "unknown-mode"],
)
def test_p4_s9_ac6_wrong_typed_field_exits_2_naming_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str, value: str
) -> None:
    text = "\n".join(
        f"{field} = {value}" if line.startswith(f"{field} =") else line
        for line in Suite().toml().splitlines()
    )
    path = tmp_path / "gate.toml"
    path.write_text(text + "\n", encoding="utf-8")

    assert_invalid(gate(capsys, CASES, path), field)


def test_p4_s9_ac6_invalid_suites_never_read_a_recording(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Exit 2 rather than 1: an unusable file isn't a failed gate."""
    root = copy_with_recordings(tmp_path)
    recording(root, "clean").unlink()
    suites = write_suites(tmp_path, Suite(), head="foo = 1\n")

    assert_invalid(gate(capsys, root, suites), "foo")


# --- Limits: gate.toml is repository input, at most 64 KB, parsed strictly ------------------------


def test_p4_s9_limits_gate_toml_over_64_kb_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = write_suites(tmp_path, Suite())
    padding = "# " + "x" * 98 + "\n"
    path.write_bytes((path.read_text(encoding="utf-8") + padding * 700).encode("utf-8"))
    assert path.stat().st_size > 64 * 1024

    assert_invalid(gate(capsys, CASES, path), "64")


def test_p4_s9_limits_gate_toml_just_under_64_kb_is_read(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = write_suites(tmp_path, Suite())
    text = path.read_text(encoding="utf-8")
    room = 64 * 1024 - len(text.encode("utf-8")) - 1
    path.write_bytes((text + "#" + "x" * (room - 2) + "\n").encode("utf-8"))
    assert path.stat().st_size <= 64 * 1024

    assert gate(capsys, CASES, path).exit_code == PASSED


@pytest.mark.parametrize(
    "content",
    [b"[[suite]\n", b"\xff\xfe[[suite]]\n", b"", b"suite = 1\n"],
    ids=["not-toml", "not-utf8", "empty", "suite-not-a-table-array"],
)
def test_p4_s9_limits_malformed_gate_toml_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "gate.toml"
    path.write_bytes(content)

    run = gate(capsys, CASES, path)

    assert run.exit_code == INVALID, run.output
    assert "Traceback" not in run.output, run.output


def test_p4_s9_limits_missing_gate_toml_exits_2_naming_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert_invalid(gate(capsys, CASES, tmp_path / "absent.toml"), "absent.toml")


def test_p4_s9_limits_duplicate_suite_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Two suites for one provider, model and mode would leave the gate's choice ambiguous."""
    run = gate(capsys, CASES, write_suites(tmp_path, Suite(), Suite()))

    assert run.exit_code == INVALID, run.output


def test_p4_s9_limits_case_id_with_a_path_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    suites = write_suites(tmp_path, Suite(cases=(*PHASE1_CASES, "../cases/clean")))

    assert gate(capsys, root, suites).exit_code == INVALID


# --- AC7: one line per suite and a final verdict; `--format json` writes `eval-gate/v1` -----------


def suite_lines(run: Run) -> list[str]:
    return [
        line
        for line in run.stdout.splitlines()
        if line and not line[0].isspace() and DEFAULT_MODEL in line
    ]


def test_p4_s9_ac7_text_has_one_line_per_suite_and_a_final_verdict(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)

    run = gate(capsys, root, write_suites(tmp_path, Suite()))

    assert run.exit_code == PASSED, run.output
    [line] = suite_lines(run)
    for word in ("diff", "cases", "fresh", "passed", "accepted"):
        assert word in line, line
    assert re.search(r"\b5\b", line), line
    last = run.stdout.strip().splitlines()[-1]
    assert re.search(r"pass", last, re.IGNORECASE), last


def test_p4_s9_ac7_text_has_a_line_for_each_committed_suite(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = gate(capsys, CASES, GATE_TOML)

    assert len(suite_lines(run)) == len(committed_suites()), run.stdout


def test_p4_s9_ac7_failed_run_says_so_in_its_final_verdict(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    recording(root, "clean").unlink()

    run = gate(capsys, root, write_suites(tmp_path, Suite()))

    assert run.exit_code == FAILED, run.output
    last = run.stdout.strip().splitlines()[-1]
    assert re.search(r"fail", last, re.IGNORECASE), last


def test_p4_s9_ac7_json_is_eval_gate_v1_with_each_suite(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)

    run = gate(capsys, root, write_suites(tmp_path, Suite()), "--format", "json")

    assert run.exit_code == PASSED, run.output
    doc = json.loads(run.stdout)
    assert doc["schema"] == "eval-gate/v1"
    [suite] = doc["suites"]
    assert (suite["provider"], suite["model"], suite["mode"]) == ("claude", DEFAULT_MODEL, "diff")
    text = json.dumps(suite)
    for key in ("cases", "fresh", "passed", "accepted"):
        assert key in text, suite


def test_p4_s9_ac7_json_on_a_failed_run_still_exits_1(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = copy_with_recordings(tmp_path)
    recording(root, "clean").unlink()

    run = gate(capsys, root, write_suites(tmp_path, Suite()), "--format", "json")

    assert run.exit_code == FAILED, run.output
    doc = json.loads(run.stdout)
    assert doc["schema"] == "eval-gate/v1"
    assert "clean" in json.dumps(doc)
