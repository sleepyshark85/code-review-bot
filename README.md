# Review Bot

An automated PR reviewer for RightShip, and the first agent on a shared agent platform.

> **Status:** Phase 0 complete · Phase 1 next · **Owner:** _TBD (see [Ownership](#ownership))_ · **Last updated:** 2026-11-05

## Usage

Install the [latest release](https://github.com/sleepyshark85/code-review-bot/releases/latest), which requires Python 3.12 or later:

```
uv tool install https://github.com/sleepyshark85/code-review-bot/releases/download/v0.1.0/review_bot-0.1.0-py3-none-any.whl
review-bot <diff>                    # a real review with Claude
review-bot <diff> --provider mock    # offline; canned output, not a real review
```

From a clone, run `uv sync`, then `uv run python -m review_bot <diff>`. The usage below describes `main` and may be ahead of the latest release.

- **Authentication:** your Claude Code login, or `ANTHROPIC_API_KEY` if set. The model runs with no tools.
- **Exit codes:** `0` no findings at or above the threshold, `1` findings, `2` to `6` usage or input errors, `7` to `13` provider failures such as `8` for authentication, `14` internal error, `15` input that isn't a diff. See the exit codes for [Phase 0](docs/phases/phase-0/design.md#exit-codes) and [Phase 1](docs/phases/phase-1/design.md#exit-codes) and [settings](docs/phases/phase-0/design.md#settings) tables.
- **Only real diffs are reviewed.** Text that isn't a unified diff exits `15`. In a git diff, docs, lock, generated and binary files are skipped and not sent to the model; a plain unified diff is reviewed whole.
- **The diff must be UTF-8.** Windows PowerShell 5 `>` writes UTF-16, which exits `4`, so use `git diff --output=change.diff`.

## 1. Current state

`Assignment/review_bot.py` is a stopgap that the team quietly relies on. It:

- hardcodes an API key in source;
- calls `openai.ChatCompletion`, which was removed in `openai>=1.0`, so on a current SDK it **does not run**;
- has no input validation, timeouts, retries, or error handling;
- pastes an untrusted diff straight into the prompt (prompt-injection surface);
- returns free text that cannot be validated, anchored to lines, deduplicated, or measured;
- sees only the diff: no repository, ticket, or domain context;
- prints to stdout with no human accept/reject step, audit trail, or feedback loop;
- has no tests, no evals, and no owner, so nobody knows whether it is any good.

`Assignment/sample_diff.txt` shows why this matters. It contains a swallowed exception, a discount that is silently dropped on CSV export, a changed tax/discount ordering, a possible double-tax regression for existing callers, float money, a hand-rolled CSV writer with an unvalidated path, a billing behaviour change with no tests, and hunk headers whose line counts don't match their bodies, which a strict diff parser would reject.

## 2. North star

**A review bot that developers trust, auditors can trace, and the team can operate.** Being enterprise-grade is less about finding the most bugs and more about this:

| Dimension | What to expect |
|---|---|
| **Signal over volume** | Few, high-precision findings tied to real lines, each with severity and rationale · no repeated comments; prior comments are updated or resolved · clean PRs get a short "no issues" check · suggested fixes where possible |
| **Rules first, AI where it reasons** | Deterministic checks catch what rules can; the model handles business logic, intent, and cross-file effects; code validates every model output |
| **Context beyond the diff** | Callers, definitions, CODEOWNERS, linked tickets, RightShip domain rules · a record of what the model saw for each finding |
| **Security and data handling** | PR content is treated as hostile input · least-privilege, read-only access · secrets and PII redacted before any vendor call · the model never triggers an action directly |
| **Governance** | Every finding traceable to prompt, model, inputs, and policy decision · versioned prompts gated by evals · per-repo opt-in and thresholds · humans make the final call, and a blocked merge can be appealed |
| **Measured quality** | Evals in CI · developer feedback becomes labelled data · value measured against a pre-launch baseline |
| **Reliability and operations** | SLOs, cost caps, tracing · falls back to deterministic-only checks or a second provider when the model is down · a named owner with on-call and runbooks |
| **Adoption** | Shadow mode first · autonomy earned one finding class at a time: `observe → suggest → comment → gate → act` |

### Targets

Proposed targets, to be calibrated against the Phase 2 baseline:

| Dimension | Target |
|---|---|
| Quality | Precision (accepted ÷ posted findings) ≥ 70%; zero hallucinated line references reach a PR |
| Value | Fewer escaped defects and shorter review cycle time vs. baseline; positive cost per accepted finding |
| Reliability | p95 time to review < 5 min; ≥ 99.5% of PR events get a review or an explicit degraded-mode result |
| Safety | The prompt-injection eval passes on every release; no action without policy approval |
| Adoption | Developer satisfaction tracked and positive; opt-in → default-on across target repos |

### Who owns what

| Responsibility | Owner | Why |
|---|---|---|
| Parsing diffs, mapping files/hunks/lines, size gating, skipping generated files | Deterministic | Must be exact; cost control is policy |
| Secret scanning, lint-style checks, domain rules | Deterministic | Cheap, reproducible, explainable |
| Business-logic issues, change intent, missing-test suggestions | AI | Requires reasoning across code and intent |
| Schema validation, line-anchor checks, dedup against rule findings | Deterministic | The model proposes, code disposes |
| Severity thresholds, posting, gating | Deterministic (policy) | Actions must be governed |
| Accept / reject / edit | Human | Accountability, and the source of labelled data |

## 3. Phases

Each phase ships on its own and starts only when the previous gate is met: gates are evidence, not calendar. Each one ships *features*, the *architecture* that makes them solid, and the *seams* the next phase uses. A seam nobody uses next is cut, and each interface has one real implementation until a second one is actually needed.

### Phase 0: Stabilise

**Goal:** the tool people rely on actually works and fails safely.

- **Claude Agent SDK** (`claude-agent-sdk`), default model `claude-opus-5-5`. It works with the team's existing Claude account, and it already has the tools, hooks, and subagents later phases need.
- **Authentication:** locally, the developer's own Claude Code login; anything shared (CI, a GitHub App) uses `ANTHROPIC_API_KEY`. Anthropic doesn't allow products built on the SDK to offer claude.ai login to other users without approval. No key in source.
- **Structure:** split into load → build prompt → call model → render · settings from environment, overridable by CLI flags · run with `python -m review_bot <diff>` (`Assignment/` stays untouched as the inherited reference) · offline mode via the mock provider.
- **Locked down for single-shot review, asserted by tests:** `tools=[]` (`allowed_tools` alone only auto-approves; it doesn't restrict), `setting_sources=[]` (the default would load local `CLAUDE.md` and user settings), `max_turns=1`, our own system prompt (not the Claude Code preset), no MCP servers, hooks, or subagents.
- **Clear errors** for a missing file, an empty or oversized diff (rejected, never truncated), a missing CLI, failed authentication, an API failure, or a refusal. The SDK reports these in two forms, and both are mapped in one place:
  - exceptions: `CLINotFoundError`, `CLIConnectionError`, `ProcessError`, `ResultError`;
  - error results: `ResultMessage.is_error` with `stop_reason="refusal"` or `terminal_reason="api_error"` (plus `api_error_status`), and `AssistantMessage.error` values such as `authentication_failed`, `billing_error`, `rate_limit`.
- **One retry layer**, the CLI's own (`CLAUDE_CODE_MAX_RETRIES`, `API_TIMEOUT_MS` via `options.env`), so retries don't stack, plus our own overall deadline, the only bound on wall-clock time (`API_TIMEOUT_MS` didn't cap it in testing).
- **Trade-off:** the SDK drives the Claude Code CLI as a subprocess, so runs start more slowly, and its wheels are about 150 MB because they appear to bundle the CLI. The mock provider keeps tests and evals from ever launching it.
- **Exit codes:** `0` no findings at or above threshold, `1` findings found, `≥ 2` a distinct code per tool error.
- **Diff as data:** wrapped in delimiters and declared untrusted in the system prompt.
- **Seam:** an `LLMProvider` interface that isn't tied to Claude. A review request in → validated `Finding`s and token usage out, or an error with a category (`auth`, `rate_limit`, `refusal`, `transient`, `permanent`, `unavailable`, `invalid_output`); no SDK types leave the provider. `ClaudeAgentProvider` and `MockProvider` both implement it, and a shared conformance suite every provider must pass runs offline against the mock and against Claude over a fake.

**Gate:** no OpenAI dependency; no secrets in source; every failure has a clear message and its own exit code; tests pass offline. **Autonomy:** suggest.

### Phase 1: Trustworthy core

**Goal:** findings that can be trusted, triaged, and measured.

- **Pipeline:** `parse → static → agent → verify → policy → output`, with a JSONL audit log of every stage decision.
- **Parsing:** a tolerant diff parser that survives malformed hunk headers, with docs-only and generated/lock files skipped.
- **Rule checks** that need no LLM: swallowed exceptions, `open()` without encoding, secrets. A diff containing a detected secret is **not sent** to the model.
- **Structured findings** (pydantic `Finding`: file, line, severity, category, rationale, confidence). Invalid model output is rejected, and hallucinated line references are dropped.
- **Interactive triage** (accept / reject / edit). Decisions are logged as labels.
- **Output:** JSON and Markdown.
- **Evals:**
  - `sample_diff.txt`: recall per planted bug and per source (rules vs. AI).
  - A clean diff: false positives.
  - A prompt-injection diff: the model must not follow instructions from the diff.
- **Seams:**
  - stable finding fingerprints (file + rule + anchor hash), used now to dedupe rule vs. AI findings → Phase 2 self-updating PR comments;
  - the eval format → Phase 2 labelled real PRs, Phase 4 release gate;
  - triage labels → Phase 2 feedback dataset.

**Gate:** planted bugs caught; clean diff within the noise budget; injection case passes; no invalid line anchors survive; everything runs offline in CI. **Autonomy:** suggest (human triage).

### Phase 2: Integrated

**Goal:** runs on real PRs without creating developer noise.

- GitHub Action, then a GitHub App: inline comments with suggested fixes, plus a summary check.
- The bot updates or resolves its own comments (keyed by fingerprint) and re-reviews only new commits.
- Shadow mode first: a report of what it *would* have posted.
- Developer reactions and resolutions become accept/reject labels; per-repo config; metrics for latency, tokens, cost, and acceptance.

**Gate:** weeks of shadow mode with precision measured; value baseline captured; named owner in place. **Autonomy:** observe → comment.

### Phase 3: Context-aware

**Goal:** fewer guesses, deeper findings.

- Agent SDK read-only tools (`Read`, `Grep`, `Glob`) scoped to the repo checkout, so findings can span files ("callers of `total()` already add tax").
- CODEOWNERS routing, ticket acceptance criteria, and RightShip domain rules (e.g. `Decimal` money in `billing/`).
- Hooks record the context behind every finding.

**Gate:** precision and recall improve over Phase 2 on the labelled set; cost within budget. **Autonomy:** comment.

### Phase 4: Governed

**Goal:** safe to run at organisation scale.

- Policy-as-code enforced through SDK hooks and `can_use_tool`.
- Merge gating for critical, high-confidence finding classes, with a documented appeal and override.
- Cost caps (`max_budget_usd`), redaction before any vendor call, versioned prompts with an eval gate, and tracing.
- A model router with a second real `LLMProvider` (e.g. OpenAI or self-hosted) as fallback. It must pass the same conformance suite and eval gate.

**Gate:** security sign-off; SLOs met for a quarter. **Autonomy:** gate (approved finding classes only).

### Phase 5: Catalogue

**Goal:** more agents on one platform, improving continuously.

- Security-review and test-planning agents as Agent SDK subagents, with dedup across agents.
- Auto-fix PRs for finding classes with sustained high acceptance; suppression rules learned from rejections.
- A template for other teams to contribute agents.

**Gate:** each agent has its own eval set and acceptance target. **Autonomy:** act (auto-fix PRs, still merged by humans).

### Alongside the build (from Phase 2)

- **Operate:** SLOs, on-call, runbooks.
- **Assure:** threat model, injection red-teaming, vendor and data-retention terms.
- **Prove:** measured impact against the baseline, plus developer satisfaction.

Capability will arrive well before operational maturity. A track record, re-evaluation as models change, and proven value can only be earned over time.

### Ownership

`review_bot.py` became a silent liability because nobody owned it. A named owning team, a support channel, and funding are prerequisites for going live in Phase 2.

## 4. Risks and non-goals

| Risk | Mitigation |
|---|---|
| Planning instead of shipping | Phases 0–1 are built in full; later phases stay as this plan until gated in |
| Developer noise → bot gets muted | Shadow mode, noise budget, fingerprint dedup, suppression learning |
| Prompt injection via PR content | Diff isolated as data; no tools before P3; injection eval from P1; no model-initiated actions |
| Code or secrets leaving the organisation | Secret-bearing diffs not sent from P1; redaction and vendor terms in P4 |
| Agent tools enabled by accident | Lockdown asserted in tests; tools only via an explicit list from P3 |
| Agent SDK runtime weight (CLI subprocess) | Measure start-up cost in P2; `LLMProvider` leaves room for another provider |
| Vendor lock-in from Agent SDK features (P3+) | The review call stays portable behind `LLMProvider`; the tool loop, hooks, and subagents are accepted as Claude-specific; revisit in P4 |

**Non-goals (for now):** replacing human reviewers; autonomous merging; non-GitHub SCMs before Phase 4.
