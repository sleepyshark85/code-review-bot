from datetime import date

import pytest
from diff_samples import section

from review_bot.diff_parser import parse_diff
from review_bot.findings import Category, Finding, ReviewedFinding, RuleId, Severity, Source
from review_bot.fingerprint import fingerprint_for
from review_bot.governance.gate import (
    NOT_ENABLED,
    NOTHING_FOUND,
    POSSIBLE_SECRET,
    Blocker,
    GateClass,
    GateDecision,
    GateReason,
    WholeDiffChecks,
    check_whole_diff,
    class_name_problem,
    decide_gate,
)
from review_bot.rules import DomainRule

GITHUB_TOKEN = "gh" + "p_" + "a1B2" * 9
MONEY = GateClass("decimal-money", Severity.HIGH, 0.9, "@owner", date(2026, 12, 1))
SECURITY = GateClass("ai:security", Severity.CRITICAL, 0.9, "@owner", date(2026, 12, 1))
SECRETS = GateClass(POSSIBLE_SECRET, Severity.CRITICAL, 0.9, "@owner", date(2026, 12, 1))
MONEY_RULE = DomainRule(RuleId.DECIMAL_MONEY, ("billing/**",))


def model_finding(
    *,
    severity: Severity = Severity.CRITICAL,
    confidence: float = 0.95,
    category: Category = Category.SECURITY,
    rule: RuleId | None = None,
    fingerprint: str = "a" * 16,
) -> ReviewedFinding:
    finding = Finding(
        file="app.py",
        line=4,
        severity=severity,
        category=category,
        rationale="Why.",
        confidence=confidence,
        rule=rule,
    )
    return ReviewedFinding(finding, Source.AI, rule, fingerprint)


def rule_finding(
    severity: Severity = Severity.HIGH, fingerprint: str = "b" * 16
) -> ReviewedFinding:
    finding = Finding.model_construct(
        file="billing/prices.py",
        line=3,
        severity=severity,
        category=Category.LOGIC,
        rationale="Money.",
        confidence=1.0,
    )
    return ReviewedFinding(finding, Source.RULE, RuleId.DECIMAL_MONEY, fingerprint)


def decide(  # noqa: PLR0913 - keyword-only, one per input of decide_gate
    reported: tuple[ReviewedFinding, ...] = (),
    *,
    whole_diff: WholeDiffChecks | None = NOTHING_FOUND,
    carried: tuple[Blocker, ...] = (),
    enabled: tuple[GateClass, ...] = (MONEY, SECURITY, SECRETS),
    judged: bool = True,
    tampered: bool = False,
) -> tuple[GateDecision, GateReason | None, list[str]]:
    result = decide_gate(
        reported,
        whole_diff=whole_diff,
        carried=carried,
        enabled=enabled,
        judged=judged,
        tampered=tampered,
    )
    return result.decision, result.reason, [blocker.fingerprint for blocker in result.blocking]


@pytest.mark.parametrize("name", ["decimal-money", "ai:security", "ai:logic", POSSIBLE_SECRET])
def test_a_class_is_a_domain_rule_an_ai_category_or_possible_secret(name: str) -> None:
    assert class_name_problem(name) is None


@pytest.mark.parametrize(
    ("name", "severity"),
    [("swallowed-exception", "medium"), ("open-without-encoding", "low")],
)
def test_a_rule_that_never_reaches_high_can_never_be_a_class(name: str, severity: str) -> None:
    assert class_name_problem(name) == (
        f"its rule reports only {severity}, below high, so it can never block"
    )


@pytest.mark.parametrize("name", ["ai:style", "secret", "", "AI:security"])
def test_anything_else_isnt_a_class(name: str) -> None:
    assert class_name_problem(name) is not None


def test_a_model_finding_blocks_under_its_ai_class_only_at_its_minimums() -> None:
    assert decide((model_finding(),))[0] is GateDecision.BLOCK
    assert decide((model_finding(severity=Severity.HIGH),))[0] is GateDecision.PASS
    assert decide((model_finding(confidence=0.85),))[0] is GateDecision.PASS


def test_a_model_finding_tagged_with_a_rule_blocks_under_no_class() -> None:
    tagged = model_finding(category=Category.LOGIC, rule=RuleId.DECIMAL_MONEY)
    logic = GateClass("ai:logic", Severity.HIGH, 0.5, "@owner", date(2026, 12, 1))

    assert decide((tagged,), enabled=(MONEY, logic))[0] is GateDecision.PASS


def test_a_rule_finding_blocks_only_from_the_whole_diff_and_at_its_class_minimum() -> None:
    whole = WholeDiffChecks(rule_findings=(rule_finding(),), secrets=())
    below = WholeDiffChecks(rule_findings=(rule_finding(Severity.MEDIUM),), secrets=())

    assert decide(whole_diff=whole) == (GateDecision.BLOCK, None, ["b" * 16])
    assert decide((rule_finding(),))[0] is GateDecision.PASS
    assert decide(whole_diff=below)[0] is GateDecision.PASS


def test_a_class_not_enabled_blocks_nothing_and_none_enabled_is_not_enabled() -> None:
    whole = WholeDiffChecks(rule_findings=(rule_finding(),), secrets=())

    assert decide(whole_diff=whole, enabled=(SECURITY,))[0] is GateDecision.PASS
    assert decide_gate((), whole_diff=whole, carried=(), enabled=(), judged=True) == NOT_ENABLED


def test_secrets_block_only_under_possible_secret() -> None:
    secret = Blocker("c" * 16, POSSIBLE_SECRET, "a.py", 2, Severity.CRITICAL)
    whole = WholeDiffChecks(rule_findings=(), secrets=(secret,))

    assert decide(whole_diff=whole)[2] == ["c" * 16]
    assert decide(whole_diff=whole, enabled=(MONEY,))[0] is GateDecision.PASS


def test_an_unjudged_run_with_no_blocker_is_unjudged_never_pass() -> None:
    assert decide(judged=False) == (GateDecision.UNJUDGED, None, [])
    assert decide(whole_diff=None, judged=False)[0] is GateDecision.UNJUDGED
    assert decide((model_finding(),), judged=False)[0] is GateDecision.BLOCK


def test_a_tamper_blocks_with_its_reason_even_with_no_blocker() -> None:
    assert decide(tampered=True) == (GateDecision.BLOCK, GateReason.TAMPERED, [])


def test_carried_blockers_count_once_and_only_under_enabled_classes() -> None:
    carried = (
        Blocker("a" * 16, "ai:security", None, None, None, carried=True),
        Blocker("d" * 16, "ai:logic", None, None, None, carried=True),
    )

    result = decide_gate(
        (model_finding(),),
        whole_diff=NOTHING_FOUND,
        carried=carried,
        enabled=(SECURITY,),
        judged=True,
    )

    assert [(blocker.fingerprint, blocker.carried) for blocker in result.blocking] == [
        ("a" * 16, False)
    ]


def test_blockers_go_ai_first_then_most_severe_first_with_unknown_severity_last() -> None:
    carried = Blocker("e" * 16, "ai:security", None, None, None, carried=True)
    secret = Blocker("c" * 16, POSSIBLE_SECRET, None, None, Severity.CRITICAL)
    whole = WholeDiffChecks(rule_findings=(rule_finding(),), secrets=(secret,))

    _, _, order = decide((model_finding(),), whole_diff=whole, carried=(carried,))

    assert order == ["a" * 16, "e" * 16, "c" * 16, "b" * 16]


def money_diff(body: str, *, path: str = "billing/prices.py") -> str:
    return section(path, body)


def test_check_whole_diff_finds_rule_findings_at_or_above_the_threshold() -> None:
    parsed = parse_diff(money_diff("+from decimal import Decimal\n+\n+total = 9.99\n"))

    found = check_whole_diff(parsed, domain_rules=(MONEY_RULE,), threshold=Severity.LOW)
    above = check_whole_diff(parsed, domain_rules=(MONEY_RULE,), threshold=Severity.CRITICAL)

    assert [(item.finding.file, item.finding.line) for item in found.rule_findings] == [
        ("billing/prices.py", 3)
    ]
    assert above.rule_findings == ()


def test_check_whole_diff_blocks_each_added_line_holding_a_secret_with_its_place() -> None:
    body = f"+a = 1\n+token = '{GITHUB_TOKEN}'\n+b = 2\n"
    parsed = parse_diff(money_diff(body, path="app.py"))

    secrets = check_whole_diff(parsed, domain_rules=(), threshold=Severity.LOW).secrets

    expected = fingerprint_for(
        file="app.py", rule=POSSIBLE_SECRET, anchor_text=f"token = '{GITHUB_TOKEN}'", ordinal=0
    )
    assert secrets == (Blocker(expected, POSSIBLE_SECRET, "app.py", 2, Severity.CRITICAL),)


def test_check_whole_diff_folds_a_secret_holding_path_into_one_blocker_with_no_place() -> None:
    path = f"keys/{GITHUB_TOKEN}.txt"
    parsed = parse_diff(money_diff(f"+{GITHUB_TOKEN}\n+other\n", path=path))

    secrets = check_whole_diff(parsed, domain_rules=(), threshold=Severity.LOW).secrets

    expected = fingerprint_for(file=path, rule=POSSIBLE_SECRET, anchor_text=path, ordinal=0)
    assert secrets == (Blocker(expected, POSSIBLE_SECRET, None, None, Severity.CRITICAL),)


def test_check_whole_diff_ignores_secrets_the_pr_doesnt_add() -> None:
    removed = (
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        f"@@ -1,2 +1,1 @@\n-{GITHUB_TOKEN}\n {GITHUB_TOKEN}\n"
    )
    renamed_from = (
        f"diff --git a/{GITHUB_TOKEN}.txt b/clean.txt\nsimilarity index 100%\n"
        f"rename from {GITHUB_TOKEN}.txt\nrename to clean.txt\n"
    )

    for diff in (removed, renamed_from):
        parsed = parse_diff(diff)
        assert check_whole_diff(parsed, domain_rules=(), threshold=Severity.LOW).secrets == ()


def test_check_whole_diff_gives_no_line_where_the_hunk_header_has_no_readable_start() -> None:
    diff = f"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ bad @@\n+{GITHUB_TOKEN}\n"

    secrets = check_whole_diff(parse_diff(diff), domain_rules=(), threshold=Severity.LOW).secrets

    assert [(blocker.file, blocker.line) for blocker in secrets] == [("app.py", None)]


def test_check_whole_diff_scans_skipped_files_too() -> None:
    parsed = parse_diff(money_diff(f"+{GITHUB_TOKEN}\n", path="README.md"))

    secrets = check_whole_diff(parsed, domain_rules=(), threshold=Severity.LOW).secrets

    assert [blocker.file for blocker in secrets] == ["README.md"]


def test_a_rule_blocker_in_a_path_holding_a_secret_never_shows_the_path() -> None:
    finding = rule_finding()
    in_secret_path = ReviewedFinding(
        finding.finding.model_copy(update={"file": f"billing/{GITHUB_TOKEN}.py"}),
        Source.RULE,
        RuleId.DECIMAL_MONEY,
        "b" * 16,
    )
    whole = WholeDiffChecks(rule_findings=(in_secret_path,), secrets=())

    result = decide_gate((), whole_diff=whole, carried=(), enabled=(MONEY,), judged=True)

    assert result.blocking == (Blocker("b" * 16, "decimal-money", None, None, Severity.HIGH),)
