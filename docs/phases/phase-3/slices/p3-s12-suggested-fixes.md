# P3-S12: Offer suggested fixes in comments

- **Status:** Draft
- **Depends on:** P3-S1
- **Touches:** `findings`, `prompt` (next context version), `verify` or `review`, `report`, `render`, `pr.comments`, `pr.shadow`

## Value
A context-mode finding can carry a replacement for its line, shown as a GitHub suggestion the PR author can commit in one click after reading it.

## Acceptance criteria
As P3-S1 and P2-S1's shadow report, with a scripted `MockProvider`. A suggestion replaces only the finding's anchored line ([Q7](../design.md#6-open-questions)).
- **AC1:** Given a context-mode finding on added line 14 with `suggestion` `"        return subtotal"`, then its comment body holds, after the rationale and any `Context:` line and before the reaction request, a fenced block opened by ```` ```suggestion ```` holding exactly that text, and the marker is still the last line.
- **AC2:** Given a suggestion holding a run of 3 backticks, then the fence is 4 backticks, always longer than the longest run inside.
- **AC3:** Given each of these, then the suggestion is dropped with that reason, the finding is kept with no block, and the value of a secret is in no output: the anchored line is a context line (`not_added_line`); more than 10 lines or 1,000 characters (`too_long`); the same as the anchored line once trailing whitespace is ignored (`unchanged`); a control character other than tab (`control_characters`); a detected secret (`possible_secret`).
- **AC4:** Given an empty suggestion, then it's kept, and renders as an empty suggestion block, which deletes the line.
- **AC5:** Given a diff-mode response with a suggestion, then it's dropped `not_requested`.
- **AC6:** Given `--format json`, then each finding has `suggestion` (the kept text or null) and `suggestion_dropped` (the reason or null); `shadow/v1` comment entries add `suggestion`.
- **AC7:** Given a suggestion holding `@user`, `#12`, a URL, `<!--` or a forged marker line, then it appears only inside the fence, the [P2-S1 AC11](../../phase-2/slices/p2-s1-shadow-report-local.md#acceptance-criteria) check passes on all text outside fenced code, and `read_marker` reads the real marker.
- **AC8:** Given `review-bot --format markdown`, then the suggestion shows as a plain fenced code block, not a suggestion block.
- **AC9:** Given the context prompt, then it describes `suggestion` (optional; the full replacement for the anchored added line only) and its version is the next free `p3-v` number.

## Limits
A suggestion is model text: at most 1,000 characters and 10 lines, checked and secret-scanned in linear time, and rendered only inside a fence it can't close.

## Interfaces
`Finding.suggestion: str | None = None`. Reasons: `not_added_line`, `too_long`, `unchanged`, `control_characters`, `possible_secret`, `not_requested`.

## Out of scope
- Multi-line ranges; updating a kept comment because only its suggestion changed (the plan's rules are unchanged); applying a suggestion automatically (Phase 5).

## Notes
- **README (main session):** describe suggested fixes.
