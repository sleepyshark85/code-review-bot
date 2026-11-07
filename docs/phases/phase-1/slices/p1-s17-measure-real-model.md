# P1-S17: Measure the real model and hold CI to the result

- **Status:** Done
- **Depends on:** P1-S13, P1-S16. The user approved [open questions 1 to 4](../design.md#4-open-questions) on 2026-11-05.
- **Touches:** `tests/evals/` recordings and tests; `docs/phases/phase-1/test-report.md`

## Value
The team knows, with numbers, how the model does on the planted bugs, the clean diff and injection, and CI catches a pipeline change that would break those results.

## Acceptance criteria
**Budget approved by the user on 2026-11-05: `--runs 5 --max-calls 25`.** Anything beyond it, including any re-run, needs the user's approval again.
- **AC1 (manual, by the user or with their explicit approval; never in CI):** Given the approved budget, when `live` runs over `sample`, `clean` and the three injection cases, with `--runs 5 --max-calls 25`, unless the user sets others, then a `live` recording exists per case, and the PR records who ran it, the model, the date, the tokens and the full report.
- **AC2:** Given the recordings and the approved thresholds, then the test report states per bug and per source: rules `2/2` in every run; each AI bug against its `min_runs_caught`; each case against `min_passing_runs`, where a `no review` run doesn't pass. A person reads each bug's matched rationales and each `needs human review`, and the report records every credit they reject, with the score after rejection. A miss is stated as a miss.
- **AC3:** Given a miss, then the options are to change the prompt (bump the version, then re-record all cases), or a decision by the user recorded in the PR. **A re-run or a fixture change without a prompt change is a user decision.** Thresholds and expectations aren't lowered without one. Fixing one keyword after a run makes that case stale, and re-recording is 5 calls and a user decision, even for an under-credit.
- **AC4:** Given the committed recordings, then `pytest` runs `replay --require-live` offline, and fails if a `live` recording is missing or stale, below its thresholds, or of a model other than the default in [settings](../../phase-0/design.md#settings).
- **AC5:** Given any synthetic recording, then it can't satisfy AC4: `replay` won't read one from `recordings/live/`.
- **AC6:** Given the test report, then it states plainly what the numbers cover: this model, this prompt, these few diffs, five runs, and keyword matching confirmed by a person. It discloses that the prompt was tuned on these same cases. It records the anchor drop rate and drift, any `invalid_output` or refusal, and run-to-run finding counts, against [ADR 0006](../../../adr/0006-line-anchors-drop-never-snap.md)'s revisit condition.
- **AC7:** Given the whole suite with `PATH` lacking `claude` and no key, then AC4 still passes.
- **AC8:** Given more than one live attempt, then the test report lists **every** attempt, in order, with its date, prompt version, reason and result, including those whose recordings were replaced.
- **AC9:** Given the report, then it records each case's fingerprint recurrence rate, the `rule` tag rate and the dedup count, as input to Phase 2's comment keying.

## Interfaces
[P1-S14](p1-s14-eval-sample.md#interfaces) and [P1-S16](p1-s16-eval-live-command.md). Threshold reasoning: [open question 3](../design.md#4-open-questions).

## Out of scope
- Repeat measurement on a schedule, or a new-model check: Phase 4's release gate.

## Notes
- This slice closes the gate's live column.
- **README (main session):** line 114's gate gets its sentence now that the user has approved open question 2 (deferred to this slice's PR). Update line 5's status line when the gate closes.
