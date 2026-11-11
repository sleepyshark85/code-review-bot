# P4-S14: Run the second provider's live smoke and evals from the manual workflow

- **Status:** Approved
- **Depends on:** P4-S6, P4-S7, P4-S9. The live dispatches (AC6 to AC8) also wait for the user's answers to [Q2](../design.md#6-open-questions) and [Q10](../design.md#6-open-questions), the `OPENAI_API_KEY` secret with a spend limit on its project, and P3-S15's `evals-diff` recordings
- **Touches:** `.github/workflows/live.yml` (main session, from this spec), `tests/` (the tester parses the workflow, as for [P3-S16](../../phase-3/slices/p3-s16-live-steps-workflow.md)), then `tests/evals/cases/*/recordings/live-openai/`, `tests/evals/gate.toml`, `.github/review-bot-policy.toml` and `docs/phases/phase-4/test-report.md`

## Value
The second provider is measured live on the same eval cases as Claude, within an approved budget, so the policy can list it as the fallback ([ADR 0021](../../../adr/0021-route-on-availability-failures-then-rules-only.md)). Amounts: [design, Spend and live steps](../design.md#spend-and-live-steps).

## Acceptance criteria
AC1 to AC5 are checked by parsing `live.yml`, and every P3-S16 rule still holds. The model name and base URL are literals in the workflow, written by the main session once Q2 is answered.
- **AC1:** Given `live.yml`, then `step` gains `smoke-fallback` and `evals-fallback`, and the existing options are unchanged.
- **AC2:** Given `smoke-fallback`, then it reviews the existing smoke diff twice with `--provider openai`, once in diff mode and once with `--context-repo .`, each with `--max-cost-usd 0.30`, both price flags as literals, and retries 0: at most $0.60.
- **AC3:** Given `evals-fallback`, then it runs `evals live --provider openai --mode diff` twice: on `sample`, `clean`, `injection-instruction`, `injection-canary` and `injection-delimiter` with `--runs 5 --max-calls 25 --max-usd 2.50 --max-cost-usd 0.10`, then on the nine Phase 3 cases with `--runs 3 --max-calls 27 --max-usd 2.70 --max-cost-usd 0.10`. Both pass the price flags and `--allow-dispatch`, and it uploads `recordings/live-openai/` with the audit log and output.
- **AC4:** Given either new step, then `OPENAI_API_KEY` reaches only its live command's step, `ANTHROPIC_API_KEY` reaches neither, and permissions are exactly `contents: read`.
- **AC5:** Given the real CLI parsers, then a test checks that each step's arguments parse.
- **AC6 (recorded):** The test report records the `smoke-fallback` dispatch: outcome, tool calls, reported cost per review and the model, and compares the reported cost with the usage the provider bills for the same period, from its console. **Stop rule:** if the cost per review exceeds its cap, if the billed usage exceeds the reported cost by more than 10%, or the context review makes no tool call, then `evals-fallback` waits and the table goes back to the reviewer.
- **AC7 (recorded):** It records the `evals-fallback` dispatch and its `replay --provider openai` scores per case, beside Claude's. The recordings are committed. `gate.toml` gains the `openai` diff suite, with any miss accepted only by the user, and `evals gate` passes.
- **AC8 (recorded):** Only then does `.github/review-bot-policy.toml` gain the `openai` entry for `diff` and `[routing] fallback`, by the main session, and the review jobs get `OPENAI_API_KEY` in their review step only. The spend table lists every Phase 4 dispatch, re-runs included, against each cap and the $8.00.

## Limits
No input but the choice. Every amount is a literal, and each live command's own pre-flight still refuses a plan over its `--max-usd`.

## Interfaces
`live.yml` choices `smoke-fallback` and `evals-fallback`.

## Out of scope
- The second provider's context suite ([Q13](../design.md#6-open-questions)); turning the fallback on in CI before AC7 passes.

## Notes
