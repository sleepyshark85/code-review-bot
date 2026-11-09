"""Keep a finding's cited spans only if the model was shown every line of them (P3-S4).

Pure.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from review_bot.context.ledger import LineSpan, ToolCall, ToolOutcome, merged_spans_by_file
from review_bot.findings import EvidenceSpan

__all__ = ["EvidenceCheck", "ShownLines", "verify_evidence"]


class ShownLines:
    """The lines the model was shown in one review, by path.

    A diff's added and context lines always count. So do the lines a tool returned, except in a
    file the diff changes when the tools' commit may not be the diff's head: its line numbers
    may then differ from the diff's (Phase 3 design, Data flow).
    """

    def __init__(
        self,
        diff_lines: Mapping[str, Iterable[int]],
        ledger: Iterable[ToolCall],
        *,
        is_diff_head: bool,
    ) -> None:
        """Index the shown lines.

        Args:
            diff_lines: Each path the diff changes, with its added and context lines shown to
                the model; none for a file it wasn't shown, such as a skipped one.
            ledger: The review's tool calls. A refused call returned no lines.
            is_diff_head: Whether the tools read the commit the diff ends at.
        """
        spans = [LineSpan(path, line, line) for path, lines in diff_lines.items() for line in lines]
        spans += [
            span
            for call in ledger
            if call.outcome is not ToolOutcome.REFUSED
            for span in call.spans
            if is_diff_head or span.path not in diff_lines
        ]
        # Merged, so a span shown in pieces that touch is still held by one of them.
        self._by_path = merged_spans_by_file(spans)

    def holds(self, span: EvidenceSpan) -> bool:
        """Say whether every line of ``span`` was shown, in time linear in its file's spans."""
        return any(
            shown.first_line <= span.start_line and span.end_line <= shown.last_line
            for shown in self._by_path.get(span.file, ())
        )


@dataclass(frozen=True, slots=True)
class EvidenceCheck:
    """The cited spans kept, in the order cited, and how many were dropped."""

    kept: tuple[EvidenceSpan, ...]
    dropped: int


def verify_evidence(evidence: Sequence[EvidenceSpan], shown: ShownLines) -> EvidenceCheck:
    """Keep each span of ``evidence`` whose every line is in ``shown``, and count the rest.

    A span's ``file`` must equal a shown path exactly, so no other path the model wrote is kept.
    """
    kept = tuple(span for span in evidence if shown.holds(span))
    return EvidenceCheck(kept, len(evidence) - len(kept))
