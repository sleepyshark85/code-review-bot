# P4-S13: Measure the SLOs over whole weeks

- **Status:** Approved
- **Depends on:** P4-S8
- **Touches:** `pr.slo` (new), `pr.cli`, `pr.reports`; `docs/runbook.md` (main session, from this spec)

## Value
The owner can show, week by week, whether the review service met its SLOs: p95 time to review under 5 minutes, and at least 99.5% of PR events served by a review or an explicit degraded result. A run that crashed before writing a report counts against the SLO ([Targets](../../../roadmap.md#targets)).

## Acceptance criteria
Offline, over downloaded reports and a runs file from `gh run list --workflow review.yml --json databaseId,event,status,conclusion,createdAt,headSha,headBranch,attempt --limit 1000`. Only `pull_request` runs count; comment-event runs (the merge gate's) are left out. Definitions: [design, Tracing and SLOs](../design.md#tracing-and-slos).
- **AC1:** Given 200 `pull_request` events in the window, 199 with reports whose outcome is `reviewed`, `degraded`, `blocked_secret`, or `skipped` for `too_large`, `nothing_to_review`, `empty`, `draft` or `already_reviewed`, and 1 with `failed`, then availability is 99.50%, and it meets its SLO.
- **AC2:** Given a report `skipped` for `no_credential`, or for any reason not named in AC1, then it counts as missed.
- **AC3:** Given a run with conclusion `failure`, `timed_out` or `startup_failure` and no report for its head SHA, then it counts as missed. Given a `cancelled` run, and a later run on the same PR that wrote a report, then it is superseded and counted neither way. "The same PR" means the same `headBranch`, or, where both runs have reports, the same PR number in them. A `cancelled` run with no later run on the same PR counts as missed.
- **AC4:** Given a successful run with no report, as for a `closed` event or a skipped job, then it isn't counted.
- **AC5:** Given reports with end-to-end times (`finished_at − event_at`) of 1 to 100 seconds, one each, then p95 is 95 s by nearest rank, as `pr metrics` computes it, and it meets its SLO. With p95 at 300 s or more, it doesn't.
- **AC6:** Given `--since 2026-11-23 --until 2027-02-21` (both inclusive, 91 days), then it shows one row per week starting on `--since` (13 rows), each with events, served, missed, availability and p95, and a whole-window row with both SLOs' verdicts. A window that isn't whole weeks exits 2.
- **AC7:** Given the same report twice (one `run_id`), then it counts once.
- **AC8:** Given `--format json`, then it writes `slo/v1` with the rows, verdicts and counts, and no PR title or username.

## Limits
As `pr metrics`: report files at most 10 MB each; the runs file at most 10 MB, parsed strictly, and malformed input exits 2, naming the file. Events are joined to reports by head SHA and time window, not by text.

## Interfaces
`python -m review_bot.pr slo --reports DIR --runs FILE --since YYYY-MM-DD --until YYYY-MM-DD [--format text|json]`: exit 0 (both SLOs met), 1 (either missed) or 2.

## Out of scope
- Alerting: the runbook names the owner as on-call and GitHub's failed-run notification as the alert.

## Notes
- **Runbook (main session):** an "SLOs and on-call" section: the two SLOs, how to run `pr slo` weekly, who is on call, and what to do on a miss.
