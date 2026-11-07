# P1-S12: Accept or reject findings interactively, with labels logged

- **Status:** Done
- **Depends on:** P1-S11
- **Touches:** `triage` (new), `cli`, `render`, `audit`

## Value
A developer makes the final call on each finding, and each call becomes a label Phase 2 can learn from.

## Acceptance criteria
`run_triage` takes injected `read_line` and `write_line`. For `main`, tests simulate terminals by replacing the streams' checks; one child-process test covers AC5 for real.

**Deciding**
- **AC1:** Given `--triage`, two findings and the inputs `a` then `r`, then each is shown once, with its file, line, severity, category, rationale and source; text and Markdown reports have only the accepted one; it exits 1.
- **AC2:** Given every finding rejected, then it exits 0, and text and Markdown show their "no findings" output. With `--format json`, the rejected findings stay in `findings` with `triage` `reject` and their rationale, `counts.reported` is 0 and `counts.rejected` is 2, so the text survives for a saved report.
- **AC3:** Given `--format json`, then an accepted finding has `triage` `accept`. Given no `--triage`, every finding's `triage` is null. Given `--triage`, none is null.
- **AC4:** Given input `x`, or an empty line, then it asks again, and the finding is still undecided.

**Never hangs**
- **AC5:** Given `--triage` with stdin or stderr not a terminal, including stdin from `/dev/null`, `NUL` on Windows (where `isatty` wrongly says true, so a console check is needed), or a pipe, then it exits 2 within 5 seconds, with one error line naming the flag and saying a terminal is needed; the diff isn't read and the provider isn't called. A terminal stdin and stderr with stdout redirected is allowed.
- **AC6:** Given EOF on stdin part-way, or Ctrl+C (`KeyboardInterrupt` from `read_line`), then no more prompts appear, remaining findings stay in the report with `triage` `undecided` and no label, `counts.undecided` holds their number, it exits 1, and there's no traceback.
- **AC7:** Given no `--triage`, including `REVIEW_BOT_TRIAGE=true`, then no run reads stdin or asks anything, in any format. Given no findings with `--triage`, then nothing is asked.

**Labels and safety**
- **AC8:** Given decisions, then the log has one `triage` record per decision with the Label fields, and no rationale text.
- **AC9:** Given a rationale or path holding `\x1b[31m` and a newline, then the prompt shows it on one line with no raw control characters.
- **AC10:** Given prompts and report, then prompts go only to stderr, and stdout holds only the report, written after the last decision.
- **AC11:** Given a secret hit, an invalid setting or a provider failure with `--triage`, then its own exit code stands and nothing is asked.
- **AC12:** Given findings below the threshold, then they aren't asked about.

## Interfaces
`run_triage(findings, *, read_line, write_line) -> TriageResult`, with `read_line` taking a prompt and returning a line, and `write_line` going to stderr. `TriageResult` holds the labels, plus each finding with its decision and any edits. Prompt: `[a]ccept [r]eject`. P1-S13 adds `[e]dit`.

- `Label`: `fingerprint`, `source`, `label` (`accept`, `reject`, `edit`), `severity_before`, `severity_after`, `rationale_changed`; the record adds `model` and `prompt_version`. This is the Phase 2 feedback seam. (`source` moved into `Label`: changed in build, main session, 2026-11-07.)
- JSON `triage`: null when triage didn't run; otherwise `accept`, `reject`, `edit` or `undecided`.
- Exit code with `--triage`: 1 if any reported finding wasn't rejected, including one undecided, otherwise 0.

Changed in build, main session, 2026-11-07:
- `read_line` raises `EOFError` at the end of input (AC6). `TriageResult` holds `findings` and `labels`.
- Answers are matched after stripping surrounding whitespace and lowercasing, so ` A ` accepts.
- Each label is a `triage`/`labelled` record ([audit log](../design.md#audit-log)). Labels are recorded right after triage, before the report is written, so a stdout failure doesn't lose them.

## Out of scope
- Editing (P1-S13), persisting decisions between runs (Phase 2).
- Rejected text in text or Markdown runs, or the log: [open question 9](../design.md#4-open-questions).

## Notes
- Ctrl+C during triage exits 1 because the review already exists. During the review it stays 14, as in Phase 0.
- **Gap:** CI runs on Linux, so AC5's `NUL` case runs only on Windows.
- `--triage` has no variable, although the CLI's help says each option can also be set by one: correct that help line.
- **README (main session):** describe `--triage` in Usage, and that it needs a terminal.
