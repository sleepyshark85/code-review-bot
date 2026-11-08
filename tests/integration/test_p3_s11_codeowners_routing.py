r"""P3-S11: route findings to code owners, read from the base commit's CODEOWNERS.

Written from the spec's criteria, Limits and Interfaces, design.md (Pipeline, Interfaces' report
keys, Q8) and ADR 0008 while the developer built the slice, before any of its code was read.

Every run calls `review_bot.pr.cli.main(["run", "--event", PATH, "--repo", DIR, "--out", OUT,
"--provider", NAME, "--mode", MODE], provider=P[, github=FAKE])` in process, as P2-S2's, P2-S5's
and P2-S9's suites do (the one place to change: `run_pr`). `FAKE` is P2-S5's in-memory GitHub.
Nothing reaches a model, the network or GitHub.

Each pull request is a real git repository in `tmp_path`: a base commit on `main` that may hold
CODEOWNERS files (a file, or a symbolic link built in git's index so no symlink support is needed
on disk), then one head commit on `feature`, left checked out, so a run that read the working
tree or the head instead of the base commit would see the head's CODEOWNERS.

`shadow.json` is read through P2-S1's `load_shadow`, whose key lists now append `codeowners`
to the document and `owners` to each comment and withheld entry (spec Interfaces). The summary
is read as GitHub-flavoured Markdown by P1-S11's parser: the `Owners:` section is the list right
after the line `Owners:`, one item per owner, then `and K more`, then `no owner: N`.
"""

import importlib
import json
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from markdown_it.token import Token
from test_p1_s11_markdown_output import (
    GFM_TRIGGER,
    PARSER,
    Item,
    inline_children,
    list_items,
    outside_code,
    visible,
)
from test_p2_s1_shadow_report_local import load_shadow
from test_p2_s2_review_pr_event import GitRepo, event
from test_p2_s5_comment_sync import GitHub

from review_bot.findings import Finding
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

GITHUB_CODEOWNERS = ".github/CODEOWNERS"
ROOT_CODEOWNERS = "CODEOWNERS"
DOCS_CODEOWNERS = "docs/CODEOWNERS"
ABSENT = {"source": None, "status": "absent", "rules": 0, "invalid_lines": 0}
MODES = ("shadow", "comment")
DEFAULT_MAX_OPEN_COMMENTS = 10
# Over 3 MB however it is counted (3,000,000 bytes or 3 MiB), and under it however counted.
OVER_3_MB = 3_300_000
UNDER_3_MB = 2_900_000
# Generous: it catches a hang or exponential matching, not a slow CI machine.
BOUND_S = 60.0

AC1_CODEOWNERS = "* @org/core\n/billing/ @org/billing alice@example.com\n*.js @docs\n"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub Actions sets `CI`; AC8's `no_credential` case sets it on purpose."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("REVIEW_BOT_THRESHOLD", raising=False)


# A pull request whose base may hold CODEOWNERS.


@dataclass(frozen=True, slots=True)
class PullRequest:
    repo: GitRepo
    base: str
    head: str


def write_files(root: Path, files: Mapping[str, str | bytes]) -> None:
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def add_symlink(repo: GitRepo, link: str, target: str) -> None:
    """Stage `link` as a symbolic link to `target` in git's index only (mode 120000)."""
    scratch = repo.path / "link-target.tmp"
    scratch.write_bytes(target.encode("utf-8"))
    blob = repo.git("hash-object", "-w", "--", str(scratch)).strip()
    scratch.unlink()
    repo.git("update-index", "--add", "--cacheinfo", f"120000,{blob},{link}")


def make_pr(
    path: Path,
    head_files: Mapping[str, str | bytes],
    base_files: Mapping[str, str | bytes] | None = None,
    *,
    base_symlinks: Mapping[str, str] | None = None,
    head_deletes: Sequence[str] = (),
) -> PullRequest:
    """Base on `main` with `base_files` and `base_symlinks`; head on `feature`, checked out.

    The head commit stages only `head_files` and `head_deletes`, so a symlink that exists only in
    the index is left as the base committed it.
    """
    repo = GitRepo(path)
    write_files(path, {"src/base.py": "base = 1\n", **(base_files or {})})
    repo.git("add", "-A")
    for link, target in (base_symlinks or {}).items():
        add_symlink(repo, link, target)
    repo.git("commit", "-q", "-m", "base")
    base = repo.git("rev-parse", "HEAD").strip()
    repo.branch("feature")
    write_files(path, head_files)
    if head_files:
        repo.git("add", "--", *head_files)
    for name in head_deletes:
        repo.git("rm", "-q", "--", name)
    repo.git("commit", "-q", "-m", "head")
    head = repo.git("rev-parse", "HEAD").strip()
    return PullRequest(repo, base, head)


def code(lines: int = 3) -> str:
    return "".join(f"value_{n} = {n}\n" for n in range(1, lines + 1))


# Running the command and reading what it writes.


@dataclass(frozen=True, slots=True)
class Run:
    exit_code: int
    stderr: str
    out: Path
    provider: MockProvider
    github: GitHub | None

    @property
    def shadow(self) -> dict[str, Any]:
        return load_shadow(self.out)

    @property
    def codeowners(self) -> dict[str, Any]:
        codeowners: dict[str, Any] = self.shadow["codeowners"]
        return codeowners

    @property
    def entries(self) -> list[dict[str, Any]]:
        """Every comment and withheld entry, comments first."""
        document = self.shadow
        return [*document["comments"], *document["withheld"]]

    def owners_of(self, file: str) -> list[list[str]]:
        """The `owners` of every entry on `file`; one list per entry."""
        return [entry["owners"] for entry in self.entries if entry["file"] == file]

    @property
    def summary_body(self) -> str:
        summary: str = self.shadow["summary_body"]
        return summary

    @property
    def bodies(self) -> list[tuple[str, int, str]]:
        return [(c["file"], c["line"], c["body"]) for c in self.shadow["comments"]]


def run_pr(  # noqa: PLR0913 - keyword-only knobs, each defaulting to an ordinary shadow run
    pull: PullRequest,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    findings: Sequence[Finding] = (),
    *,
    mode: str = "shadow",
    provider_name: str = "mock",
    event_payload: dict[str, Any] | None = None,
) -> Run:
    pr_cli = importlib.import_module("review_bot.pr.cli")
    runs = len(list(tmp_path.glob("event-*.json")))
    event_path = tmp_path / f"event-{runs}.json"
    payload = event_payload if event_payload is not None else event(pull.base, pull.head)
    event_path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / f"out-{runs}"
    provider = MockProvider(findings=list(findings))
    github = GitHub() if mode == "comment" else None
    extra = {} if github is None else {"github": github.client}
    capsys.readouterr()
    exit_code = pr_cli.main(
        [
            "run", "--event", str(event_path), "--repo", str(pull.repo.path), "--out", str(out),
            "--provider", provider_name, "--mode", mode,
        ],
        provider=provider,
        **extra,
    )  # fmt: skip
    return Run(exit_code, capsys.readouterr().err, out, provider, github)


def finding(file: str, line: int = 1) -> Finding:
    return Finding(
        file=file, line=line, severity="high", category="logic", rationale="R.", confidence=0.9
    )


def findings_on(files: Mapping[str, int]) -> list[Finding]:
    """`count` findings on lines 1 to `count` of each file."""
    return [finding(file, line) for file, count in files.items() for line in range(1, count + 1)]


def reviewed(run: Run) -> None:
    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed", run.shadow["reason"]


def found(pull: PullRequest, path: str, rules: int, invalid_lines: int = 0) -> dict[str, Any]:
    return {
        "source": f"{path}@{pull.base}",
        "status": "found",
        "rules": rules,
        "invalid_lines": invalid_lines,
    }


# The summary's `Owners:` section.


def _list_end(tokens: list[Token], start: int) -> int:
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index].type.endswith("_list_open"):
            depth += 1
        elif tokens[index].type.endswith("_list_close"):
            depth -= 1
            if depth == 0:
                return index
    return len(tokens) - 1


def owner_section(markdown: str) -> list[Item] | None:
    """The list after the line `Owners:` (however emphasised), or None if there's no such line."""
    tokens = PARSER.parse(markdown)
    for index, token in enumerate(tokens):
        if token.type != "inline":
            continue
        lines = visible(token.children or ()).splitlines()
        if not lines or lines[-1].strip() != "Owners:":
            continue
        following = index + 1
        while following < len(tokens) and tokens[following].type == "paragraph_close":
            following += 1
        if following < len(tokens) and tokens[following].type.endswith("_list_open"):
            return list_items(tokens[following : _list_end(tokens, following) + 1])
        return []
    return None


def owner_texts(markdown: str) -> list[str]:
    section = owner_section(markdown)
    assert section is not None, f"no Owners: section in\n{markdown}"
    return [item.text for item in section]


def assert_owners_in_code_spans(markdown: str) -> None:
    """Each `owner: N` item shows the owner as exactly one code span."""
    for item in owner_section(markdown) or []:
        if item.text.startswith(("and ", "no owner: ")):
            continue
        owner = item.text.rsplit(": ", 1)[0]
        assert item.code_spans == (owner,), item


def assert_no_mention_outside_code(markdown: str) -> None:
    """P2-S1 AC11's check: no `@` (so no mention or email) and no link outside a code span."""
    tokens = PARSER.parse(markdown)
    text = outside_code(tokens)
    assert "@" not in text, f"an @ outside a code span:\n{markdown}"
    assert not GFM_TRIGGER.findall(text), f"{GFM_TRIGGER.findall(text)} outside a code span"
    assert not any(c.type == "link_open" for c in inline_children(tokens)), "a link formed"


# AC1: the last matching line wins, and each entry's `owners` keeps the line's order.


@pytest.mark.parametrize("mode", MODES)
def test_p3_s11_ac1_last_matching_line_gives_the_owners(tmp_path, capsys, mode):
    head = {"billing/invoice.py": code(), "api/views.py": code(), "billing/widget.js": code()}
    pull = make_pr(tmp_path / "clone", head, {GITHUB_CODEOWNERS: AC1_CODEOWNERS})

    run = run_pr(pull, tmp_path, capsys, findings_on(dict.fromkeys(head, 1)), mode=mode)

    reviewed(run)
    assert run.owners_of("billing/invoice.py") == [["@org/billing", "alice@example.com"]]
    assert run.owners_of("api/views.py") == [["@org/core"]]
    assert run.owners_of("billing/widget.js") == [["@docs"]]
    assert run.codeowners == found(pull, GITHUB_CODEOWNERS, rules=3)


def test_p3_s11_ac1_an_earlier_line_doesnt_win_over_a_later_broader_one(tmp_path, capsys):
    """Order, not specificity: a later `*` overrides the earlier `/billing/` for every path."""
    owners_file = "/billing/ @org/billing\n* @org/core\n"
    pull = make_pr(tmp_path / "clone", {"billing/a.py": code()}, {GITHUB_CODEOWNERS: owners_file})

    run = run_pr(pull, tmp_path, capsys, [finding("billing/a.py")])

    reviewed(run)
    assert run.owners_of("billing/a.py") == [["@org/core"]]


# AC2: only the first CODEOWNERS that exists, at the base, is read.

LOCATED = {
    "all_three": (
        {GITHUB_CODEOWNERS: "* @org/github\n", ROOT_CODEOWNERS: "* @org/root\n",
         DOCS_CODEOWNERS: "* @org/docs\n"},
        GITHUB_CODEOWNERS, "@org/github",
    ),
    "root_and_docs": (
        {ROOT_CODEOWNERS: "* @org/root\n", DOCS_CODEOWNERS: "* @org/docs\n"},
        ROOT_CODEOWNERS, "@org/root",
    ),
    "docs_only": ({DOCS_CODEOWNERS: "* @org/docs\n"}, DOCS_CODEOWNERS, "@org/docs"),
}  # fmt: skip


@pytest.mark.parametrize(("base_files", "source", "owner"), LOCATED.values(), ids=LOCATED)
@pytest.mark.parametrize("mode", MODES)
def test_p3_s11_ac2_only_the_first_existing_location_is_read(  # noqa: PLR0913, PLR0917
    tmp_path, capsys, mode, base_files, source, owner
):
    pull = make_pr(tmp_path / "clone", {"src/app.py": code()}, base_files)

    run = run_pr(pull, tmp_path, capsys, [finding("src/app.py")], mode=mode)

    reviewed(run)
    assert run.owners_of("src/app.py") == [[owner]]
    assert run.codeowners == found(pull, source, rules=1)


def test_p3_s11_ac2_first_location_with_no_rules_still_hides_the_others(tmp_path, capsys):
    """`.github/CODEOWNERS` exists with only a comment: it is the file read, so nobody owns."""
    base_files = {GITHUB_CODEOWNERS: "# nobody yet\n", ROOT_CODEOWNERS: "* @org/root\n"}
    pull = make_pr(tmp_path / "clone", {"src/app.py": code()}, base_files)

    run = run_pr(pull, tmp_path, capsys, [finding("src/app.py")])

    reviewed(run)
    assert run.owners_of("src/app.py") == [[]]
    assert run.codeowners == found(pull, GITHUB_CODEOWNERS, rules=0)


def test_p3_s11_ac2_head_changing_codeowners_doesnt_change_its_own_routing(tmp_path, capsys):
    head = {"src/app.py": code(), GITHUB_CODEOWNERS: "* @org/head\n"}
    pull = make_pr(tmp_path / "clone", head, {GITHUB_CODEOWNERS: "* @org/base\n"})
    assert (pull.repo.path / GITHUB_CODEOWNERS).read_text() == "* @org/head\n"  # checked out

    run = run_pr(pull, tmp_path, capsys, [finding("src/app.py")])

    reviewed(run)
    assert run.owners_of("src/app.py") == [["@org/base"]]
    assert run.codeowners == found(pull, GITHUB_CODEOWNERS, rules=1)


def test_p3_s11_ac2_head_adding_a_higher_location_is_ignored(tmp_path, capsys):
    head = {"src/app.py": code(), GITHUB_CODEOWNERS: "* @org/head\n"}
    pull = make_pr(tmp_path / "clone", head, {ROOT_CODEOWNERS: "* @org/base\n"})

    run = run_pr(pull, tmp_path, capsys, [finding("src/app.py")])

    reviewed(run)
    assert run.owners_of("src/app.py") == [["@org/base"]]
    assert run.codeowners == found(pull, ROOT_CODEOWNERS, rules=1)


def test_p3_s11_ac2_head_deleting_codeowners_doesnt_remove_the_owners(tmp_path, capsys):
    pull = make_pr(
        tmp_path / "clone",
        {"src/app.py": code()},
        {GITHUB_CODEOWNERS: "* @org/base\n"},
        head_deletes=[GITHUB_CODEOWNERS],
    )

    run = run_pr(pull, tmp_path, capsys, [finding("src/app.py")])

    reviewed(run)
    assert run.owners_of("src/app.py") == [["@org/base"]]


def test_p3_s11_ac2_head_adding_codeowners_to_a_repo_without_one_is_ignored(tmp_path, capsys):
    head = {"src/app.py": code(), GITHUB_CODEOWNERS: "* @org/head\n"}
    pull = make_pr(tmp_path / "clone", head)

    run = run_pr(pull, tmp_path, capsys, [finding("src/app.py")])

    reviewed(run)
    assert run.owners_of("src/app.py") == [[]]
    assert run.codeowners == ABSENT


# AC2 (amended): a directory or symbolic link at a location is skipped, and the next is tried.

LINK_TARGET = "owners.real"
NOT_A_FILE_FIRST = {
    # name: (base files, base symlinks, location read, its owner)
    "directory_then_root": (
        {f"{GITHUB_CODEOWNERS}/inner": "* @org/inner\n", ROOT_CODEOWNERS: "* @org/root\n"},
        {}, ROOT_CODEOWNERS, "@org/root",
    ),
    "symlink_then_root": (
        {LINK_TARGET: "* @org/linked\n", ROOT_CODEOWNERS: "* @org/root\n"},
        {GITHUB_CODEOWNERS: f"../{LINK_TARGET}"}, ROOT_CODEOWNERS, "@org/root",
    ),
    "symlink_and_directory_then_docs": (
        {LINK_TARGET: "* @org/linked\n", f"{ROOT_CODEOWNERS}/inner": "* @org/inner\n",
         DOCS_CODEOWNERS: "* @org/docs\n"},
        {GITHUB_CODEOWNERS: f"../{LINK_TARGET}"}, DOCS_CODEOWNERS, "@org/docs",
    ),
}  # fmt: skip


@pytest.mark.parametrize(
    ("base_files", "symlinks", "source", "owner"),
    NOT_A_FILE_FIRST.values(),
    ids=NOT_A_FILE_FIRST,
)
@pytest.mark.parametrize("mode", MODES)
def test_p3_s11_ac2_non_file_location_is_skipped_for_the_next(  # noqa: PLR0913, PLR0917
    tmp_path, capsys, mode, base_files, symlinks, source, owner
):
    pull = make_pr(tmp_path / "clone", {"src/app.py": code()}, base_files, base_symlinks=symlinks)

    run = run_pr(pull, tmp_path, capsys, [finding("src/app.py")], mode=mode)

    reviewed(run)
    assert run.owners_of("src/app.py") == [[owner]]
    assert run.codeowners == found(pull, source, rules=1)


ONLY_NON_FILES = {
    "directory": ({f"{GITHUB_CODEOWNERS}/inner": "* @org/inner\n"}, {}),
    "symlink": ({LINK_TARGET: "* @org/linked\n"}, {GITHUB_CODEOWNERS: f"../{LINK_TARGET}"}),
    "every_location": (
        {LINK_TARGET: "* @org/linked\n", f"{ROOT_CODEOWNERS}/inner": "* @org/inner\n"},
        {GITHUB_CODEOWNERS: f"../{LINK_TARGET}", DOCS_CODEOWNERS: f"../{LINK_TARGET}"},
    ),
}


@pytest.mark.parametrize(("base_files", "symlinks"), ONLY_NON_FILES.values(), ids=ONLY_NON_FILES)
def test_p3_s11_ac2_no_regular_file_at_any_location_is_not_a_file(
    tmp_path, capsys, base_files, symlinks
):
    """A link to a valid file isn't followed, and a directory's contents aren't read."""
    pull = make_pr(tmp_path / "clone", {"src/app.py": code()}, base_files, base_symlinks=symlinks)

    run = run_pr(pull, tmp_path, capsys, [finding("src/app.py")])

    reviewed(run)
    assert run.owners_of("src/app.py") == [[]]
    # The spec's Notes: the `source` names the first non-file found, here always the first location.
    assert run.codeowners == {
        "source": f"{GITHUB_CODEOWNERS}@{pull.base}",
        "status": "not_a_file",
        "rules": 0,
        "invalid_lines": 0,
    }


# AC3: GitHub's documented pattern forms. One run per pattern, every path a finding on line 1.

PATTERNS = {
    # pattern: (paths it matches, paths it doesn't)
    "/docs/": (("docs/a.py", "docs/x/y/z.py"), ("src/docs/a.py", "docsx/a.py", "docs.py")),
    "logs/": (
        ("logs/a.py", "src/logs/a.py", "a/b/logs/c/d.py"),
        ("logs.py", "mylogs/a.py", "a/logsx/b.py"),
    ),
    # A `/` in the middle anchors at the root, as in gitignore (amended AC3).
    "apps/**": (("apps/a.py", "apps/x/y/z.py"), ("apps.py", "appsx/a.py", "src/apps/x.py")),
    # `build/` at the root is a generated directory Phase 1 skips, so no finding can land there.
    "**/build": (
        ("x/build/a.py", "x/y/build/z/a.py", "a/b/c/d/build/e.py"),
        ("x/builder/a.py", "x/build.py", "x/mybuild/a.py"),
    ),
    "*.js": (("a.js", "src/x/b.js"), ("a.jsx", "src/a.json", "js/a.py")),
    # GitHub's own example of `*` staying within one segment; anchored by its middle `/`.
    "docs/*": (("docs/a.py",), ("docs/x/a.py", "src/docs/a.py")),
    # More anchoring by a middle `/`: a name pattern and a wildcard one.
    "lib/util.py": (("lib/util.py",), ("src/lib/util.py", "lib/x/util.py")),
    "src/*.py": (("src/a.py",), ("x/src/a.py", "src/x/a.py")),
    # A leading `**/` with a middle segment matches at any depth, but only that name pair.
    "**/api/views.py": (("api/views.py", "x/y/api/views.py"), ("x/api/v/views.py",)),
}


@pytest.mark.parametrize(("pattern", "hits", "misses"), [(p, *v) for p, v in PATTERNS.items()])
def test_p3_s11_ac3_pattern_matches_as_github_documents(tmp_path, capsys, pattern, hits, misses):
    owners_file = f"* @org/fallback\n{pattern} @org/hit\n"
    paths = (*hits, *misses)
    assert len(paths) <= DEFAULT_MAX_OPEN_COMMENTS
    pull = make_pr(
        tmp_path / "clone", dict.fromkeys(paths, code(1)), {GITHUB_CODEOWNERS: owners_file}
    )

    run = run_pr(pull, tmp_path, capsys, [finding(path) for path in paths])

    reviewed(run)
    seen = {path: run.owners_of(path) for path in paths}
    expected = {path: [["@org/hit"]] for path in hits} | {
        path: [["@org/fallback"]] for path in misses
    }
    assert seen == expected


# AC4: an owner-less pattern clears owners; `!`, `[` and bad owners skip the line and count.

INVALID_LINES = {
    "negation": "!/billing/ @org/neg",
    "bracket": "/billing/[ab].py @org/brackets",
    "bare_word": "/billing/ bob",
    "team_too_deep": "/billing/ @org/a/b",
    "lone_at": "/billing/ @",
    "org_without_team": "/billing/ @org/",
    "trailing_at": "/billing/ alice@",
    "double_at": "/billing/ @alice@example.com",
    "angle_brackets": "/billing/ <alice@example.com>",
    "one_bad_owner_among_good": "/billing/ @org/billing bob",
}


@pytest.mark.parametrize("line", INVALID_LINES.values(), ids=INVALID_LINES)
def test_p3_s11_ac4_invalid_line_is_skipped_and_counted(tmp_path, capsys, line):
    owners_file = f"* @org/core\n{line}\n"
    pull = make_pr(tmp_path / "clone", {"billing/a.py": code()}, {GITHUB_CODEOWNERS: owners_file})

    run = run_pr(pull, tmp_path, capsys, [finding("billing/a.py")])

    reviewed(run)
    assert run.owners_of("billing/a.py") == [["@org/core"]]
    assert run.codeowners == found(pull, GITHUB_CODEOWNERS, rules=1, invalid_lines=1)


VALID_OWNERS = ("@alice", "@alice-b", "@org/team-name", "first.last@example.co.uk")


@pytest.mark.parametrize("owner", VALID_OWNERS)
def test_p3_s11_ac4_user_team_and_email_owners_are_kept(tmp_path, capsys, owner):
    owners_file = f"* @org/core\n/billing/ {owner}\n"
    pull = make_pr(tmp_path / "clone", {"billing/a.py": code()}, {GITHUB_CODEOWNERS: owners_file})

    run = run_pr(pull, tmp_path, capsys, [finding("billing/a.py")])

    reviewed(run)
    assert run.owners_of("billing/a.py") == [[owner]]
    assert run.codeowners == found(pull, GITHUB_CODEOWNERS, rules=2)


def test_p3_s11_ac4_pattern_without_owners_matching_last_gives_no_owners(tmp_path, capsys):
    owners_file = "* @org/core\n/docs/\n"
    head = {"docs/guide.py": code(), "src/app.py": code()}
    pull = make_pr(tmp_path / "clone", head, {GITHUB_CODEOWNERS: owners_file})

    run = run_pr(pull, tmp_path, capsys, findings_on(dict.fromkeys(head, 1)))

    reviewed(run)
    assert run.owners_of("docs/guide.py") == [[]]
    assert run.owners_of("src/app.py") == [["@org/core"]]
    assert run.codeowners == found(pull, GITHUB_CODEOWNERS, rules=2)


def test_p3_s11_ac4_comments_and_blank_lines_are_ignored_and_not_counted(tmp_path, capsys):
    owners_file = (
        "# Owners of this repository\n\n   \n#!/billing/ @org/neg\n# [x] @nobody\n"
        "* @org/core\n\n# billing\n/billing/ @org/billing\n"
    )
    head = {"billing/a.py": code(), "src/app.py": code()}
    pull = make_pr(tmp_path / "clone", head, {GITHUB_CODEOWNERS: owners_file})

    run = run_pr(pull, tmp_path, capsys, findings_on(dict.fromkeys(head, 1)))

    reviewed(run)
    assert run.owners_of("billing/a.py") == [["@org/billing"]]
    assert run.owners_of("src/app.py") == [["@org/core"]]
    assert run.codeowners == found(pull, GITHUB_CODEOWNERS, rules=2)


def test_p3_s11_ac4_all_invalid_kinds_together(tmp_path, capsys):
    owners_file = "* @org/core\n/docs/\n" + "\n".join(INVALID_LINES.values()) + "\n"
    head = {"billing/a.py": code(), "docs/guide.py": code()}
    pull = make_pr(tmp_path / "clone", head, {GITHUB_CODEOWNERS: owners_file})

    run = run_pr(pull, tmp_path, capsys, findings_on(dict.fromkeys(head, 1)))

    reviewed(run)
    assert run.owners_of("billing/a.py") == [["@org/core"]]
    assert run.owners_of("docs/guide.py") == [[]]
    assert run.codeowners == found(
        pull, GITHUB_CODEOWNERS, rules=2, invalid_lines=len(INVALID_LINES)
    )


# AC5: no CODEOWNERS, or one that can't be used: no owners, and the run goes on unchanged.


@pytest.mark.parametrize("mode", MODES)
def test_p3_s11_ac5_no_codeowners_is_absent_and_nobody_owns(tmp_path, capsys, mode):
    head = {"billing/a.py": code(), "src/app.py": code()}
    pull = make_pr(tmp_path / "clone", head)

    run = run_pr(pull, tmp_path, capsys, findings_on(dict.fromkeys(head, 2)), mode=mode)

    reviewed(run)
    assert run.codeowners == ABSENT
    assert [entry["owners"] for entry in run.entries] == [[]] * 4


def padded(first_line: str, size: int) -> bytes:
    """`first_line`, then comment lines to exactly `size` bytes."""
    head = first_line.encode("utf-8")
    pad = b"# padding to the size limit\n"
    body = head + pad * ((size - len(head)) // len(pad))
    return body + b"#" * (size - len(body) - 1) + b"\n"


UNUSABLE = {
    "too_large": ({GITHUB_CODEOWNERS: padded("* @org/core\n", OVER_3_MB)}, None, "too_large"),
    "not_utf8": ({GITHUB_CODEOWNERS: b"* @org/core\n/billing/ @org/b\xff\xfe\n"}, None, "invalid"),
    "symlink": (
        {"owners.real": "* @org/core\n"}, {GITHUB_CODEOWNERS: "../owners.real"}, "not_a_file"
    ),
    "directory": ({f"{GITHUB_CODEOWNERS}/inner": "* @org/core\n"}, None, "not_a_file"),
}  # fmt: skip


@pytest.mark.parametrize(("base_files", "symlinks", "status"), UNUSABLE.values(), ids=UNUSABLE)
@pytest.mark.parametrize("mode", MODES)
def test_p3_s11_ac5_unusable_codeowners_has_its_status_and_no_owners(  # noqa: PLR0913, PLR0917
    tmp_path, capsys, mode, base_files, symlinks, status
):
    head = {"billing/a.py": code(), "src/app.py": code()}
    findings = findings_on(dict.fromkeys(head, 2))
    pull = make_pr(tmp_path / "clone", head, base_files, base_symlinks=symlinks)
    control = make_pr(tmp_path / "control", head)

    started = time.monotonic()
    run = run_pr(pull, tmp_path, capsys, findings, mode=mode)
    elapsed = time.monotonic() - started
    expected = run_pr(control, tmp_path, capsys, findings, mode=mode)

    assert elapsed < BOUND_S
    reviewed(run)
    assert run.exit_code == expected.exit_code
    assert run.codeowners["status"] == status
    assert (run.codeowners["rules"], run.codeowners["invalid_lines"]) == (0, 0)
    assert [entry["owners"] for entry in run.entries] == [[]] * 4
    assert [(c["file"], c["line"]) for c in run.shadow["comments"]] == [
        (c["file"], c["line"]) for c in expected.shadow["comments"]
    ]


def test_p3_s11_ac5_just_under_3_mb_is_read(tmp_path, capsys):
    pull = make_pr(
        tmp_path / "clone",
        {"src/app.py": code()},
        {GITHUB_CODEOWNERS: padded("* @org/core\n", UNDER_3_MB)},
    )

    started = time.monotonic()
    run = run_pr(pull, tmp_path, capsys, [finding("src/app.py")])
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S
    reviewed(run)
    assert run.owners_of("src/app.py") == [["@org/core"]]
    assert run.codeowners == found(pull, GITHUB_CODEOWNERS, rules=1)


# AC6: entries carry owners, and the summary counts findings per owner.

# 13 findings: 10 comments and 3 withheld over the default limit, all counted per owner.
AC6_CODEOWNERS = "* @org/core\n/billing/ @org/billing\n/api/ @org/api alice@example.com\n/free/\n"
AC6_FINDINGS = {"core.py": 5, "billing/a.py": 3, "api/b.py": 3, "free/c.py": 2}
AC6_ITEMS = [
    "@org/core: 5",
    "@org/api: 3",
    "@org/billing: 3",
    "alice@example.com: 3",
    "no owner: 2",
]


def ac6_run(tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str) -> Run:
    head = {file: code(count) for file, count in AC6_FINDINGS.items()}
    pull = make_pr(tmp_path / "clone", head, {GITHUB_CODEOWNERS: AC6_CODEOWNERS})
    return run_pr(pull, tmp_path, capsys, findings_on(AC6_FINDINGS), mode=mode)


@pytest.mark.parametrize("mode", MODES)
def test_p3_s11_ac6_every_comment_and_withheld_entry_has_its_owners(tmp_path, capsys, mode):
    run = ac6_run(tmp_path, capsys, mode)

    reviewed(run)
    document = run.shadow
    assert len(document["comments"]) == DEFAULT_MAX_OPEN_COMMENTS
    assert [w["reason"] for w in document["withheld"]] == ["over_limit"] * 3
    expected = {
        "core.py": ["@org/core"],
        "billing/a.py": ["@org/billing"],
        "api/b.py": ["@org/api", "alice@example.com"],
        "free/c.py": [],
    }
    assert [entry["owners"] for entry in run.entries] == [
        expected[entry["file"]] for entry in run.entries
    ]


@pytest.mark.parametrize("mode", MODES)
def test_p3_s11_ac6_summary_counts_by_owner_then_name_then_no_owner(tmp_path, capsys, mode):
    run = ac6_run(tmp_path, capsys, mode)

    reviewed(run)
    assert owner_texts(run.summary_body) == AC6_ITEMS
    assert_owners_in_code_spans(run.summary_body)


def test_p3_s11_ac6_posted_summary_has_the_owners_section(tmp_path, capsys):
    run = ac6_run(tmp_path, capsys, "comment")

    reviewed(run)
    assert run.github is not None
    posted = run.github.summary()
    assert posted is not None
    assert owner_texts(posted) == AC6_ITEMS


def owners_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], teams: int, *, unowned: bool = False
) -> Run:
    """`teams` files, each owned by its own team `@org/tNN` and holding one finding."""
    files = {f"t{n:02d}/a.py": f"@org/t{n:02d}" for n in range(teams)}
    owners_file = "".join(f"/{file.split('/')[0]}/ {team}\n" for file, team in files.items())
    head = dict.fromkeys(files, code(1))
    if unowned:
        head["free.py"] = code(1)
    pull = make_pr(tmp_path / "clone", head, {GITHUB_CODEOWNERS: owners_file})
    return run_pr(pull, tmp_path, capsys, [finding(file) for file in head])


def test_p3_s11_ac6_more_than_10_owners_lists_10_then_and_k_more(tmp_path, capsys):
    run = owners_run(tmp_path, capsys, 13, unowned=True)

    reviewed(run)
    expected = [f"@org/t{n:02d}: 1" for n in range(10)] + ["and 3 more", "no owner: 1"]
    assert owner_texts(run.summary_body) == expected
    assert_owners_in_code_spans(run.summary_body)


def test_p3_s11_ac6_exactly_10_owners_lists_all_10(tmp_path, capsys):
    run = owners_run(tmp_path, capsys, 10)

    reviewed(run)
    assert owner_texts(run.summary_body) == [f"@org/t{n:02d}: 1" for n in range(10)]


def test_p3_s11_ac6_no_codeowners_has_no_owners_section(tmp_path, capsys):
    pull = make_pr(tmp_path / "clone", {"src/app.py": code()})

    run = run_pr(pull, tmp_path, capsys, [finding("src/app.py")])

    reviewed(run)
    assert owner_section(run.summary_body) is None
    assert "Owners" not in run.summary_body


def test_p3_s11_ac6_codeowners_matching_no_finding_has_no_owners_section(tmp_path, capsys):
    """Every finding is unowned: no owners at all, so not even `no owner: N`."""
    pull = make_pr(
        tmp_path / "clone", {"src/app.py": code()}, {GITHUB_CODEOWNERS: "/nothing/ @org/x\n"}
    )

    run = run_pr(pull, tmp_path, capsys, [finding("src/app.py")])

    reviewed(run)
    assert run.owners_of("src/app.py") == [[]]
    assert owner_section(run.summary_body) is None
    assert "Owners" not in run.summary_body


def test_p3_s11_ac6_no_findings_has_no_owners_section(tmp_path, capsys):
    pull = make_pr(tmp_path / "clone", {"src/app.py": code()}, {GITHUB_CODEOWNERS: "* @org/x\n"})

    run = run_pr(pull, tmp_path, capsys, [])

    reviewed(run)
    assert owner_section(run.summary_body) is None


# AC7: no mention or email outside a code span, and comment bodies unchanged by owners.


@pytest.mark.parametrize("mode", MODES)
def test_p3_s11_ac7_summary_has_no_mention_outside_a_code_span(tmp_path, capsys, mode):
    run = ac6_run(tmp_path, capsys, mode)

    reviewed(run)
    assert_no_mention_outside_code(run.summary_body)
    for _, _, body in run.bodies:
        assert_no_mention_outside_code(body)


def test_p3_s11_ac7_posted_summary_and_bodies_have_no_mention_outside_a_code_span(tmp_path, capsys):
    run = ac6_run(tmp_path, capsys, "comment")

    reviewed(run)
    assert run.github is not None
    posted = run.github.summary()
    assert posted is not None
    assert_no_mention_outside_code(posted)
    for comment in run.github.comments():
        assert_no_mention_outside_code(comment.body)


@pytest.mark.parametrize("mode", MODES)
def test_p3_s11_ac7_comment_bodies_are_the_same_as_without_codeowners(tmp_path, capsys, mode):
    head = {file: code(count) for file, count in AC6_FINDINGS.items()}
    owned = make_pr(tmp_path / "owned", head, {GITHUB_CODEOWNERS: AC6_CODEOWNERS})
    unowned = make_pr(tmp_path / "unowned", head)

    owned_run = run_pr(owned, tmp_path, capsys, findings_on(AC6_FINDINGS), mode=mode)
    unowned_run = run_pr(unowned, tmp_path, capsys, findings_on(AC6_FINDINGS), mode=mode)

    reviewed(owned_run)
    assert owned_run.bodies == unowned_run.bodies
    for _, _, body in owned_run.bodies:
        assert "@org/" not in body
        assert "alice@example.com" not in body
    if mode == "comment":
        assert owned_run.github is not None
        assert unowned_run.github is not None
        assert [c.body for c in owned_run.github.comments()] == [
            c.body for c in unowned_run.github.comments()
        ]


HOSTILE_OWNERS = {
    "backtick_in_email": "a`b@example.com",
    "double_backtick_in_email": "a``b@example.com",
    "html_in_email": "<b>x</b>@example.com",
    "underscore_user": "@_x_",
}


@pytest.mark.parametrize("owner", HOSTILE_OWNERS.values(), ids=HOSTILE_OWNERS)
def test_p3_s11_ac7_hostile_owner_text_is_skipped_or_stays_in_code(tmp_path, capsys, owner):
    """Whether or not the owner is accepted, nothing it holds escapes a code span."""
    owners_file = f"* @org/core\n/billing/ {owner}\n"
    pull = make_pr(tmp_path / "clone", {"billing/a.py": code()}, {GITHUB_CODEOWNERS: owners_file})

    run = run_pr(pull, tmp_path, capsys, [finding("billing/a.py")])

    reviewed(run)
    assert_no_mention_outside_code(run.summary_body)
    assert run.owners_of("billing/a.py") in ([[owner]], [["@org/core"]])
    tokens = PARSER.parse(run.summary_body)
    html = [t for t in [*tokens, *inline_children(tokens)] if t.type.startswith("html")]
    assert html == [], [t.content for t in html]


# AC8: a refused run doesn't read CODEOWNERS.


@pytest.fixture
def git_processes(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every git process started after the fixture is requested, by any route through Popen."""
    started: list[list[str]] = []
    original = subprocess.Popen.__init__

    def init(self: subprocess.Popen[Any], args: Any, *rest: Any, **kwargs: Any) -> None:
        argv = [str(arg) for arg in args] if isinstance(args, (list, tuple)) else [str(args)]
        if "git" in Path(argv[0]).name.lower():
            started.append(argv)
        original(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", init)
    return started


REFUSALS = {
    "action": ({"action": "closed"}, "mock", "action"),
    "fork": ({"head_repo": "someone/review-bot"}, "mock", "fork"),
    "no_credential": ({}, "claude", "no_credential"),
}


@pytest.mark.parametrize(("change", "provider_name", "reason"), REFUSALS.values(), ids=REFUSALS)
def test_p3_s11_ac8_refused_run_doesnt_read_codeowners(  # noqa: PLR0913, PLR0917
    tmp_path, capsys, monkeypatch, git_processes, change, provider_name, reason
):
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    head = {"src/app.py": code()}
    owned = make_pr(tmp_path / "owned", head, {GITHUB_CODEOWNERS: "* @org/core\n"})
    unowned = make_pr(tmp_path / "unowned", head)

    def refused(pull: PullRequest) -> tuple[Run, list[list[str]]]:
        git_processes.clear()
        payload = event(pull.base, pull.head, **change)
        run = run_pr(
            pull, tmp_path, capsys, [finding("src/app.py")],
            provider_name=provider_name, event_payload=payload,
        )  # fmt: skip
        repo = str(pull.repo.path)
        return run, [[arg.replace(repo, "<repo>") for arg in argv[1:]] for argv in git_processes]

    owned_run, owned_git = refused(owned)
    unowned_run, unowned_git = refused(unowned)

    assert owned_run.exit_code == 0, owned_run.stderr
    document = owned_run.shadow
    assert (document["outcome"], document["reason"]) == ("skipped", reason)
    assert unowned_run.shadow["reason"] == reason
    assert owned_run.provider.requests == []
    assert not any("CODEOWNERS" in arg for argv in owned_git for arg in argv), owned_git
    assert owned_git == unowned_git
    codeowners = document["codeowners"]
    assert codeowners is None or (codeowners["status"], codeowners["rules"]) == ("absent", 0)
    assert "Owners" not in document["summary_body"]
    if reason in {"action", "fork"}:
        assert owned_git == [], "a refused action or fork runs no git at all (P2-S2 AC3, AC4)"


# Limits: matching is linear, with no backtracking pattern.


def test_p3_s11_limits_many_double_star_rules_match_in_bounded_time(tmp_path, capsys):
    """500 rules of 60 `**` segments against a 30-deep path none of them matches.

    A backtracking translation of `**` is exponential here; the spec says matching is linear.
    """
    deep = "/".join(["d"] * 30) + "/deep.py"
    rules = "".join("**/" * 60 + f"nomatch{n} @org/x{n}\n" for n in range(500))
    owners_file = "* @org/core\n" + rules
    pull = make_pr(tmp_path / "clone", {deep: code(3)}, {GITHUB_CODEOWNERS: owners_file})

    started = time.monotonic()
    run = run_pr(pull, tmp_path, capsys, findings_on({deep: 3}))
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    reviewed(run)
    assert run.owners_of(deep) == [["@org/core"]] * 3
    assert run.codeowners == found(pull, GITHUB_CODEOWNERS, rules=501)


def test_p3_s11_limits_star_heavy_patterns_match_in_bounded_time(tmp_path, capsys):
    """Segments of many `*` against long names that almost match: catastrophic for a regex."""
    name = "a" * 80 + ".py"  # long enough to backtrack, short enough for Windows MAX_PATH
    rules = "".join("*a" * 80 + f"b{n} @org/x{n}\n" for n in range(200))
    pull = make_pr(
        tmp_path / "clone", {f"src/{name}": code(1)}, {GITHUB_CODEOWNERS: "* @org/core\n" + rules}
    )

    started = time.monotonic()
    run = run_pr(pull, tmp_path, capsys, [finding(f"src/{name}")])
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    reviewed(run)
    assert run.owners_of(f"src/{name}") == [["@org/core"]]
