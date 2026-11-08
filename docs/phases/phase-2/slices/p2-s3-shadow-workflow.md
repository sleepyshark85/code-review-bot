# P2-S3: See a shadow report on every same-repo PR, from an Action that posts nothing

- **Status:** Approved
- **Depends on:** P2-S2
- **Touches:** `.github/workflows/review.yml` (new, written by the main session); otherwise tests only

## Value
Every push to a PR in this repository gets a shadow review in its job summary and an artifact. This starts the weeks of shadow mode the gate needs.

## Acceptance criteria
AC1 to AC11 parse `.github/workflows/review.yml` with PyYAML. Rules: [safety](../design.md#safety-on-the-pr-path), [caps](../design.md#ci-settings-and-caps), [ADR 0008](../../../adr/0008-pull-request-action-with-github-token.md), [ADR 0010](../../../adr/0010-shadow-report-artifact.md).
- **AC1:** The only event is `pull_request`, and the strings `pull_request_target` and `workflow_run` appear nowhere in the file. The shadow job runs only for the actions `opened`, `synchronize`, `reopened` and `ready_for_review`. Later slices may add trigger types, as S8 adds `closed`, but not other events.
- **AC2:** Workflow-level permissions are `contents: read` only, and the shadow job sets exactly the same.
- **AC3:** The shadow job runs only when the PR's head repository equals `github.repository` and `vars.REVIEW_BOT_MODE` is unset or `shadow`. No job is named `gate`, and this slice leaves `ci.yml` unchanged.
- **AC4:** The repository is checked out at the base SHA with `fetch-depth: 0` and `persist-credentials: false`. The head SHA is only fetched. Dependencies install with `uv sync --locked --no-dev`. No step checks out or runs anything at the head ref or SHA.
- **AC5:** In the shadow job, `secrets.ANTHROPIC_API_KEY` appears only in the `env` of the step that runs `python -m review_bot.pr run`, which passes `--mode shadow` and `--out` (the directory holding the report and the audit log). No other secret, and no `GITHUB_TOKEN`, reaches any step of this job.
- **AC6:** In every job, no `run:` block contains `${{`. Step `env` values read only `github.event.pull_request.number`, `.base.sha` and `.head.sha`. `if:` expressions may also read `github.event.action` and `github.event.pull_request.head.repo.full_name`. No other `github.event` field appears anywhere in the file.
- **AC7:** The run step's environment sets the caps in the design's table. The job has `timeout-minutes: 10`. Concurrency is grouped by PR number with `cancel-in-progress: true`.
- **AC8:** One step writes `shadow.md` to `$GITHUB_STEP_SUMMARY`. `actions/upload-artifact` uploads `shadow.json`, `shadow.md` and `audit.jsonl` from the `--out` directory, under a name holding the PR number, the head SHA and `github.run_attempt`, with `retention-days: 90`. Both steps run `if: always()`.
- **AC9:** Every `uses:` is pinned to a 40-hex commit SHA, because the job holds a secret.
- **AC10:** No step has `continue-on-error`, so a non-zero exit from `run` turns this job red ([Q9](../design.md#5-open-questions)).
- **AC11:** Given `REVIEW_BOT_MODE` is anything other than unset, `off`, `shadow` or `comment`, then a check job fails with one line naming the variable and its allowed values, and no review job runs.
- **AC12 (manual, recorded in the test report):** on the first same-repo PR after merge, the user or main session confirms the following:
  - the job ran, and the summary and artifact appeared;
  - the tokens were under the output cap;
  - the latency, and the time taken by checkout, `uv sync` and the review step, taken from the Actions log;
  - branch protection still requires only `gate`.

  A Dependabot PR, or a run before the secret exists, shows `skipped`, `no_credential`, with a green job.

## Limits
None beyond P2-S2: the workflow passes the event file and a clone, and reads nothing from the PR itself.

## Interfaces
Artifact contents: [ADR 0010](../../../adr/0010-shadow-report-artifact.md). `REVIEW_BOT_MODE` is a repository variable: unset or `shadow` runs this job, and `off` runs nothing.

## Out of scope
- Posting anything, or `pull-requests` permission (S6); the `closed` trigger (S8).

## Notes
- **Main session:** write the workflow from this spec; the developer has no path to it. Pin each action by SHA with its tag in a comment, so Dependabot can update it.
- **README (main session):** say that PRs get a shadow review and where to find it.
