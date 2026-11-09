import time
from pathlib import Path

import pytest
from diff_samples import section

from review_bot.diff_parser import parse_diff
from review_bot.findings import Category, RuleId, Severity, Source
from review_bot.fingerprint import fingerprint_for
from review_bot.rules import DOMAIN_RULE_IDS, DomainRule, RuleResult, run_rules

RATIONALE = (
    "Exception swallowed: the handler discards it, so failures pass silently. "
    "Handle it, log it or re-raise it."
)
SAMPLE_DIFF = Path(__file__).parents[2] / "Assignment" / "sample_diff.txt"


def hunk_diff(body: str, *, path: str = "app.py", header: str = "@@ -10,9 +10,9 @@") -> str:
    """One file with one hunk at new line 10, so body line ``n`` (0-based) is line ``10 + n``."""
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{header}\n{body}"


def hit_lines(diff: str) -> list[tuple[str, int]]:
    return hit_lines_of(run_rules(parse_diff(diff)))


def hit_lines_of(result: RuleResult) -> list[tuple[str, int]]:
    return [(item.finding.file, item.finding.line) for item in result.findings]


def test_run_rules_on_the_sample_diff_finds_the_swallowed_exception_at_line_35() -> None:
    result = run_rules(parse_diff(SAMPLE_DIFF.read_text(encoding="utf-8")))

    reviewed, _ = result.findings
    assert reviewed.finding.model_dump() == {
        "file": "billing/invoice.py",
        "line": 35,
        "severity": Severity.MEDIUM,
        "category": Category.RELIABILITY,
        "rationale": RATIONALE,
        "confidence": 1.0,
        "rule": None,
        "evidence": (),
        "suggestion": None,
    }
    assert (reviewed.source, reviewed.rule) == (Source.RULE, RuleId.SWALLOWED_EXCEPTION)
    assert reviewed.fingerprint == fingerprint_for(
        file="billing/invoice.py",
        rule="swallowed-exception",
        anchor_text="except Exception:",
        ordinal=0,
    )
    assert result.unanchored_hits == 0


@pytest.mark.parametrize(
    "body",
    [
        "+try:\n+    x()\n+except Exception:\n+    pass\n",
        "+try:\n+    x()\n+except:\n+    ...\n",
        "+try:\n+    x()\n+except (A, B) as e:\n+    # ignored on purpose\n+\n+    pass\n",
        "+try:\n+    x()\n+except E: pass\n",
        "+try:\n+    x()\n+except E:  # why\n+    pass  # noqa\n",
        "+try:\n+    x()\n+except* E:\n+    pass\n",
    ],
    ids=["pass", "ellipsis", "comment_and_blank_first", "one_line", "comments", "except_star"],
)
def test_run_rules_anchors_a_handler_doing_nothing_at_its_except_line(body: str) -> None:
    assert hit_lines(hunk_diff(body)) == [("app.py", 12)]


@pytest.mark.parametrize(
    "body",
    [
        "+except E:\n+    log(e)\n+    pass\n",
        "+except E:\n+    raise\n",
        "+except E:\n+    return None\n",
        "+except E: log(e)\n",
        "+except E:\n+    passed = True\n",
        "+except E:\n+    pass; log(e)\n",
        "+# except E:\n+    pass\n",
        "+except_handler = 1\n+pass\n",
        "+    raise ValueError('except E:')\n+    pass\n",
    ],
    ids=[
        "log_then_pass",
        "raise",
        "return",
        "one_line_call",
        "pass_prefix",
        "pass_then_call",
        "commented_out",
        "identifier",
        "string",
    ],
)
def test_run_rules_ignores_a_handler_that_does_something_first(body: str) -> None:
    assert hit_lines(hunk_diff(body)) == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (" except E:\n+    pass\n", [("app.py", 10)]),
        ("+except E:\n     pass\n", [("app.py", 10)]),
        (" except E:\n     pass\n", []),
        ("-except E:\n-    pass\n", []),
        (" except E:\n-    log(e)\n+    pass\n", [("app.py", 10)]),
        (" except E:\n-    pass\n+    raise\n", []),
    ],
    ids=[
        "added_pass",
        "added_except",
        "both_context",
        "both_removed",
        "pass_replaces_a_call",
        "raise_replaces_pass",
    ],
)
def test_run_rules_reports_a_handler_only_if_the_clause_or_statement_is_added(
    body: str, expected: list[tuple[str, int]]
) -> None:
    assert hit_lines(hunk_diff(body)) == expected


@pytest.mark.parametrize("body", ["+except E:\n", "+except E:\n+    # nothing yet\n+\n"])
def test_run_rules_ignores_a_handler_whose_body_the_hunk_does_not_show(body: str) -> None:
    assert hit_lines(hunk_diff(body)) == []


def test_run_rules_does_not_read_a_handler_across_hunks() -> None:
    diff = hunk_diff("+except E:\n@@ -20 +20 @@\n+    pass\n")

    assert hit_lines(diff) == []


@pytest.mark.parametrize("path", ["x.js", "x.pyi", "README.md", "pkg/x_pb2.py", "x.py.txt"])
def test_run_rules_checks_only_python_files_that_are_not_skipped(path: str) -> None:
    assert hit_lines(hunk_diff("+except E:\n+    pass\n", path=path)) == []


def test_run_rules_ignores_a_deleted_file() -> None:
    diff = (
        "diff --git a/x.py b/x.py\ndeleted file mode 100644\n--- a/x.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-except E:\n-    pass\n"
    )

    assert hit_lines(diff) == []


@pytest.mark.parametrize(
    "header", ["@@ -10,2 +ten,2 @@", "@@@ -1,2 -1,2 +1,3 @@@", "@@ -1,2 +0,2 @@"]
)
def test_run_rules_counts_a_hit_it_cannot_anchor_and_reports_none(header: str) -> None:
    result = run_rules(parse_diff(hunk_diff("+except E:\n+    pass\n", header=header)))

    assert result.findings == ()
    assert result.unanchored_hits == 1


def test_run_rules_orders_findings_by_file_and_line_and_numbers_identical_handlers() -> None:
    handler = "+except E:\n+    pass\n"
    diff = (
        hunk_diff(handler, path="b.py", header="@@ -1,2 +40,2 @@")
        + hunk_diff(handler, path="a.py", header="@@ -1,2 +30,2 @@")
        + "@@ -1,2 +5,2 @@\n"
        + handler
    )

    result = run_rules(parse_diff(diff))

    assert [(item.finding.file, item.finding.line) for item in result.findings] == [
        ("a.py", 5),
        ("a.py", 30),
        ("b.py", 40),
    ]
    assert [item.fingerprint for item in result.findings] == [
        fingerprint_for(
            file="a.py", rule=RuleId.SWALLOWED_EXCEPTION, anchor_text="except E:", ordinal=0
        ),
        fingerprint_for(
            file="a.py", rule=RuleId.SWALLOWED_EXCEPTION, anchor_text="except E:", ordinal=1
        ),
        fingerprint_for(
            file="b.py", rule=RuleId.SWALLOWED_EXCEPTION, anchor_text="except E:", ordinal=0
        ),
    ]


def test_run_rules_reports_a_python_path_that_is_not_utf8_as_parsed() -> None:
    diff = (
        'diff --git "a/caf\\377.py" "b/caf\\377.py"\n--- "a/caf\\377.py"\n+++ "b/caf\\377.py"\n'
        "@@ -1,2 +1,2 @@\n+except E:\n+    pass\n"
    )

    assert hit_lines(diff) == [("caf\udcff.py", 1)]


def test_run_rules_with_no_python_file_finds_nothing() -> None:
    result = run_rules(parse_diff(section("app.js", "+except E:\n+    pass\n")))

    assert (result.findings, result.unanchored_hits) == ((), 0)


def test_run_rules_reads_a_hunk_of_80000_except_lines_in_linear_time() -> None:
    # Each clause reads only up to its handler's first statement, never the rest of the hunk.
    # Quadratic work took about 24 s here; linear takes about 0.3 s, so the bound is generous.
    parsed = parse_diff(hunk_diff("+except E:\n" * 80_000 + "+    pass\n"))

    started = time.perf_counter()
    result = run_rules(parsed)

    assert time.perf_counter() - started < 3.0
    assert hit_lines_of(result) == [("app.py", 80_009)]


OPEN_RATIONALE = (
    'open() without encoding uses the platform default. Pass encoding="utf-8", or open in binary '
    "mode."
)


def test_run_rules_on_the_sample_diff_finds_the_open_call_at_line_39() -> None:
    result = run_rules(parse_diff(SAMPLE_DIFF.read_text(encoding="utf-8")))

    assert hit_lines_of(result) == [("billing/invoice.py", 35), ("billing/invoice.py", 39)]
    reviewed = result.findings[1]
    assert reviewed.finding.model_dump() == {
        "file": "billing/invoice.py",
        "line": 39,
        "severity": Severity.LOW,
        "category": Category.RELIABILITY,
        "rationale": OPEN_RATIONALE,
        "confidence": 1.0,
        "rule": None,
        "evidence": (),
        "suggestion": None,
    }
    assert (reviewed.source, reviewed.rule) == (Source.RULE, RuleId.OPEN_WITHOUT_ENCODING)
    assert reviewed.fingerprint == fingerprint_for(
        file="billing/invoice.py",
        rule="open-without-encoding",
        anchor_text='with open(path, "w") as f:',
        ordinal=0,
    )


@pytest.mark.parametrize(
    "body",
    [
        "+open(p)\n",
        '+open(p, "w")\n',
        '+open(p, mode="a+")\n',
        "+with open(p) as f:\n",
        "+io.open(p)\n",
        '+f = open(\n+    p,\n+    "w",\n+)\n',
        "+f = open(p, 'rt',)\n",
        '+open(os.path.join(a, "b"), "w")\n',
        "+open(file=p)\n",
        '+open (p, mode = "w", newline="")\n',
        "+return open(p)  # see open(q, encoding=e)\n",
        ' f = open(\n+    p,\n     "w")\n',
        '+f = open(p, \\\n+    "w")\n',
        '+x = """Doc."""; open(p)\n',
    ],
    ids=[
        "no_mode",
        "write",
        "mode_keyword",
        "with",
        "io_open",
        "three_lines",
        "trailing_comma",
        "nested_call",
        "file_keyword",
        "spaces_and_other_keywords",
        "comment_after",
        "one_added_line_of_the_call",
        "backslash_continuation",
        "after_a_closed_docstring",
    ],
)
def test_run_rules_reports_open_in_text_mode_without_encoding_at_the_open_line(body: str) -> None:
    assert hit_lines(hunk_diff(body)) == [("app.py", 10)]


@pytest.mark.parametrize(
    "body",
    [
        '+open(p, encoding="utf-8")\n',
        '+open(p, "w", encoding=enc)\n',
        '+open(p, "rb")\n',
        '+open(p, mode="wb")\n',
        '+open(p, "r+b")\n',
        '+f = open(\n+    p,\n+    encoding="utf-8")\n',
        '+open(p, "w", -1, "utf-8")\n',
        '+f = open(p, "w", \\\n+         encoding="utf-8")\n',
    ],
    ids=[
        "encoding",
        "encoding_variable",
        "binary",
        "binary_keyword",
        "binary_update",
        "multi_line_encoding",
        "positional_encoding",
        "backslash_continuation",
    ],
)
def test_run_rules_ignores_open_with_an_encoding_or_in_binary_mode(body: str) -> None:
    assert hit_lines(hunk_diff(body)) == []


@pytest.mark.parametrize(
    "body",
    [
        "+os.open(p, flags)\n",
        "+self.open(p)\n",
        "+Path(p).open()\n",
        "+reopen(p)\n",
        "+# open(p)\n",
        '+x = "open(p)"\n',
        "+open(p, mode)\n",
        "+f = open(\n+    p,\n",
        "+def open(self, p):\n",
        "+class open(Base):\n",
        "+open(*args)\n",
        "+open(p, **options)\n",
        "+open()\n",
        '+open(p, "r" "b")\n',
        "+open(p, f'{m}')\n",
        '+"""\n+Call open(p) to read.\n+"""\n',
        "+x = 'unclosed open(p)\n",
        "+open(p]\n+)\n",
    ],
    ids=[
        "os_open",
        "method",
        "path_open",
        "other_name",
        "comment",
        "string",
        "variable_mode",
        "unclosed_at_hunk_end",
        "definition",
        "class_definition",
        "unpacked_arguments",
        "unpacked_keywords",
        "no_file",
        "concatenated_mode",
        "f_string_mode",
        "docstring",
        "unclosed_string",
        "mismatched_brackets",
    ],
)
def test_run_rules_stays_silent_on_an_open_call_it_cannot_judge(body: str) -> None:
    assert hit_lines(hunk_diff(body)) == []


@pytest.mark.parametrize("body", [" open(p)\n", "-open(p)\n", "-open(p)\n+open(p, 'rb')\n"])
def test_run_rules_ignores_an_open_call_that_is_not_added(body: str) -> None:
    assert hit_lines(hunk_diff(body)) == []


@pytest.mark.parametrize("path", ["x.js", "x.pyi", "pkg/x_pb2.py"])
def test_run_rules_checks_open_only_in_python_files_that_are_not_skipped(path: str) -> None:
    assert hit_lines(hunk_diff("+open(p)\n", path=path)) == []


def test_run_rules_judges_an_open_call_spanning_up_to_20_lines_and_no_more() -> None:
    within = "+f = open(\n+    p,\n" + "+    # a note\n" * 17 + "+)\n"
    beyond = "+f = open(\n+    p,\n" + "+    # a note\n" * 18 + "+)\n"

    assert hit_lines(hunk_diff(within, header="@@ -9,0 +10,20 @@")) == [("app.py", 10)]
    assert hit_lines(hunk_diff(beyond, header="@@ -9,0 +10,21 @@")) == []


def test_run_rules_reports_only_the_open_call_without_encoding_on_a_line() -> None:
    body = '+a, b = open(p, encoding="utf-8"), open(q)\n'

    assert hit_lines(hunk_diff(body)) == [("app.py", 10)]


def test_run_rules_reports_two_open_calls_on_one_line_with_different_fingerprints() -> None:
    result = run_rules(parse_diff(hunk_diff("+a, b = open(p), open(q)\n")))

    first, second = result.findings
    assert (first.finding.line, second.finding.line) == (10, 10)
    assert first.fingerprint != second.fingerprint


def test_run_rules_counts_an_open_call_it_cannot_anchor() -> None:
    result = run_rules(parse_diff(hunk_diff("+open(p)\n", header="@@ -10,2 +ten,2 @@")))

    assert (result.findings, result.unanchored_hits) == ((), 1)


@pytest.mark.parametrize(
    "body",
    [
        ' Use it like this:\n+    open(p)\n """\n+x = 1\n',
        '+open(p)\n+x = """\n',
        '+open(p)\n+x = """a"""\n+y = """\n',
    ],
    ids=["started_inside_a_docstring", "ends_inside_a_docstring", "odd_quote_count"],
)
def test_run_rules_reports_no_open_call_in_a_hunk_ending_inside_a_triple_quoted_string(
    body: str,
) -> None:
    assert hit_lines(hunk_diff(body)) == []


def test_run_rules_does_not_count_open_calls_dropped_for_an_unclosed_string() -> None:
    body = ' Use it like this:\n+    open(p)\n """\n'

    result = run_rules(parse_diff(hunk_diff(body, header="@@ -10,2 +ten,3 @@")))

    assert (result.findings, result.unanchored_hits) == ((), 0)


@pytest.mark.parametrize(
    ("body", "hit_count"),
    [
        ("+open(p)\n" * 20_000, 20_000),
        ("+open(p,\n" * 80_000, 0),
        ("+" + "open(" * 80_000 + "\n", 0),
        ("+open(\n" + "+    p,\n" * 80_000 + "+)\n", 0),
        ("+" + "([" * 80_000 + "\n", 0),
        ('+"""\n' * 80_000, 0),
        ("+" + "open(p), " * 20_000 + "\n", 20_000),
    ],
    ids=[
        "closed_calls",
        "unclosed_calls",
        "nested_on_one_line",
        "long_call",
        "brackets",
        "quotes",
        "many_calls_on_one_line",
    ],
)
def test_run_rules_reads_hostile_open_calls_in_linear_time(body: str, hit_count: int) -> None:
    # One pass over the tokens: each call is judged when it closes, never by rereading the hunk,
    # and a line's anchor hash is worked out once, however many calls it holds. Linear work takes
    # under 2 s here, so the 5 s bound is generous; quadratic work took about 40 s on many calls
    # on one line.
    parsed = parse_diff(hunk_diff(body))

    started = time.perf_counter()
    result = run_rules(parsed)

    assert time.perf_counter() - started < 5.0
    assert len(result.findings) == hit_count


MONEY = DomainRule(RuleId.DECIMAL_MONEY, ("billing/**",))
MONEY_RATIONALE = (
    "Money as a binary float: most decimal amounts have no exact float value, so sums and "
    "rounding drift. Use decimal.Decimal, built from a string or an int."
)


def money_hits(
    body: str, *, path: str = "billing/invoice.py", rules: tuple[DomainRule, ...] = (MONEY,)
) -> list[int]:
    result = run_rules(parse_diff(hunk_diff(body, path=path)), domain_rules=rules)
    return [item.finding.line for item in result.findings if item.rule is RuleId.DECIMAL_MONEY]


@pytest.mark.parametrize(
    "line",
    [
        "price = 19.99",
        "rate = .5",
        "big = 1e3",
        "small = 2E-4",
        "whole = 10.",
        "grouped = 1_000.50",
        "amount = float(raw)",
        "amount = float (raw)",
        "    total = round(price * 1.2, 2)  # with tax",
    ],
)
def test_run_rules_reports_float_money_on_an_added_line_of_a_matched_file(line: str) -> None:
    assert money_hits(f"+{line}\n") == [10]


@pytest.mark.parametrize(
    "line",
    [
        'tax = Decimal("0.2")',
        "count = 3",
        "# rate 0.5",
        'label = "0.5"',
        "z = 2j",
        "mask = 0x1e3",
        "flags = 0b101",
        "value = obj.float(raw)",
        "def float(raw):",
        "cast = float",
        "name_1e3 = 1",
        "x = data['1.5']",
    ],
)
def test_run_rules_leaves_a_line_without_float_money_alone(line: str) -> None:
    assert money_hits(f"+{line}\n") == []


def test_run_rules_gives_a_domain_finding_its_fixed_fields_and_rule_fingerprint() -> None:
    result = run_rules(
        parse_diff(hunk_diff("+price = 19.99\n", path="billing/invoice.py")),
        domain_rules=(MONEY,),
    )

    [reviewed] = result.findings
    assert reviewed.finding.model_dump() == {
        "file": "billing/invoice.py",
        "line": 10,
        "severity": Severity.HIGH,
        "category": Category.LOGIC,
        "rationale": MONEY_RATIONALE,
        "confidence": 1.0,
        "rule": None,
        "evidence": (),
        "suggestion": None,
    }
    assert (reviewed.source, reviewed.rule) == (Source.RULE, RuleId.DECIMAL_MONEY)
    assert reviewed.fingerprint == fingerprint_for(
        file="billing/invoice.py", rule="decimal-money", anchor_text="price = 19.99", ordinal=0
    )


def test_run_rules_reports_one_domain_finding_per_line_however_many_hits_it_holds() -> None:
    assert money_hits("+price = float(raw) * 1.5 + 2e3\n") == [10]


def test_run_rules_checks_only_added_lines_for_a_domain_rule() -> None:
    assert money_hits(" a = 1.5\n-b = 2.5\n+c = 3.5\n") == [11]


@pytest.mark.parametrize("path", ["api/views.py", "billing/rates.json", "src/billing/invoice.py"])
def test_run_rules_skips_a_file_the_domain_rule_isnt_enabled_on(path: str) -> None:
    assert money_hits("+price = 19.99\n", path=path) == []


def test_run_rules_without_domain_rules_reports_no_float_money() -> None:
    assert money_hits("+price = 19.99\n", rules=()) == []


def test_run_rules_gives_a_domain_finding_its_entrys_severity() -> None:
    medium = DomainRule(RuleId.DECIMAL_MONEY, ("billing/**",), Severity.MEDIUM)

    result = run_rules(
        parse_diff(hunk_diff("+price = 19.99\n", path="billing/a.py")), domain_rules=(medium,)
    )

    assert [item.finding.severity for item in result.findings] == [Severity.MEDIUM]


def test_run_rules_reports_a_rule_enabled_twice_on_a_file_once_at_its_most_severe() -> None:
    rules = (
        DomainRule(RuleId.DECIMAL_MONEY, ("*.py",), Severity.LOW),
        DomainRule(RuleId.DECIMAL_MONEY, ("billing/**",), Severity.CRITICAL),
        DomainRule(RuleId.DECIMAL_MONEY, ("billing/*.py",), Severity.MEDIUM),
    )

    result = run_rules(
        parse_diff(hunk_diff("+price = 19.99\n", path="billing/a.py")), domain_rules=rules
    )

    assert [item.finding.severity for item in result.findings] == [Severity.CRITICAL]


@pytest.mark.parametrize(
    "line", ['s = """', "s = '''abc", "s = 'abc", 's = "abc\\"', "s = r'abc\\'"]
)
def test_run_rules_skips_and_counts_an_added_line_that_ends_inside_a_string(line: str) -> None:
    result = run_rules(
        parse_diff(hunk_diff(f"+{line} 1.5\n+price = 19.99\n", path="billing/a.py")),
        domain_rules=(MONEY,),
    )

    assert [item.finding.line for item in result.findings] == [11]
    assert result.unchecked_lines == 1


@pytest.mark.parametrize("line", ['s = """1.5"""', "s = 'a\\\\'", 's = ""'])
def test_run_rules_checks_a_line_whose_strings_all_close(line: str) -> None:
    result = run_rules(
        parse_diff(hunk_diff(f"+{line}\n", path="billing/a.py")), domain_rules=(MONEY,)
    )

    assert (result.findings, result.unchecked_lines) == ((), 0)


def test_run_rules_tokenises_no_line_where_no_domain_rule_is_enabled() -> None:
    result = run_rules(parse_diff(hunk_diff('+s = """\n', path="billing/a.py")))

    assert result.unchecked_lines == 0


def test_run_rules_counts_a_domain_hit_it_cant_anchor() -> None:
    diff = hunk_diff("+price = 19.99\n", path="billing/a.py", header="@@ -10,2 +ten,2 @@")

    result = run_rules(parse_diff(diff), domain_rules=(MONEY,))

    assert (result.findings, result.unanchored_hits) == ((), 1)


def test_run_rules_orders_domain_and_built_in_findings_by_file_and_line() -> None:
    body = "+price = 19.99\n+try:\n+    x()\n+except E:\n+    pass\n+rate = .5\n"

    result = run_rules(parse_diff(hunk_diff(body, path="billing/a.py")), domain_rules=(MONEY,))

    assert [(item.finding.line, item.rule) for item in result.findings] == [
        (10, RuleId.DECIMAL_MONEY),
        (13, RuleId.SWALLOWED_EXCEPTION),
        (15, RuleId.DECIMAL_MONEY),
    ]


@pytest.mark.parametrize(
    "line",
    ["x = " + "1.5 + " * 100_000 + "1", "s = '" + "\\'" * 200_000, "x = " + "(" * 200_000],
    ids=["many_numbers", "escaped_quotes", "brackets"],
)
def test_run_rules_tokenises_a_long_added_line_in_linear_time(line: str) -> None:
    # Python's own tokenize copies the line for every token, which took minutes and gigabytes on
    # lines like these.
    parsed = parse_diff(hunk_diff(f"+{line}\n", path="billing/a.py"))

    started = time.perf_counter()
    run_rules(parsed, domain_rules=(MONEY,))

    assert time.perf_counter() - started < 5.0


def test_domain_rule_rejects_a_built_in_rule() -> None:
    with pytest.raises(ValueError, match="not a domain rule"):
        DomainRule(RuleId.SWALLOWED_EXCEPTION, ("*.py",))


def test_domain_rule_ids_are_the_catalogue() -> None:
    assert {RuleId.DECIMAL_MONEY} == DOMAIN_RULE_IDS
    assert "decimal.Decimal" in MONEY.description
