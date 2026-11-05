# P1-S13: Edit a finding during triage

- **Status:** Approved
- **Depends on:** P1-S12
- **Touches:** `triage`, `render`, `audit`

## Value
A developer can keep a useful finding while correcting its severity or wording, without losing its anchor.

## Acceptance criteria
With injected `read_line`. The prompt is `[a]ccept [r]eject [e]dit`; `e` then asks for severity, then rationale, where an empty answer keeps the current value.
- **AC1:** Given `e`, `low`, `Better text.`, then the report shows severity `low` and that rationale, in every format; `triage` is `edit`. With `--format json`, `original_rationale` holds the model's or rule's text.
- **AC2:** Given `e` and two empty answers, then the label is still `edit`, with values unchanged, `rationale_changed` false and `original_rationale` null.
- **AC3:** Given severity `urgent`, then it asks again, with the rest unaffected.
- **AC4:** Given an edit, then `file`, `line`, `source`, `rule`, `category`, `confidence` and `fingerprint` don't change, and no prompt offers them.
- **AC5:** Given `critical` lowered to `low` with `--threshold high`, then the finding stays in the report and it exits 1: a human's decision outranks the threshold, which was applied before triage.
- **AC6:** Given an edit, then the `triage` record has `severity_before`, `severity_after` and `rationale_changed` true, and not the new text.
- **AC7:** Given new text holding a newline or `\x1b`, then it's a single input line by construction, and it renders safely under the rules of P1-S10 and P1-S11.
- **AC8:** Given new text holding an assembled secret, then the report shows `[REDACTED]` in its place.
- **AC9:** Given EOF or Ctrl+C part-way through an edit, then that finding stays `undecided` with its original values, as in P1-S12 AC6.

## Interfaces
[P1-S12](p1-s12-triage.md#interfaces).

## Out of scope
- Editing `file` or `line`: an edited anchor would bypass verify.

## Notes
