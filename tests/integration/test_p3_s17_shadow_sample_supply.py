"""P3-S17: let this repository's own pull requests reach a shadow review.

Written from the spec's criteria, before reading any change it led to. Two things are tested:

- the rule for stand-in credentials in tests: placeholder values pass the secret scan, other
  fake values still block (AC1, AC2), and a guard keeps every tracked file under `tests/` free
  of scanner hits unless it is on `SECRET_TEST_FILES` (AC3);
- this repository's committed `.review-bot.toml`, copied to the base of a P2-S9 pull request
  repository and run through `review_bot.pr.cli.main` with a scripted `MockProvider` (AC5 to
  AC8).

No credential-shaped value appears in this file: each one in AC2, AC3 and AC7 is assembled at
run time from pieces, so the guard's own scan of this file finds nothing (spec, Acceptance
criteria).
"""

import importlib
import inspect
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path

import pytest
from diff_support import section
from repo_paths import REPO_ROOT
from test_p2_s9_repo_config import CONFIG, PullRequest, canary, make_pr, run_pr

from review_bot.secret_scan import scan_secrets

pytestmark = pytest.mark.integration

SECRET_EXIT = 16
REVIEW_BYTES_VARIABLE = "REVIEW_BOT_MAX_REVIEW_BYTES"
COMMITTED_CONFIG = REPO_ROOT / CONFIG
RECORDINGS_GLOB = "tests/evals/cases/*/recordings/**"

PLACEHOLDER = "test-placeholder-not-a-key"
# 20 characters with digits and no placeholder word: a value the scan reports (P1-S3 Notes).
V = "q7Rk2Vw9Lp" + "4Xn8Bt3Mz6"
API_KEY_NAME = "API" + "_KEY"
ANTHROPIC_NAME = "ANTHROPIC" + "_API_KEY"
GITHUB_NAME = "GITHUB" + "_TOKEN"

# The tracked files under `tests/` that test secret detection, blocking, refusal or redaction,
# and so must hold values the scan reports. Every other tracked file under `tests/` must not.
SECRET_TEST_FILES = frozenset(
    {
        "tests/integration/test_p1_s3_secret_block.py",
        "tests/unit/test_secret_scan.py",
    }
)
# The files S17 rewrote: none is on the list, and none has a hit.
REWRITTEN_FILES = (
    "tests/integration/test_p0_s4_messages.py",
    "tests/integration/test_p0_s4_real_sdk.py",
    "tests/integration/test_p3_s6_live_evals_workflow.py",
    "tests/integration/test_p3_s8_pr_review_with_context.py",
    "tests/integration/test_p3_s14_context_in_ci.py",
    "tests/integration/test_p3_s16_live_workflow.py",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub Actions sets `CI`, and a developer may set a cap; each test sets its own."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("REVIEW_BOT_THRESHOLD", raising=False)
    monkeypatch.delenv(REVIEW_BYTES_VARIABLE, raising=False)


def assigned(name: str, value: str) -> str:
    return f'{name} = "{value}"'


def json_pair(name: str, value: str) -> str:
    return f'"{name}": "{value}"'


def yaml_pair(name: str, value: str) -> str:
    return f'{name}: "{value}"'


def anthropic_key() -> str:
    return "sk" + "-ant-" + "b4Hq9Tz2Wm7Kc5Rx8Jn3Lp6V"


def aws_key() -> str:
    return "AK" + "IA" + "Q7XW3RT9LP2MN5VB"


# AC1: a placeholder value under any credential name is reviewed.

PLACEHOLDER_LINES = {
    "python_assignment": ("app.py", assigned(API_KEY_NAME, PLACEHOLDER)),
    "json_anthropic": ("settings.json", json_pair(ANTHROPIC_NAME, PLACEHOLDER)),
    "json_github": ("settings.json", json_pair(GITHUB_NAME, PLACEHOLDER)),
    "yaml_token": ("deploy.yml", yaml_pair("token", PLACEHOLDER)),
}


@pytest.mark.parametrize(("path", "line"), PLACEHOLDER_LINES.values(), ids=PLACEHOLDER_LINES.keys())
def test_p3_s17_ac1_placeholder_value_is_sent_to_the_provider_once(bot, write_diff, path, line):
    provider = bot.mock(findings=[])

    run = bot.review(write_diff(section(path, f"+{line}\n")), provider=provider)

    assert run.exit_code != SECRET_EXIT, run.stderr
    assert len(provider.requests) == 1
    assert line in provider.requests[0].user_prompt


def test_p3_s17_ac1_all_four_placeholder_lines_in_one_diff_are_reviewed(bot, write_diff):
    diff = "".join(
        section(f"{name}/{path}", f"+{line}\n") for name, (path, line) in PLACEHOLDER_LINES.items()
    )
    provider = bot.mock(findings=[])

    run = bot.review(write_diff(diff), provider=provider)

    assert run.exit_code != SECRET_EXIT, run.stderr
    assert len(provider.requests) == 1


# AC2: any other fake value still blocks, whatever its name says.

STILL_BLOCKED = {
    "fake_key": assigned(API_KEY_NAME, "fake-key" + "-for-the-test"),
    "key_sentinel": assigned(API_KEY_NAME, "test-key" + "-sentinel"),
    "fake_github_token": json_pair(GITHUB_NAME, "fake-token" + "-for-the-test"),
    "placeholder_in_the_name": assigned("PLACEHOLDER_" + API_KEY_NAME, V),
    "placeholder_in_the_yaml_name": yaml_pair("TEST_PLACEHOLDER" + "_TOKEN", V),
    "anthropic_key": json_pair(ANTHROPIC_NAME, anthropic_key()),
}


@pytest.mark.parametrize("line", STILL_BLOCKED.values(), ids=STILL_BLOCKED.keys())
def test_p3_s17_ac2_fake_value_still_exits_16_without_a_call(bot, write_diff, line):
    provider = bot.mock(findings=[])

    run = bot.review(write_diff(section("app.py", f"+{line}\n")), provider=provider)

    assert run.exit_code == SECRET_EXIT, run.stderr
    assert provider.requests == []


def test_p3_s17_ac2_v_is_a_value_the_scan_reports():
    """The precondition the spec puts on V: 20 or more characters, a digit, no placeholder word."""
    assert len(V) >= 20
    assert any(character.isdigit() for character in V)
    assert scan_secrets(assigned("token", V))


# AC3: the guard.


def tracked_test_files() -> dict[str, str]:
    """Every tracked file under `tests/`, by its repository path, decoded as UTF-8."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "tests"],  # noqa: S607 - git from PATH, as CI runs it
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    paths = [path for path in listing.split("\0") if path]
    return {
        path: (REPO_ROOT / path).read_bytes().decode("utf-8", errors="replace") for path in paths
    }


def unlisted_hits(files: Mapping[str, str]) -> list[str]:
    """`path:line: kind` for each scanner hit in a file not on `SECRET_TEST_FILES`."""
    return [
        f"{path}:{hit.diff_line}: {hit.kind.value}"
        for path, text in sorted(files.items())
        if path not in SECRET_TEST_FILES
        for hit in scan_secrets(text)
    ]


def check_no_unlisted_hits(files: Mapping[str, str]) -> None:
    hits = unlisted_hits(files)
    assert not hits, (
        "A stand-in credential in a test takes a form the secret scan reports. Use a value "
        "with a placeholder word, or build it at run time (CONVENTIONS.md, P3-S17):\n"
        + "\n".join(hits)
    )


def test_p3_s17_ac3_no_tracked_test_file_off_the_list_has_a_scanner_hit():
    check_no_unlisted_hits(tracked_test_files())


def test_p3_s17_ac3_the_six_rewritten_files_are_tracked_unlisted_and_clean():
    files = tracked_test_files()

    for path in REWRITTEN_FILES:
        assert path in files, path
        assert path not in SECRET_TEST_FILES, path
        assert scan_secrets(files[path]) == (), path


def test_p3_s17_ac3_every_listed_file_is_tracked_and_still_needs_its_place():
    """A listed file that no longer holds a hit should leave the list, so it can't hide one."""
    files = tracked_test_files()

    for path in SECRET_TEST_FILES:
        assert path in files, path
        assert scan_secrets(files[path]), path


def test_p3_s17_ac3_an_unlisted_hit_fails_naming_path_line_and_kind_not_the_value():
    value = "fake-key" + "-for-the-test"
    files = {
        "tests/unit/test_secret_scan.py": assigned(API_KEY_NAME, V),  # listed: allowed
        "tests/integration/test_new.py": "import os\n\n" + assigned(API_KEY_NAME, value) + "\n",
    }

    with pytest.raises(AssertionError) as failure:
        check_no_unlisted_hits(files)

    message = str(failure.value)
    assert "tests/integration/test_new.py:3: assigned-credential" in message
    assert "tests/unit/test_secret_scan.py" not in message
    assert value not in message
    assert V not in message


def test_p3_s17_ac3_a_clean_file_set_passes_the_check():
    check_no_unlisted_hits({"tests/integration/test_new.py": assigned(API_KEY_NAME, PLACEHOLDER)})


def test_p3_s17_ac3_this_file_has_no_scanner_hit():
    assert scan_secrets(Path(__file__).read_text(encoding="utf-8")) == ()


# AC5 to AC8: this repository's committed config, at the base of a pull request.

RECORDING = "tests/evals/cases/sample-context/recordings/synthetic/double_tax_1_of_3.json"
CASE = "tests/evals/cases/sample-context/case.json"
CASE_SOURCE = "tests/evals/cases/sample-context/repo/billing/invoice.py"
BOT_SOURCE = "src/review_bot/review.py"


def committed_config() -> bytes:
    return COMMITTED_CONFIG.read_bytes()


def json_canary(path: str) -> str:
    return f'{{"note": "{canary(path)}"}}\n'


def python_canary(path: str) -> str:
    return f'NOTE = "{canary(path)}"\n'


def sample_context_pr(path: Path) -> PullRequest:
    head: dict[str, str | bytes] = {
        RECORDING: json_canary(RECORDING),
        CASE: json_canary(CASE),
        CASE_SOURCE: python_canary(CASE_SOURCE),
        BOT_SOURCE: python_canary(BOT_SOURCE),
    }
    return make_pr(path, head, base_config=committed_config())


def test_p3_s17_ac5_only_the_recording_is_skipped_as_excluded_by_config(tmp_path, capsys):
    pull = sample_context_pr(tmp_path / "clone")

    run = run_pr(pull, tmp_path, capsys)

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    skipped = [(s["file"], s["reason"]) for s in run.shadow["report"]["skipped_files"]]
    assert skipped == [(RECORDING, "excluded_by_config")]
    prompt = run.prompt
    assert canary(RECORDING) not in prompt
    for path in (CASE, CASE_SOURCE, BOT_SOURCE):
        assert canary(path) in prompt, path
    assert run.config["source"] == f"{CONFIG}@{pull.base}"


RECORDINGS_BYTES = 150_000
SOURCE_BYTES = 2_000
REVIEW_CAP = "120000"
LARGE_RECORDINGS = (
    "tests/evals/cases/sample-context/recordings/synthetic/run_1.json",
    "tests/evals/cases/other-case/recordings/live/run_2.json",
)
APP = "src/app.py"


def filler(size: int) -> str:
    """`size` bytes of 100-byte lines that hold no secret."""
    line = "a" * 99 + "\n"
    assert size % len(line) == 0
    return line * (size // len(line))


def large_pr_head() -> dict[str, str | bytes]:
    per_recording = RECORDINGS_BYTES // len(LARGE_RECORDINGS)
    source = "".join(f"value_{n:03d} = {n:07d}\n" for n in range(100))
    assert len(source) == SOURCE_BYTES
    return {**{path: filler(per_recording) for path in LARGE_RECORDINGS}, APP: source}


def test_p3_s17_ac6_recordings_over_the_cap_dont_stop_the_review_with_the_config(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv(REVIEW_BYTES_VARIABLE, REVIEW_CAP)
    pull = make_pr(tmp_path / "clone", large_pr_head(), base_config=committed_config())

    run = run_pr(pull, tmp_path, capsys)

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    skipped = sorted((s["file"], s["reason"]) for s in run.shadow["report"]["skipped_files"])
    assert skipped == sorted((path, "excluded_by_config") for path in LARGE_RECORDINGS)
    assert "value_001 = 0000001" in run.prompt


def test_p3_s17_ac6_the_same_pr_without_the_config_is_too_large(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(REVIEW_BYTES_VARIABLE, REVIEW_CAP)
    pull = make_pr(tmp_path / "clone", large_pr_head())

    run = run_pr(pull, tmp_path, capsys)

    assert (run.shadow["outcome"], run.shadow["reason"]) == ("skipped", "too_large")
    assert run.provider.requests == []


def test_p3_s17_ac7_secret_in_an_excluded_recording_still_blocks(tmp_path, capsys):
    key = aws_key()
    head: dict[str, str | bytes] = {
        RECORDING: f'{{"note": "{key}"}}\n',
        BOT_SOURCE: python_canary(BOT_SOURCE),
    }
    pull = make_pr(tmp_path / "clone", head, base_config=committed_config())

    run = run_pr(pull, tmp_path, capsys)

    assert run.shadow["outcome"] == "blocked_secret"
    assert run.provider.requests == []
    assert key.encode() not in (run.out / "shadow.json").read_bytes()
    assert key not in run.stderr


def test_p3_s17_ac8_committed_config_sets_only_the_recordings_exclusion():
    assert tomllib.loads(committed_config().decode("utf-8")) == {"exclude": [RECORDINGS_GLOB]}


def test_p3_s17_ac8_threshold_and_max_open_comments_stay_at_their_defaults(tmp_path, capsys):
    run = run_pr(sample_context_pr(tmp_path / "clone"), tmp_path, capsys)

    assert run.exit_code == 0, run.stderr
    assert run.config["threshold"] == "low"
    assert run.config["max_open_comments"] == 10
    assert run.config["exclude"] == [RECORDINGS_GLOB]


# AC9: P0-S4 AC11 and P1-S5 AC8 keep the value `test-key-sentinel`.

SENTINEL_SUITES = (
    ("test_p0_s4_messages", "test_p0_s4_ac11_"),
    ("test_p0_s4_real_sdk", "test_p0_s4_ac11_"),
    ("test_p1_s5_provider_notices", "test_p1_s5_ac8_"),
)


@pytest.mark.parametrize(("module_name", "prefix"), SENTINEL_SUITES)
def test_p3_s17_ac9_suites_still_use_the_test_key_sentinel(module_name, prefix):
    module = importlib.import_module(module_name)

    assert module.KEY_SENTINEL == "test-key-sentinel"
    criterion_tests = [test for name, test in vars(module).items() if name.startswith(prefix)]
    assert any("KEY_SENTINEL" in inspect.getsource(test) for test in criterion_tests), prefix
