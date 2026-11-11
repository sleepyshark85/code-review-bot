# 0006: Count anchors from hunk bodies, and drop findings that miss rather than moving them

- **Status:** Accepted
- **Date:** 2026-11-05
- **Phase:** 1

## Context
The gate requires that no invalid line anchor survives. The sample diff's hunk headers [declare counts their bodies don't match](../roadmap.md#1-current-state), so a parser that trusts them either rejects the diff or accepts lines that aren't there. Real runs also report varying lines for one issue, which tempts a tolerant repair. Every later phase's inline comments depend on this rule.

## Decision
Valid lines are computed from hunk bodies, using only the header's new start. A model finding survives only if it names a reviewable file and an added or context line in it. Anything else is dropped and logged, never moved to a nearby line. [Rule](../phases/phase-1/design.md#line-anchors) and [worked example](../phases/phase-1/slices/p1-s2-parse-and-skip.md).

## Consequences
- Every anchor that reaches a user exists in the diff, and malformed counts don't matter.
- It doesn't prove that the line is the right one: a wrong line inside a hunk survives.
- A misnumbering model loses findings outright, which the audit log and the live eval's drop count reveal.
- If all of a run's findings are dropped, the run looks clean, so it warns on stderr.
- A hunk whose new start is unreadable can't anchor anything, including rule findings.

## Alternatives considered
- **Snap to the nearest valid line:** keeps more findings but invents locations, and CONVENTIONS forbids repairing model output.
- **Require the model to quote the line:** the strongest check, and it would catch wrong lines. Rejected for now, as there's no data that wrong lines are the real problem and it adds a schema field. Revisit if the live eval shows a high drop rate or heavy drift.
- **List valid line ranges in the prompt:** may reduce misses, but changes the prompt's frame with no evidence it's needed. Same revisit condition.
- **Added lines only:** the prompt's nearest-line guidance often lands on context, so this would drop good findings.
- **Reject the whole response on one bad anchor:** one bad line would cost the whole review.
