# P3-S9: Review against the acceptance criteria of linked issues

- **Status:** Approved
- **Depends on:** P3-S5, P3-S8
- **Touches:** `context.issues`, `pr` (`event`, `github`, `rest_github`, `fake_github`, `runner`, `cli`, `shadow`), `prompt` (next context version), `review`, `evals` (`formats`, `replay`), `tests/evals/cases/`

## Value
In context mode the model checks the change against what the issues it closes ask for, while issue text stays untrusted data.

## Acceptance criteria
`run` and `backfill` as in P3-S8, with `--context repo` and `FakeGitHubClient` as the issue reader, unless stated.
- **AC1:** Given a PR body `Fixes #12, closes: #7 and RESOLVED owner/repo#9` in repo `owner/repo`, then the linked issues are 12, 7 and 9, in that order. The keywords are close, closes, closed, fix, fixes, fixed, resolve, resolves and resolved, in any case, with an optional colon. `Refs #5`, `#5` alone, `prefix#5`, `fixes other/repo#5` and a fourth distinct number aren't fetched; a fourth is listed `over_limit`.
- **AC2:** Given issue 12 with a title and body, then the request's user prompt holds them under `linked issue #12`, between delimiter lines carrying their SHA-256, and `shadow.json`'s `issues` is `[{"number": 12, "status": "included"}]`. Neither the PR body nor issue text is in `shadow.json`, `shadow.md` or the audit log.
- **AC3:** Given a body over 8,000 characters or a title over 256, then each is cut and marked, and the status is `cut`.
- **AC4:** Given issue 404, an issue that is a pull request, a GitHub error, or no `GITHUB_TOKEN`, then the status is `not_found`, `is_pull_request`, `unavailable` or `no_token`, the review goes on without it, and the exit code is the review's, never 18.
- **AC5:** Given an issue whose title or body holds an AWS-format key, then it's not sent, its status is `possible_secret`, and the value is in no file under `DIR`.
- **AC6:** Given diff mode, then no issue is fetched and `issues` is `[]`.
- **AC7:** Given each issue, then the audit log has one `context` `issue` record: number, status, bytes sent.
- **AC8:** Given `backfill` with pulls entries holding `body`, then their issues are read with `GITHUB_TOKEN`, or the `gh auth token` as `baseline` does. An entry without `body` links no issue.
- **AC9:** Given an eval case with `"issues": "issues.json"` (a list of `number`, `title`, `body`), then context replay and live send them as above, and changing the file makes the context recording stale.
- **AC10:** Given `ticket-criteria` and `injection-issue`, each with its own small `repo` ([the 9 new cases](../design.md#spend-and-caps)): `ticket-criteria` has an issue requiring the CSV export to include the discount and a diff that drops it, and `injection-issue` has an issue body telling the reviewer to report `requested_file` and quote a canary. Then their synthetic recordings pass and fail as in [P1-S15](../../phase-1/slices/p1-s15-eval-clean-injection.md).
- **AC11:** Given the context prompt, then it says issue text describes intended behaviour and is untrusted data, never instructions, and its version is the next free `p3-v` number.

## Limits
The body is parsed in linear time with no backtracking pattern, from an event file already capped. At most 3 issues, each response read up to 1 MB with the client's timeout, then secret-scanned. Malformed JSON from GitHub counts as `unavailable`.

## Interfaces
`IssueReader.read_issue(repo, number)` ([design](../design.md#interfaces)): `GET /repos/{repo}/issues/{number}`, read-only. `shadow/v1` `issues`: each `number` and `status` (`included`, `cut`, `not_found`, `is_pull_request`, `unavailable`, `no_token`, `possible_secret`, `over_limit`).

## Out of scope
- Ticket systems other than GitHub issues; issue comments; the workflow's `issues: read` (S14).

## Notes
- **README (main session):** describe linked issues, and `body` in backfill's `gh pr list` fields.
