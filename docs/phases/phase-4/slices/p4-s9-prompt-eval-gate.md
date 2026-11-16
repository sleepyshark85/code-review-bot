# P4-S9: Gate prompt versions and models on evals

- **Status:** Done
- **Depends on:** P4-S7
- **Touches:** `prompt`, `evals.gate` (new), `evals.cli`, `evals.replay`, `evals.live`, `tests/evals/gate.toml` (new), `.github/workflows/ci.yml` and `.github/review-bot-policy.toml` (main session, from this spec)

## Value
No prompt text changes without a new version, and no model reviews code in CI unless its live recordings pass the same eval cases as the default model's. CI checks both on every PR, so a release can't ship an ungated prompt or model ([ADR 0007](../../../adr/0007-evals-replay-and-live.md), [ADR 0021](../../../adr/0021-route-on-availability-failures-then-rules-only.md)).

## Acceptance criteria
Rules: [design, Versioned prompts and the eval gate](../design.md#versioned-prompts-and-the-eval-gate).
- **AC1:** Given `PROMPT_FINGERPRINTS` pinning `p1-v1` and the current context version, when the diff-mode system prompt's text changes but `PROMPT_VERSION` doesn't, then `evals gate` exits 1, naming the version: "its text changed; give it a new version".
- **AC2:** Given `tests/evals/gate.toml` with one suite (`claude`, `claude-opus-5-5`, `diff`, cases `sample`, `clean`, `injection-instruction`, `injection-canary` and `injection-delimiter`, and an accepted miss `sample`/`double-tax` with `approved_by` and `approved_on`), then `evals gate --cases tests/evals/cases --suites tests/evals/gate.toml` exits 0 on today's committed recordings.
- **AC3:** Given a suite case whose live recording is missing, stale (naming the stale fields) or by another model, or a miss that isn't accepted, then exit 1, naming the suite, the case and the reason.
- **AC4:** Given `--policy` listing `openai:m1` for `diff` with no suite for it, then exit 1, "no suite for openai:m1 in diff mode". Given its suite lacks a case that the default model's diff suite has, then exit 1, naming the missing cases.
- **AC5:** Given `evals live --provider openai --model m1`, then recordings go to `recordings/live-openai/recording.json` (context: `live-openai-context/`), with `model` `m1`. `replay --provider openai` scores them, and Claude's paths are unchanged.
- **AC6:** Given an invalid `gate.toml` (unknown key, missing field, unknown case ID, an accepted miss without `approved_by`), then exit 2, naming the key or case.
- **AC7:** Given a run, then it prints one line per suite (cases, fresh, passed, accepted misses, verdict) and a final verdict. `--format json` writes `eval-gate/v1`.
- **AC8:** Given `ci.yml`, then the `gate` job runs `evals gate` with `--policy .github/review-bot-policy.toml`. That policy has `version`, the three tools, `[cost] max_cost_usd = "0.60"` (CI's context cap, now also a ceiling on diff-mode calls), and one `[[models]]` entry, `claude:claude-opus-5-5`, and no other section. Once S10 passes it, CI's review runs report the same findings as before, and `caps` shows the ceiling. The tester checks both files by parsing them.
- **AC9:** Given the context suite isn't in `gate.toml` until P3-S15 adds it, then the entry lists `modes = ["diff"]` until then, and `context` is added together with its suite (see Out of scope). The tester checks that whenever the entry lists `context`, `gate.toml` has a Claude context suite. S10's wiring merges only after that ([S10](p4-s10-merge-gate-check.md)).

## Limits
`gate.toml` is repository input, at most 64 KB, parsed strictly. Recordings are read under the existing 10 MB per-file cap.

## Interfaces
`python -m review_bot.evals gate --cases DIR --suites FILE [--policy PATH] [--format text|json]`, exiting 0 (passed), 1 (failed) or 2 (invalid input). `gate.toml`: `[[suite]]` `provider`, `model`, `mode`, `cases`, and `[[suite.accepted_miss]]` `case`, `bug`, `approved_by`, `approved_on`.

## Out of scope
- Recording the second provider live (S14).
- Adding the nine Phase 3 cases to the diff suite, the context suite and the policy's `context` mode: whichever of this slice and P3-S15 merges second adds them, in this format.

## Notes
Build choices (developer):
- **Fingerprint:** the SHA-256 of the JSON list of what a version sends for a fixed reference input: diff mode its system and user prompts; context mode also a domain rule, a linked issue and a framed tool result. `PROMPT_FINGERPRINTS` pins only the current versions. A version with no pin fails too.
- **Which suites run:** every suite in the file, not only those the policy lists, so S14's `openai` suite is checked before the policy lists it. The policy is resolved as `replay` resolves it (`--policy`, else `REVIEW_BOT_POLICY`, else built-in), and cases are scored under it.
- **Stricter `gate.toml`:** a suite per provider, model and mode, no case twice, each accepted miss on one of its suite's cases and a bug of that case, `approved_on` a TOML date (as in the policy's gate classes). Each is exit 2.
- **`live`:** gains `--provider`, `--model` and the price flags; the `openai` base URL comes from `REVIEW_BOT_OPENAI_BASE_URL`. An `openai` recording's `sdk_version` is the bot's own version, as its client is the bot's. `replay --provider openai --require-live` is refused (exit 2), as `--require-live` means Claude's default model.
- **Placement:** the report is `evals.gate_render`, as `compare` has `compare_render`, and `evals.score` gains the miss helpers the gate needs.
- **Accepted misses:** `gate.toml` holds only misses the owner's decision is recorded for: `sample`/`double-tax` (diff, 2026-11-07) and `sample-context`'s `tax-discount-ordering` and `float-money` (context, 2026-11-10). Diff mode's `sample-context`/`double-tax` and `ticket-criteria`/`discount-column-missing` were accepted on 2026-11-15 (next note).
- **Owner decision (2026-11-15):** while S9 was built, the owner accepted two diff-mode misses that had no earlier decision: `sample-context`/`double-tax` (1 of 3, needs 2) and `ticket-criteria`/`discount-column-missing` (0 of 3; diff mode sends no issue). Context mode catches both 3 of 3. They are `[[suite.accepted_miss]]` entries in `gate.toml`'s Claude diff suite. No threshold changed and nothing was re-run.
