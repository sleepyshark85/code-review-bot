# P2-S12: Close Phase 2: measure the gate, name the owner, release 0.3.0

- **Status:** Draft
- **Depends on:** P2-S1 to P2-S11, P2-S13, and Q1's weeks of shadow mode
- **Touches:** `tests/evals/labelled/`, `docs/phases/phase-2/test-report.md`, `pyproject.toml`; README, runbook and release notes by the main session

## Value
The phase closes on evidence: measured shadow precision, a captured baseline and a named owner, released as 0.3.0.

## Acceptance criteria
Mostly recorded evidence, as in [P1-S17](../../phase-1/slices/p1-s17-measure-real-model.md); definitions in [How the gate is evidenced](../design.md#how-the-gate-is-evidenced).
- **AC1 (recorded):** The test report gives the live window's dates and length in weeks, the reviewed PRs (live and backfilled), the would-be comments, the judged count and coverage, and whether [Q1](../design.md#5-open-questions) is met.
- **AC2 (recorded):** It gives shadow precision: the point estimate, its count, its Wilson 95% interval and the lower bound counting duplicates. It splits by origin, source, rule part and reject reason, and gives the `wrong_line` count, the PR count and the largest PR's share. It says whether 70% is reached, notes the clustering caveat, and includes the full `metrics` output.
- **AC3 (recorded):** It gives the `baseline/v1` numbers, with `until` no later than the day comments were turned on. It adds cost per accepted finding at the user's prices, and comment precision with its coverage if comments were on.
- **AC4 (recorded):** It gives the start-up cost: review time against end-to-end time, and the set-up step times from one Actions run.
- **AC5 (recorded):** It states the user's switch decision and date, or that the switch is deferred and why. It also states the dogfooding limits: agent-written PRs, one merger, already-reviewed backfilled PRs, and a prompt tuned on Phase 1's cases.
- **AC6:** Given the committed `tests/evals/labelled/labels.jsonl`, `would-be.jsonl` and `baseline.json`, then a test recomputes shadow precision, count and coverage offline, reproduces AC2 exactly, and finds no body, rationale or username in any line. `would-be.jsonl` holds one line per unit, with `repo`, `pr`, `file`, `rule_part`, `anchor`, `index`, `source` and `origin`.
- **AC7 (recorded):** README Ownership names the user as owner, the support channel (issues labelled `review-bot`), the runbook and who funds the key. The status line says Phase 2 is complete.
- **AC8 (recorded):** The runbook covers turning the bot off (`REVIEW_BOT_MODE=off`), rotating the key, reading a failed run and its artifact, every `outcome` and `reason`, and checking spend. A person confirms, with the date, that branch protection on `main` still requires only `gate`.
- **AC9:** `pyproject.toml` is at `0.3.0`, and `docs/phases/phase-2/release-notes.md` lists the changes. The tag and release follow [CLAUDE.md](../../../../CLAUDE.md).

## Limits
None beyond S10 and S11.

## Interfaces
The main session derives `would-be.jsonl` from the shadow reports, grouping as `label` does ([P2-S4](p2-s4-label-shadow-findings.md)).

## Out of scope
- Re-running shadow mode until precision passes: a shortfall goes to the user with the numbers.

## Notes
- **Closing designer task:** mark every spec `Done`, apply the user's answers to Q7 and Q11 (README changes go through the main session), and record the handover seams for Phase 3: the labelled set, `shadow/v1` and `feedback/v1`.
