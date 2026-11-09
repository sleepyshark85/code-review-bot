# P3-S3: Let Claude drive the tools, locked down and cost-capped

- **Status:** Done
- **Depends on:** P3-S1, P3-S2
- **Touches:** `providers` (`claude_agent`, `claude_tools`, `base`), `settings`, `cli`, `review`, `report`, the provider conformance suite, `pyproject.toml` (`mcp`)

## Value
A real context review runs with Claude using only the three repository tools, within a turn cap and a per-review dollar cap, and reports what it cost.

## Acceptance criteria
`ClaudeAgentProvider` with a fake `sdk_call`, as in Phase 0; no CLI is launched. Lockdown: [Tool safety](../design.md#tool-safety); caps: [ADR 0015](../../../adr/0015-per-call-cost-cap-and-step-allowances.md).
- **AC1:** Given a request with tools and `max_tool_calls` 12, then the options have: the context system prompt; `tools` `[]`; `allowed_tools` exactly `mcp__repo__read`, `mcp__repo__grep`, `mcp__repo__glob`; `mcp_servers` with only the in-process server `repo` offering those three tools; `strict_mcp_config` true; `setting_sources` `[]`; no agents; `permission_mode` `dontAsk`; `max_turns` 14; hooks holding only a `PreToolUse` hook; and the same `env` as diff mode.
- **AC2:** Given a request without tools, then the options are exactly Phase 0's locked-down ones, and the Phase 0 lockdown tests pass unchanged. In both modes, `max_budget_usd` is set exactly when `--max-cost-usd` is, to its value as a float rounded down to the cent (`0.309` gives `0.30`), never above the cap.
- **AC3:** Given a fake that calls the server's `read` handler with `{"path": "billing/checkout.py"}`, then the executor records that call, the fake receives the executor's text as the tool result, and the findings of the fake's final result are returned.
- **AC4:** Given the `PreToolUse` hook called with `Bash`, `Read`, `Write`, `WebFetch`, `Task`, `ToolSearch`, `ListMcpResourcesTool`, `ReadMcpResourceTool` or `mcp__other__read`, then it returns a deny decision naming the allowed tools. Given any of the three allowed names, then it doesn't deny.
- **AC5:** Given a context-mode final result with subtype `error_max_turns`, then the provider raises a `permanent` error whose message names `--max-tool-calls`. Given `error_max_budget_usd` in either mode, a `permanent` error naming `--max-cost-usd`. Each error says which cap it hit (`turns` or `cost`), so reports and recordings can name cap-hit runs, and the audit's `agent` `failed` record carries it as `cap` (null for every other error). Given `error_max_turns` in diff mode, then the error is Phase 0's `permanent` one, with no cap and no mention of `--max-tool-calls`, which doesn't apply there. No message holds CLI or model text.
- **AC6:** Given a result with `total_cost_usd` 0.1234, then the response's `cost_usd` is `Decimal("0.1234")` and `report/v1`'s `usage.cost_usd` is the string `"0.1234"`. Given a negative, non-finite or boolean value, anything but a JSON number (numeric text such as `"0.1234"` included), or none, then `cost_usd` is null and the review still succeeds. In both modes `usage.cost_usd` and the audit's `agent` `called` record's `cost_usd` are always present, as that string or null.
- **AC7:** Given the conformance suite's new tool-loop case, then `MockProvider` and Claude over the fake both make one scripted `read`, return its findings, and leave one ledger entry.
- **AC8:** Given `--max-cost-usd` of `0`, `0.009`, `-1`, `1000.01`, `nan` or `abc`, then it exits 2 naming the flag: it must be from `0.01`, so rounding down to the cent never gives 0, to `1000`, so the rounded cents are exact as a float. `0.01` and `1000` are accepted.
- **AC9:** Moved to [P3-S16 AC8](p3-s16-live-steps-workflow.md#acceptance-criteria), the live smoke.
- **AC10:** Given a fake whose init `SystemMessage` lists tools other than exactly the three (for example with `Bash`, or without `mcp__repo__grep`), then the provider raises a `permanent` error naming the unexpected or missing tools before any tool handler runs; an init whose tool list is missing or isn't a list counts as listing none, so it fails too. Given exactly the three, the review proceeds, and the list is kept for the audit log (`agent` `tools` record). After a wrong list, a tool handler call is refused without running. Given a context stream with no init message, the review isn't failed and has no `agent` `tools` record. In diff mode the init list is neither checked nor recorded.

## Limits
The CLI's result fields are untrusted and type-checked, as Phase 0's are. Tool arguments reach the executor unchanged, which validates them (P3-S1, P3-S2): the MCP server checks nothing against the schema first, so a malformed call is recorded and counts towards `max_tool_calls` ([Tool safety](../design.md#tool-safety)). The query is bounded by `max_turns`, `max_budget_usd`, the output token cap and the deadline.

## Interfaces
`--max-cost-usd` / `REVIEW_BOT_MAX_COST_USD`: a Decimal from 0.01 to 1000, unset by default. `PipelineResult.cost_usd` and `report/v1` `usage.cost_usd` ([design](../design.md#interfaces)). `ProviderError.cap: Literal["cost", "turns"] | None`, set only for the `error_max_budget_usd` (`cost`) and context-mode `error_max_turns` (`turns`) result subtypes, None for every other error. `ReviewResponse.offered_tools`: the init's tool list in context mode, else None. No new exit code or error category.

## Out of scope
- Live commands' `--max-usd` (S6, S8); the live smoke (S16).

## Notes
- The MCP handler calls the executor, whose git calls run through `asyncio.to_thread` (CONVENTIONS §10).
- **README (main session):** "The model runs with no tools" becomes true of diff mode only; describe `--max-cost-usd`.
- **Build deviations, decided by the designer (2026-11-09) and folded into AC5, AC6, AC8, AC10, Limits and Interfaces:** the 1000 upper bound (`Decimal.quantize` fails past 28 digits, a float holds about 15). The Claude provider is now `claude_agent` and `claude_tools`, both under ADR 0001, which isn't amended: its rule is that nothing outside the Claude provider uses the SDK. Our own `mcp` server instead of `create_sdk_mcp_server`, and `mcp` as a direct dependency: recorded in [design](../design.md#tool-safety), no ADR, since it's confined to the provider and reversible there. **Reverted:** `error_max_turns` in diff mode naming `--max-tool-calls`; diff mode's turn cap is Phase 0's one turn (AC5).
