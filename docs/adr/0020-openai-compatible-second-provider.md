# 0020: The second provider speaks the OpenAI-compatible Chat Completions API over the standard library, with cost computed from configured prices

- **Status:** Accepted
- **Date:** 2026-11-12
- **Phase:** 4

## Context
[Roadmap Phase 4](../roadmap.md#phase-4-governed) asks for a second real `LLMProvider`, "e.g. OpenAI or self-hosted", that passes the same conformance suite and eval gate ([ADR 0002](0002-llm-provider-interface.md)). The user hasn't chosen a vendor or model yet ([Q2](../phases/phase-4/design.md#6-open-questions)). Phase 0 removed the inherited `openai` dependency, and every dependency must earn its weight (CONVENTIONS §12). The context mode's tools must reach the provider through the existing executor ([ADR 0014](0014-tools-as-a-provider-neutral-executor.md)).

## Decision
The second provider calls the Chat Completions API with the standard library's HTTP client, behind an injected transport, at a configurable base URL. It uses function calling for the tool loop, and needs no vendor SDK. The same code serves OpenAI and any self-hosted server that speaks that API. It reports cost from token prices the operator supplies, and enforces the per-review cap after each turn. [Details](../phases/phase-4/design.md#second-provider-and-routing).

## Consequences
- The build doesn't wait for the vendor choice. Only the live steps and the key do.
- We own the error mapping, retries and tool loop for this provider, and its fakes are our responsibility.
- Self-hosted servers differ in how fully they support function calling. Context mode on them is proven by evals, not assumed.
- Cost is only as right as the prices given, and with a cap set, a run without prices refuses.

## Alternatives considered
- **The `openai` SDK:** a heavy dependency, tied to one vendor's newer APIs, that self-hosted servers don't all mirror.
- **OpenAI's Responses API:** not implemented by common self-hosted servers.
- **A multi-vendor library (for example LiteLLM):** a large transitive surface for one adapter.
- **A self-hosted model only:** needs infrastructure and an endpoint reachable from Actions, which no one owns yet.
