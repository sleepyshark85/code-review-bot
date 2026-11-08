# 0016: Measure Phase 3 against a frozen Phase 2 on the same cases and PRs, with per-unit precision and pooled recall

- **Status:** Accepted
- **Date:** 2026-11-09
- **Phase:** 3

## Context
[The Phase 3 gate](../../README.md#phase-3-context-aware) asks that precision and recall improve over Phase 2 on the labelled set. Phase 2's labels judge only the comments Phase 2 produced, so a context review's new findings have no verdict, and true recall is unknowable without a full answer key. Phase 2's shadow weeks are still running, and changing its review mid-window would mix two systems in one measurement.

## Decision
Context mode is opt-in, with its own prompt version; diff mode and its prompt stay byte-for-byte Phase 2's, and Phase 2's shadow reports and Phase 1's recordings are the baseline. On the same PRs, each context report is paired with Phase 2's report of the same head SHA, and PRs without one are excluded. Precision is Phase 2's per-unit formula on each side, and every new unit is judged by a person to the same coverage. Recall is relative to a pool: the accepted issues either side found, grouped by file and anchored line. Eval cases are compared mode against mode with the existing scorer, by each bug's share of runs; runs that hit a cap count as misses. [Details](../phases/phase-3/design.md#how-the-gate-is-evidenced).

## Consequences
- The baseline costs nothing new, and the shadow window measures one system.
- Pooled recall only says which side finds more of the known issues; issues neither found stay invisible.
- People must judge the context run's new units before the verdict is decidable.
- Head-SHA pairing compares the same code, but drops PRs whose final head Phase 2 never reviewed.
- Grouping by anchored line can merge two different accepted issues on one line, which slightly favours neither side.

## Alternatives considered
- **Recall against Phase 2's accepted units only:** credits nothing Phase 3 finds that Phase 2 missed.
- **Judging with a model:** not deterministic, and a live dependency in the measurement.
- **Switching the shadow workflow to context mode:** would contaminate Phase 2's own gate.
