# P3-S16: Run the live smoke from a manual workflow, and accept CI only for such a dispatch

- **Status:** Done
- **Depends on:** P3-S3
- **Touches:** `.github/workflows/live.yml` (main session, from this spec), `settings` (`in_ci`), `evals` (`cli`, `live`), `pr` (`cli`), `tests/` (the tester parses the workflow, as for [P2-S3](../../phase-2/slices/p2-s3-shadow-workflow.md))

## Value
Approved live steps run where the key is, the `ANTHROPIC_API_KEY` repository secret, by a person's manual dispatch only, with every amount fixed in the workflow. This slice creates the workflow with its first step, the smoke that checks the SDK wiring; S6 and S8 add the eval and backfill steps.

## Acceptance criteria
AC1 to AC6 by parsing `live.yml`; AC7 through the CLIs. Decision: [ADR 0015](../../../adr/0015-per-call-cost-cap-and-step-allowances.md).
- **AC1:** Given `live.yml`, then its only trigger is `workflow_dispatch`, with exactly one input, `step`, of type `choice`, required. This slice's only option is `smoke`; S6 adds `evals-diff` and `evals-context`, and S8 adds `backfill`. There's no free-form input.
- **AC2:** Given the `smoke` step, then it checks out `main` with `fetch-depth: 0`, writes the diff of `src/` between two commit SHAs fixed in the workflow with `git diff --output`, fails before any call unless it's under 20 KB, and runs `review-bot` on it twice, each with `--context-repo .`, `--context-rev` the fixed head SHA, `--max-cost-usd 0.30` and retries 0: at most $0.60, the [design table](../design.md#spend-and-caps)'s line.
- **AC3:** Given every job, then it has `if: github.ref == 'refs/heads/main'`, one concurrency group shared by all steps with `cancel-in-progress: false`, `timeout-minutes` (at most 60), `permissions` exactly `contents: read`, and checkouts with `persist-credentials: false`.
- **AC4:** Given the secrets, then `ANTHROPIC_API_KEY` reaches only the live command's step. No `run:` block interpolates `${{ github.event.* }}` or `${{ inputs.* }}`; the choice is read only in `if:` expressions.
- **AC5:** Given any run, then a step with `if: always()` uploads one artifact holding the recordings and reports written, the audit logs, and the command's stdout and stderr captured to files, kept 90 days.
- **AC6:** Given `live.yml`, then no job is named `gate`, and it isn't a required check.
- **AC7:** Given `evals live` or `pr backfill` with `CI` set, then it runs only if `GITHUB_EVENT_NAME` is `workflow_dispatch` **and** `--allow-dispatch` is given. Given `CI` set with the flag but `GITHUB_EVENT_NAME` `pull_request`, `push`, `schedule` or unset, or with `workflow_dispatch` but no flag, then it refuses before any call, exit 2, as today. Without `CI`, the flag changes nothing. Each case has a test.
- **AC8 (recorded), moved from P3-S3 AC9:** As soon as this slice merges, the `smoke` step is dispatched. The test report records each run's tool calls and outcomes, reported cost, findings count, the SDK version, and the init message's tool list from the audit log. **Stop rule:** if any built-in tool is available or any of the three MCP tools is missing, S6 and S8 pause and S3 goes back to design.
- **AC9 (recorded):** The test report lists every dispatch run of `live.yml`, re-runs and failures included, with its URL, step and reported spend, summed against the step's cap and the $20. S6, S8 and S15 add their runs to the same list.

## Limits
No input but the choice; every amount is a literal. Each live command's own pre-flight still refuses a plan over its `--max-usd`.

## Interfaces
`--allow-dispatch` on `evals live` and `pr backfill`: accepted in CI only with `GITHUB_EVENT_NAME=workflow_dispatch`.

## Out of scope
- The `evals-diff`, `evals-context` (S6) and `backfill` (S8) choices; deciding to dispatch a step: a person, with approval (S15).

## Notes
- **README (main session):** describe `live.yml` and who may dispatch it.
- **Smoke diff:** no merged slice's whole diff is under 20 KB, so AC2 limits it to `src/`: a real code change, reviewed with the tests and docs readable through the tools at the head SHA. The SHAs are P1-S11's change.
- **One step, two runs:** both reviews run as one loop in a single step, so the key reaches one step only (AC4) and the second review runs even if the first fails.
- **Exit codes:** 0 and 1 are a completed review, without or with findings ([Phase 0 exit codes](../../phase-0/design.md)); a code above 1 fails the step, after both runs.
