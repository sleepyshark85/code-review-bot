# P4-S6: Review with repository context on the second provider, within a cost cap

- **Status:** Approved
- **Depends on:** P4-S2
- **Touches:** `providers.openai_wire`, `providers.openai_compat`, `settings`, `cli`, `pr.cli`, `evals.cli`, `tests/conformance`

## Value
The OpenAI-compatible provider drives the same read-only tools through function calling, under the same tool caps and a per-review cost cap, so it passes the whole conformance suite ([ADR 0020](../../../adr/0020-openai-compatible-second-provider.md)).

## Acceptance criteria
Offline, over a fake `HttpTransport` and the real `RepoTools` on a fixture repository.
- **AC1:** Given a request with tools, then each POST carries `tools`, one `{"type": "function", "function": {"name", "description", "parameters"}}` per executor definition, in the executor's order.
- **AC2:** Given a response whose message has `tool_calls`, then each call, in order, goes to the executor with its parsed arguments. Its result is appended as a `tool` message with the call's `tool_call_id`, after the assistant message, and the next POST carries the whole conversation. The loop ends at the first message with no tool calls, whose content is parsed as findings.
- **AC3:** Given a tool call whose `arguments` isn't JSON, isn't an object, or is over 64 KB, then the executor is still called, with an empty mapping, so the call is refused, recorded in the ledger and counted. The review goes on.
- **AC4:** Given a model that keeps calling tools, then after `max_tool_calls + 2` model turns the review fails `permanent` with `cap` `turns`, naming `--max-tool-calls`.
- **AC5:** Given `--price-input-usd-per-mtok 2.50 --price-output-usd-per-mtok 10` and turns using (1,000,000 in, 10,000 out) then (500,000 in, 5,000 out), then `cost_usd` equals 3.90 exactly as a `Decimal` value (2.50 + 0.10 + 1.25 + 0.05), whatever its exponent.
- **AC6:** Given `--max-cost-usd 3.00` and both those turns asking for tools, then the review fails `permanent` with `cap` `cost`, naming `--max-cost-usd`, after the second turn (running total $3.90), and no third POST is sent. If the turn that goes over the cap is the final answer instead, its findings are returned with `cost_usd` $3.90, as Claude's one-turn overshoot is ([ADR 0015](../../../adr/0015-per-call-cost-cap-and-step-allowances.md)).
- **AC7:** This replaces P4-S2 AC9's refusal. Given `--max-cost-usd` without both prices, for `--provider openai`, then exit 2 before any request, naming the missing price flag. Without a cap and without prices, `cost_usd` is null.
- **AC8:** Given diff mode, then no `tools` are sent, and a response with `tool_calls` fails `invalid_output`.
- **AC9:** Given `tests/conformance`, then the `tool_loop` scenario passes for `openai`, so every scenario passes for all three providers.
- **AC10:** Given the loop, then `deadline_s` bounds the whole review, and retries (P4-S2 AC4) apply per POST, within the deadline.

## Limits
Model output is untrusted. Each response is capped at 4 MB, each call's arguments at 64 KB, and the calls per message are bounded by the executor's `max_tool_calls`, past which every call gets the fixed refusal. Tool results are already capped and delimited by the executor.

## Interfaces
Settings `price_input_usd_per_mtok` and `price_output_usd_per_mtok` (flags `--price-input-usd-per-mtok` and `--price-output-usd-per-mtok`, env `REVIEW_BOT_PRICE_*`), decimal strings as in `price_flags`. They are used only by `openai`.

## Out of scope
- Live context evals for this provider ([Q13](../design.md#6-open-questions)).

## Notes
