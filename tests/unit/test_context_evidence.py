import time
from collections.abc import Iterable, Mapping

import pytest

from review_bot.context.evidence import EvidenceCheck, ShownLines, verify_evidence
from review_bot.context.ledger import LineSpan, RefusalReason, ToolCall, ToolName, ToolOutcome
from review_bot.findings import EvidenceSpan

CHECKOUT = "billing/checkout.py"


def call(
    *spans: LineSpan,
    tool: ToolName = ToolName.READ,
    outcome: ToolOutcome = ToolOutcome.OK,
    reason: RefusalReason | None = None,
) -> ToolCall:
    return ToolCall(tool, outcome, reason, spans, (), 10, "0" * 64)


def span(start: int, end: int, file: str = CHECKOUT) -> EvidenceSpan:
    return EvidenceSpan(file=file, start_line=start, end_line=end)


def check(
    evidence: list[EvidenceSpan],
    *ledger: ToolCall,
    diff_lines: Mapping[str, Iterable[int]] | None = None,
    is_diff_head: bool = False,
) -> EvidenceCheck:
    shown = ShownLines(diff_lines or {}, ledger, is_diff_head=is_diff_head)
    return verify_evidence(evidence, shown)


def test_verify_evidence_keeps_a_span_inside_a_read() -> None:
    result = check([span(12, 12)], call(LineSpan(CHECKOUT, 1, 30)))

    assert result == EvidenceCheck(kept=(span(12, 12),), dropped=0)


def test_verify_evidence_keeps_a_grep_matchs_line_but_not_its_neighbours() -> None:
    grep = call(LineSpan(CHECKOUT, 12, 12), tool=ToolName.GREP)

    result = check([span(12, 12), span(11, 13)], grep)

    assert result == EvidenceCheck(kept=(span(12, 12),), dropped=1)


@pytest.mark.parametrize(
    "cited",
    [
        span(1, 1, file="billing/other.py"),
        span(25, 31),
        span(31, 31),
        span(1, 1, file="./billing/checkout.py"),
        span(1, 1, file="Billing/Checkout.py"),
    ],
)
def test_verify_evidence_drops_a_span_of_another_file_or_past_the_lines_returned(
    cited: EvidenceSpan,
) -> None:
    result = check([cited], call(LineSpan(CHECKOUT, 1, 30)))

    assert result == EvidenceCheck(kept=(), dropped=1)


def test_verify_evidence_counts_only_the_lines_a_truncated_read_returned() -> None:
    truncated = call(LineSpan(CHECKOUT, 1, 5), outcome=ToolOutcome.TRUNCATED)

    result = check([span(5, 5), span(6, 6)], truncated)

    assert result == EvidenceCheck(kept=(span(5, 5),), dropped=1)


def test_verify_evidence_drops_a_span_of_a_refused_calls_file_even_if_it_carries_spans() -> None:
    refused = call(
        LineSpan(CHECKOUT, 1, 30), outcome=ToolOutcome.REFUSED, reason=RefusalReason.TOO_LARGE
    )

    assert check([span(1, 1)], refused) == EvidenceCheck(kept=(), dropped=1)


def test_verify_evidence_does_not_count_a_globbed_path_as_shown_lines() -> None:
    glob = ToolCall(ToolName.GLOB, ToolOutcome.OK, None, (), (CHECKOUT,), 10, "0" * 64)

    assert check([span(1, 1)], glob) == EvidenceCheck(kept=(), dropped=1)


def test_verify_evidence_keeps_a_span_across_touching_results() -> None:
    reads = (call(LineSpan(CHECKOUT, 1, 10)), call(LineSpan(CHECKOUT, 11, 20)))

    assert check([span(8, 14)], *reads).kept == (span(8, 14),)


def test_verify_evidence_drops_a_span_across_a_gap_between_results() -> None:
    reads = (call(LineSpan(CHECKOUT, 1, 10)), call(LineSpan(CHECKOUT, 12, 20)))

    assert check([span(8, 14)], *reads).dropped == 1


def test_verify_evidence_keeps_the_kept_spans_in_the_order_cited() -> None:
    cited = [span(20, 20), span(99, 99), span(3, 4), span(20, 20)]

    result = check(cited, call(LineSpan(CHECKOUT, 1, 30)))

    assert result == EvidenceCheck(kept=(span(20, 20), span(3, 4), span(20, 20)), dropped=1)


def test_verify_evidence_of_no_spans_keeps_and_drops_nothing() -> None:
    assert check([], call(LineSpan(CHECKOUT, 1, 30))) == EvidenceCheck(kept=(), dropped=0)


def test_verify_evidence_keeps_a_diff_line_with_no_tool_call() -> None:
    result = check([span(3, 4), span(5, 5)], diff_lines={CHECKOUT: [3, 4]})

    assert result == EvidenceCheck(kept=(span(3, 4),), dropped=1)


@pytest.mark.parametrize("is_diff_head", [False, True])
def test_verify_evidence_counts_tool_lines_of_a_file_the_diff_doesnt_change(
    is_diff_head: bool,
) -> None:
    read = call(LineSpan("billing/tax.py", 1, 30))

    result = check(
        [span(5, 5, file="billing/tax.py")],
        read,
        diff_lines={CHECKOUT: [3]},
        is_diff_head=is_diff_head,
    )

    assert result.dropped == 0


def test_verify_evidence_counts_tool_lines_of_a_changed_file_when_tools_read_the_diffs_head() -> (
    None
):
    read = call(LineSpan(CHECKOUT, 1, 30))

    result = check([span(10, 12), span(3, 3)], read, diff_lines={CHECKOUT: [3]}, is_diff_head=True)

    assert result == EvidenceCheck(kept=(span(10, 12), span(3, 3)), dropped=0)


def test_verify_evidence_ignores_tool_lines_of_a_changed_file_when_the_commit_may_differ() -> None:
    read = call(LineSpan(CHECKOUT, 1, 30))

    result = check([span(10, 12), span(3, 3)], read, diff_lines={CHECKOUT: [3]})

    assert result == EvidenceCheck(kept=(span(3, 3),), dropped=1)


def test_verify_evidence_ignores_tool_lines_of_a_changed_file_shown_no_diff_line() -> None:
    read = call(LineSpan("package-lock.json", 1, 30))

    result = check(
        [span(1, 1, file="package-lock.json")], read, diff_lines={"package-lock.json": []}
    )

    assert result.dropped == 1


def test_verify_evidence_checks_a_span_ending_far_away_without_walking_its_lines() -> None:
    reads = [call(LineSpan(CHECKOUT, line, line)) for line in range(1, 20_000, 2)]
    cited = [span(1, 1_000_000_000)] * 5

    started = time.perf_counter()
    result = check(cited, *reads, diff_lines={"other.py": range(1, 20_000)})

    assert result.dropped == 5
    assert time.perf_counter() - started < 2
