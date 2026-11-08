# 0015: Cap each context review's cost through the CLI's budget, refuse any live step whose calls times that cap exceed its allowance, and run live steps only by manual dispatch

- **Status:** Proposed
- **Date:** 2026-11-09
- **Phase:** 3

## Context
A context review is one query of several turns, so its cost varies with how much the model looks up. The user approved $20 in total for Phase 3's live spend and asked for hard caps. [ADR 0012](0012-ci-credential-and-cost-caps.md) allows at most one model call per run and rejected token budgets because reported token counts can read zero; the Claude CLI now offers a per-query dollar budget and reports each query's cost. The only key is a repository secret, and the live commands refuse to run in CI.

## Decision
This amends ADR 0012's "at most one model call" for context mode: one query of several turns. Each context review passes a per-review dollar cap to the CLI's query budget, rounded down to the cent, alongside caps on tool calls, on the bytes tools return, and the existing output cap. Every live command takes a dollar allowance, refuses before any call unless planned calls times the per-review cap fit, and starts no call once reported spend plus that cap would exceed it, counting an unreported cost as the full cap. The $20 is split into fixed allowances per step, covering every dispatched run, re-runs included. Live steps run only from a manual-dispatch workflow on `main` whose one input picks a step and whose amounts are literals; the live commands accept CI only for such a dispatch with an explicit flag. Each step joins the workflow with the slice that builds its command, and a test checks the real CLI parser accepts its arguments. [Values](../phases/phase-3/design.md#spend-and-caps); the workflow: [P3-S16](../phases/phase-3/slices/p3-s16-live-steps-workflow.md) (smoke and guard), [P3-S6](../phases/phase-3/slices/p3-s6-live-context-evals.md) (evals), [P3-S8](../phases/phase-3/slices/p3-s8-pr-review-with-context.md) (backfill).

## Consequences
- Worst-case spend is known before any call and doesn't depend on reported tokens.
- The CLI checks its budget between turns, so a review can overshoot by one turn; the reserve covers it.
- Reviews that hit a cap fail as `permanent`, marked with the cap, with no new exit code, and count as misses.
- No one can dispatch a different amount without a reviewed change to the workflow; an unused allowance isn't moved without approval.
- The key is used by a second workflow, still only in the step that needs it.

## Alternatives considered
- **A token budget from reported usage:** unreliable counts, as ADR 0012 found.
- **A spend ledger shared across commands:** one more stateful file to trust, for a one-off total that a fixed split and the test report's sum of dispatch runs cover.
- **Running live steps locally with the user's login:** the user is away, and spend would be invisible to the repository.
- **Free-form dispatch inputs for amounts:** one typo could spend past the allowance.
