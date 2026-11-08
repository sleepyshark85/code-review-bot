# P3-S16: Run approved live steps from a manual workflow

- **Status:** Draft
- **Depends on:** P3-S3
- **Touches:** `.github/workflows/live.yml` (main session, from this spec), `settings` (`in_ci`), `evals` (`cli`, `live`), `pr` (`cli`), `tests/` (the tester parses the workflow, as for [P2-S3](../../phase-2/slices/p2-s3-shadow-workflow.md))

## Value
The approved live steps run where the key is, the `ANTHROPIC_API_KEY` repository secret, by a person's manual dispatch only, with every amount fixed in the workflow, so no run can spend more than its line of [the $20 table](../design.md#spend-and-caps).

## Acceptance criteria
AC1 to AC6 by parsing `live.yml`; AC7 through the CLIs. Decision: [ADR 0015](../../../adr/0015-per-call-cost-cap-and-step-allowances.md).
- **AC1:** Given `live.yml`, then its only trigger is `workflow_dispatch`, with exactly one input, `step`, of type `choice`, required, with options `smoke`, `evals-diff`, `evals-context` and `backfill`. There's no free-form input.
- **AC2:** Given each step, then its command's amounts are literals from the design table: `smoke` runs `review-bot` twice on a diff between two commit SHAs fixed in the workflow (under 20 KB) with `--context-repo .` and `--max-cost-usd 0.30`; `evals-diff` and `evals-context` run `evals live` with `--runs 3`, `--max-calls 27`, `--max-usd 2.70` / `8.10`, `--max-cost-usd 0.10` / `0.30` and the 9 case IDs as `--case` literals; `backfill` runs `pr backfill --context repo --max-calls 12 --max-usd 7.20 --max-cost-usd 0.60` on the committed `tests/evals/labelled/phase-3-pulls.json`. Each live command also passes `--allow-dispatch` and retries 0.
- **AC3:** Given every job, then it has `if: github.ref == 'refs/heads/main'`, one concurrency group shared by all steps with `cancel-in-progress: false`, `timeout-minutes` (at most 60), and `permissions` exactly `contents: read`. The `backfill` job adds exactly `pull-requests: read` and `issues: read`, and fetches `refs/pull/*/head` before the run. Checkouts use `persist-credentials: false`.
- **AC4:** Given the secrets, then `ANTHROPIC_API_KEY` reaches only the live command's step, and `GITHUB_TOKEN` only the `backfill` step. No `run:` block interpolates `${{ github.event.* }}` or `${{ inputs.* }}`; the choice is read only in `if:` expressions.
- **AC5:** Given any run, then a step with `if: always()` uploads one artifact holding the recordings written, the reports under `--out`, the audit logs, and the command's stdout and stderr captured to files, kept 90 days.
- **AC6:** Given `live.yml`, then no job is named `gate`, and it isn't a required check.
- **AC7:** Given `evals live` or `pr backfill` with `CI` set, then it runs only if `GITHUB_EVENT_NAME` is `workflow_dispatch` **and** `--allow-dispatch` is given. Given `CI` set with the flag but `GITHUB_EVENT_NAME` `pull_request`, `push`, `schedule` or unset, or with `workflow_dispatch` but no flag, then it refuses before any call, exit 2, as today. Without `CI`, the flag changes nothing. Each case has a test.
- **AC8 (recorded), moved from P3-S3 AC9:** As soon as this slice merges, the `smoke` step is dispatched. The test report records each run's tool calls and outcomes, reported cost, findings count, the SDK version, and the init message's tool list from the audit log. **Stop rule:** if any built-in tool is available or any of the three MCP tools is missing, S6 and S8 pause and S3 goes back to design.
- **AC9 (recorded):** The test report lists every dispatch run of `live.yml`, re-runs and failures included, with its URL, step and reported spend, summed against the step's cap and the $20.

## Limits
No input but the choice; every amount is a literal. Each command's own pre-flight still refuses a plan over its `--max-usd`.

## Interfaces
`--allow-dispatch` on `evals live` and `pr backfill`: accepted in CI only with `GITHUB_EVENT_NAME=workflow_dispatch`. `tests/evals/labelled/phase-3-pulls.json` is written by the main session in S15 under [Q10](../design.md#6-open-questions).

## Out of scope
- Deciding to dispatch a step: a person, with approval (S15).

## Notes
- **README (main session):** describe `live.yml` and who may dispatch it.
