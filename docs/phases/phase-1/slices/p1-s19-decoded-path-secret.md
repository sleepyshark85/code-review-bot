# P1-S19: Stop a secret spelt in a quoted path's escapes from reaching stderr or the model

- **Status:** Draft
- **Depends on:** P1-S4
- **Touches:** `diff_parser`, `review`, `cli`

## Value
A secret spelt in git's octal escapes in a file path is caught like a plain one: the run exits 16, the diff never reaches the model, and no stderr line shows a path's secret.

## Acceptance criteria
Every secret here is assembled at test run time, as in [P1-S3's Notes](p1-s3-secret-block.md#notes). `V` is the assembled value, and `E(x)` is `x` with every byte written as a three-digit octal escape, built at run time too. A file's **header lines** run from its file-start line to its first `@@`, or its end. The **decoded paths** are (a) every `path` and `old_path` ([P1-S2 Interfaces](p1-s2-parse-and-skip.md#interfaces)) the parser yields, quoted or not, however it picks the token, each scanned on its own, and (b) every other C-quoted string (`"` … `"`, with `"` and `\` inside only as escapes) in the header lines, read left to right and decoded the same way. Each names the diff line it was read from.
- **AC1:** Given issue #24's repro, a diff holding only a new docs file whose 1st line is `diff --git "a/docs/E(V).md" "b/docs/E(V).md"` and whose 5th line is `+++ "b/docs/E(V).md"`, when run with `--provider mock`, then it exits 16, stdout is empty, `MockProvider.requests` is empty, and stderr is exactly the [P1-S3 error line](p1-s3-secret-block.md#interfaces) for 3 hits: `aws-access-key` at diff lines 1, 1 and 5. That line equals the one for the same diff with the plain path `docs/V.md`.
- **AC2:** Parametrised over where the quoted path sits: each of `diff --git`'s two paths; `diff --cc`; `--- "a/…"`; `+++ "b/…"`; `rename from`, `rename to`, `copy from`, `copy to`; each path of a `Binary files "a/…" and "b/…" differ` line; a plain unified diff with no `diff --git`; and a reviewed file (`src/E(V).py`) as well as a skipped one, then it exits 16 with no provider call, naming the diff line that holds the quoted path, and `V` and `E(V)` are in neither stdout nor stderr.
- **AC3:** Parametrised over the seven [P1-S3 kinds](p1-s3-secret-block.md#interfaces), given a skipped file's quoted path `"b/docs/E(F).md"`, where `F` is a detected form of that kind and every byte of it is escaped, then it exits 16 naming that kind.
- **AC4:** Given only `V`'s first byte escaped (`"b/docs/\101KIA…"`), an invalid UTF-8 byte right before `V` (`"b/docs/\377E(V).md"`), and an escaped line feed before `V` (`"b/docs/x\nE(V).md"`), then each exits 16, and each hit names the diff line holding the quoted path.
- **AC5:** Given a quoted path that decodes to no secret, such as `"b/docs/caf\303\251.md"`, or to a P1-S3 near miss (`AKIA` plus 15 characters), then the run behaves as before this slice: [P1-S2 AC9](p1-s2-parse-and-skip.md#acceptance-criteria) for a skipped file, a normal review otherwise.
- **AC6:** Given a raw-text hit on an added line of `app.py` and `E(V)` in a skipped file's quoted path, then the error line names only the raw hit (`1 possible secret`). Decoded paths are scanned only when the raw-text scan finds nothing, so no secret is counted twice. With the raw hit removed, a rerun names the path's hits.
- **AC7:** Given AC1's diff with `--audit-log PATH`, then the records are `run.start`, `static.secrets` (the kinds and diff lines of AC1), `agent.skipped` with reason `secret_detected`, and `run.end` with 16, in that order, as for a raw hit ([P1-S4 AC6](p1-s4-audit-log.md#acceptance-criteria)); there's no `parse.parsed`; and the file holds neither `V` nor `E(V)`.
- **AC8:** Given a skipped file at `docs/-----END RSA PRIVATE KEY-----.md`, which the scan doesn't flag, then it exits 0 and the [P1-S2 AC9](p1-s2-parse-and-skip.md#acceptance-criteria) warning names that file `[REDACTED].md`: each named path passes `redact`, and an `END` alone is redacted from the path's start ([P1-S3 AC7](p1-s3-secret-block.md#acceptance-criteria)).
- **AC9:** Parametrised over exits 2, 3, 4, 5, 6 and 15, given a path holding `V` on the command line (for exit 2, a stray extra argument: `review-bot x.diff tmp/V.diff`; otherwise the diff's own path, such as `tmp/V.diff`), then the error line shows `[REDACTED]` where `V` was, and stderr has no `V`. Such a path is redacted, not scanned: it never reaches the model.
- **AC10:** Given a `diff --git` line whose quoted strings, read left to right, pair differently from the parser's pick of the new path, `diff --git "x" "y\" "b/src/E(V).py"`, then `new file mode 100644` and a hunk adding one line, then it exits 16 with no provider call, naming diff line 1. Today the provider is called.
- **AC11:** Property, over generated diffs with quoted, escaped and malformed header paths: whenever a `path` or `old_path` that `parse_diff` yields holds a detected form, the run exits 16 with no provider call.
- **AC12:** Given an unquoted parsed path that the raw-text scan misses because it starts mid-line, `rename to -V.md` with `V` an `aws-access-key` (mid-line, the `-` fails P1-S3's prefix boundary, but at the path's start it counts as a marker) or `rename to password: "L".md` with `L` a 16-character literal holding a digit, assembled like `V` (the YAML form is anchored at a line start), each in a pure rename (`diff --git`, `similarity index 100%`, `rename from x.md`, then that `rename to` line, with no `---`/`+++` lines), then it exits 16 with no provider call, naming the `rename to` line.

## Limits
- **Input:** the diff up to the [size limit](../../phase-0/design.md#settings) (default 400,000 bytes, at most 100,000,000). The largest case is one 400 KB header line.
- **Cost:** linear in the header lines, plus P1-S3's cost to scan what they decode, on hostile input: a 400 KB header line of `\101` escapes; one of `\"` pairs with no closing quote; one of thousands of short quoted strings; and 10,000 files each with quoted paths.
- **Malformed quoting:** a string with no closing quote, or ending in a lone `\`, isn't decoded, so only the raw-text scan covers it. An escape that is neither a three-digit octal from `\000` to `\377` nor a named control escape keeps its character: `\q` gives `q`, `\8` gives `8`, `\400` gives `400`. Every path the parser yields, quoted or not, is scanned exactly as yielded (AC11, AC12). Neither case fails the run.

## Interfaces
Error line: [P1-S3](p1-s3-secret-block.md#interfaces). Scan order: [design.md](../design.md#data-flow). Records: [design.md](../design.md#audit-log).

## Out of scope
- Escaped or encoded secrets in hunk lines or the preamble (string escapes, base64): the raw-text scan covers them as written, as for any [unlisted form](../design.md#secrets).
- A bypass for a false positive ([open question 7](../design.md#4-open-questions)).

## Notes
- **Fixes:** GitHub issue #24. Decision: the user, 2026-11-06, redact and block. Raw-clean-only scanning (AC6) and the out-of-scope hunk escapes were accepted by the main session, 2026-11-06.
- **Changes to Phase 1 criteria:** none are renumbered or replaced. This extends P1-S3 AC2's locations with decoded paths, and P1-S2 AC9's warning now passes each path through `redact`.
- **README (main session):** line 22: add that file paths in the headers are also scanned as decoded.
