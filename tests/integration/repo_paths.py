"""Where the repository is, for tests that read its files or run from its root."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
