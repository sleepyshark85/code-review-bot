# P4-S11: See whether a finding class has earned gating

- **Status:** Approved
- **Depends on:** P4-S5, P4-S10 (its `gate/v1` files)
- **Touches:** `pr.metrics`, `pr.run_metrics`, `pr.metrics_report`, `pr.cli`

## Value
The owner approves a class for gating on numbers, not on a hunch: its judged precision, how often it would have blocked, and how often a block was overridden, each set against a fixed bar ([Q4](../design.md#6-open-questions)).

## Acceptance criteria
Offline, over `shadow/v1` reports, `feedback/v1` labels and `gate/v1` files. Precision, units and Wilson intervals as in [Phase 2](../../phase-2/design.md#how-the-gate-is-evidenced). The bar: at least 20 judged, a Wilson 95% lower bound of at least 0.80, and, once gating, an override rate of at most 10%.
- **AC1:** Given `pr metrics --by-class` with reports and labels, then for each rule part it shows units, judged, accepted, rejected (by reason), precision, the Wilson lower bound, would-block units (from each report's `gate.blocking`), and `eligible` `yes` or `no` with the first unmet condition.
- **AC2:** Given `swallowed-exception` with 20 judged and 20 accepted, then the lower bound is 0.839 and it is eligible.
- **AC3:** Given 25 judged with 23 accepted, then the lower bound is 0.750 and it isn't eligible: "lower bound 0.750 is under 0.80".
- **AC4:** Given 19 judged, all accepted, then it isn't eligible: "19 judged, needs 20".
- **AC5:** Given `--gates DIR` holding `gate/v1` files where `swallowed-exception` blocked 10 distinct (PR, fingerprint) units and 2 were overridden, then the override rate is 20%, and it isn't eligible: "override rate 20% is over 10%". With no gate files, the rate is shown as `n/a` and isn't a condition.
- **AC6:** Given `possible-secret`, then it is listed with its would-block count, overrides and `eligible` `yes (deterministic)` whatever the labels. Its blockers have fingerprints, clear on a fresh whole-diff scan and have a defined override (P4-S5 AC6, P4-S10 AC6), per [Q4](../design.md#6-open-questions).
- **AC7:** Given a run that re-reviews the same PR, then a unit counts once, as in Phase 2, and the latest verdict wins. `--format json` adds `classes` to `metrics/v1`.

## Limits
As `pr metrics` (P2-S10): each report and label file is at most 10 MB, and `gate/v1` files have the same cap. A malformed file exits 2, naming it.

## Interfaces
`pr metrics ... --by-class [--gates DIR]`. Thresholds are constants in `pr.metrics`, named in the output.

## Out of scope
- Writing the approval into the policy: the owner, in a reviewed change.

## Notes
