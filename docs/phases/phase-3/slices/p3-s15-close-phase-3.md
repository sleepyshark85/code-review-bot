# P3-S15: Close Phase 3: measure the gate, release 0.4.0

- **Status:** Done, with exceptions accepted by the owner on 2026-11-12: AC3 and AC7 not run, and the recall clause not met on the eval cases ([test report](../test-report.md#p3-s15-close-phase-3))
- **Depends on:** P3-S1 to P3-S14, P3-S16, and P2-S12 AC1 to AC6 ([#67](https://github.com/sleepyshark85/code-review-bot/issues/67)): the committed labelled set
- **Touches:** `tests/evals/cases/*/recordings/`, `tests/evals/labelled/`, `tests/evals/`, `docs/phases/phase-3/test-report.md`, `pyproject.toml`; README and release notes by the main session

## Value
The phase closes on evidence: context reviews measured against Phase 2's on the same eval cases and pull requests, within the $20 approved, released as 0.4.0.

## Acceptance criteria
Mostly recorded evidence, as in [P2-S12](../../phase-2/slices/p2-s12-close-phase-2.md). Definitions: [How the gate is evidenced](../design.md#how-the-gate-is-evidenced); caps: [Spend and caps](../design.md#spend-and-caps). Every live step is dispatched from `live.yml` (P3-S16) once, with approval; a miss goes to the user with the numbers and is never re-run until it passes ([ADR 0007](../../../adr/0007-evals-replay-and-live.md)).
- **AC1 (recorded):** Before any live run, the test report compares the S16 smoke's reported cost per review with the per-call caps, and goes back to the reviewer if it exceeds them. It then gives each dispatch and its result: `evals-diff` (`--runs 3 --max-usd 2.70 --max-cost-usd 0.10`) and `evals-context` (`--runs 3 --max-usd 8.10 --max-cost-usd 0.30`) on the 9 new cases, `sample-context`, `clean-context`, `injection-tool-result`, `injection-instruction-context`, `injection-canary-context`, `injection-delimiter-context`, `ticket-criteria`, `injection-issue` and `domain-money`. Their recordings, from the artifacts, are committed.
- **AC2 (recorded):** It includes the full `evals compare` output: each bug's share of runs caught per mode, `double-tax` on `sample-context` named, precision per mode, every context injection run's result, cap-hit runs per mode by name (counted as misses), tokens, cost and tool calls.
- **AC3 (recorded):** It names the labelled PRs chosen by [Q10](../design.md#6-open-questions) (final head SHA with a Phase 2 report, newest first, at most 12), as listed in `tests/evals/labelled/phase-3-pulls.json`, which the main session committed in its own PR after #67 closed and before the dispatch ([P3-S8 Interfaces](p3-s8-pr-review-with-context.md#interfaces)); then the `backfill` dispatch (`--context repo --max-usd 7.20 --max-cost-usd 0.60`), the count of new units judged with `pr label` and both sides' coverage, and the full `pr compare` output with its verdict and its `no_same_head` exclusions.
- **AC4 (recorded):** It gives a spend table listing **every** dispatch run of `live.yml`, re-runs and failed runs included, each with its URL, step, cap and reported spend, summed against each step's cap and against $20.00. Any use of the reserve names its approver: the user where possible, else the reviewer. It confirms from the reports that no review exceeded its `max_cost_usd` by more than one turn.
- **AC5 (recorded):** It states whether the gate is met under [Q3](../design.md#6-open-questions) and [Q4](../design.md#6-open-questions), clause by clause, and the disclosures: sample sizes, agent-written PRs merged by one person, cases written to need context, few PRs linking issues, and prompts tuned on these cases.
- **AC6:** Given the committed context recordings, then `replay --mode context --require-live` scores the 9 cases offline with the default model and reproduces AC2's per-bug numbers; any miss the user accepted is expected by the test, as `double-tax` is in Phase 1.
- **AC7:** Given the committed `tests/evals/labelled/` files, now with the candidate's would-be comments and the new labels, then a test recomputes AC3's precision, recall, coverage and verdict exactly, and finds no body, rationale or username in any line.
- **AC8:** Given the Phase 1 cases, then diff-mode `replay` still exits 1 only for `double-tax`, and the Phase 1 and Phase 2 suites pass unchanged since P3-S5 (which scoped P1-S17's checks to the Phase 1 cases).
- **AC9:** `pyproject.toml` is at `0.4.0`, and `docs/phases/phase-3/release-notes.md` lists the changes. The tag and release follow [CLAUDE.md](../../../../CLAUDE.md).

## Limits
None beyond S6, S8, S13 and S16.

## Interfaces
`would-be.jsonl` gains the candidate's units in [P2-S12's](../../phase-2/slices/p2-s12-close-phase-2.md#acceptance-criteria) shape, with `head_sha` and a `run` field, `phase-2` or `phase-3`.

## Out of scope
- Turning context mode on in CI (S14, the owner's call).

## Notes
- **Closing designer task:** mark every spec `Done`; update the README's Phase 3 status and its risk row "no tools before P3" (main session).
