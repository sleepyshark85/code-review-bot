"""The summary marker's gate fields and the summary's gate section (P4-S5)."""

import pytest

from review_bot.findings import Severity
from review_bot.governance.gate import (
    NO_CONFIG,
    NOT_ENABLED,
    Blocker,
    GateDecision,
    GateReason,
    GateResult,
)
from review_bot.pr.comments import (
    GATE_HEADING,
    MarkerEntry,
    SummaryMarker,
    body_before_gate,
    gate_summary,
    holds_gate_state,
    is_summary,
    read_reviewed_sha,
    read_summary_marker,
    with_summary_marker,
)

SHA = "0123456789abcdef" * 2 + "01234567"
AI = Blocker("a" * 16, "ai:security", "app.py", 4, Severity.CRITICAL)
MONEY = Blocker("b" * 16, "decimal-money", "billing/prices.py", 3, Severity.HIGH)
PATH_SECRET = Blocker("c" * 16, "possible-secret", None, None, Severity.CRITICAL)
GONE = Blocker("d" * 16, "ai:logic", None, None, None, carried=True)


def gated(
    *blocking: Blocker,
    decision: GateDecision = GateDecision.BLOCK,
    reason: GateReason | None = None,
    rebuilt: bool = False,
) -> GateResult:
    return GateResult(True, decision, reason, ("decimal-money", "ai:security"), blocking, rebuilt)


def marker_line(body: str) -> str:
    return body.rsplit("\n", maxsplit=1)[-1]


def test_a_gate_with_no_blocker_writes_phase_3s_marker() -> None:
    passing = gated(decision=GateDecision.PASS)

    for gate in (None, NOT_ENABLED, NO_CONFIG, passing):
        body = with_summary_marker("Body.", reviewed_sha=SHA, gate=gate)
        assert body == f"Body.\n\n<!-- review-bot:v1 summary reviewed={SHA} -->"


def test_a_gate_marker_lists_its_blockers_in_order_and_reads_back() -> None:
    body = with_summary_marker("Body.", reviewed_sha=SHA, gate=gated(AI, MONEY))

    assert marker_line(body) == (
        f"<!-- review-bot:v1 summary reviewed={SHA} "
        f"gate={'a' * 16}:ai:security,{'b' * 16}:decimal-money -->"
    )
    assert read_summary_marker(body) == SummaryMarker(
        SHA, (MarkerEntry("a" * 16, "ai:security"), MarkerEntry("b" * 16, "decimal-money"))
    )
    assert read_reviewed_sha(body) == SHA


def test_a_gate_marker_lists_25_blockers_then_how_many_it_left_out() -> None:
    blockers = [
        Blocker(f"{index:016x}", "decimal-money", "a.py", index, Severity.HIGH)
        for index in range(1, 28)
    ]

    body = with_summary_marker("Body.", reviewed_sha=None, gate=gated(*blockers))

    marker = read_summary_marker(body)
    assert marker is not None
    assert len(marker.entries) == 25
    assert marker.more == 2
    assert marker_line(body).endswith(" more=2 -->")


def test_a_tampered_gate_says_so_last_even_with_no_blocker() -> None:
    body = with_summary_marker("Body.", reviewed_sha=None, gate=gated(reason=GateReason.TAMPERED))

    assert marker_line(body) == "<!-- review-bot:v1 summary reviewed=none tampered -->"
    assert read_summary_marker(body) == SummaryMarker(None, tampered=True)
    assert holds_gate_state(body)


@pytest.mark.parametrize(
    "line",
    [
        f"<!-- review-bot:v1 summary reviewed={SHA} gate= -->",
        f"<!-- review-bot:v1 summary reviewed={SHA} gate={'a' * 15}:ai:security -->",
        f"<!-- review-bot:v1 summary reviewed={SHA} gate={'a' * 16}:swallowed-exception -->",
        f"<!-- review-bot:v1 summary reviewed={SHA} gate={'a' * 16}:ai:style -->",
        f"<!-- review-bot:v1 summary reviewed={SHA} gate={'b' * 16}:decimal-money,"
        f"{'a' * 16}:ai:security -->",
        f"<!-- review-bot:v1 summary reviewed={SHA} gate={'a' * 16}:ai:security more=1 -->",
        f"<!-- review-bot:v1 summary reviewed={SHA} more=01 -->",
        f"<!-- review-bot:v1 summary reviewed={SHA} tampered gate={'a' * 16}:ai:security -->",
        f"<!-- review-bot:v1 summary reviewed={SHA}  tampered -->",
        f"<!-- review-bot:v1 summary reviewed={SHA} extra -->",
        "<!-- review-bot:v1 summary reviewed=abc -->",
        f"<!-- review-bot:v1 summary reviewed={SHA} gate="
        + ",".join(f"{index:016x}:ai:logic" for index in range(26))
        + " -->",
    ],
    ids=[
        "empty-gate",
        "short-fingerprint",
        "never-blocking-class",
        "unknown-class",
        "ai-not-first",
        "more-without-25",
        "more-leading-zero",
        "out-of-order",
        "double-space",
        "unknown-field",
        "short-sha",
        "26-entries",
    ],
)
def test_any_marker_outside_the_grammar_is_malformed_but_still_the_summary(line: str) -> None:
    body = f"Body.\n\n{line}"

    assert read_summary_marker(body) is None
    assert is_summary(body)


def test_a_marker_over_2000_characters_is_malformed() -> None:
    line = f"<!-- review-bot:v1 summary reviewed={SHA}{' tampered' * 300} -->"
    assert len(line) > 2_000

    assert read_summary_marker(f"Body.\n\n{line}") is None
    assert is_summary(f"Body.\n\n{line}")


def test_a_marker_with_gate_fields_holds_gate_state_and_a_plain_one_doesnt() -> None:
    plain = with_summary_marker("Body.", reviewed_sha=SHA)

    assert not holds_gate_state(plain)
    assert holds_gate_state(f"Body.\n\n<!-- review-bot:v1 summary reviewed={SHA} more=3 -->")
    assert holds_gate_state("Body.\n\n<!-- review-bot:v1 summary reviewed=x gate=junk -->")
    assert not holds_gate_state("Body.")


def test_a_gated_summary_says_it_blocks_lists_each_blocker_and_how_to_appeal() -> None:
    section = gate_summary(gated(AI, MONEY, PATH_SECRET, GONE), would=False, threaded={"a" * 16})

    assert section.split("\n\n")[0] == (
        f"{GATE_HEADING} Blocks the merge: 4 findings "
        "(ai:security, decimal-money, possible-secret, ai:logic)."
    )
    assert "- `app.py:4`: ai:security\n" in section
    assert f"- `billing/prices.py:3`: decimal-money, fingerprint `{'b' * 16}`" in section
    assert (
        f"- a file path holding a possible secret: possible-secret, fingerprint `{'c' * 16}`"
        in (section)
    )
    assert (
        f"- carried from an earlier review, no comment: ai:logic, fingerprint `{'d' * 16}`"
        in section
    )
    assert "`/review-bot override: <reason>`" in section
    assert "`/review-bot override <fingerprint>: <reason>`" in section


def test_a_shadow_summary_says_would_block_with_no_appeal() -> None:
    section = gate_summary(gated(MONEY), would=True, threaded=())

    assert section.startswith(f"{GATE_HEADING} Would block the merge: 1 finding (decimal-money).")
    assert "override" not in section


@pytest.mark.parametrize(
    ("decision", "would", "headline"),
    [
        (
            GateDecision.PASS,
            False,
            "Passes: no finding of a gated class (decimal-money, ai:security).",
        ),
        (GateDecision.PASS, True, "Would pass: no finding of a gated class"),
        (GateDecision.UNJUDGED, False, "`unjudged`: the review didn't complete"),
    ],
)
def test_a_summary_with_no_blocker_says_pass_or_unjudged(
    decision: GateDecision, would: bool, headline: str
) -> None:
    section = gate_summary(gated(decision=decision), would=would, threaded=())

    assert section.startswith(f"{GATE_HEADING} {headline}")


def test_a_tampered_summary_says_a_tamper_occurred_and_a_rebuilt_one_too() -> None:
    tampered = gate_summary(gated(reason=GateReason.TAMPERED), would=False, threaded=())
    rebuilt = gate_summary(
        gated(decision=GateDecision.PASS, rebuilt=True), would=False, threaded=()
    )

    assert tampered.startswith(f"{GATE_HEADING} Blocks the merge (`tampered`).")
    assert "A tamper occurred" in tampered
    assert "A tamper occurred" in rebuilt
    assert "rebuilt from the bot's own inline comments" in rebuilt


def test_a_summary_lists_25_blockers_and_says_how_many_ai_ones_wont_carry() -> None:
    blockers = [
        Blocker(f"{index:016x}", "ai:logic", "a.py", index, Severity.HIGH) for index in range(1, 29)
    ]

    section = gate_summary(gated(*blockers), would=False, threaded=())

    assert section.count("\n- `a.py:") + section.startswith("- `a.py:") == 25
    assert "- and 3 more" in section
    assert "3 `ai:` findings beyond the 25 listed aren't carried" in section


def test_body_before_gate_drops_the_marker_and_the_gate_section() -> None:
    posted = with_summary_marker(
        f"Body.\n\nMore.\n\n{gate_summary(gated(MONEY), would=False, threaded=())}",
        reviewed_sha=SHA,
        gate=gated(MONEY),
    )

    assert body_before_gate(posted) == "Body.\n\nMore."
    assert body_before_gate(with_summary_marker("Body.", reviewed_sha=SHA)) == "Body."
