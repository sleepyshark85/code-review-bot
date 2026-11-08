# P3-S4: See the context behind every finding

- **Status:** Draft
- **Depends on:** P3-S1
- **Touches:** `findings`, `context.evidence`, `prompt` (next context version), `review`, `report`, `render`, `pr.comments`, `pr.shadow`

## Value
Each finding shows which lines of which files it relied on, and only lines the model was actually shown, so a reviewer can check a cross-file claim at once.

## Acceptance criteria
As P3-S1. A finding may carry `evidence`: spans `{file, start_line, end_line}` the model cites. Code keeps a span only if the model was shown every line of it ([design](../design.md#interfaces)).
- **AC1:** Given a ledger with a `read` of `billing/checkout.py` lines 1 to 30, and a finding citing lines 12 to 12 of it, then the span is kept.
- **AC2:** Given only a `grep` match on line 12 of `billing/checkout.py`, then a span of 12 to 12 is kept and a span of 11 to 13 is dropped.
- **AC3:** Given a span of a file no tool returned and the diff doesn't hold, or of lines past those returned, or of a refused call's file, then the span is dropped, the finding is kept, and its `evidence_dropped` is 1. A truncated `read` counts only the lines it returned.
- **AC4:** Given a span in a file the diff changes, then it's kept only if every line in it is an added or context line of the diff's new version.
- **AC5:** Given a finding with 6 spans, a span whose `start_line` is after its `end_line`, or a line below 1, then the whole output is rejected `invalid_output` (exit 13), as any schema violation is.
- **AC6:** Given `--format json`, then each finding has `evidence` (the kept spans, in the model's order) and `evidence_dropped`. Given diff mode, then a span is checked against the diff alone, and `evidence` is `[]` when the model gives none.
- **AC7:** Given a comment body (shadow Markdown or comment mode) for a finding with kept spans, then after the rationale it has one line `Context: ` and each span as a code span, `path:12` or `path:10-14`, and the marker stays the last line. A finding with none has no such line.
- **AC8:** Given any run, then each `verify` `checked` record adds `evidence_kept` and `evidence_dropped` counts, and no span text.
- **AC9:** Given the context prompt, then it asks for `evidence` and its version is the next free `p3-v` number ([prompt versions](../design.md#interfaces)); the diff-mode prompt is unchanged.

## Limits
Spans come from the model: at most 5 per finding, each checked against the ledger and the parsed diff in time linear in the ledger's spans. A span's `file` is kept only if it equals a path the ledger or diff holds, so no model-written path is shown.

## Interfaces
`Finding.evidence: tuple[EvidenceSpan, ...] = ()`, each `file` (non-empty), `start_line`, `end_line` (at least 1). `report/v1` and `shadow/v1` comment entries add `evidence` and `evidence_dropped`.

## Out of scope
- Replaying evidence from recordings (S5); any change to anchoring, which stays on diff lines ([ADR 0006](../../../adr/0006-line-anchors-drop-never-snap.md)).

## Notes
