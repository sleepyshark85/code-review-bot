# Review Bot runbook

For the owner named in the [roadmap](roadmap.md#ownership). The workflow is [`.github/workflows/review.yml`](../.github/workflows/review.yml); its design is in [Phase 2](phases/phase-2/design.md) and ADRs 0008 to 0012.

## Turn the bot off, or change its mode

The repository variable `REVIEW_BOT_MODE` picks what runs on each same-repo pull request:

| Value | Effect |
|---|---|
| unset or `shadow` | Reviews and uploads a shadow report; posts nothing |
| `comment` | Also posts and maintains inline comments and one summary comment |
| `off` | Runs nothing, not even feedback collection |

```
gh variable set REVIEW_BOT_MODE --body off --repo sleepyshark85/code-review-bot
```

The change applies from the next pull request event. The review jobs are never a required check, so turning the bot off never blocks a merge.

## Rotate the model key

1. In the Anthropic Console, create a new key in the bot's workspace.
2. `gh secret set ANTHROPIC_API_KEY --repo sleepyshark85/code-review-bot`, then paste the new key when asked.
3. Delete the old key in the Console.
4. Push to an open pull request that changes code, or re-run its last `review` workflow run, and check the review job ends `outcome=reviewed`. A docs-only PR is skipped without calling the model, so it proves nothing about the key.

A removed or invalid key never blocks a merge. A missing key ends the run `skipped` with reason `no_credential`. An invalid key fails the job with `failed`/`auth` (exit `8`), which shows on the PR but isn't a required check.

## Check spend

- The bot's workspace in the Anthropic Console shows usage and its spend limit. When the limit is hit, runs end `failed` with reason `rate_limit` (HTTP 429) or `permanent` (a billing error) until it resets.
- Tokens per run are in each shadow report (`report.usage`). For totals and cost over a period, download the reports and run `python -m review_bot.pr metrics --reports DIR --labels FILE --price-input-usd-per-mtok X --price-output-usd-per-mtok Y`.

## Read a failed run

1. Open the pull request's `review` workflow run and the failed job.
2. When a report was written, the `Review the pull request` step prints a line ending `outcome=… reason=… comments=… withheld=… run_id=…`. For a `failed` run the error line follows it, and for a skip a hint may come before it. Exits `2` and `19` can stop before any report is written; the error line says why.
3. The job summary shows the report. Download the artifact, `shadow-pr<N>-<head sha>-<attempt>` (shadow job) or `comment-pr<N>-<head sha>-<attempt>` (comment job). It holds `shadow.json` (outcome, reason, timings and token usage), `shadow.md`, and `audit.jsonl` (every stage decision, with scrubbed provider diagnostics and no diff text).
4. The job's exit code matches the exit codes in [usage](usage.md#reviewing-a-diff):
   - `2`: invalid settings or an invalid `.review-bot.toml`;
   - `7` to `13`: a provider failure;
   - `17`: the audit log couldn't be written;
   - `18`: a GitHub API call failed;
   - `19`: a bad event file or a missing commit.

## Outcomes and reasons

| `outcome` | `reason` | Meaning | Action |
|---|---|---|---|
| `reviewed` | none | Reviewed; comments planned or posted | None |
| `reviewed` | `no_new_changes` | Comment mode: nothing changed since the last reviewed commit, so no model call | None |
| `blocked_secret` | none | A possible secret is in the diff; nothing was sent to the model | Check the diff, then rotate the secret if it's real |
| `skipped` | `nothing_to_review` | Only docs, lock, generated or excluded files changed | None |
| `skipped` | `empty` | The diff is empty | None |
| `skipped` | `too_large` | The raw diff is over `REVIEW_BOT_MAX_DIFF_BYTES`, or the diff after skipped files is over `REVIEW_BOT_MAX_REVIEW_BYTES` (both set in the workflow) | Split the PR, or raise the cap in the workflow |
| `skipped` | `fork` | The PR comes from a fork, which never gets the key | None |
| `skipped` | `action` | An event type the bot doesn't review | None |
| `skipped` | `draft` | Comment mode waits until the draft is ready for review | None |
| `skipped` | `already_reviewed` | Comment mode: this head was already reviewed | None |
| `skipped` | `no_credential` | `ANTHROPIC_API_KEY` is missing | Add the secret (see above) |
| `skipped` | `missing_commit` | Backfill: the base or head commit isn't in the clone | Fetch `refs/pull/*/head`, then re-run |
| `skipped` | `not_a_diff`, `unreadable` | The diff couldn't be read | Open an issue labelled `review-bot` |
| `failed` | `invalid_config` | The base commit's `.review-bot.toml` is invalid | Fix the file in a separate PR |
| `failed` | `auth` | The key was rejected | Rotate the key |
| `failed` | `rate_limit` | Rate limit or spend limit reached | Wait, or raise the workspace limit |
| `failed` | `transient` | The provider was briefly unavailable | Re-run the job |
| `failed` | `unavailable` | The Claude Code CLI wasn't found in the job | Check the install step; open an issue labelled `review-bot` |
| `failed` | `permanent` | The call failed in a way retrying won't fix, including a billing error or spend limit | Check the workspace's spend and billing, then the job log |
| `failed` | `refusal`, `invalid_output` | The model refused, or answered in a form the bot rejects | Re-run once; if it repeats, open an issue labelled `review-bot` with the run link |

## Branch protection

`main` requires only the CI `gate` check. The `review` workflow's jobs must never become required checks. Confirm in Settings → Branches after any change to the workflow, and record the date in the Phase 2 test report.

## Support

Open an issue labelled `review-bot`.
