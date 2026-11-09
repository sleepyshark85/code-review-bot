# P3-S12: Offer suggested fixes in comments

- **Status:** Done
- **Depends on:** P3-S4 (the comment body's `Context:` line comes first)
- **Touches:** `findings`, `prompt` (next context version), `verify` or `review`, `report`, `render`, `pr.comments`, `pr.shadow`, `evals.live` (scrubbing only)

## Value
A context-mode finding can carry a replacement for its line, shown as a GitHub suggestion the PR author can commit in one click after reading it.

## Acceptance criteria
As P3-S1 and P2-S1's shadow report, with a scripted `MockProvider`. A suggestion replaces only the finding's anchored line ([Q7](../design.md#6-open-questions)).
- **AC1:** Given a context-mode finding on added line 14 with `suggestion` `"        return subtotal"`, then its comment body holds, after the rationale and any `Context:` line and before the reaction request, a fenced block opened by ```` ```suggestion ```` holding exactly that text, and the marker is still the last line.
- **AC2:** Given a suggestion holding a run of 3 backticks, then the fence is 4 backticks, always longer than the longest run inside.
- **AC3:** Given each of these, then the suggestion is dropped with that reason, the finding is kept with no block, and the value of a secret is in no output: the anchored line is a context line (`not_added_line`); more than 10 lines or 1,000 characters (`too_long`); the same as the anchored line once trailing whitespace is ignored (`unchanged`); a control character other than tab and line feed, a bidi control (U+202A to U+202E, U+2066 to U+2069, U+200E, U+200F) or U+2028 or U+2029 (`control_characters`); a detected secret (`possible_secret`). Lines are the pieces between line feeds, so 10 lines and a trailing line feed are 11. For `unchanged`, trailing whitespace means only spaces, tabs and line feeds, and a suggestion holding a `control_characters` character is never `unchanged`, so it always gets `control_characters` unless an earlier reason applies. A change of leading indentation isn't `unchanged`, nor is an empty suggestion. `\r` (so a CRLF suggestion), NUL, ESC, DEL and C1 characters are `control_characters`. When several apply, the first of `not_requested` (AC5) and this list's order is the reason.
- **AC4:** Given an empty suggestion, then it's kept, and renders as an empty suggestion block, which deletes the line.
- **AC5:** Given a diff-mode response with a suggestion, then it's dropped `not_requested`. Only kept findings have their suggestion checked; a dropped finding's `suggestion_dropped` is null.
- **AC6:** Given `--format json`, then each finding, rule findings included, has `suggestion` (the kept text or null) and `suggestion_dropped` (the reason or null; null when there was no suggestion); `shadow/v1` comment entries add `suggestion`.
- **AC7:** Given a suggestion holding `@user`, `#12`, a URL, `<!--` or a forged marker line, then it appears only inside the fence, the [P2-S1 AC11](../../phase-2/slices/p2-s1-shadow-report-local.md#acceptance-criteria) check passes on all text outside fenced code, and `read_marker` reads the real marker.
- **AC8:** Given `review-bot --format markdown`, then the suggestion shows as a plain fenced code block with no info string, not a suggestion block.
- **AC9:** Given the context prompt, then it describes `suggestion` (optional; the full replacement for the anchored added line only) and its version is the next free `p3-v` number.
- **AC10:** Given each AC3 and AC5 case and a kept suggestion, then the finding's `verify` `checked` audit record has `suggestion_dropped`, the reason or null, and no suggestion text ([ADR 0005](../../../adr/0005-audit-log.md)).
- **AC11:** Given a live eval call whose response's suggestion holds an AWS-format key, then the recording is written with the key's value replaced by `[REDACTED]`, as for `rationale`, not refused.

## Limits
A suggestion is model text: at most 1,000 characters and 10 lines, checked and secret-scanned in linear time, and rendered only inside a fence it can't close.

## Interfaces
`Finding.suggestion: str | None = None`. Reasons: `not_added_line`, `too_long`, `unchanged`, `control_characters`, `possible_secret`, `not_requested`.

## Out of scope
- Multi-line ranges; updating a kept comment because only its suggestion changed (the plan's rules are unchanged); applying a suggestion automatically (Phase 5).

## Notes
- **README (main session):** describe suggested fixes.
- **Amended by the designer, 2026-11-09, after implementation:** AC3, AC5, AC6 and AC8 now state the readings the developer and tester made (line counting, `unchanged`, control characters, reason order, which findings are checked, rule findings, no info string). AC10 (the drop reason is a verify decision, so it is audited) and AC11 (`suggestion` is model text, so live recordings scrub it like `rationale`; S12 adds the field, so the gap is S12's, not S6's) are added. No criterion is renumbered.
- **Why bidi controls are dropped:** one click commits a suggestion into source, so a prompt injection could plant a Trojan Source payload: code that reads one way and compiles another. The listed characters reorder or break lines invisibly, and no fix needs them.
