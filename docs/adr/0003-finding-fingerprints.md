# 0003: Identify findings by file, rule and anchored line content, not by line number

- **Status:** Accepted
- **Date:** 2026-11-05
- **Phase:** 1

## Context
[Roadmap Phase 1](../roadmap.md#phase-1-trustworthy-core) asks for stable fingerprints, used now to dedupe rule and AI findings and in Phase 2 to key self-updating PR comments. Changing the scheme later would orphan every posted comment. Two facts complicate it: rule and AI findings need a shared rule identity to compare at all, and the model's line number for one issue varies between runs.

## Decision
A fingerprint is a versioned hash of the file, the rule, the whitespace-normalised text of the anchored line and an ordinal among otherwise identical findings. The model may tag a finding with one of a closed set of rule identifiers, and an untagged AI finding uses its category as the rule part. An AI finding is a duplicate of a rule finding only with the same file and rule, nearby in one hunk, and a severity no higher than the rule's. [Details](../phases/phase-1/design.md#fingerprints-and-dedup).

## Consequences
- A finding keeps its identity when lines above it move, or its indentation changes.
- Phase 2 can key comments on it, but editing the anchored line produces a new identity, and the old comment must be resolved.
- An AI finding that picks a different line between runs gets a different fingerprint. The live eval reports a recurrence rate per case as Phase 2 input, and this may prove too unstable to key comments on.
- The ordinal depends on order, so swapping two findings on one line can swap their identities.
- The model's tag is untrusted: a wrong tag can hide an AI finding of equal or lower severity, so the live eval reports the tag rate and dedup count.
- The findings schema and prompt change, so the prompt version moves.

## Alternatives considered
- **Include the line number:** simplest, but any edit above a finding would change its identity.
- **Include the rationale:** distinguishes findings, but model wording varies on every run.
- **Dedupe by location alone:** would drop a real AI finding about a different problem on the same line as a rule finding, such as an unvalidated path on the open call.
- **Dedupe by exact fingerprint only:** the README's wording, but it misses the same issue anchored on a neighbouring line.
- **Dedupe regardless of severity:** simpler, but a model tag could then suppress a more serious finding.
- **Infer the rule from the rationale's words:** fragile, and wrong in ways nobody could test.
