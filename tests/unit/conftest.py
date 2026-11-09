"""Unit-test fixtures shared across modules."""

from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def audit_log_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Send each run's audit log to the test's own directory, never the repository's."""
    path = tmp_path / "audit" / "audit.jsonl"
    monkeypatch.setenv("REVIEW_BOT_AUDIT_LOG", str(path))
    return path


@pytest.fixture
def ci_run(monkeypatch: pytest.MonkeyPatch) -> Callable[[str | None], None]:
    """A function that sets ``CI``, and ``GITHUB_EVENT_NAME`` to its event, or unsets it if None."""

    def start(event: str | None) -> None:
        monkeypatch.setenv("CI", "true")
        if event is None:
            monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
        else:
            monkeypatch.setenv("GITHUB_EVENT_NAME", event)

    return start
