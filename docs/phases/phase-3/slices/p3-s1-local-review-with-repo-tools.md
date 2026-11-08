# P3-S1: Review a local diff with read-only repository tools, offline

- **Status:** Approved
- **Depends on:** none
- **Touches:** `git` (moved from `pr.git`), `context` (`paths`, `tools`, `ledger`), `providers` (`base`, `mock`), `prompt`, `review`, `report`, `render`, `audit`, `settings`, `cli`

## Value
A developer can review a diff with the model able to read, search and list the repository at a commit, and see every lookup it made.

## Acceptance criteria
`review-bot DIFF --context-repo DIR [--context-rev REV]` with a git repository in `tmp_path` and a `MockProvider` scripted with tool calls ([design](../design.md#the-tools)). The fixture commit `C` holds `billing/invoice.py` (50 lines) and `billing/checkout.py` (30 lines; line 12 is `charged = invoice.total() * (1 + invoice.tax_rate)`).
- **AC1:** Given the sample diff, `--context-repo DIR`, and a mock that calls `read` with `{"path": "billing/checkout.py", "start_line": 10, "line_count": 5}` and then returns one valid finding, then the request's tools are exactly `read`, `grep` and `glob`; the result shows lines 10 to 14 of that file at `C`, each as `<line>\t<text>`; and the run exits 1 with the finding.
- **AC2:** Given an uncommitted edit to `billing/checkout.py` in `DIR`, then `read` returns the committed text. Given `--context-rev` naming an older commit, then files come from that commit.
- **AC3:** Given `grep` with `{"literal": "total("}`, then each matching line is `path:line:text`, ordered by path then line. `path_glob` limits the files, matched in our code with P2-S9's glob rules, and `ignore_case` true matches `TOTAL(`. Given `{"literal": "a.b"}`, then a line holding `axb` doesn't match.
- **AC4:** Given `glob` with `{"pattern": "billing/*.py"}`, then both files are listed, sorted, one per line; `**/*.py` also lists files at any depth.
- **AC5:** Given `start_line` past the file's end, then the result says so and holds no lines. Given `line_count` 401, an unknown key, a wrong type or a missing required key, then the call is refused `invalid_arguments`. Given an unknown tool name, it's refused `unknown_tool`. Given a path with no file, `not_found`; a directory, `directory`. The review goes on after any refusal.
- **AC6:** Given a line over 2,000 characters, then it's cut and marked `(cut)`. Given a result over `max_tool_result_bytes`, then it ends at a line boundary within the limit, with a note, and the outcome is `truncated`. Given more than 50 `grep` matches or 200 `glob` paths, then the first are returned, with a note, outcome `truncated`.
- **AC7:** Given `--max-tool-calls 2` and a mock making 3 calls, then the third is refused `call_limit`, and git isn't run for it. Given `--max-tool-total-bytes 3000` and two reads of 2,000 bytes each, then the second is refused `byte_limit`.
- **AC8:** Given any result, refusals included, then it is wrapped in an opening and a closing delimiter line that carry the SHA-256 of the text between them. Given a file holding a line that imitates a closing delimiter, then that line is inside the wrapping, and the real closing line's hash differs from it.
- **AC9:** Given a run with 3 tool calls, then the audit log has 3 `context` `tool_call` records, each with `tool`, `outcome`, `reason`, `spans` (path, first and last line), `result_bytes` and `input_sha256`. A unique string from the read file and the grep literal appear nowhere in the log.
- **AC10:** Given `--format json`, then `report/v1` has `mode` `context`, `prompt_version` `p3-v1`, and `context` with `commit` (40 hex), `tool_calls`, `refused`, `result_bytes` and `files_read`. stderr's summary line appends `tool_calls=3`. Text output is otherwise unchanged.
- **AC11:** Given no `--context-repo`, then the request has no tools, its system and user prompts are byte-for-byte Phase 1's (`p1-v1`), `mode` is `diff` and `context` is null.
- **AC12:** Given `--context-repo` naming a directory that isn't a git repository, or `--context-rev` naming no commit, or `--context-rev` without `--context-repo`, then it exits 2 naming the flag, before any provider call.

## Limits
Tool arguments come from the model, so they're hostile: each is checked against its schema before git runs, and strings are capped at 1,000 characters. Every git command has the existing timeout, and output is read only up to the cap plus one byte. Hostile paths and repositories are S2's.

## Interfaces
- Arguments: `read` `{path, start_line?, line_count?}`; `grep` `{literal, path_glob?, ignore_case?}` (literal 1 to 200 characters, no line break); `glob` `{pattern}` (1 to 200 characters).
- A refusal's text is `refused: <reason>: <one sentence>`. Reasons from this slice: `invalid_arguments`, `unknown_tool`, `not_found`, `directory`, `call_limit`, `byte_limit`.
- `MockProvider(tool_calls=[(name, arguments), ...])` makes each call in order through `request.tools`, keeps each result in `tool_results`, then answers as scripted.
- Settings and flags: `context_repo`, `context_rev` (default `HEAD`), `max_tool_calls` (12), `max_tool_result_bytes` (16,000), `max_tool_total_bytes` (96,000), with [design ranges](../design.md#spend-and-caps).

## Out of scope
- Hostile paths, symlinks, secrets (S2); Claude (S3); evidence (S4); pull requests (S8).

## Notes
- **README (main session):** describe `--context-repo` and `--context-rev`. Reword Phase 3 at README.md:154 and :156: the tools are our own read-only `read`, `grep` and `glob` over a git commit ([ADR 0013](../../../adr/0013-read-only-repo-tools-over-a-commit.md)), and the context behind each finding is recorded by the tools, with hooks enforcing the allowlist ([ADR 0014](../../../adr/0014-tools-as-a-provider-neutral-executor.md)).
- **The `pr.git` → `git` move is mechanical:** the module moves and imports change, with no behaviour change in the same commit; new methods come in separate commits.
- git runs through `asyncio.to_thread` inside `RepoTools.call` (CONVENTIONS §10).
