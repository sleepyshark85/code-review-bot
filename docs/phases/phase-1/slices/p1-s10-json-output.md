# P1-S10: Get the review as JSON

- **Status:** Approved
- **Depends on:** P1-S9
- **Touches:** `render`, `settings`, `cli`

## Value
A script or later pipeline can consume findings, with fingerprints and source, while existing text users see no change.

## Acceptance criteria
With a scripted `MockProvider`. Schema in Interfaces.
- **AC1:** Given `Assignment/sample_diff.txt`, `--format json` and no AI findings, then stdout is exactly one parseable JSON document with `schema` `report/v1`, and its `findings` are the two rule findings, each with `fingerprint`, `source` `rule`, `rule`, `file`, `line`, `severity`, `category`, `rationale`, `confidence`, `triage` null and `original_rationale` null. Exit 1.
- **AC2:** Given a valid AI finding, then it follows them with `source` `ai`, and `rule` null if the model gave none.
- **AC3:** Given a run with a dropped, a deduped and a below-threshold finding and a skipped file, then `counts` shows `reported`, `below_threshold` 1, `dropped` 1, `deduped` 1, `rejected` 0, `undecided` 0, and `skipped_files` lists the file with its reason. Dropped, deduped and below-threshold findings aren't in `findings`.
- **AC4:** Given no findings, then `findings` is `[]`, exit 0, and the document is still complete. Given all files skipped, the same with `usage` zeros.
- **AC5:** Given a rationale holding `\x1b[31m`, a newline, U+2028, `→` and a quote, with a path holding the same via a diff header, then stdout is pure ASCII and one line, and the parsed values equal the originals.
- **AC6:** Given the same run twice, with a fixed clock and `run_id` aside, then the two documents are identical, with stable key order.
- **AC7:** Given any exit from 2 to 17 other than a delivered report, then stdout is empty, in every format, and stderr is as with `text`.
- **AC8:** Given stdout closed or failing, then it exits 14 as P0-S3 AC4 requires, with one error line.
- **AC9:** Given no `--format`, or `--format text`, then stdout is byte-identical to before this slice, for the goldens in `tests/fixtures/`. Stderr's summary is unchanged by format.
- **AC10:** Given `--format yaml`, or `REVIEW_BOT_FORMAT=yaml`, then it exits 2 and names the source. The flag beats the variable.
- **AC11:** Given `--format json`, then the document's `run_id` equals the summary's `run_id=` and the log's.

## Interfaces
`render_json`: [module map](../design.md#module-map); `--format`: [settings](../design.md#settings).

`report/v1`, escaped to ASCII: `schema`, `run_id`, `provider`, `model`, `prompt_version`, `threshold`, `findings`, `counts` (`reported`, `below_threshold`, `dropped`, `deduped`, `rejected`, `undecided`), `skipped_files`, `usage`. `triage` values are in [P1-S12](p1-s12-triage.md#interfaces). `reported` excludes rejected findings.

## Out of scope
- Markdown (P1-S11); triage values (P1-S12) stay null here.

## Notes
- **README (main session):** describe `--format json` in Usage, and say text stays the default.
- **Non-UTF-8 paths (changed before build, main session, 2026-11-06):** a finding's `file`, or a skipped file's path, can hold lone surrogates for bytes that aren't valid UTF-8 (rule findings on such paths are built with `model_construct`). Write it as the audit log does: git's quoted form plus `path_quoted: true` on that object ([paths in `audit/v1`](../design.md#audit-log)). Trap: `Finding.model_dump_json()` raises `PydanticSerializationError` on such a path, turning exit 1 into 14.
