# P2-S10: Measure latency, tokens, cost, outcomes and precision

- **Status:** Done
- **Depends on:** P2-S4, P2-S8
- **Touches:** `pr` (`metrics`, `cli`)

## Value
One offline command turns downloaded reports and labels into the numbers the gate and the README's targets ask for.

## Acceptance criteria
`python -m review_bot.pr metrics --reports DIR [--labels FILE ...] [--since DATE] [--until DATE] [--price-input-usd-per-mtok X --price-output-usd-per-mtok Y] [--format text|json]`, on fixture files. Definitions: [precision](../design.md#how-the-gate-is-evidenced).
- **AC1:** Given reports with outcomes 4 `reviewed`, 1 `skipped`, 1 `blocked_secret` and 1 `failed`, then the counts per outcome and reason are shown, split by `mode` and `origin`.
- **AC2:** Given 4 reviewed runs whose `finished_at` minus `event_at` is 60, 90, 120 and 400 seconds, then end-to-end latency p50 is 90 s and p95 is 400 s (nearest rank). It is computed over reviewed runs with `origin` `event` only, since a backfilled run's `event_at` is its merge time. Review time, `finished_at` minus `started_at`, is shown the same way beside it.
- **AC3:** Given the same `run_id` in two files, then it is counted once.
- **AC4:** Given usage per reviewed run, then input and output tokens each show their total, mean and maximum, plus the count of `over_budget` runs.
- **AC5:** Given prices of 15 and 75 USD per million tokens, and 1,000,000 input plus 100,000 output tokens, then the cost is 22.50 USD, computed in `Decimal` and rounded half-even to cents. With 9 accepted shadow labels, cost per accepted finding is 2.50 USD. Given no prices, then cost shows `not priced`. Given only one price, or a negative or non-numeric one, then it exits 2 naming the flag.
- **AC6:** Given shadow labels on 34 would-be comments, 21 `accept`, 9 `reject` for other reasons and 2 `reject` `duplicate`, then:
  - shadow precision is 70.0% (21/30), with a Wilson 95% interval of 52.1% to 83.3%;
  - duplicates are 2, shown separately, and the lower bound with duplicates counted as rejections is 65.6% (21/32);
  - coverage is 94.1% (32/34);
  - the number of PRs and the largest PR's share are shown;
  - there are splits by origin, source, rule part and reject reason.
- **AC7:** Given would-be comments in reports with `mode` `comment`, then they aren't counted in shadow precision or coverage.
- **AC8:** Given two shadow labels for one (repo, PR, key, index), or two reaction labels for one (repo, PR, fingerprint), then only the one with the later `recorded_at` counts. Reaction records are already combined per fingerprint ([P2-S8 AC11](p2-s8-reactions-as-labels.md)), so a moved comment counts once.
- **AC9:** Given a reaction label and the `mode` `comment` report whose `comments` created that fingerprint, then the label takes its `source`, `rule_part`, `severity`, `model` and `prompt_version` from that report. Comment precision and its coverage are shown separately from shadow precision. `conflict` and `resolved` are counted but never enter precision.
- **AC10:** Given no judged labels, then precision shows `n/a`, with no error.
- **AC11:** Given `--since` and `--until`, then only runs whose `event_at` falls in the window count, and only the would-be comments from those runs.
- **AC12:** Given a file that isn't valid `shadow/v1` or `feedback/v1`, or a label that joins to no report, then each is skipped or counted as `orphan`, with one stderr line each, and excluded from every number.
- **AC13:** Given `--format json`, then stdout is one ASCII JSON document with `schema` `metrics/v1` and the same numbers.

## Limits
Each input file is at most 10 MB and is parsed with a schema, in time linear in the total size. Dates are UTC.

## Interfaces
Exit codes: 0 when done, 2 for usage errors or an invalid flag. Prices are the user's input and are never stored in code ([Needs a person](../design.md#4-needs-a-person)).

## Out of scope
- Workflow runs that wrote no report: they show as failed runs in Actions, and the test report counts them by hand. The baseline (S11).

## Notes
- **README (main session):** describe `metrics`.

Build decisions (lead and developer, 2026-11-08):
- **Placement:** `pr.reports` reads report and label files, each capped at `MAX_FILE_BYTES` (moved from `label`; used by `label`, `backfill` and `metrics`), and `parse_labels` is the one labels-file parser. The shared statistics (`ratio`, `wilson_interval`, `token_cost_usd`, `to_cents`, `Verdicts`, `Precision`) are in `pr.metrics`, `summarise_runs` in `pr.run_metrics`, and the `metrics/v1` text and JSON renderers in `pr.metrics_report`; each module stays under about 400 lines. `pr.units.distinct_runs` is the one `run_id` dedupe. `ShadowFile` now also parses `reason`, `event_at`, `started_at`, `over_budget` and `report.usage`.
- **Window:** `--since` and `--until` are each optional UTC days, both included. Labels whose finding is only in runs outside the window are left out silently; a label is an orphan only if no report in `--reports` has its finding.
- **Latency:** end-to-end covers `reviewed` runs with `origin` `event`; review time covers `reviewed` runs of every origin. Each shows its run count.
- **Tokens and cost:** over every `reviewed` run in the window. Cost per accepted finding divides the unrounded cost by the accepted shadow labels, then rounds; with none accepted it is null (`n/a`).
- **Prices:** digits with an optional fraction only, so a sign, exponent, `nan` or `inf` exits 2 naming the flag. With one price, the error names the missing flag.
- **Labels files:** `--labels` takes several files after one flag (and may repeat). A file that can't be read, is over 10 MB or has any line that isn't `feedback/v1` is skipped whole, with one stderr line naming the file and the first bad line. A report file over 10 MB is skipped with one line.
- **Comment precision:** units are the distinct (repo, PR, fingerprint) `create` entries with `applied` `done` in `reviewed` comment mode reports; a `failed` or `not_attempted` create was never posted, so it is no unit and no join target; coverage is reaction `accept` or `reject` labels on them over that. `conflict` and `resolved` are counted beside it. A finding's traits come from the latest report with it.
- **Splits:** by origin, source, rule part, severity, model and prompt version for both channels, plus reject reason. A split's `rejected` excludes duplicates, which have their own count.
- **Shape:** JSON shares are fractions rounded to 3 places; text shows percentages to 1 place, `n/a` and `not priced`. Money is a cent string. Text from reports is JSON-quoted in text output.
