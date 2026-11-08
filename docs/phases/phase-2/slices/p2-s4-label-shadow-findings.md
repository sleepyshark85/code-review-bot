# P2-S4: Judge would-be comments from shadow reports, and keep the labels

- **Status:** Draft
- **Depends on:** P2-S1
- **Touches:** `pr` (`label`, `feedback`, `cli`), `console`

## Value
The user turns downloaded shadow reports into accept and reject labels. These are the data the gate's precision is computed from.

## Acceptance criteria
`python -m review_bot.pr label --reports DIR --labels FILE`, with injected `read_line` and `write_line` as in [P1-S12](../../phase-1/slices/p1-s12-triage.md#interfaces). The unit is a would-be comment, grouped by repo, PR, key and index: [precision](../design.md#how-the-gate-is-evidenced), [ADR 0011](../../../adr/0011-feedback-labels.md).
- **AC1:** Given `DIR` holds, at any depth, three `shadow.json` files for one PR, with `create` entries for keys {K1, K2}, {K1, K2} and {K1, K3}, then exactly three would-be comments are asked about (K1, K2 and K3), each once. Each shows the PR, file, line, severity, source, rule and body from the latest report by `finished_at`, plus how many reports had it.
- **AC2:** Given entries for one key in different reports of one PR, with different fingerprints or lines (a rewording, or an ordinal shift), then they are one would-be comment. Given one report with two entries for one key (two identical lines), and another report with one, then the key has two would-be comments, indexed 0 and 1 in line order.
- **AC3:** Given input `a`, then one `feedback/v1` line with `channel` `shadow` and `label` `accept` is appended to `FILE` at once, before the next question.
- **AC4:** Given input `r`, then it asks for a reason, `[w]rong [n]ot worth it [l]ine wrong [d]uplicate`. The line it writes has `label` `reject` and `reason` `wrong`, `not_worth_it`, `wrong_line` or `duplicate`. Any other answer asks again.
- **AC5:** Given input `s`, then nothing is written, and that would-be comment is asked about again next session.
- **AC6:** Given `FILE` already labels (repo, PR, K1, index 0), then that would-be comment isn't asked about. The same key on another PR is a separate would-be comment.
- **AC7:** Given reports with `mode` `comment`, an `outcome` other than `reviewed`, or entries whose `action` isn't `create`, then they contribute nothing. Reports with `origin` `backfill` count like `event`.
- **AC8:** Given end of input or Ctrl+C part-way, then the lines already written stay, there's no traceback, and it exits 0. At the end, stderr shows the judged, skipped and remaining counts.
- **AC9:** Given stdin or stderr not a terminal, then it exits 2 at once, naming `label` and saying a terminal is needed, as in P1-S12 AC5.
- **AC10:** Given a record, then it has exactly these keys and nothing else: `schema`, `channel`, `repo`, `pr`, `fingerprint` (the latest entry's), `file`, `rule_part`, `anchor`, `index` (null for reaction and resolution records), `source`, `severity`, `label`, `reason` (null on accept), `run_id`, `model`, `prompt_version`, `recorded_at` (UTC). It never holds the body, the rationale or a username.
- **AC11:** Given a file in `DIR` that isn't JSON, has another `schema`, or is over 10 MB, then it is skipped with one stderr line naming it, and the count of skipped files is shown.
- **AC12:** Given a malformed line in an existing `FILE`, then it exits 2 naming the line number, and asks or appends nothing.
- **AC13:** Given a body holding `\x1b[31m` and a newline, then the prompt shows it with no raw control characters, as in P1-S12 AC9.

## Limits
Reports are files the user downloaded, so they are treated as untrusted. Each is parsed with a schema, is at most 10 MB, and is read in time linear in its size. Model text is shown only after control characters are neutralised.

## Interfaces
`feedback/v1`: AC10 is the full record. S8 adds the channels `reaction` and `resolution` and the labels `conflict` and `resolved`. Exit codes: 0 when done or interrupted, 2 for usage errors or an invalid labels file.

## Out of scope
- Computing precision (S10); reactions (S8); committing the labels file ([Q6](../design.md#5-open-questions), main session).

## Notes
- **README (main session):** describe `label`, and downloading artifacts with `gh run download`.
