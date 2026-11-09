# P3-S4: See the context behind every finding

- **Status:** Done
- **Depends on:** P3-S1
- **Touches:** `findings`, `context.evidence`, `prompt` (next context version), `review`, `report`, `render`, `pr.comments`, `pr.shadow`

## Value
Each finding shows which lines of which files it relied on, and only lines the model was actually shown, so a reviewer can check a cross-file claim at once.

## Acceptance criteria
As P3-S1. A finding may carry `evidence`: spans `{file, start_line, end_line}` the model cites. Code keeps a span only if the model was shown every line of it ([design](../design.md#interfaces)).
- **AC1:** Given a ledger with a `read` of `billing/checkout.py` lines 1 to 30, and a finding citing lines 12 to 12 of it, then the span is kept.
- **AC2:** Given only a `grep` match on line 12 of `billing/checkout.py`, then a span of 12 to 12 is kept and a span of 11 to 13 is dropped.
- **AC3:** Given a span of a file no tool returned and the diff doesn't hold, or of lines past those returned, or of a refused call's file, then the span is dropped, the finding is kept, and its `evidence_dropped` is 1. A truncated `read` counts only the lines it returned.
- **AC4:** Given a span in a file the diff changes, then which lines count depends on whether the tools' commit is known to be the diff's head:
  - **PR mode** (`run`, `backfill`: the tools read the head SHA the diff ends at): a line counts if it's an added or context line of the diff, or a tool returned it.
  - **Otherwise** (`review-bot --context-repo`, whose `--context-rev` may differ from the diff's new version): only added and context lines of the diff count; ledger lines of that file don't.

  AC1 to AC3 are unchanged for files the diff doesn't change.
- **AC5:** Given a finding with 6 spans, a span whose `start_line` is after its `end_line`, or a line below 1, then the whole output is rejected `invalid_output` (exit 13), as any schema violation is.
- **AC6:** Given `--format json`, then each finding has `evidence` (the kept spans, in the model's order) and `evidence_dropped`. Given diff mode, then a span is checked against the diff alone, and `evidence` is `[]` when the model gives none.
- **AC7:** Given a comment body (shadow Markdown or comment mode) for a finding with kept spans, then after the rationale it has one line `Context: ` and each span as a code span, `path:12` or `path:10-14`, and the marker stays the last line. A finding with none has no such line.
- **AC8:** Given any run, then each `verify` `checked` record adds `evidence_kept` and `evidence_dropped` counts, and no span text.
- **AC9:** Given the context prompt, then it asks for `evidence` and its version is the next free `p3-v` number ([prompt versions](../design.md#interfaces)); the diff-mode prompt is unchanged.

## Limits
Spans come from the model: at most 5 per finding, each checked against the ledger and the parsed diff in time linear in the ledger's spans. A span's `file` is kept only if it equals a path the ledger or diff holds, so no model-written path is shown.

## Interfaces
`Finding.evidence: tuple[EvidenceSpan, ...] = ()`, each `file` (non-empty), `start_line`, `end_line` (at least 1). `report/v1` and `shadow/v1` comment entries add `evidence` and `evidence_dropped`. `ReviewContext.is_diff_head: bool = False` says the tools' commit is the diff's head; `review-bot` leaves it false, and `pr.runner` sets it from S8 (tested here through `review_diff`).

## Out of scope
- Replaying evidence from recordings (S5); any change to anchoring, which stays on diff lines ([ADR 0006](../../../adr/0006-line-anchors-drop-never-snap.md)).

## Notes
Changed in build:
- `context.evidence` holds `ShownLines` (the diff's lines plus the ledger's, merged per path, built once per review) and `verify_evidence`. `verify_findings` takes keyword-only `ledger` and `is_diff_head`, checks every proposal's evidence whatever its anchor outcome, and stores the result on `ProposalCheck.evidence`. A kept finding carries its kept spans in `Finding.evidence` and the count in `ReviewedFinding.evidence_dropped`.
- "A file the diff changes" is any new path in the diff, skipped files included: their diff lines never count (the model wasn't shown them), and their ledger lines count only when `is_diff_head`.
- `shadow/v1` resolve entries carry `evidence: null` and `evidence_dropped: null`, as they do `source` and `body`: a comment read back from the PR holds no evidence.
- The context prompt is `p3-v2`.
