# P4-S1: Run under a versioned policy file that every report records

- **Status:** Done
- **Depends on:** none
- **Touches:** `governance.policy` (new), `threshold` (renamed from `policy`, no behaviour change), `settings`, `cli`, `pr.cli`, `evals.cli`, `evals.live`, `context.tools`, `review`, `report`, `pr.shadow`

## Value
An operator chooses which tools a review may use and a ceiling on each call's cost in one file, and every report says which policy it ran under ([ADR 0018](../../../adr/0018-policy-as-code-file.md)). No live command spends without a dollar cap any more, in diff mode either ([design, cost row](../design.md#what-exists-and-what-is-new)).

## Acceptance criteria
Policy format: [design, Interfaces](../design.md#interfaces). This slice adds `version`, `[tools]` and `[cost]` only. Any other key is invalid until its slice adds it.
- **AC1:** Given no `--policy` and no `REVIEW_BOT_POLICY`, when any command reviews, then `report/v1` and `shadow/v1` have `policy` `{"version": "builtin-v1", "sha256": <fixed>, "source": "builtin"}`, and the requests, findings, caps and exit codes are exactly as before. The Phase 0 to 3 suites pass unchanged, except tests that pin the report keys (they gain `policy`) and tests that run `evals live` or `pr backfill` without the flags AC8 now requires (they gain the flags).
- **AC2:** Given `--policy p.toml` holding `version = "org-1"`, then `policy` is `{"version": "org-1", "sha256": <SHA-256 of the file's bytes>, "source": "p.toml"}` in both reports, and the audit log's `run`/`start` record has `policy_version` and `policy_sha256`. `--policy` wins over `REVIEW_BOT_POLICY`.
- **AC3:** Given a policy file that is missing, unreadable, over 64 KB, not UTF-8 or not TOML, or that has an unknown key (`foo`), no `version`, a `version` not matching `[A-Za-z0-9._-]{1,64}`, a tool not in `read`, `grep`, `glob`, a repeated tool, or a `max_cost_usd` outside 0.01 to 1000 or not a decimal string, then the command exits 2 before reading the diff. The message names the file and the key or problem, and no provider is built.
- **AC4:** Given `[tools] allowed = ["read", "glob"]` and a context review whose `MockProvider` scripts a `grep` call, then the request's tool definitions are exactly `read` and `glob`, the `grep` call returns a delimited refusal, and its ledger entry and audit `context`/`tool_call` record are `refused` with reason `policy_denied`. It counts towards `max_tool_calls`.
- **AC5:** Given `allowed = []` and `--context-repo` (or `pr run --context repo`), then the command exits 2 before any call: "The policy `<version>` allows no tools, so context mode can't run; use diff mode." Diff mode runs as usual.
- **AC6:** Given `[cost] max_cost_usd = "0.60"`: with no `--max-cost-usd`, every model call is capped at 0.60, in diff and context mode, and `caps` in `shadow/v1` shows it. With `--max-cost-usd 0.30`, the cap is 0.30. With `--max-cost-usd 1.00`, the command exits 2, naming both amounts and the policy, before any call.
- **AC7:** Given that ceiling, then `evals live` and `pr backfill` take `--policy` too, and refuse a `--max-cost-usd` above it with exit 2 before any call. Their allowance checks use the effective cap.
- **AC8:** Given `evals live` or `pr backfill` in **any** mode, diff included, then each refuses with exit 2 before any call unless it has `--max-usd` and a per-call cap, from `--max-cost-usd` or the policy's ceiling, and planned calls × that cap fit in `--max-usd`. The message names what is missing. For example, `evals live --mode diff --runs 3 --max-calls 15` with neither flag and the built-in policy exits 2, naming both `--max-usd` and `--max-cost-usd`. The existing `live.yml` steps already pass both, and still parse.

## Limits
The file is operator input, but it is still parsed strictly: at most 64 KB, read whole once, with `tomllib` in linear time. A message shows at most 100 characters of a key or value, with unprintable characters escaped.

## Interfaces
- `--policy PATH` / `REVIEW_BOT_POLICY` on `review-bot`, `pr shadow|run|backfill` and `evals live|replay`.
- `RefusalReason.POLICY_DENIED` = `"policy_denied"`. `BUILTIN_POLICY.version` = `"builtin-v1"`.

## Out of scope
- Claude's hook and `can_use_tool` (S3); redaction (S4); gate classes (S5); models and routing (S7).
- Passing a policy in CI (S10).

## Notes
- `version` is required. A left-out `[tools]` or `[cost]`, or its key, means the built-in behaviour: all three tools, no ceiling.
- In diff mode, `caps` shows `max_cost_usd` only under a policy with a ceiling, so a run under the built-in policy keeps Phase 2's caps (AC1, AC6).
- `evals replay` and `live` in context mode offer only the policy's tools, which `prompt_sha256` covers: a recording made under a policy replays fresh under the same tool set. Both refuse context mode under a policy with no tools, as AC5's commands do. The context prompt's text still names all three tools, so requests stay as before (AC1).
- `pr backfill` with no call planned (every report already in `--out`) still needs no amounts, as before: nothing can be spent.
