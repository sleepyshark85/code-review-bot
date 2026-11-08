"""``plan_comments`` against the bot's existing comments: one test per comment-state rule."""

import dataclasses

import pytest
from diff_samples import section

from review_bot.diff_parser import parse_diff
from review_bot.findings import Category, Finding, ReviewedFinding, Severity, Source
from review_bot.fingerprint import anchor_for, fingerprint_for
from review_bot.pr.comments import comment_body
from review_bot.pr.github import BotComment, Resolver
from review_bot.pr.plan import (
    Action,
    CommentPlan,
    PlannedComment,
    WithholdReason,
    plan_comments,
)

# Lines 1 and 3 are identical, as when a line is added above a commented twin.
TWIN = parse_diff(section("t.py", "+dup()\n+other()\n+dup()\n"))
# Assembled, so no token is written literally.
GITHUB_TOKEN = "gh" + "p_" + "A1b2C3d4" * 4 + "E5f6"


def finding(
    line: int,
    *,
    file: str = "t.py",
    fingerprint: str = "f0f0f0f0f0f0f0f0",
    severity: Severity = Severity.MEDIUM,
    rationale: str = "Why.",
) -> ReviewedFinding:
    model = Finding.model_construct(
        file=file,
        line=line,
        severity=severity,
        category=Category.LOGIC,
        rationale=rationale,
        confidence=0.5,
    )
    return ReviewedFinding(model, Source.AI, None, fingerprint)


def comment(  # noqa: PLR0913 - keyword-only, each a field a test may set
    comment_id: int,
    *,
    line: int | None,
    text: str = "dup()",
    path: str = "t.py",
    fingerprint: str = "c0c0c0c0c0c0c0c0",
    severity: Severity = Severity.MEDIUM,
    resolved_by: Resolver | None = None,
) -> BotComment:
    return BotComment(
        comment_id=comment_id,
        thread_id=f"thread-{comment_id}",
        fingerprint=fingerprint,
        rule_part="ai:logic",
        anchor=anchor_for(text),
        path=path,
        line=line,
        severity=severity,
        resolved_by=resolved_by,
    )


def plan(
    findings: list[ReviewedFinding],
    existing: list[BotComment],
    *,
    diff: str | None = None,
    limit: int = 10,
) -> CommentPlan:
    pr_diff = TWIN if diff is None else parse_diff(diff)
    return plan_comments(findings, existing, pr_diff=pr_diff, max_open_comments=limit)


def actions(result: CommentPlan) -> list[tuple[Action, int, int | None]]:
    """Each planned comment's action, line and paired comment ID."""
    return [
        (
            item.action,
            item.reviewed.finding.line,
            None if item.paired is None else item.paired.comment_id,
        )
        for item in result.comments
    ]


def resolved_ids(result: CommentPlan) -> list[int]:
    return [item.comment_id for item in result.resolved]


# Paired comments: keep, update, move, or nothing for a person's resolution.


def test_a_finding_found_again_on_its_comments_line_keeps_it_with_its_posted_fingerprint() -> None:
    result = plan([finding(3, fingerprint="1111111111111111")], [comment(1, line=3)])

    assert actions(result) == [(Action.KEEP, 3, 1)]
    assert result.comments[0].reviewed.fingerprint == "c0c0c0c0c0c0c0c0"
    assert (result.withheld, result.resolved) == ((), ())


def test_a_changed_severity_updates_only_that_comment() -> None:
    result = plan([finding(3, severity=Severity.HIGH)], [comment(1, line=3)])

    assert actions(result) == [(Action.UPDATE, 3, 1)]
    assert result.comments[0].reviewed.finding.severity is Severity.HIGH


def test_an_outdated_paired_comment_moves_to_the_findings_line_with_its_fingerprint() -> None:
    result = plan([finding(3)], [comment(1, line=None)])

    assert actions(result) == [(Action.MOVE, 3, 1)]
    assert result.comments[0].reviewed.fingerprint == "c0c0c0c0c0c0c0c0"
    assert result.resolved == ()


def test_a_moved_comments_body_follows_the_new_rationale_and_keeps_the_posted_fingerprint() -> None:
    reworded = finding(3, fingerprint="1111111111111111", rationale="Reworded reason.")

    (moved,) = plan([reworded], [comment(1, line=None)]).comments

    body = comment_body(moved)
    assert moved.action is Action.MOVE
    assert "Reworded reason" in body
    assert "fingerprint=c0c0c0c0c0c0c0c0 " in body
    assert "1111111111111111" not in body


def test_a_move_goes_to_the_pairing_findings_line_when_the_anchor_is_on_several() -> None:
    result = plan([finding(1)], [comment(1, line=None)])

    assert actions(result) == [(Action.MOVE, 1, 1)]


@pytest.mark.parametrize("line", [3, None], ids=["current", "outdated"])
def test_a_comment_a_person_resolved_is_kept_as_it_is(line: int | None) -> None:
    person = comment(1, line=line, severity=Severity.LOW, resolved_by=Resolver.PERSON)

    result = plan([finding(3)], [person])

    assert actions(result) == [(Action.KEEP, 3, 1)]


def test_a_thread_the_bot_resolved_never_pairs_even_with_the_same_fingerprint() -> None:
    bot_resolved = comment(1, line=3, fingerprint="f0f0f0f0f0f0f0f0", resolved_by=Resolver.BOT)

    result = plan([finding(3)], [bot_resolved])

    assert actions(result) == [(Action.CREATE, 3, None)]
    assert result.resolved == ()


# Pairing order (AC3).


def test_an_identical_line_added_above_leaves_the_comment_on_its_own_line() -> None:
    # The new line 1 takes ordinal 0, so it now has the comment's fingerprint.
    commented = comment(1, line=3, fingerprint="f0f0f0f0f0f0f0f0")
    findings = [
        finding(1, fingerprint="f0f0f0f0f0f0f0f0"),
        finding(3, fingerprint="f1f1f1f1f1f1f1f1"),
    ]

    result = plan(findings, [commented])

    assert actions(result) == [(Action.CREATE, 1, None), (Action.KEEP, 3, 1)]


def test_a_comment_created_on_an_identical_line_takes_the_next_free_fingerprint() -> None:
    first = fingerprint_for(file="t.py", rule="ai:logic", anchor_text="dup()", ordinal=0)
    second = fingerprint_for(file="t.py", rule="ai:logic", anchor_text="dup()", ordinal=1)
    findings = [finding(1, fingerprint=first), finding(3, fingerprint=second)]

    created, kept = plan(findings, [comment(1, line=3, fingerprint=first)]).comments

    assert kept.reviewed.fingerprint == first
    assert created.reviewed.fingerprint == fingerprint_for(
        file="t.py", rule="ai:logic", anchor_text="dup()", ordinal=2
    )


def test_identical_lines_settle_with_no_duplicate_to_resolve() -> None:
    first = fingerprint_for(file="t.py", rule="ai:logic", anchor_text="dup()", ordinal=0)
    second = fingerprint_for(file="t.py", rule="ai:logic", anchor_text="dup()", ordinal=1)
    findings = [finding(1, fingerprint=first), finding(3, fingerprint=second)]
    created, _ = plan(findings, [comment(1, line=3, fingerprint=first)]).comments
    existing = [
        comment(1, line=3, fingerprint=first),
        comment(2, line=1, fingerprint=created.reviewed.fingerprint),
    ]

    result = plan(findings, existing)

    assert actions(result) == [(Action.KEEP, 1, 2), (Action.KEEP, 3, 1)]
    assert result.resolved == ()


def test_a_created_comment_keeps_its_own_fingerprint_when_no_comment_holds_it() -> None:
    bot_resolved = comment(1, line=3, fingerprint="f0f0f0f0f0f0f0f0", resolved_by=Resolver.BOT)

    (created,) = plan([finding(3)], [bot_resolved]).comments

    assert created.reviewed.fingerprint == "f0f0f0f0f0f0f0f0"


@pytest.mark.parametrize("resolved_by", [None, Resolver.PERSON])
def test_a_created_comment_never_takes_a_fingerprint_an_unpaired_comment_holds(
    resolved_by: Resolver | None,
) -> None:
    holder = comment(
        1, line=2, text="other()", fingerprint="f0f0f0f0f0f0f0f0", resolved_by=resolved_by
    )

    (created,) = plan([finding(3)], [holder]).comments

    assert created.action is Action.CREATE
    assert created.reviewed.fingerprint != "f0f0f0f0f0f0f0f0"


def test_the_same_fingerprint_on_the_findings_line_pairs_first() -> None:
    other = comment(1, line=3, fingerprint="1111111111111111")
    same = comment(2, line=3, fingerprint="f0f0f0f0f0f0f0f0")

    result = plan([finding(3)], [other, same])

    assert actions(result) == [(Action.KEEP, 3, 2)]


def test_between_candidates_on_one_line_the_one_with_the_findings_severity_pairs() -> None:
    high = comment(1, line=3, severity=Severity.HIGH)
    medium = comment(2, line=3, fingerprint="d0d0d0d0d0d0d0d0")

    result = plan([finding(3)], [high, medium])

    assert actions(result) == [(Action.KEEP, 3, 2)]


def test_a_nearer_candidate_wins_over_an_equal_severity_one_further_away() -> None:
    far = comment(1, line=1)
    near = comment(2, line=3, fingerprint="d0d0d0d0d0d0d0d0", severity=Severity.HIGH)

    result = plan([finding(3)], [far, near])

    assert actions(result) == [(Action.UPDATE, 3, 2)]
    assert result.resolved == ()


def test_a_current_candidate_wins_over_an_outdated_one() -> None:
    outdated = comment(1, line=None)
    current = comment(2, line=1, fingerprint="d0d0d0d0d0d0d0d0")

    result = plan([finding(3)], [outdated, current])

    assert actions(result) == [(Action.KEEP, 3, 2)]


def test_a_remaining_tie_goes_to_an_open_thread_over_a_person_resolved_one() -> None:
    person = comment(1, line=3, resolved_by=Resolver.PERSON)
    open_one = comment(2, line=3, fingerprint="d0d0d0d0d0d0d0d0")

    result = plan([finding(3)], [person, open_one])

    assert actions(result) == [(Action.KEEP, 3, 2)]


def test_a_remaining_tie_between_open_threads_goes_to_the_oldest_comment() -> None:
    newer = comment(5, line=3, fingerprint="d0d0d0d0d0d0d0d0")
    older = comment(4, line=3)

    result = plan([finding(3)], [newer, older])

    assert actions(result) == [(Action.KEEP, 3, 4)]


def test_pairing_needs_the_same_key() -> None:
    other_line = comment(1, line=2, text="other()")

    result = plan([finding(3)], [other_line])

    assert actions(result) == [(Action.CREATE, 3, None)]


# Unpaired open comments: resolved only when their anchored line is gone (AC4, AC5).


def test_an_unpaired_comment_whose_line_is_unchanged_is_kept() -> None:
    result = plan([], [comment(1, line=2, text="other()")])

    assert (result.comments, result.resolved) == ((), ())


@pytest.mark.parametrize(
    "existing",
    [
        comment(1, line=9, text="other()"),
        comment(1, line=2, text="before_the_edit()"),
        comment(1, line=1, path="gone.py"),
    ],
    ids=["line-deleted", "line-edited", "file-gone"],
)
def test_an_unpaired_comment_whose_anchored_line_is_gone_is_resolved(
    existing: BotComment,
) -> None:
    result = plan([], [existing])

    assert resolved_ids(result) == [1]


def test_an_unpaired_outdated_comment_is_kept_while_its_anchor_is_on_a_line_of_its_file() -> None:
    result = plan([], [comment(1, line=None, text="other()")])

    assert result.resolved == ()


def test_an_unpaired_outdated_comment_is_resolved_once_its_anchor_is_on_no_line() -> None:
    result = plan([], [comment(1, line=None, text="removed()")])

    assert resolved_ids(result) == [1]


def test_a_fixed_issue_below_an_unchanged_anchored_line_resolves_nothing() -> None:
    diff = section("e.py", "+try:\n+    run()\n+except Exception:\n+    log.exception('x')\n")
    swallowed = comment(1, line=3, path="e.py", text="except Exception:")

    result = plan([], [swallowed], diff=diff)

    assert result.resolved == ()


# Renames (AC6).

RENAMED = (
    "diff --git a/old.py b/new.py\nsimilarity index 80%\nrename from old.py\nrename to new.py\n"
    "--- a/old.py\n+++ b/new.py\n@@ -1,3 +1,3 @@\n dup()\n-was()\n+now()\n kept()\n"
)


def test_a_comment_on_a_renamed_file_moves_to_the_new_path_with_its_fingerprint() -> None:
    result = plan([finding(1, file="new.py")], [comment(1, line=None, path="old.py")], diff=RENAMED)

    (moved,) = result.comments
    assert (moved.action, moved.reviewed.finding.file) == (Action.MOVE, "new.py")
    assert moved.reviewed.fingerprint == "c0c0c0c0c0c0c0c0"


def test_a_comment_on_a_renamed_file_not_found_again_is_kept_while_its_anchor_is_there() -> None:
    result = plan([], [comment(1, line=None, path="old.py", text="kept()")], diff=RENAMED)

    assert (result.comments, result.resolved) == ((), ())


# Duplicates (AC8).


def test_of_two_open_comments_with_one_fingerprint_the_outdated_one_is_resolved() -> None:
    outdated = comment(1, line=None)
    current = comment(2, line=3)

    result = plan([], [outdated, current])

    assert resolved_ids(result) == [1]


def test_of_two_current_comments_with_one_fingerprint_the_newer_one_is_resolved() -> None:
    result = plan([], [comment(1, line=1), comment(2, line=3)])

    assert resolved_ids(result) == [2]


def test_a_resolved_duplicate_never_pairs() -> None:
    result = plan([finding(1)], [comment(1, line=3), comment(2, line=1)])

    assert actions(result) == [(Action.KEEP, 1, 1)]
    assert resolved_ids(result) == [2]


# Volume and secrets.

FILE_OF_20 = section("a.py", "".join(f"+line_{number}()\n" for number in range(1, 21)))


def test_creates_stop_at_the_open_comment_limit() -> None:
    existing = [
        comment(
            number, line=number, path="a.py", text=f"line_{number}()", fingerprint=f"{number:016x}"
        )
        for number in range(1, 9)
    ]
    findings = [
        finding(line, file="a.py", fingerprint=f"{line:016x}", severity=Severity.HIGH)
        for line in range(11, 16)
    ]

    result = plan(findings, existing, diff=FILE_OF_20, limit=10)

    assert [item.reviewed.finding.line for item in result.comments] == [11, 12]
    assert [(item.reason, item.reviewed.finding.line) for item in result.withheld] == [
        (WithholdReason.OVER_LIMIT, line) for line in (13, 14, 15)
    ]


def test_comments_resolved_in_the_same_run_free_their_places() -> None:
    gone = comment(1, line=1, path="a.py", text="deleted()")

    result = plan([finding(5, file="a.py")], [gone], diff=FILE_OF_20, limit=1)

    assert actions(result) == [(Action.CREATE, 5, None)]
    assert resolved_ids(result) == [1]


def test_a_finding_holding_a_secret_is_withheld_and_never_pairs() -> None:
    result = plan([finding(3, rationale=f"Leaks {GITHUB_TOKEN}")], [comment(1, line=3)])

    assert result.comments == ()
    assert [item.reason for item in result.withheld] == [WithholdReason.POSSIBLE_SECRET]
    assert result.resolved == ()


def test_withheld_findings_are_in_comment_order_whatever_withheld_them() -> None:
    findings = [
        finding(3, severity=Severity.LOW),
        finding(9),
        finding(1, severity=Severity.HIGH, fingerprint="1111111111111111"),
    ]

    result = plan(findings, [], limit=0)

    assert [(item.reviewed.finding.line, item.reason) for item in result.withheld] == [
        (1, WithholdReason.OVER_LIMIT),
        (9, WithholdReason.NOT_COMMENTABLE),
        (3, WithholdReason.OVER_LIMIT),
    ]


@pytest.mark.parametrize(
    ("action", "paired"),
    [(Action.CREATE, comment(1, line=1)), (Action.KEEP, None)],
    ids=["create-with-a-comment", "keep-without-one"],
)
def test_planned_comment_has_a_paired_comment_exactly_when_it_isnt_new(
    action: Action, paired: BotComment | None
) -> None:
    with pytest.raises(ValueError, match="paired comment exactly"):
        PlannedComment(action, finding(1), "a" * 16, paired)


def test_a_paired_comments_planned_fingerprint_is_the_only_field_changed() -> None:
    original = finding(3, fingerprint="1111111111111111")

    (kept,) = plan([original], [comment(1, line=3)]).comments

    assert kept.reviewed == dataclasses.replace(original, fingerprint="c0c0c0c0c0c0c0c0")


# Incremental runs (P2-S7): ``changed`` is the diff since the last reviewed commit.

RENAME = "diff --git a/a.py b/t.py\nsimilarity index 100%\nrename from a.py\nrename to t.py\n"
UNRELATED = section("elsewhere.py", "+moved_on()\n")
# The diff since the last reviewed commit of a t.py new since then.
ADDED_SINCE = (
    "diff --git a/t.py b/t.py\nnew file mode 100644\n--- /dev/null\n+++ b/t.py\n"
    "@@ -0,0 +1,3 @@\n+dup()\n+other()\n+dup()\n"
)


def changed_removing(text: str, *, path: str = "t.py") -> str:
    """A diff since the last reviewed commit that edits ``text``, on line 1 of ``path``."""
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-{text}\n+x\n"
    )


def plan_since(
    findings: list[ReviewedFinding], existing: list[BotComment], changed: str | None
) -> CommentPlan:
    """Plan against ``TWIN`` in an incremental run, or a full one if ``changed`` is None."""
    return plan_comments(
        findings,
        existing,
        pr_diff=TWIN,
        changed=None if changed is None else parse_diff(changed),
        max_open_comments=10,
    )


@pytest.mark.parametrize("line", [5, None], ids=["at-its-line", "outdated"])
def test_a_gone_comment_whose_line_didnt_change_since_then_is_kept(line: int | None) -> None:
    gone = comment(1, line=line, text="gone()")

    assert resolved_ids(plan_since([], [gone], None)) == [1]
    assert resolved_ids(plan_since([], [gone], UNRELATED)) == []


@pytest.mark.parametrize("line", [5, None], ids=["at-its-line", "outdated"])
def test_a_gone_comment_whose_line_changed_since_then_is_resolved(line: int | None) -> None:
    gone = comment(1, line=line, text="gone()")

    assert resolved_ids(plan_since([], [gone], changed_removing("gone()"))) == [1]


def test_a_comment_whose_line_changed_but_is_still_there_is_kept() -> None:
    # Its anchor's text was edited elsewhere in the file, but stays on the comment's line.
    kept = comment(1, line=2, text="other()")

    assert resolved_ids(plan_since([], [kept], changed_removing("other()"))) == []


def test_a_line_removed_in_another_file_doesnt_count_as_the_comments() -> None:
    gone = comment(1, line=5, text="gone()")

    assert resolved_ids(plan_since([], [gone], changed_removing("gone()", path="u.py"))) == []


def test_a_comments_path_follows_a_rename_since_then() -> None:
    renamed = comment(1, line=2, text="other()", path="a.py")

    assert resolved_ids(plan_since([], [renamed], None)) == [1]
    assert resolved_ids(plan_since([], [renamed], RENAME)) == []


def test_an_outdated_comment_renamed_since_then_moves_to_its_finding() -> None:
    renamed = comment(1, line=None, text="other()", path="a.py", fingerprint="1111111111111111")

    result = plan_since([finding(2)], [renamed], RENAME)

    assert actions(result) == [(Action.MOVE, 2, 1)]
    assert result.comments[0].reviewed.fingerprint == "1111111111111111"


def test_a_finding_at_a_new_ordinal_pairs_with_its_comment_by_key_and_creates_nothing() -> None:
    # Posted at ordinal 1, below its twin; found at ordinal 0 when only its line was reviewed.
    posted = comment(1, line=3, fingerprint="1111111111111111")
    twin = comment(2, line=1, fingerprint="0000000000000000")

    result = plan_since([finding(3, fingerprint="0000000000000000")], [posted, twin], UNRELATED)

    assert actions(result) == [(Action.KEEP, 3, 1)]
    assert result.resolved == ()


def test_a_finding_on_a_line_outside_the_pr_diff_is_withheld_in_an_incremental_run() -> None:
    result = plan_since([finding(9)], [], changed_removing("dup()"))

    assert [(item.reviewed.finding.line, item.reason) for item in result.withheld] == [
        (9, WithholdReason.NOT_COMMENTABLE)
    ]


def test_a_comment_left_on_a_path_renamed_before_then_is_resolved_once_its_line_is_gone() -> None:
    # Posted on a.py, renamed to t.py by an earlier push; now its line is edited in t.py.
    stale = comment(1, line=None, text="bad()", path="a.py")

    assert resolved_ids(plan_since([], [stale], None)) == [1]
    assert resolved_ids(plan_since([], [stale], changed_removing("bad()"))) == [1]


@pytest.mark.parametrize("line", [2, None], ids=["at-its-line", "outdated"])
def test_a_comment_in_a_file_added_since_then_is_resolved_once_its_line_is_gone(
    line: int | None,
) -> None:
    # t.py is new since the last reviewed commit, and the commented line is no longer in it.
    edited = comment(1, line=line, text="bad()")

    assert resolved_ids(plan_since([], [edited], ADDED_SINCE)) == [1]


def test_a_comment_in_a_file_added_since_then_whose_line_is_there_is_kept() -> None:
    kept = comment(1, line=2, text="other()")

    assert resolved_ids(plan_since([], [kept], ADDED_SINCE)) == []


def test_open_after_counts_open_comments_less_resolves_plus_creates() -> None:
    existing = [
        comment(1, line=2, text="other()", fingerprint="1111111111111111"),
        comment(2, line=None, text="gone()", fingerprint="2222222222222222"),
        comment(3, line=None, text="elsewhere()", resolved_by=Resolver.PERSON),
    ]

    result = plan([finding(3, fingerprint="3333333333333333")], existing)

    assert (resolved_ids(result), result.open_after) == ([2], 2)


def test_open_after_is_unchanged_by_a_move() -> None:
    outdated = comment(1, line=None)

    result = plan([finding(3)], [outdated])

    assert (actions(result), result.open_after) == ([(Action.MOVE, 3, 1)], 1)
