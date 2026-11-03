"""PreToolUse hook: restrict which paths each subagent may write.

Claude Code passes the tool call as JSON on stdin. Inside a subagent, the payload
carries ``agent_type``; the main session has none and is not restricted. A denied
write exits with code 2, and the reason on stderr is shown to the agent.
"""

import json
import os
import sys
from fnmatch import fnmatch
from pathlib import Path

# Glob patterns (relative to the project root, POSIX separators) each role may write.
# Mirrors the "Writes to" column of CLAUDE.md.
ALLOWED_PATHS: dict[str, tuple[str, ...]] = {
    "designer": ("docs/phases/*", "docs/adr/*"),
    "developer": (
        "src/*",
        "tests/unit/*",
        "pyproject.toml",
        "uv.lock",
        "docs/phases/*/design.md",  # a "Notes" line for a justified deviation
    ),
    "tester": ("tests/*", "docs/phases/*/test-report.md"),
    "reviewer": (),
}
DENIED_PATHS: dict[str, tuple[str, ...]] = {
    "tester": ("tests/unit/*",),
}


def is_allowed(agent: str, relative_path: str) -> bool:
    """Return whether ``agent`` may write ``relative_path`` (POSIX, project-relative)."""
    if agent not in ALLOWED_PATHS:
        return True  # an agent without a role in CLAUDE.md is not restricted here
    if any(fnmatch(relative_path, pattern) for pattern in DENIED_PATHS.get(agent, ())):
        return False
    return any(fnmatch(relative_path, pattern) for pattern in ALLOWED_PATHS[agent])


def main() -> int:
    """Read the hook payload from stdin and allow or block the write."""
    payload = json.load(sys.stdin)
    agent = payload.get("agent_type")
    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not agent or not file_path:
        return 0

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", payload.get("cwd", "."))).resolve()
    target = Path(file_path).resolve()
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError:
        sys.stderr.write(f"{agent} may not write outside the project: {target}\n")
        return 2

    if is_allowed(agent, relative):
        return 0
    allowed = ", ".join(ALLOWED_PATHS[agent]) or "nothing (read-only)"
    sys.stderr.write(
        f"{agent} may not write {relative}. Allowed: {allowed}. See CLAUDE.md > Roles.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
