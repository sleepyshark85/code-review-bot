"""P1-S1: whether the Claude SDK loads, seen from a real child process that reports afterwards.

The pytest process always holds the SDK (`tests/conftest.py` imports it), so only a child can tell.
The report goes to a file, because stdout and stderr are what the criteria inspect.

Two traps shape the children:
- P0-S4's child imports the SDK to patch it, which would make AC3 pass whatever `cli` does. Here the
  patch waits for something else to load the SDK, and runs before anything can use it.
- A `sys.modules` entry of `None` would make `'claude_agent_sdk' in sys.modules` true and can't
  carry `SENTINEL`, so AC4 and AC5 refuse the import from a meta-path finder, which also counts
  attempts.

`--help` leaves `main` through `SystemExit(0)`, not a return, so the child reports either way.
"""

import json
import sys

import pytest
from p0_s4_support import PREFIX
from real_sdk_support import FAKE_CLI, FINDING_LINE_START, assert_failed_cleanly
from repo_paths import REPO_ROOT
from stream_support import run_process

pytestmark = pytest.mark.integration

SAMPLE_DIFF = REPO_ROOT / "Assignment" / "sample_diff.txt"

CHILD_START = """
import json, pathlib, sys

report, *argv = sys.argv[1:]
attempts = []
"""

REFUSE_THE_SDK = """
class RefuseTheSdk:
    def find_spec(self, name, path=None, target=None):
        if name.partition(".")[0] == "claude_agent_sdk":
            attempts.append(name)
            raise ImportError("SENTINEL")
        return None

sys.meta_path.insert(0, RefuseTheSdk())
"""

# Patching when the top-level package loads, not its transport module, means a renamed module fails
# the run here: no route reaches the real CLI. `getattr` first, as in P0-S4's child.
FAKE_CLI_ONCE_THE_SDK_LOADS = """
import importlib.util

fake_cli, marker, mode, *argv = argv

def use_the_fake_cli():
    from claude_agent_sdk._internal.transport import subprocess_cli

    Transport = subprocess_cli.SubprocessCLITransport
    getattr(Transport, "_build_command")
    Transport._build_command = lambda self: [sys.executable, fake_cli, marker, mode]
    Transport._find_cli = lambda self: sys.executable

class PatchTheSdkOnceLoaded:
    def find_spec(self, name, path=None, target=None):
        if name != "claude_agent_sdk":
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        load = spec.loader.exec_module

        def load_then_patch(module):
            load(module)
            use_the_fake_cli()

        spec.loader.exec_module = load_then_patch
        return spec

sys.meta_path.insert(0, PatchTheSdkOnceLoaded())
"""

RUN_MAIN = """
from review_bot import cli

try:
    code = cli.main(argv)
except SystemExit as ended:
    code = ended.code
pathlib.Path(report).write_text(
    json.dumps({"sdk_loaded": "claude_agent_sdk" in sys.modules, "attempts": attempts}),
    encoding="utf-8",
)
sys.exit(code)
"""

# The provider class is imported after the build, so it can't be what loads the SDK or spawns.
BUILD_PROVIDER = """
spawns = []
SPAWNING = {
    "subprocess.Popen", "os.system", "os.spawn", "os.exec", "os.fork", "os.forkpty",
    "os.posix_spawn", "os.startfile",
}
sys.addaudithook(lambda event, args: spawns.append(event) if event in SPAWNING else None)

from review_bot import cli

_, settings = cli.parse_args(argv)
provider = cli.build_provider(settings)
found_spawns = list(spawns)

from review_bot.providers.claude_agent import ClaudeAgentProvider

pathlib.Path(report).write_text(
    json.dumps({"is_claude": isinstance(provider, ClaudeAgentProvider), "spawns": found_spawns}),
    encoding="utf-8",
)
"""

# `--help` and an unknown flag end before the environment is read, so a variable can't differ here.
ANY_PROVIDER_FLAGS = [
    pytest.param([], id="default"),
    pytest.param(["--provider", "mock"], id="mock"),
    pytest.param(["--provider", "claude"], id="claude"),
]


def run_child(tmp_path, code, *argv):
    """Run this child code, and return the process and what it reported."""
    report = tmp_path / "report.json"

    completed = run_process([sys.executable, "-c", CHILD_START + code, str(report), *argv])

    assert report.exists(), f"exit {completed.returncode}, no report: {completed.stderr}"
    return completed, json.loads(report.read_text(encoding="utf-8"))


def run_main(tmp_path, *argv, refuse_the_sdk=False):
    prelude = REFUSE_THE_SDK if refuse_the_sdk else ""
    return run_child(tmp_path, prelude + RUN_MAIN, *argv)


def run_on_a_fake_cli(tmp_path, monkeypatch, mode):
    """A run with no `--provider`, so the default is what reaches the SDK and the fake CLI."""
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    fake_cli = tmp_path / "fake_cli.py"
    fake_cli.write_text(FAKE_CLI, encoding="utf-8")
    marker = tmp_path / "fake_cli_ran"
    diff = tmp_path / "change.diff"
    diff.write_text("+x\n", encoding="utf-8")

    completed, report = run_child(
        tmp_path,
        FAKE_CLI_ONCE_THE_SDK_LOADS + RUN_MAIN,
        str(fake_cli),
        str(marker),
        mode,
        str(diff),
    )

    assert marker.exists(), completed.stderr
    return completed, report


# --- AC1 ------------------------------------------------------------------------------------------


def test_p1_s1_ac1_mock_review_of_the_sample_completes_without_the_sdk(tmp_path):
    completed, report = run_main(tmp_path, str(SAMPLE_DIFF), "--provider", "mock")

    assert completed.returncode in {0, 1}, completed.stderr
    assert completed.stdout.strip() != ""
    assert report["sdk_loaded"] is False


# --- AC2 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("flags", ANY_PROVIDER_FLAGS)
def test_p1_s1_ac2_help_doesnt_load_the_sdk(tmp_path, flags):
    completed, report = run_main(tmp_path, "--help", *flags)

    assert completed.returncode == 0, completed.stderr
    assert "usage" in completed.stdout.lower()
    assert report["sdk_loaded"] is False


@pytest.mark.parametrize("flags", ANY_PROVIDER_FLAGS)
def test_p1_s1_ac2_unknown_flag_doesnt_load_the_sdk(tmp_path, flags):
    completed, report = run_main(tmp_path, str(SAMPLE_DIFF), "--no-such-flag", *flags)

    assert completed.returncode == 2, completed.stderr
    assert report["sdk_loaded"] is False


def test_p1_s1_ac2_missing_diff_with_mock_doesnt_load_the_sdk(tmp_path):
    completed, report = run_main(tmp_path, str(tmp_path / "no_such.diff"), "--provider", "mock")

    assert completed.returncode == 3, completed.stderr
    assert report["sdk_loaded"] is False


def test_p1_s1_ac2_invalid_provider_value_doesnt_load_the_sdk(tmp_path):
    # Beyond AC2's list: the slice's Value says "a usage error", and this one ends in the settings,
    # later than an unknown flag does.
    completed, report = run_main(tmp_path, str(SAMPLE_DIFF), "--provider", "no-such-provider")

    assert completed.returncode == 2, completed.stderr
    assert report["sdk_loaded"] is False


# --- AC3 ------------------------------------------------------------------------------------------


def test_p1_s1_ac3_default_provider_loads_the_sdk_and_delivers_the_review(tmp_path, monkeypatch):
    completed, report = run_on_a_fake_cli(tmp_path, monkeypatch, "success_then_crash")

    assert report["sdk_loaded"] is True
    assert completed.returncode == 1, completed.stderr
    assert completed.stdout.startswith(FINDING_LINE_START)
    assert PREFIX not in completed.stderr
    assert "Fatal error" not in completed.stderr
    assert len(completed.stderr.splitlines()) == 1, completed.stderr
    assert "provider=claude" in completed.stderr


@pytest.mark.parametrize("mode", ["crash_on_the_first_request", "malformed_json"])
def test_p1_s1_ac3_failures_exit_as_in_p0_s4_when_the_sdk_loads_during_the_run(
    tmp_path, monkeypatch, mode
):
    # These two make the SDK log at error level, so they also catch a missing `NullHandler`.
    completed, report = run_on_a_fake_cli(tmp_path, monkeypatch, mode)

    assert report["sdk_loaded"] is True
    assert_failed_cleanly(completed, 12)


# --- AC4 ------------------------------------------------------------------------------------------


def test_p1_s1_ac4_mock_review_needs_no_importable_sdk(tmp_path):
    completed, report = run_main(
        tmp_path, str(SAMPLE_DIFF), "--provider", "mock", refuse_the_sdk=True
    )

    assert completed.returncode in {0, 1}, completed.stderr
    assert completed.stdout.strip() != ""
    assert report["sdk_loaded"] is False
    assert report["attempts"] == [], "a swallowed failed import is still an import"


# --- AC5 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flags", [pytest.param(["--provider", "claude"], id="flag"), pytest.param([], id="default")]
)
def test_p1_s1_ac5_claude_with_an_sdk_that_wont_import_exits_14_without_its_text(tmp_path, flags):
    # AC5 names the flag. The default also catches a wrong default.
    completed, report = run_main(tmp_path, str(SAMPLE_DIFF), *flags, refuse_the_sdk=True)

    assert report["attempts"] != [], "the refusal never fired, so 14 came from something else"
    assert_failed_cleanly(completed, 14)


# --- AC6 ------------------------------------------------------------------------------------------


def test_p1_s1_ac6_build_provider_returns_claude_and_starts_no_subprocess(tmp_path):
    # The default route, as in AC5, so a wrong default is caught here too.
    completed, report = run_child(tmp_path, BUILD_PROVIDER, "change.diff")

    assert completed.returncode == 0, completed.stderr
    assert report["is_claude"] is True
    assert report["spawns"] == []
