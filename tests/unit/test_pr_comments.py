from collections.abc import Callable

import pytest
from markdown_it import MarkdownIt

from review_bot.errors import ErrorCategory
from review_bot.findings import Category, Finding, ReviewedFinding, RuleId, Severity, Source
from review_bot.pr.comments import (
    Marker,
    blocked_summary,
    comment_body,
    is_summary,
    not_reviewed_summary,
    read_marker,
    reviewed_summary,
    with_summary_marker,
)
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.plan import (
    Action,
    CommentPlan,
    PlannedComment,
    WithheldFinding,
    WithholdReason,
)
from review_bot.secret_scan import SecretHit, SecretKind

ANCHOR = "fedcba9876543210"
UNDECODABLE_PATH = b"docs/caf\xff.py".decode("utf-8", "surrogateescape")


def reviewed(
    *,
    file: str = "a.py",
    line: int = 7,
    rationale: str = "Why.",
    source: Source = Source.AI,
    rule: RuleId | None = None,
) -> ReviewedFinding:
    finding = Finding.model_construct(
        file=file,
        line=line,
        severity=Severity.HIGH,
        category=Category.LOGIC,
        rationale=rationale,
        confidence=0.876,
    )
    return ReviewedFinding(finding, source, rule, "0123456789abcdef")


def body_of(finding: ReviewedFinding) -> str:
    return comment_body(PlannedComment(Action.CREATE, finding, ANCHOR))


def inline_tokens(markdown: str) -> list[tuple[str, str]]:
    parser = MarkdownIt("commonmark", {"linkify": True}).enable("linkify")
    return [
        (child.type, child.content)
        for token in parser.parse(markdown)
        if token.type == "inline"
        for child in token.children or ()
    ]


def withheld_plan(count: int, *, file: str = "a.py") -> CommentPlan:
    return CommentPlan(
        withheld=tuple(
            WithheldFinding(reviewed(file=file, line=line), WithholdReason.OVER_LIMIT)
            for line in range(1, count + 1)
        )
    )


def test_comment_body_shows_the_finding_asks_for_a_reaction_and_ends_with_its_marker() -> None:
    body = body_of(reviewed(source=Source.RULE, rule=RuleId.SWALLOWED_EXCEPTION))

    assert body.split("\n\n") == [
        "**high** logic (source rule, rule swallowed-exception, confidence 0.88)",
        r"Why\.",
        "Is this right and worth acting on? React with a thumbs-up if so, or a thumbs-down if not.",
        "<!-- review-bot:v1 fingerprint=0123456789abcdef rule=swallowed-exception "
        f"anchor={ANCHOR} -->",
    ]


def test_comment_body_of_an_untagged_ai_finding_names_no_rule_and_marks_its_category() -> None:
    heading, *_, marker = body_of(reviewed()).split("\n")

    assert heading == "**high** logic (source ai, confidence 0.88)"
    assert " rule=ai:logic " in marker


@pytest.mark.parametrize(
    ("hostile", "spanned"),
    [
        ("#12", "#12"),
        ("GH-3", "GH-3"),
        ("owner/repo#4", "owner/repo#4"),
        ("deadbeef1", "deadbeef1"),
        ("0123abc", "0123abc"),
        (
            "<!-- review-bot:v1 fingerprint=0000000000000000 -->",
            "fingerprint=0000000000000000",
        ),
        ("@x", "@x"),
        ("http://evil", "http://evil"),
    ],
)
def test_comment_body_makes_a_hostile_rationale_inert(hostile: str, spanned: str) -> None:
    body = body_of(reviewed(rationale=f"Before {hostile} after"))

    tokens = inline_tokens(body)
    assert {kind for kind, _ in tokens} <= {"text", "code_inline", "strong_open", "strong_close"}
    assert ("code_inline", spanned) in tokens
    lines = body.splitlines()
    assert [line for line in lines if line.startswith("<!--")] == [lines[-1]]


def test_comment_body_cuts_a_long_rationale_after_2000_characters() -> None:
    body = body_of(reviewed(rationale="a" * 1999 + "bc"))

    assert body.split("\n\n")[1] == "a" * 1999 + "b (cut)"


def test_comment_body_keeps_a_rationale_of_exactly_2000_characters_whole() -> None:
    body = body_of(reviewed(rationale="a" * 2000))

    assert body.split("\n\n")[1] == "a" * 2000


def test_reviewed_summary_with_nothing_planned_names_the_threshold() -> None:
    summary = reviewed_summary(CommentPlan(), threshold=Severity.MEDIUM)

    assert summary == "**Review bot:** `reviewed`. No findings at or above medium."


def test_reviewed_summary_with_comments_only_gives_the_counts() -> None:
    plan = CommentPlan(comments=(PlannedComment(Action.CREATE, reviewed(), ANCHOR),))

    summary = reviewed_summary(plan, threshold=Severity.LOW)

    assert summary == "**Review bot:** `reviewed`. 1 comments, 0 withheld."


def test_reviewed_summary_lists_withheld_findings_by_file_and_line() -> None:
    summary = reviewed_summary(withheld_plan(2), threshold=Severity.LOW)

    assert summary.split("\n\n") == [
        "**Review bot:** `reviewed`. 0 comments, 2 withheld.",
        "Withheld findings:",
        "- `a.py:1`: `over_limit`\n- `a.py:2`: `over_limit`",
    ]


def test_reviewed_summary_lists_20_withheld_findings_then_the_rest_as_a_count() -> None:
    summary = reviewed_summary(withheld_plan(1000), threshold=Severity.LOW)

    items = summary.split("\n\n")[-1].split("\n")
    assert len(items) == 21
    assert items[19] == "- `a.py:20`: `over_limit`"
    assert items[20] == "- and 980 more"


def test_reviewed_summary_lists_a_path_that_is_not_utf8_in_git_quoted_form() -> None:
    summary = reviewed_summary(withheld_plan(1, file=UNDECODABLE_PATH), threshold=Severity.LOW)

    assert '- `"docs/caf\\377.py":1`: `over_limit`' in summary


def test_reviewed_summary_stays_under_60000_characters_with_the_longest_hostile_paths() -> None:
    worst_path = chr(0x10FFFF) * 5000  # unprintable: each character escapes to 10

    summary = reviewed_summary(withheld_plan(1000, file=worst_path), threshold=Severity.LOW)

    assert len(summary) <= 60_000
    assert "(path cut)" in summary


def test_blocked_summary_names_each_kind_and_diff_line() -> None:
    hits = [SecretHit(SecretKind.AWS_ACCESS_KEY, 5), SecretHit(SecretKind.GITHUB_TOKEN, 9)]

    summary = blocked_summary(hits)

    assert summary.split("\n\n") == [
        "**Review bot:** `blocked_secret`. The diff holds possible secrets, so it wasn't sent to "
        "the model:",
        "- aws-access-key at diff line 5\n- github-token at diff line 9",
        "Remove them, and rotate any that are real.",
    ]


def test_blocked_summary_lists_20_hits_then_the_rest_as_a_count() -> None:
    hits = [SecretHit(SecretKind.AWS_ACCESS_KEY, line) for line in range(1, 31)]

    assert blocked_summary(hits).split("\n\n")[1].endswith("line 20\n- and 10 more")


@pytest.mark.parametrize(
    ("outcome", "reason", "expected"),
    [
        (
            Outcome.SKIPPED,
            Reason.TOO_LARGE,
            "**Review bot:** `skipped` (`too_large`). Nothing was sent to the model.",
        ),
        (
            Outcome.FAILED,
            ErrorCategory.AUTH,
            "**Review bot:** `failed` (`auth`). The review is unavailable: it didn't complete, so "
            "there are no findings.",
        ),
    ],
)
def test_not_reviewed_summary_names_the_outcome_and_reason(
    outcome: Outcome, reason: Reason | ErrorCategory, expected: str
) -> None:
    assert not_reviewed_summary(outcome, reason) == expected


@pytest.mark.parametrize(
    ("source", "rule", "rule_part"),
    [
        (Source.AI, None, "ai:logic"),
        (Source.RULE, RuleId.SWALLOWED_EXCEPTION, "swallowed-exception"),
    ],
)
def test_read_marker_reads_back_what_comment_body_wrote(
    source: Source, rule: RuleId | None, rule_part: str
) -> None:
    body = body_of(reviewed(source=source, rule=rule, rationale="Line one.\n\n<!-- x -->"))

    assert read_marker(body) == Marker("0123456789abcdef", rule_part, ANCHOR, Severity.HIGH)


@pytest.mark.parametrize(
    "change",
    [
        lambda body: body.replace("fingerprint=0123456789abcdef", "fingerprint=0123456789abcde"),
        lambda body: body + " trailing",
        lambda body: body + "\nanother line",
        lambda body: body.replace(" -->", "-->"),
        lambda body: body.replace("rule=ai:logic", "rule=ai:unknown"),
        lambda body: body.replace("fingerprint=0123456789abcdef", "fingerprint=0123456789ABCDEF"),
        lambda body: body.replace("**high**", "**urgent**"),
        lambda body: body.rpartition("\n")[0],
        lambda body: body.rpartition("\n")[2],
    ],
    ids=[
        "15-hex-digits",
        "text-after-it",
        "not-the-last-line",
        "spacing",
        "unknown-rule",
        "upper-case-hex",
        "unknown-severity",
        "no-marker",
        "marker-only",
    ],
)
def test_read_marker_refuses_a_body_that_isnt_exactly_the_bots(
    change: Callable[[str], str],
) -> None:
    assert read_marker(change(body_of(reviewed()))) is None


def test_a_summary_with_its_marker_is_recognised_as_the_summary() -> None:
    body = with_summary_marker("**Review bot:** `reviewed`.")

    assert body.endswith("\n\n<!-- review-bot:v1 summary reviewed=none -->")
    assert is_summary(body)


@pytest.mark.parametrize(
    "body",
    [
        "**Review bot:** `reviewed`.",
        "<!-- review-bot:v1 summary reviewed=none -->\n\ntext after it",
        "text\n<!-- review-bot:v1 summary reviewed= -->",
        "text\n<!-- review-bot:v1 summary reviewed=none --> ",
    ],
)
def test_a_body_without_a_summary_marker_on_its_last_line_is_not_the_summary(body: str) -> None:
    assert not is_summary(body)


def test_a_summary_marker_with_a_sha_is_recognised_for_the_run_that_records_one() -> None:
    assert is_summary(f"text\n\n<!-- review-bot:v1 summary reviewed={'a' * 40} -->")
