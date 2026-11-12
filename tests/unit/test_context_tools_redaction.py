"""Personal data in tool results is masked before the result is fitted and returned (P4-S4)."""

import asyncio
from pathlib import Path

import pytest
from git_samples import commit, init_repo

from review_bot.context.ledger import LineSpan
from review_bot.context.tool_text import utf8_size
from review_bot.context.tools import RepoRef, RepoTools, ToolCaps
from review_bot.git import GitRepo
from review_bot.governance.redaction import RedactionKind

CAPS = ToolCaps(max_calls=12, max_result_bytes=16_000, max_total_bytes=96_000)
EMAIL = frozenset({RedactionKind.EMAIL})
CONTACTS = "owner = 'jane@rightship.com'\nphone = '+12025550143'\nsupport = 'bob@rightship.com'\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def make_tools(
    repo: Path,
    files: dict[str, str],
    *,
    caps: ToolCaps = CAPS,
    redaction: frozenset[RedactionKind] = EMAIL,
) -> RepoTools:
    sha = commit(repo, files)
    return RepoTools(
        RepoRef(git=GitRepo(repo, timeout_s=60), commit=sha), caps=caps, redaction=redaction
    )


def call(tools: RepoTools, name: str, arguments: dict[str, object]) -> str:
    framed = asyncio.run(tools.call(name, arguments))
    return "\n".join(framed.split("\n")[1:-1])


def test_read_masks_each_line_and_the_ledger_describes_the_masked_text(repo: Path) -> None:
    tools = make_tools(repo, {"contacts.py": CONTACTS})

    text = call(tools, "read", {"path": "contacts.py"})

    assert text == (
        "1\towner = '[REDACTED:email]'\n2\tphone = '+12025550143'\n3\tsupport = '[REDACTED:email]'"
    )
    assert tools.ledger[-1].spans == (LineSpan("contacts.py", 1, 3),)
    assert tools.ledger[-1].result_bytes == utf8_size(text)
    assert tools.redacted == {RedactionKind.EMAIL: 2}


def test_read_masks_a_long_line_before_cutting_it(repo: Path) -> None:
    tools = make_tools(repo, {"long.py": "x" * 1990 + " jane@rightship.com\n"})

    text = call(tools, "read", {"path": "long.py"})

    assert text == "1\t" + "x" * 1990 + " [REDACTED (cut)"


def test_grep_masks_the_matching_lines(repo: Path) -> None:
    tools = make_tools(repo, {"contacts.py": CONTACTS})

    text = call(tools, "grep", {"literal": "rightship"})

    assert (
        text
        == "contacts.py:1:owner = '[REDACTED:email]'\ncontacts.py:3:support = '[REDACTED:email]'"
    )
    assert tools.redacted == {RedactionKind.EMAIL: 2}


def test_glob_masks_a_path_but_the_ledger_lists_the_real_one(repo: Path) -> None:
    tools = make_tools(repo, {"people/jane@rightship.com/notes.txt": "hi\n"})

    text = call(tools, "glob", {"pattern": "people/**"})

    assert text == "people/[REDACTED:email]/notes.txt"
    assert tools.ledger[-1].listed == ("people/jane@rightship.com/notes.txt",)
    assert tools.redacted == {RedactionKind.EMAIL: 1}


def test_counts_cover_only_the_lines_a_capped_result_returns(repo: Path) -> None:
    lines = "".join(f"{'y' * 95} a{number}@rightship.com\n" for number in range(300))
    tools = make_tools(
        repo,
        {"big.py": lines},
        caps=ToolCaps(max_calls=12, max_result_bytes=1_200, max_total_bytes=96_000),
    )

    text = call(tools, "read", {"path": "big.py"})

    returned = text.count("[REDACTED:email]")
    assert 0 < returned < 300
    assert tools.redacted == {RedactionKind.EMAIL: returned}


def test_a_result_over_the_total_byte_cap_counts_nothing(repo: Path) -> None:
    tools = make_tools(
        repo,
        {"contacts.py": CONTACTS + "z" * 2_000 + "\n"},
        caps=ToolCaps(max_calls=12, max_result_bytes=16_000, max_total_bytes=100),
    )

    call(tools, "read", {"path": "contacts.py"})

    assert tools.redacted == {}


def test_with_no_kinds_a_result_is_as_it_was(repo: Path) -> None:
    tools = make_tools(repo, {"contacts.py": CONTACTS}, redaction=frozenset())

    text = call(tools, "read", {"path": "contacts.py"})

    assert "1\towner = 'jane@rightship.com'" in text
    assert tools.redacted == {}
