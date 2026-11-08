# P3-S3: Let Claude drive the tools, locked down and cost-capped

- **Status:** Draft
- **Depends on:** P3-S1, P3-S2
- **Touches:** `providers` (`claude_agent`, `base`), `settings`, `cli`, `review`, `report`, the provider conformance suite

## Value
A real context review runs with Claude using only the three repository tools, within a turn cap and a per-review dollar cap, and reports what it cost.

## Acceptance criteria
`ClaudeAgentProvider` with a fake `sdk_call`, as in Phase 0; no CLI is launched. Lockdown: [Tool safety](../design.md#tool-safety); caps: [ADR 0015](../../../adr/0015-per-call-cost-cap-and-step-allowances.md).
- **AC1:** Given a request with tools and `max_tool_calls` 12, then the options have: the context system prompt; `tools` `[]`; `allowed_tools` exactly `mcp__repo__read`, `mcp__repo__grep`, `mcp__repo__glob`; `mcp_servers` with only the in-process server `repo` offering those three tools; `strict_mcp_config` true; `setting_sources` `[]`; no agents; `permission_mode` `dontAsk`; `max_turns` 14; hooks holding only a `PreToolUse` hook; and the same `env` as diff mode.
- **AC2:** Given a request without tools, then the options are exactly Phase 0's locked-down ones, and the Phase 0 lockdown tests pass unchanged. In both modes, `max_budget_usd` is set exactly when `--max-cost-usd` is, to its value.
- **AC3:** Given a fake that calls the server's `read` handler with `{"path": "billing/checkout.py"}`, then the executor records that call, the fake receives the executor's text as the tool result, and the findings of the fake's final result are returned.
- **AC4:** Given the `PreToolUse` hook called with `Bash`, `Read`, `Write`, `WebFetch` or `mcp__other__read`, then it returns a deny decision naming the allowed tools. Given any of the three allowed names, then it doesn't deny.
- **AC5:** Given a final result with subtype `error_max_turns`, then the provider raises a `permanent` error whose message names `--max-tool-calls`. Given `error_max_budget_usd`, a `permanent` error naming `--max-cost-usd`. Neither message holds CLI or model text.
- **AC6:** Given a result with `total_cost_usd` 0.1234, then the response's `cost_usd` is `Decimal("0.1234")` and `report/v1`'s `usage.cost_usd` is the string `"0.1234"`. Given a negative, non-finite, boolean or non-numeric value, or none, then `cost_usd` is null and the review still succeeds.
- **AC7:** Given the conformance suite's new tool-loop case, then `MockProvider` and Claude over the fake both make one scripted `read`, return its findings, and leave one ledger entry.
- **AC8:** Given `--max-cost-usd` of `0`, `-1` or `abc`, then it exits 2 naming the flag.
- **AC9 (recorded):** With the user's login, `review-bot` on a small diff of this repository with `--context-repo .` and `--max-cost-usd 0.30`, at most 2 runs. The test report records each run's tool calls and outcomes, reported cost, findings count, the SDK version, and that only the three tools ran. If built-ins stay available, MCP tools vanish with `tools=[]`, or the cost fields don't behave as AC1 and AC6 assume, the slice goes back to the designer.

## Limits
The CLI's result fields are untrusted and type-checked, as Phase 0's are. Tool arguments reach the executor unchanged, which validates them (P3-S1, P3-S2). The query is bounded by `max_turns`, `max_budget_usd`, the output token cap and the deadline.

## Interfaces
`--max-cost-usd` / `REVIEW_BOT_MAX_COST_USD`: a Decimal above 0, unset by default. `PipelineResult.cost_usd` and `report/v1` `usage.cost_usd` ([design](../design.md#interfaces)). No new exit code or error category.

## Out of scope
- Live commands' `--max-usd` (S6, S8).

## Notes
- **README (main session):** "The model runs with no tools" becomes true of diff mode only; describe `--max-cost-usd`.
