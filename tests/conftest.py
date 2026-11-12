"""Suite-wide offline guarantee: no Claude CLI, no credentials, no external network.

P0-S1 AC16 requires every criterion to pass without `claude` on PATH and without
`ANTHROPIC_API_KEY`, so the environment is enforced here rather than assumed from the machine
running the tests. `tests/integration/test_p0_s1_offline.py` proves that it holds.
"""

import os
import shutil
import socket
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk._internal.client import InternalClient
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

CLAUDE_EXECUTABLES = ("claude", "claude.exe", "claude.cmd", "claude.bat", "claude.ps1")
# OPENAI_API_KEY since P4-S2: a developer's own key must never reach a test either.
CREDENTIAL_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
)
AUDIT_LOG_VARIABLE = "REVIEW_BOT_AUDIT_LOG"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_real_query_calls: list[dict[str, Any]] = []


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
def offline_environment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Apply to the whole run, including the child processes tests start."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PATH", _path_without_claude(os.environ.get("PATH", "")))
        for name in CREDENTIAL_VARIABLES:
            patch.delenv(name, raising=False)
        # A developer's own settings must not change what a criterion observes.
        for name in [name for name in os.environ if name.startswith("REVIEW_BOT_")]:
            patch.delenv(name)
        # Every run appends an audit record (P1-S4); by default that lands in the working
        # directory, which here is the repository. A test of the default unsets this itself.
        audit_log = tmp_path_factory.mktemp("audit") / "audit.jsonl"
        patch.setenv(AUDIT_LOG_VARIABLE, str(audit_log))
        yield


@pytest.fixture(scope="session", autouse=True)
def real_query_guard() -> Iterator[None]:
    """Stop the real `claude_agent_sdk.query`, for P0-S2 AC15.

    The patches sit below `query` because a module that did `from claude_agent_sdk import query`
    at import time already holds the real function. `connect` also covers a route around `query`.
    """

    def stop(_self: object, *_args: Any, **kwargs: Any) -> None:
        _real_query_calls.append(kwargs)
        raise AssertionError("a test reached the real claude_agent_sdk.query")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(InternalClient, "process_query", stop)
        patch.setattr(SubprocessCLITransport, "connect", stop)
        yield


@pytest.fixture
def real_query_calls() -> list[dict[str, Any]]:
    """The arguments of each stopped call. A control test clears this to pass its teardown."""
    return _real_query_calls


@pytest.fixture(autouse=True)
def fail_on_real_query() -> Iterator[None]:
    """Fail after the test: `main` turns the guard's own exception into exit 14 (P0-S2 AC17)."""
    yield
    if _real_query_calls:
        _real_query_calls.clear()
        pytest.fail("this test reached the real claude_agent_sdk.query")


@pytest.fixture(autouse=True)
def own_unraisable_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start and end every test with pytest's `sys.unraisablehook`.

    An in-process `main` that builds Claude discards unraisable reports until the process exits
    (P1-S18), and pytest installs its hook once per session, so without this every later test
    would lose its `__del__` errors.
    """
    monkeypatch.setattr(sys, "unraisablehook", sys.unraisablehook)


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
