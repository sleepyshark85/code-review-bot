# P2-S13: Backfill shadow reports over recent merged PRs

- **Status:** Done
- **Depends on:** P2-S2
- **Touches:** `pr` (`runner`, `cli`, `event`, `git`, `shadow`, `outcome`), `errors`

## Value
The user gets shadow reports for this repository's recent merged PRs in one bounded run, so the precision sample doesn't depend only on PRs opened during the window.

## Acceptance criteria
`python -m review_bot.pr backfill --pulls FILE --repo DIR --out OUT --max-calls N`, with a git repository in `tmp_path` and a scripted `MockProvider`. `FILE` is the JSON written by `gh pr list --state merged --json number,baseRefOid,headRefOid,mergedAt,isCrossRepository`.
- **AC1:** Given `FILE` with 3 PRs whose commits are in `DIR`, then `OUT/<number>/shadow.json` is written for each. Each has `mode` `shadow`, `origin` `backfill`, and `pr` holding that PR's number and SHAs, reviewed as in P2-S2 AC1 and AC2. `event_at` is `mergedAt`.
- **AC2:** Given a PR with `isCrossRepository` true, then its report is `skipped` with reason `fork`, and there's no call.
- **AC3:** Given 5 entries, none already in `OUT`, and `--max-calls 3`, then it refuses before any call, naming the planned and allowed calls. Exit 2. The planned calls are every entry not already in `OUT`, counted before any is skipped.
- **AC4:** Given a PR whose head SHA isn't in `DIR`, then its report is `skipped` with reason `missing_commit`, and the others still run. stderr names it and says to fetch `refs/pull/<number>/head`.
- **AC5:** Given a PR whose reviewable bytes exceed the cap, or that changes only docs, then its report is `skipped` with reason `too_large` or `nothing_to_review`, and there's no call.
- **AC6:** Given `CI` set, then it refuses before any call. Exit 2.
- **AC7:** Given a provider failure on one PR, then that report is `failed`, the rest continue, and the exit code is the first failure's.
- **AC8:** Given a malformed `FILE`, or an entry with a SHA that isn't 40 hex digits, then it exits 19 naming the entry, before any call.
- **AC9:** Given any run, then stdout ends with counts per outcome and reason, including how many PRs were `too_large`.
- **AC10:** Given `OUT/<number>/shadow.json` already exists, then that PR is not reviewed again and isn't counted towards `--max-calls`.
- **AC11:** Given `REVIEW_BOT_MAX_RETRIES=2`, then every call is still made with retries 0, as `evals live` does, and `caps.retries` is 0 in every report.

## Limits
`FILE` is at most 10 MB and at most 500 entries. git limits are as in P2-S2. The calls are sequential and bounded by `--max-calls`, with retries forced to 0, so each call is one attempt.

## Interfaces
Same caps and settings as `run` ([design](../design.md#ci-settings-and-caps)). No GitHub API is called: the user produces `FILE` with `gh` and fetches the PR heads.

## Out of scope
- Running backfill in CI: it uses the user's login or key, with the budget the user approved ([Needs a person](../design.md#4-needs-a-person)).

## Notes
- **README (main session):** describe `backfill`, including `git fetch origin 'refs/pull/*/head:refs/pull/*/head'`.

Changed in build, main session, 2026-11-08:
- `FILE` names no repository, but `pr.repo` must be `owner/name` (`pr.units` needs it). A required `--github-repo OWNER/NAME`, checked with the event's `repository.full_name` pattern, gives it. Forks are still found by `isCrossRepository`.
- Each backfilled PR is its own run, with its own `run_id` and audit records, because `label` and `metrics` deduplicate reports by `run_id`.
- "`caps.retries`" in AC11 is the report's `caps.max_retries`. `--max-retries` is accepted, as on every command, and overridden to 0.
- `FILE` listing one PR number twice is malformed (AC8), since each PR's report has its own directory.
- A missing base commit is `missing_commit` too. Any other git failure stops the backfill with exit 19 at that PR, after the counts so far are printed.
- A PR whose existing report has outcome `failed` isn't done (review round 1). It is planned and counted towards `--max-calls` (AC3), reviewed again, and its report replaced. Reports with any other outcome stay untouched (AC10), and so does one that can't be read as `shadow/v1`.
- Counts (AC9): stdout's last two lines are `backfill: entries=E already_in_out=A run=R`, then `backfill:` followed by `name=N` pairs: every outcome, then each reason seen, with `too_large` always shown.
