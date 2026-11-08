import pytest

from review_bot.context.codeowners import (
    MAX_CODEOWNERS_BYTES,
    NO_CODEOWNERS,
    CodeOwners,
    CodeOwnersStatus,
    owners_for,
    parse_codeowners,
)

SOURCE = f".github/CODEOWNERS@{'a' * 40}"


def parse(text: str) -> CodeOwners:
    return parse_codeowners(text.encode("utf-8"), source=SOURCE)


def test_owners_for_takes_the_last_matching_line() -> None:
    codeowners = parse("* @org/core\n/billing/ @org/billing alice@example.com\n*.md @docs\n")

    assert owners_for(codeowners, "billing/invoice.py") == ("@org/billing", "alice@example.com")
    assert owners_for(codeowners, "api/views.py") == ("@org/core",)
    assert owners_for(codeowners, "billing/README.md") == ("@docs",)


def test_parse_codeowners_reports_its_source_and_rules() -> None:
    codeowners = parse("* @org/core\n/billing/ @org/billing\n")

    assert (codeowners.source, codeowners.status) == (SOURCE, CodeOwnersStatus.FOUND)
    assert (len(codeowners.rules), codeowners.invalid_lines) == (2, 0)


@pytest.mark.parametrize(
    ("pattern", "path", "is_owned"),
    [
        ("/docs/", "docs/a.md", True),
        ("/docs/", "docs/x/y/b.md", True),
        ("/docs/", "src/docs/a.md", False),
        ("/docs/", "docs", False),
        ("logs/", "logs/a.log", True),
        ("logs/", "build/deep/logs/a.log", True),
        ("logs/", "logs", False),
        ("logs/", "catalogs/a.log", False),
        ("apps/**", "apps/a.py", True),
        ("apps/**", "apps/x/y/z.py", True),
        ("apps/**", "apps", False),
        ("apps/**", "src/apps/a.py", False),
        ("**/build", "build", True),
        ("**/build", "a/b/build", True),
        ("**/build", "a/build/out.o", True),
        ("**/build", "a/rebuild", False),
        ("*.js", "a.js", True),
        ("*.js", "src/deep/a.js", True),
        ("*.js", "a.jsx", False),
        ("docs/*", "docs/a.md", True),
        ("docs/*", "docs/build-app/troubleshooting.md", False),
        ("docs/*", "src/docs/a.md", False),
        ("src/app", "src/app/main.py", True),
        ("src/app", "src/app", True),
        ("src/app", "x/src/app/main.py", False),
        ("/README", "README", True),
        ("/README", "docs/README", False),
        ("a?c.py", "abc.py", True),
        ("a?c.py", "ac.py", False),
        ("*", "any/depth/file.txt", True),
        ("a*b*c", "aXXbYYbZZc", True),
        ("a*b*c", "aXXbYY", False),
    ],
)
def test_owners_for_matches_patterns_as_github_documents(
    pattern: str, path: str, *, is_owned: bool
) -> None:
    codeowners = parse(f"{pattern} @owner\n")

    assert owners_for(codeowners, path) == (("@owner",) if is_owned else ())


def test_a_pattern_with_no_owners_unowns_what_it_matches_last() -> None:
    codeowners = parse("/apps/ @octocat\n/apps/github\n")

    assert owners_for(codeowners, "apps/github/a.py") == ()
    assert owners_for(codeowners, "apps/other.py") == ("@octocat",)
    assert codeowners.invalid_lines == 0


@pytest.mark.parametrize(
    "line",
    [
        "!*.md @docs",
        "*.[ch] @c-team",
        "* not-an-owner",
        "* @org/team/extra",
        "* @",
        "* alice@localhost",
        "* @" + "a" * 40,
        "a//b @owner",
        "/ @owner",
    ],
)
def test_parse_codeowners_skips_and_counts_an_invalid_line(line: str) -> None:
    codeowners = parse(f"* @org/core\n{line}\n")

    assert (len(codeowners.rules), codeowners.invalid_lines) == (1, 1)
    assert owners_for(codeowners, "x.md") == ("@org/core",)


def test_parse_codeowners_ignores_comments_and_blank_lines() -> None:
    codeowners = parse("# owners\n\n   \r\n* @org/core # the default\n  # indented\n")

    assert (len(codeowners.rules), codeowners.invalid_lines) == (1, 0)
    assert owners_for(codeowners, "a.py") == ("@org/core",)


def test_parse_codeowners_names_each_owner_once() -> None:
    codeowners = parse("* @a @b @a\n")

    assert owners_for(codeowners, "x") == ("@a", "@b")


def test_parse_codeowners_over_the_limit_is_too_large_with_no_rules() -> None:
    data = b"* @org/core\n" + b"#" * MAX_CODEOWNERS_BYTES

    codeowners = parse_codeowners(data, source=SOURCE)

    assert (codeowners.source, codeowners.status, codeowners.rules) == (
        SOURCE,
        CodeOwnersStatus.TOO_LARGE,
        (),
    )


def test_parse_codeowners_not_utf8_is_invalid_with_no_rules() -> None:
    codeowners = parse_codeowners(b"* @org/core\n\xff\n", source=SOURCE)

    assert (codeowners.status, codeowners.rules) == (CodeOwnersStatus.INVALID, ())


def test_no_codeowners_is_absent_and_owns_nothing() -> None:
    assert (NO_CODEOWNERS.source, NO_CODEOWNERS.status) == (None, CodeOwnersStatus.ABSENT)
    assert owners_for(NO_CODEOWNERS, "a.py") == ()
