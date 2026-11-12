# P4-S3: Let Claude's hook and `can_use_tool` enforce the tool policy

- **Status:** Done
- **Depends on:** P4-S1. Phase 3's `evals-context` dispatch ran on the earlier wiring (2026-11-10); the owner decided on 2026-11-12 not to run the `backfill`, and Phase 3 closed, so the earlier condition to merge only after both dispatches no longer applies
- **Touches:** `providers.claude_tools`, `providers.claude_agent`, `providers.base`, `review`, `tests/conformance`

## Value
In a Claude context review, the SDK's own permission layers apply the same tool decision the executor does, so a tool outside the policy is denied three times over, and every denial is recorded ([ADR 0018](../../../adr/0018-policy-as-code-file.md)).

## Acceptance criteria
Offline, over the fake SDK of [P3-S3](../../phase-3/slices/p3-s3-claude-drives-tools.md). Lockdown: [Phase 3 Tool safety](../../phase-3/design.md#tool-safety).
- **AC1:** Given a policy allowing `read` and `glob`, when Claude reviews in context mode, then the options' `allowed_tools` are exactly `mcp__repo__read` and `mcp__repo__glob`, the MCP server lists only those two, and the init check fails `permanent` if the CLI lists `mcp__repo__grep` or anything else.
- **AC2:** Given that policy, then the `PreToolUse` hook allows `mcp__repo__read` and denies `mcp__repo__grep`, `Bash` and `Read`, each with a reason naming the policy's version. It decides by the same pure function as the executor.
- **AC3:** Given context mode, then the options carry a `can_use_tool` callback, which allows exactly the policy's tools by full name and denies any other with the same reason. The prompt is sent as streaming input holding the same user prompt text. `permission_prompt_tool_name` is unset.
- **AC4:** Given a fake stream in which the hook or `can_use_tool` denies a tool, then the response carries the denied names, and the audit log has one `agent`/`denied` record per denial, with `tool` (at most 100 characters, escaped), `layer` (`hook` or `can_use_tool`) and `policy_version`. The review goes on. If the review then fails (a cap, or an init check), the denials made before the failure are still recorded, before its `agent`/`failed` record.
- **AC5:** Given diff mode, then the options are exactly as in Phase 3: `tools=[]`, `max_turns=1`, no hooks, no `can_use_tool`, a string prompt. Diff mode's lockdown tests pass unchanged.
- **AC6:** Given `tests/conformance`, then every scenario, `tool_loop` included, still passes for Claude over the fake SDK.
- **AC7 (recorded):** Once merged, and with the Phase 4 budget approved ([Q10](../design.md#6-open-questions)), the existing `smoke` step is dispatched (≤ $0.60). The test report records each run's init tool list, outcome, tool calls, cost and any `agent`/`denied` records. **Stop rule:** if a run fails because of the wiring (a missing tool, a permission error, or a review that never calls a tool when it did in Phase 3's smoke), this slice is reverted before any other context run.

## Limits
The hook's input and the callback's tool name are the CLI's untrusted text. Each is compared exactly, and shown capped and escaped.

## Interfaces
`ReviewResponse.denied_tools: tuple[DeniedTool, ...] = ()`, each with `tool` and `layer`. The audit record `agent`/`denied`.

## Out of scope
- The executor's own enforcement (S1); the second provider's tools (S6).

## Notes
- **Build choices (developer, 2026-11-12):** the one tool decision is `governance.policy.allows_tool`, which the executor, the hook and `can_use_tool` all call; the Claude side first requires the exact `mcp__repo__` prefix. The policy reaches the provider as `ReviewRequest.policy` (default `BUILTIN_POLICY`), set through `build_context_request(policy=...)`. A denied name is escaped character by character and cut so the escaped text is at most 100 characters. The SDK's `CanUseToolShadowedWarning` is silenced inside the real SDK call, since the allowlist shadowing `can_use_tool` is by design ([design §5](../design.md#5-known-risks)) and the warning would otherwise reach stderr.
- **Failed reviews (round 1):** a failed review carries its denials on `ProviderError.denied_tools`, as it carries `diagnostics`, and the pipeline records them before `agent`/`failed` (AC4 as extended).
