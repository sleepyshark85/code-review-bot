"""What P3-S16's AC7 tests share: the CI events a live command refuses, and setting them."""

import pytest

__all__ = ["REFUSED_EVENTS", "set_ci"]

# Every GITHUB_EVENT_NAME that isn't exactly `workflow_dispatch`: the spec's four, then lookalikes.
REFUSED_EVENTS = [
    pytest.param("pull_request", id="pull_request"),
    pytest.param("push", id="push"),
    pytest.param("schedule", id="schedule"),
    pytest.param(None, id="unset"),
    pytest.param("", id="empty"),
    pytest.param("pull_request_target", id="pull_request_target"),
    pytest.param("repository_dispatch", id="repository_dispatch"),
    pytest.param("workflow_call", id="workflow_call"),
    pytest.param("WORKFLOW_DISPATCH", id="upper-case"),
    pytest.param(" workflow_dispatch", id="leading-space"),
    pytest.param("workflow_dispatch\n", id="trailing-newline"),
    pytest.param("workflow_dispatch_x", id="longer-name"),
]


def set_ci(monkeypatch: pytest.MonkeyPatch, event: str | None, ci: str = "true") -> None:
    """Set `CI` to `ci`, and `GITHUB_EVENT_NAME` to `event` unless it's None (left unset)."""
    monkeypatch.setenv("CI", ci)
    if event is not None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", event)
