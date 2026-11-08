# P2-S11: Capture the value baseline from PR history

- **Status:** Done
- **Depends on:** P2-S6 (the real client)
- **Touches:** `pr` (`metrics`, `github`, `cli`)

## Value
The owner records how this repository's PRs went before the bot commented, so that later phases can show whether it helps.

## Acceptance criteria
`python -m review_bot.pr baseline --repo OWNER/NAME --since DATE --until DATE --out FILE`, with an injected `FakeGitHubClient`. AC9 uses S6's local stub. Measures and proxy: [baseline](../design.md#how-the-gate-is-evidenced), [Q8](../design.md#5-open-questions).
- **AC1:** Given 4 PRs merged in the window, open for 2, 5, 10 and 30 hours before merging, then `FILE` is one ASCII JSON document with `schema` `baseline/v1` and `pr_count` 4. Open-to-merge time has a median of 7.5 hours (the mean of the middle two) and a p90 of 30 hours (nearest rank).
- **AC2:** Given commits on those PRs, then the commits whose committer date is after the PR opened are counted per PR, with median and p90. They are reported as commits, not pushes.
- **AC3:** Given these titles of merged PRs: `Fix rounding`, `fix: totals`, `Fixture cleanup` and `Prefix tidy`, then only the first two count as `fix_prs` (case-insensitive, `fix` as a whole first word). With 2 issues labelled `bug` created in the window, `bug_issues` is 2 and `escaped_defects_per_merged_pr` is 1.0.
- **AC4:** Given PRs closed without merging, or merged outside the window, then they aren't counted.
- **AC5:** Given an empty window, then `pr_count` is 0, the medians and p90s are null, and it exits 0.
- **AC6:** Given any run, then `FILE` also holds `repo`, `since`, `until` and `captured_at` (UTC), and stdout shows the same numbers as text.
- **AC7:** Given `--until` before `--since`, a date that isn't ISO 8601, or a repo that isn't `owner/name`, then it exits 2 naming the flag. Given no `GITHUB_TOKEN`, then it exits 2 naming the variable.
- **AC8:** Given the client fails, then it exits 18, and `FILE` isn't written.
- **AC9:** Given the stub, then `RestGitHubClient` lists the merged PRs, their commits and the matching issues, with S6's page cap, timeouts and errors. The token appears nowhere in the output.
- **AC10:** Given PR titles holding control characters or a canary, then no title appears in `FILE`; only the count of `fix` PRs does.

## Limits
API limits are as in S6. A window can cover at most 1,000 merged PRs; more exits 18, naming the limit.

## Interfaces
`list_merged_pull_requests` and `count_issues`: [design](../design.md#interfaces). The command is read-only and needs a token with `contents: read`, `pull-requests: read` and `issues: read`, or the user's own `gh auth token`.

## Out of scope
- Choosing the window's end: the day comments were turned on, or today if they weren't (the user decides, in S12).

## Notes
- **README (main session):** describe `baseline`.
- **Build decisions (lead, 2026-11-08):** the window is the UTC days `--since` to `--until`, both whole, applied to merge time; bug issues are counted by `created_at` in the same window, and a PR labelled `bug` isn't one. An empty window gives null `escaped_defects_per_merged_pr`. On failure an earlier `FILE` is left as it was: it is staged as `FILE.tmp` and then replaces `FILE`.
- **Shape:** `open_to_merge_hours` and `commits_after_open` each hold `median` and `p90`, as floats (hours rounded to 2 places, the ratio to 3). Stdout repeats each value as the JSON writes it, so null reads `null`.
- **REST:** the search API (`/search/issues`, oldest first, `is:pr is:merged merged:<window>`, each PR kept once; `is:issue label:"bug" created:<window>`, `per_page=1`, its `total_count`), then `/pulls/{n}/commits` per PR. A `total_count` over 1,000, the search API's own cap, exits 18; so does `incomplete_results`.
