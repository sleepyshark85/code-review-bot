# 0021: Route to one fallback model only on availability failures, then degrade to rules-only, and allow only models whose eval suite passes

- **Status:** Accepted
- **Date:** 2026-11-12
- **Phase:** 4

## Context
The [north star](../roadmap.md#2-north-star) asks the bot to fall back to a second provider or to deterministic checks when the model is down, and the [Targets](../roadmap.md#targets) count a degraded result as served. Falling back on the wrong failures would hide problems. After a billing error it would bypass the organisation's spend limit, after a refusal it would shop for a compliant model, and after a cap it would double spend. A model that hasn't passed the evals mustn't review code just because the primary is down.

## Decision
A routing provider tries the policy's primary model, then its single fallback, only after `unavailable`, `transient` or `rate_limit`, never in live commands, and each attempt has its own cost cap. It records every attempt. If every route fails that way and the policy allows it, the run reports the rules' findings with outcome `degraded`. The policy may list only models with a passing eval suite for each mode they serve, which CI's eval gate checks. [Details](../phases/phase-4/design.md#second-provider-and-routing).

## Consequences
- An Anthropic outage still yields a model review, or at least the rule findings, and deterministic gate classes still gate.
- A review's worst-case cost is two caps, which live allowances and CI caps must account for. Its deadline is unchanged: the fallback gets only the time left.
- A degraded run never passes the merge gate unjudged: it blocks on any whole-diff or carried blocker, otherwise it reports `unjudged`.
- Findings from the two models aren't interchangeable, so reports name the model that served, and metrics can split by it.

## Alternatives considered
- **Fall back on every failure:** bypasses spend limits and refusals.
- **Several fallbacks, or load balancing:** speculative with one second provider.
- **Rules-only without a second provider:** loses model findings during every outage.
- **Retry the primary instead:** the CLI already retries ([Phase 0](../roadmap.md#phase-0-stabilise)); a second layer stacks retries.
