"""Read an audit log back, for tests that check what a run recorded."""

import json
from pathlib import Path
from typing import Any


def read_records(path: Path) -> list[dict[str, Any]]:
    """Every record in the log, in order. Each line must be one JSON object."""
    return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]


def events(path: Path) -> list[str]:
    """``stage.event`` for every record, in order."""
    return [f"{record['stage']}.{record['event']}" for record in read_records(path)]


def data_of(path: Path, event: str) -> dict[str, Any]:
    """The data of the only record for ``stage.event``."""
    [data] = [
        record["data"]
        for record in read_records(path)
        if f"{record['stage']}.{record['event']}" == event
    ]
    return data
