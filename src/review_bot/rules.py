"""Checks that need no model, on the added lines of reviewable Python files.

A hunk is a fragment of a file, so each check reads the lines it shows rather than parsing Python.
A hit counts only if the hunk adds a line it rests on. The built-in rules always run; a domain
rule runs only on the paths a repository enables it for (ADR 0017), and reads each added line
alone.
"""

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from review_bot.diff_parser import DiffLine, FileDiff, Hunk, LineKind, ParsedDiff, glob_matches
from review_bot.findings import Category, Finding, ReviewedFinding, RuleId, Severity, Source
from review_bot.fingerprint import Fingerprinter
from review_bot.python_tokens import TokenKind, line_tokens, tokens_of

__all__ = [
    "DEFAULT_DOMAIN_RULE_SEVERITY",
    "DOMAIN_RULE_IDS",
    "DomainRule",
    "RuleResult",
    "run_rules",
]

_PYTHON_SUFFIX: Final = ".py"
# An ``except`` clause's line, bare or naming exceptions, and whatever follows its colon.
# ``[^:]*`` stops at the first colon, so a clause holding a slice or a ``lambda`` is missed.
_EXCEPT_CLAUSE: Final = re.compile(r"\s*except\b[^:]*:(?P<after_colon>.*)")
# A statement that does nothing, with an optional trailing comment.
_DOES_NOTHING: Final = re.compile(r"(?:pass|\.\.\.)\s*(?:#.*)?")

_CLOSER_OF: Final = {"(": ")", "[": "]", "{": "}"}
# The builtin ``open``, as a call to it is spelled.
_OPEN_NAMES: Final = frozenset({"open", "io.open"})
# Tokens before ``open`` or ``float`` that make it something else: an attribute, or a definition.
_NOT_THE_BUILTIN_AFTER: Final = frozenset({".", "def", "class"})
# A call spread over more lines than this is too unusual to judge from a fragment.
_MAX_CALL_LINES: Final = 20
# ``open(file, mode, buffering, encoding, ...)``: the positions of its parameters.
_MODE_POSITION: Final = 1
_ENCODING_POSITION: Final = 3
# A string literal that can be a mode, such as ``"w"`` or ``'r+b'``.
_MODE_LITERAL: Final = re.compile(r"[rRuU]?(['\"])(?P<mode>[a-zA-Z+]*)\1")

_BUILT_IN_SEVERITY: Final = {
    RuleId.SWALLOWED_EXCEPTION: Severity.MEDIUM,
    RuleId.OPEN_WITHOUT_ENCODING: Severity.LOW,
}
_CATEGORY_AND_RATIONALE: Final = {
    RuleId.SWALLOWED_EXCEPTION: (
        Category.RELIABILITY,
        "Exception swallowed: the handler discards it, so failures pass silently. "
        "Handle it, log it or re-raise it.",
    ),
    RuleId.OPEN_WITHOUT_ENCODING: (
        Category.RELIABILITY,
        "open() without encoding uses the platform default. "
        'Pass encoding="utf-8", or open in binary mode.',
    ),
    RuleId.DECIMAL_MONEY: (
        Category.LOGIC,
        "Money as a binary float: most decimal amounts have no exact float value, so sums and "
        "rounding drift. Use decimal.Decimal, built from a string or an int.",
    ),
}
# The prefixes of a number literal with no decimal point or exponent, though it may hold an "e".
_INTEGER_PREFIXES: Final = ("0x", "0o", "0b")

type _LineTokens = Sequence[tuple[TokenKind, str]]

# A domain rule's severity when its config entry sets none.
DEFAULT_DOMAIN_RULE_SEVERITY: Final = Severity.HIGH


@dataclass(frozen=True, slots=True)
class DomainRule:
    """A domain rule from the catalogue, enabled on the paths matching ``paths``.

    ``paths`` are globs on a file's new path, as ``parse_diff`` matches ``exclude``. The rule's
    findings have ``severity``.

    Raises:
        ValueError: If ``rule`` isn't in the catalogue, ``DOMAIN_RULE_IDS``.
    """

    rule: RuleId
    paths: tuple[str, ...]
    severity: Severity = DEFAULT_DOMAIN_RULE_SEVERITY

    def __post_init__(self) -> None:
        """Check that ``rule`` is a domain rule."""
        if self.rule not in _DOMAIN_CATALOGUE:
            raise ValueError(f"{self.rule} is a built-in rule, not a domain rule")

    @property
    def description(self) -> str:
        """What the rule finds, as fixed text the context prompt gives the model."""
        return _DOMAIN_CATALOGUE[self.rule].description


@dataclass(frozen=True, slots=True)
class RuleResult:
    """The rules' findings, by file and line, and how many lines and hits they couldn't use.

    A hit in a hunk whose header has no readable new start is counted, not reported (ADR 0006).
    ``unchecked_lines`` counts the added lines a domain rule skipped, as they can't be tokenised
    alone.
    """

    findings: tuple[ReviewedFinding, ...]
    unanchored_hits: int
    unchecked_lines: int


@dataclass(frozen=True, slots=True)
class _DomainCheck:
    """A domain rule's fixed description for the model, and whether it finds a hit in a line.

    ``finds`` reads the tokens of one added line, read alone.
    """

    description: str
    finds: Callable[[_LineTokens], bool]


@dataclass(frozen=True, slots=True)
class _Hit:
    """One rule's hit, anchored to a line of the file's new version."""

    path: str
    line: int
    rule: RuleId
    severity: Severity
    text: str


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


def run_rules(parsed: ParsedDiff, *, domain_rules: Sequence[DomainRule] = ()) -> RuleResult:
    """Find swallowed exceptions, text files opened without an encoding, and domain rule hits.

    Only reviewable ``.py`` files are checked, and a domain rule only in the files whose new path
    matches one of its ``paths``.
    """
    anchored: list[_Hit] = []
    unanchored_hits = 0
    unchecked_lines = 0
    for file in parsed.files:
        path = _reviewable_python_path(file)
        if path is None:
            continue
        enabled = _enabled_domain_rules(path, domain_rules)
        for hunk in file.hunks:
            domain_hits, unchecked = _domain_hits(hunk, enabled)
            unchecked_lines += unchecked
            for rule, severity, line in (*_hits(hunk), *domain_hits):
                # A header declaring new start 0 numbers its first line 0, which can't anchor.
                if line.new_line is None or line.new_line < 1:
                    unanchored_hits += 1
                else:
                    anchored.append(_Hit(path, line.new_line, rule, severity, line.text))
    # The one place rule findings are ordered: verify keeps them as given. Sorted before
    # fingerprinting, so ordinals follow that order.
    anchored.sort(key=lambda hit: (hit.path, hit.line))
    fingerprinter = Fingerprinter()
    findings = tuple(_rule_finding(hit, fingerprinter) for hit in anchored)
    return RuleResult(
        findings=findings, unanchored_hits=unanchored_hits, unchecked_lines=unchecked_lines
    )


def _reviewable_python_path(file: FileDiff) -> str | None:
    """The file's new path if the rules check it: not skipped, not deleted, and ending ``.py``."""
    if file.path is None or file.skip_reason is not None:
        return None
    return file.path if file.path.endswith(_PYTHON_SUFFIX) else None


def _hits(hunk: Hunk) -> Iterator[tuple[RuleId, Severity, DiffLine]]:
    """Yield each built-in rule's hits in ``hunk``, with the line each one anchors to."""
    new_version = [line for line in hunk.lines if line.kind is not LineKind.REMOVED]
    for line in _swallowing_except_clauses(new_version):
        rule = RuleId.SWALLOWED_EXCEPTION
        yield rule, _BUILT_IN_SEVERITY[rule], line
    for line in _open_calls_without_encoding(new_version):
        rule = RuleId.OPEN_WITHOUT_ENCODING
        yield rule, _BUILT_IN_SEVERITY[rule], line


def _enabled_domain_rules(path: str, domain_rules: Sequence[DomainRule]) -> dict[RuleId, Severity]:
    """The domain rules enabled on ``path``, each with its severity there.

    A rule that several entries enable on the path takes the most severe of their severities, so
    one finding stands for it.
    """
    enabled: dict[RuleId, Severity] = {}
    for domain_rule in domain_rules:
        if not any(glob_matches(path, pattern) for pattern in domain_rule.paths):
            continue
        current = enabled.get(domain_rule.rule)
        if current is None or domain_rule.severity.rank > current.rank:
            enabled[domain_rule.rule] = domain_rule.severity
    return enabled


def _domain_hits(
    hunk: Hunk, enabled: Mapping[RuleId, Severity]
) -> tuple[list[tuple[RuleId, Severity, DiffLine]], int]:
    """Each ``enabled`` rule's hits on the added lines of ``hunk``, and the lines left unchecked.

    Each added line is tokenised alone, so a line that can't be, such as one opening a string it
    doesn't close, is left unchecked and counted.
    """
    hits: list[tuple[RuleId, Severity, DiffLine]] = []
    unchecked = 0
    if not enabled:
        return hits, unchecked
    for line in hunk.lines:
        if line.kind is not LineKind.ADDED:
            continue
        tokens = line_tokens(line)
        if tokens is None:
            unchecked += 1
            continue
        hits.extend(
            (rule, severity, line)
            for rule, severity in enabled.items()
            if _DOMAIN_CATALOGUE[rule].finds(tokens)
        )
    return hits, unchecked


def _holds_float_money(tokens: _LineTokens) -> bool:
    """Whether a line has a number literal with a decimal point or exponent, or calls ``float``."""
    previous = ""
    for index, (kind, text) in enumerate(tokens):
        if kind == "number" and _is_float_literal(text):
            return True
        is_called = index + 1 < len(tokens) and tokens[index + 1][1] == "("
        if (
            kind == "name"
            and text == "float"
            and is_called
            and previous not in _NOT_THE_BUILTIN_AFTER
        ):
            return True
        previous = text
    return False


def _is_float_literal(number: str) -> bool:
    """Whether a number token has a decimal point or an exponent, so Python reads a float."""
    if number[:2].lower() in _INTEGER_PREFIXES:
        return False
    return any(character in number for character in ".eE")


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
    for index, kind, text in tokens_of(lines):
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


def _add_token(arguments: list[list[str]], kind: TokenKind, text: str) -> None:
    """Add a token to the last of ``arguments``, or start the next one at a comma."""
    if kind == "other" and text == ",":
        arguments.append([])
    else:
        arguments[-1].append(text)


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


def _rule_finding(hit: _Hit, fingerprinter: Fingerprinter) -> ReviewedFinding:
    category, rationale = _CATEGORY_AND_RATIONALE[hit.rule]
    # Not validated, because a diff path may hold lone surrogates, standing for bytes that aren't
    # UTF-8, which the model's schema rejects. Every other field is a constant or a line >= 1.
    finding = Finding.model_construct(
        file=hit.path,
        line=hit.line,
        severity=hit.severity,
        category=category,
        rationale=rationale,
        confidence=1.0,
    )
    fingerprint = fingerprinter.fingerprint(file=hit.path, rule=hit.rule, anchor_text=hit.text)
    return ReviewedFinding(finding, Source.RULE, hit.rule, fingerprint)


# The domain rules a repository may enable (ADR 0017).
_DOMAIN_CATALOGUE: Final[Mapping[RuleId, _DomainCheck]] = {
    RuleId.DECIMAL_MONEY: _DomainCheck(
        description="money held as a binary float, where amounts must be decimal.Decimal: a "
        "number literal with a decimal point or an exponent, or a call to float()",
        finds=_holds_float_money,
    ),
}
DOMAIN_RULE_IDS: Final = frozenset(_DOMAIN_CATALOGUE)
