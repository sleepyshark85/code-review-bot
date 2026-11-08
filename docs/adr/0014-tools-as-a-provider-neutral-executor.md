# 0014: Pass tools to providers as a provider-neutral executor that records every call, and use SDK hooks only to enforce

- **Status:** Accepted
- **Date:** 2026-11-09
- **Phase:** 3

## Context
The provider interface takes a request and returns findings ([ADR 0002](0002-llm-provider-interface.md)). Tools need a loop between model and code, and the README wants hooks to record the context behind each finding. Recording only in Claude SDK hooks would make the record Claude-specific, so `MockProvider` runs, and every offline test, would record nothing; the README accepts the tool loop itself as Claude-specific but keeps the review call portable.

## Decision
A review request may carry a tool executor: the tool definitions and one asynchronous call operation that never raises on model input. The executor records every call in a ledger of files, line spans, outcomes and bytes, with no file content or model text. Providers only drive the loop: `MockProvider` makes scripted calls, and the Claude provider exposes the executor as an in-process MCP server, with a `PreToolUse` hook that denies every other tool. [Signatures](../phases/phase-3/design.md#interfaces).

## Consequences
- The context record is the same for every provider and fully testable offline; per-finding evidence is checked against it.
- A Phase 4 provider must drive the same executor and passes a new conformance case for the tool loop.
- The hook is the enforcement point Phase 4 extends with policy-as-code.
- The request now carries an object with behaviour, not only data.

## Alternatives considered
- **Record in `PostToolUse` hooks:** Claude-only, and offline tests would see no record.
- **A second provider operation for tool reviews:** two paths through every provider and the conformance suite for one difference.
- **Let the provider own the tools:** each provider would reimplement confinement.
