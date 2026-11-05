"""Suite-wide offline guarantee: no Claude CLI, no credentials, no external network.

P0-S1 AC16 requires every criterion to pass without `claude` on PATH and without
`ANTHROPIC_API_KEY`, so the environment is enforced here rather than assumed from the machine
running the tests. `tests/integration/test_p0_s1_offline.py` proves that it holds.
"""

import os
import shutil
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

CLAUDE_EXECUTABLES = ("claude", "claude.exe", "claude.cmd", "claude.bat", "claude.ps1")
CREDENTIAL_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _holds_claude(directory: str) -> bool:
    if shutil.which("claude", path=directory):
        return True
    return any((Path(directory) / name).exists() for name in CLAUDE_EXECUTABLES)


def _path_without_claude(path: str) -> str:
    entries = path.split(os.pathsep)
    return os.pathsep.join(entry for entry in entries if entry and not _holds_claude(entry))


@pytest.fixture
def path_without_claude() -> Callable[[str], str]:
    """Expose the PATH filter, so a test can show it removes a directory holding `claude`."""
    return _path_without_claude


@pytest.fixture(scope="session", autouse=True)
def offline_environment() -> Iterator[None]:
    """Apply to the whole run, including the child processes tests start."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PATH", _path_without_claude(os.environ.get("PATH", "")))
        for name in CREDENTIAL_VARIABLES:
            patch.delenv(name, raising=False)
        # A developer's own settings must not change what a criterion observes.
        for name in [name for name in os.environ if name.startswith("REVIEW_BOT_")]:
            patch.delenv(name)
        yield


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse non-loopback connections. Loopback stays open: asyncio's self-pipe needs it."""
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        if isinstance(address, tuple) and address[0] not in LOOPBACK_HOSTS:
            raise AssertionError(f"network access attempted in a test: {address!r}")
        real_connect(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in LOOPBACK_HOSTS and host is not None:
            raise AssertionError(f"network lookup attempted in a test: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
