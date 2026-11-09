import pytest

from review_bot.diff_parser import DiffLine, LineKind
from review_bot.python_tokens import line_tokens


def added(text: str) -> DiffLine:
    return DiffLine(kind=LineKind.ADDED, text=text, new_line=1)


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("x = 19.99", "19.99"),
        ("x = .5", ".5"),
        ("x = 1e3", "1e3"),
        ("x = 2.5E-4j", "2.5E-4j"),
        ("x = 1_000.50", "1_000.50"),
        ("x = 0x1e3", "0x1e3"),
        ("x = 10.", "10."),
    ],
)
def test_line_tokens_reads_a_number_literal_whole(text: str, number: str) -> None:
    assert line_tokens(added(text)) == [("name", "x"), ("other", "="), ("number", number)]


def test_line_tokens_reads_a_name_with_digits_as_one_name() -> None:
    assert line_tokens(added("rate_1e3")) == [("name", "rate_1e3")]


def test_line_tokens_skips_a_comment_and_keeps_a_closed_string_whole() -> None:
    assert line_tokens(added("f('1.5')  # 2.5")) == [
        ("name", "f"),
        ("opener", "("),
        ("string", "'1.5'"),
        ("closer", ")"),
    ]


@pytest.mark.parametrize("text", ['s = """', "s = '''x", "s = 'x", "s = 'x\\'", 's = b"x\\\\\\"'])
def test_line_tokens_cant_read_a_line_ending_inside_a_string(text: str) -> None:
    assert line_tokens(added(text)) is None


@pytest.mark.parametrize("text", ['s = """x"""', "s = 'x\\\\'", "s = ''", ""])
def test_line_tokens_reads_a_line_whose_strings_close(text: str) -> None:
    assert line_tokens(added(text)) is not None
