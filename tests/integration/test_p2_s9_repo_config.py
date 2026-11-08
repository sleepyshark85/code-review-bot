r"""P2-S9: set the review policy per repository with `.review-bot.toml`, read from the base commit.

Written from the spec's criteria, design.md (Config, Commands and exit codes, Safety, CI settings
and caps) and ADR 0008 while the developer built the slice, before any of its code was read.

Every run calls `review_bot.pr.cli.main(["run", "--event", PATH, "--repo", DIR, "--out", OUT,
"--provider", "mock", "--mode", MODE, *FLAGS], provider=P[, github=FAKE])` in process, as
P2-S2's and P2-S5's suites do (the one place to change: `run_pr`). Nothing reaches a model, the
network or GitHub.

Each pull request is a real git repository in `tmp_path`: a base commit on `main` that may hold
`.review-bot.toml` (as a file, a symlink or a directory, built in git's index so no symlink
support is needed on disk), then one head commit on `feature`. The head is left checked out, so
a run that read the working tree instead of the base commit would see the head's file.

The `config` key of `shadow.json` is read as an object with `threshold`, `exclude`,
`max_open_comments` and `source` (the spec's AC1 and AC2 name these).
"""

import importlib
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_p2_s1_shadow_report_local import load_shadow, summary_text
from test_p2_s2_review_pr_event import GitRepo, event
from test_p2_s5_comment_sync import GitHub

from review_bot.findings import Finding
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

CONFIG = ".review-bot.toml"
USAGE_EXIT = 2
DEFAULT_THRESHOLD = "low"  # Settings' default
DEFAULT_MAX_OPEN_COMMENTS = 10
THRESHOLD_VARIABLE = "REVIEW_BOT_THRESHOLD"
REVIEW_BYTES_VARIABLE = "REVIEW_BOT_MAX_REVIEW_BYTES"
# Generous: it catches a hang or exponential matching, not a slow CI machine.
BOUND_S = 60.0

APP = "src/app.py"
APP_LINES = 6
APP_TEXT = "".join(f"value_{n} = {n}\n" for n in range(1, APP_LINES + 1))
SEVERITIES = ("low", "medium", "high", "critical")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub Actions sets `CI`, and a developer may set the threshold; each test sets its own."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv(THRESHOLD_VARIABLE, raising=False)
    monkeypatch.delenv(REVIEW_BYTES_VARIABLE, raising=False)


# A pull request whose base may hold the config.


@dataclass(frozen=True, slots=True)
class PullRequest:
    repo: GitRepo
    base: str
    head: str


def write_files(root: Path, files: dict[str, str | bytes]) -> None:
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def make_pr(
    path: Path,
    head_files: dict[str, str | bytes],
    *,
    base_config: str | bytes | None = None,
    config_as: str = "file",
) -> PullRequest:
    """Base on `main` with `base_config` at the root as `config_as`; head on `feature`, checked out.

    `config_as` is `file`, `symlink` (a link whose target is `base_config`'s file) or
    `directory` (a directory at the config's path holding `base_config`).
    """
    repo = GitRepo(path)
    write_files(path, {"src/base.py": "base = 1\n"})
    if base_config is not None and config_as == "file":
        write_files(path, {CONFIG: base_config})
    if base_config is not None and config_as == "directory":
        write_files(path, {f"{CONFIG}/inner.toml": base_config})
    repo.git("add", "-A")
    if base_config is not None and config_as == "symlink":
        write_files(path, {"real.toml": base_config})
        repo.git("add", "real.toml")
        target = path / "link-target.txt"
        target.write_bytes(b"real.toml")
        blob = repo.git("hash-object", "-w", "--", str(target)).strip()
        target.unlink()
        repo.git("update-index", "--add", "--cacheinfo", f"120000,{blob},{CONFIG}")
    repo.git("commit", "-q", "-m", "base")
    base = repo.git("rev-parse", "HEAD").strip()
    repo.branch("feature")
    write_files(path, head_files)
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "head")
    head = repo.git("rev-parse", "HEAD").strip()
    return PullRequest(repo, base, head)


# Running the command.


@dataclass(frozen=True, slots=True)
class Run:
    exit_code: int
    stderr: str
    out: Path
    provider: MockProvider

    @property
    def shadow(self) -> dict[str, Any]:
        return load_shadow(self.out)

    @property
    def config(self) -> dict[str, Any]:
        config: dict[str, Any] = self.shadow["config"]
        return config

    @property
    def planned(self) -> list[tuple[str, int, str]]:
        """Each planned comment's (file, line, severity), in the report's order."""
        return [(c["file"], c["line"], c["severity"]) for c in self.shadow["comments"]]

    @property
    def prompt(self) -> str:
        (request,) = self.provider.requests
        return request.user_prompt


def run_pr(  # noqa: PLR0913 - keyword-only knobs, each defaulting to an ordinary shadow run
    pull: PullRequest,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    findings: Sequence[Finding] = (),
    *flags: str,
    mode: str = "shadow",
    github: Any = None,
) -> Run:
    pr_cli = importlib.import_module("review_bot.pr.cli")
    runs = len(list(tmp_path.glob("event-*.json")))
    event_path = tmp_path / f"event-{runs}.json"
    event_path.write_text(json.dumps(event(pull.base, pull.head)), encoding="utf-8")
    out = tmp_path / f"out-{runs}"
    provider = MockProvider(findings=list(findings))
    extra = {} if github is None else {"github": github}
    capsys.readouterr()
    exit_code = pr_cli.main(
        [
            "run", "--event", str(event_path), "--repo", str(pull.repo.path), "--out", str(out),
            "--provider", "mock", "--mode", mode, *flags,
        ],
        provider=provider,
        **extra,
    )  # fmt: skip
    return Run(exit_code, capsys.readouterr().err, out, provider)


def finding(line: int, severity: str = "high", file: str = APP) -> Finding:
    return Finding(
        file=file, line=line, severity=severity, category="logic", rationale="R.", confidence=0.9
    )


def one_of_each_severity() -> list[Finding]:
    """A `low`, `medium`, `high` and `critical` finding on APP's lines 1 to 4."""
    return [finding(line, severity) for line, severity in enumerate(SEVERITIES, start=1)]


def severities(run: Run) -> list[str]:
    return sorted((severity for _, _, severity in run.planned), key=SEVERITIES.index)


def toml_list(items: Sequence[str]) -> str:
    return "[" + ", ".join(json.dumps(item) for item in items) + "]"


# AC1: no file at the base: the defaults apply, and `config.source` is `default`.


def test_p2_s9_ac1_no_file_applies_the_defaults_and_reports_them(tmp_path, capsys):
    pull = make_pr(tmp_path / "clone", {APP: APP_TEXT})

    run = run_pr(pull, tmp_path, capsys, one_of_each_severity())

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    config = run.config
    assert config["source"] == "default"
    assert config["threshold"] == DEFAULT_THRESHOLD
    assert config["exclude"] == []
    assert config["max_open_comments"] == DEFAULT_MAX_OPEN_COMMENTS
    assert severities(run) == list(SEVERITIES)


def test_p2_s9_ac1_a_file_only_the_head_adds_is_ignored(tmp_path, capsys):
    """A PR brings no rules of its own: not an exclusion, a threshold or even broken TOML."""
    head_config = 'threshold = "critical"\nexclude = ["*.py"]\nnot toml [[[\n'
    pull = make_pr(tmp_path / "clone", {APP: APP_TEXT, CONFIG: head_config})

    run = run_pr(pull, tmp_path, capsys, one_of_each_severity())

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    assert run.config["source"] == "default"
    assert "value_1 = 1" in run.prompt
    assert severities(run) == list(SEVERITIES)


# AC2: the base's threshold applies, whatever the head changes it to.


def test_p2_s9_ac2_base_threshold_medium_drops_low_findings(tmp_path, capsys):
    pull = make_pr(tmp_path / "clone", {APP: APP_TEXT}, base_config='threshold = "medium"\n')

    run = run_pr(pull, tmp_path, capsys, one_of_each_severity())

    assert run.exit_code == 0, run.stderr
    assert severities(run) == ["medium", "high", "critical"]
    assert run.config["source"] == f"{CONFIG}@{pull.base}"
    assert run.config["threshold"] == "medium"


def test_p2_s9_ac2_head_raising_the_threshold_doesnt_change_its_own_review(tmp_path, capsys):
    pull = make_pr(
        tmp_path / "clone",
        {APP: APP_TEXT, CONFIG: 'threshold = "critical"\n'},
        base_config='threshold = "medium"\n',
    )

    run = run_pr(pull, tmp_path, capsys, one_of_each_severity())

    assert run.exit_code == 0, run.stderr
    assert severities(run) == ["medium", "high", "critical"]
    assert run.config["source"] == f"{CONFIG}@{pull.base}"


# AC3: `exclude` globs on the new path, with the spec's nested-path examples.

SKIPPED = ("generated/a.py", "generated/x/y.py", "a.snap", "tests/x/a.snap", "src/a.json")
NOT_SKIPPED = ("src/generated/a.py", "src/x/a.json")
EXCLUDE = ("generated/**", "*.snap", "src/*.json")


def canary(path: str) -> str:
    """A line unique to `path`, so the prompt shows whether that file was sent."""
    return "CANARY_" + re.sub(r"[^0-9A-Za-z]", "_", path)


def test_p2_s9_ac3_exactly_the_matching_files_are_skipped_and_not_sent(tmp_path, capsys):
    head = {path: f'"{canary(path)}"\n' for path in (*SKIPPED, *NOT_SKIPPED)}
    pull = make_pr(tmp_path / "clone", head, base_config=f"exclude = {toml_list(EXCLUDE)}\n")

    run = run_pr(pull, tmp_path, capsys)

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    skipped = run.shadow["report"]["skipped_files"]
    assert sorted((s["file"], s["reason"]) for s in skipped) == sorted(
        (path, "excluded_by_config") for path in SKIPPED
    )
    prompt = run.prompt
    for path in SKIPPED:
        assert canary(path) not in prompt, path
    for path in NOT_SKIPPED:
        assert canary(path) in prompt, path
    assert run.config["exclude"] == list(EXCLUDE)


def test_p2_s9_ac3_excluded_files_dont_count_towards_reviewable_bytes(
    tmp_path, capsys, monkeypatch
):
    """A large excluded file under a small cap: reviewed with it excluded, too large without."""
    monkeypatch.setenv(REVIEW_BYTES_VARIABLE, "2000")
    head = {APP: APP_TEXT, "fixtures/big.py": "".join(f"row_{n} = {n}\n" for n in range(2000))}
    excluded = make_pr(tmp_path / "excluded", head, base_config='exclude = ["fixtures/**"]\n')
    included = make_pr(tmp_path / "included", head)

    excluded_run = run_pr(excluded, tmp_path, capsys)
    included_run = run_pr(included, tmp_path, capsys)

    assert (included_run.shadow["outcome"], included_run.shadow["reason"]) == (
        "skipped",
        "too_large",
    )
    assert excluded_run.exit_code == 0, excluded_run.stderr
    assert excluded_run.shadow["outcome"] == "reviewed"
    assert "row_1 = 1" not in excluded_run.prompt


def test_p2_s9_ac3_a_finding_on_an_excluded_file_isnt_planned(tmp_path, capsys):
    """An excluded file wasn't reviewed, so nothing may be commented on it."""
    head = {APP: APP_TEXT, "generated/a.py": APP_TEXT}
    pull = make_pr(tmp_path / "clone", head, base_config='exclude = ["generated/**"]\n')

    run = run_pr(pull, tmp_path, capsys, [finding(2), finding(2, file="generated/a.py")])

    assert run.exit_code == 0, run.stderr
    assert run.planned == [(APP, 2, "high")]


def test_p2_s9_limits_nested_double_star_patterns_match_in_bounded_time(tmp_path, capsys):
    """50 patterns of 60 `**` segments each against a deep path none of them matches.

    A backtracking translation of `**` is exponential here; the spec says matching is linear.
    """
    deep = "/".join(["d"] * 30) + "/deep.py"
    patterns = ["**/" * 60 + f"nomatch{n}" for n in range(50)]
    assert all(len(pattern) <= 200 for pattern in patterns)
    pull = make_pr(
        tmp_path / "clone",
        {APP: APP_TEXT, deep: "deep = 1\n"},
        base_config=f"exclude = {toml_list(patterns)}\n",
    )

    started = time.monotonic()
    run = run_pr(pull, tmp_path, capsys)
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S
    assert run.exit_code == 0, run.stderr
    assert run.shadow["report"]["skipped_files"] == []
    assert "deep = 1" in run.prompt


# AC4: `max_open_comments` from the file caps the comments a run creates.


def test_p2_s9_ac4_max_open_comments_3_creates_3_and_withholds_2(tmp_path, capsys):
    pull = make_pr(tmp_path / "clone", {APP: APP_TEXT}, base_config="max_open_comments = 3\n")
    github = GitHub()

    run = run_pr(
        pull,
        tmp_path,
        capsys,
        [finding(line) for line in range(1, 6)],
        mode="comment",
        github=github.client,
    )

    assert run.exit_code == 0, run.stderr
    posted = [c for call in github.calls() if call.name == "post_review" for c in call.args[1]]
    assert len(posted) == 3
    assert len(github.comments()) == 3
    assert [e["action"] for e in run.shadow["comments"]] == ["create"] * 3
    assert [w["reason"] for w in run.shadow["withheld"]] == ["over_limit"] * 2
    assert run.config["max_open_comments"] == 3


# AC5: an invalid config is outcome `failed`, `invalid_config`, no provider call, exit 2.

PATTERN_OVER_200 = "a" * 201
SIZE_PROBLEM = r"(?i)65,?536|64 ?KB|over the limit"
INVALID_CONFIGS: dict[str, tuple[str | bytes, str]] = {
    # name: (the file's content, a pattern the summary must match to name the key or problem)
    "invalid_toml": ('threshold = "medium\n', r"TOML"),
    "unknown_key": ('colour = "red"\n', r"colour"),
    "cap_max_review_bytes": ("max_review_bytes = 999999999\n", r"max_review_bytes"),
    "cap_max_output_tokens": ("max_output_tokens = 999999999\n", r"max_output_tokens"),
    "mode": ('mode = "comment"\n', r"\bmode\b"),
    "nested_table": ("[caps]\nmax_retries = 9\n", r"caps"),
    "threshold_not_a_string": ("threshold = 3\n", r"threshold"),
    "exclude_not_a_list": ('exclude = "*.py"\n', r"exclude"),
    "exclude_item_not_a_string": ("exclude = [1]\n", r"exclude"),
    "max_open_comments_a_string": ('max_open_comments = "3"\n', r"max_open_comments"),
    "max_open_comments_a_bool": ("max_open_comments = true\n", r"max_open_comments"),
    "max_open_comments_0": ("max_open_comments = 0\n", r"max_open_comments"),
    "max_open_comments_26": ("max_open_comments = 26\n", r"max_open_comments"),
    "threshold_urgent": ('threshold = "urgent"\n', r"threshold"),
    "51_patterns": (f"exclude = {toml_list([f'p{n}/**' for n in range(51)])}\n", r"exclude"),
    "pattern_over_200": (f"exclude = {toml_list([PATTERN_OVER_200])}\n", r"exclude"),
    "over_64_kb": ("# padding\n" * 6554 + 'threshold = "high"\n', SIZE_PROBLEM),
    "not_utf8": (b'threshold = "\xff"\n', r"(?i)TOML|UTF-8|encod"),
}


def assert_invalid_config(run: Run, problem: str) -> None:
    assert run.exit_code == USAGE_EXIT, run.stderr
    document = run.shadow
    assert (document["outcome"], document["reason"]) == ("failed", "invalid_config")
    assert run.provider.requests == []
    shown = summary_text(document)  # as GitHub shows it, Markdown escapes removed
    assert re.search(problem, shown), shown


@pytest.mark.parametrize(
    ("content", "problem"), list(INVALID_CONFIGS.values()), ids=list(INVALID_CONFIGS)
)
def test_p2_s9_ac5_invalid_config_fails_invalid_config_without_a_call_and_exits_2(
    tmp_path, capsys, content, problem
):
    pull = make_pr(tmp_path / "clone", {APP: APP_TEXT}, base_config=content)

    run = run_pr(pull, tmp_path, capsys, [finding(1)])

    assert_invalid_config(run, problem)


@pytest.mark.parametrize(
    ("config_as", "problem"),
    [("symlink", r"(?i)link|regular file|not a file"), ("directory", r"(?i)director|not a file")],
)
def test_p2_s9_ac5_symlink_or_directory_at_the_path_fails_invalid_config(
    tmp_path, capsys, config_as, problem
):
    pull = make_pr(
        tmp_path / "clone",
        {APP: APP_TEXT},
        base_config='threshold = "medium"\n',
        config_as=config_as,
    )

    run = run_pr(pull, tmp_path, capsys, [finding(1)])

    assert_invalid_config(run, problem)


def test_p2_s9_ac5_limits_a_huge_file_fails_fast(tmp_path, capsys):
    """Several megabytes at the path: refused on size within the bound, not parsed."""
    pull = make_pr(tmp_path / "clone", {APP: APP_TEXT}, base_config="# x\n" * 1_000_000)

    started = time.monotonic()
    run = run_pr(pull, tmp_path, capsys, [finding(1)])
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S
    assert_invalid_config(run, SIZE_PROBLEM)


def test_p2_s9_ac5_limits_boundaries_are_accepted(tmp_path, capsys):
    """The largest valid values: 50 patterns of 200 characters, 25 comments, under 64 KB."""
    patterns = [f"p{n:02d}/" + "x" * 196 for n in range(50)]
    assert {len(pattern) for pattern in patterns} == {200}
    content = f"max_open_comments = 25\nexclude = {toml_list(patterns)}\n"
    content += "# padding\n" * ((60_000 - len(content)) // 10)
    assert len(content.encode()) < 64_000
    pull = make_pr(tmp_path / "clone", {APP: APP_TEXT}, base_config=content)

    run = run_pr(pull, tmp_path, capsys, [finding(1)])

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    assert run.config["max_open_comments"] == 25


# AC6: flag, then environment, then file, then default.


@pytest.mark.parametrize(
    ("flag", "env", "expected"),
    [
        (None, "high", ["high", "critical"]),
        ("critical", "high", ["critical"]),
        ("low", None, list(SEVERITIES)),
    ],
    ids=["env_over_file", "flag_over_env_and_file", "lower_flag_over_file"],
)
def test_p2_s9_ac6_flag_then_environment_then_file(  # noqa: PLR0913, PLR0917 - fixtures, parameters
    tmp_path, capsys, monkeypatch, flag, env, expected
):
    if env is not None:
        monkeypatch.setenv(THRESHOLD_VARIABLE, env)
    flags = () if flag is None else ("--threshold", flag)
    pull = make_pr(tmp_path / "clone", {APP: APP_TEXT}, base_config='threshold = "medium"\n')

    run = run_pr(pull, tmp_path, capsys, one_of_each_severity(), *flags)

    assert run.exit_code == 0, run.stderr
    assert severities(run) == expected
