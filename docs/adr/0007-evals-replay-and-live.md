# 0007: Evaluate models by manual live runs, and replay their recordings offline in CI

- **Status:** Accepted
- **Date:** 2026-11-05
- **Phase:** 1

## Context
[The Phase 1 gate](../roadmap.md#phase-1-trustworthy-core) wants planted bugs caught and injection resisted, and everything offline in CI. Those conflict: a mock can test the pipeline but can't measure a model, and tests may never call a live model. The eval format is also a seam for Phase 2's labelled pull requests and Phase 4's release gate, so it is costly to change.

## Decision
Eval cases are versioned files holding a diff, its expectations and its pass thresholds, and a separate manual command runs them against the real model and records what it returned, before verification. CI replays those recordings through today's pipeline with the existing mock provider, and rejects any whose case file, diff or exact prompts have changed. The gate needs both kinds of evidence. [What each proves](../phases/phase-1/design.md#what-the-evals-prove).

## Consequences
- A passing CI eval means the pipeline still handles a past response correctly. It says nothing about the model today, and replay output states this.
- Any change to the case, or to what the model would see, needs a new live run to get CI green, which costs tokens and needs a login. That's the prompt gate the roadmap wants, arriving earlier.
- Injection is scored on what the model returned, so verification dropping a requested finding can't hide a compromised model.
- Hand-written recordings are marked synthetic, sit apart from live ones and can never count as a measurement. They exist to prove the scorer can fail.
- A new model isn't detected by replay alone, so the final gate test requires recordings of the default model.
- Scoring by file, line window, category and keyword groups is approximate, so a person confirms each credit, and the prompt is tuned on the cases it's scored on, which the test report discloses.
- Re-running until a threshold passes would void the result, so every attempt is reported, and a re-run without a prompt change is the user's decision.

## Alternatives considered
- **Mock responses only:** always green, and proves nothing about a model.
- **Live evals in CI:** direct evidence, but costly, flaky, needs a shared secret, and breaks the no-live-calls rule.
- **A dedicated replay provider:** a third provider with no behaviour the mock lacks.
- **Record raw model text:** closer to the wire, but needs a raw field on the provider seam, and invalid output is already a recorded failure.
- **Judge findings with a model:** better matching, but a live dependency inside the scorer, and it isn't deterministic.
