# P4-S7: Fall back to a second model when the first is unavailable

- **Status:** Done
- **Depends on:** P4-S1, P4-S2
- **Touches:** `providers.router` (new), `providers.base`, `errors`, `governance.policy`, `cli`, `pr.cli`, `pr.runner`, `evals.cli`, `review`, `report`

## Value
When Claude is down, a review is still done by the policy's fallback model, and the report says which models were tried and which one served. Only models the policy lists may review code at all ([ADR 0021](../../../adr/0021-route-on-availability-failures-then-rules-only.md)).

## Acceptance criteria
Policy sections `[[models]]` (`provider`, `model`, `modes`, optional `base_url`) and `[routing] fallback` (an entry that is also in `[[models]]`). Offline: `MockProvider`s per route, or fakes. Rules: [design, routing](../design.md#second-provider-and-routing).
A run's **attempts** are written, in order, as one audit `agent`/`attempt` record each, and as `report/v1` `route` when a report is written; a failed run writes no report, so its attempts are only in the audit. A tried attempt is `{"route", "category", "cost_usd"}`; a skipped one is `{"route", "skipped"}`, `skipped` being `no_credential` or `deadline`.
- **AC1:** Given models `claude:claude-opus-5-5` (diff, context) and `openai:m1` (diff), fallback `openai:m1`, and a primary that fails `unavailable`, when a diff is reviewed, then the fallback gets the same request once. `report/v1` `route` is `[{"route": "claude:claude-opus-5-5", "category": "unavailable", "cost_usd": null}, {"route": "openai:m1", "category": null, "cost_usd": ...}]`, the findings are the fallback's, and the audit log has one `agent`/`attempt` record per attempt.
- **AC2:** Given a primary failing `transient` or `rate_limit`, then AC1 holds. Given `auth`, `refusal`, `invalid_output` or `permanent` (which includes every cost, turn or tool-call cap failure, with either provider), then no fallback is tried, and the run fails as today, with one attempt.
- **AC3:** Given both routes failing `unavailable`, then the run fails with the last attempt's category and exit code, and its attempts are both routes.
- **AC4:** Given a policy with no `[routing]`, or the built-in policy, then exactly one provider is called, and requests, findings and exit codes are as before. `route` holds the single attempt.
- **AC5:** Given a policy listing models, then `--provider claude --model other`, or context mode when the matching entry lists only `diff`, exits 2 before the diff is read, naming the allowed entries. The mock provider is exempt. A fallback that isn't in `[[models]]` makes the policy invalid (exit 2).
- **AC6:** Given context mode with a fallback that lists only `diff`, then no fallback is tried.
- **AC7:** Given context mode and a primary that made 5 tool calls before failing `transient`, then the fallback shares the same executor: with `max_tool_calls` 12, it has 7 calls left, and the ledger holds all 12 in order.
- **AC8:** Given `max_cost_usd` 0.60, then each attempt is capped at 0.60. Under a policy with a fallback, `shadow/v1` `over_budget` is true if any attempt's reported cost is over it, in diff and context mode alike; without a fallback it is judged as today (cost in context mode only, tokens in both).
- **AC9:** Given `pr run` in CI with no `ANTHROPIC_API_KEY` but `OPENAI_API_KEY` set and the fallback configured, then the run goes straight to the fallback, and its audit has an `agent`/`attempt` record `{"route": "claude:…", "skipped": "no_credential"}`. With neither key, it is `skipped`, `no_credential`, as today, which the SLO counts as missed (S13).
- **AC10:** Given `evals live` or `pr backfill` under a policy with a fallback, then only the primary is ever called, so their budgets stay planned calls × cap.
- **AC11:** Given `deadline_s` 240 and a primary that fails `transient` after 200 s, then the fallback runs with the 40 s left, and the whole review fails `transient` at 240 s if it isn't done. With no time left, no fallback is tried, and the run fails with the primary's category; its attempts are the primary's, then `{"route": "openai:m1", "skipped": "deadline"}`.

## Limits
None beyond P4-S2 and Phase 3: the router adds no input. An attempt's error message is the provider's own, which is already safe to show.

## Interfaces
`Attempt`, `RoutingProvider`, `ReviewResponse.attempts` and `ProviderError.attempts`, per the design. Route names are `<provider>:<model>`.

## Out of scope
- Rules-only degradation (S8); the eval gate on listed models (S9); the fallback's key and live runs (S14).

## Notes
- A `Route` is a provider name, a model and a maker of its provider given the seconds left, since a fallback's HTTP call can't be cancelled and must get only the time left; None when it has no credential (skipped). `Attempt` lives in `providers.base`, beside `ReviewResponse`, and `providers.router` re-exports it, to avoid an import cycle.
- A failed run's audit records come from `ProviderError.attempts`. A review that isn't routed has one attempt, on `<settings.provider>:<settings.model>`.
- A fallback without its key is skipped, not refused, so a review the primary serves still runs. An `openai` fallback needs both prices under a cost cap (exit 2). `[routing] fallback` must match a `[[models]]` entry's provider, model and `base_url`.
- `render` also changed, to write `route`.
