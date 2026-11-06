# P1-S4: Trace every run in a JSONL audit log

- **Status:** Done
- **Depends on:** P1-S3
- **Touches:** `audit` (new), `review`, `settings`, `cli`, `errors`, `diff_parser`

## Value
An auditor can reconstruct what each run decided, from one file, without it holding the code under review.

## Acceptance criteria
Records and stages: [design.md](../design.md#audit-log). `SENTINEL-DIFF` is in the diff, and `V` is an assembled secret.
- **AC1:** Given a successful mock review with `--audit-log PATH`, then every line of `PATH` is a JSON object with `schema` `audit/v1`, one `run_id`, a UTC `ts`, and a `stage`, `event` and `data`; and `run.start`, `parse.parsed`, `agent.called`, `run.end` appear in that order.
- **AC2:** Given `run.start`, then it holds the provider, model, prompt version, threshold, and SHA-256 and byte size of the diff.
- **AC3:** Given two runs with one path, then the file holds both, appended, with different `run_id`s.
- **AC4:** Given neither flag nor variable, then it writes `.review-bot/audit.jsonl` under the working directory. `REVIEW_BOT_AUDIT_LOG` and the flag follow the [settings](../design.md#settings) precedence.
- **AC5:** Given a skipped file and a mismatched hunk, then `parse.parsed` lists the file with its reason, and the hunk by file, hunk index (0-based within its file) and declared new start; a hunk with an unreadable header has a new start of `null`, and no header text is written. Given a skipped file or a flagged hunk in a deleted file (`+++ /dev/null`), then its `file` is the old path from `--- a/`, not `null`. Given 51 skipped files and 51 flagged hunks, then it lists the first 50 of each in diff order, plus totals of 51 and 51. Given all files skipped, then `agent.skipped` has reason `nothing_to_review` and there's no `agent.called`. Given a skipped file whose quoted path decodes `\377` (`+++ "b/docs/caf\377.md"`), then the run doesn't exit 17, the file is pure ASCII, every line parses, and that entry, parsed, equals the JSON `{"file": "\"docs/caf\\377.md\"", "reason": "docs", "path_quoted": true}` ([paths](../design.md#audit-log)). Given a valid UTF-8 path, such as `docs/café.md`, then `file` is the plain path and the entry has no `path_quoted` key.
- **AC6:** Given a secret hit, then `static.secrets` has its kinds and diff lines, `agent.skipped` has reason `secret_detected`, `run.end` has 16, and the file has no `V`, including `V` in a `+++ b/` path or in the diff's own path.
- **AC7:** Given each exit from 3 to 15, then the last record is `run.end` with that code; provider failures also give `agent.failed` with their category. Given an unexpected `RuntimeError("SENTINEL")`, then `run.end` has code 14 and `RuntimeError`, with no `SENTINEL`.
- **AC8:** Given a run with findings holding `SENTINEL-RATIONALE` and a diff with `SENTINEL-DIFF`, then neither appears anywhere in the file, and stdout and stderr are as without the log, apart from `run_id=`.
- **AC9:** Given `--audit-log` on an unwritable path (a directory, or a missing parent that can't be created), then it exits 17 with one `review-bot: error:` line naming the path and `--audit-log`, stdout is empty, the diff isn't read and the provider isn't called, even if the diff is missing.
- **AC10:** Given a log that fails during the run, before stdout, then it exits 17, stdout is empty. Given a failed review whose `run.end` can't be written, then its own code stands.
- **AC11:** Given a valid review with `run.end` failing after stdout, then it exits 17 and stdout is intact.
- **AC12:** Given a run, then stderr's summary ends with `run_id=<the run's id>`, and it equals the log's `run_id`.
- **AC13:** Given the default path and no `.review-bot/`, then the run creates it with a `.gitignore` containing `*`, so the log is ignored in any repository. Given the directory already exists, or a custom path, then no `.gitignore` is written.

## Limits
- **Input:** a diff up to the [size limit](../../phase-0/design.md#settings) (`--max-diff-bytes`, default 400,000, at most 100,000,000); a larger one exits 6 unparsed, so it has no `parse.parsed`. `parse.parsed` lists at most 50 skipped files and 50 flagged hunks, plus totals (AC5), so its entry count doesn't grow with the diff.
- **Cost:** linear in the diff. Records come from the run's parse, scan and results, with no rescan of the diff per record.
- **Hostile values:** a path holding control characters, newlines, quotes or a secret still gives one JSON object per line (AC1), and passes `redact` first ([secrets](../design.md#secrets)), so no `V` remains (AC6). Records are escaped to ASCII and hold no lone surrogates. A path that isn't valid UTF-8 is written in git's C-quoted form with `path_quoted: true` ([paths](../design.md#audit-log)), keeping every byte, and never stops the run (AC5). Skip reasons and error categories come from fixed sets; exception text, rationale and diff lines are never written (AC7, AC8).

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
- **Implementation (developer):** `run.start` is written once the diff loads, as it holds the diff's hash, so a run that exits 3 to 6 has only `run.end`. `static.secrets` is written only on a hit. Every string in a record passes `redact`, as does the path in an exit-17 message. The parser changes this slice needs (`Hunk.new_start`, a deleted file's old path, undecodable path bytes kept) are in [P1-S2's interfaces](p1-s2-parse-and-skip.md#interfaces). `review_diff` is renamed `run_pipeline` ([interfaces](../design.md#interfaces)). If a failed run can't record `run.end`, its code stands and stderr gets one `review-bot: warning:` line after the error.
- **Paths (developer, review round 1):** the parser keeps a path's undecodable bytes as lone surrogates (`surrogateescape`), and `audit.file_fields` turns them back into bytes and C-quotes them. `AuditLog.record` raises `ValueError` on any lone surrogate, so none can reach the log. `old_path` also reads `rename from`, and is `None` for `new file mode` or `--- /dev/null`. A section whose headers both say `/dev/null` has no path, so its `file` is `/dev/null`. A run aborted by `SystemExit` or `KeyboardInterrupt` records that class in `run.end`.
