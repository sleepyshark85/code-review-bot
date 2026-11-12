"""The issues a pull request closes, and what a review may send of each (P3-S9)."""

import time

import pytest

from review_bot.context.issues import (
    MAX_ISSUE_BODY_CHARS,
    MAX_ISSUE_TITLE_CHARS,
    MAX_LINKED_ISSUES,
    MAX_LISTED_ISSUES,
    IssueStatus,
    IssueText,
    LinkedIssue,
    linked_issue,
    linked_issue_numbers,
    read_linked_issues,
)
from review_bot.governance.redaction import RedactionKind

REPO = "owner/repo"
# An AWS-format access key ID, built so no scanner flags this file.
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


# linked_issue_numbers


def test_linked_issue_numbers_reads_each_keyword_form_in_order() -> None:
    body = "Fixes #12, closes: #7 and RESOLVED owner/repo#9"

    assert linked_issue_numbers(body, REPO) == (12, 7, 9)


@pytest.mark.parametrize(
    "keyword",
    ["close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved"],
)
def test_linked_issue_numbers_accepts_every_keyword_in_any_case_with_an_optional_colon(
    keyword: str,
) -> None:
    for written in (keyword, keyword.upper(), f"{keyword.title()}:"):
        assert linked_issue_numbers(f"{written} #4", REPO) == (4,)


@pytest.mark.parametrize(
    "body",
    [
        "Refs #5",
        "#5",
        "fixes prefix#5",
        "prefix#5",
        "fixes other/repo#5",
        "fixes #5abc",
        "fixes #0",
        "fixes # 5",
        "prefixes #5",
        "fixes\n\nSee #5",
        "ﬁxes #5",  # a ligature that folds to "fixes" outside ASCII
    ],
)
def test_linked_issue_numbers_ignores_what_isnt_a_closing_reference(body: str) -> None:
    assert linked_issue_numbers(body, REPO) == ()


def test_linked_issue_numbers_matches_the_repository_in_any_case() -> None:
    assert linked_issue_numbers("Fixes Owner/Repo#3.", REPO) == (3,)


def test_linked_issue_numbers_lists_a_repeated_issue_once() -> None:
    assert linked_issue_numbers("fixes #3 and fixes owner/repo#3, fixes #4", REPO) == (3, 4)


def test_linked_issue_numbers_lists_at_most_the_cap() -> None:
    body = " ".join(f"fixes #{number}" for number in range(1, MAX_LISTED_ISSUES + 10))

    assert linked_issue_numbers(body, REPO) == tuple(range(1, MAX_LISTED_ISSUES + 1))


def test_linked_issue_numbers_reads_a_large_hostile_body_quickly() -> None:
    body = "fixes " * 500_000 + "#" * 1_000_000 + "fixes:" * 300_000

    started = time.perf_counter()
    numbers = linked_issue_numbers(body, REPO)

    assert numbers == ()
    assert time.perf_counter() - started < 5


# linked_issue


def test_linked_issue_sends_its_title_and_body_as_they_are() -> None:
    issue = linked_issue(12, IssueText("Export totals", "The CSV must include the discount."))

    assert issue == LinkedIssue(
        12, IssueStatus.INCLUDED, "Export totals\n\nThe CSV must include the discount."
    )
    assert issue.is_sent


def test_linked_issue_at_the_limits_isnt_cut() -> None:
    title, body = "t" * MAX_ISSUE_TITLE_CHARS, "b" * MAX_ISSUE_BODY_CHARS

    assert linked_issue(1, IssueText(title, body)).status is IssueStatus.INCLUDED


def test_linked_issue_cuts_a_long_body_and_marks_it() -> None:
    issue = linked_issue(1, IssueText("Title", "b" * MAX_ISSUE_BODY_CHARS + "TAIL"))

    assert issue.status is IssueStatus.CUT
    assert "TAIL" not in issue.text
    assert issue.text.endswith("[body cut: over 8,000 characters]")


def test_linked_issue_cuts_a_long_title_and_marks_it() -> None:
    issue = linked_issue(1, IssueText("t" * MAX_ISSUE_TITLE_CHARS + "TAIL", "Body"))

    assert issue.status is IssueStatus.CUT
    assert "TAIL" not in issue.text
    assert "[title cut: over 256 characters]" in issue.text


@pytest.mark.parametrize(
    "text", [IssueText(f"Key {AWS_KEY}", "Body"), IssueText("Title", f"key = {AWS_KEY}")]
)
def test_linked_issue_with_a_possible_secret_sends_nothing(text: IssueText) -> None:
    issue = linked_issue(5, text)

    assert issue == LinkedIssue(5, IssueStatus.POSSIBLE_SECRET)
    assert not issue.is_sent


def test_linked_issue_with_a_secret_past_the_cut_still_sends_nothing() -> None:
    body = "b" * MAX_ISSUE_BODY_CHARS + f"\n{AWS_KEY}"

    assert linked_issue(5, IssueText("Title", body)).status is IssueStatus.POSSIBLE_SECRET


def test_linked_issue_not_found_or_a_pull_request_sends_nothing() -> None:
    assert linked_issue(404, None) == LinkedIssue(404, IssueStatus.NOT_FOUND)
    assert linked_issue(8, IssueText("A pull", "", is_pull_request=True)) == LinkedIssue(
        8, IssueStatus.IS_PULL_REQUEST
    )


# read_linked_issues


def test_read_linked_issues_reads_up_to_the_cap_and_lists_the_rest_as_over_limit() -> None:
    read: list[int] = []

    def reader(number: int) -> LinkedIssue:
        read.append(number)
        return LinkedIssue(number, IssueStatus.NOT_FOUND)

    issues = read_linked_issues([12, 7, 9, 4, 5], reader)

    assert read == [12, 7, 9]
    assert len(read) == MAX_LINKED_ISSUES
    assert [(issue.number, issue.status) for issue in issues] == [
        (12, IssueStatus.NOT_FOUND),
        (7, IssueStatus.NOT_FOUND),
        (9, IssueStatus.NOT_FOUND),
        (4, IssueStatus.OVER_LIMIT),
        (5, IssueStatus.OVER_LIMIT),
    ]


def test_linked_issue_refuses_text_for_an_issue_that_isnt_sent() -> None:
    with pytest.raises(ValueError, match="#4 is over_limit"):
        LinkedIssue(4, IssueStatus.OVER_LIMIT, "text")


def test_linked_issue_masks_the_kinds_asked_for_in_its_title_and_body() -> None:
    text = IssueText("Mail jane@rightship.com", "Or call +12025550143 from 10.0.0.1.")

    issue = linked_issue(1, text, frozenset({RedactionKind.EMAIL, RedactionKind.PHONE}))

    assert issue.text == "Mail [REDACTED:email]\n\nOr call [REDACTED:phone] from 10.0.0.1."
    assert issue.redacted == {RedactionKind.EMAIL: 1, RedactionKind.PHONE: 1}


def test_linked_issue_masks_before_the_cut_so_a_value_on_it_is_never_split() -> None:
    address = "jane.doe@rightship.com"
    body = "b" * (MAX_ISSUE_BODY_CHARS - 13) + " " + address + " and more"

    issue = linked_issue(1, IssueText("Title", body), frozenset({RedactionKind.EMAIL}))

    assert issue.status is IssueStatus.CUT
    assert "jane" not in issue.text
    assert "[REDACTED:em" in issue.text
    # The placeholder itself is cut, so no whole masked value is counted as sent.
    assert issue.redacted == {}


def test_linked_issue_counts_only_the_masked_values_its_cut_keeps() -> None:
    body = "a@rightship.com " + "b" * MAX_ISSUE_BODY_CHARS + " c@rightship.com"

    issue = linked_issue(1, IssueText("Title", body), frozenset({RedactionKind.EMAIL}))

    assert issue.text.count("[REDACTED:email]") == 1
    assert issue.redacted == {RedactionKind.EMAIL: 1}


def test_linked_issue_counts_no_more_than_it_masked_when_the_text_held_a_placeholder() -> None:
    body = "[REDACTED:email] was here; ask a@rightship.com"

    issue = linked_issue(1, IssueText("Title", body), frozenset({RedactionKind.EMAIL}))

    assert issue.redacted == {RedactionKind.EMAIL: 1}


def test_linked_issue_with_no_kinds_sends_its_text_as_it_is() -> None:
    issue = linked_issue(1, IssueText("Mail jane@rightship.com", ""))

    assert issue == LinkedIssue(1, IssueStatus.INCLUDED, "Mail jane@rightship.com\n\n")
