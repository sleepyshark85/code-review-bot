"""PostToolUse hook: format and auto-fix a Python file right after it is written.

Runs ``ruff format`` and ``ruff check --fix`` on the edited file. If lint errors
remain, exits with code 2 so the agent sees them and fixes them. Non-Python files
and a missing ``uv`` are ignored.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

TIMEOUT_S = 60


def run_ruff(uv: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``uv run ruff <args>`` and capture its output."""
    return subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [uv, "run", "--quiet", "ruff", *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=False,
    )


def main() -> int:
    """Read the hook payload from stdin and format the edited file."""
    payload = json.load(sys.stdin)
    file_path = payload.get("tool_input", {}).get("file_path", "")
    uv = shutil.which("uv")
    if Path(file_path).suffix != ".py" or uv is None:
        return 0

    run_ruff(uv, ["format", file_path])
    result = run_ruff(uv, ["check", "--fix", "--output-format", "concise", file_path])
    if result.returncode == 0:
        return 0
    sys.stderr.write(f"ruff found issues it could not fix:\n{result.stdout}{result.stderr}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
