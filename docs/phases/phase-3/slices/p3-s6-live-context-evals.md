# P3-S6: Record live context evals within a dollar cap

- **Status:** Draft
- **Depends on:** P3-S3, P3-S5
- **Touches:** `evals` (`live`, `cli`, `render`), `audit`

## Value
The user records live context-mode runs of chosen cases with one command that can't spend more than the dollars approved for it.

## Acceptance criteria
`python -m review_bot.evals live --cases DIR --mode context --runs N --max-calls M --max-usd X --max-cost-usd Y [--case ID ...]`, with a fake `sdk_call` as in [P1-S16](../../phase-1/slices/p1-s16-eval-live-command.md); no CLI is launched. Caps: [ADR 0015](../../../adr/0015-per-call-cost-cap-and-step-allowances.md).
- **AC1:** Given 2 cases with `repo`, `--runs 3`, and enough budget, then each case's `recordings/live-context/recording.json` is written with `mode` `context`, 3 runs, each run's `tool_calls` and `usage.cost_usd`, and fresh freshness fields, so `replay --mode context` scores it without a stale problem.
- **AC2:** Given `--mode context` without `--max-usd` or without `--max-cost-usd`, or with planned calls × `--max-cost-usd` over `--max-usd` (6 calls × 0.30 > 1.50), then it refuses before any call, naming the planned calls, the per-call cap and the allowance. Exit 2. `CI` set and a short `--max-calls` still refuse as in P1-S16.
- **AC3:** Given reported costs that leave less than `--max-cost-usd` of `--max-usd`, then no further call starts. Cases with all their runs done are written; a case cut short keeps its old recording, and stderr names it. stdout says `stopped: budget`, and it exits 1.
- **AC4:** Given a run whose cost isn't reported, then it counts as `--max-cost-usd` towards the spend.
- **AC5:** Given `--case sample-context --case clean-context`, then only those run, and only their calls are planned. An unknown ID exits 2 naming it.
- **AC6:** Given diff mode with `--max-usd` and `--max-cost-usd`, then the same checks apply and the diff-mode recording is written as before. Given diff mode without them, it behaves exactly as P1-S16.
- **AC7:** Given any call, then it's made with retries 0 and `max_budget_usd` equal to `--max-cost-usd`, and the live audit log records its cost.
- **AC8:** Given a run, then stdout adds each case's tool calls (mean and largest per run) and cost, and ends with `spent=$S of --max-usd $X`.
- **AC9:** Given a run in which the model read a file holding a unique string, then that string isn't in the recording, which holds paths, spans, outcomes and byte counts only.

## Limits
As P1-S16: sequential calls, each one attempt. Spend is bounded by `--max-usd`, less one turn's overshoot per call that the CLI's own check allows ([design](../design.md#spend-and-caps)).

## Interfaces
`--max-usd` and `--max-cost-usd` are decimals above 0. `--case` repeats. Exit codes as P1-S16: 0 pass, 1 fail or stopped, 2 refused or invalid input.

## Out of scope
- Running it: that's S15, with the user's approval.

## Notes
- **README (main session):** describe `--mode context`, `--case`, `--max-usd` and `--max-cost-usd`.
