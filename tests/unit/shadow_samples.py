"""Builders of ``shadow/v1`` documents for the labelling tests."""

import itertools
import json
from pathlib import Path
from typing import Any

K1 = ("src/app.py", "swallowed-exception", "a" * 16)
K2 = ("src/app.py", "ai:logic", "b" * 16)
K3 = ("src/db.py", "ai:security", "c" * 16)

# Each report is its own run unless a test says otherwise.
_RUN_NUMBERS = itertools.count(1)


def entry(
    key: tuple[str, str, str],
    *,
    line: int = 10,
    fingerprint: str = "f" * 16,
    action: str = "create",
    body: str = "**medium** logic",
) -> dict[str, Any]:
    file, rule_part, anchor = key
    return {
        "action": action,
        "fingerprint": fingerprint,
        "rule_part": rule_part,
        "anchor": anchor,
        "source": "ai",
        "severity": "medium",
        "file": file,
        "line": line,
        "body": body,
    }


def report(
    *entries: dict[str, Any],
    finished_at: str = "2026-11-08T10:00:00+00:00",
    number: int = 7,
    **fields: Any,
) -> dict[str, Any]:
    """A reviewed shadow report from an event on PR ``number``, with ``fields`` replaced.

    Its ``run_id`` is new on every call.
    """
    document = {
        "schema": "shadow/v1",
        "run_id": f"run-{next(_RUN_NUMBERS)}",
        "mode": "shadow",
        "origin": "event",
        "outcome": "reviewed",
        "reason": None,
        "pr": {"repo": "owner/repo", "number": number, "base_sha": "0" * 40, "head_sha": "1" * 40},
        "event_at": finished_at,
        "started_at": finished_at,
        "finished_at": finished_at,
        "report": {"schema": "report/v1", "model": "claude-x", "prompt_version": "p1-v1"},
        "summary_body": "summary",
        "comments": list(entries),
        "withheld": [],
        "caps": {},
        "over_budget": False,
    }
    return document | fields


def write_report(directory: Path, name: str, document: dict[str, Any]) -> Path:
    path = directory / name / "shadow.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding="ascii")
    return path
