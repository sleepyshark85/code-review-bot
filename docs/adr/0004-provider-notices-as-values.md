# 0004: Providers return warnings and failure diagnostics as values

- **Status:** Provisional
- **Date:** 2026-11-05
- **Phase:** 1

## Context
The audit log must record every stage decision, but the provider writes its overridden-signal warning straight to stderr, and it discards the CLI's stderr, so neither reaches the log. A response holds only findings and usage, and a failed call produces no response at all. [ADR 0002](0002-llm-provider-interface.md) defines this interface, and a second provider must implement it in Phase 4.

## Decision
This extends ADR 0002 without changing its operation or categories. A response also carries a list of warnings, and, pending the [user's decision](../phases/phase-1/design.md#4-open-questions) on CLI diagnostics, a provider error also carries raw diagnostics, both defaulting to empty. Providers stop writing to any stream: the orchestrator writes warnings to stderr and the audit log, and only the audit module reads diagnostics, after scrubbing them. [Signatures](../phases/phase-1/design.md#interfaces).

## Consequences
- The provider becomes easier to test, as a notice is an ordinary return value, and it no longer imports the console module.
- The warning's text and its stream don't change for users.
- Diagnostics are untrusted text inside an exception, so the rule that only the audit module reads them relies on tests and review, not on types.
- The conformance suite and the Phase 4 provider must fill these fields, or leave them empty.

## Alternatives considered
- **Inject an observer into providers:** a second seam with a single implementation, and it adds I/O to the adapter's interface.
- **Capture through logging handlers:** logging is deliberately silenced to keep raw CLI output off stderr, and a handler would make a safety property depend on global configuration.
- **Put diagnostics in the error's message:** the command line prints the message, so raw CLI text would reach the user.
