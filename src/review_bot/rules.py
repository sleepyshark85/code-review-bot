"""Checks that need no model, on the added lines of reviewable Python files.

A hunk is a fragment of a file, so each check reads the lines it shows rather than parsing Python.
A hit counts only if the hunk adds a line it rests on.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast

from review_bot.diff_parser import DiffLine, FileDiff, Hunk, LineKind, ParsedDiff
from review_bot.findings import Category, Finding, ReviewedFinding, RuleId, Severity, Source
from review_bot.fingerprint import Fingerprinter

__all__ = ["RuleResult", "run_rules"]

_PYTHON_SUFFIX: Final = ".py"
# An ``except`` clause's line, bare or naming exceptions, and whatever follows its colon.
# ``[^:]*`` stops at the first colon, so a clause holding a slice or a ``lambda`` is missed.
_EXCEPT_CLAUSE: Final = re.compile(r"\s*except\b[^:]*:(?P<after_colon>.*)")
# A statement that does nothing, with an optional trailing comment.
_DOES_NOTHING: Final = re.compile(r"(?:pass|\.\.\.)\s*(?:#.*)?")

# One token of a line of Python, after any whitespace. A string with no closing quote runs to the
# line's end. Every repeat is possessive, so a match never backtracks into it.
_TOKEN: Final = re.compile(
    r"\s*+(?:"
    r"(?P<triple_quote>[rRbBuUfF]{0,2}+(?:'''|\"\"\"))"
    r"|(?P<string>[rRbBuUfF]{0,2}+"
    r"(?:'[^'\\]*+(?:\\.?+[^'\\]*+)*+'?+|\"[^\"\\]*+(?:\\.?+[^\"\\]*+)*+\"?+))"
    r"|(?P<comment>#)"
    r"|(?P<name>[^\W\d]\w*+(?:\.[^\W\d]\w*+)*+)"
    r"|(?P<opener>[(\[{])"
    r"|(?P<closer>[)\]}])"
    r"|(?P<other>==|!=|<=|>=|:=|\*\*|\S)"
    r")"
)
# The ``_TOKEN`` groups, and ``unclosed_string`` for a hunk that ends inside a triple-quoted string.
type _TokenKind = Literal[
    "triple_quote", "string", "comment", "name", "opener", "closer", "other", "unclosed_string"
]
_CLOSER_OF: Final = {"(": ")", "[": "]", "{": "}"}
# The builtin ``open``, as a call to it is spelled.
_OPEN_NAMES: Final = frozenset({"open", "io.open"})
# Tokens before ``open`` that make it something else: an attribute, or a definition.
_NOT_THE_BUILTIN_AFTER: Final = frozenset({".", "def", "class"})
# A call spread over more lines than this is too unusual to judge from a fragment.
_MAX_CALL_LINES: Final = 20
# ``open(file, mode, buffering, encoding, ...)``: the positions of its parameters.
_MODE_POSITION: Final = 1
_ENCODING_POSITION: Final = 3
# A string literal that can be a mode, such as ``"w"`` or ``'r+b'``.
_MODE_LITERAL: Final = re.compile(r"[rRuU]?(['\"])(?P<mode>[a-zA-Z+]*)\1")

_SEVERITY_AND_RATIONALE: Final = {
    RuleId.SWALLOWED_EXCEPTION: (
        Severity.MEDIUM,
        "Exception swallowed: the handler discards it, so failures pass silently. "
        "Handle it, log it or re-raise it.",
    ),
    RuleId.OPEN_WITHOUT_ENCODING: (
        Severity.LOW,
        "open() without encoding uses the platform default. "
        'Pass encoding="utf-8", or open in binary mode.',
    ),
}


@dataclass(frozen=True, slots=True)
class RuleResult:
    """The rules' findings, by file and line, and how many hits had no line number to anchor to.

    A hit in a hunk whose header has no readable new start is counted, not reported (ADR 0006).
    """

    findings: tuple[ReviewedFinding, ...]
    unanchored_hits: int


@dataclass(frozen=True, slots=True)
class _Bracket:
    """A bracket not yet closed, with the tokens inside it so far.

    ``call_line_index`` is the index of the line holding the bracket if it opens a call to the
    builtin ``open``, and None otherwise. ``arguments`` holds one list of token texts per
    comma-separated part. A bracket nested in a part stands in it as its opening character.
    """

    closer: str
    call_line_index: int | None
    arguments: list[list[str]]


def run_rules(parsed: ParsedDiff) -> RuleResult:
    """Find swallowed exceptions and text files opened without an encoding in ``parsed``.

    Only reviewable ``.py`` files are checked.
    """
    anchored: list[tuple[str, int, RuleId, str]] = []
    unanchored_hits = 0
    for file in parsed.files:
        path = _reviewable_python_path(file)
        if path is None:
            continue
        for hunk in file.hunks:
            for rule, line in _hits(hunk):
                # A header declaring new start 0 numbers its first line 0, which can't anchor.
                if line.new_line is None or line.new_line < 1:
                    unanchored_hits += 1
                else:
                    anchored.append((path, line.new_line, rule, line.text))
    # The one place rule findings are ordered: verify keeps them as given. Sorted before
    # fingerprinting, so ordinals follow that order.
    anchored.sort(key=lambda hit: hit[:2])
    fingerprinter = Fingerprinter()
    findings = tuple(
        _rule_finding(path, line, rule, text, fingerprinter) for path, line, rule, text in anchored
    )
    return RuleResult(findings=findings, unanchored_hits=unanchored_hits)


def _reviewable_python_path(file: FileDiff) -> str | None:
    """The file's new path if the rules check it: not skipped, not deleted, and ending ``.py``."""
    if file.path is None or file.skip_reason is not None:
        return None
    return file.path if file.path.endswith(_PYTHON_SUFFIX) else None


def _hits(hunk: Hunk) -> Iterator[tuple[RuleId, DiffLine]]:
    """Yield each rule's hits in ``hunk``, with the line each one anchors to."""
    new_version = [line for line in hunk.lines if line.kind is not LineKind.REMOVED]
    for line in _swallowing_except_clauses(new_version):
        yield RuleId.SWALLOWED_EXCEPTION, line
    for line in _open_calls_without_encoding(new_version):
        yield RuleId.OPEN_WITHOUT_ENCODING, line


def _swallowing_except_clauses(new_version: Sequence[DiffLine]) -> Iterator[DiffLine]:
    """Yield each ``except`` line whose handler's first statement is ``pass`` or ``...``.

    The clause or that statement must be added, so a handler the hunk only shows as context isn't
    reported.
    """
    for index, line in enumerate(new_version):
        clause = _EXCEPT_CLAUSE.fullmatch(line.text)
        if clause is None:
            continue
        statement = _first_statement(new_version, index, clause["after_colon"])
        if statement is None:
            continue
        text, statement_line = statement
        is_added = LineKind.ADDED in (line.kind, statement_line.kind)
        if is_added and _DOES_NOTHING.fullmatch(text):
            yield line


def _first_statement(
    lines: Sequence[DiffLine], clause_index: int, after_colon: str
) -> tuple[str, DiffLine] | None:
    """The handler's first statement, stripped, and the line holding it.

    That is the text after the colon, in a one-line handler, or else the first line after
    ``lines[clause_index]`` that isn't blank or only a comment. None if the hunk ends before it.
    Indexed, not sliced or skipped through, so each clause costs only the lines it reads.
    """
    inline = after_colon.strip()
    if inline and not inline.startswith("#"):
        return inline, lines[clause_index]
    for index in range(clause_index + 1, len(lines)):
        line = lines[index]
        text = line.text.strip()
        if text and not text.startswith("#"):
            return text, line
    return None


def _open_calls_without_encoding(new_version: Sequence[DiffLine]) -> Iterator[DiffLine]:
    """Yield the line holding ``open(`` for each call surely opening a text file with no encoding.

    A call counts only if the hunk doesn't end inside a triple-quoted string, and the call closes
    within the hunk, spans at most ``_MAX_CALL_LINES`` lines, one of them added, and has arguments
    that can be judged. When unsure, the rule stays silent.
    """
    for first, last, arguments in _open_calls(new_version):
        if last - first >= _MAX_CALL_LINES:
            continue
        is_added = any(line.kind is LineKind.ADDED for line in new_version[first : last + 1])
        if is_added and _opens_text_without_encoding(arguments):
            yield new_version[first]


def _open_calls(lines: Sequence[DiffLine]) -> list[tuple[int, int, list[list[str]]]]:
    """Each closed call to the builtin ``open``: its first and last line index, and arguments.

    One pass over the tokens, matching brackets on a stack, so the work is linear in the hunk's
    size. A closing bracket that doesn't match the innermost open one means the fragment's shape
    is unclear, so every bracket still open is dropped. A hunk that ends inside a triple-quoted
    string may have started inside one, reading prose as code and code as prose, so it gives no
    call at all.
    """
    calls: list[tuple[int, int, list[list[str]]]] = []
    stack: list[_Bracket] = []
    before_previous, previous = "", ""  # the texts of the two tokens before this one
    for index, kind, text in _tokens(lines):
        if kind == "unclosed_string":
            return []
        if kind != "closer":
            if stack:
                _add_token(stack[-1].arguments, kind, text)
            if kind == "opener":
                is_open_call = (
                    text == "("
                    and previous in _OPEN_NAMES
                    and before_previous not in _NOT_THE_BUILTIN_AFTER
                )
                stack.append(_Bracket(_CLOSER_OF[text], index if is_open_call else None, [[]]))
        elif stack and stack[-1].closer == text:
            bracket = stack.pop()
            if bracket.call_line_index is not None:
                calls.append((bracket.call_line_index, index, bracket.arguments))
        else:
            stack.clear()
        before_previous, previous = previous, text
    return calls


def _add_token(arguments: list[list[str]], kind: _TokenKind, text: str) -> None:
    """Add a token to the last of ``arguments``, or start the next one at a comma."""
    if kind == "other" and text == ",":
        arguments.append([])
    else:
        arguments[-1].append(text)


def _tokens(lines: Sequence[DiffLine]) -> Iterator[tuple[int, _TokenKind, str]]:
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
            # Every alternative of ``_TOKEN`` is a group named in ``_TokenKind``, so one matched.
            kind = cast(_TokenKind, token.lastgroup)
            token_text = token[kind]
            if kind == "triple_quote":
                open_triple_quote = token_text[-3:]
            yield index, kind, token_text
    if open_triple_quote is not None:
        yield len(lines) - 1, "unclosed_string", ""


def _opens_text_without_encoding(arguments: list[list[str]]) -> bool:
    """Whether a call's arguments surely open a file in text mode with no encoding.

    False when unsure: unpacked arguments, a mode that isn't a string literal, or no file.
    """
    if not arguments[-1]:  # no arguments, or a trailing comma
        arguments = arguments[:-1]
    positional: list[list[str]] = []
    keywords: dict[str, list[str]] = {}
    for argument in arguments:
        if not argument or argument[0] in {"*", "**"}:
            return False
        if len(argument) > 1 and argument[1] == "=":
            keywords[argument[0]] = argument[2:]
        else:
            positional.append(argument)
    if not positional and "file" not in keywords:
        return False
    if "encoding" in keywords or len(positional) > _ENCODING_POSITION:
        return False
    mode = positional[_MODE_POSITION] if len(positional) > _MODE_POSITION else keywords.get("mode")
    if mode is None:
        return True
    literal = _MODE_LITERAL.fullmatch(mode[0]) if len(mode) == 1 else None
    return literal is not None and "b" not in literal["mode"]


def _rule_finding(
    path: str, line: int, rule: RuleId, text: str, fingerprinter: Fingerprinter
) -> ReviewedFinding:
    severity, rationale = _SEVERITY_AND_RATIONALE[rule]
    # Not validated, because a diff path may hold lone surrogates, standing for bytes that aren't
    # UTF-8, which the model's schema rejects. Every other field is a constant or a line >= 1.
    finding = Finding.model_construct(
        file=path,
        line=line,
        severity=severity,
        category=Category.RELIABILITY,
        rationale=rationale,
        confidence=1.0,
    )
    fingerprint = fingerprinter.fingerprint(file=path, rule=rule, anchor_text=text)
    return ReviewedFinding(finding, Source.RULE, rule, fingerprint)
