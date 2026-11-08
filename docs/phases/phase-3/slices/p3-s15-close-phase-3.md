# P3-S15: Close Phase 3: measure the gate, release 0.4.0

- **Status:** Draft
- **Depends on:** P3-S1 to P3-S14, and P2-S12 (Phase 2's labelled set)
- **Touches:** `tests/evals/cases/*/recordings/`, `tests/evals/labelled/`, `tests/evals/`, `docs/phases/phase-3/test-report.md`, `pyproject.toml`; README and release notes by the main session

## Value
The phase closes on evidence: context reviews measured against Phase 2's on the same eval cases and pull requests, within the $20 approved, released as 0.4.0.

## Acceptance criteria
Mostly recorded evidence, as in [P2-S12](../../phase-2/slices/p2-s12-close-phase-2.md). Definitions: [How the gate is evidenced](../design.md#how-the-gate-is-evidenced); caps: [Spend and caps](../design.md#spend-and-caps). Every live step is run once with the user's approval; a miss goes to the user with the numbers and is never re-run until it passes ([ADR 0007](../../../adr/0007-evals-replay-and-live.md)).
- **AC1 (recorded):** The test report gives each live command as run, and its result: `evals live --mode diff` on the 8 cases with `repo`, `--runs 3 --max-usd 2.40 --max-cost-usd 0.10`; then `--mode context` on the same cases, `--max-usd 7.20 --max-cost-usd 0.30`. Their recordings are committed.
- **AC2 (recorded):** It includes the full `evals compare` output: per-bug catches per mode, `double-tax` on `sample-context` named, precision per mode, every context injection run's result, tokens, cost and tool calls.
- **AC3 (recorded):** It names the labelled PRs chosen by [Q10](../design.md#6-open-questions), the `pr backfill --context repo --max-usd 7.20 --max-cost-usd 0.60` run, the count of new units judged with `pr label` and both sides' coverage, and the full `pr compare` output with its verdict.
- **AC4 (recorded):** It gives a spend table: each step's cap and reported spend, and the total, at most $20.00. Any use of the reserve names the reviewer's approval. It confirms from the reports that no review exceeded its `max_cost_usd` by more than one turn.
- **AC5 (recorded):** It states whether the gate is met under [Q3](../design.md#6-open-questions) and [Q4](../design.md#6-open-questions), clause by clause, and the disclosures: sample sizes, agent-written PRs merged by one person, cases written to need context, few PRs linking issues, and prompts tuned on these cases.
- **AC6:** Given the committed context recordings, then `replay --mode context --require-live` scores the 8 cases offline with the default model and reproduces AC2's per-bug numbers; any miss the user accepted is expected by the test, as `double-tax` is in Phase 1.
- **AC7:** Given the committed `tests/evals/labelled/` files, now with the candidate's would-be comments and the new labels, then a test recomputes AC3's precision, recall, coverage and verdict exactly, and finds no body, rationale or username in any line.
- **AC8:** Given the Phase 1 cases, then diff-mode `replay` still exits 1 only for `double-tax`, and the Phase 1 and Phase 2 suites pass unchanged.
- **AC9:** `pyproject.toml` is at `0.4.0`, and `docs/phases/phase-3/release-notes.md` lists the changes. The tag and release follow [CLAUDE.md](../../../../CLAUDE.md).

## Limits
None beyond S6, S8 and S13.

## Interfaces
`would-be.jsonl` gains the candidate's units in [P2-S12's](../../phase-2/slices/p2-s12-close-phase-2.md#acceptance-criteria) shape, with a `run` field, `phase-2` or `phase-3`.

## Out of scope
- Turning context mode on in CI (S14, the owner's call).

## Notes
- **Closing designer task:** mark every spec `Done`; update the README's Phase 3 status and its risk row "no tools before P3" (main session).
