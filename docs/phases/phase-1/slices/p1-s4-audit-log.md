# P1-S4: Trace every run in a JSONL audit log

- **Status:** Approved
- **Depends on:** P1-S3
- **Touches:** `audit` (new), `review`, `settings`, `cli`, `errors`

## Value
An auditor can reconstruct what each run decided, from one file, without it holding the code under review.

## Acceptance criteria
Records and stages: [design.md](../design.md#audit-log). `SENTINEL-DIFF` is in the diff, and `V` is an assembled secret.
- **AC1:** Given a successful mock review with `--audit-log PATH`, then every line of `PATH` is a JSON object with `schema` `audit/v1`, one `run_id`, a UTC `ts`, and a `stage`, `event` and `data`; and `run.start`, `parse.parsed`, `agent.called`, `run.end` appear in that order.
- **AC2:** Given `run.start`, then it holds the provider, model, prompt version, threshold, and SHA-256 and byte size of the diff.
- **AC3:** Given two runs with one path, then the file holds both, appended, with different `run_id`s.
- **AC4:** Given neither flag nor variable, then it writes `.review-bot/audit.jsonl` under the working directory. `REVIEW_BOT_AUDIT_LOG` and the flag follow the [settings](../design.md#settings) precedence.
- **AC5:** Given a skipped file and a mismatched hunk, then `parse.parsed` lists the file with its reason and the hunk. Given all files skipped, then `agent.skipped` has reason `nothing_to_review` and there's no `agent.called`.
- **AC6:** Given a secret hit, then `static.secrets` has its kinds and diff lines, `agent.skipped` has reason `secret_detected`, `run.end` has 16, and the file has no `V`, including `V` in a `+++ b/` path or in the diff's own path.
- **AC7:** Given each exit from 3 to 15, then the last record is `run.end` with that code; provider failures also give `agent.failed` with their category. Given an unexpected `RuntimeError("SENTINEL")`, then `run.end` has code 14 and `RuntimeError`, with no `SENTINEL`.
- **AC8:** Given a run with findings holding `SENTINEL-RATIONALE` and a diff with `SENTINEL-DIFF`, then neither appears anywhere in the file, and stdout and stderr are as without the log, apart from `run_id=`.
- **AC9:** Given `--audit-log` on an unwritable path (a directory, or a missing parent that can't be created), then it exits 17 with one `review-bot: error:` line naming the path and `--audit-log`, stdout is empty, the diff isn't read and the provider isn't called, even if the diff is missing.
- **AC10:** Given a log that fails during the run, before stdout, then it exits 17, stdout is empty. Given a failed review whose `run.end` can't be written, then its own code stands.
- **AC11:** Given a valid review with `run.end` failing after stdout, then it exits 17 and stdout is intact.
- **AC12:** Given a run, then stderr's summary ends with `run_id=<the run's id>`, and it equals the log's `run_id`.
- **AC13:** Given the default path and no `.review-bot/`, then the run creates it with a `.gitignore` containing `*`, so the log is ignored in any repository. Given the directory already exists, or a custom path, then no `.gitignore` is written.

## Interfaces
`AuditLog`: [design.md](../design.md#interfaces). Tests read the file; `ts` is only checked for format and UTC.

## Out of scope
- Provider warnings and diagnostics (P1-S5), verify and policy records (P1-S6), triage labels (P1-S12): each adds its own.
- Rotation and retention.

## Notes
- **Never in the log:** diff lines, rationale text, model text, secret values. Decision: [ADR 0005](../../../adr/0005-audit-log.md).
- `tests/conftest.py` strips `REVIEW_BOT_*`, so tests would write `.review-bot/` into the repository: direct the log to a temporary path for the whole run, including child processes.
- A usage error (2) and `--help` write no record: settings aren't known yet.
- **`.gitignore` (main session):** add `.review-bot/` to this repository's `.gitignore`.
- **README (main session):** add exit 17 at line 16, and describe `--audit-log` in Usage. Add the audit log's location near line 99.
