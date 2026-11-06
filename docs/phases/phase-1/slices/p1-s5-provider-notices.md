# P1-S5: See provider warnings and failure diagnostics in the audit log

- **Status:** Done
- **Depends on:** P1-S4
- **Touches:** `providers/base`, `providers/mock`, `providers/claude_agent`, `errors`, `review`, `audit`

## Value
Someone investigating a failed or odd Claude call can read what the CLI said, scrubbed, in the audit log, while stdout and stderr stay as safe as they are today.

## Acceptance criteria
Over a fake `sdk_call` that also invokes `options.stderr` with the given lines. Decision: [ADR 0004](../../../adr/0004-provider-notices-as-values.md).

**Warnings**
- **AC1:** Given `AssistantMessage.error="rate_limit"` then a successful result, then the stderr warning's text is unchanged from P0-S4 AC6, `main` exits by the findings, the response's `warnings` holds it, and the log has `agent.warning` with it.
- **AC2:** Given `MockProvider(warnings=("W",))`, then stderr has `W` as a line and the log has `agent.warning`. Given no warnings, there's neither.
- **AC3:** Given stderr closed or failing, then the warning is dropped from stderr, stays in the log, and the exit code stands.
- **AC4:** Given `ClaudeAgentProvider`, then nothing in `providers/` writes to stdout or stderr or imports `console`.

**Diagnostics.** Per the user's decision on [open question 5](../design.md#4-open-questions), a failed call keeps a capped, scrubbed tail of CLI stderr.
- **AC5:** Given a failed call (`ResultError`, error result, or deadline) whose CLI stderr has `CLI-SENTINEL`, then `agent.failed.data` holds that line, and it's in neither stdout nor stderr. The exit code and message are as today.
- **AC6:** Given a successful call with CLI stderr, then the log holds no CLI text.
- **AC7:** Given 50 lines of stderr, then the log has only the last 20. Given a 10,000-character line, then the total kept is at most 2,000 characters. Given a secret that spans the cap's position, or a PEM block longer than the cap, then scrubbing has run first, so no part of either is kept.
- **AC8:** Given CLI lines holding an assembled secret, a PEM block split over lines, a tail with only a PEM's `END` line and the body before it, or `ANTHROPIC_API_KEY=test-key-sentinel` in `environ` and that value in a line, then the log has `[REDACTED]` and none of those values. The same applies to `ANTHROPIC_AUTH_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN`. Given one of them set to an empty value, then the line is unchanged, with no `[REDACTED]` between its characters.
- **AC9:** Parametrised over a CLI line containing a diff line of 8 or more characters: without its `+`, `-` or space marker; without its leading whitespace; in JSON-escaped form (`\"`, `\\`, `\n`), then that whole CLI line is replaced in the log by `[removed: quotes the diff]`. Given a diff line of under 8 characters, it isn't matched.
- **AC10:** Given `MockProvider(error=ProviderError(AUTH, "m", diagnostics=("d",)))`, then the log has `d`, and `d` isn't on stderr.
- **AC11:** Given `tests/conformance/`, then both providers still pass, and `warnings` and `diagnostics` hold only `str`.

## Interfaces
`ReviewResponse`, `ProviderError`, `MockProvider`, `scrub_diagnostics`: [design.md](../design.md#interfaces). Before scrubbing, the provider holds at most the last 1,000 lines, to bound memory.

## Out of scope
- Stream-level diagnostics on a successful call, or any flag to show them on stderr.

## Notes
- AC1's "exits by the findings" uses P1-S2's minimal diff constant; P1-S6 moves this test onto an anchored diff.
- **Limit:** a fragment shorter than a diff line, a reworded diff line, or an unlisted secret format can pass.
- **Handover:** P0-S3 AC5's "a provider writes it during the review" now means the orchestrator writes it, still before the output. P0-S2 AC8 still holds: the callback is set, and CLI text reaches neither stream.
- Scrubbing reads the credential variables and never stores or logs them. This makes Phase 0's "never reads" untrue: see the [closing task](../design.md#3-slices).
- **Implementation (developer):** records are `agent.warning` `{"text"}` and `agent.failed` `{"category", "diagnostics": [...]}`; AC9's 8 characters are counted without the marker and outer whitespace; the orchestrator (`review`) writes each warning to stderr whole, so it carries its own `review-bot: warning:` prefix.
