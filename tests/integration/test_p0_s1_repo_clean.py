"""P0-S1 AC17 and AC18: the repository is free of `openai` and of exclusions for `cli.py`."""

import ast
import fnmatch
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from repo_paths import REPO_ROOT

pytestmark = pytest.mark.integration

SRC = REPO_ROOT / "src"
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
INHERITED_SCRIPT = REPO_ROOT / "Assignment" / "review_bot.py"
CLI_PATH = "src/review_bot/cli.py"

OPENAI_PACKAGE = re.compile(r"""["']openai(?![\w.-])""", re.IGNORECASE)
DYNAMIC_IMPORT = re.compile(r"""(?:import_module|__import__)\(\s*["']openai\b""")
KEY_LITERAL = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]+")


def imports_openai(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        if any(module.split(".")[0] == "openai" for module in modules):
            return True
    return DYNAMIC_IMPORT.search(source) is not None


def src_files() -> list[Path]:
    return [p for p in SRC.rglob("*") if p.is_file() and "__pycache__" not in p.parts]


@pytest.mark.parametrize("path", [PYPROJECT, UV_LOCK], ids=["pyproject", "uv_lock"])
def test_p0_s1_ac17_package_files_list_no_openai(path):
    assert OPENAI_PACKAGE.search(path.read_text("utf-8")) is None


def test_p0_s1_ac17_src_contains_no_openai_import():
    sources = [path for path in src_files() if path.suffix == ".py"]

    assert [path for path in sources if imports_openai(path.read_text("utf-8"))] == []


def test_p0_s1_ac17_src_contains_no_key_literal():
    files = src_files()

    assert [p for p in files if KEY_LITERAL.search(p.read_text("utf-8", errors="replace"))] == []


def test_p0_s1_ac17_control_checkers_see_the_inherited_script():
    source = INHERITED_SCRIPT.read_text("utf-8")

    assert imports_openai(source)
    assert KEY_LITERAL.search(source)


@pytest.mark.parametrize(
    "text",
    ['dependencies = ["openai>=1"]', 'dev = ["OpenAI[datalib]==1.0"]', 'name = "openai"'],
    ids=["requirement", "extras_and_case", "lock_entry"],
)
def test_p0_s1_ac17_control_package_check_sees(text):
    assert OPENAI_PACKAGE.search(text)


def glob_covers(pattern: str) -> bool:
    """Whether a ruff or coverage glob reaches `cli.py`, directly or through a parent directory.

    Ruff also matches bare names, and coverage absolute paths. `fnmatch`'s `*` crosses `/`, so it
    stands in for both tools' `**`.
    """
    glob = pattern.strip().removeprefix("./").rstrip("/").replace("**/", "*").replace("/**", "/*")
    targets = ["src", "src/review_bot", "review_bot", "cli.py", "review_bot/cli.py", CLI_PATH]
    return any(fnmatch.fnmatchcase(target, glob) for target in [*targets, f"/repo/{CLI_PATH}"])


def exclude_values(node: object) -> Iterator[str]:
    """Yield every `exclude` in the parsed config: at top level or on a hook."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "exclude":
                yield value
            else:
                yield from exclude_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from exclude_values(item)


def pre_commit_exclusions(config_text: str) -> list[str]:
    # `re.search`, the way pre-commit itself applies `exclude`.
    return [p for p in exclude_values(yaml.safe_load(config_text)) if re.search(p, CLI_PATH)]


def ruff_exclusions(toml_text: str) -> list[str]:
    ruff = tomllib.loads(toml_text).get("tool", {}).get("ruff", {})
    lint = ruff.get("lint", {})
    patterns = [
        *ruff.get("exclude", []),
        *ruff.get("extend-exclude", []),
        *lint.get("exclude", []),
        *ruff.get("format", {}).get("exclude", []),
    ]
    for table in (ruff, lint):
        patterns += [*table.get("per-file-ignores", {}), *table.get("extend-per-file-ignores", {})]
    return [pattern for pattern in patterns if glob_covers(pattern)]


def mypy_exclusions(toml_text: str) -> list[str]:
    mypy = tomllib.loads(toml_text).get("tool", {}).get("mypy", {})
    excludes = mypy.get("exclude", [])
    found = [
        pattern
        for pattern in ([excludes] if isinstance(excludes, str) else excludes)
        if re.search(pattern, CLI_PATH)
    ]
    if mypy.get("ignore_errors"):
        found.append("ignore_errors")
    for override in mypy.get("overrides", []):
        modules = override.get("module", [])
        modules = [modules] if isinstance(modules, str) else modules
        is_silenced = override.get("ignore_errors") or override.get("follow_imports") == "skip"
        if is_silenced and any(fnmatch.fnmatchcase("review_bot.cli", m) for m in modules):
            found.append(f"overrides {modules}")
    return found


def coverage_exclusions(toml_text: str) -> list[str]:
    coverage = tomllib.loads(toml_text).get("tool", {}).get("coverage", {})
    patterns = [
        *coverage.get("run", {}).get("omit", []),
        *coverage.get("report", {}).get("omit", []),
    ]
    return [pattern for pattern in patterns if glob_covers(pattern)]


@pytest.mark.parametrize(
    ("path", "exclusions"),
    [
        (PRE_COMMIT_CONFIG, pre_commit_exclusions),
        (PYPROJECT, ruff_exclusions),
        (PYPROJECT, mypy_exclusions),
        (PYPROJECT, coverage_exclusions),
    ],
    ids=["pre_commit", "ruff", "mypy", "coverage"],
)
def test_p0_s1_ac18_no_exclusion_covers_cli(path, exclusions):
    assert exclusions(path.read_text("utf-8")) == []


@pytest.mark.parametrize(
    ("exclusions", "config"),
    [
        (pre_commit_exclusions, r"exclude: ^(Assignment/|src/review_bot/cli\.py$)"),
        (pre_commit_exclusions, "repos:\n  - hooks:\n      - id: x\n        exclude: ^src/"),
        (pre_commit_exclusions, "exclude: |\n  (?x)^(\n    src/review_bot/cli\\.py\n  )$"),
        (ruff_exclusions, '[tool.ruff]\nextend-exclude = ["src/review_bot/cli.py"]'),
        (ruff_exclusions, '[tool.ruff.lint.per-file-ignores]\n"**/cli.py" = ["T201"]'),
        (mypy_exclusions, "[tool.mypy]\nexclude = ['^src/review_bot/cli\\.py$']"),
        (mypy_exclusions, '[[tool.mypy.overrides]]\nmodule = "review_bot.*"\nignore_errors = true'),
        (coverage_exclusions, '[tool.coverage.run]\nomit = ["*/review_bot/cli.py"]'),
    ],
    ids=[
        "pre_commit_baseline_spelling",
        "pre_commit_on_a_hook",
        "pre_commit_verbose_block",
        "ruff_baseline_spelling",
        "ruff_per_file_ignore_of_one_rule",
        "mypy_baseline_spelling",
        "mypy_override",
        "coverage_baseline_spelling",
    ],
)
def test_p0_s1_ac18_control_checker_sees(exclusions, config):
    assert exclusions(config) != []


@pytest.mark.parametrize(
    ("exclusions", "config"),
    [
        (pre_commit_exclusions, "exclude: ^Assignment/"),
        (ruff_exclusions, '[tool.ruff]\nextend-exclude = ["Assignment"]'),
        (ruff_exclusions, '[tool.ruff.lint.per-file-ignores]\n"tests/**" = ["S101"]'),
    ],
    ids=["pre_commit", "ruff_exclude", "ruff_per_file_ignores"],
)
def test_p0_s1_ac18_control_checker_allows_other_paths(exclusions, config):
    assert exclusions(config) == []
