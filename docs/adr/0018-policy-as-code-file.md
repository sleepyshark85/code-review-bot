# 0018: One versioned policy file governs what the bot may send, to which models, with which tools and gates, and each decision is enforced where its action happens

- **Status:** Accepted
- **Date:** 2026-11-12
- **Phase:** 4

## Context
[Roadmap Phase 4](../roadmap.md#phase-4-governed) asks for policy as code, enforced through SDK hooks and `can_use_tool`. Today the rules are spread out. The tool allowlist is hard-coded in the Claude provider ([ADR 0014](0014-tools-as-a-provider-neutral-executor.md)), caps are in workflow variables, and `.review-bot.toml` is written by each repository ([P2-S9](../phases/phase-2/slices/p2-s9-repo-config.md)). Auditors need one artefact that says what a run was allowed to do, and a second provider must obey the same rules as Claude.

## Decision
One versioned TOML policy file, read by CI from the base commit, decides the tools, cost ceiling, redaction, models and gate classes, and every report records its version and hash; with no file, a built-in policy reproduces Phase 3. A repository's config can only opt in to what the policy approves. The decisions are pure functions, enforced where each action happens: the tool executor for every provider, Claude's `PreToolUse` hook and `can_use_tool`, and the pipeline ([design](../phases/phase-4/design.md#policy-as-code)).

## Consequences
- One file answers "what was this run allowed to do", and its hash ties every finding to it.
- Each section arrives with the slice that enforces it, and unknown keys are invalid, so the format stays honest.
- A policy change is a reviewed commit that takes effect only once merged, because CI reads it from the base commit.
- Enforcement is in more than one place for tools: the executor is authoritative, and the SDK layers are defence in depth.

## Alternatives considered
- **Extend `.review-bot.toml`:** a repository would govern itself, which defeats an organisation policy.
- **OPA/Rego or another policy engine:** a new runtime and language for about five decisions.
- **Policy in workflow variables:** untyped and spread across jobs, and not recorded in reports.
- **Enforce only in SDK hooks:** the second provider and every offline test would be ungoverned.
