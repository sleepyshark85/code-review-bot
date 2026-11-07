# P1-S16: Record a live eval run with one explicit command

- **Status:** Done
- **Depends on:** P1-S5, P1-S15
- **Touches:** `evals`

## Value
Someone authorised can spend a bounded budget to measure the real model with one command, and nothing can trigger that by accident.

## Acceptance criteria
Command: `python -m review_bot.evals live --cases DIR --runs N --max-calls M`. Tests inject `MockProvider`, or `ClaudeAgentProvider` over a fake `sdk_call`, through its Python entry point: **no criterion here makes a live call.**
- **AC1:** Given 2 cases, `--runs 2` and a scripted provider, then it makes 4 calls and writes one `live` recording per case at `<case_id>/recordings/live/recording.json`, with every [Recording field](p1-s14-eval-sample.md#interfaces) and each run's `findings`, `usage` and `warnings`.
- **AC2:** Given a recording written here, then `replay` accepts it and scores it to the same result the live command printed.
- **AC3:** Given a provider that fails on one run, then that run is recorded with its `error` category, the others continue, and the failure counts in the report.
- **AC4:** Given findings with a secret-like value or invalid output, then what's written is the validated findings only, with no raw model text, and with secret values as `[REDACTED]`, and the file passes the secret scan.
- **AC5:** Given no `--max-calls`, or a planned call count above it, then it exits 2 before any call and states the planned count. Given no `--runs`, the same.
- **AC6:** Given `CI` set in the environment, then it exits 2 before any call, even with every flag. Given the default `replay`, no flag or variable can reach a live call.
- **AC7:** Given a case whose diff has a detected secret, then it exits 2 before any call and names the case.
- **AC8:** Given a case, then its recording is written atomically when that case's runs finish, including runs recorded as failed, so an interruption leaves that case's old file intact.
- **AC9:** Given the report, then it shows, per bug, runs caught over total and each matching rationale; per source, recall; per case, noise and injection results, including `needs human review`; the anchor drop rate; for each bug, the distinct lines used, as drift; per case, the fingerprint recurrence rate (the share of fingerprints seen in at least 2 runs); the model's `rule` tag rate and the dedup count; `invalid_output` and refusal counts; run-to-run finding counts; total tokens; and every unmatched finding, for a person to read.
- **AC10:** Given the guard in `tests/conftest.py`, then the suite still records no real SDK call.
- **AC11:** Given `live`, then retries are 0 whatever `--max-retries` or `REVIEW_BOT_MAX_RETRIES` say: over a fake `sdk_call`, each call's `options.env` has `CLAUDE_CODE_MAX_RETRIES` of `0`. A retried call would otherwise make `--max-calls` understate API attempts up to threefold, and token counts can read 0, so a token total couldn't enforce a bound. The bound holds by CLI contract; no live run has confirmed that 0 means one attempt.
- **AC12:** Given a `live` run, then it appends to the audit log at `.review-bot/eval-live-audit.jsonl`, under the working directory, one `run_id` per call, whatever `--audit-log` settings exist. If it creates `.review-bot/`, it adds the same `.gitignore` containing `*` as P1-S4 AC13, and a test checks this.

## Interfaces
[Layout and recording format](p1-s14-eval-sample.md#interfaces). Uses `ClaudeAgentProvider` and the settings' deadline and model.

## Out of scope
- Running it for real (P1-S17), CI scheduling, cost in currency: no price is held in the repository.
- A hard wall-clock bound: at the default deadline, 25 calls can take about 2 hours at worst.

## Notes
- With retries at 0, a transient failure is recorded as a failed run. Re-running because of it is a user decision under P1-S17.
- **README (main session):** describe the `live` command, and that it spends real tokens.
- **Changed in build, developer, 2026-11-07:** the test seam is `review_bot.evals.main(argv, *, sdk_call=...)`, which builds `ClaudeAgentProvider` over it. `live` takes only the model, deadline and API timeout from the settings; it has no `--max-retries` flag, so passing one exits 2 before any call. It exits 0 or 1 by replay's exit rule, scoring each recording as replay reads it from disk. Each call's audit records end at `agent.called` or `agent.failed`, with no `run.end`. A string still holding a secret in its JSON form after redaction is written as `[REDACTED]` whole, and a recording that would still fail the secret scan isn't written (exit 2).
- **Changed in build, main session's decision, 2026-11-07:** after each call, stderr gets one line, `live: case <case_id> run <n>/<runs> ... ok` or `... failed (<category>)`; stdout holds only the report. A case whose files are all skipped exits 2 before any call with `case <case_id>: nothing to review: every file is skipped, so no call would reach the model. Give the case a diff with a file that isn't skipped.` Once the calls end, `sys.unraisablehook` reports are discarded, as in the review CLI (P1-S18).
- **Changed in review round 1, main session's decision, 2026-11-07:** an unexpected exception in a call is recorded as a failed run with category `internal`, which the recording format now allows, with its class name in the audit log and never its text; the other runs continue. Any other unexpected exception stops the command with exit 2 and the one line `review-bot evals: error: internal error. Its text isn't shown, as it can hold model or CLI output.` Every setting but the model, deadline and API timeout is fixed, so no other `REVIEW_BOT_*` variable is read.
