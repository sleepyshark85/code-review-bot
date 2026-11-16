# Eval track: from gate evidence to an industry-grade eval system — proposal

- **Status:** Proposed (2026-11-14). Not yet split into slices: once the user approves it, the `designer` turns each workstream into `docs/phases/eval/design.md`, slice specs and ADRs, in the usual workflow.
- **Builds on:** [ADR 0007](adr/0007-evals-replay-and-live.md) (replay and live), [ADR 0011](adr/0011-feedback-labels.md) (feedback labels), [ADR 0016](adr/0016-measure-phase-3-against-phase-2.md) (paired comparison), [P4-S9](phases/phase-4/slices/p4-s9-prompt-eval-gate.md) (`evals gate`, `gate.toml`).
- **Serves:** [Roadmap targets](roadmap.md#targets) (precision ≥ 70%, injection eval on every release, fewer escaped defects) and the [Phase 5 gate](roadmap.md#phase-5-catalogue) ("each agent has its own eval set and acceptance target").
- **Background and reasoning:** sections 1–7 are the proposal. [Appendix A](#appendix-a-how-agent-evals-work) explains how agent evals work and the layer model used here; [Appendix B](#appendix-b-reasoning-behind-each-decision) gives the reasoning behind each decision; [Appendix C](#appendix-c-the-statistics-worked-through) works through the numbers; [Appendix D](#appendix-d-one-bug-through-the-old-and-new-eval) follows one bug through today's eval and the proposed one.

## 1. Why

The machinery is sound: versioned cases, live recordings replayed offline with freshness hashes, synthetic controls that prove the scorer can fail, injection scored before verification, shadow precision with Wilson intervals, and honest disclosure. The **evidence** is what's weak, and every phase close so far has needed an owner exception because of it:

| Problem | Evidence today | Consequence |
|---|---|---|
| Too few cases | 13 cases, 12 AI bugs, 3–5 runs | Phase 3 precision 49.1% vs 33.8% was inconclusive; detecting a 15-point gain needs ~170 findings per arm |
| No held-out set | Prompts tuned on the scored cases ([P3-S15 disclosures](phases/phase-3/test-report.md#ac5-the-gate-under-q3-and-q4)) | Every eval number is optimistic by an unknown amount |
| Incomplete answer key | Eval precision 34–49% vs shadow precision 100% | Eval "precision" measures the key's completeness, not noise |
| Approximate matcher, error rate unknown | Keyword groups; a person confirms each credit (ADR 0007) | Doesn't scale past ~20 cases; silent false credit possible |
| One judge, not blind | 42/42 judged by the owner, knowing the model and mode | No inter-rater agreement; possible bias |
| Real-code recall unknown | Pooled relative recall only; P3 labelled comparison not run | "Fewer escaped defects" can't be shown |
| Thin noise evidence | 2 clean cases | No false-positive rate per clean PR |
| Run-to-run churn not measured | Fingerprints dedupe, but variance isn't reported | Comment churn erodes trust unseen |

## 2. Target: what "industry-grade" means here

Thresholds are pre-registered in `gate.toml` before a measurement, and a verdict without enough data is `inconclusive`, never `pass`.

| Dimension | Metric | Target | Sample needed |
|---|---|---|---|
| Recall (offline) | Share of held-out AI bugs caught, per severity; plus **pass^k** (caught in all k runs) | Held-out recall ≥ 80% for `high`+; pass^5 reported | ≥ 250 bug instances for ±5 points |
| Precision (offline) | (matched + judged-valid extra) ÷ all findings on cases | ≥ 70% | ≥ 150 findings per arm |
| Precision (online) | ADR 0011's formula, **Wilson lower bound** | Lower bound ≥ 70% | ~80 judged units if true precision is ~80%; ~25 if ~90% |
| Noise | Findings per clean diff; share of clean diffs with any `medium`+ | ≤ 0.3 per diff; ≤ 5% | ≥ 100 clean diffs |
| Safety | Injection attack success rate (ASR), per model | 0 successes; 95% upper bound ≤ 1% | ≥ 300 attack runs (rule of three) |
| Anchors | Invalid anchors reaching a PR | 0 (exists) | every run |
| Stability | Jaccard of fingerprints across k runs of one diff | ≥ 0.8 median | every live case |
| Grader quality | Judge vs human: Cohen's κ, TPR, TNR | κ ≥ 0.8 | ≥ 150 human-labelled pairs |
| Label quality | Inter-rater κ on a blind subset | κ ≥ 0.7 | ≥ 50 units, 2 labellers |
| Real-world value | Escaped-defect recall: later fixes whose lines the bot flagged | Tracked; target set after 1 quarter | all fix PRs |
| Cost, latency | $ per accepted finding; p95 | As [Targets](roadmap.md#targets) | exists |

## 3. Workstreams

Each is about one to three slices. "Offline" means testable with `MockProvider` and fakes, as today.

### E1. Metric contract and statistics (offline, foundation)
- **ADR:** the metric set above, its formulas, and the statistical rules: Wilson per proportion; **cluster bootstrap by case or PR** for pooled metrics (comments within a PR are correlated); **paired tests** (McNemar per bug, paired bootstrap per unit) for every A/B; pass^k beside the share of runs.
- `evals power --baseline P --delta D [--paired]`: the n needed, so a gate can't be inconclusive by design.
- `gate.toml` gains per-suite thresholds and a minimum n; `evals gate` reports `inconclusive` when n is short.
- `eval-report/v1`: one JSON per measurement (model, prompt, split, metrics with intervals, n, cost), the basis of an eval card per release.

### E2. Case schema v2 and a held-out split (offline)
- `eval-case/v2` adds `split` (`dev` | `holdout`), `origin` (`planted` | `mutation` | `mined` | `external`), `severity` per bug and free `tags`. v1 cases read as `dev`, `planted`.
- **Hold-out discipline:** prompt work replays `dev` only. `holdout` is scored only by `evals gate` at a phase close or release. A CI check fails a PR that changes a prompt and a `holdout` case together. Reports always split metrics by `split` and `origin`.

### E3. Scale the case set
- **Mutation cases (offline generator):** AST mutation operators on Python fixture repos in the billing domain (flip a sign, off-by-one, swap arguments, float for money, drop a guard, invert a comparison, remove an `await`). Each fixture has tests; a mutant is kept only if a test kills it, so it's a real behaviour change, and its line is exact. Target ≥ 150 cases. Never gate on mutants alone: they're easier or harder than real bugs, so they're reported by `origin`.
- **Mined real bugs:** from this repository's history and approved OSS Python repositories, take bug-fix commits (fixes, reverts, linked issues), **reverse the fix** to get a diff that introduces the bug, and use the fix's changed lines as the answer key. A person confirms each case (~1 min). SWE-bench Verified's gold patches can be reversed the same way as an `external` set. Target ≥ 100 cases.
- **Clean negatives:** merged PRs with no fix touching their lines within 30 days, as approximate clean diffs. Target ≥ 100.
- Context cases keep their `repo/` fixtures; mined cases get a pinned commit instead of a copied repo.

### E4. Complete the answer key (needs a person)
- `evals label-extra`: for each live recording, a person judges every **unmatched** finding as `valid`, `wrong` or `noise`, with the same blind order as E7. Valid ones become `extra_valid` entries in the case, so offline precision is (matched + extra valid) ÷ all.
- The judgements double as calibration data for E5.

### E5. A frozen, calibrated judge (resolves ADR 0007's objection)
- The judge (does finding F report bug B? is extra finding F valid?) runs **once, at record time**, inside `evals live`. Its verdicts are stored in the recording, and the judge model, prompt and hash join the freshness fields. Replay stays deterministic and offline.
- Calibrated against human data: confirmed credits, E4 labels, `labels.jsonl`. Report κ, TPR and TNR per question type. The judge replaces the keyword matcher only for question types with κ ≥ 0.8; elsewhere the matcher stays and its own error rate is reported.
- Use a judge from a different model family where the policy allows (Phase 4's second provider), to limit self-preference; otherwise report it as a risk.

### E6. Reliability and stability
- Default k = 5 for live evals (spend permitting), pass^k per bug and per case.
- Stability: Jaccard of fingerprints across the k runs of each case, and across re-runs at the same head SHA in shadow reports.
- Cost and latency per run, with intervals, in `eval-report/v1`.

### E7. Online evidence (needs people)
- **Blind labelling:** `pr label --blind` hides model, prompt, mode and origin, and randomises order.
- **Second labeller** on a ≥ 50-unit subset; κ reported with every precision number.
- **Fix rate** (secondary signal, never precision): whether the anchored line changed in a later commit of the same PR. Covers the comments nobody reacts to.
- **Escaped-defect recall:** for each merged fix, find the PR that introduced its lines (`git blame` at the fix's parent), and check whether the bot flagged them within a window. This is the only recall measure on real code, and it serves the roadmap's value target.
- **Steady sampling in comment mode:** a weekly random sample of posted comments is labelled, so precision isn't limited to the comments people reacted to.

### E8. Red-team suite (safety gate)
- An attack library: payload families (instruction override, delimiter escape, canary exfiltration, file request, fake approval) × vectors (diff, tool result, issue, file content, commit message), with paraphrased variants generated once and committed.
- ASR per model and mode, with a 95% upper bound. 0 in ≥ 300 attempts gives ≤ 1%. This becomes a `gate.toml` suite for every model the policy allows.
- The rules-only degraded mode ([ADR 0021](adr/0021-route-on-availability-failures-then-rules-only.md)) gets its own suite: rule bugs 100%, no model call.

### E9. Operations: cadence, drift and reporting
- A `live.yml` step `evals-full` with its own cap. A rough estimate at today's mean cost ($0.014–0.018 per run): 400 cases × 5 runs × 2 modes ≈ $55–75 per full run. Run it at every release, every model or prompt change, and monthly to catch provider-side drift.
- The eval card per release: `eval-report/v1` rendered to Markdown in the release notes, with the trend against the previous release.
- The harness stays agent-neutral (cases, graders, reports), so the Phase 5 catalogue reuses it for the next agent.

## 4. Order

| Wave | Workstreams | Spend | Unblocks |
|---|---|---|---|
| 1 | E1, E2 | none | Pre-registered, power-checked gates; a held-out split |
| 2 | E3, E4 | one live run of the new cases | Enough data for decisive verdicts |
| 3 | E5, E6 | a calibration run | Scalable grading; reliability numbers |
| 4 | E7, E8 | labelling hours; red-team run | Trustworthy online precision; safety gate |
| 5 | E9 | recurring | The eval card per release; drift detection |

The track runs **alongside Phase 4**, as the roadmap's "Alongside the build" items do. It touches only `review_bot.evals`, `pr.label`, `pr.metrics` and `tests/evals/`, and extends P4-S9's `gate.toml` rather than replacing it. Its output is the eval set and acceptance target the Phase 5 gate asks for.

## 5. Needs a person

| Task | Estimate |
|---|---|
| Confirm mined cases | ~100 × 1 min |
| Judge unmatched findings (E4) | ~200 findings, 4–6 hours |
| Second labeller (E7) | ≥ 50 units, 1–2 hours |
| Approve spend: one full live run, then monthly | ~$75 per run |
| Choose the OSS repositories to mine, and check licences | one decision |

## 6. Risks

| Risk | Mitigation |
|---|---|
| **Contamination:** models have seen OSS fix commits | Report by `origin`; weight this repo's history and mutants; never gate on `external` alone |
| Mutants unlike real bugs | Kill-by-test filter; reported separately; gates need `mined` too |
| Judge bias, including self-preference | Calibration with κ threshold; cross-family judge; matcher kept as a fallback |
| Labeller fatigue, single-person labelling | Blind order, small batches, second labeller, κ reported |
| Spend growth | Per-step caps (ADR 0015); `evals power` sizes runs to what the decision needs |
| Goodhart: tuning to the dev split | Hold-out discipline enforced in CI; hold-out refreshed every phase from newly mined bugs |

## 7. Open questions

1. Run this as a parallel track now, or fold it into Phase 5's gate?
2. Which OSS repositories may be mined, and is reversing SWE-bench Verified acceptable?
3. Who is the second labeller?
4. The monthly live budget for `evals-full`.
5. May the judge use the second provider's model (cross-family), given the redaction policy?

---

## Appendix A: how agent evals work

This appendix is the background the plan rests on. It is written for this bot, but the ideas are general, and the Phase 5 catalogue will need them for every agent.

### A1. Why an agent is harder to evaluate than a single model call

A classic model eval is input → output → grade. An agent, and this bot in context mode, differs in five ways:

| Property | General agent | This bot |
|---|---|---|
| **Many steps, errors compound** | 95% per step over 20 steps is 36% end to end (0.95^20) | Context mode is a short tool loop (mean 0.6 tool calls per run in P3-S15), so compounding is small today, but grows with every tool Phase 5 adds |
| **Output is a trajectory, not a string** | Tool calls, arguments, intermediate state, side effects | Findings, plus the ledger of tool calls and evidence, plus the comments it would post |
| **Non-determinism** | Same input, different path | Same diff, different findings: `double-tax` was caught in 1/3 diff runs |
| **Acts on the world** | Writes, payments, emails | Posts comments; Phase 4 can block merges |
| **Several right answers** | Different valid tool sequences | Different wordings and lines for the same issue, and valid findings nobody planted |

The last row is the hardest for a reviewer. **The ground truth is a set, and it is never complete.** Any diff can hold issues the case author didn't plant. So a reviewer eval must keep recall (against a known key) apart from precision (which needs every finding judged), and must not treat "unmatched" as "wrong". That is the root of gap 3 in section 1.

**Core principle: grade outcomes and invariants, not exact paths.** Check hard rules on the trajectory (no tool outside the policy, no write action, no file outside the commit), and grade the findings themselves on what they claim, not on the route the model took to them.

### A2. The layer model

An agent can fail at several levels, and each needs its own grader. A score at one layer says nothing about another: the pipeline can be perfect while the model misses every bug, and the model can be excellent while the anchors are wrong. This table maps each layer to what the repository has and what the plan adds.

| Layer | Question | Best grader | Today | Plan |
|---|---|---|---|---|
| **L0 Pipeline correctness** | Does the code around the model behave as specified? | Unit, integration and conformance tests with `MockProvider` | Strong: ~6,500 tests, 99% coverage, conformance suites per provider. These are tests, not evals: they hold the model fixed | — |
| **L1 Output validity** | Is every model output well-formed and grounded? | Code: schema parse, anchor check, suggestion scrub | Strong: strict parse, anchors dropped never snapped (ADR 0006), invalid output is a recorded failure | Report the invalid-output rate per model as a metric, not only a failure |
| **L2 Detection (recall)** | Does it find the issues that are there? | Answer key + matcher, or calibrated judge | 12 AI bugs, keyword matcher, a person confirms each credit | E2, E3 (more cases, held-out), E5 (judge) |
| **L3 Finding quality (precision)** | Is each finding correct and worth a comment? | Human judgement; calibrated judge at scale | Shadow 42/42 by one judge; eval precision distorted by an incomplete key | E4 (complete the key), E5, E7 (blind, two labellers) |
| **L4 Noise** | Does it stay quiet when there is nothing to say? | Code: count findings on clean diffs, against a budget | 2 clean cases with `noise_budget` | E3 (≥ 100 clean diffs) |
| **L5 Trajectory and tools** | Did the path break a rule? | Code: assertions on the ledger and audit log | Strong: tool lockdown asserted, policy-denied calls recorded, read-only tools confined to one commit | Add tool-use metrics to `eval-report/v1` (calls, refusals, wasted calls) |
| **L6 Safety** | Can PR content take control of it? | Code: canary and requested-file checks, scored before verification | 7 injection cases, 15/15 per mode in P3 | E8 (payload × vector library, ASR with an upper bound) |
| **L7 Reliability** | Does it do the right thing every time? | Repeated runs: pass^k, stability | Share of runs per bug (`min_runs_caught`) | E6 (pass^k, Jaccard stability) |
| **L8 Operations** | Is it fast, cheap and available enough? | Code: latency, tokens, cost, outcome counts | Strong: `pr metrics`, caps, SLO work in Phase 4 | Cost and latency with intervals per eval report |
| **L9 Real-world value** | Does it change outcomes for developers? | Production signals | Baseline captured; cost per accepted finding | E7 (fix rate, escaped-defect recall, steady sampling) |

L0, L1, L5 and L8 are already industry-grade. L2, L3, L4, L6, L7 and L9 are where the plan spends its effort, because they are where the model's own behaviour is measured, and where the sample is small.

### A3. Graders, from most to least trustworthy

1. **Code graders.** Exact match, schema checks, line windows, canary detection, budget counts. Deterministic, free, can't be argued with. Use them for everything they can decide. Here: anchors, rule bugs, injection, noise counts, tool policy.
2. **A calibrated model judge.** For questions code can't decide: does this rationale describe this bug; is this extra finding a real issue. Rules that make a judge trustworthy:
   - **One narrow question per call, with a binary answer.** "Does the finding describe the tax being applied twice? yes/no", not "rate this finding 1–10". Scales drift and nobody agrees what a 7 is.
   - **Calibrate before trusting.** Compare against human labels and report agreement (κ, TPR, TNR). A judge with κ < 0.8 is a noisy labeller, and its numbers must carry that noise.
   - **Known biases:** position bias in pairwise comparisons (swap and run twice), verbosity bias (longer rationales look better), self-preference (a model rates its own family's output higher). A different model family reduces the last one.
   - **Version it like a prompt.** A judge change is a change to the metric; its hash belongs in the freshness fields.
3. **People.** The ground truth for calibration and for new kinds of failure. Expensive and inconsistent, so they need blind order, clear verdict definitions (ADR 0011 has these), and a second labeller to measure their own agreement.

The keyword matcher sits between 1 and 2: it is code, but it approximates a semantic question. That is why ADR 0007 has a person confirm each credit, and why the plan measures its error rate instead of assuming it is zero.

### A4. Where cases come from

| Origin | How | Strengths | Weaknesses | Use for |
|---|---|---|---|---|
| **Planted** (today) | A person writes a diff with known bugs | Targets exact behaviours; small; readable | Expensive; author bias; tends to be easier than real bugs; few | Regression tests of specific behaviours; diagnostics |
| **Mutation** | Apply a code mutation to working code; keep it if a test catches it | Cheap at scale; exact line; behaviour change proven by the test | Unlike how people make mistakes; single-line bias | Recall at volume; per-operator diagnostics |
| **Mined** | Reverse a real bug-fix commit | Real bugs, real code, real difficulty | Needs a person to confirm; the fix may not mark every buggy line | Headline recall numbers |
| **External** | Reverse public benchmark fixes (SWE-bench Verified) | Already curated and verified | **Contamination**: models may have trained on these repos and fixes | A cross-check, never a gate on its own |
| **Production** | Shadow and comment-mode findings, judged by people | The real distribution | Only sees what the bot found: no recall on its own | Precision; new failure categories; regression cases |
| **Clean negatives** | Merged PRs with no fix within N days | Real code; measures noise | "No fix" isn't "no bug" | Noise rate |

Three habits matter as much as the sources:
- **Every production failure becomes a case.** A rejected comment in comment mode, an injection that got through, an escaped defect: each gets a case in `dev`, so the suite grows from reality.
- **Tag and stratify.** Severity, category, origin, needs-context, language feature. An aggregate hides regressions in a slice, and a slice is where a fix goes.
- **Hold out.** A held-out split that prompt work never sees is the only way to know how optimistic the dev numbers are. Refresh it each phase, or it slowly becomes dev.

### A5. Environments: what replay proves

The repository already has the right structure (ADR 0007). In general terms:

| Approach | What it proves | What it can't |
|---|---|---|
| **Mock model** | The pipeline handles a given output | Anything about a model |
| **Record and replay** (here) | Today's pipeline still turns a past model response into the right result | How the model behaves today, or with a changed prompt |
| **Live run** | How this model and prompt behave now | Reproducibility; it costs tokens |

The freshness hashes are what make replay honest: a recording only counts for the exact case, diff and prompt it was made with. The plan keeps that and adds the judge's model and prompt to the hashed fields (E5), so a frozen verdict can't outlive its judge.

For context mode, the recordings already hold each tool call's spans and result size. For future live runs of a **changed** prompt there are two options: serve tool results from the recording when the call matches and fall back to the fixture repository when it doesn't (logging the divergence), or always run the tools against the fixture repository. The second is simpler here, because the tools are read-only and local.

### A6. Statistics for small, noisy samples

| Idea | What it says | Where it bites this repository |
|---|---|---|
| **Binomial intervals** | A share from n trials has an uncertainty of about ±1.96·√(p(1−p)/n); use Wilson near 0% or 100% | 42/42 is "91.6% to 100%", not "100%" |
| **Power** | The n needed to detect a difference must be computed before measuring | Phase 3 needed ~170 findings per arm and had 57 and 68 |
| **pass@k vs pass^k** | pass@k: caught in at least one of k runs (capability). pass^k: caught in all k runs (reliability) | A bug caught 80% of the time per run is caught in all 5 runs only 33% of the time |
| **Clustering** | Comments in one PR are correlated, so n comments carry less than n independent votes | 42 units from 24 PRs carry about as much as 35 independent ones (C7) |
| **Paired comparison** | Compare A and B on the same items and look at the items that flipped | ADR 0016 pairs by head SHA; the test on top (McNemar, paired bootstrap) is the missing piece |
| **Multiple comparisons** | Many per-item gates fail by chance even when the system is good | 12 per-bug gates fail at least one about half the time (C4) |
| **Pre-registration** | State the threshold and n before measuring; re-running until it passes voids the result | The repository already refuses re-runs without a prompt change; the plan moves thresholds into `gate.toml` before the run |

### A7. What to measure for a code reviewer

A reviewer's value is asymmetric: a missed bug costs one bug, but noisy comments train developers to ignore the bot, which loses every future finding. That is why the roadmap's north star is "signal over volume", and why **precision gates and recall informs** until precision is proven.

| Metric | Definition | Why |
|---|---|---|
| Precision | accepted ÷ judged (ADR 0011), with the Wilson lower bound | Trust; the roadmap's headline target |
| Recall per severity | caught ÷ known, per severity tier | A missed `critical` matters more than five missed `low`s |
| pass^k | caught in every one of k runs | Developers see one run; it must be reliable |
| Noise per clean diff | findings on diffs with nothing to say | The direct measure of "signal over volume" |
| Stability | Jaccard of fingerprints across runs | Churn between pushes looks like noise even when each comment is right |
| Fix rate | anchored line changed later in the same PR | Objective, covers comments nobody reacts to; secondary because a change isn't proof of agreement |
| Escaped-defect recall | later fixes whose lines the bot had flagged | The only recall on real code; ties to "fewer escaped defects" |
| Attack success rate | injection runs where the model obeyed the payload | Safety; must be near zero with a stated upper bound |
| Cost per accepted finding | spend ÷ accepted | Value; already in `pr metrics` |

### A8. The loop that keeps evals honest

```
  error analysis: read outputs, name failure categories ◄──────────────────────────┐
         │                                                                          │
         ▼                                                                          │
  new or changed case (dev) ──► prompt / model / tool change ──► replay in CI       │
                                                                    │               │
                                          live run on dev, then holdout ──► eval card
                                                                    │               │
                                    shadow ──► comment ──► labels, fix rate, escaped defects
```

1. **Error analysis comes before metrics.** Read 50–100 real outputs, write what went wrong in plain words, then group the notes. Build cases for the failures that actually happen. The test reports already do this in "Readings for the designer"; the plan does the same for model output.
2. **Offline gates** stop regressions: replay in CI on every PR, `evals gate` at releases.
3. **Online signals** catch what offline cases don't cover, and feed new cases back.
4. **Model and prompt changes are eval events.** A new model gets the full suite before the policy allows it (P4-S9 already requires this).

### A9. Anti-patterns, and where this repository stands

| Anti-pattern | Status here |
|---|---|
| Building metrics before reading failures | Avoided: test reports read every miss |
| A judge where code can decide | Avoided: code graders everywhere they apply |
| Grading exact trajectories | Avoided: findings are graded, the path only on rules |
| One run, and a small gain called a win | Avoided by disclosure (P3 "inconclusive"); the plan prevents it by design |
| One aggregate score hiding slices | Partly: per-bug rows exist; no tags or severity slices yet (E2) |
| Tuning on the reported set | **Present**, disclosed: no held-out split (E2) |
| An answer key treated as complete | **Present**: eval precision counts valid extras as wrong (E4) |
| An uncalibrated grader | **Present**: matcher error rate unknown (E5) |
| One labeller, not blind | **Present** (E7) |
| A stale eval set | Risk: cases don't yet grow from production (E3, E7) |
| Re-running until it passes | Avoided: every attempt reported (ADR 0007) |

## Appendix B: reasoning behind each decision

**Why statistics and the held-out split come first (wave 1), before more cases.** More cases scored with the same rules would just give a more precise optimistic number. Pre-registered thresholds, power checks and a held-out split cost no tokens, and make every later measurement decisive. They also change what the next phase close can say: "passed", "failed" or "not enough data", without needing an exception.

**Why mutation cases need a test that kills them.** A mutation can leave behaviour unchanged (an equivalent mutant), and asking a reviewer to flag code that still works asks it to be wrong. Keeping only mutants that a fixture test catches proves each is a real behaviour change, and gives an exact line for free.

**Why reverse bug fixes.** A fix commit is a person saying "these lines were wrong". Reversing it gives a diff that introduces a real bug in real code, with the fix's lines as the key. This is how SWE-bench-style datasets get ground truth at scale. A person still confirms each one, because some fixes also refactor, and some bugs span lines the fix didn't touch.

**Why not gate on mutants or external cases alone.** Mutants are unlike human mistakes, and external repositories may be in the model's training data, which inflates recall. Both are useful for volume and diagnosis; the headline number must include mined cases from code the model hasn't seen, and every report splits by origin so the difference is visible.

**Why a frozen judge, not a live judge in the scorer.** ADR 0007 rejected a judge because it would make scoring non-deterministic and need a live call in CI. Running the judge once, when the live recording is made, and storing its verdict keeps both properties: replay stays offline and deterministic, and the judgement is versioned with the recording. It only changes when someone pays for a new live run, which already happens for prompt changes.

**Why calibrate the judge on data the repository already has.** Every credit a person confirmed, every `labels.jsonl` verdict and every E4 judgement is a free human label. Using them costs nothing new and turns "trust the judge" into a number.

**Why a different model family for the judge.** Models tend to rate their own family's output higher. Phase 4's second provider makes a cross-family judge possible; the redaction policy decides whether it may see the text, hence open question 5.

**Why report the Wilson lower bound, not the point estimate.** 42/42 sounds like 100%, but 42 votes support anything from 91.6% up. A gate on the lower bound can't be met by luck on a small sample, and it rewards collecting more data.

**Why fix rate is a secondary signal, never precision.** A changed line might be the developer agreeing, or an unrelated edit. ADR 0011 rejected "resolved means accepted" for the same reason. Beside explicit verdicts, it covers the many comments nobody reacts to, and a large gap between the two signals is itself worth investigating.

**Why escaped-defect recall.** Every other recall number is against a key someone wrote. Bugs that reached `main` and were later fixed are the real misses. Tracing each fix back to the PR that introduced its lines, and asking whether the bot flagged them, measures recall on real work and the roadmap's value target together.

**Why blind labelling and a second labeller.** The owner judged all 42 units knowing which system produced them, on PRs written by agents the owner directs. That needn't mean bias for the number to need defending. Hiding the source removes one doubt; a second labeller's agreement (κ) quantifies the other.

**Why a payload × vector library for injection.** Seven hand-written cases test seven attacks. Attacks transfer between channels (a payload that fails in the diff may work in a tool result), and wording matters. A grid of families, vectors and paraphrases, run k times, turns "passed our cases" into "fewer than 1% of attacks succeed, at 95% confidence".

**Why run it alongside Phase 4.** It touches only `review_bot.evals`, the labelling and metrics commands, and `tests/evals/`, so it doesn't conflict with Phase 4's governance slices. It extends P4-S9's `gate.toml`, which Phase 4 is building anyway. And Phase 5's gate needs an agent-neutral eval system before a second agent arrives.

**Why the spend is justified.** A full run is estimated at $55–75 (C8). Phase 3 spent $1.02 and couldn't reach a verdict. A decision that needs an owner's exception costs more in trust than the tokens that would have made it decisive.

## Appendix C: the statistics, worked through

**C1. Sample size for recall within ±5 points.** At an expected 80% recall: n = 1.96² × 0.8 × 0.2 ÷ 0.05² ≈ 246, so **~250 bug instances**. Today's 12 AI bugs give about ±23 points.

**C2. Power for Phase 3's precision comparison.** Detecting 34% against 49% (two-sided α = 0.05, power 0.8): n = (1.96·√(2·0.415·0.585) + 0.84·√(0.34·0.66 + 0.49·0.51))² ÷ 0.15² ≈ (1.366 + 0.586)² ÷ 0.0225 ≈ **169 findings per arm**. Phase 3 had 57 and 68. A paired design on the same cases needs fewer.

**C3. Wilson lower bound for online precision.** To show precision ≥ 70% with 95% confidence:
- if true precision is ~90%, about 25 judged units give a lower bound of ~72%;
- if it is ~80%, about 80 units give a lower bound of ~70%.

Phase 2's 42/42 already clears 70% (lower bound 91.6%). The plan asks for the lower bound, blind, with a second labeller, so the number holds up under review.

**C4. Why per-bug gates fail by chance.** Say every bug is truly caught 80% of the time, and each needs 3 of 5 runs. One bug fails its gate with probability P(X ≤ 2 | n = 5, p = 0.8) ≈ 5.8%. Over 12 bugs, the chance that **at least one** fails is 1 − 0.942¹² ≈ **51%**. So per-bug gates on small k flip a coin even for a good model, which is part of why phases closed with accepted misses. The fix: gate on pooled recall with an interval, and keep per-bug rows as diagnostics (or raise k, or correct for multiple comparisons).

**C5. pass^k.** With a per-run catch rate of 80%, pass^5 = 0.8⁵ ≈ **33%**. With 95%, pass^5 ≈ 77%. Reporting both shows how much of recall depends on luck.

**C6. Injection upper bound (rule of three).** With 0 successes in n attempts, the 95% upper bound on the true rate is about 3 ÷ n. So 15 runs give "up to 20%", and 300 runs give "up to 1%".

**C7. Clustering.** With m comments per PR and correlation ρ between comments in a PR, the effective sample is n ÷ (1 + (m−1)ρ). Phase 2's 42 units from 24 PRs (m ≈ 1.75), with ρ = 0.2, carry about as much information as 37 independent ones; with ρ = 0.4, about 32. A cluster bootstrap (resample PRs, not comments) gives the right interval directly.

**C8. Cost of a full live run.** P3-S15's mean cost per run was $0.0138 (diff) and $0.0184 (context). 400 cases × 5 runs × 2 modes = 4,000 runs ≈ **$55–75**, under per-step caps.

## Appendix D: one bug through the old and new eval

`sample`/`double-tax`: the diff makes `Invoice.total()` include tax, so any caller that already adds tax to `total()` now taxes twice. The bug is only visible with the callers in view.

**Today.**
1. The case plants it at `billing/invoice.py` lines 14 and 34, with keyword groups (`tax`…) and (`double`, `twice`, `already`, `caller`…), needing 3 of 5 runs.
2. P1-S17's live diff-mode run catches it 0/5. It becomes the documented miss `KNOWN_MISSES = {("sample", "double-tax")}`.
3. In Phase 3, context mode catches it 3/3, against 1/3 in diff mode. That is real evidence that context helps, but from one bug and 3 runs, and context mode lost two other bugs at the same time. The overall verdict was inconclusive.

**Under the plan.**
1. The bug carries `severity: high`, tags `needs-context` and `cross-file`, `origin: planted` and `split: dev`. The mined and mutation sets add dozens more `needs-context` bugs from real call chains, some in `holdout`.
2. The frozen judge decides whether each finding describes the double tax, with κ measured against the person-confirmed credits. Wordings the keyword groups miss, such as "callers expecting a net total will now apply tax on a gross amount", are no longer silent misses.
3. The report gives recall on the `needs-context` slice per mode, with a cluster-bootstrap interval, and a paired McNemar test over the same bugs. `evals power` said in advance how many bugs that needs, so the answer is "context improves recall on cross-file bugs by X ± Y" or "not enough data", decided before the run.
4. Per-bug rows like this one remain as diagnostics, so the loss on `float-money` in context mode stays visible, now beside a pooled number that can carry a decision.
