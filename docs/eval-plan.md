# Eval plan

- **Status:** Accepted direction, not scheduled (2026-11-15). Work starts after Phase 4 closes, as part of the Phase 5 design. Phase 5's gate needs it: "each agent has its own eval set and acceptance target" ([roadmap](roadmap.md#phase-5-catalogue)). When a stage starts, the `designer` turns it into slice specs and ADRs in the usual workflow, so this file states direction and decisions, not specs.
- **Builds on:** [ADR 0007](adr/0007-evals-replay-and-live.md) (replay and live), [ADR 0011](adr/0011-feedback-labels.md) (feedback labels), [ADR 0016](adr/0016-measure-phase-3-against-phase-2.md) (paired comparison), [P4-S9](phases/phase-4/slices/p4-s9-prompt-eval-gate.md) (`evals gate`, `gate.toml`).
- **Background:** [eval-track-proposal.md](eval-track-proposal.md) explains how agent evals work and works through the statistics. This plan keeps its diagnosis and its method, and corrects its cost, scope and targets (see [What changed from the proposal](#what-changed-from-the-proposal)).

## 1. Why

The eval machinery is sound: versioned cases, live recordings replayed offline with freshness hashes, synthetic controls that prove the scorer can fail, injection scored before verification, and every attempt reported. The **evidence** is weak, and every phase close so far has needed an owner exception because of it.

| Gap | Today | Effect |
|---|---|---|
| Too few cases | 14 cases (about a dozen planted AI bugs), 3–5 runs each | Phase 3's precision comparison (49.1% vs 33.8%) was inconclusive |
| Per-bug gates | Each bug must be caught in 3 of 5 runs | A model that catches every bug 80% of the time still fails at least one of 12 gates about half the time |
| No held-out set | Prompts were tuned on the scored cases | Every eval number is optimistic by an unknown amount |
| Incomplete answer key | Valid findings nobody planted count as wrong | Eval precision (34–49%) measures the key, not the noise |
| Matcher error unknown | Keyword groups, a person confirms each credit | Doesn't scale; silent false credit is possible |
| One labeller, not blind | The owner judged 42/42 shadow units, knowing the source | No agreement measure |
| Thin noise and safety evidence | 2 clean cases; 8 injection cases | No false-positive rate per clean diff; 0 successes in 15 runs bounds attack success only at ≤ 20% |

## 2. Principles

1. **Pre-register the method, then measure.** A verdict is `pass`, `fail` or `inconclusive` (not enough data), decided by rules written in `gate.toml` before the run. Re-running until it passes voids the result.
2. **Numbers come after a baseline.** Thresholds other than the roadmap's are set from the first measurement under this plan, not guessed in advance.
3. **Gate on pooled numbers with intervals; keep per-bug rows as diagnostics.** Wilson intervals for one proportion, cluster bootstrap by case or PR for pooled metrics, paired tests (McNemar, paired bootstrap) for every A/B.
4. **Precision gates, recall informs**, until precision is proven ([roadmap north star](roadmap.md)).
5. **Code grades whatever code can decide** (anchors, rule bugs, injection canaries, noise counts, tool policy). A model judge is used only where it is calibrated against people.
6. **Replay stays offline and deterministic.** Anything model-graded is judged once at record time and stored with the recording.
7. **Every case records where it came from**, and every report splits by origin, because planted, mutated, mined and external cases differ in difficulty.
8. **Spend is sized before it is spent**, from the measured cost of the same kind of diff.

## 3. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | `evals gate` gains a minimum n per suite and an `inconclusive` verdict. Recall is gated pooled, with a Wilson lower bound; per-bug rows are reported, not gated | Removes the coin-flip failures of per-bug gates, and stops an under-powered run from passing or failing by luck |
| D2 | `evals power --baseline P --delta D [--paired]` prints the n a decision needs; a suite whose n is below it can only be `inconclusive` | A gate can't be inconclusive by design without anyone noticing |
| D3 | `eval-case/v2` adds `split` (`dev` \| `holdout`), `origin` (`planted` \| `mutation` \| `mined` \| `external`), a severity per bug, and tags. v1 cases read as `dev`, `planted` | Slicing and hold-out need the fields |
| D4 | Hold-out cases live in their own directory, with a CODEOWNERS entry for the owner. Only the release workflow scores them. CI fails a PR that changes a prompt or a model and also touches `holdout/` | One PR can't tune on the hold-out. Owner review guards the directory across PRs, which a per-PR check alone can't. The hold-out is refreshed each phase from newly mined bugs |
| D5 | Eval precision counts judged-valid extra findings as correct (`extra_valid` in the case) | An incomplete key otherwise reports real findings as noise |
| D6 | A model judge may replace the keyword matcher only for a question type whose agreement with people is κ ≥ 0.8, measured on labels we already have. It runs once at record time, and its model, prompt and hash join the freshness fields | Keeps ADR 0007's determinism and makes trust in the judge a number |
| D7 | The judge uses a different model family from the reviewer where the policy allows | Limits self-preference |
| D8 | Online precision is reported as the Wilson lower bound, from blind labelling, with a second labeller's agreement on a subset | The roadmap's ≥ 70% then holds up under review |
| D9 | `eval-report/v1`: one JSON per measurement (model, prompt, split, metrics with intervals, n, cost), rendered as an eval card in each release's notes | One comparable record per release |
| D10 | Mutation and external cases never gate on their own; a headline number includes mined cases | Mutants are unlike human mistakes, and models may have trained on public fixes |

## 4. Stages

Each stage ships on its own and ends with an exit check. The stages are ordered so that the cheap work that makes every later number decisive comes first.

| Stage | Work | Spend | People | Exit check |
|---|---|---|---|---|
| **0. Method** | D1, D2, D3, D9; `gate.toml` gains thresholds and minimum n | None (offline, `MockProvider`) | None | Today's suites report `pass`, `fail` or `inconclusive` with intervals, and `evals power` sizes each one |
| **1. Baseline** | Complete the key on existing recordings (D5); build the mutation set: billing-domain fixture repositories with tests, mutants kept only if a test kills them (target ~150); one live run of the mutation set | ~$10–20 (small diffs) | 4–6 h judging unmatched findings | A first baseline per metric, with intervals. **The owner sets the numeric targets from it** |
| **2. Real bugs** | Mined cases: reverse bug-fix commits from this repository and from owner-approved OSS repositories, a person confirming each; clean negatives (merged PRs with no fix touching their lines within 30 days); start the hold-out split; calibrate the judge (D6, D7); pass^k and stability for live cases | Sized by `evals power`; see §5 | ~1 min per mined case; licence choice | Held-out recall and offline precision measured on mined cases with `origin` splits; judge κ reported per question type |
| **3. Online and safety** | Blind labelling and a second labeller (D8); fix rate as a secondary signal; escaped-defect recall (trace each fix to the PR that introduced its lines); an injection library of payload families × vectors with committed paraphrases, scored per model | Red-team run sized to the bound wanted (0 in 300 attempts gives ≤ 1%) | Second labeller, 1–2 h | Online precision lower bound with κ; attack success with a 95% upper bound per allowed model |
| **4. Operate** | `evals-full` step in `live.yml` with its own cap; eval card per release; drift run after each model or prompt change and on a fixed cadence | Recurring, owner-approved | None routine | The eval card appears in the release notes, with the trend |

The harness stays agent-neutral (cases, graders, reports), so each Phase 5 agent brings its own cases and reuses the rest.

## 5. Cost model

Cost per run depends on the size of the diff, so estimates use the measured cost of the same kind of input:

| Input | Measured cost per run | Source |
|---|---|---|
| Planted eval case (0.5–3 KB diff) | $0.014 (diff mode), $0.018 (context) | P3-S15 live runs |
| A real PR of ~8 KB, context mode | $0.04–0.10 | Phase 3 self-review |
| Real merged PRs, diff mode | ~$0.40 | Phase 2 backfill, 21 reviews for $8.43 |

A run's spend is cases × runs × modes × cost per run, so the same suite size can cost tens or hundreds of dollars depending on its origin mix. For example, 150 mutants × 3 runs × 2 modes ≈ $15, while 100 mined cases × 3 runs × 2 modes is $25–240. Rules:

- Every live step has a dollar cap (ADR 0015), and its estimate is stated from this table before the owner approves it.
- `evals power` sets the run count: no more runs than the decision needs.
- Prompt work replays recordings, which costs nothing. The full suite runs live only at releases and after a model or prompt change.

## 6. Needs from the owner

Before stage 1:
1. Approve stage 1's spend and the judging time.

Before stage 2:
2. Choose the OSS repositories that may be mined, and confirm their licences. Decide whether SWE-bench Verified may be used as an `external` cross-check.
3. Name a second labeller.
4. Decide whether the judge may send code to the second provider (D7), given the redaction policy.

From stage 4:
5. A recurring budget for `evals-full`.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Contamination: models have seen public fixes | Split every report by `origin`; weight this repository's history and mutants; never gate on `external` alone (D10) |
| Mutants unlike real bugs | Kill-by-test filter; reported separately; headline numbers need mined cases |
| Judge bias | Calibration threshold (D6), cross-family judge (D7), keyword matcher kept as a fallback |
| Labeller fatigue and single-labeller bias | Blind order, small batches, second labeller, κ reported |
| Spend growth | Caps per step, estimates from §5, `evals power` sizing |
| Tuning to the dev split | Hold-out guarded by directory, owner review and CI (D4), refreshed each phase |
| Labelling stalls the plan, as #67 stalled two phase closes | Stages 0 and 1 need little labelling; each later stage states its hours up front, and its exit check waits for them rather than being waived |

## What changed from the proposal

The [proposal](eval-track-proposal.md)'s diagnosis, statistics and method stand. This plan changes five things:

1. **Cost.** The proposal priced 4,000 runs at the planted cases' $0.014–0.018 per run ($55–75). Mined and clean-negative cases are real-size diffs that cost $0.04–0.40 per run, so that run would cost roughly $160–1,600. §5 replaces the single figure with a model per kind of case.
2. **Scope and timing.** Nine workstreams are about a phase of work, not a side track beside Phase 4, so the plan starts with the Phase 5 design and is split into stages with exit checks.
3. **Targets.** Fixed numbers before any measurement (for example held-out recall ≥ 80%, κ ≥ 0.8 for labels, ≤ 0.3 findings per clean diff) would invite more owner exceptions. The plan pre-registers the method now and sets numbers from stage 1's baseline (principle 2). The roadmap's precision ≥ 70% stays.
4. **Hold-out enforcement.** A check on one PR can be split across two; D4 adds directory ownership and release-only scoring.
5. **Case sources.** This repository has under 100 merged PRs, so it can't supply 100 mined bugs or 100 clean diffs on its own. The mutation set (stage 1) comes before mining, and mining needs the owner's OSS choice (§6).
