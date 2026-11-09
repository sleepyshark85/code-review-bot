"""Pure: the tokens of a fragment of Python, read without parsing it.

A diff shows fragments of files, so each line is split into tokens with one regular expression
whose every repeat is possessive, in time linear in the line's length.
"""

import re
from collections.abc import Iterator, Sequence
from typing import Final, Literal, cast

from review_bot.diff_parser import DiffLine

__all__ = ["TokenKind", "line_tokens", "tokens_of"]

# One token of a line of Python, after any whitespace. A string with no closing quote runs to the
# line's end. A number is a whole literal: hexadecimal, octal or binary, or decimal with an optional
# point, exponent and imaginary ``j``. Every repeat is possessive, so a match never backtracks into
# it.
_TOKEN: Final = re.compile(
    r"\s*+(?:"
    r"(?P<triple_quote>[rRbBuUfF]{0,2}+(?:'''|\"\"\"))"
    r"|(?P<string>[rRbBuUfF]{0,2}+"
    r"(?:'[^'\\]*+(?:\\.?+[^'\\]*+)*+'?+|\"[^\"\\]*+(?:\\.?+[^\"\\]*+)*+\"?+))"
    r"|(?P<comment>#)"
    r"|(?P<name>[^\W\d]\w*+(?:\.[^\W\d]\w*+)*+)"
    r"|(?P<number>0[xXoObB]\w*+"
    r"|(?:\d[\d_]*+(?:\.[\d_]*+)?+|\.\d[\d_]*+)(?:[eE][+-]?+\d[\d_]*+)?+[jJ]?+)"
    r"|(?P<opener>[(\[{])"
    r"|(?P<closer>[)\]}])"
    r"|(?P<other>==|!=|<=|>=|:=|\*\*|\S)"
    r")"
)
# The ``_TOKEN`` groups, and ``unclosed_string`` for a hunk that ends inside a triple-quoted string.
type TokenKind = Literal[
    "triple_quote",
    "string",
    "comment",
    "name",
    "number",
    "opener",
    "closer",
    "other",
    "unclosed_string",
]


def tokens_of(lines: Sequence[DiffLine]) -> Iterator[tuple[int, TokenKind, str]]:
    """Yield each code token of ``lines``: its line index, kind and text.

    Comments and line-continuation backslashes are skipped. A triple-quoted string, which may span
    lines, is yielded as its opening quotes, and its text is skipped. If it is still open after the
    last line, an ``unclosed_string`` token, with no text, comes last.
    """
    open_triple_quote: str | None = None
    for index, line in enumerate(lines):
        # A backslash ending a line joins it to the next, so it separates tokens as a space would.
        # Inside a string it can't end the string either way.
        text = line.text.removesuffix("\\")
        position = 0
        while True:
            if open_triple_quote is not None:
                end = text.find(open_triple_quote, position)
                if end < 0:
                    break
                position = end + len(open_triple_quote)
                open_triple_quote = None
            token = _TOKEN.match(text, position)
            if token is None or token.lastgroup == "comment":
                break
            position = token.end()
            # Every alternative of ``_TOKEN`` is a group named in ``TokenKind``, so one matched.
            kind = cast(TokenKind, token.lastgroup)
            token_text = token[kind]
            if kind == "triple_quote":
                open_triple_quote = token_text[-3:]
            yield index, kind, token_text
    if open_triple_quote is not None:
        yield len(lines) - 1, "unclosed_string", ""


def line_tokens(line: DiffLine) -> list[tuple[TokenKind, str]] | None:
    """The code tokens of ``line`` read alone, each kind and text, as ``tokens_of`` yields them.

    None if the line can't be read alone, as it ends inside a string: a triple-quoted one, which
    may close on a later line, or one with no closing quote.
    """
    found = [(kind, text) for _, kind, text in tokens_of([line])]
    if not found:
        return found
    last_kind, last_text = found[-1]
    if last_kind == "unclosed_string" or (last_kind == "string" and not _is_closed(last_text)):
        return None
    return found


def _is_closed(string: str) -> bool:
    """Whether a one-line string token ends with its closing quote, not an escaped one.

    ``_TOKEN`` runs a string with no closing quote to the line's end, so only the last token of
    a line can be unclosed.
    """
    quoted = string.lstrip("rRbBuUfF")
    quote, rest = quoted[0], quoted[1:]
    if not rest.endswith(quote):
        return False
    before_quote = rest[:-1]
    backslashes = len(before_quote) - len(before_quote.rstrip("\\"))
    return backslashes % 2 == 0
