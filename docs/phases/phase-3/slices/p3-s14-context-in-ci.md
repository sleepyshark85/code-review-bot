# P3-S14: Turn on context reviews in CI with one repository variable

- **Status:** Done
- **Depends on:** P3-S8, P3-S9
- **Touches:** `.github/workflows/review.yml` (main session, from this spec), `tests/` (the tester parses the workflow, as for [P2-S3](../../phase-2/slices/p2-s3-shadow-workflow.md))

## Value
The owner switches this repository's PR reviews to context mode by setting one variable, with every cap fixed in the workflow, and nothing changes until then.

## Acceptance criteria
Checked by parsing `review.yml`, except AC7. Caps: [Spend and caps](../design.md#spend-and-caps).
- **AC1:** Given the `check-mode` job, then it also fails visibly, naming the value, when `vars.REVIEW_BOT_CONTEXT` is anything but unset, `off` or `repo`.
- **AC2:** Given the shadow and comment review steps, then each sets `REVIEW_BOT_CONTEXT` from `vars.REVIEW_BOT_CONTEXT`, defaulting to `off`; `REVIEW_BOT_MAX_TOOL_CALLS` `12`, `REVIEW_BOT_MAX_TOOL_RESULT_BYTES` `16000` and `REVIEW_BOT_MAX_TOOL_TOTAL_BYTES` `96000` as literals; `REVIEW_BOT_MAX_COST_USD` `0.60` **only** when the variable is `repo`, exported by the step's `run:` block from that literal (an `env:` key can't be omitted, and an empty value is invalid), and never set otherwise, so a diff-mode run's settings and `caps` stay exactly Phase 2's; and `REVIEW_BOT_DEADLINE_S` `300` when the variable is `repo`, else `240`. Phase 2's caps are unchanged.
- **AC3:** Given the shadow job, then its permissions are exactly `contents: read` and `issues: read`, and its review step gets `GITHUB_TOKEN`. The comment job's are exactly `contents: read`, `pull-requests: write` and `issues: read`. No other job's permissions change.
- **AC4:** Given every job, then the head commit is still fetched and never checked out, no `run:` block interpolates `${{ github.event.* }}`, no job is named `gate`, and the bot still installs from the base commit.
- **AC5:** Given `ANTHROPIC_API_KEY` and `GITHUB_TOKEN`, then each reaches only the review step, as in [ADR 0012](../../../adr/0012-ci-credential-and-cost-caps.md); `GITHUB_TOKEN` still never reaches the model's process (`take_github_token`).
- **AC6:** Given a run with `vars.REVIEW_BOT_CONTEXT` unset, then the review step's environment selects `off` and `REVIEW_BOT_MAX_COST_USD` is not in it, so the run is Phase 2's (P3-S8 AC3).
- **AC7 (recorded):** If the owner sets `REVIEW_BOT_CONTEXT=repo`, the test report records the date and one run: `mode` `context`, reported cost (at most $0.60), tool calls, review and end-to-end time, and the set-up time from the Actions log. Otherwise it records that context mode wasn't turned on, and why.

## Limits
None beyond P3-S8 and P3-S9: the workflow adds no input.

## Interfaces
Repository variable `REVIEW_BOT_CONTEXT`: unset or `off` (default), or `repo`.

## Out of scope
- Choosing when to turn it on: the owner's call ([Q6](../design.md#6-open-questions)).

## Notes
- **README (main session):** describe `REVIEW_BOT_CONTEXT` and the CI caps.
