# P4-S8: Report the rules' findings when every model is down

- **Status:** Approved
- **Depends on:** P4-S5, P4-S7
- **Touches:** `review`, `governance.policy`, `report`, `pr.outcome`, `pr.shadow`, `pr.runner`, `pr.comments`, `pr.run_metrics`, `cli`

## Value
When no model can review, a PR still gets the deterministic checks' findings, clearly marked degraded, so the rules and any gate class built on them keep working through an outage ([ADR 0021](../../../adr/0021-route-on-availability-failures-then-rules-only.md)).

## Acceptance criteria
Policy key `[routing] degrade_to_rules` (bool, default false).
- **AC1:** Given `degrade_to_rules = true`, every route failing `unavailable`, and a diff with a swallowed exception, when `pr run` reviews it, then `shadow/v1` has `outcome` `degraded` and `reason` `unavailable`, `report/v1` has `degraded` `"rules_only"`, its findings are exactly the rules' findings, and the exit code is the provider's (7, `unavailable`).
- **AC2:** Given AC1 in comment mode, then the rule findings are planned and posted as in a reviewed run, and the summary starts "Degraded: no model was available, so only the built-in rules ran." The summary keeps the previous `reviewed=` SHA (or none), so the next run reviews from there, and the merge gate sees this head as not reviewed.
- **AC3:** Given `degrade_to_rules = false` (the built-in policy), or a last failure that isn't routable (`auth`, `refusal`, `invalid_output`, `permanent`, a cap), then the run fails as today, with `outcome` `failed`.
- **AC4:** Given AC1 in comment mode, with P4-S5's money config and money class, and a diff that also adds `total = 9.99` to `billing/prices.py`, then the gate decision is `block` on that `decimal-money` rule finding, the marker lists it, and `reviewed=` is kept. Given no rule, secret or carried blocker, then the decision is `unjudged`, never `pass` (P4-S5 AC11).
- **AC5:** Given `review-bot DIFF` with AC1's policy, then the rule findings print as usual, stderr says the review is degraded and why, the JSON report has `degraded` `"rules_only"`, and the exit code is the provider's.
- **AC6:** Given a report with outcome `degraded`, then `pr metrics` counts it as its own outcome, leaves it out of model latency, tokens and cost, and includes its findings in precision by `source` `rule`.
- **AC7:** Given any degraded run, then the audit log's `agent` stage has `degraded` with the last category, and the rules' records are as in a reviewed run.

## Limits
None: no new input.

## Interfaces
`Outcome.DEGRADED` = `"degraded"`. `report/v1` `degraded`: null or `"rules_only"`.

## Out of scope
- The SLO's treatment of `degraded` (S13).

## Notes
- 2026-11-12, designer: AC4 used `swallowed-exception`, whose rule always reports `medium` and so can never be a gate class (P4-S5 AC13); it now uses `decimal-money`.
