"""Run a command with its stdout or stderr really broken, for the real-process tests."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from repo_paths import REPO_ROOT

PIPE = "pipe"
MERGED = "merged"
CLOSED = "closed"
READER_GONE = "reader_gone"
DEV_FULL = "dev_full"

needs_dev_full = pytest.mark.skipif(not Path("/dev/full").exists(), reason="no /dev/full here")
# Python and the CRT treat a descriptor closed by a parent like `>&-`, and `os.close` also clears
# the Windows standard handle. `cmd` has no such redirection, and this works on both platforms.
CLOSE_THEN_RUN = """
import os, subprocess, sys
for descriptor in sys.argv[1].split(","):
    os.close(int(descriptor))
sys.exit(subprocess.call(sys.argv[2:], close_fds=False))
"""


def run_process(command, *, stdout=PIPE, stderr=PIPE):
    opened = []

    def target(kind):
        if kind == PIPE:
            return subprocess.PIPE
        if kind == MERGED:
            return subprocess.STDOUT
        if kind == CLOSED:
            return subprocess.DEVNULL  # replaced: the launcher closes the descriptor
        if kind == DEV_FULL:
            descriptor = os.open("/dev/full", os.O_WRONLY)
        else:
            read_end, descriptor = os.pipe()
            os.close(read_end)
        opened.append(descriptor)
        return descriptor

    closing = ",".join(str(fd) for fd, kind in ((1, stdout), (2, stderr)) if kind == CLOSED)
    if closing:
        command = [sys.executable, "-c", CLOSE_THEN_RUN, closing, *command]
    try:
        return subprocess.run(  # noqa: S603 - fixed arguments, our own interpreter
            command,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=target(stdout),
            stderr=target(stderr),
            encoding="utf-8",
            timeout=120,
            check=False,
        )
    finally:
        for descriptor in opened:
            os.close(descriptor)
