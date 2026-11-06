"""Unit-test fixtures shared across modules."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def audit_log_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Send each run's audit log to the test's own directory, never the repository's."""
    path = tmp_path / "audit" / "audit.jsonl"
    monkeypatch.setenv("REVIEW_BOT_AUDIT_LOG", str(path))
    return path
