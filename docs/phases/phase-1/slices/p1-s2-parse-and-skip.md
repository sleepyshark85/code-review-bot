# P1-S2: Skip docs, lock and generated files, and reject text that isn't a diff

- **Status:** Done
- **Depends on:** P1-S1
- **Touches:** `diff_parser` (new), `review`, `errors`, `cli`

## Value
A developer's review spends no tokens on files nobody wants reviewed, survives malformed hunk headers, and fails clearly when given something that isn't a diff.

## Acceptance criteria
**Parsing**
- **AC1:** Given `Assignment/sample_diff.txt`, whose headers declare 7 and 15 new lines over bodies of 3 and 12, when `parse_diff` runs, then there is one file, `billing/invoice.py`, with two hunks, both `has_count_mismatch`; the `new_line` values of its added and context lines are exactly 12 to 14 and 30 to 41; and added lines are 14, 32 to 36 and 38 to 41. The blank line before the second `@@` is not a body line; the blank before `def export_csv` is context, line 37.
- **AC2:** Given each of: a header with no counts (`@@ -1 +1 @@`); a `\ No newline at end of file` line; CRLF endings; a `--- x` then `+++ y` inside a hunk with no hunk header following, then numbering is correct and the file count doesn't change. A `---` and `+++` pair starts a new file only if a hunk header follows it.
- **AC3:** Given a hunk header whose new start is unreadable (`@@ garbage @@`), then parsing succeeds and that hunk's lines all have `new_line` of `None`.
- **AC4:** Given a rename, a new file (`--- /dev/null`), a deleted file (`+++ /dev/null`), a `--no-prefix` diff, a quoted path with octal escapes, and a plain unified diff with no `diff --git`, then `path` is the new path, or `None` for the deleted file.
- **AC5:** Given text with no file header, including `+x\n`, prose, or orphan `@@` hunks, then the run exits 15, stdout is empty, stderr has one `review-bot: error:` line naming the path, and the provider isn't called.
- **AC6:** Given a commit-message preamble before the first file, then the run isn't rejected and the preamble stays in `reviewable_text`.

**Skipping.** Path rules are in Interfaces.
- **AC7:** Given a mix of `app.py`, `README.md`, `uv.lock` and `src/pb/x_pb2.py`, then the request's `user_prompt` contains only the `app.py` section and the preamble, byte for byte.
- **AC8:** Given no skipped files, then the diff in `user_prompt` equals the input byte for byte, including CRLF.
- **AC9:** Given a diff where every file is skipped, then the provider isn't called, it exits 0, stdout is `No findings at or above low.`, the summary has `skipped_files=N model_called=false`, and stderr has `review-bot: warning: nothing was reviewed: every changed file was skipped: <paths>`, naming up to 10 paths, then a count.
- **AC10:** Given `app.py` whose added lines say `# @generated` or `DO NOT EDIT`, then it is still reviewed: content can't opt a file out. Given a `Binary files a/x.bin and b/x.bin differ` section, then it is skipped as binary.
- **AC11:** Parametrised over `requirements.txt`, `requirements-dev.txt`, `CMakeLists.txt`, `docs/conf.py`, `src/build/x.py`, `src/dist/x.py`, then each is reviewed. Parametrised over `notes.txt`, `docs/guide.md`, `build/x.py`, `dist/x.py`, `src/vendor/x.py`, then each is skipped.

## Interfaces
`parse_diff` raises `DiffNotParsableError` (15). Summary: [compatibility](../design.md#compatibility).

- `ParsedDiff`: `files`, `reviewable_text` (the input minus skipped file sections, otherwise byte for byte).
- `FileDiff`: `path` (new path, one `a/` or `b/` stripped; `None` if deleted), `hunks`, `skip_reason` (`SkipReason`: `docs`, `lock`, `generated`, `binary`; or `None`).
- `Hunk`: `header`, `lines`, `has_count_mismatch` (declared counts differ from the body, or are unreadable).
- `DiffLine`: `kind` (`LineKind`: `added`, `context`, `removed`), `text` (no marker or line ending), `new_line`.

| Skip | Paths |
|---|---|
| Docs | `.md`, `.rst`, `.adoc`, `.txt`, except `requirements*.txt` and `CMakeLists.txt`; `LICENSE*` and `CHANGELOG*` only with no extension or a docs extension |
| Lock | `uv.lock`, `poetry.lock`, `Pipfile.lock`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`, `go.sum`, `composer.lock`, `Gemfile.lock` |
| Generated | `*.min.js`, `*.min.css`, `*.map`, `*_pb2.py`, `*_pb2_grpc.py`, `*.pb.go`; under a root `dist/` or `build/`; under a `vendor/` or `node_modules/` at any depth |

## Out of scope
- Secret scan (P1-S3), audit records (P1-S4), checking findings against the parse (P1-S6).

## Notes
- **Handovers:** P0-S1 AC12's non-diff case is replaced by AC5, and AC11 needs a limit that fits a real diff. `write_diff("+x\n")` is a literal at about 60 sites, and `claude_argv` writes it too: replace them with one shared constant for a real minimal diff, in one change. `tests/fixtures/p0_s1/prompt_injection.diff` targets `notes.md`, now skipped, so P0-S1 AC13's tests must retarget it to a code path.
- **Test dependency:** the developer adds `hypothesis` (CONVENTIONS §14) for the parser's tests; the tester can't edit `pyproject.toml`.
- **README (main session):** add exit 15 at line 16, and say non-diff text is rejected.
- **Implementation choices (developer):**
  - The AC9 warning ends `<up to 10 paths, comma-separated> (<N> in total)`. A skipped deleted file keeps `path` `None` and is named `(deleted file)`. It is classified by its old path: the `---` line, or else the `diff --git` line.
  - The summary always appends `skipped_files=N model_called=true|false`. When the model isn't called, the mock's "canned" warning is left out, since no canned output was produced.
  - The shared minimal diff is `MINIMAL_DIFF` in `tests/unit/diff_samples.py`, the only test path the developer can write to. Integration tests import it through `tests/integration/diff_support.py`. A tests-root module owned by the tester would be the better home.
  - **File starts:** if the text has any `diff --git` line, only `diff --git` lines start files, so hunk lines can't fake a file header. A binary file is one with a `Binary files … differ` or `GIT binary patch` line, and its path comes from the `diff --git` line.
  - **Plain unified diffs (decided by the main session, 2026-11-06):** without a `diff --git` line, a file header inside a hunk can't be told apart from a real one, so such a diff skips no files: every section is reviewed. This fails closed, costing tokens on docs in rare plain diffs, instead of letting a faked header hide code.
- **Changed during review (main session, 2026-11-06):** `CHANGELOG.py`, `LICENSE.js` and `x.generated.py` are reviewed, so naming a file can't hide its code. A deleted file is skipped by its old path.
