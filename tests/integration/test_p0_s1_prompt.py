"""P0-S1 AC12 to AC15: the diff is carried unchanged, as delimited data."""

import re

import pytest
from diff_support import MINIMAL_DIFF, section

from review_bot.findings import Category, Finding, Severity

pytestmark = pytest.mark.integration

FINDING_FIELDS = ("file", "line", "severity", "category", "rationale", "confidence")
SEVERITY_VALUES = ("low", "medium", "high", "critical")
CATEGORY_VALUES = ("logic", "security", "reliability", "maintainability", "testing")

ANY_DIFF = {
    "lf_terminated": "diff --git a/x.py b/x.py\n+unique-diff-marker = 1\n",
    "no_final_newline": "diff --git a/x.py b/x.py\n+unique-diff-marker = 1",
    "crlf": "diff --git a/x.py b/x.py\r\n+unique-diff-marker = 1\r\n",
    "non_ascii": "diff --git a/x.py b/x.py\n+unique-diff-marker = 'é→'\n",
}


def delimiter_lines(user_prompt: str, diff: str) -> tuple[str, str]:
    """Check `user_prompt` is opening line, diff, closing line, and return the two lines.

    One newline may separate the diff from the closing line even if the diff ends with its own;
    without a final newline in the diff, that separator is required.
    """
    before, found, after = user_prompt.partition(diff)
    assert found, "the diff isn't in user_prompt unchanged"
    assert before.endswith("\n"), "the opening delimiter isn't a line of its own"
    if not diff.endswith("\n"):
        assert after.startswith("\n"), "the closing delimiter isn't a line of its own"
    opening = before.removesuffix("\n")
    closing = after.removeprefix("\n").removesuffix("\n")
    assert opening.strip(), "there is no opening delimiter"
    assert closing.strip(), "there is no closing delimiter"
    assert "\n" not in opening, "more than one line comes before the diff"
    assert "\n" not in closing, "more than one line comes after the diff"
    return opening, closing


def test_p0_s1_ac12_sample_diff_is_not_rejected(bot, sample_diff):
    provider = bot.mock(findings=[])

    run = bot.review(sample_diff, provider=provider)

    # Not rejected: its swallowed exception is a rule finding since P1-S7 (AC1), so it exits 1.
    assert run.exit_code == 1, run.stderr
    assert len(provider.requests) == 1


def test_p0_s1_ac12_sample_diff_text_reaches_the_prompt_unchanged(bot, sample_diff):
    text = sample_diff.read_bytes().decode("utf-8")

    request = bot.request_for(sample_diff, exit_code=1)  # P1-S7 AC1: a rule finding

    assert text in request.user_prompt
    assert "{self.id}" in request.user_prompt


def test_p0_s1_ac12_template_metacharacters_reach_the_prompt_unchanged(
    bot, write_diff, fixture_text
):
    text = fixture_text("template_metacharacters.diff")

    request = bot.request_for(write_diff(text))

    assert text in request.user_prompt


def test_p0_s1_ac12_crlf_line_endings_reach_the_prompt_unchanged(bot, write_diff):
    text = "diff --git a/x b/x\r\n+y\r\n"

    request = bot.request_for(write_diff(text))

    assert text in request.user_prompt


# AC12's "text that is not a diff is accepted", and AC13's `single_line` case, were replaced by
# P1-S2 AC5: such text now exits 15 (`test_p1_s2_parse_and_skip.py`).


@pytest.mark.parametrize("diff", ANY_DIFF.values(), ids=ANY_DIFF.keys())
def test_p0_s1_ac13_user_prompt_is_opening_line_diff_closing_line(bot, write_diff, diff):
    request = bot.request_for(write_diff(diff))

    delimiter_lines(request.user_prompt, diff)


def test_p0_s1_ac13_sample_diff_is_framed_by_delimiter_lines(bot, sample_diff):
    text = sample_diff.read_bytes().decode("utf-8")

    request = bot.request_for(sample_diff, exit_code=1)  # P1-S7 AC1: a rule finding

    delimiter_lines(request.user_prompt, text)


def test_p0_s1_ac13_injection_attempt_stays_inside_the_delimiters(bot, write_diff, fixture_text):
    text = fixture_text("prompt_injection.diff")

    request = bot.request_for(write_diff(text))

    delimiter_lines(request.user_prompt, text)
    assert "ignore all previous instructions" not in request.system_prompt


def test_p0_s1_ac13_lookalike_delimiters_are_not_the_real_ones(bot, write_diff, fixture_text):
    text = fixture_text("prompt_injection.diff")

    opening, closing = delimiter_lines(bot.request_for(write_diff(text)).user_prompt, text)

    # Guards the fixture: its lines must keep the shape of the real delimiters, differing by hash.
    assert re.sub(r"[0-9a-f]{8,}", "0000", opening) in text
    assert re.sub(r"[0-9a-f]{8,}", "0000", closing) in text
    assert opening not in text
    assert closing not in text


def test_p0_s1_ac13_system_prompt_states_the_same_hash_rule(bot, write_diff):
    system_prompt = bot.request_for(write_diff(MINIMAL_DIFF)).system_prompt.lower()

    assert "same hash" in system_prompt


def test_p0_s1_ac13_system_prompt_says_lookalike_delimiters_are_data(bot, write_diff):
    system_prompt = bot.request_for(write_diff(MINIMAL_DIFF)).system_prompt.lower()

    assert re.search(r"[^.]*\bother hash\b[^.]*\bdata\b", system_prompt)


@pytest.mark.parametrize("diff", ANY_DIFF.values(), ids=ANY_DIFF.keys())
def test_p0_s1_ac13_system_prompt_does_not_contain_the_diff(bot, write_diff, diff):
    request = bot.request_for(write_diff(diff))

    assert diff not in request.system_prompt
    assert "unique-diff-marker" not in request.system_prompt


def test_p0_s1_ac13_system_prompt_says_delimited_content_is_untrusted_data(bot, write_diff):
    system_prompt = bot.request_for(write_diff(MINIMAL_DIFF)).system_prompt.lower()

    assert "untrusted" in system_prompt
    assert re.search(r"\bdata\b", system_prompt)
    assert "instructions" in system_prompt
    assert re.search(r"delimit|marker|between", system_prompt)


def _delimiters_of(bot, write_diff, text: str) -> tuple[str, str]:
    request = bot.request_for(write_diff(text))
    return delimiter_lines(request.user_prompt, text)


@pytest.mark.parametrize(
    "embed",
    ["{line}\n", "+{line}\n", "  context {line} trailing\n"],
    ids=["as_a_bare_line", "as_an_added_line", "inside_a_longer_line"],
)
def test_p0_s1_ac14_delimiters_from_another_diff_are_not_reused(bot, write_diff, embed):
    other_opening, other_closing = _delimiters_of(
        bot, write_diff, section("other.py", "+the other diff\n")
    )
    diff = (
        section("x.py", "")
        + embed.format(line=other_opening)
        + "+between\n"
        + embed.format(line=other_closing)
    )

    opening, closing = _delimiters_of(bot, write_diff, diff)

    assert opening not in diff
    assert closing not in diff


def test_p0_s1_ac14_delimiters_stay_unique_as_collisions_accumulate(bot, write_diff):
    diff = "diff --git a/x b/x\n+start\n"

    for _ in range(5):
        opening, closing = _delimiters_of(bot, write_diff, diff)

        assert opening not in diff
        assert closing not in diff

        diff += f"{opening}\n+{closing}\n"


def test_p0_s1_ac15_system_prompt_requires_the_bare_json_shape(bot, write_diff):
    system_prompt = bot.request_for(write_diff(MINIMAL_DIFF)).system_prompt

    assert "json" in system_prompt.lower()
    assert re.search(r'\{\s*"findings"\s*:\s*\[', system_prompt)


def test_p0_s1_ac15_system_prompt_forbids_a_code_fence(bot, write_diff):
    system_prompt = bot.request_for(write_diff(MINIMAL_DIFF)).system_prompt.lower()

    assert "fence" in system_prompt


def labels_field(system_prompt: str, field: str) -> bool:
    # As a label: `file` and `line` also occur as ordinary words in the prompt's prose.
    return re.search(rf"\b{field}\b\s*:", system_prompt) is not None


def names_value(system_prompt: str, value: str) -> bool:
    return re.search(rf"\b{value}\b", system_prompt) is not None


@pytest.mark.parametrize("field", FINDING_FIELDS)
def test_p0_s1_ac15_system_prompt_labels_each_finding_field(bot, write_diff, field):
    system_prompt = bot.request_for(write_diff(MINIMAL_DIFF)).system_prompt

    assert labels_field(system_prompt, field), f"{field!r} isn't labelled"


@pytest.mark.parametrize("value", SEVERITY_VALUES + CATEGORY_VALUES)
def test_p0_s1_ac15_system_prompt_names_each_enum_value(bot, write_diff, value):
    system_prompt = bot.request_for(write_diff(MINIMAL_DIFF)).system_prompt

    assert names_value(system_prompt, value), f"{value!r} isn't named"


def test_p0_s1_ac15_system_prompt_covers_everything_in_the_real_schema(bot, write_diff):
    # Guards the literal lists above against the schema growing without the prompt following it.
    fields = list(Finding.model_fields)
    values = [member.value for member in (*Severity, *Category)]

    system_prompt = bot.request_for(write_diff(MINIMAL_DIFF)).system_prompt

    assert [field for field in fields if not labels_field(system_prompt, field)] == []
    assert [value for value in values if not names_value(system_prompt, value)] == []
    assert {*fields, *values} == {*FINDING_FIELDS, *SEVERITY_VALUES, *CATEGORY_VALUES}
