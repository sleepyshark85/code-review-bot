"""P0-S1 AC16: the environment `tests/conftest.py` enforces holds, and a review needs no more."""

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPORT_CHILD_ENVIRONMENT = """
import json, os, shutil
print(json.dumps({
    "claude": shutil.which("claude"),
    "has_key": "ANTHROPIC_API_KEY" in os.environ,
}))
"""


def _path_entries() -> list[str]:
    return [entry for entry in os.environ["PATH"].split(os.pathsep) if entry]


def test_p0_s1_ac16_claude_is_not_on_path():
    assert shutil.which("claude") is None


@pytest.mark.parametrize("name", ["claude", "claude.exe", "claude.cmd", "claude.bat", "claude.ps1"])
def test_p0_s1_ac16_no_path_entry_holds_a_claude_executable(name):
    assert [entry for entry in _path_entries() if (Path(entry) / name).exists()] == []


def test_p0_s1_ac16_api_key_is_absent():
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_p0_s1_ac16_child_processes_get_the_same_environment():
    completed = subprocess.run(  # noqa: S603 - fixed argument list, our own interpreter
        [sys.executable, "-c", REPORT_CHILD_ENVIRONMENT],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )

    assert json.loads(completed.stdout) == {"claude": None, "has_key": False}


def test_p0_s1_ac16_path_filter_removes_a_directory_holding_claude(tmp_path, path_without_claude):
    with_claude = tmp_path / "with"
    without_claude = tmp_path / "without"
    with_claude.mkdir()
    without_claude.mkdir()
    (with_claude / "claude.exe").write_bytes(b"")

    filtered = path_without_claude(os.pathsep.join([str(with_claude), str(without_claude)]))

    assert filtered == str(without_claude)


def test_p0_s1_ac16_external_connection_is_refused_in_tests():
    with socket.socket() as sock, pytest.raises(AssertionError, match="network access"):
        sock.connect(("192.0.2.1", 80))  # TEST-NET-1: never reached


def test_p0_s1_ac16_external_lookup_is_refused_in_tests():
    with pytest.raises(AssertionError, match="network lookup"):
        socket.getaddrinfo("example.invalid", 80)


def test_p0_s1_ac16_review_runs_with_no_subprocess_and_no_network(
    bot, sample_diff, monkeypatch, golden
):
    def refuse_process(*args, **kwargs):
        raise AssertionError("a review must not start a process")

    monkeypatch.setattr(subprocess.Popen, "__init__", refuse_process)

    # No injected provider: this is the path a real run takes through `build_provider`.
    run = bot.review(sample_diff)

    assert run.exit_code == 1, run.stderr
    assert run.stdout_body == golden("canned_finding.golden.txt")
