import time

import pytest
from markdown_it import MarkdownIt

from review_bot.diff_parser import DiffLine, LineKind
from review_bot.suggestion import SuggestionDropReason, fenced_block, suggestion_drop_reason

ADDED = DiffLine(LineKind.ADDED, "        return subtotal * 1.2", 14)
# A key in the vendor format `secret_scan` detects, built so this file holds no literal key.
GITHUB_TOKEN = "gh" + "p_" + "A1b2C3d4" * 4 + "E5f6"


def code_blocks(markdown: str) -> list[tuple[str, str]]:
    return [
        (token.info, token.content)
        for token in MarkdownIt("commonmark").parse(markdown)
        if token.type == "fence"
    ]


@pytest.mark.parametrize(
    "suggestion",
    ["        return subtotal", "", "\tx = 1\n\ty = 2", "\n".join(["x"] * 10), "x" * 1_000],
    ids=["one-line", "empty-deletes", "tabs-and-lines", "10-lines", "1000-chars"],
)
def test_suggestion_drop_reason_keeps_a_fitting_replacement_of_an_added_line(
    suggestion: str,
) -> None:
    assert suggestion_drop_reason(suggestion, ADDED) is None


def test_suggestion_drop_reason_refuses_a_context_line() -> None:
    context = DiffLine(LineKind.CONTEXT, "x = 1", 3)

    assert suggestion_drop_reason("x = 2", context) is SuggestionDropReason.NOT_ADDED_LINE


@pytest.mark.parametrize(
    "suggestion", ["\n".join(["x"] * 11), "x" * 1_001], ids=["11-lines", "1001-chars"]
)
def test_suggestion_drop_reason_refuses_more_than_10_lines_or_1000_characters(
    suggestion: str,
) -> None:
    assert suggestion_drop_reason(suggestion, ADDED) is SuggestionDropReason.TOO_LONG


@pytest.mark.parametrize(
    "suggestion",
    [
        "        return subtotal * 1.2",
        "        return subtotal * 1.2  \t",
        "        return subtotal * 1.2\n",
    ],
)
def test_suggestion_drop_reason_refuses_the_anchored_line_up_to_trailing_whitespace(
    suggestion: str,
) -> None:
    assert suggestion_drop_reason(suggestion, ADDED) is SuggestionDropReason.UNCHANGED


@pytest.mark.parametrize("ending", ["\r", "\r\n", "\u2028", "\x85", "\x0b"])
def test_suggestion_drop_reason_counts_only_spaces_tabs_and_lfs_as_trailing_whitespace(
    ending: str,
) -> None:
    suggestion = ADDED.text + ending

    assert suggestion_drop_reason(suggestion, ADDED) is SuggestionDropReason.CONTROL_CHARACTERS


@pytest.mark.parametrize("suggestion", ["x = 1\r", "x = 1\r\n"], ids=["identical", "crlf"])
def test_suggestion_drop_reason_on_a_crlf_line_drops_control_characters_not_unchanged(
    suggestion: str,
) -> None:
    crlf_line = DiffLine(LineKind.ADDED, "x = 1\r", 5)

    assert suggestion_drop_reason(suggestion, crlf_line) is SuggestionDropReason.CONTROL_CHARACTERS


def test_suggestion_drop_reason_keeps_a_change_of_leading_indentation() -> None:
    assert suggestion_drop_reason(ADDED.text.lstrip(), ADDED) is None


def test_suggestion_drop_reason_keeps_an_empty_suggestion_for_a_blank_line() -> None:
    blank = DiffLine(LineKind.ADDED, "   ", 2)

    assert suggestion_drop_reason("", blank) is None


@pytest.mark.parametrize(
    "char",
    [
        *["\r", "\x00", "\x1b", "\x7f", "\x85", "\x0b"],
        *["\u202a", "\u202e", "\u2066", "\u2069", "\u200e", "\u200f", "\u2028", "\u2029"],
    ],
)
def test_suggestion_drop_reason_refuses_a_control_character_other_than_tab_or_lf(
    char: str,
) -> None:
    suggestion = f"x = 1{char}"

    assert suggestion_drop_reason(suggestion, ADDED) is SuggestionDropReason.CONTROL_CHARACTERS


def test_suggestion_drop_reason_refuses_a_detected_secret() -> None:
    suggestion = f'token = "{GITHUB_TOKEN}"'

    assert suggestion_drop_reason(suggestion, ADDED) is SuggestionDropReason.POSSIBLE_SECRET


def test_suggestion_drop_reason_checks_the_longest_hostile_suggestion_quickly() -> None:
    started = time.perf_counter()
    for _ in range(100):
        suggestion_drop_reason("-" * 999 + "\n", ADDED)
        suggestion_drop_reason("sk-" + "a" * 997, ADDED)

    assert time.perf_counter() - started < 1.0


def test_fenced_block_opens_with_the_info_and_holds_exactly_the_text() -> None:
    block = fenced_block("        return subtotal", info="suggestion")

    assert block == "```suggestion\n        return subtotal\n```"
    assert code_blocks(block) == [("suggestion", "        return subtotal\n")]


def test_fenced_block_of_empty_text_is_an_empty_block() -> None:
    assert fenced_block("", info="suggestion") == "```suggestion\n```"
    assert code_blocks(fenced_block("")) == [("", "")]


@pytest.mark.parametrize(
    ("text", "fence"),
    [("a ``` b", "````"), ("```\n````", "`````"), ("`x`", "```"), ("~~~", "```")],
)
def test_fenced_block_fence_is_longer_than_any_backtick_run_inside(text: str, fence: str) -> None:
    block = fenced_block(text)

    assert block == f"{fence}\n{text}\n{fence}"
    assert code_blocks(block + "\n\nafter") == [("", f"{text}\n")]


@pytest.mark.parametrize("text", ["x\r```", "x\u2028```", "x = 1\u202e"])
def test_fenced_block_refuses_a_character_a_suggestion_may_not_hold(text: str) -> None:
    with pytest.raises(ValueError, match="control or bidi character"):
        fenced_block(text)


@pytest.mark.parametrize(
    "anchored",
    [ADDED, DiffLine(LineKind.CONTEXT, "x = 1", 3)],
    ids=["added-line", "context-line"],
)
def test_suggestion_drop_reason_refuses_a_placeholder_for_masked_data_first(
    anchored: DiffLine,
) -> None:
    suggestion = 'contact = "[REDACTED:email]"' + "x" * 1_000

    assert suggestion_drop_reason(suggestion, anchored) is SuggestionDropReason.REDACTED


def test_suggestion_drop_reason_keeps_a_suggestion_naming_redaction_without_a_placeholder() -> None:
    assert suggestion_drop_reason('label = "[REDACTED]"', ADDED) is None
