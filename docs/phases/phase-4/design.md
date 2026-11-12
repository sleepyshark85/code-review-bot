# Phase 4: Governed — design

- **Status:** Approved on 2026-11-12 by the reviewer on the user's behalf, after four review rounds (the fourth approved by the user). On 2026-11-12 the user said to continue to the next phase, and that the design doesn't need the user's approval if the designer and reviewer agree. Q2, Q10 and Q11 stay open for the user; they gate only the live steps (S14) and the close (S15, S16).

## 1. Goal and gate

The bot is safe to run at organisation scale. A versioned policy file decides what the bot may send, to which models, with which tools and at what cost, and every run records which policy it ran under. Approved finding classes can block a merge, with a documented appeal and a recorded override. Personal data is redacted before any vendor call. Prompt versions are pinned and gated by evals for every model the policy allows. A second real provider can take over when Claude is unavailable. Scope and gate: [Roadmap Phase 4](../../roadmap.md#phase-4-governed).

Read for this repository, the gate is: **a named person signs off the security review packet (S15); the review service meets its SLOs ([Targets](../../roadmap.md#targets): p95 time to review under 5 minutes, and at least 99.5% of PR events get a review or an explicit degraded result) over 13 consecutive weeks; and merge gating is on for at least one approved finding class.** [How the gate is evidenced](#how-the-gate-is-evidenced) says what closes each clause. Autonomy: gate (approved finding classes only).

**Phase 3's gate isn't met yet.** Phase 3 is fully built. Its close, P3-S15 ([PR #96](https://github.com/sleepyshark85/code-review-bot/pull/96)), waits for Phase 2's labelled set ([#67](https://github.com/sleepyshark85/code-review-bot/issues/67), human labelling) and for the shadow window, which runs to about 2026-11-29. The README starts a phase only after the previous gate is met. Phase 4 starts early as a stated exception, as Phase 3 did for Phase 2 ([Q1](#6-open-questions)). Every slice builds and tests offline with `MockProvider` and fakes. Nothing a slice ships changes which findings a Phase 2 or Phase 3 shadow or comment run reports unless a person turns it on: every new behaviour is off under the built-in policy, and CI's review runs pass no policy file until S10, whose committed policy (from S9) keeps today's behaviour. S10 merges only once P3-S15 has added the context eval suite and the policy lists `context`, so turning context mode on in CI never meets a policy that refuses it ([S9 AC9](slices/p4-s9-prompt-eval-gate.md)). S3 changes how Claude is called in context mode, so it holds its merge until P3-S15's live context steps have run ([S3](slices/p4-s3-claude-enforces-tool-policy.md)). S16, the close, waits for P3-S15.

## 2. Architecture

### What exists, and what is new

| Roadmap item | Already built (Phases 0 to 3) | New in Phase 4 |
|---|---|---|
| Policy as code, through SDK hooks and `can_use_tool` | A `PreToolUse` hook denies all but the three tools, the CLI's init tool list is checked, and repository config comes from the base commit ([ADR 0014](../../adr/0014-tools-as-a-provider-neutral-executor.md)) | A versioned policy file that is recorded in every report. It decides tools, the cost ceiling, redaction, the models allowed and their order, and the gate classes. It is enforced in the executor for every provider, and in Claude's hook and `can_use_tool` (S1, S3) |
| Merge gating, with appeal and override | None. Review jobs are never required checks | A gate decision for each approved class, reported as "would block" in shadow mode (S5); a `merge gate` check; an override by reply; a documented appeal (S10); evidence for approving a class (S11) |
| Cost caps | `max_cost_usd` per review, passed as the CLI's `max_budget_usd` ([ADR 0015](../../adr/0015-per-call-cost-cap-and-step-allowances.md)); dollar allowances for **context-mode** live commands only; the workspace limit. Diff-mode `evals live` and `pr backfill` have no dollar cap today: a diff-mode backfill of 21 PRs cost $8.43 | A policy ceiling on every model call (S1); `evals live` and `pr backfill` refuse in **every** mode without `--max-usd` and a per-call cap (S1); `[cost]` in CI's policy (S9); a cost cap for the second provider, from configured prices (S6); a separate cap on each routed attempt (S7) |
| Redaction before any vendor call | A diff holding a secret is never sent. Tools refuse secrets, and issues are secret-scanned | Personal data is replaced by typed placeholders in all text sent: the diff, tool results and issues (S4) |
| Versioned prompts with an eval gate | `PROMPT_VERSION`, `CONTEXT_PROMPT_VERSION`, freshness checks that fail stale recordings, and `--require-live` ([ADR 0007](../../adr/0007-evals-replay-and-live.md)) | Each version is pinned to its text. `evals gate` checks every model and mode the policy allows against one committed suite list, with accepted misses, and runs in CI (S9) |
| Tracing | The audit log records every stage, plus the ledger, the evidence and `run_id` ([ADR 0005](../../adr/0005-audit-log.md)) | Policy, redaction, routing and gate records; a fingerprint on every policy record; `trace` for one finding (S12) |
| A model router with a second real `LLMProvider` | The `LLMProvider` seam, and a conformance suite run over the mock and over Claude with fakes ([ADR 0002](../../adr/0002-llm-provider-interface.md)) | An OpenAI-compatible provider (S2, S6); a routing provider (S7); a rules-only degraded mode (S8) |
| SLOs | `pr metrics`: outcomes, and latency p50 and p95 | `pr slo`: runs that wrote no report count as misses, over a window of whole weeks (S13) |

### Module map

New or changed modules under `src/review_bot/`. The `governance` package, `providers.openai_wire`, `evals.gate`, `pr.gate_check`, `pr.slo` and `trace.lineage` are pure. `providers.openai_compat` (over HTTP), `providers.router` (through its providers), `pr.rest_github` and the CLIs touch the outside world.

| Module | Responsibility | Key types | From |
|---|---|---|---|
| `governance.policy` | Parses and validates `policy/v1`; holds the built-in policy; checks settings against the policy's ceilings | `Policy`, `BUILTIN_POLICY`, `parse_policy`, `PolicyError` | S1, grows in S4, S5, S7, S8, S10 |
| `governance.redaction` | Masks personal data with typed placeholders, in linear time | `RedactionKind`, `mask_personal_data`, `Masked` | S4 |
| `governance.gate` | Which findings block a merge: rule and secret classes from the whole PR diff each run, `ai:` classes from this run plus carried blockers | `GateClass`, `GateDecision`, `decide_gate` | S5 |
| `threshold` (renamed from `policy`) | The existing severity-threshold decision, renamed so that `governance.policy` doesn't share its name | `Decision`, `decide` | S1 |
| `context.tools` (changed) | Offers and runs only the tools the policy allows; any other call is refused `policy_denied` | `RepoTools(..., allowed=...)` | S1 |
| `providers.claude_tools` (changed) | Builds the hook and `can_use_tool` from the same tool decision | | S3 |
| `providers.openai_wire` | Pure: the Chat Completions request and response JSON, tool calls, and the error mapping | `ChatRequest`, `read_completion` | S2, S6 |
| `providers.openai_compat` | The second real `LLMProvider`, over an injected HTTP transport | `OpenAICompatibleProvider`, `HttpTransport`, `UrllibTransport` | S2, S6 |
| `providers.router` | An `LLMProvider` that tries the policy's routes in order on availability failures | `RoutingProvider`, `Route`, `Attempt` | S7 |
| `providers.base`, `errors` (changed) | Responses and errors carry the attempts made | `ReviewResponse.attempts`, `ProviderError.attempts` | S7 |
| `review` (changed) | Applies redaction to everything sent; degrades to the rules' findings | `PipelineResult.degraded`, `.redaction` | S4, S8 |
| `prompt` (changed) | Pins each prompt version to the fingerprint of its text | `PROMPT_FINGERPRINTS` | S9 |
| `evals.gate`, `evals.cli` (changed) | Suites per provider, model and mode; accepted misses; the verdict | `Suite`, `gate_verdict` | S9 |
| `pr.config` (changed) | `gate` in `.review-bot.toml`: the repository's opt-in, a subset of the policy's classes | `RepoConfig.gate` | S5 |
| `pr.comments`, `pr.shadow`, `pr.runner` (changed) | Gate state in the summary and its marker; the `gate` key; the outcome `degraded` | | S5, S8 |
| `pr.gate_check`, `pr.cli` (changed) | `pr gate`: the merge decision from the bot's summary and any override replies | `MergeVerdict`, `decide_merge` | S10 |
| `pr.github`, `pr.rest_github`, `pr.fake_github` (changed) | Who last edited the summary; replies to the bot's comments and PR reviews, each with its author and last editor; whether a comment-job run exists for the PR (Actions API) | `SummaryState`, `Reply` | S10 |
| `pr.metrics`, `pr.metrics_report` (changed) | Precision and overrides by class, and whether each class is eligible | `class_evidence` | S11 |
| `trace.lineage`, `trace.cli` | `python -m review_bot.trace`: one finding's lineage across audit logs | `trace_finding` | S12 |
| `pr.slo` | SLOs over whole weeks, from reports and the list of workflow runs | `summarise_slo` | S13 |
| `settings` (changed) | `policy`, `provider` `openai`, `openai_base_url`, the token prices | | S1, S2, S6 |
| `.github/review-bot-policy.toml`, `review.yml`, `live.yml`, `ci.yml` (main session) | This repository's policy, the merge gate job, the second provider's live steps, and the eval gate in CI | | S9, S10, S14 |
| `docs/security/` (main session) | The security review packet | | S15 |

### Data flow (one PR run)

event → **refuse** (as Phase 2) → **policy** (S1; from `--policy`, else the built-in policy) and the base commit's `.review-bot.toml` (now with `gate`) → **settings checked against the policy's ceilings** → diff → secret scan → parse → skip → rules → **redact** (S4) → **route** (S7): the primary model, then the fallback only on `unavailable`, `transient` or `rate_limit`, with tools allowed by the policy and redacted results → verify → threshold → **gate** (S5; rule and secret classes from the whole base...head diff) → plan, owners → `shadow/v1` and Markdown, and in comment mode the summary with its gate marker → audit log. If every route fails and the policy allows it, the run is **degraded** (S8): the rules' findings, outcome `degraded`.

**Merge gate** (S10), a separate job with no model and no key: event → the bot's summary (its identity, last editor and marker) and inline comments → override replies on blocking threads, and override PR reviews for secret blockers → `gate/v1`, with exit 0 or 1 as the check's result. In `review.yml`, the existing jobs run only for `pull_request` events, so the review-comment and review events that start the merge gate never start a review.

### Interfaces

Slice specs list the fields; this is the shape.

```python
# governance/policy.py
@dataclass(frozen=True, slots=True)
class Policy:
    version: str  # the file's own label, 1 to 64 of [A-Za-z0-9._-]
    sha256: str  # of the file's bytes; of a fixed text for the built-in policy
    source: str  # "builtin", or the path as given
    tools: frozenset[str]  # S1: a subset of read, grep, glob
    max_cost_usd: Decimal | None  # S1: ceiling on every model call
    redaction: frozenset[RedactionKind]  # S4: empty = off
    gate_classes: tuple[GateClass, ...]  # S5: classes approved for gating
    models: tuple[ModelEntry, ...]  # S7: empty = any model of the chosen provider
    fallback: ModelEntry | None  # S7
    degrade_to_rules: bool  # S8
    self_override: bool  # S10


BUILTIN_POLICY: Final[Policy]  # today's behaviour: all three tools, no ceiling, nothing else


def parse_policy(
    data: bytes, *, source: str
) -> Policy: ...  # raises PolicyError (a ConfigError, exit 2)
def check_settings(
    settings: Settings, policy: Policy
) -> Settings: ...  # S1: applies the ceiling, or raises ConfigError


# governance/gate.py (S5)
@dataclass(frozen=True, slots=True)
class GateClass:
    rule_part: str  # a RuleId, "ai:<category>", or "possible-secret"
    min_severity: Severity  # high or critical
    min_confidence: float  # 0.5 to 1.0
    approved_by: str
    approved_on: date


class GateDecision(StrEnum):
    NOT_ENABLED = "not_enabled"
    PASS = "pass"
    BLOCK = "block"
    UNJUDGED = "unjudged"  # the model's part wasn't judged (failed, degraded or skipped) and no blocker was found


def decide_gate(
    reported: Sequence[ReviewedFinding],  # ai: findings of this run
    *,
    whole_diff: WholeDiffChecks,  # rule findings and secret hits over base...head, every run
    outcome: Outcome,
    enabled: Sequence[GateClass],
    carried: frozenset[
        str
    ],  # ai: fingerprints from the previous summary, only if the bot last edited it
) -> GateResult: ...  # decision, and the blocking findings, each with its class


# providers/router.py (S7); ReviewResponse and ProviderError each add attempts: tuple[Attempt, ...]
@dataclass(frozen=True, slots=True)
class Attempt:
    route: str  # "<provider>:<model>"
    category: ErrorCategory | None  # None: this attempt served the review
    cost_usd: Decimal | None


class RoutingProvider:  # an LLMProvider
    def __init__(self, routes: Sequence[Route]) -> None: ...


# providers/openai_compat.py (S2)
class HttpTransport(Protocol):  # one real (urllib, in a thread), one fake for tests
    async def post_json(
        self,
        url: str,
        body: Mapping[str, JsonValue],
        *,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> HttpResponse: ...  # status, headers, body bytes (at most 4 MB); never follows a redirect


class OpenAICompatibleProvider:  # an LLMProvider
    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str,
        api_key: str | None,
        transport: HttpTransport | None = None,
    ) -> None: ...


# governance/redaction.py (S4)
def mask_personal_data(
    text: str, kinds: frozenset[RedactionKind]
) -> Masked: ...  # text, counts by kind
```

**`policy/v1`**, a TOML file of at most 64 KB. Unknown keys are invalid. Each slice adds the section it enforces:

```toml
version = "rightship-2026-11"            # S1
[tools]                                   # S1
allowed = ["read", "grep", "glob"]
[cost]                                    # S1
max_cost_usd = "0.60"
[redaction]                               # S4
kinds = ["email", "phone", "ipv4"]
[[models]]                                # S7: allowed, primary first
provider = "claude"
model = "claude-opus-5-5"
modes = ["diff", "context"]
[routing]                                 # S7, S8
fallback = { provider = "openai", model = "<the user's choice, Q2>", base_url = "https://api.openai.com/v1" }
degrade_to_rules = true
[gate]                                    # S10
self_override = false
[[gate.classes]]                          # S5
rule = "swallowed-exception"
min_severity = "high"
min_confidence = 0.9
approved_by = "@sleepyshark85"
approved_on = 2026-12-01
```

Without `--policy` or `REVIEW_BOT_POLICY`, the run uses `BUILTIN_POLICY`, which reproduces Phase 3 exactly. The mock provider sends nothing out, so the model allowlist doesn't apply to it.

**Report keys, appended only.** `report/v1` adds `policy` (`version`, `sha256`, `source`, S1), `redaction` (counts by kind and source, S4), `route` (each attempt, S7) and `degraded` (S8). `shadow/v1` adds `policy` (S1), `gate` (`enabled`, `decision`, `blocking`, `classes`, S5), and the outcome value `degraded` (S8). The audit log's `run`/`start` record adds `policy_version` and `policy_sha256` (S1). New records: `static`/`redaction` (S4), `policy`/`gate` (S5) and `agent`/`attempt` (S7). The `policy`/`decided` record adds `fingerprint` (S12). As before, no record holds diff lines, model text or personal data.

**Summary marker.** In comment mode with gating enabled, the summary's last line becomes `<!-- review-bot:v1 summary reviewed=<40 hex> gate=<16 hex>[,<16 hex>]*[ tampered] -->`; ` tampered` is present while a tamper's rebuild is pending ([P4-S5 AC9](slices/p4-s5-gate-decision.md)). With gating on, every comment-mode run that isn't refused writes the summary, skips included, keeping `reviewed=` ([P4-S5 AC11](slices/p4-s5-gate-decision.md)). The `gate=` part lists every current blocker's fingerprint, at most 25, and is left out when there are none, so a summary on a repository that doesn't gate is unchanged ([ADR 0009](../../adr/0009-comment-state-on-the-pr.md)). A `possible-secret` blocker's fingerprint is built as a finding's is ([ADR 0003](../../adr/0003-finding-fingerprints.md)), from its file, the rule part `possible-secret` and the hash of its line, so it never holds the value.

### Policy as code

Decision: [ADR 0018](../../adr/0018-policy-as-code-file.md). One file, chosen by whoever runs the bot (in CI, by the workflow, read from the base commit), decides what the bot may do. A repository's `.review-bot.toml` can only opt in to what the policy allows: its `gate` list must name classes the policy approves. The decisions are pure functions. They are enforced where the action happens:

- **Tools** (S1, S3). The executor offers only the allowed tools, and refuses any other call with `policy_denied`, for every provider. In Claude's context mode, the `PreToolUse` hook and `can_use_tool` both call the same decision (S3). `allowed_tools` and the init tool-list check now list the policy's tools.
- **Cost** (S1). Every model call carries a cap no higher than the policy's ceiling. With no cap set, the ceiling is used. A cap set above it refuses the run with exit 2, before any call. `evals live` and `pr backfill` refuse in every mode unless they have `--max-usd` and a per-call cap, from the flag or the ceiling. CI's policy sets `[cost] max_cost_usd` (S9).
- **Data sent** (S4). The policy's redaction kinds apply to every text in a request.
- **Models** (S7). The run's provider, model and mode must match an entry. The fallback must be an entry too, and every entry needs a passing eval suite (S9).
- **Gating** (S5, S10). The classes approved, and who may override.

### Merge gating, appeal and override

Decision: [ADR 0019](../../adr/0019-merge-gate-from-the-bot-summary.md).

- **Classes.** A class is a rule part (`swallowed-exception`, `decimal-money`, `ai:security` and so on), or `possible-secret`. It comes with a minimum severity and confidence. The policy approves classes, and a repository opts in through `gate` in `.review-bot.toml`. Only findings at or above the threshold, verified and anchored, block. A finding whose comment is withheld still blocks, and the summary lists it by file and line.
- **Observe first.** In shadow mode the decision is computed and reported as "would block", and nothing is enforced. That gives each class a track record before anyone turns gating on.
- **Deterministic classes are decided afresh every run.** Rule classes and `possible-secret` are decided from the rules and the secret scan over the whole base...head diff, on incremental runs too. A blocker fixed anywhere in the PR therefore clears at the next run, even below its anchor, and nothing about them is carried.
- **Model classes carry.** The model reviews only new changes on an incremental run, so an `ai:` blocker is carried in the summary marker. It stays until the bot itself resolves its comment because the anchored line is gone; a person resolving the thread doesn't clear it. A carried blocker is read only from a summary that the bot last edited. If anyone else edited it, the same run rebuilds the carried `ai:` blockers from the bot's own open or person-resolved inline comments of enabled classes, each last edited by the bot, using the fingerprint and rule part in their markers. Until that rebuild succeeds, the decision is `block`, reason `tampered`, and the bot keeps ` tampered` in the marker it writes, so later runs and the check stay `tampered`. Two disclosed exceptions can't be rebuilt, and the packet lists both ([P4-S5 AC9](slices/p4-s5-gate-decision.md)): a withheld `ai:` blocker, which has no inline comment, and an `ai:` blocker whose inline comment was deleted before the summary was edited. A PR stuck in `tampered` has a documented way out in the runbook ([S10](slices/p4-s10-merge-gate-check.md)): a maintainer merges with a recorded admin bypass, or opens a new PR.
- **No blocker is ever `pass` unjudged.** A run that failed, degraded or skipped keeps the previous `reviewed=` SHA. With gating on it still writes the summary, unless it was refused. Its decision is `block` if any deterministic or carried blocker is found, otherwise `unjudged`.
- **The check.** `merge gate` is its own job in `review.yml`. The existing jobs run only for `pull_request` events, so its extra review-comment triggers start no review and cancel nothing. It runs after the comment job, and on `pull_request_review_comment` and `pull_request_review` events, never `issue_comment`, whose check runs land on the default branch rather than the PR's head. It has `pull-requests: read`, `actions: read` and no model key. Its concurrency group includes the event name, so a reply never cancels a push's gate run. For comment and review events, `pr gate` checks that the PR is same-repo after reading it. It decides in this order:
  1. → `tampered` (fail): the summary was last edited by anyone but the bot, or its marker carries `tampered`; or there is no summary although a `review.yml` comment-job run for this PR wrote one, the PR has the bot's inline comments, or the comment job reports it reviewed this head.
  2. → `block` (fail): any blocker isn't overridden.
  3. → `overridden` (pass): every blocker is overridden.
  4. → `pass`: reviewed at head, with no blockers.
  5. → **`open`** (pass, reason `not_reviewed_at_head`), named in the job summary: anything else.

  Gating not enabled → `not_enabled` (pass). A GitHub API failure fails the check (exit 18), so a re-run decides it.
- **Override and appeal.** The author appeals by replying on the blocking thread, which notifies the owner through GitHub. A person whose association is `OWNER`, `MEMBER` or `COLLABORATOR` overrides by replying `/review-bot override: <reason>` on that thread. A `possible-secret` blocker has no thread, so its override is a submitted PR review whose body starts `/review-bot override <fingerprint>: <reason>`. The reason is 10 to 500 characters. The reply counts only if its last editor is its author. The PR's author may override only if the policy's `self_override` is true. An override covers that fingerprint for the rest of the PR, and is recorded in `gate/v1` and in the audit log with the comment ID and login, never the reason's text. **Agents never post override replies:** the main session posts as the owner's account, so with `self_override` true an agent could clear its own blocker. CLAUDE.md and the runbook state the rule (S10), and the security packet discloses it (S15). The runbook documents both appeal and override ([S10](slices/p4-s10-merge-gate-check.md)).
- **Turning it on** is a person's action: comment mode, `REVIEW_BOT_GATE=on`, a policy class approved on [S11's evidence](slices/p4-s11-class-approval-evidence.md), and `merge gate` made a required check.

### Second provider and routing

Decisions: [ADR 0020](../../adr/0020-openai-compatible-second-provider.md) and [ADR 0021](../../adr/0021-route-on-availability-failures-then-rules-only.md).

- **`--provider openai`** speaks the OpenAI-compatible Chat Completions API, through the standard library's HTTP client and with no vendor SDK, so the same code serves OpenAI and a self-hosted server ([Q2](#6-open-questions)). `--openai-base-url` (`REVIEW_BOT_OPENAI_BASE_URL`, default `https://api.openai.com/v1`) must be `https`, except on a loopback host. The key comes only from `OPENAI_API_KEY`, which is removed from the environment before any provider is built, so the Claude CLI's process never holds it. Redirects are never followed, and responses are capped at 4 MB. The output cap is sent as `max_completion_tokens` to `api.openai.com`, and as `max_tokens` to any other host, since OpenAI's newer models refuse `max_tokens` and many self-hosted servers know only `max_tokens`. Q2's answer confirms the field for the chosen model.
- **Same prompts, same suite.** It sends `p1-v1` and the context prompt unchanged, and passes `tests/conformance` with the mock and Claude, the tool-loop scenario included (S2, S6).
- **Cost.** It reports `cost_usd` from `--price-input-usd-per-mtok` and `--price-output-usd-per-mtok` (settings `price_*`, which are never stored in code). With a cap set and no prices, the run refuses with exit 2. Until S6 lands, any `--max-cost-usd` with `openai` refuses with exit 2, since nothing would enforce it. The cap is checked after each turn, so a review can overshoot by one turn, as with Claude.
- **Routing.** The fallback is tried once, only after `unavailable`, `transient` or `rate_limit`, and never after `auth`, `refusal`, `invalid_output`, `permanent` or a cap. Each attempt has its own cap, so a review's worst case is two caps. Tool caps, and `deadline_s`, cover the whole review across attempts: the fallback gets only the time left. In CI, a missing primary key routes straight to the fallback, and the run is `no_credential` only if no route has a key. Live commands (`evals live`, `pr backfill`) never route: they measure one model.
- **Degraded.** If every route fails with a routable category and the policy's `degrade_to_rules` is true, the report holds the rules' findings with outcome `degraded`, and the exit code is still the provider's, so the job shows red ([Phase 2 Q9](../phase-2/design.md#5-open-questions)). In comment mode the summary keeps the previous `reviewed=` SHA, so the next run reviews from there, and the gate decision follows the rule above: `block` on any blocker, else `unjudged`.

### Redaction

Decision: [ADR 0022](../../adr/0022-redact-personal-data-at-the-vendor-boundary.md). The policy picks kinds from a fixed catalogue: `email`, `phone` (E.164: `+` and 8 to 15 digits, with single spaces or hyphens between groups) and `ipv4`. Each match becomes `[REDACTED:<kind>]` on the same line, so line numbers, anchors and fingerprints don't change, because they come from the original diff. Addresses in the reserved documentation domains and ranges (`example.com`, `.org` and `.net`, `*.example`, `*.test`, `*.invalid`, 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) are left alone. Redaction applies to the reviewable diff, tool results and issue text, everything in a request except our own fixed prompt text. Secrets still stop the review ([Phase 1](../phase-1/design.md)). A suggestion holding a placeholder is dropped (`redacted`). Replay applies the case's policy too, so a case with no match keeps its recordings fresh.

### Versioned prompts and the eval gate

This extends [ADR 0007](../../adr/0007-evals-replay-and-live.md); there is no new ADR. Each prompt version is pinned to a fingerprint of its text: its system prompt and its framing of a fixed reference input. Changing the text without a new version fails `evals gate`. `tests/evals/gate.toml` lists the suites: provider, model, mode, case IDs, and accepted misses, each with its approver and date. S9 creates it with Claude's diff suite (the five Phase 1 cases, `double-tax` accepted). The nine Phase 3 cases and the context suite are added by whichever of S9 and P3-S15 merges second, and S14 adds the second provider's suite. Recordings of a provider other than Claude go in `recordings/live-<provider>/` and `recordings/live-<provider>-context/`, and Claude's paths don't change. `evals gate --cases DIR --suites FILE [--policy PATH]` passes only if every `[[models]]` entry and mode in the policy (or, with no policy, the default model in diff mode) has a suite with at least the cases of the default model's suite for that mode, each with a fresh live recording by that provider and model, and every miss is accepted. CI's `gate` job runs it with `.github/review-bot-policy.toml`, so a release can't ship a model or prompt that hasn't passed.

### Tracing and SLOs

- **Trace.** The audit log stays the trace ([Q8](#6-open-questions)). `python -m review_bot.trace --audit-log PATH ... --run-id ID --fingerprint FP [--format json]` prints one finding's lineage: bot version, policy, provider and model with each attempt, prompt version and its fingerprint, diff SHA-256, redaction counts, the tool calls behind its evidence, verify outcome, threshold decision and gate decision. With the merge gate's audit log, it adds any override (S12).
- **SLOs.** `python -m review_bot.pr slo --reports DIR --runs FILE --since --until` joins downloaded reports with `gh run list` output for `review.yml`. Its definitions: an event is **served** if its report's outcome is `reviewed`, `degraded` or `blocked_secret`, or `skipped` for an intentional reason: `too_large`, `nothing_to_review`, `empty`, `draft` or `already_reviewed`. It is **missed** if the outcome is `failed`, or `skipped` for any other reason (`no_credential` among them), or if a run that wasn't superseded wrote no report. A run is superseded when a later run on the same PR (matched by the report's PR number, or by the run's `headBranch`) wrote a report. Availability is served ÷ (served + missed). Latency is the p95 of `finished_at − event_at`. The SLOs are availability ≥ 99.5% and p95 < 300 s, over the window and per week ([Q9](#6-open-questions)). The runbook gains the SLOs, the on-call duty (the owner) and the alert route (S13).

### Spend and live steps

Live steps follow [ADR 0015](../../adr/0015-per-call-cost-cap-and-step-allowances.md): dispatched by a person from `live.yml` on `main`, with literal amounts. The table is a **proposal of $8.00, which needs the user's approval** ([Q10](#6-open-questions)). Nothing is dispatched before then. The second provider's key is the `OPENAI_API_KEY` repository secret, and it reaches only that provider's live step.

| Step | When | Calls | Per call | Cap |
|---|---|---|---|---|
| `smoke` (exists): two Claude context reviews, now with the policy's hook and `can_use_tool` | After S3 merges | 2 | $0.30 | $0.60 |
| `smoke-fallback` (S14): one diff and one context review by the second provider | After S14 merges, and the key exists | 2 | $0.30 | $0.60 |
| `evals-fallback` (S14): diff mode, the five Phase 1 cases × 5 runs and the nine Phase 3 cases × 3 runs | After the smoke, and after P3-S15's `evals-diff` | 52 | $0.10 | $5.20 |
| Reserve, approved by the user | | | | $1.60 |
| **Total** | | | | **$8.00** |

The second provider serves diff mode only in Phase 4. Its context suite (nine cases × 3 at $0.30, $8.10) needs a separate approval ([Q13](#6-open-questions)). The smoke stop rule from Phase 3 applies: if `smoke-fallback`'s cost per review exceeds its cap, `evals-fallback` waits and the table goes back to the reviewer.

### How the gate is evidenced

| Gate clause | Evidence | Closed by |
|---|---|---|
| Security sign-off | `docs/security/review-packet.md`: the threat model, data flow, each control mapped to the tests that prove it, vendor and retention terms, residual risks; signed by the person named in [Q11](#6-open-questions), with date and scope | S15 builds; a person signs; S16 records |
| SLOs met for a quarter | `pr slo` over 13 consecutive weeks from S13's merge, both SLOs over the window, with the weekly table and the sample size | S13 computes; S16 records |
| Autonomy: gate, approved classes only | At least one class approved on S11's evidence, `REVIEW_BOT_GATE=on`, `merge gate` required on `main`, and the overrides counted | S5, S10, S11; the owner; S16 records |
| Second provider passes the same suite and eval gate | Conformance over the mock, Claude and OpenAI-compatible with fakes; `evals gate` green with the fallback's suite | S2, S6, S9, S14; S16 records |

**What it can't show.** One repository, written by agents and merged by one person. A quarter of PR events on this repository is perhaps 100 to 200, so 99.5% means at most one miss, and the interval is wide. The test report discloses this.

### Phase 5 seams

- `policy/v1` and the tool decision → each catalogue agent's allowed tools and models.
- `RoutingProvider` and the eval gate per model → subagents on other models, each with its own suite.
- Gate classes and S11's class evidence → auto-fix PRs for classes with sustained acceptance.
- Overrides by class → suppression rules learned from rejections.

## 3. Slices

Order of work; real dependencies are in each spec. Slices in one wave can run in parallel, each on its own branch.

1. [P4-S1: Run under a versioned policy file that every report records](slices/p4-s1-policy-file.md)
2. [P4-S2: Review with a second, OpenAI-compatible provider that passes the conformance suite](slices/p4-s2-openai-compatible-provider.md)
3. [P4-S3: Let Claude's hook and `can_use_tool` enforce the tool policy](slices/p4-s3-claude-enforces-tool-policy.md)
4. [P4-S4: Redact personal data before any vendor call](slices/p4-s4-redaction.md)
5. [P4-S5: See which findings would block a merge, per approved class](slices/p4-s5-gate-decision.md)
6. [P4-S6: Review with repository context on the second provider, within a cost cap](slices/p4-s6-openai-tool-loop.md)
7. [P4-S7: Fall back to a second model when the first is unavailable](slices/p4-s7-fallback-routing.md)
8. [P4-S8: Report the rules' findings when every model is down](slices/p4-s8-rules-only-degraded.md)
9. [P4-S9: Gate prompt versions and models on evals](slices/p4-s9-prompt-eval-gate.md)
10. [P4-S10: Block a merge with a required check, with appeal and override](slices/p4-s10-merge-gate-check.md)
11. [P4-S11: See whether a finding class has earned gating](slices/p4-s11-class-approval-evidence.md)
12. [P4-S12: Trace one finding from its inputs to its gate decision](slices/p4-s12-trace-a-finding.md)
13. [P4-S13: Measure the SLOs over whole weeks](slices/p4-s13-slo-report.md)
14. [P4-S14: Run the second provider's live smoke and evals from the manual workflow](slices/p4-s14-fallback-live-steps.md)
15. [P4-S15: Hand over a security review packet for sign-off](slices/p4-s15-security-review-packet.md)
16. [P4-S16: Close Phase 4: record the gate, release 0.5.0](slices/p4-s16-close-phase-4.md)

**Parallel waves:** S1 ∥ S2 → S3 ∥ S4 ∥ S5 ∥ S6 ∥ S7 → S8 ∥ S9 → S10 ∥ S13 ∥ S14 → S11 ∥ S12 → S15 → S16. Held merges:
- S3 waits until P3-S15's `evals-context` and `backfill` dispatches have run.
- S10, whose `review.yml` wiring passes the policy, waits until P3-S15 has added the context suite and the policy lists `context`.
- S16 waits for P3-S15, the sign-off and the 13 weeks.

## 4. Needs a person

No agent can do these. Each is a stop point: the slices build and test offline up to it.

| Action | Blocks |
|---|---|
| Approve the Phase 4 live budget ($8.00, [Q10](#6-open-questions)), and later any use of the reserve | Every Phase 4 dispatch |
| Finish #67 and dispatch P3-S15's live steps (`evals-diff`, `evals-context`, `backfill`), under Phase 3's approval | S3's merge; the context suite, and so S10's merge; `evals-fallback` |
| Dispatch `smoke` after S3 merges (≤ $0.60), and confirm the record | Trusting S3's wiring; stop rule in S3 |
| Choose the second provider and model, and create its key or endpoint ([Q2](#6-open-questions)): the `OPENAI_API_KEY` secret, and for a self-hosted server, its URL and network access from Actions | S14's live steps; the fallback in CI |
| Set a monthly spend limit on the OpenAI project or key, as for the Anthropic workspace | The fallback's only monthly cap; S14's dispatches |
| Dispatch `smoke-fallback`, then `evals-fallback`, and commit the recordings | The fallback's eval suite (S9, S14) |
| Accept or reject each miss in the `openai` suite (S14 AC7); only the user accepts one | The fallback's suite passing `evals gate` |
| Add the fallback to `.github/review-bot-policy.toml` once its suite passes | Fallback in CI (S7) |
| Turn comment mode on (`REVIEW_BOT_MODE=comment`, after #67) | Gating, which reads the summary |
| Approve each gate class in the policy, on S11's evidence | Any class gating |
| Set `REVIEW_BOT_GATE=on`, and add `merge gate` to `main`'s required checks | Autonomy "gate"; the gate clause |
| Confirm on a test PR, once the gate is on, that an override turns the required check green (S10 AC12) | Trusting the override path |
| Optionally, add a deny rule to `.claude/settings.json` so agents' shell can't post `/review-bot override` comments. It changes Claude Code's permissions, which only the user may do; the CLAUDE.md rule (S10) applies either way | Defence in depth for "agents never override" |
| Act as on-call for the quarter: watch failed runs, keep PRs flowing | The SLO clause (S13, S16) |
| Read and sign the security review packet, or name someone to ([Q11](#6-open-questions)) | The sign-off clause (S15, S16) |
| Confirm the vendor and data-retention terms for Anthropic and the second provider | The packet's terms section (S15) |

## 5. Known risks

| Risk | Handling |
|---|---|
| `can_use_tool` needs the SDK's streaming input, and may not be consulted for tools that `allowed_tools` already approves | The hook stays the per-call decision and `can_use_tool` is the backstop. S3's fakes test our side, the `smoke` checks the CLI's. **Stop rule:** if the smoke fails, S3 is reverted before any other context run |
| S3 changes Claude's context-mode call while Phase 3's live evidence is pending | S3 merges only after P3-S15's live context steps |
| A gate that blocks wrongly costs developers' trust fast | Shadow "would block" first; approval on evidence (S11); override by reply; fail open when unreviewed |
| A collaborator edits or deletes the bot's summary, or edits someone's reply into an override, to clear the gate | The review run carries nothing from a summary the bot didn't last edit, and rebuilds `ai:` blockers from its own inline comments, staying `tampered` until it can; the check fails `tampered` on such a summary, or on a missing one where a comment-job run wrote one; an override counts only if its author last edited it. A withheld `ai:` blocker, or one whose comment was deleted first, can't be rebuilt after a tamper, which the packet discloses; the runbook gives the way out of a stuck `tampered`. Admins can still bypass branch protection, which the packet records as a residual risk |
| An agent clears its own blocker, since the main session posts as the owner | Agents never post override replies (CLAUDE.md and runbook rule, S10); an optional deny rule by the user (§4); disclosed in the packet |
| Fork PRs are never reviewed, so `merge gate` is skipped there and a skipped required check passes | Disclosed in the packet as a residual risk; forks have no key ([ADR 0008](../../adr/0008-pull-request-action-with-github-token.md)) |
| The second provider handles injection worse | It must pass the same suite, injection cases included, before the policy lists it (S9) |
| Falling back on a spend limit would bypass the organisation's cap | `permanent` (billing) never routes |
| Redaction misses personal data in other forms, or masks code | A fixed catalogue, off by default, documentation addresses left alone, counts reported; the packet states what it doesn't catch |
| Too few PR events to show 99.5% | Disclosed; the user decides at S16 whether the window is evidence enough |

## 6. Open questions

The reviewer decided Q1, Q3 to Q9, Q12 and Q13 on the user's behalf on 2026-11-12, after round-1 review, and the user may still overrule any of them. Q2, Q10 and Q11 stay open for the user; they block only live steps and the close.

**Q1. Building Phase 4 before Phase 3's gate.** **Decided by the reviewer on the user's behalf, 2026-11-12: accepted**, as a documented exception to the README's "starts only when the previous gate is met", as Phase 3 did ([Phase 3 Q11](../phase-3/design.md#6-open-questions)). Conditions: S3's merge waits for P3-S15's live context steps; S10's merge waits for the context suite; S16 waits for P3-S15. Reason: every slice is off by default and builds offline, so Phase 3's evidence isn't disturbed.

**Q2. Which second provider and model? (needs the user)** **Recommended:** the OpenAI API through the OpenAI-compatible provider. It needs no infrastructure to run, and the same code serves a self-hosted server later through `--openai-base-url`. The model is the user's choice, and the design doesn't guess it. The answer also fixes the output-token field ([routing](#second-provider-and-routing)) and the price literals in `live.yml` (S14). No build slice waits for this answer. S14's live steps do.

**Q3. Who may override?** **Decided by the reviewer on the user's behalf, 2026-11-12:** an `OWNER`, `MEMBER` or `COLLABORATOR` other than the PR's author, by a reply whose last editor is its author. The built-in `self_override` is false. This repository's policy sets it to true, because one person writes and merges every PR, and the packet discloses this. Agents never post override replies. Reason: separation of duties wherever there are two people, and an honest record where there is one.

**Q4. When is a class approved for gating?** **Decided by the reviewer on the user's behalf, 2026-11-12:** S11 marks a class eligible at ≥ 20 judged comments with a Wilson 95% lower bound on precision ≥ 0.80, and, once gating, an override rate ≤ 10%. `min_severity` is `high` or `critical`, default `critical`. `min_confidence` defaults to 0.9. The owner approves each class with a reviewed change to the policy. `possible-secret` is exempt from labels, because its blockers have fingerprints, clear on a fresh whole-diff scan and have a defined override. S16 must say if it is the only gating class. Reason: a bar the small sample can meet only for a genuinely precise class.

**Q5. Fail open or fail closed?** **Decided by the reviewer on the user's behalf, 2026-11-12:** fail open only for code the bot hasn't reviewed (`open`, named in the check's summary), as amended by round 1. Deterministic classes are decided on the whole diff every run; `ai:` blockers carry only from a summary the bot last edited, and after a tamper are rebuilt from the bot's own inline comments, staying `tampered` until that succeeds; a run that failed, degraded or skipped is never `pass`; a missing or edited summary where the bot did review (a comment-job run that wrote one, inline comments, or a reviewed head) is `tampered`; override replies and reviews count only if unedited by others; replies and reviews never cancel a push's gate run. Reason: an outage never blocks every merge, and no edit, deletion or push launders a blocker, with two stated exceptions: an `ai:` blocker whose comment was withheld, and one whose inline comment was deleted before the summary was edited, can't be rebuilt after a tamper. The tamper itself is reported in the summary, `gate.json` and the audit log, and the packet lists the gap (S15 AC6).

**Q6. When to fall back.** **Decided by the reviewer on the user's behalf, 2026-11-12:** once, after `unavailable`, `transient` or `rate_limit`; never after a cap, `permanent`, `auth`, `refusal` or `invalid_output`; never in live commands; and `deadline_s` bounds the whole routed review. Reason: routing must not bypass spend limits or refusals, or stretch the deadline.

**Q7. Redaction kinds.** **Decided by the reviewer on the user's behalf, 2026-11-12:** `email`, `phone` (E.164) and `ipv4`, off in the built-in policy, with documentation domains and ranges exempt. Reason: names, postal addresses and IPv6 have no linear-time rule that is precise enough on code.

**Q8. Tracing: the audit log, or OpenTelemetry?** **Decided by the reviewer on the user's behalf, 2026-11-12:** the audit log is the trace, with `trace` for a finding. Reason: there is no collector to send spans to, and adding one is a new dependency and data flow with no consumer. Revisit when the organisation has a collector.

**Q9. SLO definitions.** **Decided by the reviewer on the user's behalf, 2026-11-12:** as in [Tracing and SLOs](#tracing-and-slos). `skipped` is served only for the named intentional reasons, and `no_credential` and other refusals are misses. A run cancelled by a newer run on the same PR (by PR number or `headBranch`) is superseded, not missed. A quarter is 13 consecutive weeks from S13's merge, judged over the whole window, with the weekly table shown. Reason: an unconfigured or broken bot must count against the SLO.

**Q10. The Phase 4 live budget (needs the user).** **Recommended:** $8.00, as in [the table](#spend-and-live-steps). Nothing is dispatched before approval.

**Q11. Who signs the security review? (needs the user)** **Recommended:** an independent reviewer if the user can name one; otherwise the user as owner, with the packet stating that the sign-off isn't independent.

**Q12. Version.** **Decided by the reviewer on the user's behalf, 2026-11-12:** 0.5.0, after Phase 3's 0.4.0. Reason: one minor version per phase, as before.

**Q13. The second provider in context mode.** **Decided by the reviewer on the user's behalf, 2026-11-12:** diff mode only in Phase 4. S6 builds and tests the tool loop offline, because the conformance suite requires it, but the policy lists the fallback for `diff` only. Its context suite, and so context in CI on the fallback, needs a separate approval of $8.10. Reason: it keeps Phase 4's spend small, and context on the fallback is earned by evals.
