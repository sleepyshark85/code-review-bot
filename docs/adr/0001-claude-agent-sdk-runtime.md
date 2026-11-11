# 0001: Use the Claude Agent SDK as the model runtime

- **Status:** Accepted
- **Date:** 2026-11-04
- **Phase:** 0

## Context
The inherited tool calls an OpenAI API that no longer exists, so it needs a new model runtime. [Roadmap Phase 0](../roadmap.md#phase-0-stabilise) names the Claude Agent SDK. It works with the team's existing Claude account, and Phases 3 to 5 need its tools, hooks and subagents. Its cost is that it drives the Claude Code CLI as a subprocess, with agent defaults a single-shot review must not inherit.

## Decision
The Claude provider uses the Claude Agent SDK, pinned to version 0.2.178, locked down as the roadmap specifies and also restricted to explicitly supplied MCP configuration, with lockdown asserted in tests. The CLI's retries are the only retry layer, and our overall deadline setting is the only wall-clock bound. No other module uses the SDK.

## Consequences
- Later phases get tools, hooks and subagents without changing runtime.
- Real runs start more slowly, and the wheels are about 150 MB, apparently because they bundle the CLI. A missing CLI is therefore rare but still handled.
- A CLI is probably present wherever the package is installed, including CI. Tests stay offline because they fake the SDK call and never launch it, not because the CLI is absent.
- Fakes can't prove real SDK behaviour, so the Claude slices keep manual smoke runs. An SDK upgrade must re-check the error mapping.
- Authentication belongs to the CLI: the bot stores no key. Shared environments must use an Anthropic API key.

## Alternatives considered
- **Anthropic API client directly:** lighter, with no subprocess, but needs an API key for every developer and leaves us to build the tool loop and hooks for Phase 3.
- **Fix the OpenAI call:** smallest change, but the phase gate rules out the dependency and it offers none of the later agent features.
- **Multi-vendor framework:** a second abstraction over our provider interface, with no second provider before Phase 4.
