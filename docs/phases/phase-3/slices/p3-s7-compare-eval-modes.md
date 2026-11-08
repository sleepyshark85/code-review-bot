# P3-S7: Compare diff mode and context mode on the same eval cases

- **Status:** Draft
- **Depends on:** P3-S5
- **Touches:** `evals` (`compare`, `cli`, `render`)

## Value
One offline command shows, case by case, whether context mode catches more and reports less noise than diff mode, and what it costs in tokens, dollars and tool calls.

## Acceptance criteria
`python -m review_bot.evals compare --cases DIR [--price-input-usd-per-mtok X --price-output-usd-per-mtok Y] [--format json]`. It replays each case's `recordings/live/recording.json` (diff) and `recordings/live-context/recording.json` (context) through today's pipeline, as `replay` does, and calls no model.
- **AC1:** Given a case with both recordings fresh, then each AI bug shows the runs that caught it per mode (`diff 2/5`, `context 3/3`) and whether each mode met `min_runs_caught`; each case shows pass or fail per mode.
- **AC2:** Given several paired cases, then each mode shows: AI bugs caught ÷ AI bugs; credited runs ÷ runs, summed over bugs; **precision**, final AI findings that credit a bug ÷ all final AI findings, over cases with bugs; findings per run on cases with a noise budget; and injection runs passed ÷ runs.
- **AC3:** Given recordings of different run counts (5 and 3), then each rate uses its own mode's runs.
- **AC4:** Given every run with `usage.cost_usd`, then each mode shows total and mean cost per run from them. Given some runs without it and both prices, then cost comes from tokens at those prices, in `Decimal`, marked `estimated`. Otherwise cost shows `not recorded`. Tokens (mean input and output per run) and, for context mode, tool calls (mean and largest per run) are always shown.
- **AC5:** Given the results, then a closing block states, per [Q3](../design.md#6-open-questions): precision `higher`, `same` or `lower`; the AI bugs caught in a smaller share of runs in context mode than in diff mode, by ID, or `none`; and whether every context injection run passed.
- **AC6:** Given a failed run in either recording, then it counts as a run with no findings, and each mode lists its failed runs by error category.
- **AC7:** Given a case with `repo` that lacks a fresh recording in either mode, then it is listed as `unpaired` with the reason, left out of every aggregate, and the exit code is 1. Cases without `repo` that have no context recording are left out silently. With every case with `repo` paired, it exits 0; invalid input exits 2.
- **AC8:** Given `--format json`, then stdout is one ASCII JSON document, `eval-compare/v1`, holding per case and per mode exactly what the text shows.

## Limits
Reads only committed case and recording files, with the formats' strict schemas. Time is linear in recorded findings times bugs.

## Interfaces
`eval-compare/v1`: `schema`, `cases` (each `id`, `paired`, `reason`, `modes`: `diff` and `context`, each with per-bug `caught_runs`, `runs`, `met`), `totals` per mode, `verdict`. Exit codes 0, 1, 2 as above.

## Out of scope
- Labelled pull requests (S13).

## Notes
- **README (main session):** describe `compare`.
