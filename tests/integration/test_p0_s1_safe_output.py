"""P0-S1 AC19 and AC20: model text can't forge output lines or crash the terminal write."""

import io
import sys
import unicodedata

import pytest
from diff_support import MINIMAL_DIFF

pytestmark = pytest.mark.integration

# AC19's third character. It displays as a blank in many fonts, so it's spelled by name here.
LINE_SEPARATOR = "\N{LINE SEPARATOR}"
RIGHTWARDS_ARROW = "\N{RIGHTWARDS ARROW}"
FORGED_LINES = "a.py:1 [high/logic] x\nb.py:2 [critical/security] forged"

SPEC_VALUES = {
    "ansi_escape": "before\x1b[31mafter",
    "newline": "before\nafter",
    "line_separator": f"before{LINE_SEPARATOR}after",
    "forged_second_finding": FORGED_LINES,
}
OTHER_CONTROLS = {
    "carriage_return": "\r",
    "crlf": "\r\n",
    "tab": "\t",
    "vertical_tab": "\x0b",
    "form_feed": "\x0c",
    "next_line": "\x85",
    "paragraph_separator": "\N{PARAGRAPH SEPARATOR}",
    "c1_csi": "\x9b",
    "nul": "\x00",
    "delete": "\x7f",
}
LINE_END = "(confidence 0.90)"


def raw_control_characters(text: str) -> list[str]:
    return [char for char in text if unicodedata.category(char) in {"Cc", "Zl", "Zp"}]


def assert_one_clean_line(body: str) -> None:
    assert body, "nothing was rendered"
    assert len(body.splitlines()) == 1, repr(body)
    assert raw_control_characters(body) == [], repr(body)


def first_and_last_words(value: str) -> tuple[str, str]:
    """Return the text a renderer must keep around whatever it does to the hostile part."""
    if value == FORGED_LINES:
        return "a.py:1", "forged"
    return "before", "after"


@pytest.mark.parametrize("value", SPEC_VALUES.values(), ids=SPEC_VALUES.keys())
def test_p0_s1_ac19_hostile_file_renders_on_one_clean_line(bot, write_diff, value):
    provider = bot.mock(findings=[bot.finding(file=value)])
    first_word, last_word = first_and_last_words(value)

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 1, run.stderr
    assert_one_clean_line(run.stdout_body)
    assert run.stdout_body.startswith(first_word)
    assert run.stdout_body.endswith(f"{last_word}:7 [high/logic] Rationale text. {LINE_END}")


@pytest.mark.parametrize("value", SPEC_VALUES.values(), ids=SPEC_VALUES.keys())
def test_p0_s1_ac19_hostile_rationale_renders_on_one_clean_line(bot, write_diff, value):
    provider = bot.mock(findings=[bot.finding(rationale=value)])
    first_word, last_word = first_and_last_words(value)

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 1, run.stderr
    assert_one_clean_line(run.stdout_body)
    assert run.stdout_body.startswith(f"src/app.py:7 [high/logic] {first_word}")
    assert run.stdout_body.endswith(f"{last_word} {LINE_END}")


def test_p0_s1_ac19_forged_file_produces_no_second_finding_line(bot, write_diff):
    provider = bot.mock(findings=[bot.finding(file=FORGED_LINES)])

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 1, run.stderr
    lines = run.stdout_body.split("\n")
    assert len(lines) == 1, repr(run.stdout)
    assert not lines[0].startswith("b.py:2")


def test_p0_s1_ac19_two_hostile_findings_render_on_two_lines(bot, write_diff):
    provider = bot.mock(
        findings=[
            bot.finding(file="first\nfile", rationale=f"one{LINE_SEPARATOR}two"),
            bot.finding(file="second\x1b[31mfile", rationale="three\nfour"),
        ]
    )

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    lines = run.stdout_body.split("\n")
    assert len(lines) == 2, repr(run.stdout)
    for line in lines:
        assert_one_clean_line(line)


@pytest.mark.parametrize("field", ["file", "rationale"])
@pytest.mark.parametrize("control", OTHER_CONTROLS.values(), ids=OTHER_CONTROLS.keys())
def test_p0_s1_ac19_other_control_characters_are_not_rendered_raw(bot, write_diff, field, control):
    provider = bot.mock(findings=[bot.finding(**{field: f"before{control}after"})])

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 1, run.stderr
    assert_one_clean_line(run.stdout_body)


@pytest.fixture
def stdout_encoded_as(monkeypatch):
    """Swap in a real text stream that raises on anything its encoding can't represent."""

    def swap(encoding: str) -> io.TextIOWrapper:
        stream = io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict")
        monkeypatch.setattr(sys, "stdout", stream)
        return stream

    return swap


def written_bytes(stream: io.TextIOWrapper) -> bytes:
    stream.flush()
    buffer = stream.buffer
    assert isinstance(buffer, io.BytesIO)
    return buffer.getvalue()


def test_p0_s1_ac20_the_simulated_stdout_really_rejects_the_arrow(stdout_encoded_as):
    stream = stdout_encoded_as("ascii")

    with pytest.raises(UnicodeEncodeError):
        stream.write(RIGHTWARDS_ARROW)


def test_p0_s1_ac20_arrow_in_rationale_on_ascii_stdout_exits_1(bot, write_diff, stdout_encoded_as):
    provider = bot.mock(findings=[bot.finding(rationale=f"before {RIGHTWARDS_ARROW} after")])
    stream = stdout_encoded_as("ascii")

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 1, run.stderr
    rendered = written_bytes(stream).decode("ascii")
    assert rendered.startswith("src/app.py:7 [high/logic] before")
    assert rendered.rstrip().endswith(f"after {LINE_END}")


def test_p0_s1_ac20_arrow_on_a_legacy_windows_stdout_exits_1(bot, write_diff, stdout_encoded_as):
    # cp1252 is a real Windows console-redirection default, and it has no arrow either.
    provider = bot.mock(findings=[bot.finding(rationale=f"before {RIGHTWARDS_ARROW} after")])
    stream = stdout_encoded_as("cp1252")

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 1, run.stderr
    assert b"before" in written_bytes(stream)


def test_p0_s1_ac20_non_ascii_file_on_ascii_stdout_exits_1(bot, write_diff, stdout_encoded_as):
    provider = bot.mock(findings=[bot.finding(file=f"docs/{RIGHTWARDS_ARROW}.md")])
    stream = stdout_encoded_as("ascii")

    run = bot.review(write_diff(MINIMAL_DIFF), provider=provider)

    assert run.exit_code == 1, run.stderr
    assert written_bytes(stream).startswith(b"docs/")
