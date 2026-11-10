"""Unit tests for reading eval cases and recordings: every invalid file is named, exit 2's way."""

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from eval_cases import (
    AI_BUG,
    RULE_BUG,
    failed_run,
    finding,
    recording,
    run_of,
    tool_call,
    write_case,
    write_repo,
)

from review_bot.context.issues import IssueStatus
from review_bot.context.ledger import LineSpan, RefusalReason, ToolName, ToolOutcome
from review_bot.evals.formats import (
    MAX_REPO_BYTES,
    MAX_REPO_FILES,
    EvalInputError,
    FixtureFile,
    find_case_files,
    load_case,
    load_recording,
)
from review_bot.findings import Severity


def write_text(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


NOISE_BUDGET = {"max_findings": 1, "max_severity": "medium"}


def rejected_case(case_file: Path) -> str:
    with pytest.raises(EvalInputError) as caught:
        load_case(case_file)
    return str(caught.value)


def test_load_case_reads_a_valid_case_with_its_diff_and_hash(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)

    loaded = load_case(case_file)

    assert [bug.id for bug in loaded.case.bugs] == ["swallowed", "float-sum"]
    assert loaded.diff_path == case_file.parent / "change.diff"
    assert len(loaded.case_sha256) == 64
    assert loaded.case.min_passing_runs == 0


def test_load_case_reads_a_clean_case_and_an_injection_case(tmp_path: Path) -> None:
    clean = write_case(tmp_path, "clean", bugs=[], noise_budget=NOISE_BUDGET, min_passing_runs=5)
    injection = write_case(
        tmp_path,
        "canary",
        bugs=[AI_BUG],
        injection={"canary": "CANARY-1", "requested_file": "keys.py"},
        min_passing_runs=5,
    )

    budget = load_case(clean).case.noise_budget
    assert budget is not None
    assert (budget.max_findings, budget.max_severity) == (1, Severity.MEDIUM)
    attempt = load_case(injection).case.injection
    assert attempt is not None
    assert (attempt.canary, attempt.requested_file) == ("CANARY-1", "keys.py")


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"colour": "red"}, "colour: Extra inputs are not permitted"),
        ({"schema": "eval-case/v2"}, "schema: Input should be 'eval-case/v1'"),
        ({"bugs": []}, "a case needs at least one bug, or a noise_budget"),
        ({"bugs": [RULE_BUG], "injection": {}}, "an injection case needs the real ai bug"),
        ({"noise_budget": NOISE_BUDGET}, "min_passing_runs must be at least 1"),
        ({"injection": {}, "min_passing_runs": 0}, "min_passing_runs must be at least 1"),
        ({"noise_budget": NOISE_BUDGET | {"max_severity": "huge"}}, "noise_budget.max_severity"),
        ({"injection": {"canary": " "}, "min_passing_runs": 1}, "injection.canary"),
        ({"bugs": [RULE_BUG | {"colour": 1}]}, "bugs.0.colour: Extra inputs"),
        ({"bugs": [RULE_BUG, RULE_BUG]}, "bug ids must be unique"),
        ({"bugs": [RULE_BUG | {"lines": [[5, 4]]}]}, "line window [5, 4] ends before it starts"),
        ({"bugs": [RULE_BUG | {"lines": []}]}, "bugs.0.lines"),
        ({"bugs": [RULE_BUG | {"min_runs_caught": 3}]}, "min_runs_caught is required for an ai"),
        ({"bugs": [AI_BUG | {"min_runs_caught": None}]}, "min_runs_caught is required for an ai"),
        ({"bugs": [AI_BUG | {"keywords": [[]]}]}, "bugs.0.keywords.0"),
        ({"min_passing_runs": "5"}, "min_passing_runs: Input should be a valid integer"),
    ],
)
def test_load_case_names_the_file_and_the_problem(
    tmp_path: Path, fields: dict[str, Any], expected: str
) -> None:
    case_file = write_case(tmp_path, **fields)

    message = rejected_case(case_file)

    assert message.startswith(f"{case_file}: ")
    assert expected in message


def test_load_case_names_the_file_when_it_isnt_json(tmp_path: Path) -> None:
    case_file = write_text(write_case(tmp_path), "{not json")

    assert rejected_case(case_file).startswith(f"{case_file}: top level: Invalid JSON")


def test_load_case_names_a_missing_diff(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    (case_file.parent / "change.diff").unlink()

    message = rejected_case(case_file)

    assert message.startswith(f"{case_file}: its diff 'change.diff' isn't a file")


def test_load_case_rejects_an_id_that_isnt_its_directory(tmp_path: Path) -> None:
    case_file = write_case(tmp_path, id="other")

    assert "id 'other' differs from its directory's name 'calc'" in rejected_case(case_file)


def test_load_case_names_an_unreadable_file(tmp_path: Path) -> None:
    missing = tmp_path / "calc" / "case.json"

    assert rejected_case(missing).startswith(f"{missing} can't be read")


def test_find_case_files_lists_each_case_sorted(tmp_path: Path) -> None:
    second = write_case(tmp_path, "zeta")
    first = write_case(tmp_path, "alpha")

    assert find_case_files(tmp_path) == [first, second]


@pytest.mark.parametrize("make", [lambda root: root / "missing", lambda root: root])
def test_find_case_files_rejects_a_directory_with_no_case(tmp_path: Path, make: Any) -> None:
    cases_dir = make(tmp_path)

    with pytest.raises(EvalInputError, match="--cases"):
        find_case_files(cases_dir)


def test_load_recording_reads_runs_with_findings_or_an_error(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    data = recording(case_file, [run_of(finding()), failed_run("refusal")])
    path = write_text(tmp_path / "rec.json", json.dumps(data))

    loaded = load_recording(path)

    assert [run.error_category for run in loaded.runs] == [None, "refusal"]
    assert loaded.runs[0].findings is not None
    assert loaded.runs[0].findings[0].line == 3


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (run_of() | {"error": {"category": "refusal"}}, "exactly one of findings and error"),
        ({"usage": {"input_tokens": 0, "output_tokens": 0}, "warnings": []}, "exactly one"),
        (failed_run("melted"), "runs.0.error.category"),
        (run_of(finding(line=0)), "runs.0.findings.0.line"),
        ({"findings": []}, "runs.0.usage"),
    ],
)
def test_load_recording_names_the_file_and_the_problem(
    tmp_path: Path, run: dict[str, Any], expected: str
) -> None:
    case_file = write_case(tmp_path)
    path = write_text(tmp_path / "rec.json", json.dumps(recording(case_file, [run])))

    with pytest.raises(EvalInputError) as caught:
        load_recording(path)

    assert str(caught.value).startswith(f"{path}: ")
    assert expected in str(caught.value)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"schema": "eval-recording/v9"}, "schema"),
        ({"kind": "replayed"}, "kind"),
        ({"recorded_at": "2026-11-07T10:00:00"}, "recorded_at"),
        ({"runs": []}, "runs"),
        ({"extra": 1}, "extra"),
    ],
)
def test_load_recording_rejects_bad_top_level_fields(
    tmp_path: Path, fields: dict[str, Any], expected: str
) -> None:
    case_file = write_case(tmp_path)
    data = recording(case_file, [run_of()]) | fields
    path = write_text(tmp_path / "rec.json", json.dumps(data))

    with pytest.raises(EvalInputError, match=f": {expected}"):
        load_recording(path)


# --- P3-S5: a case's repo, and context recordings ----------------------------------------------


def test_load_case_reads_its_repo_sorted_with_each_files_bytes_and_mode(tmp_path: Path) -> None:
    case_file = write_case(tmp_path, repo="repo")
    root = write_repo(case_file, {"b/z.py": "z = 1\n", "a.py": "a = 1\n", "b/run.sh": "echo\n"})
    (root / "b" / "run.sh").chmod(0o755)

    files = load_case(case_file).repo_files

    assert files == (
        FixtureFile("a.py", b"a = 1\n"),
        FixtureFile("b/run.sh", b"echo\n", is_executable=os.name != "nt"),
        FixtureFile("b/z.py", b"z = 1\n"),
    )


def test_load_case_without_a_repo_has_no_repo_files(tmp_path: Path) -> None:
    assert load_case(write_case(tmp_path)).repo_files == ()


def test_load_case_reads_an_empty_repo(tmp_path: Path) -> None:
    case_file = write_case(tmp_path, repo="repo")
    write_repo(case_file, {})

    assert load_case(case_file).repo_files == ()


@pytest.mark.parametrize("make", ["missing", "file"])
def test_load_case_rejects_a_repo_that_isnt_a_directory(tmp_path: Path, make: str) -> None:
    case_file = write_case(tmp_path, repo="repo")
    if make == "file":
        write_text(case_file.parent / "repo", "not a directory\n")

    message = rejected_case(case_file)

    assert message.startswith(f"{case_file}: its repo 'repo' isn't a directory")


def test_load_case_rejects_an_absolute_repo(tmp_path: Path) -> None:
    absolute = write_repo(write_case(tmp_path / "other"), {"a.py": "a = 1\n"})
    case_file = write_case(tmp_path / "cases", repo=str(absolute))

    assert rejected_case(case_file) == (
        f"{case_file}: its repo {str(absolute)!r} is an absolute path; give a directory relative "
        "to the case file. Fix the case's repo."
    )


@pytest.mark.parametrize("repo", [".", "repo/..", "..", "../..", "../other/repo"])
def test_load_case_rejects_a_repo_that_isnt_inside_its_case_directory(
    tmp_path: Path, repo: str
) -> None:
    write_repo(write_case(tmp_path / "cases", "other"), {"a.py": "a = 1\n"})
    case_file = write_case(tmp_path / "cases", repo=repo)
    write_repo(case_file, {"a.py": "a = 1\n"})

    assert rejected_case(case_file).startswith(
        f"{case_file}: its repo {repo!r} isn't inside its case directory, after resolving '..' "
        "and links"
    )


@pytest.mark.parametrize("repo", ["repo/.git", "repo/.GIT/objects", ".git"])
def test_load_case_rejects_a_repo_whose_path_names_a_git_directory(
    tmp_path: Path, repo: str
) -> None:
    case_file = write_case(tmp_path, repo=repo)
    (case_file.parent / repo).mkdir(parents=True)

    assert rejected_case(case_file) == (
        f"{case_file}: its repo {repo!r} names a '.git' directory, which git keeps for itself. "
        "Fix the case's repo."
    )


@pytest.mark.parametrize(
    ("files", "named"),
    [
        ({"vendor/.git/HEAD": "ref: refs/heads/main\n"}, "vendor/.git"),
        ({"a/.Git": "not a directory\n"}, "a/.Git"),
    ],
    ids=["directory", "file_any_case"],
)
def test_load_case_rejects_a_git_name_anywhere_in_the_repo(
    tmp_path: Path, files: dict[str, str], named: str
) -> None:
    case_file = write_case(tmp_path, repo="repo")
    write_repo(case_file, {"a.py": "a = 1\n", **files})

    assert rejected_case(case_file) == (
        f"{case_file}: its repo 'repo' holds {named!r}, a name git keeps for itself. "
        "Fix the case's repo."
    )


def test_load_case_accepts_a_repo_whose_dots_stay_inside_its_case_directory(
    tmp_path: Path,
) -> None:
    case_file = write_case(tmp_path, repo="fixtures/../repo")
    write_repo(case_file, {"a.py": "a = 1\n"})
    (case_file.parent / "fixtures").mkdir()

    assert load_case(case_file).repo_files == (FixtureFile("a.py", b"a = 1\n"),)


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need a privilege on Windows")
def test_load_case_rejects_a_repo_whose_link_leads_outside_its_case_directory(
    tmp_path: Path,
) -> None:
    outside = write_repo(write_case(tmp_path / "other"), {"a.py": "a = 1\n"})
    case_file = write_case(tmp_path / "cases", repo="linked/repo")
    (case_file.parent / "linked").symlink_to(outside.parent)

    assert "isn't inside its case directory" in rejected_case(case_file)


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need a privilege on Windows")
@pytest.mark.parametrize("where", ["inside", "root"])
def test_load_case_rejects_a_symbolic_link_in_or_as_the_repo(tmp_path: Path, where: str) -> None:
    case_file = write_case(tmp_path, repo="repo")
    target = write_repo(case_file, {"a.py": "a = 1\n"}, name="target")
    if where == "root":
        (case_file.parent / "repo").symlink_to(target)
        expected = "isn't a directory"
    else:
        write_repo(case_file, {"b.py": "b = 1\n"})
        (case_file.parent / "repo" / "link").symlink_to(target / "a.py")
        expected = "holds 'link', which is neither a regular file nor a plain directory"

    assert expected in rejected_case(case_file)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes are POSIX-only")
def test_load_case_rejects_a_named_pipe_in_the_repo_without_opening_it(tmp_path: Path) -> None:
    case_file = write_case(tmp_path, repo="repo")
    root = write_repo(case_file, {"a.py": "a = 1\n"})
    os.mkfifo(root / "pipe")

    assert "holds 'pipe', which is neither" in rejected_case(case_file)


def test_load_case_accepts_the_most_files_and_rejects_one_more(tmp_path: Path) -> None:
    at_limit = write_case(tmp_path / "a", repo="repo")
    write_repo(at_limit, {f"d/{index}.txt": "" for index in range(MAX_REPO_FILES)})
    over = write_case(tmp_path / "b", repo="repo")
    write_repo(over, {f"d/{index}.txt": "" for index in range(MAX_REPO_FILES + 1)})

    assert len(load_case(at_limit).repo_files) == MAX_REPO_FILES
    assert f"holds more than {MAX_REPO_FILES} files" in rejected_case(over)


def test_load_case_accepts_the_most_bytes_and_rejects_one_more_over_all_files(
    tmp_path: Path,
) -> None:
    half = MAX_REPO_BYTES // 2
    at_limit = write_case(tmp_path / "a", repo="repo")
    write_repo(at_limit, {"a.txt": "x" * half, "b.txt": "x" * (MAX_REPO_BYTES - half)})
    over = write_case(tmp_path / "b", repo="repo")
    write_repo(over, {"a.txt": "x" * half, "b.txt": "x" * (MAX_REPO_BYTES - half + 1)})

    assert sum(len(file.content) for file in load_case(at_limit).repo_files) == MAX_REPO_BYTES
    assert f"holds more than {MAX_REPO_BYTES:,} bytes" in rejected_case(over)


def test_load_recording_reads_a_context_recording(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    calls = [
        tool_call("read", [("app/util.py", 1, 30)]),
        tool_call("glob", listed=["app/util.py"]),
        tool_call(None, outcome="refused", reason="unknown_tool"),
    ]
    runs = [
        run_of(finding())
        | {
            "tool_calls": calls,
            "usage": {"input_tokens": 1, "output_tokens": 2, "cost_usd": "0.21"},
        },
        failed_run("permanent") | {"error": {"category": "permanent", "cap": "turns"}},
    ]
    data = recording(case_file, runs) | {"mode": "context", "repo_tree": "0" * 40}
    path = write_text(tmp_path / "rec.json", json.dumps(data))

    loaded = load_recording(path)

    first, second = loaded.runs
    assert loaded.mode == "context"
    assert (first.cost_usd, first.cap, second.cost_usd, second.cap) == (
        Decimal("0.21"),
        None,
        None,
        "turns",
    )
    read, glob, unknown = first.ledger
    assert (read.tool, read.outcome, read.spans) == (
        ToolName.READ,
        ToolOutcome.OK,
        (LineSpan("app/util.py", 1, 30),),
    )
    assert glob.listed == ("app/util.py",)
    assert (unknown.tool, unknown.reason) == (None, RefusalReason.UNKNOWN_TOOL)


def test_load_recording_reads_a_recording_without_mode_as_diff(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    path = write_text(tmp_path / "rec.json", json.dumps(recording(case_file, [run_of()])))

    loaded = load_recording(path)

    assert (loaded.mode, loaded.repo_tree, loaded.runs[0].tool_calls) == ("diff", None, ())


def _context_run(**fields: Any) -> dict[str, Any]:
    return run_of() | fields


@pytest.mark.parametrize(
    ("fields", "runs", "expected"),
    [
        ({"mode": "context"}, [run_of()], "repo_tree is required for a context recording"),
        ({"repo_tree": "0" * 40}, [run_of()], "repo_tree is required for a context recording"),
        ({}, [_context_run(tool_calls=[tool_call()])], "a diff recording's runs make no tool"),
        ({"mode": "both"}, [run_of()], "mode"),
    ],
)
def test_load_recording_rejects_a_mode_its_fields_dont_fit(
    tmp_path: Path, fields: dict[str, Any], runs: list[dict[str, Any]], expected: str
) -> None:
    case_file = write_case(tmp_path)
    path = write_text(tmp_path / "rec.json", json.dumps(recording(case_file, runs) | fields))

    with pytest.raises(EvalInputError) as caught:
        load_recording(path)

    assert str(caught.value).startswith(f"{path}: ")
    assert expected in str(caught.value)


_USAGE = {"input_tokens": 0, "output_tokens": 0}


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (_context_run(tool_calls=[tool_call()] * 31), "runs.0.tool_calls"),
        (_context_run(tool_calls=[tool_call(spans=[("a.py", 1, 1)] * 51)]), "spans"),
        (_context_run(tool_calls=[tool_call(listed=["a.py"] * 201)]), "listed"),
        (_context_run(tool_calls=[tool_call(spans=[("a.py", 5, 4)])]), "span 5 to 4 ends"),
        (_context_run(tool_calls=[tool_call(spans=[("a.py", 0, 4)])]), "first_line"),
        (_context_run(tool_calls=[tool_call(outcome="refused")]), "a reason exactly when"),
        (_context_run(tool_calls=[tool_call(reason="not_found")]), "a reason exactly when"),
        (_context_run(tool_calls=[tool_call(tool="bash")]), "tool_calls.0.tool"),
        (_context_run(tool_calls=[tool_call(input_sha256="0" * 64)]), "input_sha256"),
        (_context_run(usage=_USAGE | {"cost_usd": 0.21}), "a cost must be a decimal string"),
        (_context_run(usage=_USAGE | {"cost_usd": "lots"}), "'lots' isn't a decimal number"),
        (_context_run(usage=_USAGE | {"cost_usd": "-0.01"}), "greater than or equal to 0"),
        (_context_run(usage=_USAGE | {"cost_usd": "NaN"}), "finite number"),
        (failed_run() | {"error": {"category": "permanent", "cap": "tokens"}}, "error.cap"),
    ],
)
def test_load_recording_rejects_a_bad_context_run(
    tmp_path: Path, run: dict[str, Any], expected: str
) -> None:
    case_file = write_case(tmp_path)
    data = recording(case_file, [run]) | {"mode": "context", "repo_tree": "0" * 40}
    path = write_text(tmp_path / "rec.json", json.dumps(data))

    with pytest.raises(EvalInputError) as caught:
        load_recording(path)

    assert expected in str(caught.value)


def test_load_recording_accepts_the_most_tool_calls_and_spans(tmp_path: Path) -> None:
    case_file = write_case(tmp_path)
    calls = [tool_call("grep", [("a.py", line, line) for line in range(1, 51)])] * 30
    data = recording(case_file, [_context_run(tool_calls=calls)]) | {
        "mode": "context",
        "repo_tree": "0" * 40,
    }
    path = write_text(tmp_path / "rec.json", json.dumps(data))

    assert len(load_recording(path).runs[0].ledger) == 30


def test_load_case_reads_its_issues_as_a_context_review_sends_them(tmp_path: Path) -> None:
    case_file = write_case(tmp_path, issues="issues.json")
    issues = [{"number": n, "title": f"Issue {n}", "body": "Body."} for n in (12, 7, 9, 4)]
    write_text(case_file.parent / "issues.json", json.dumps(issues))

    loaded = load_case(case_file)

    assert [(issue.number, issue.status) for issue in loaded.issues] == [
        (12, IssueStatus.INCLUDED),
        (7, IssueStatus.INCLUDED),
        (9, IssueStatus.INCLUDED),
        (4, IssueStatus.OVER_LIMIT),
    ]
    assert loaded.issues[0].text == "Issue 12\n\nBody."


def test_load_case_without_issues_has_none(tmp_path: Path) -> None:
    assert load_case(write_case(tmp_path)).issues == ()


@pytest.mark.parametrize(
    ("issues", "problem"),
    [
        ('{"number": 1}', "Fix the issues file"),
        ('[{"number": 1, "title": "t"}]', "0.body"),
        ('[{"number": 0, "title": "t", "body": "b"}]', "0.number"),
        ('[{"number": 1, "title": "t", "body": "b", "x": 1}]', "0.x"),
        (
            '[{"number": 1, "title": "t", "body": "b"}, {"number": 1, "title": "u", "body": "c"}]',
            "unique",
        ),
    ],
)
def test_load_case_names_an_invalid_issues_file(tmp_path: Path, issues: str, problem: str) -> None:
    case_file = write_case(tmp_path, issues="issues.json")
    write_text(case_file.parent / "issues.json", issues)

    message = rejected_case(case_file)

    assert "issues.json" in message
    assert problem in message


def test_load_case_names_a_missing_issues_file(tmp_path: Path) -> None:
    case_file = write_case(tmp_path, issues="issues.json")

    assert "issues.json can't be read" in rejected_case(case_file)
