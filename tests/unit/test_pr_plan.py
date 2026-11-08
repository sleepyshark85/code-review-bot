from collections.abc import Sequence

from diff_samples import section

from review_bot.diff_parser import parse_diff
from review_bot.findings import Category, Finding, ReviewedFinding, RuleId, Severity, Source
from review_bot.fingerprint import anchor_for
from review_bot.pr.plan import (
    Action,
    PlannedComment,
    WithheldFinding,
    WithholdReason,
    plan_comments,
)

# Assembled, so no token is written literally.
GITHUB_TOKEN = "gh" + "p_" + "A1b2C3d4" * 4 + "E5f6"
UNDECODABLE_PATH = b"caf\xff.py".decode("utf-8", "surrogateescape")
DIFF = parse_diff(
    section("a.py", "".join(f"+a line {number}\n" for number in range(1, 21)))
    + section("b.py", "+b line 1\n+b line 2\n")
)


def reviewed(
    file: str = "a.py",
    line: int = 1,
    *,
    severity: Severity = Severity.MEDIUM,
    rationale: str = "Why.",
    fingerprint: str = "0123456789abcdef",
) -> ReviewedFinding:
    finding = Finding.model_construct(
        file=file,
        line=line,
        severity=severity,
        category=Category.LOGIC,
        rationale=rationale,
        confidence=0.5,
    )
    return ReviewedFinding(finding, Source.AI, None, fingerprint)


def locations(items: Sequence[PlannedComment | WithheldFinding]) -> list[tuple[str, int]]:
    return [(item.reviewed.finding.file, item.reviewed.finding.line) for item in items]


def test_plan_comments_creates_one_comment_per_finding_with_its_line_anchor() -> None:
    plan = plan_comments([reviewed("b.py", 2)], (), pr_diff=DIFF, max_open_comments=10)

    (comment,) = plan.comments
    assert comment.action is Action.CREATE
    assert comment.anchor == anchor_for("b line 2")
    assert plan.withheld == ()


def test_plan_comments_orders_by_severity_then_file_then_line() -> None:
    findings = [
        reviewed("b.py", 1, severity=Severity.LOW),
        reviewed("b.py", 2, severity=Severity.HIGH),
        reviewed("a.py", 9, severity=Severity.HIGH),
        reviewed("a.py", 3, severity=Severity.HIGH),
        reviewed("a.py", 1, severity=Severity.CRITICAL),
    ]

    plan = plan_comments(findings, (), pr_diff=DIFF, max_open_comments=10)

    assert locations(plan.comments) == [
        ("a.py", 1),
        ("a.py", 3),
        ("a.py", 9),
        ("b.py", 2),
        ("b.py", 1),
    ]


def test_plan_comments_withholds_findings_past_the_limit_in_comment_order() -> None:
    findings = [reviewed("a.py", line) for line in range(12, 0, -1)]

    plan = plan_comments(findings, (), pr_diff=DIFF, max_open_comments=10)

    assert locations(plan.comments) == [("a.py", line) for line in range(1, 11)]
    assert locations(plan.withheld) == [("a.py", 11), ("a.py", 12)]
    assert {item.reason for item in plan.withheld} == {WithholdReason.OVER_LIMIT}


def test_plan_comments_withholds_a_rationale_holding_a_secret_without_counting_it() -> None:
    findings = [reviewed("a.py", 1, rationale=f"Leaks {GITHUB_TOKEN}."), reviewed("a.py", 2)]

    plan = plan_comments(findings, (), pr_diff=DIFF, max_open_comments=1)

    assert locations(plan.comments) == [("a.py", 2)]
    assert [(item.reason, item.reviewed.finding.line) for item in plan.withheld] == [
        (WithholdReason.POSSIBLE_SECRET, 1)
    ]


def test_plan_comments_withholds_a_secret_split_by_markdown_escaping() -> None:
    # Escaped, the token's `_` becomes `\_`, so only the raw rationale shows the token.
    plan = plan_comments([reviewed(rationale=GITHUB_TOKEN)], (), pr_diff=DIFF, max_open_comments=10)

    assert [item.reason for item in plan.withheld] == [WithholdReason.POSSIBLE_SECRET]


def test_plan_comments_withholds_a_path_that_is_not_utf8() -> None:
    diff = parse_diff('diff --git "a/caf\\377.py" "b/caf\\377.py"\n@@ -0,0 +1 @@\n+x\n')
    finding = ReviewedFinding(
        reviewed(UNDECODABLE_PATH).finding, Source.RULE, RuleId.SWALLOWED_EXCEPTION, "f" * 16
    )

    plan = plan_comments([finding], (), pr_diff=diff, max_open_comments=10)

    assert plan.comments == ()
    assert [item.reason for item in plan.withheld] == [WithholdReason.NOT_COMMENTABLE]


def test_plan_comments_withholds_a_line_that_is_not_in_the_pr_diff() -> None:
    findings = [reviewed("a.py", 21), reviewed("c.py", 1)]

    plan = plan_comments(findings, (), pr_diff=DIFF, max_open_comments=10)

    assert plan.comments == ()
    assert {item.reason for item in plan.withheld} == {WithholdReason.NOT_COMMENTABLE}


def test_plan_comments_with_no_findings_is_empty() -> None:
    plan = plan_comments([], (), pr_diff=DIFF, max_open_comments=10)

    assert (plan.comments, plan.withheld) == ((), ())


def test_plan_comments_ignores_a_deleted_file_which_has_no_line_to_comment_on() -> None:
    diff = parse_diff(
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n"
        "+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )

    plan = plan_comments([reviewed("gone.py", 1)], (), pr_diff=diff, max_open_comments=10)

    assert [item.reason for item in plan.withheld] == [WithholdReason.NOT_COMMENTABLE]
