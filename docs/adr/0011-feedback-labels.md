# 0011: Turn shadow judgements, reactions and resolutions into one feedback record, and count only explicit verdicts

- **Status:** Proposed
- **Date:** 2026-11-08
- **Phase:** 2

## Context
[README Phase 2](../../README.md#phase-2-integrated) wants developer reactions and resolutions to become accept and reject labels, and its gate wants precision measured. Phase 1's triage `Label` is the seam ([P1-S12](../phases/phase-1/slices/p1-s12-triage.md#interfaces)). Reactions and resolutions are ambiguous: a resolved thread can mean "fixed" or "dismissed", and people react inconsistently.

## Decision
There is one versioned JSONL record (`feedback/v1`) per finding and signal. A shadow judgement is keyed by repository, PR and would-be comment (key and index, since shadow fingerprints vary between runs); a reaction or resolution is keyed by repository, PR and the comment's posted fingerprint. Each record names the channel it came from: a person's judgement of a shadow report, a reaction, or a resolution. Reactions on every comment that shares a fingerprint (a moved comment and its old thread) are combined: thumbs up alone is `accept`, thumbs down alone is `reject`, and both is `conflict`. A thread a person resolved always gets its own `resolved` record, whatever the reactions. Precision counts only `accept` and `reject`, separately per channel, and the latest record per finding wins. Records hold no rationale text and no usernames. Source, rule part, severity and model come from joining to the `shadow/v1` report that created the comment, never from parsing comment text. [Definitions](../phases/phase-2/design.md#how-the-gate-is-evidenced).

## Consequences
- Shadow precision, the gate's number, rests on the user's explicit verdicts, not on inference.
- Comment-mode precision counts only comments someone reacted to, so its coverage is always reported next to it.
- `resolved` and `conflict` are kept as data for later phases, but don't move precision.
- A reaction label whose report is missing can't be split by source and counts as an orphan, so reports must be kept as long as labels.

## Alternatives considered
- **Resolved means accepted:** more data, but counts dismissals as successes.
- **Silence means accepted:** inflates precision with every ignored comment.
- **Reply keywords ("false positive"):** fragile parsing of free text written by anyone.
- **Read metadata from the comment body:** no join needed, but it trusts text that a body edit or format change can break.
- **Store who reacted:** enables per-person analysis nobody needs, and is personal data.
