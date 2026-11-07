# P1-S14: Score the sample diff per planted bug and per source, offline

- **Status:** Done
- **Depends on:** P1-S9
- **Touches:** `evals` (new)

## Value
Anyone can see, offline, which planted bugs the rules catch for real, and that the scorer works, without mistaking that for a measurement of the model.

## Acceptance criteria
Command: `python -m review_bot.evals replay --cases DIR [--recording PATH] [--require-live]`. Evidence: [design.md](../design.md#what-the-evals-prove). **Until P1-S17, every AI result here is synthetic.** Synthetic recordings here hold 5 runs.
- **AC1:** Given the `sample` case with no recording, then both `rule` bugs are caught, the report has one row per bug, marking AI bugs `not measured`, and it exits 0. With `--require-live`, it exits 1.
- **AC2:** Given a synthetic recording matching all six AI bugs in every run, then each shows `5/5`, per-source recall shows `rules 2/2`, `ai 6/6`, and each row says `evidence=synthetic`. It exits 0.
- **AC3:** Given one matching only `float-money`, then `ai 1/6`, and it exits 1. Given `double-tax` in 2 of 5 runs with its `min_runs_caught` of 3, it exits 1. Given 3 of 5, that bug passes.
- **AC4:** Given a rule disabled through a test double, or a sample whose line 39 uses an encoding, then the `rules` score falls and it exits 1.
- **AC5:** Given a recording, then it goes through the real pipeline: a matching finding on line 42 doesn't count. AI recall is scored on `ai_anchor_valid`, before dedup; the `final` column is scored after it.
- **AC6:** Given a recording whose `case_sha256`, `diff_sha256`, `prompt_version` or `prompt_sha256` differs from today's, then it exits 1 and names the field and the `live` command, without scoring. Changing either prompt, the framing, or what's skipped changes `prompt_sha256`. Given a recording of another model, it's scored for display and the model is shown. With `--require-live`, it exits 1: that flag, not the test calling it, enforces the default-model check.
- **AC7:** Every run's output contains the fixed line: `Replay checks the pipeline against a past model response. It is not a new measurement of the model.` Each row shows the recording's date and model.
- **AC8:** Given a recording holding a failed run (`error` with a category), then that run catches nothing and is listed by category.
- **AC9:** Given a case with an unknown field, an unknown version or a missing diff, then it exits 2 and names the file. Given `--cases` missing, the same. Given a `synthetic` file under `recordings/live/`, a `live` file under `recordings/synthetic/`, `--recording` with a `live` file, or `--recording` with `--require-live`, the same.
- **AC10:** Given a whole run, then `tests/conftest.py`'s guard records no live call and no subprocess, and nothing is written outside a temporary audit path, which is discarded.

**Matching**
- **AC11 (word boundaries):** Given `reorder` or `disorder`, then `order` doesn't match; given `export_csv`, then `export` and `csv` both match, because `_` counts as a boundary.
- **AC12 (no cross-credit):** Given `Float arithmetic on the discount` on line 34, then only `float-money` is credited. Given `CSV export drops the discount` on line 40, then only `discount-dropped-on-export`. Given `Tax is applied before the discount` on line 14, then `tax-discount-ordering`, not `double-tax`. Given `open() uses the platform encoding for this path` on line 39, then not `csv-path`. Given `missing-tests`, a finding saying "test" in category `logic` isn't credited; any `testing` finding is.
- **AC13:** Given any credited bug, then the report lists each matching rationale under that bug, for a person to confirm, and each unmatched finding.

## Interfaces
**Layout, under `DIR`:** `<case_id>/case.json`; `<case_id>/recordings/live/recording.json`; `<case_id>/recordings/synthetic/<name>.json`. `replay` reads each case's live recording if present. It reads a synthetic one only through `--recording`, so a control can never count as a measurement. The tester owns `tests/evals/cases/`.

**`EvalCase`:** `schema` `eval-case/v1`, `id`, `diff` (relative to `case.json`), `bugs`, and for later cases `noise_budget`, `injection`, `min_passing_runs`. Each bug: `id`, `source`, `file`, `lines` (inclusive windows or `any`), optional `categories`, `keywords` (all groups must match; any phrase in a group, case-insensitive, at non-alphanumeric boundaries), and for AI `min_runs_caught`.

**`Recording`:** `schema`, `case_id`, `kind` (`live` or `synthetic`), `case_sha256`, `diff_sha256`, `prompt_version`, `prompt_sha256` (system prompt plus built `user_prompt`), `model`, `sdk_version`, `recorded_at`, `runs`. Each run: `findings` as returned, or `error`; `usage`; `warnings`.

**Exit rule:** `replay` exits 0 only if every case is valid, every recording read is fresh, every rule bug is caught, each AI bug is caught in at least `min_runs_caught` of its runs, and at least `min_passing_runs` runs pass. A failed run never passes. A case with no recording is scored on rules alone, unless `--require-live`. A finding may credit two bugs only if it meets both matchers.

All in `billing/invoice.py`; keyword columns are groups, all required:

| Bug | Source | Lines | Keywords | Min |
|---|---|---|---|---|
| `swallowed-exception` | rule | 35 | | always |
| `open-without-encoding` | rule | 39 | | always |
| `discount-dropped-on-export` | ai | 34, 38-41 | (`discount`, `discounts`, `total_override`) and (`export`, `exports`, `exported`, `csv`) | 3 |
| `tax-discount-ordering` | ai | 14, 32-36 | (`tax`, `taxes`, `tax_rate`) and (`discount`, `discounts`) and (`order`, `ordering`, `before`, `after`, `inclusive`) | 3 |
| `double-tax` | ai | 14, 34 | (`tax`, `taxes`, `taxed`, `tax_rate`) and (`double`, `doubled`, `twice`, `already`, `caller`, `callers`) | 3 |
| `float-money` | ai | 13-14, 34, 40 | (`float`, `floats`, `floating`, `decimal`) | 3 |
| `csv-path` | ai | 38-41 | (`unvalidated path`, `path validation`, `validate the path`, `validated`, `validate`, `sanitize`, `arbitrary path`, `untrusted path`, `traversal`, `csv module`, `csv.writer`, `escaping`, `quoting`, `hand-rolled`) | 3 |
| `missing-tests` | ai | any | category `testing` | 3 |

## Out of scope
- Clean and injection cases (P1-S15), the live command (P1-S16).

## Notes
- AC9's format errors use the eval command's own [exit codes](../design.md#exit-codes).
- **README (main session):** describe the `replay` command.
- **Changed in build, main session, 2026-11-07:** `evidence=` and the recording's date and model are on AI rows; rule rows say `evidence=rules`, as their results are real (AC2, AC7). Line windows are `[start, end]` pairs, files are validated strictly, and `verify` exposes `ai_anchor_valid` for AC5.
