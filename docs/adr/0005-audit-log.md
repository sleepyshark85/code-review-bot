# 0005: Keep an always-on JSONL audit log that holds decisions, not content

- **Status:** Provisional
- **Date:** 2026-11-05
- **Phase:** 1

## Context
[README Phase 1](../../README.md#phase-1-trustworthy-core) requires a JSONL record of every stage decision, and auditors and Phase 2's feedback data will read it. That format is costly to change once consumers exist. The log may also be shared more widely than a developer's machine, while diffs are confidential and may hold secrets the scanner can't recognise.

## Decision
Every run appends versioned records to a local JSONL file, on by default, and an unwritable log stops the run before the diff is read. Records hold decisions, identifiers and hashes, never diff lines, rationale text or model-chosen strings such as a file name that isn't in the diff. The one exception is scrubbed, size-capped CLI diagnostics on a failed call, pending the [user's decision](../phases/phase-1/design.md#4-open-questions).

## Consequences
- Every model call has a trace, including failed and aborted ones.
- The log alone can't show what a finding said. Readers join to a saved JSON report by fingerprint and run identifier, and text-format runs keep no rejected text, which limits Phase 2's dataset ([open question 9](../phases/phase-1/design.md#4-open-questions)).
- Each run writes a file into the working directory, so the default directory ignores itself in any repository, and a read-only directory needs the path setting.
- Scrubbing is best-effort: a short fragment, a reworded diff line or an unlisted secret format can pass through diagnostics.
- Rotation and retention are not handled.

## Alternatives considered
- **Opt-in log:** avoids writing files, but an audit trail that depends on remembering a flag isn't one.
- **Continue when the log fails:** keeps reviews flowing, but allows untraced calls to a vendor.
- **Store rationale text:** convenient for Phase 2, but it can quote the diff or an undetected secret.
- **Discard CLI diagnostics:** the strictest option and the user's to choose, but failed calls stay undiagnosable.
- **SQLite or a logging service:** query power nobody needs yet, and JSONL is what the README names.
