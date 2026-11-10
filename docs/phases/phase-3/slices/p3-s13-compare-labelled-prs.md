# P3-S13: Compare Phase 3 against Phase 2 on the labelled pull requests

- **Status:** Done
- **Depends on:** P3-S8
- **Touches:** `pr` (`compare`, `cli`, `units`, `reports`, `metrics_report`)

## Value
The user sees, on the same pull requests, whether the context review is more precise and finds more accepted issues than the Phase 2 review, at what cost.

## Acceptance criteria
`python -m review_bot.pr compare --baseline DIR --candidate DIR --labels FILE [--price-input-usd-per-mtok X --price-output-usd-per-mtok Y] [--format json]`, on fixture `shadow/v1` reports and `feedback/v1` labels. Units, verdicts and the precision formula are Phase 2's ([How the gate is evidenced](../../phase-2/design.md#how-the-gate-is-evidenced)); pooling is [ADR 0016](../../../adr/0016-measure-phase-3-against-phase-2.md)'s.
- **AC1:** Pairing is by head SHA: a candidate report is compared with the Phase 2 baseline report of the same repo, PR and head SHA, both `reviewed` in shadow mode. Given baseline reports for PR 2 at heads `A` and `B` and a candidate for PR 2 at `B`, then only the `B` reports are compared. Given a candidate for PR 3 at `C` with baseline reports only at other heads, or a candidate for PR 4 with no baseline report, then PR 3 and PR 4 are excluded with reason `no_same_head`. Baseline PRs with no candidate are excluded `no_candidate`. Only units of compared reports count on either side.
- **AC2:** Given each side's units (as `pr.units` groups them) joined to `FILE` by repo, PR, file, rule part, anchor and index, then each side shows accepted, rejected by reason, unjudged, coverage, precision with its Wilson 95% interval, and the lower bound counting duplicates.
- **AC3:** Given accepted units grouped by repo, PR, file and anchor into issues, then the pool is every issue either side has an accepted unit in, and each side's recall is its issues ÷ the pool. It also shows issues found only by each side. Given a baseline `ai:logic` unit and a candidate `ai:reliability` unit on one anchored line, both accepted, then they are one issue found by both.
- **AC4:** Given every compared report holding `usage.cost_usd`, then each side shows total cost, mean cost per reviewed PR and cost per accepted unit from them. Otherwise, with both prices, cost is computed from tokens in `Decimal` and marked `estimated`; with neither, `not available`. Tokens per PR (mean), tool calls per PR (mean and largest) and review time (p50, p95) are always shown.
- **AC5:** Given both sides at coverage of at least 90%, then a verdict states precision `higher`, `same` or `lower` and recall `higher`, `same` or `lower`, for the candidate, as [Q3](../design.md#6-open-questions) defines. Below 90% on either side, the verdict is `not decidable: coverage`, with each side's coverage.
- **AC6:** Given a label for a unit on a PR not compared, then it's ignored. Given two labels for one unit, the latest `recorded_at` wins.
- **AC7:** Given a malformed `FILE` line, then it exits 2 naming the line. Given a report that isn't `shadow/v1`, or is over 10 MB, then it's skipped with one stderr line, as `label` does. Otherwise it exits 0.
- **AC8:** Given `--format json`, then stdout is one ASCII JSON document, `compare/v1`, holding exactly what the text shows; no body, rationale or username appears in either format.

## Limits
Reports and labels are downloaded files, so untrusted: each report at most 10 MB, each read with Phase 2's schema checks, in time linear in its size.

## Interfaces
`compare/v1`: `schema`, `prs` (compared, each with its head SHA; excluded, each with its reason), `baseline` and `candidate` (each `units`, `precision`, `recall`, `cost`, `tokens`, `tool_calls`, `latency`), `pool`, `verdict`. Exit codes 0 and 2.

## Out of scope
- Judging the new units, which is `pr label` on the candidate reports; comment-mode reactions.

## Notes
- **README (main session):** describe `compare`.
- **Build choices (developer):**
  - `pr.units`' `ShadowFile` now reads `pr.head_sha` (40 lowercase hex, written since P2-S2), `report.usage.cost_usd` (optional, 0 to 1,000,000) and `report.context.tool_calls` (optional; 0 without). `pr.reports.read_reports` takes the flag naming its directory, so a missing `--baseline` or `--candidate` is named.
  - A candidate PR with reports at several heads that a baseline shares is compared at the head of its latest such report. Reports that aren't `reviewed` shadow reports of a PR are ignored on both sides, as `label` and `metrics` ignore them.
  - Every compared report at the compared head counts, re-runs included: tokens, tool calls and cost are summed per side and divided by the compared PRs; review time is per report. Cost basis is per side (Phase 2 reports have no `cost_usd`, so the baseline is `estimated` with prices). Money is shown to 4 decimal places, half-even, as `evals compare` does.
  - Coverage of a side with no units counts as met (nothing is left to judge). A decidable change is `not measured` (JSON `null`) when either side has nothing to measure it on: no precision denominator, or an empty pool.
  - The pure comparison is `pr.compare.compare_runs`; its `compare/v1` and text renderers are in `pr.metrics_report`, beside `metrics/v1`'s, sharing its formatting. `pr.cli` reads the files and maps an unusable `--labels` to exit 2.
