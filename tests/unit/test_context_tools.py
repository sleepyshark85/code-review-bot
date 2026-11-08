import asyncio
import hashlib
import io
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from git_samples import commit, git, init_repo

from review_bot.context.ledger import LineSpan, RefusalReason, ToolCall, ToolName, ToolOutcome
from review_bot.context.tools import RepoRef, RepoTools, ToolCaps
from review_bot.git import GitRepo

CAPS = ToolCaps(max_calls=12, max_result_bytes=16_000, max_total_bytes=96_000)
CHECKOUT = "".join(f"checkout line {number}\n" for number in range(1, 31))


def make_tools(repo: Path, sha: str, caps: ToolCaps = CAPS) -> RepoTools:
    return RepoTools(RepoRef(git=GitRepo(repo, timeout_s=60), commit=sha), caps=caps)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


@pytest.fixture
def tools(repo: Path) -> RepoTools:
    sha = commit(
        repo,
        {
            "billing/checkout.py": CHECKOUT,
            "billing/invoice.py": "def total():\n    return TOTAL(1)\n",
            "docs/notes.md": "total( in prose\n",
        },
    )
    return make_tools(repo, sha)


def call(tools: RepoTools, name: str, arguments: Mapping[str, object]) -> str:
    """The result's text, between its delimiter lines."""
    framed = asyncio.run(tools.call(name, arguments))
    return "\n".join(framed.split("\n")[1:-1])


def test_read_returns_numbered_lines_of_the_window(tools: RepoTools) -> None:
    text = call(tools, "read", {"path": "billing/checkout.py", "start_line": 10, "line_count": 3})

    assert text == "10\tcheckout line 10\n11\tcheckout line 11\n12\tcheckout line 12"
    assert tools.ledger[-1].spans == (LineSpan("billing/checkout.py", 10, 12),)
    assert tools.ledger[-1].outcome is ToolOutcome.OK


def test_read_defaults_to_200_lines_from_line_1(tools: RepoTools) -> None:
    text = call(tools, "read", {"path": "billing/checkout.py"})

    assert text.split("\n")[0] == "1\tcheckout line 1"
    assert text.split("\n")[-1] == "30\tcheckout line 30"


def test_read_past_the_end_says_so_and_holds_no_lines(tools: RepoTools) -> None:
    text = call(tools, "read", {"path": "billing/checkout.py", "start_line": 31})

    assert "past the end of the file" in text
    assert "30" in text
    assert tools.ledger[-1].spans == ()
    assert tools.ledger[-1].outcome is ToolOutcome.OK


def test_read_of_an_empty_file_is_past_its_end(repo: Path) -> None:
    tools = make_tools(repo, commit(repo, {"empty.py": ""}))

    assert "line count is 0" in call(tools, "read", {"path": "empty.py"})


def test_read_cuts_a_long_line_and_marks_it(repo: Path) -> None:
    tools = make_tools(repo, commit(repo, {"long.py": "x" * 2500 + "\n"}))

    text = call(tools, "read", {"path": "long.py"})

    assert text == "1\t" + "x" * 2000 + " (cut)"


def test_read_over_the_result_cap_ends_at_a_line_boundary_with_a_note(repo: Path) -> None:
    lines = "".join(f"{'y' * 95}{number:04}\n" for number in range(300))
    tools = make_tools(
        repo,
        commit(repo, {"big.py": lines}),
        ToolCaps(max_calls=12, max_result_bytes=1000, max_total_bytes=96_000),
    )

    text = call(tools, "read", {"path": "big.py"})

    assert len(text.encode("utf-8")) <= 1000
    *shown, note = text.split("\n")
    assert note.startswith("(truncated")
    assert all(line.endswith(f"{number:04}") for number, line in enumerate(shown))
    assert tools.ledger[-1].outcome is ToolOutcome.TRUNCATED
    assert tools.ledger[-1].spans == (LineSpan("big.py", 1, len(shown)),)


def test_read_of_a_line_too_long_for_the_cap_returns_only_the_note(repo: Path) -> None:
    tools = make_tools(
        repo,
        commit(repo, {"wide.py": "é" * 1500 + "\n"}),
        ToolCaps(max_calls=12, max_result_bytes=1000, max_total_bytes=96_000),
    )

    text = call(tools, "read", {"path": "wide.py"})

    assert text.startswith("(truncated")
    assert tools.ledger[-1].spans == ()
    assert tools.ledger[-1].outcome is ToolOutcome.TRUNCATED


def test_read_shows_committed_text_not_the_working_tree(repo: Path) -> None:
    tools = make_tools(repo, commit(repo, {"a.py": "committed\n"}))
    (repo / "a.py").write_text("edited\n", encoding="utf-8")

    assert call(tools, "read", {"path": "a.py"}) == "1\tcommitted"


@pytest.mark.parametrize(
    ("path", "reason"),
    [("missing.py", RefusalReason.NOT_FOUND), ("billing", RefusalReason.DIRECTORY)],
)
def test_read_refuses_a_path_that_is_not_a_file(
    tools: RepoTools, path: str, reason: RefusalReason
) -> None:
    text = call(tools, "read", {"path": path})

    assert text.startswith(f"refused: {reason}: ")
    assert tools.ledger[-1].reason is reason
    assert tools.ledger[-1].outcome is ToolOutcome.REFUSED


def test_read_refuses_a_symbolic_link(repo: Path) -> None:
    commit(repo, {"target.py": "x\n"})
    blob = git(repo, "rev-parse", "HEAD:target.py")  # the link's target path, as git stores it
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},link")
    git(repo, "commit", "-q", "-m", "link")
    tools = make_tools(repo, git(repo, "rev-parse", "HEAD"))

    assert call(tools, "read", {"path": "link"}).startswith("refused: symlink: ")


def test_read_refuses_a_blob_over_1_mb(repo: Path) -> None:
    tools = make_tools(repo, commit(repo, {"huge.txt": "z" * 1_000_001}))

    assert call(tools, "read", {"path": "huge.txt"}).startswith("refused: too_large: ")


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"path": "a.py", "line_count": 401},
        {"path": "a.py", "line_count": 0},
        {"path": "a.py", "start_line": 0},
        {"path": "a.py", "start_line": "10"},
        {"path": "a.py", "start_line": True},
        {"path": "a.py", "line_count": 5.0},
        {"path": 7},
        {"path": "a.py", "extra": 1},
    ],
)
def test_read_refuses_arguments_outside_its_schema(
    tools: RepoTools, arguments: Mapping[str, object]
) -> None:
    assert call(tools, "read", arguments).startswith("refused: invalid_arguments: ")


@pytest.mark.parametrize(
    "arguments",
    [
        {"literal": ""},
        {"literal": "a" * 201},
        {"literal": "a\nb"},
        {"literal": "a\rb"},
        {"literal": "a\x00b"},
        {"literal": "a\x1bb"},
        {"literal": "a\x7fb"},
        {"literal": "x", "ignore_case": "yes"},
        {"literal": "x", "path_glob": ""},
        {"pattern": "x"},
    ],
)
def test_grep_refuses_arguments_outside_its_schema(
    tools: RepoTools, arguments: Mapping[str, object]
) -> None:
    assert call(tools, "grep", arguments).startswith("refused: invalid_arguments: ")


@pytest.mark.parametrize("arguments", [{"pattern": ""}, {"pattern": "a" * 201}, {}])
def test_glob_refuses_arguments_outside_its_schema(
    tools: RepoTools, arguments: Mapping[str, object]
) -> None:
    assert call(tools, "glob", arguments).startswith("refused: invalid_arguments: ")


def test_grep_literal_holding_a_nul_is_refused_and_recorded(tools: RepoTools) -> None:
    text = call(tools, "grep", {"literal": "a\x00b"})

    assert text.startswith("refused: invalid_arguments: ")
    assert tools.ledger[-1].reason is RefusalReason.INVALID_ARGUMENTS


def test_grep_finds_a_literal_holding_a_tab(repo: Path) -> None:
    tools = make_tools(repo, commit(repo, {"a.py": "if x:\n\treturn total(\n"}))

    assert call(tools, "grep", {"literal": "\treturn"}) == "a.py:2:\treturn total("


def test_unknown_tool_is_refused_and_its_name_is_not_kept(tools: RepoTools) -> None:
    text = call(tools, "Bash", {"command": "rm -rf /"})

    assert text.startswith("refused: unknown_tool: ")
    assert tools.ledger[-1].tool is None


def test_grep_lists_matches_by_path_then_line(tools: RepoTools) -> None:
    text = call(tools, "grep", {"literal": "total("})

    assert text == "billing/invoice.py:1:def total():\ndocs/notes.md:1:total( in prose"
    assert tools.ledger[-1].spans == (
        LineSpan("billing/invoice.py", 1, 1),
        LineSpan("docs/notes.md", 1, 1),
    )


def test_grep_ignoring_case_matches_other_case(tools: RepoTools) -> None:
    text = call(tools, "grep", {"literal": "total(", "ignore_case": True})

    assert "billing/invoice.py:2:    return TOTAL(1)" in text.split("\n")


def test_grep_path_glob_limits_the_files(tools: RepoTools) -> None:
    text = call(tools, "grep", {"literal": "total(", "path_glob": "billing/*.py"})

    assert text == "billing/invoice.py:1:def total():"


def test_grep_treats_the_literal_as_text_not_a_pattern(repo: Path) -> None:
    tools = make_tools(repo, commit(repo, {"a.py": "axb\na.b\n"}))

    assert call(tools, "grep", {"literal": "a.b"}) == "a.py:2:a.b"


def test_grep_with_no_match_says_so(tools: RepoTools) -> None:
    assert call(tools, "grep", {"literal": "nowhere"}) == "No line matches."
    assert tools.ledger[-1].outcome is ToolOutcome.OK


def test_grep_returns_the_first_50_matches_with_a_note(repo: Path) -> None:
    tools = make_tools(repo, commit(repo, {"a.py": "hit\n" * 60}))

    *lines, note = call(tools, "grep", {"literal": "hit"}).split("\n")

    assert lines == [f"a.py:{number}:hit" for number in range(1, 51)]
    assert note.startswith("(truncated")
    assert tools.ledger[-1].outcome is ToolOutcome.TRUNCATED


def test_glob_lists_matching_files_sorted(tools: RepoTools) -> None:
    assert call(tools, "glob", {"pattern": "billing/*.py"}) == (
        "billing/checkout.py\nbilling/invoice.py"
    )
    assert tools.ledger[-1].listed == ("billing/checkout.py", "billing/invoice.py")


def test_glob_double_star_matches_any_depth(tools: RepoTools) -> None:
    assert call(tools, "glob", {"pattern": "**/*.md"}) == "docs/notes.md"


def test_glob_with_no_match_says_so(tools: RepoTools) -> None:
    assert call(tools, "glob", {"pattern": "*.rs"}) == "No file matches."


def test_glob_returns_the_first_200_paths_with_a_note(repo: Path) -> None:
    tools = make_tools(repo, commit(repo, {f"f{number:03}.py": "" for number in range(201)}))

    *lines, note = call(tools, "glob", {"pattern": "*.py"}).split("\n")

    assert lines == [f"f{number:03}.py" for number in range(200)]
    assert note.startswith("(truncated")
    assert tools.ledger[-1].outcome is ToolOutcome.TRUNCATED


def test_calls_past_the_call_cap_are_refused(repo: Path) -> None:
    tools = make_tools(
        repo,
        commit(repo, {"a.py": "x\n"}),
        ToolCaps(max_calls=1, max_result_bytes=16_000, max_total_bytes=96_000),
    )

    call(tools, "read", {"path": "a.py"})

    assert call(tools, "read", {"path": "a.py"}).startswith("refused: call_limit: ")
    assert call(tools, "nonsense", {}).startswith("refused: call_limit: ")


def test_a_result_past_the_byte_cap_is_refused_and_so_is_every_later_call(repo: Path) -> None:
    tools = make_tools(
        repo,
        commit(repo, {"a.py": "a" * 1500 + "\n", "b.py": "b\n"}),
        ToolCaps(max_calls=12, max_result_bytes=16_000, max_total_bytes=2000),
    )

    call(tools, "read", {"path": "a.py"})

    assert call(tools, "read", {"path": "a.py"}).startswith("refused: byte_limit: ")
    assert call(tools, "read", {"path": "b.py"}).startswith("refused: byte_limit: ")


def test_refusals_do_not_count_against_the_byte_cap(repo: Path) -> None:
    tools = make_tools(
        repo,
        commit(repo, {"a.py": "a" * 900 + "\n"}),
        ToolCaps(max_calls=12, max_result_bytes=16_000, max_total_bytes=1000),
    )

    for _ in range(5):
        call(tools, "read", {"path": "missing.py"})

    assert call(tools, "read", {"path": "a.py"}).startswith("1\t")


def test_every_result_is_framed_by_lines_carrying_its_hash(tools: RepoTools) -> None:
    framed = asyncio.run(tools.call("read", {"path": "missing.py"}))
    opening, *body, closing = framed.split("\n")

    digest = hashlib.sha256("\n".join(body).encode("utf-8")).hexdigest()
    assert opening == f"<<<UNTRUSTED-TOOL-RESULT-{digest}>>>"
    assert closing == f"<<<END-UNTRUSTED-TOOL-RESULT-{digest}>>>"


def test_ledger_records_the_size_and_input_hash_but_no_text(tools: RepoTools) -> None:
    arguments = {"path": "billing/invoice.py", "line_count": 1}

    text = call(tools, "read", arguments)

    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    assert tools.ledger == (
        ToolCall(
            tool=ToolName.READ,
            outcome=ToolOutcome.OK,
            reason=None,
            spans=(LineSpan("billing/invoice.py", 1, 1),),
            listed=(),
            result_bytes=len(text.encode("utf-8")),
            input_sha256=hashlib.sha256(canonical.encode("ascii")).hexdigest(),
        ),
    )


def test_definitions_name_the_three_tools_with_object_schemas(tools: RepoTools) -> None:
    definitions = tools.definitions

    assert [definition.name for definition in definitions] == ["read", "grep", "glob"]
    assert all(definition.input_schema["type"] == "object" for definition in definitions)
    assert all(
        definition.input_schema["additionalProperties"] is False for definition in definitions
    )


def test_grep_on_unreadable_git_output_is_refused_and_the_review_goes_on(
    tools: RepoTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_run_git = GitRepo._run_git

    def garbled_grep(
        repo: GitRepo, args: list[str], read: Callable[..., Any], **kwargs: object
    ) -> object:
        if args[0] == "grep":
            return read(io.BytesIO(b"not-the-commit:a.py\x001\x00text\n"))[0]
        return real_run_git(repo, args, read, **kwargs)

    monkeypatch.setattr(GitRepo, "_run_git", garbled_grep)

    assert call(tools, "grep", {"literal": "x"}).startswith("refused: git_failed: ")
    assert call(tools, "glob", {"pattern": "*.md"}) == "docs/notes.md"


@pytest.mark.parametrize(
    "caps",
    [
        {"max_calls": 0, "max_result_bytes": 1000, "max_total_bytes": 1},
        {"max_calls": 1, "max_result_bytes": 999, "max_total_bytes": 1},
        {"max_calls": 1, "max_result_bytes": 1000, "max_total_bytes": 0},
    ],
)
def test_tool_caps_reject_values_too_small_to_hold_a_result(caps: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="at least"):
        ToolCaps(**caps)
