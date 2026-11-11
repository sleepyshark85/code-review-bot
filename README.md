# Review Bot

An automated PR reviewer for RightShip, and the first agent on a shared agent platform.

> **Status:** Phases 0 to 2 released (0.3.0). Phase 2's gate evidence is still being collected in shadow mode ([#67](https://github.com/sleepyshark85/code-review-bot/issues/67)). Phase 3 is built and waits on that evidence to close. **Owner:** [@sleepyshark85](https://github.com/sleepyshark85) ([Ownership](#ownership)) · **Last updated:** 2026-11-11

## Usage

Install the [latest release](https://github.com/sleepyshark85/code-review-bot/releases/latest) (Python 3.12+):

```
uv tool install https://github.com/sleepyshark85/code-review-bot/releases/download/v0.3.0/review_bot-0.3.0-py3-none-any.whl
review-bot <diff>                    # review with Claude
review-bot <diff> --provider mock    # offline smoke test, no model call
```

From a clone: `uv sync`, then `uv run python -m review_bot <diff>`. Authentication is your Claude Code login, or `ANTHROPIC_API_KEY` if set. The diff must be UTF-8 (`git diff --output=change.diff`).

| Command | Does |
|---|---|
| `review-bot DIFF [--format text\|json\|markdown] [--triage]` | Reviews a diff: built-in rules, then the model. Exits `0` clean, `1` findings, `2`+ errors |
| `review-bot DIFF --context-repo DIR` | Lets the model read the repository through three read-only tools |
| `python -m review_bot.pr shadow\|run` | Writes the review a pull request would get, or posts it (`--mode comment`) |
| `python -m review_bot.pr label\|metrics\|compare\|baseline` | Judges shadow findings and measures precision, cost and value |
| `python -m review_bot.evals replay\|compare` | Scores recorded model runs offline |
| `python -m review_bot.evals live`, `pr backfill`, `live.yml` | Live runs that spend real tokens, each capped |

Safety by default: a diff holding a possible secret is never sent (exit `16`), the diff and tool results are treated as untrusted data, and every run is traced in `.review-bot/audit.jsonl`, which never holds diff lines or secret values. On this repository, every same-repo PR gets a shadow review that posts nothing, unless the `REVIEW_BOT_MODE` variable says otherwise.

**Full reference:** [docs/usage.md](docs/usage.md), covering every option, exit code, setting and workflow. It describes `main`, which may be ahead of the latest release.

## 1. Current state

`Assignment/review_bot.py` is a stopgap the team quietly relies on. It hardcodes an API key, calls an OpenAI API removed in `openai>=1.0` (so it **does not run**), has no validation, timeouts or error handling, pastes an untrusted diff into the prompt, returns free text that can't be checked or measured, sees only the diff, has no human step or audit trail, and has no tests, evals or owner.

`Assignment/sample_diff.txt` shows why this matters: a swallowed exception, a discount dropped on CSV export, changed tax/discount ordering, a double-tax regression, float money, an unvalidated CSV path, no tests, and malformed hunk headers.

## 2. North star

**A review bot that developers trust, auditors can trace, and the team can operate.** Enterprise-grade means this more than finding the most bugs:

| Dimension | What to expect |
|---|---|
| **Signal over volume** | Few, precise findings on real lines · no repeated comments · suggested fixes where possible |
| **Rules first, AI where it reasons** | Rules catch what they can; the model handles logic and intent; code validates every model output |
| **Context beyond the diff** | Callers, CODEOWNERS, linked tickets, domain rules · a record of what the model saw |
| **Security** | PR content is hostile input · read-only, least privilege · secrets kept from the vendor · the model never acts directly |
| **Governance** | Every finding traceable to prompt, model and inputs · prompts gated by evals · humans decide |
| **Measured quality** | Evals in CI · feedback becomes labelled data · value measured against a baseline |
| **Operations** | SLOs, cost caps, tracing · a fallback when the model is down · a named owner and runbook |
| **Adoption** | Shadow mode first · autonomy earned per finding class: `observe → suggest → comment → gate → act` |

### Targets

Proposed, to be calibrated against the Phase 2 baseline:

| Dimension | Target |
|---|---|
| Quality | Precision (accepted ÷ posted) ≥ 70%; no hallucinated line reaches a PR |
| Value | Fewer escaped defects and shorter review cycles than the baseline |
| Reliability | p95 review < 5 min; ≥ 99.5% of PR events get a review or an explicit degraded result |
| Safety | The prompt-injection eval passes on every release |
| Adoption | Positive developer satisfaction; opt-in grows to default-on |

### Who owns what

| Responsibility | Owner |
|---|---|
| Diff parsing, size limits, secret scanning, rule checks | Code |
| Business logic, intent, missing tests | AI |
| Validating output, anchoring lines, dedup, thresholds, posting | Code: the model proposes, code disposes |
| Accept, reject, edit | Human |

## 3. Phases

Each phase ships on its own and starts only once the previous gate is met by evidence, not by date. Each adds only the seams the next phase uses.

### Phase 0: Stabilise

**Goal:** the tool works and fails safely. Claude Agent SDK, locked down to one turn with no tools, clear errors with one exit code each, no key in source, and an `LLMProvider` interface with a mock for offline tests ([design](docs/phases/phase-0/design.md)).

**Gate:** no OpenAI dependency; no secrets in source; clear failures; tests pass offline. **Autonomy:** suggest.

### Phase 1: Trustworthy core

**Goal:** findings that can be trusted, triaged and measured. Secret scan before anything is sent, a tolerant diff parser, rule checks, validated findings with stable fingerprints, triage, JSON and Markdown output, an audit log, and evals for planted bugs, noise and prompt injection ([design](docs/phases/phase-1/design.md)).

**Gate:** planted bugs caught; clean diff within the noise budget; injection case passes; no invalid line anchors; runs offline in CI. **Autonomy:** suggest (human triage).

### Phase 2: Integrated

**Goal:** runs on real PRs without noise. A GitHub Action with shadow mode, then comments it keeps up to date, per-repo config, reactions as labels, and metrics ([design](docs/phases/phase-2/design.md)).

**Gate:** weeks of shadow mode with precision measured; value baseline captured; named owner. **Autonomy:** observe → comment.

### Phase 3: Context-aware

**Goal:** fewer guesses, deeper findings. Read-only repository tools so findings can span files, evidence for every finding, CODEOWNERS, linked-issue criteria, domain rules and suggested fixes ([design](docs/phases/phase-3/design.md), [ADR 0013](docs/adr/0013-read-only-repo-tools-over-a-commit.md)).

**Gate:** precision and recall improve over Phase 2 on the labelled set; cost within budget. **Autonomy:** comment.

### Phase 4: Governed

**Goal:** safe at organisation scale. Policy as code, merge gating for approved finding classes with an appeal, redaction, versioned prompts behind an eval gate, and a second model provider as fallback.

**Gate:** security sign-off; SLOs met for a quarter. **Autonomy:** gate.

### Phase 5: Catalogue

**Goal:** more agents on one platform. Security and test-planning agents, auto-fix PRs for well-accepted finding classes, and a template for other teams.

**Gate:** each agent has its own evals and acceptance target. **Autonomy:** act (humans still merge).

### Alongside the build (from Phase 2)

Operate (SLOs, on-call, runbooks), assure (threat model, red-teaming, vendor terms) and prove (impact against the baseline). Capability arrives before operational maturity; trust is earned over time.

### Ownership

`review_bot.py` became a liability because nobody owned it.

- **Owner:** [@sleepyshark85](https://github.com/sleepyshark85), who funds the model key.
- **Spend:** capped by a dedicated Anthropic workspace limit; the key is the `ANTHROPIC_API_KEY` repository secret.
- **Support:** open an issue labelled `review-bot`.
- **Runbook:** [docs/runbook.md](docs/runbook.md): turning the bot off, rotating the key, checking spend, reading a failed run.

## 4. Risks and non-goals

| Risk | Mitigation |
|---|---|
| Planning instead of shipping | Later phases stay a plan until gated in |
| Noise gets the bot muted | Shadow mode, noise budget, fingerprint dedup |
| Prompt injection via PR content | Content isolated as data; read-only tools; injection evals; no model-initiated actions |
| Code or secrets leaving the organisation | Secret-bearing diffs never sent; redaction in Phase 4 |
| Tools enabled by accident | Lockdown asserted in tests; an explicit tool allowlist |
| Agent SDK weight and lock-in | The review call stays behind `LLMProvider`; revisit in Phase 4 |

**Non-goals (for now):** replacing human reviewers; autonomous merging; non-GitHub hosting before Phase 4.
