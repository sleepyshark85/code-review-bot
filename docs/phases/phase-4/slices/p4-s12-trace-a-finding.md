# P4-S12: Trace one finding from its inputs to its gate decision

- **Status:** Approved
- **Depends on:** P4-S4, P4-S9, P4-S10 (and through them S1, S5 and S7)
- **Touches:** `trace.lineage`, `trace.cli` (new), `review` (the `policy`/`decided` record), `audit`

## Value
An auditor gives a finding's fingerprint and gets everything that produced it and decided its fate: the policy, the models tried, the prompt, the inputs' hashes, redaction, the tool calls behind its evidence, and the threshold, gate and override decisions. All of it comes from the audit logs already kept, and none of it is code or model text.

## Acceptance criteria
Rules: [design, Tracing and SLOs](../design.md#tracing-and-slos).
- **AC1:** Given the audit log of a context run in which finding `F` was reported, when `python -m review_bot.trace --audit-log L --run-id R --fingerprint F` runs, then it prints: `run_id`, bot version, policy version and SHA-256, each attempt's route and category, prompt version and fingerprint, `diff_sha256`, redaction counts, every tool call (tool, outcome, spans) whose spans cover one of F's kept evidence spans, verify outcome, threshold outcome, and gate decision, in that order. It exits 0.
- **AC2:** Given `--format json`, then it writes one `trace/v1` document with the same fields.
- **AC3:** Given a second `--audit-log` holding the merge gate's records for the same PR, in which F was overridden, then the trace ends with the override's reply ID and login.
- **AC4:** Given a fingerprint the run didn't report, then exit 1: "Run R has no finding F." The same if the run ID isn't in any log.
- **AC5:** Given a log line that isn't JSON or isn't `audit/v1`, then exit 2, naming the file and line number.
- **AC6:** Given any run from this slice on, then each `policy`/`decided` record carries `fingerprint`.
- **AC7:** Given any trace, then its output holds no diff line, rationale, suggestion, tool result or masked value: only hashes, counts, paths and line numbers, as in the audit log ([ADR 0005](../../../adr/0005-audit-log.md)).
- **AC8:** Given a Phase 3 log, from before this slice, then the fields it lacks are shown as `not recorded`, and the trace still exits 0.

## Limits
Audit logs are read as a stream, each up to 100 MB, with lines up to 1 MB. A longer line exits 2. Paths are shown as the audit log stores them.

## Interfaces
`python -m review_bot.trace --audit-log PATH [--audit-log PATH ...] --run-id ID --fingerprint 16HEX [--format text|json]`.

## Out of scope
- Exporting spans to OpenTelemetry ([Q8](../design.md#6-open-questions)).

## Notes
