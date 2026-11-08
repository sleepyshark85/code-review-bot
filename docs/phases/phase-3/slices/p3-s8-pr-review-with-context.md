# P3-S8: Review pull requests with repository context

- **Status:** Draft
- **Depends on:** P3-S2, P3-S3
- **Touches:** `pr` (`runner`, `cli`, `shadow`), `settings`

## Value
A pull request is reviewed with the model able to read the PR's head commit, in CI and in a bounded backfill, while the default stays Phase 2's review.

## Acceptance criteria
`run` and `backfill` as in [P2-S2](../../phase-2/slices/p2-s2-review-pr-event.md) and [P2-S13](../../phase-2/slices/p2-s13-shadow-backfill.md), with a git repository in `tmp_path` whose working tree is at the base and whose head commit is fetched but not checked out, and a scripted `MockProvider`.
- **AC1:** Given `run --context repo` and a mock that reads `billing/checkout.py`, then the text returned is the head commit's, not the working tree's. `shadow.json`'s `report` has `mode` `context` and the context prompt version, and its `context.commit` is the head SHA. Evidence in a changed file counts lines the tools returned, as [P3-S4 AC4](p3-s4-finding-evidence.md#acceptance-criteria)'s PR mode says.
- **AC2:** Given comment mode and a review of only the hunks changed since the last reviewed commit ([P2-S7](../../phase-2/slices/p2-s7-review-new-commits.md)), then the tools still read the head commit.
- **AC3:** Given `REVIEW_BOT_CONTEXT=repo`, then it behaves as `--context repo`. Given it unset or `off`, then the run is Phase 2's: no tools, `p1-v1`, and `shadow.json` differs from Phase 2's only by the new keys, null or empty. Any other value exits 2 naming it.
- **AC4:** Given a context run, then `caps` adds `max_tool_calls`, `max_tool_result_bytes`, `max_tool_total_bytes` and `max_cost_usd`, `report.usage` has `cost_usd`, and `over_budget` is also true when the reported cost exceeds `max_cost_usd`.
- **AC5:** Given a provider failure from the cost cap or the turn cap, then the report is written with outcome `failed`, reason `permanent`, and the provider's exit code, as for any provider failure.
- **AC6:** Given a refused run (fork, action, no credential, draft), then neither the tools nor the provider run, as in Phase 2.
- **AC7:** Given `backfill --context repo` without `--max-usd` or `--max-cost-usd`, or with planned calls × `--max-cost-usd` over `--max-usd`, then it refuses before any call, naming them. Exit 2.
- **AC8:** Given reported costs that leave less than `--max-cost-usd` of `--max-usd`, then no further PR is reviewed and no report is written for those PRs, so a later run picks them up. stdout's counts add `stopped=budget` and the spend.
- **AC9:** Given `backfill` without `--context`, then it behaves exactly as P2-S13.
- **AC10:** Given a context run, then `DIR/audit.jsonl` holds its `context` `tool_call` records.

## Limits
As P2-S2 and P3-S2. The head commit is read only through git objects, never checked out, so no filter, hook or attribute driver from the PR runs.

## Interfaces
`--context off|repo` / `REVIEW_BOT_CONTEXT` on `run` and `backfill`, default `off`. `backfill` adds `--max-usd`, and reads `--max-cost-usd` as every command does. Caps and values: [Spend and caps](../design.md#spend-and-caps).

## Out of scope
- Linked issues (S9); the workflow change (S14).

## Notes
- **README (main session):** describe `--context repo` on `run` and `backfill`, and backfill's dollar cap.
