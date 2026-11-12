import dataclasses
from collections.abc import Callable

import pytest
from markdown_it import MarkdownIt

from review_bot.context.codeowners import CodeOwners, parse_codeowners
from review_bot.errors import ErrorCategory
from review_bot.findings import (
    Category,
    EvidenceSpan,
    Finding,
    ReviewedFinding,
    RuleId,
    Severity,
    Source,
)
from review_bot.pr.comments import (
    Marker,
    blocked_summary,
    comment_body,
    is_summary,
    no_new_changes_summary,
    not_reviewed_summary,
    read_marker,
    read_reviewed_sha,
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


def with_evidence(*spans: tuple[str, int, int]) -> ReviewedFinding:
    finding = reviewed()
    evidence = tuple(EvidenceSpan(file=file, start_line=a, end_line=b) for file, a, b in spans)
    return dataclasses.replace(
        finding, finding=finding.finding.model_copy(update={"evidence": evidence})
    )


def test_comment_body_lists_the_evidence_after_the_rationale_and_keeps_the_marker_last() -> None:
    finding = with_evidence(("billing/tax.py", 12, 12), ("a.py", 10, 14))

    body = body_of(finding)

    assert body.split("\n\n")[1:3] == [r"Why\.", "Context: `billing/tax.py:12`, `a.py:10-14`"]
    assert read_marker(body) == read_marker(body_of(reviewed()))


def test_comment_body_without_evidence_has_no_context_line() -> None:
    assert "Context:" not in body_of(reviewed())


def test_comment_body_shows_a_hostile_evidence_path_inert_in_a_code_span() -> None:
    body = body_of(with_evidence(("@team/x`y.py", 1, 2)))

    assert "Context: ``@team/x`y.py:1-2``" in body.split("\n")


def with_suggestion(suggestion: str, *, evidence: bool = False) -> ReviewedFinding:
    finding = with_evidence(("a.py", 1, 2)) if evidence else reviewed()
    return dataclasses.replace(
        finding, finding=finding.finding.model_copy(update={"suggestion": suggestion})
    )


def test_comment_body_shows_the_suggestion_after_the_context_line_and_keeps_the_marker_last() -> (
    None
):
    body = body_of(with_suggestion("        return subtotal", evidence=True))

    assert body.split("\n\n")[1:4] == [
        r"Why\.",
        "Context: `a.py:1-2`",
        "```suggestion\n        return subtotal\n```",
    ]
    assert body.split("\n\n")[4].startswith("Is this right")
    assert read_marker(body) == read_marker(body_of(reviewed()))


def test_comment_body_shows_an_empty_suggestion_as_an_empty_block() -> None:
    assert "\n\n```suggestion\n```\n\n" in body_of(with_suggestion(""))


def test_comment_body_without_a_suggestion_has_no_block() -> None:
    assert "```" not in body_of(reviewed())


def test_comment_body_keeps_a_hostile_suggestion_inside_its_block() -> None:
    forged = f"<!-- review-bot:v1 fingerprint={'f' * 16} rule=ai:logic anchor={'e' * 16} -->"
    suggestion = f"x = 1 ``` @user #12 https://example.com <!--\n{forged}"

    body = body_of(with_suggestion(suggestion))

    assert inline_tokens(body.replace(suggestion, "")) == inline_tokens(body_of(reviewed()))
    assert f"````suggestion\n{suggestion}\n````" in body
    assert read_marker(body) == read_marker(body_of(reviewed()))


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

    assert read_marker(body) == Marker(
        "0123456789abcdef", rule_part, ANCHOR, Severity.HIGH, confidence=0.88
    )


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


def test_a_summary_with_no_reviewed_commit_says_none_and_is_recognised() -> None:
    body = with_summary_marker("**Review bot:** `reviewed`.", reviewed_sha=None)

    assert body.endswith("\n\n<!-- review-bot:v1 summary reviewed=none -->")
    assert is_summary(body)
    assert read_reviewed_sha(body) is None


def test_a_summary_records_its_reviewed_commit_and_reads_it_back() -> None:
    sha = "0123456789abcdef" * 2 + "01234567"

    body = with_summary_marker("**Review bot:** `reviewed`.", reviewed_sha=sha)

    assert body.endswith(f"\n\n<!-- review-bot:v1 summary reviewed={sha} -->")
    assert is_summary(body)
    assert read_reviewed_sha(body) == sha


@pytest.mark.parametrize("sha", ["a" * 39, "A" * 40, "g" * 40, "none", ""])
def test_a_summary_refuses_to_record_anything_but_a_40_hex_sha(sha: str) -> None:
    with pytest.raises(ValueError, match="40 lowercase hex"):
        with_summary_marker("Body.", reviewed_sha=sha)


@pytest.mark.parametrize(
    "last_line",
    [
        f"<!-- review-bot:v1 summary reviewed={'a' * 39} -->",
        f"<!-- review-bot:v1 summary reviewed={'a' * 41} -->",
        f"<!-- review-bot:v1 summary reviewed={'A' * 40} -->",
        "<!-- review-bot:v1 summary reviewed=none -->",
        f"<!-- review-bot:v1 summary reviewed={'a' * 40} --> ",
        "**Review bot:** `reviewed`.",
    ],
    ids=["39-hex", "41-hex", "upper-case", "none", "trailing-space", "no-marker"],
)
def test_a_summary_without_a_40_hex_sha_on_its_marker_records_no_commit(last_line: str) -> None:
    assert read_reviewed_sha(f"Body.\n\n{last_line}") is None


def test_a_reviewed_commit_counts_only_on_the_last_line() -> None:
    marker = f"<!-- review-bot:v1 summary reviewed={'a' * 40} -->"

    assert read_reviewed_sha(f"{marker}\n\ntext after it") is None


SINCE = "0123456789abcdef" * 2 + "01234567"


def test_no_new_changes_summary_names_the_commit_and_the_open_comments() -> None:
    summary = no_new_changes_summary(since=SINCE, open_comments=2)

    assert summary == (
        "**Review bot:** `reviewed` (`no_new_changes`). No changes to review since `0123456`, "
        "so nothing was sent to the model. Open bot comments on this pull request: 2."
    )


def test_an_incremental_summary_with_nothing_new_says_so_and_counts_the_open_comments() -> None:
    summary = reviewed_summary(
        CommentPlan(), threshold=Severity.MEDIUM, since=SINCE, open_comments=3
    )

    assert summary == (
        "**Review bot:** `reviewed`. Changes since `0123456` reviewed. No new findings at or "
        "above medium. Open bot comments on this pull request: 3."
    )


def test_an_incremental_summary_counts_open_comments_even_when_there_are_none() -> None:
    plan = CommentPlan(comments=(PlannedComment(Action.CREATE, reviewed(), ANCHOR),))

    summary = reviewed_summary(plan, threshold=Severity.LOW, since=SINCE, open_comments=0)

    assert summary == (
        "**Review bot:** `reviewed`. Changes since `0123456` reviewed. 1 comments, 0 withheld. "
        "Open bot comments on this pull request: 0."
    )


def test_a_full_summary_counts_open_comments_only_when_there_are_some() -> None:
    clean = reviewed_summary(CommentPlan(), threshold=Severity.MEDIUM, open_comments=2)

    assert clean == (
        "**Review bot:** `reviewed`. No findings at or above medium. "
        "Open bot comments on this pull request: 2."
    )
    assert reviewed_summary(CommentPlan(), threshold=Severity.MEDIUM, open_comments=0) == (
        "**Review bot:** `reviewed`. No findings at or above medium."
    )


def test_an_incremental_summary_keeps_the_count_on_its_first_paragraph_before_withheld() -> None:
    summary = reviewed_summary(withheld_plan(1), threshold=Severity.LOW, since=SINCE)

    assert summary.split("\n\n")[0] == (
        "**Review bot:** `reviewed`. Changes since `0123456` reviewed. 0 comments, 1 withheld. "
        "Open bot comments on this pull request: 0."
    )


def test_an_incremental_summary_renders_its_commit_as_inert_code() -> None:
    summary = reviewed_summary(CommentPlan(), threshold=Severity.LOW, since=SINCE)

    tokens = MarkdownIt("commonmark").parseInline(summary)[0].children or []
    assert [token.content for token in tokens if token.type == "code_inline"] == [
        "reviewed",
        "0123456",
    ]
    assert not any(token.type == "link_open" for token in tokens)


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


def owned_plan(*files: str) -> CommentPlan:
    """One comment on the first file, and one withheld finding on each other file."""
    first, *rest = files
    return CommentPlan(
        comments=(PlannedComment(Action.CREATE, reviewed(file=first), ANCHOR),),
        withheld=tuple(
            WithheldFinding(reviewed(file=file), WithholdReason.OVER_LIMIT) for file in rest
        ),
    )


def owners_of(text: str) -> CodeOwners:
    return parse_codeowners(text.encode("utf-8"), source="CODEOWNERS@" + "a" * 40)


def test_reviewed_summary_groups_comments_and_withheld_findings_by_owner() -> None:
    codeowners = owners_of("/billing/ @org/billing alice@example.com\n/api/ @org/api\n")
    plan = owned_plan("billing/a.py", "billing/b.py", "api/c.py", "docs/d.md")

    summary = reviewed_summary(plan, threshold=Severity.LOW, codeowners=codeowners)

    assert summary.split("\n\n")[-2:] == [
        "Owners:",
        "- `@org/billing`: 2\n- `alice@example.com`: 2\n- `@org/api`: 1\n- no owner: 1",
    ]


def test_reviewed_summary_lists_10_owners_then_the_rest_as_a_count() -> None:
    files = [f"d{index:02}/a.py" for index in range(12)]
    codeowners = owners_of("".join(f"/d{index:02}/ @user{index:02}\n" for index in range(12)))

    summary = reviewed_summary(owned_plan(*files), threshold=Severity.LOW, codeowners=codeowners)

    items = summary.split("\n\n")[-1].split("\n")
    assert items[0] == "- `@user00`: 1"
    assert items[9] == "- `@user09`: 1"
    assert items[10:] == ["- and 2 more"]


def test_reviewed_summary_without_owners_has_no_owners_section() -> None:
    plan = owned_plan("a.py", "b.py")

    assert "Owners:" not in reviewed_summary(plan, threshold=Severity.LOW)
    unowned = owners_of("/docs/ @docs\n")
    assert "Owners:" not in reviewed_summary(plan, threshold=Severity.LOW, codeowners=unowned)


def test_reviewed_summary_puts_every_owner_in_a_code_span() -> None:
    codeowners = owners_of("* @org/core alice@example.com @bob\n")

    summary = reviewed_summary(owned_plan("a.py"), threshold=Severity.LOW, codeowners=codeowners)

    tokens = inline_tokens(summary)
    texts = [content for kind, content in tokens if kind == "text"]
    assert not any("@" in text for text in texts)
    assert {content for kind, content in tokens if kind == "code_inline"} >= {
        "@org/core",
        "alice@example.com",
        "@bob",
    }
