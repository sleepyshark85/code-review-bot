"""Temporary eval cases and recordings for the eval unit tests.

`CALC_DIFF` adds lines 2 to 5 of `app/calc.py`; its `except Exception:` on line 4 is a swallowed
exception, which the rules catch. `RULE_BUG` is that; `AI_BUG` wants "float" on line 3, in at
least 2 runs.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from review_bot.evals import freshness_fields

CALC_FILE: Final = "app/calc.py"
CALC_DIFF: Final = (
    f"diff --git a/{CALC_FILE} b/{CALC_FILE}\n--- a/{CALC_FILE}\n+++ b/{CALC_FILE}\n"
    "@@ -1,2 +1,5 @@\n"
    " def total(items):\n"
    "-    return sum(items)\n"
    "+    try:\n"
    "+        return sum(items)\n"
    "+    except Exception:\n"
    "+        pass\n"
)
RULE_BUG: Final = {"id": "swallowed", "source": "rule", "file": CALC_FILE, "lines": [[4, 4]]}
AI_BUG: Final = {
    "id": "float-sum",
    "source": "ai",
    "file": CALC_FILE,
    "lines": [[3, 3]],
    "keywords": [["float", "floats"]],
    "min_runs_caught": 2,
}
NO_USAGE: Final = {"input_tokens": 0, "output_tokens": 0}


def write_json(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_case(
    cases_dir: Path,
    case_id: str = "calc",
    *,
    bugs: Sequence[Mapping[str, Any]] = (RULE_BUG, AI_BUG),
    diff: str = CALC_DIFF,
    **fields: Any,
) -> Path:
    """Write `<case_id>/case.json` and its diff; return the case file's path."""
    case_dir = cases_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "change.diff").write_bytes(diff.encode("utf-8"))
    case = {"schema": "eval-case/v1", "id": case_id, "diff": "change.diff", "bugs": list(bugs)}
    return write_json(case_dir / "case.json", case | fields)


def finding(line: int = 3, rationale: str = "Summing floats loses cents.", **fields: Any) -> dict:
    return {
        "file": CALC_FILE,
        "line": line,
        "severity": "medium",
        "category": "logic",
        "rationale": rationale,
        "confidence": 0.8,
    } | fields


def run_of(*findings: Mapping[str, Any]) -> dict:
    return {"findings": list(findings), "usage": NO_USAGE, "warnings": []}


def failed_run(category: str = "invalid_output") -> dict:
    return {"error": {"category": category}, "usage": NO_USAGE, "warnings": []}


def recording(case_file: Path, runs: Sequence[Mapping[str, Any]], **fields: Any) -> dict:
    """A fresh synthetic recording of `case_file`'s case, with `fields` overriding any field."""
    return {
        "schema": "eval-recording/v1",
        "case_id": case_file.parent.name,
        "kind": "synthetic",
        **freshness_fields(case_file),
        "model": "claude-opus-5-5",
        "sdk_version": "0.2.178",
        "recorded_at": "2026-11-07T10:00:00+00:00",
        "runs": list(runs),
    } | fields


def write_live(case_file: Path, data: Mapping[str, Any]) -> Path:
    return write_json(case_file.parent / "recordings" / "live" / "recording.json", data)


def write_synthetic(case_file: Path, data: Mapping[str, Any], name: str = "control") -> Path:
    return write_json(case_file.parent / "recordings" / "synthetic" / f"{name}.json", data)
