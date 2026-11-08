# 0015: Cap each context review's cost through the CLI's budget, and refuse any live step whose calls times that cap exceed its allowance

- **Status:** Proposed
- **Date:** 2026-11-09
- **Phase:** 3

## Context
A context review is one query of several turns, so its cost varies with how much the model looks up. The user approved $20 in total for Phase 3's live spend and asked for hard caps. [ADR 0012](0012-ci-credential-and-cost-caps.md) rejected token budgets because reported token counts can read zero; the Claude CLI now offers a per-query dollar budget and reports each query's cost.

## Decision
Each context review passes a per-review dollar cap to the CLI's query budget, alongside a cap on tool calls, on the bytes tools return, and the existing output cap. Every live command takes a dollar allowance, refuses before any call unless its planned calls times the per-review cap fit, and starts no call once reported spend plus that cap would exceed it, counting an unreported cost as the full cap. The $20 is split into fixed allowances per step. [Values](../phases/phase-3/design.md#spend-and-caps).

## Consequences
- Worst-case spend is known before any call and doesn't depend on reported tokens.
- The CLI checks its budget between turns, so a review can overshoot by one turn; the allowances leave a reserve for it.
- Reviews that hit a cap fail as `permanent`, with no new exit code, and are reported.
- Allowances are coarse: an unused allowance isn't moved between steps without approval.

## Alternatives considered
- **A token budget from reported usage:** unreliable counts, as ADR 0012 found.
- **A spend ledger shared across commands:** one more stateful file to trust, for a one-off total that a fixed split covers.
- **Only the account's monthly limit:** not a cap on Phase 3's own steps.
