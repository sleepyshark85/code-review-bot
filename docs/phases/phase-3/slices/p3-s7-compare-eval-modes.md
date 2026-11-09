# P3-S7: Compare diff mode and context mode on the same eval cases

- **Status:** Done
- **Depends on:** P3-S5
- **Touches:** `evals` (`compare`, `compare_render`, `cli`, `formats`, `score`, `replay`), `price_flags` (new), `pr.cli`

## Value
One offline command shows, case by case, whether context mode catches more and reports less noise than diff mode, and what it costs in tokens, dollars and tool calls.

## Acceptance criteria
`python -m review_bot.evals compare --cases DIR [--price-input-usd-per-mtok X --price-output-usd-per-mtok Y] [--format json]`. It replays each case's `recordings/live/recording.json` (diff) and `recordings/live-context/recording.json` (context) through today's pipeline, as `replay` does, and calls no model.
- **AC1:** Given a case with both recordings fresh, then each AI bug shows the runs that caught it per mode (`diff 2/5`, `context 3/3`) and whether each mode met `min_runs_caught`; each case shows pass or fail per mode.
- **AC2:** Given several paired cases, then each mode shows: AI bugs caught ÷ AI bugs, where a bug is caught when it meets `min_runs_caught` in that mode (as Phase 1's AI recall); credited runs ÷ runs, summed over bugs; **precision**, final AI findings that credit a bug ÷ all final AI findings, over cases with bugs; findings per run on cases with a noise budget; and injection runs passed ÷ runs. A rate with no cases to measure (no noise budget, no injection case) shows `not measured`, JSON `null`.
- **AC3:** Given recordings of different run counts (5 and 3), then each rate uses its own mode's runs.
- **AC4:** Given every run with `usage.cost_usd`, then each mode shows total and mean cost per run from them. Given some runs without it and both prices, then that mode's whole cost comes from every run's tokens at those prices (recorded costs aren't mixed in), in `Decimal`, marked `estimated`. Otherwise cost shows `not recorded`. Given only one price flag, or a negative or non-numeric price, then it exits 2 naming the flag, even if every run is costed, as `pr metrics` does ([P2-S10](../../phase-2/slices/p2-s10-metrics.md) AC5). Money is shown in USD to 4 decimal places, rounded half-even. Tokens (mean input and output per run) and, for context mode, tool calls (mean and largest per run) are always shown.
- **AC5:** Given the results, then a closing block states, per [Q3](../design.md#6-open-questions): precision `higher`, `same` or `lower`, or `not measured` (JSON `null`) when either mode has no final AI findings, whose precision then also shows `not measured`; the AI bugs caught in a smaller share of runs in context mode than in diff mode (shares, never counts), by ID, or `none`; and whether every context injection run passed, or `not measured` (JSON `null`) with no injection case.
- **AC6:** Given a failed run in either recording, then it counts as a run with no findings, and each mode lists its failed runs by error category. Runs that hit a cost or turn cap (`error.cap`) are listed per mode by case and run number, and count as misses for every bug and as failed injection runs.
- **AC7:** Given a case with `repo`, or with a context recording, that lacks a fresh recording in either mode, then it is listed as `unpaired` with the reason, left out of every aggregate, and the exit code is 1. Cases without `repo` and without a context recording are left out silently. With every such case paired, it exits 0; invalid input exits 2.
- **AC8:** Given `--format json`, then stdout is one ASCII JSON document, `eval-compare/v1`, holding per case and per mode exactly what the text shows.

## Limits
Reads only committed case and recording files, with the formats' strict schemas. Time is linear in recorded findings times bugs.

## Interfaces
`eval-compare/v1`: `schema`, `cases` (each `id`, `paired`, `reason`, `modes`: `diff` and `context`, each with per-bug `caught_runs`, `runs`, `met`), `totals` per mode, `verdict`. An unmeasured rate in `totals` keeps its counts (`part` and `whole`, or `findings` and `runs`) with its `share` or `mean` `null`; the verdict's `precision` and `context_injection_passed` are themselves `null`. Exit codes 0, 1, 2 as above. The comparison is pure, in `evals.compare` (`compare_cases`, [design](../design.md)); its text and JSON rendering live in `evals.compare_render`, apart from `evals.render`, to keep modules within [CONVENTIONS](../../../../CONVENTIONS.md) §2's size. The price flags and their parsing move from `pr.cli` to a shared `price_flags` module (`add_price_arguments`, `prices_from_flags`), used by both CLIs; `pr metrics` behaviour is unchanged.

## Out of scope
- Labelled pull requests (S13).

## Notes
- **README (main session):** describe `compare`.
