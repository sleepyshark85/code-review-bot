# 0002: Put every model behind an asynchronous provider interface that raises categorised errors

- **Status:** Accepted
- **Date:** 2026-11-04
- **Phase:** 0

## Context
[README Phase 0](../../README.md#phase-0-stabilise) requires a provider seam not tied to Claude, used by the mock provider now and by a second real provider in Phase 4, which must pass the same conformance suite. Its shape is costly to change once several providers and the conformance suite depend on it.

## Decision
The provider interface is a structural protocol with one asynchronous operation: it takes a review request and returns validated findings with token usage ([signatures](../phases/phase-0/design.md#interfaces)). Known failures raise a single provider error carrying one of the seven categories in the README. The output schema is the fixed findings schema, not a parameter, and every provider validates through one shared parser.

## Consequences
- The CLI maps one error type to exit codes, and no SDK type leaves a provider.
- Conformance can run over any provider given a fake for its edge, so the Claude adapter runs offline too.
- Adding a category means adding an exit code and a conformance case.
- A second output schema (Phase 5 agents, perhaps) would mean revisiting this interface.

## Alternatives considered
- **Return a result or error value:** every caller must branch, and CONVENTIONS §8 has adapters raise domain errors.
- **Five categories plus a sub-code:** a missing CLI and invalid model output need their own exit codes, but two fields would have to agree and a sub-code invites Claude-specific values.
- **Synchronous interface:** simpler to call, but would need a thread or nested event loop around the SDK's asynchronous call.
- **Schema as a parameter:** generic, but speculative with a single schema.
