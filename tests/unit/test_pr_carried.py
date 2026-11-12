"""What a gated run carries, rebuilds and pairs (P4-S5), and plan's pairing it relies on."""

from datetime import date

from diff_samples import section

from review_bot.diff_parser import parse_diff
from review_bot.findings import Category, Finding, ReviewedFinding, RuleId, Severity, Source
from review_bot.fingerprint import anchor_for
from review_bot.governance.gate import Blocker, GateClass
from review_bot.pr.carried import (
    FRESH,
    Prior,
    carried_blockers,
    check_pr_diff,
    read_prior,
    standing_comments,
)
from review_bot.pr.comments import MarkerEntry, SummaryMarker, with_summary_marker
from review_bot.pr.github import BOT_LOGIN, BotComment, BotIssueComment, Resolver
from review_bot.pr.plan import pair_fingerprints
from review_bot.rules import DomainRule

SHA = "0123456789abcdef" * 2 + "01234567"
SECURITY = GateClass("ai:security", Severity.HIGH, 0.9, "@owner", date(2026, 12, 1))
MONEY = GateClass("decimal-money", Severity.HIGH, 0.9, "@owner", date(2026, 12, 1))
A, B, C, R = "a" * 16, "b" * 16, "c" * 16, "e" * 16
TRUSTED_BODY = f"Body.\n\n<!-- review-bot:v1 summary reviewed={SHA} gate={A}:ai:security -->"


def comment(  # noqa: PLR0913 - keyword-only, one per field a test varies
    comment_id: int,
    fingerprint: str,
    *,
    rule_part: str = "ai:security",
    resolved_by: Resolver | None = None,
    severity: Severity = Severity.HIGH,
    confidence: float | None = 0.95,
    path: str = "app.py",
    line: int | None = 4,
    anchor: str = "f" * 16,
) -> BotComment:
    return BotComment(
        comment_id=comment_id,
        thread_id=f"T{comment_id}",
        fingerprint=fingerprint,
        rule_part=rule_part,
        anchor=anchor,
        path=path,
        line=line,
        severity=severity,
        resolved_by=resolved_by,
        confidence=confidence,
    )


def carried(
    prior: Prior,
    existing: tuple[BotComment, ...],
    *,
    checks_ran: bool = True,
    resolved: tuple[int, ...] = (),
) -> list[tuple[str, str, str | None]]:
    """What ``prior`` carries, with the comments ``resolved`` resolved by this run."""
    blockers = carried_blockers(
        prior,
        classes=(SECURITY, MONEY),
        standing=standing_comments(existing, resolved),
        resolved_now={c.fingerprint for c in existing if c.comment_id in resolved},
        checks_ran=checks_ran,
    )
    return [(blocker.fingerprint, blocker.gate_class, blocker.file) for blocker in blockers]


def marker_prior(*entries: MarkerEntry) -> Prior:
    return Prior(SummaryMarker(SHA, entries), "Body.")


def issue(body: str, editor: str | None = None, comment_id: int = 50) -> BotIssueComment:
    return BotIssueComment(comment_id, body, editor)


def test_a_summary_the_bot_last_edited_with_a_good_marker_is_trusted() -> None:
    for editor in (None, BOT_LOGIN):
        prior = read_prior((issue(TRUSTED_BODY, editor),), {})
        assert prior == Prior(
            SummaryMarker(SHA, (MarkerEntry(A, "ai:security"),)), "Body.", rebuild=False
        )
        assert prior.reviewed_sha == SHA


def test_a_summary_someone_else_edited_or_malformed_or_tampered_is_rebuilt_from() -> None:
    tampered = with_summary_marker("Body.", reviewed_sha=SHA).replace(" -->", " tampered -->")
    for state in (
        issue(TRUSTED_BODY, "mallory"),
        issue("Body.\n\n<!-- review-bot:v1 summary reviewed=junk -->"),
        issue(tampered, BOT_LOGIN),
    ):
        prior = read_prior((state,), {})
        assert (prior.marker, prior.rebuild, prior.reviewed_sha) == (None, True, None)


def test_no_summary_rebuilds_only_while_the_bot_has_inline_comments() -> None:
    assert read_prior((), {}) == FRESH
    assert read_prior((issue("Coverage is 91%."),), {}) == FRESH
    assert read_prior((), {7: None}) == Prior(rebuild=True)


def test_a_summary_whose_marker_someone_edited_away_is_a_tamper_not_a_deletion() -> None:
    edited_away = issue("All clear.", "mallory", comment_id=40)

    assert read_prior((edited_away,), {}) == Prior(rebuild=True)
    assert read_prior((edited_away, issue(TRUSTED_BODY)), {}) == Prior(rebuild=True)


def test_the_summary_is_the_oldest_bot_issue_comment_with_a_marker() -> None:
    older = issue(TRUSTED_BODY, comment_id=10)
    newer = issue(f"Newer.\n\n<!-- review-bot:v1 summary reviewed={'f' * 40} -->", comment_id=20)

    assert read_prior((older, newer), {}).reviewed_sha == SHA


def test_an_inline_comment_someone_else_edited_marks_the_prior_edited() -> None:
    prior = read_prior((issue(TRUSTED_BODY),), {7: BOT_LOGIN, 8: "mallory"})

    assert prior.marker is not None
    assert prior.edited
    assert prior.is_tampered(checks_ran=True)


def test_a_rebuild_is_tampered_until_the_whole_diff_can_be_checked() -> None:
    assert Prior(rebuild=True).is_tampered(checks_ran=False)
    assert not Prior(rebuild=True).is_tampered(checks_ran=True)
    assert not FRESH.is_tampered(checks_ran=False)


def test_a_trusted_ai_entry_carries_while_it_stands_and_drops_when_this_run_resolves_it() -> None:
    prior = marker_prior(MarkerEntry(A, "ai:security"))

    assert carried(prior, (comment(1, A),)) == [(A, "ai:security", "app.py")]
    assert carried(prior, (comment(1, A, resolved_by=Resolver.PERSON),)) == [
        (A, "ai:security", "app.py")
    ]
    assert carried(prior, (comment(1, A),), resolved=(1,)) == []


def test_a_listed_entry_sharing_an_older_bot_resolved_comments_fingerprint_still_carries() -> None:
    # Push 1 commented F as X; push 2 removed the line and the bot resolved X; push 3 re-added
    # it, withheld over the limit with the same F, so the marker lists F. Push 4 resolves
    # nothing: F must still block.
    prior = marker_prior(MarkerEntry(A, "ai:security"))
    resolved_earlier = comment(1, A, resolved_by=Resolver.BOT)

    assert carried(prior, (resolved_earlier,)) == [(A, "ai:security", None)]


def test_a_trusted_ai_entry_with_no_comment_carries_with_no_place() -> None:
    prior = marker_prior(MarkerEntry(B, "ai:security"))

    blockers = carried_blockers(
        prior, classes=(SECURITY,), standing=(), resolved_now=frozenset(), checks_ran=True
    )

    assert blockers == (Blocker(B, "ai:security", None, None, None, carried=True),)


def test_rule_entries_carry_only_when_the_whole_diff_couldnt_be_checked() -> None:
    prior = marker_prior(MarkerEntry(A, "ai:security"), MarkerEntry(R, "decimal-money"))

    assert carried(prior, (), checks_ran=True) == [(A, "ai:security", None)]
    assert carried(prior, (), checks_ran=False) == [
        (A, "ai:security", None),
        (R, "decimal-money", None),
    ]


def test_entries_of_classes_no_longer_enabled_dont_carry() -> None:
    prior = marker_prior(MarkerEntry(A, "ai:logic"))

    assert carried(prior, ()) == []


def test_a_rebuild_takes_standing_ai_comments_whose_shown_severity_and_confidence_meet() -> None:
    existing = (
        comment(1, A),
        comment(2, B, resolved_by=Resolver.PERSON),
        comment(3, C, resolved_by=Resolver.BOT),
        comment(4, "1" * 16, severity=Severity.MEDIUM),
        comment(5, "2" * 16, confidence=0.85),
        comment(6, "3" * 16, confidence=None),
        comment(7, "4" * 16, rule_part="ai:logic"),
        comment(8, R, rule_part="decimal-money"),
    )

    assert carried(Prior(rebuild=True), existing) == [
        (A, "ai:security", "app.py"),
        (B, "ai:security", "app.py"),
    ]


def test_a_fresh_prior_carries_nothing() -> None:
    assert carried(FRESH, (comment(1, A),)) == []


def rule_finding(line: int, fingerprint: str) -> ReviewedFinding:
    finding = Finding.model_construct(
        file="billing/prices.py",
        line=line,
        severity=Severity.HIGH,
        category=Category.LOGIC,
        rationale="Money.",
        confidence=1.0,
    )
    return ReviewedFinding(finding, Source.RULE, RuleId.DECIMAL_MONEY, fingerprint)


MONEY_DIFF = parse_diff(section("billing/prices.py", "+x = 1\n+total = 9.99\n+total = 9.99\n"))


def money_comment(comment_id: int, fingerprint: str, line: int | None) -> BotComment:
    return comment(
        comment_id,
        fingerprint,
        rule_part="decimal-money",
        path="billing/prices.py",
        line=line,
        anchor=anchor_for("total = 9.99"),
    )


def test_pair_fingerprints_takes_the_paired_comments_fingerprint() -> None:
    # The identical line above was removed, so the finding on line 2 has ordinal 0's
    # fingerprint, while the comment GitHub now shows there holds ordinal 1's.
    findings = (rule_finding(2, "0" * 16),)
    existing = (money_comment(1, "0" * 16, None), money_comment(2, "1" * 16, 2))
    diff = parse_diff(section("billing/prices.py", "+x = 1\n+total = 9.99\n"))

    (paired,) = pair_fingerprints(findings, existing, pr_diff=diff)

    assert paired.fingerprint == "1" * 16


def test_pair_fingerprints_keeps_an_unpaired_findings_own_unless_a_comment_holds_it() -> None:
    findings = (rule_finding(2, "0" * 16), rule_finding(3, "9" * 16))
    existing = (money_comment(1, "9" * 16, 2),)

    named = pair_fingerprints(findings, existing, pr_diff=MONEY_DIFF)

    assert named[0].fingerprint == "9" * 16
    assert named[1].fingerprint not in {"0" * 16, "9" * 16}


def test_check_pr_diff_pairs_rule_findings_with_the_bots_comments() -> None:
    rule = DomainRule(RuleId.DECIMAL_MONEY, ("billing/**",))
    unpaired = check_pr_diff(MONEY_DIFF, (), domain_rules=(rule,), threshold=Severity.LOW)
    first = unpaired.rule_findings[0]

    paired = check_pr_diff(
        MONEY_DIFF,
        (money_comment(1, "7" * 16, first.finding.line),),
        domain_rules=(rule,),
        threshold=Severity.LOW,
    )

    assert paired.rule_findings[0].fingerprint == "7" * 16
    assert len(paired.rule_findings) == 2
