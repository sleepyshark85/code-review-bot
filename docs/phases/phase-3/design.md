# Phase 3: Context-aware — design

- **Status:** Approved on 2026-11-09 by the reviewer after three rounds, on the user's behalf: the user delegated design approval to the reviewer that day. Specs are `Approved` and ADRs 0013 to 0017 `Accepted`. Criterion IDs are now stable.

## 1. Goal and gate

Fewer guesses, deeper findings: the model can look up callers and definitions in the repository before it reports, reads the acceptance criteria of the issues a PR closes, and works under the repository's own domain rules. Each finding records what the model read, findings are routed to code owners, and a finding can carry a suggested fix. Scope and gate: [README Phase 3](../../../README.md#phase-3-context-aware). Suggested fixes move here from Phase 2 (Phase 2 Q7, Option A). The GitHub App stays out.

Read for this repository, the gate is: **on the same pull requests and eval cases, a context review's precision and recall both improve on the Phase 2 review's, and every live step stays within its share of the $20 the user approved.** [How the gate is evidenced](#how-the-gate-is-evidenced) says what closes each clause, and [Q3](#6-open-questions) and [Q4](#6-open-questions) define "improve" and "within budget". Autonomy: comment.

**Phase 2's gate isn't met yet.** P2-S12 is Done, released early on 2026-11-09 as v0.3.0, and its evidence criteria (the shadow weeks, precision, the labelled set) are open in issue [#67](https://github.com/sleepyshark85/code-review-bot/issues/67). The README starts a phase only after the previous gate. Slices S1 to S14 and S16 build and test entirely offline while Phase 2's shadow weeks run. Nothing they ship changes which findings a Phase 2 shadow run reports unless a person turns it on: S11 adds owners to the report and summary, but no finding. Only S15, the close, waits for P2-S12 AC1 to AC6 (#67): the committed labelled set ([Q11](#6-open-questions)).

## 2. Architecture

### Two review modes

| | `diff` mode (Phase 2, the default) | `context` mode (Phase 3, opt-in) |
|---|---|---|
| Turned on by | default; `--context off` | `--context repo` / `REVIEW_BOT_CONTEXT=repo`, or `review-bot --context-repo DIR` |
| Prompt | `p1-v1`, byte-for-byte unchanged | `p3-vN`, its own versioned prompt |
| Model call | one turn, no tools ([Phase 0 lockdown](../../../README.md#phase-0-stabilise)) | one query of several turns, with three read-only tools |
| Linked issues, domain-rule guidance, suggested fixes, evidence | none | in the prompt and the output |

`diff` mode stays exactly what Phase 2 measures, so its recordings stay fresh and the shadow window isn't disturbed ([ADR 0016](../../adr/0016-measure-phase-3-against-phase-2.md)). Deterministic additions, CODEOWNERS routing (S11) and domain-rule checks (S10), apply in both modes, because they don't touch the model; domain checks run only where a repository's config switches them on ([ADR 0017](../../adr/0017-domain-rules-from-a-catalogue.md)).

### Module map

New or changed modules under `src/review_bot/`. `context.paths`, `context.ledger`, `context.evidence`, `context.issues`, `context.codeowners`, `evals.compare` and `pr.compare` are pure. `git`, `context.tools` (through `git`), `providers`, `pr.rest_github` and the CLIs touch the outside world. `review` and `pr.runner` orchestrate through injected collaborators.

| Module | Responsibility | Key types | From |
|---|---|---|---|
| `git` (moved from `pr.git`) | A clone's diffs, files, listings and literal searches at a commit, by subprocess | `GitRepo`, `TreeEntry` | S1 |
| `context.paths` | Validate a path or glob from the model; the deny-list | `is_valid_path`, `is_valid_glob`, `is_denied` (named in build, S2; the secret-name check is `secret_scan.path_holds_secret`) | S1, S2 |
| `context.tools` | The `read`, `grep` and `glob` tools over one commit, with caps, delimiters and the ledger | `RepoTools`, `RepoRef`, `ToolCaps` | S1, S2 |
| `context.tool_args` | Pure: the tools' strict argument models and definitions (split from `context.tools` in build, S2) | `TOOL_DEFINITIONS`, `parse_arguments` | S1, S2 |
| `context.tool_text` | Pure: shaping result text under the caps (split in build, S2) | `fit_lines`, `cut_line` | S1, S2 |
| `context.commit_files` | The files a commit may show: path rules, deny-list, exact tree entry, binary, size, and one secret scan per file (split in build, S2) | `CommitFiles`, `FileText`, `Refusal` | S2 |
| `git_output` | Pure: git's output formats: tree entries, the exact-path match, and a streaming `git grep -z` reader that keeps only accepted paths (split from `git` in build, S2) | `TreeEntry`, `GrepReader`, `find_tree_entry` | S2 |
| `path_quoting` | Pure: C-quoting of odd paths, shared by `report` and `context.paths` (moved from `report` in build, S2) | `c_quoted` | S2 |
| `context.ledger` | What each tool call returned: files and line spans, bytes, outcome | `ToolCall`, `LineSpan`, `ToolOutcome` | S1 |
| `context.evidence` | Keep a finding's cited spans only if a tool returned them or the diff shows them | `verify_evidence` | S4 |
| `context.issues` | Closing keywords in a PR body; issue text capped and scanned | `linked_issue_numbers`, `LinkedIssue` | S9 |
| `context.codeowners` | Parse CODEOWNERS; owners of a path | `parse_codeowners`, `owners_for` | S11 |
| `providers.base` (changed) | The request may carry a tool executor; the response may carry a cost | `ToolExecutor`, `ToolDefinition` | S1, S3 |
| `providers.mock` (changed) | Scripted tool calls before its answer | `MockProvider(tool_calls=...)` | S1 |
| `providers.claude_agent` (changed), `providers.claude_tools` (split in build, S3) | The Claude provider ([ADR 0001](../../adr/0001-claude-agent-sdk-runtime.md)), the only modules importing the SDK or `mcp`: drives the tools through an in-process MCP server, locked down; `claude_tools` holds the server, the hook and the init check | `ClaudeAgentProvider` | S3 |
| `prompt` (changed) | The context prompt, its version, and the untrusted framing of tool results and issues | `build_context_request`, `CONTEXT_PROMPT_VERSION` | S1, grows in S4, S9, S10, S12 |
| `findings` (changed) | `evidence` and `suggestion` on `Finding`; `decimal-money` in `RuleId` | | S4, S10, S12 |
| `rules` (changed) | Domain checks switched on per path | `DomainRule` | S10 |
| `python_tokens` | Pure: the code tokens of diff lines for `rules`' checks, in time and memory linear in a line's length; a line read alone that ends inside a string has none (split from `rules` in build, S10) | `TokenKind`, `tokens_of`, `line_tokens` | S10 |
| `review` (changed) | Context mode: build the tools, record the ledger, verify evidence and suggestions | `review_diff(..., context=...)`, `ReviewContext` | S1 |
| `pr.config` (changed) | `[[domain_rules]]` in `.review-bot.toml` | `RepoConfig.domain_rules` | S10 |
| `pr.event` (changed) | Linked issue numbers from the PR body, never the body | `PullRequestRef.linked_issues` | S9 |
| `pr.github`, `pr.rest_github`, `pr.fake_github` (changed) | Read one issue, read-only | `IssueReader` | S9 |
| `pr.runner`, `pr.cli`, `pr.shadow`, `pr.comments` (changed) | Context mode on PRs; owners; evidence and suggestions in bodies | | S8, S11, S12 |
| `pr.compare` | Phase 3 against Phase 2 on the labelled PRs | `compare_runs` | S13 |
| `evals.formats`, `evals.replay`, `evals.live` (changed) | Repo and issue fixtures; context recordings; spend caps | | S5, S6, S9 |
| `evals.compare` | Diff mode against context mode on the same cases | `compare_cases` | S7 |
| `evals.cli`, `pr.cli`, `settings` (changed) | Accept CI only for a `workflow_dispatch` run with an explicit flag | `in_ci`, `--allow-dispatch` | S16 |
| `.github/workflows/live.yml` (new, main session) | The approved live steps, by manual dispatch only | | S16 (`smoke`), S6 (evals), S8 (`backfill`) |

### Data flow (one context review of a PR)

event → **refuse** (as Phase 2) → base commit: `.review-bot.toml` (now with domain rules) and CODEOWNERS → diff → **secret scan → parse → skip → rules, with domain rules** → linked issues fetched read-only, capped and secret-scanned (S9) → **agent**: the context prompt plus the tools over the **head** commit, every call recorded in the ledger → **verify**: anchors (unchanged: a finding anchors on a diff line, [ADR 0006](../../adr/0006-line-anchors-drop-never-snap.md)), then evidence against the ledger (S4), then suggestions (S12) → policy → plan, with owners (S11) → `shadow/v1`, Markdown and audit log.

Policy (config, CODEOWNERS) comes from the base commit, so a PR can't change it ([ADR 0008](../../adr/0008-pull-request-action-with-github-token.md)). The code the model reads comes from the head commit, which CI already fetches without checking it out, and is read only through git objects ([ADR 0013](../../adr/0013-read-only-repo-tools-over-a-commit.md)). A cross-file finding still anchors on a diff line; the other files it relies on are its evidence.

### Interfaces

Slice specs list fields; this is the shape.

```python
# git.py (moved from pr/git.py, S1). Existing methods unchanged; added:
@dataclass(frozen=True, slots=True)
class TreeEntry:
    path: str  # repository path, "/" separated, as git stores it
    mode: str  # "100644", "100755", "120000" (symlink), "160000" (submodule)
    size: int | None  # blob size; None for a submodule


class GitRepo:
    def list_files(
        self, commit: str, *, max_entries: int
    ) -> tuple[TreeEntry, ...]: ...  # ls-tree -r -z -l
    def grep(  # git grep -F -n -z -I --no-textconv -e <literal> <commit> --; read up to max_bytes + 1
        self, commit: str, literal: str, *, ignore_case: bool, max_bytes: int
    ) -> bytes: ...  # no pathspec: grep's path_glob is applied in our code (S2)


# providers/base.py
@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str  # "read", "grep" or "glob"
    description: str
    input_schema: Mapping[str, JsonValue]  # JSON Schema of the arguments


class ToolExecutor(Protocol):
    @property
    def definitions(self) -> tuple[ToolDefinition, ...]: ...
    async def call(self, name: str, arguments: Mapping[str, object]) -> str: ...

    # Never raises on model input: a bad call returns a refusal text, already delimited.
    # git runs through asyncio.to_thread here and in the MCP handler (CONVENTIONS §10).


@dataclass(frozen=True, slots=True)
class ReviewRequest:  # adds:
    tools: ToolExecutor | None = None  # None: diff mode, single turn, no tools


@dataclass(frozen=True, slots=True)
class ReviewResponse:  # adds:
    cost_usd: Decimal | None = None  # as the provider reports it (S3); None if unknown
    offered_tools: tuple[str, ...] | None = None  # the runtime's tool list (S3); None if unstated


class ProviderError(Exception):  # adds:
    cap: Literal["cost", "turns"] | None  # which cap ended the call (S3); None otherwise


# context/
@dataclass(frozen=True, slots=True)
class RepoRef:
    git: GitRepo
    commit: str  # 40 hex digits, resolved before the review starts


@dataclass(frozen=True, slots=True)
class ToolCaps:
    max_calls: int
    max_result_bytes: int
    max_total_bytes: int


class ToolOutcome(StrEnum):
    OK = "ok"
    TRUNCATED = "truncated"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class LineSpan:
    path: str
    first_line: int
    last_line: int


@dataclass(frozen=True, slots=True)
class ToolCall:  # one ledger entry; holds no file content and no model text
    tool: str
    outcome: ToolOutcome
    reason: RefusalReason | None  # S1 Interfaces lists the values
    spans: tuple[LineSpan, ...]  # lines returned by read and grep
    listed: tuple[str, ...]  # paths returned by glob
    result_bytes: int
    input_sha256: str


class RepoTools:  # the one real ToolExecutor
    def __init__(self, repo: RepoRef, *, caps: ToolCaps) -> None: ...
    @property
    def ledger(self) -> tuple[ToolCall, ...]: ...


# review.py
@dataclass(frozen=True, slots=True)
class ReviewContext:
    repo: RepoRef
    is_diff_head: bool = False  # S4: the tools' commit is the diff's head (PR mode)
    issues: tuple[LinkedIssue, ...] = ()  # S9


async def review_diff(  # adds the keyword-only context and domain_rules
    diff: str,
    settings: Settings,
    provider: LLMProvider,
    audit: AuditLog,
    *,
    name: str,
    exclude: Sequence[str] = (),
    context: ReviewContext | None = None,
    domain_rules: Sequence[DomainRule] = (),  # S10
) -> PipelineResult: ...  # PipelineResult adds ledger (S1) and cost_usd (S3)


# pr/github.py (S9): separate from GitHubClient, because shadow mode has no GitHubClient
class IssueReader(Protocol):
    def read_issue(self, repo: str, number: int) -> IssueText | None: ...  # None on 404
```

**Prompt versions.** `PROMPT_VERSION` stays `p1-v1` for diff mode, and its text and framing don't change, so Phase 1's recordings stay fresh. Context mode records `CONTEXT_PROMPT_VERSION`, starting at `p3-v1` (S1). Every slice that changes the context prompt or its framing bumps it to the next free number, whatever order slices merge in, and refreshes the freshness fields of synthetic context recordings. Live context recordings are made only in S15, once the prompt has settled.

**Report keys, appended only.** `report/v1` adds `mode`, `context` (`commit`, `tool_calls`, `refused`, `result_bytes`, `files_read`: each path with its line spans, at most 200), `usage.cost_usd`, and per finding `evidence` (S4) and `suggestion` (S12). `shadow/v1` adds the same `context`, the tool and cost caps under `caps`, `issues` (S9), `codeowners` (S11) and per entry `owners` (S11). A new audit stage, `context`, has one `tool_call` event per call: tool, outcome, reason, spans (at most 50), bytes and the input's SHA-256, never the model's arguments as text ([ADR 0005](../../adr/0005-audit-log.md)). The `agent` stage's `called` record adds `cost_usd` (as in the report) and `failed` adds `cap`, both always present and null when absent; a new `agent` `tools` record lists the tools the CLI's init offered, in context mode only (S3).

### The tools

Decision: [ADR 0013](../../adr/0013-read-only-repo-tools-over-a-commit.md); the seam: [ADR 0014](../../adr/0014-tools-as-a-provider-neutral-executor.md). The model gets three tools named after the SDK's built-ins and doing their read-only subset, all over one commit:

- **`read`** `path`, optional `start_line` (default 1) and `line_count` (default 200, at most 400): numbered lines of one file.
- **`grep`** `literal`, optional `path_glob` and `ignore_case`: matching lines as `path:line:text`, at most 50. A literal, not a regular expression, so no pattern from the model can cost more than linear time ([Q9](#6-open-questions)).
- **`glob`** `pattern`: matching file paths, sorted, at most 200, with [P2-S9's glob rules](../phase-2/slices/p2-s9-repo-config.md#interfaces).

### Tool safety

- **Read-only by construction.** Every tool reads git objects of one commit: no working tree, no checkout, no filter, textconv or hook runs, and no write, shell, network or web tool exists. In context mode the Claude CLI's built-in tools are off (`tools=[]`), the only allowed tools are the three by full name, a `PreToolUse` hook denies any other name, `permission_mode` is `dontAsk`, and `setting_sources=[]`, no agents and strict MCP config stay as in Phase 0. The provider also reads the CLI's init message's tool list and fails `permanent` before any tool runs if it holds anything but the three (S3); a stream without one isn't failed, since the hook and allowlist still hold, and S16's smoke checks the real CLI sends it. Lockdown is asserted in tests for both modes; diff mode's assertions don't change.
- **Every call reaches the executor.** The in-process server is our own server from the `mcp` library (a direct dependency, in step with the SDK's), handed to the SDK as an in-process MCP config. The SDK's `create_sdk_mcp_server` isn't used: it validates arguments against the schema before the handler, so a malformed call would never be recorded or count towards `max_tool_calls` ([ADR 0014](../../adr/0014-tools-as-a-provider-neutral-executor.md)). The executor alone validates (S3 Limits).
- **No git option or pathspec from the model.** The literal always follows `-e`, and the commit is followed by `--` with no pathspec after it, so `-O`, `--open-files-in-pager`, `-f` or `--no-index` are searched as text. `grep`'s `path_glob` is matched in our code with P2-S9's matcher; a pattern starting with `:` is refused. A `read` returns only a tree entry whose path equals the requested path.
- **Confined to the commit.** A path must be relative, `/`-separated, at most 1,000 characters, with no empty, `.` or `..` segment, no backslash, NUL or control character. `.git` internals aren't in a commit; any path segment named `.git` is refused anyway. Symbolic links and submodules are refused, never followed. **Secrets files** are refused and left out of `grep` and `glob` results: `.env` and `.env.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `*.ppk`, `*.kdbx`, `*.tfstate`, `*.tfvars`, `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519` and their non-`.pub` variants, `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `.htpasswd`, `.pgpass`, `credentials` and `credentials.*`. Matching is case-insensitive on the last segment.
- **No secret reaches the model.** The Phase 1 secret scan runs on each whole blob, once per file and commit, giving the line span of each hit. A `read` window that overlaps a hit's span is refused (`possible_secret`), so a PEM block can't be read in slices; a `grep` line inside a hit's span is dropped and counted. A **path** in which the scan finds anything is never shown: it's left out of `glob` and `grep` results like a denied file, a `read` of it is refused `possible_secret` without echoing the path, and it's in no report or audit record (P3-S2 AC14, amended 2026-11-09).
- **Bounded.** Per call: `max_tool_result_bytes` (16,000), lines cut at 2,000 characters, a blob over 1 MB refused, a listing over 50,000 entries refused, and a `grep` reads at most 100 candidate files (files with a match, after `path_glob`), then stops with the truncated note (P3-S2 AC15, amended 2026-11-09). Per review: `max_tool_calls` (12) and `max_tool_total_bytes` (96,000); past either, every call returns a fixed refusal asking for the findings now. The CLI's `max_turns` is `max_tool_calls + 2`, a backstop. Each git command has the existing timeout.
- **Untrusted output.** Every result, refusals included, is wrapped in delimiter lines carrying the SHA-256 of the result, as the diff is ([prompt.py's scheme](../../../src/review_bot/prompt.py)), and the context prompt says tool results and issue text are data, never instructions. Injection cases arrive through a repository file (S5) and an issue body (S9), and the three Phase 1 injection diffs are re-run in context mode (S5, S15).

### Spend and caps

Decision: [ADR 0015](../../adr/0015-per-call-cost-cap-and-step-allowances.md). Context reviews are one query of several turns, so they cost more than one call. Each is bounded three ways: the tool caps above bound what goes in, `max_output_tokens` bounds each turn's output, and **`max_cost_usd`** is passed to the CLI as its per-query budget (`max_budget_usd`), which ends the query when exceeded. The CLI takes a float, so the Decimal is converted rounding down to the cent, never above the cap. The CLI checks it between turns, so one turn can overshoot; the allowances below leave room for that. For context mode this amends [ADR 0012](../../adr/0012-ci-credential-and-cost-caps.md)'s "at most one model call" to one query of several turns.

| Setting | Default | CI (S14) | Notes |
|---|---|---|---|
| `context` | `off` | `off` until the owner sets `REVIEW_BOT_CONTEXT=repo` ([Q6](#6-open-questions)) | |
| `max_tool_calls` | 12 (1 to 30) | 12 | |
| `max_tool_result_bytes` | 16,000 (1,000 to 64,000) | 16,000 | |
| `max_tool_total_bytes` | 96,000 (at most 400,000) | 96,000 | |
| `grep` candidate files per call | 100, fixed (not a setting) | 100 | P3-S2 AC15 |
| `max_cost_usd` | unset; required by every live command in context mode | 0.60, set only when `REVIEW_BOT_CONTEXT=repo`, so diff-mode runs are unchanged | Decimal, 0.01 to 1,000, so the cents are exact as a float |
| `deadline_s` | 300 | 300 in context mode, 240 otherwise | Job timeout stays 10 min |

**Live commands** (`evals live`, `pr backfill`) take `--max-usd`. Before any call they refuse unless planned calls × `max_cost_usd` ≤ `--max-usd`, and during the run they start no call once the reported spend plus `max_cost_usd` would exceed it. Retries stay 0. A failure to report cost counts as `max_cost_usd`.

**Where live steps run.** The key exists only as the `ANTHROPIC_API_KEY` repository secret, and both live commands refuse in CI. Every approved step therefore runs from the manual workflow `live.yml`: `workflow_dispatch` only, on `main` only, with one choice of step and every amount a literal from the table below. S16 creates it with the `smoke` choice and the CI guard; S6 adds `evals-diff` and `evals-context`, and S8 adds `backfill`, each with a test that the real CLI parser accepts the step's arguments. The CLIs accept CI only for a `workflow_dispatch` event with an explicit flag; any other CI context still refuses ([ADR 0015](../../adr/0015-per-call-cost-cap-and-step-allowances.md)).

**The $20 the user approved**, by step. Each line is a hard cap. The $20 covers every dispatched run, re-runs included, summed from reported spend. The reserve is approved by the user where possible, else by the reviewer, and the test report names the approver.

The 9 new context cases: `sample-context`, `clean-context`, `injection-tool-result`, `injection-instruction-context`, `injection-canary-context`, `injection-delimiter-context` (S5), `ticket-criteria`, `injection-issue` (S9) and `domain-money` (S10). Every one has a `repo`: a directory below its case directory holding the diff's base, never its head. They live beside Phase 1's in `tests/evals/cases/`; P1-S17's checks name the five Phase 1 cases, so the new cases need no diff-mode live recording before S15 (P3-S5 AC5).

| Step (S16 choice) | Calls | Per call | Cap |
|---|---|---|---|
| `smoke`: one context review on this repo | 2 | $0.30 | $0.60 |
| `evals-diff`: diff mode on the 9 new cases | 27 (9 × 3) | $0.10 | $2.70 |
| `evals-context`: context mode on the same 9 cases | 27 (9 × 3) | $0.30 | $8.10 |
| `backfill`: labelled PRs, context mode | at most 12 | $0.60 | $7.20 |
| Reserve | | | $1.40 |
| **Total** | | | **$20.00** |

Before S15's live runs, the per-call caps are checked against the smoke's reported cost; if the smoke's cost per review exceeds the context caps, the table goes back to the reviewer. A run that hits its cost or turn cap is reported by name per mode and counts as a miss.

The Phase 2 side costs nothing new: Phase 1's diff-mode recordings and Phase 2's shadow reports of the same PRs are the baseline.

### How the gate is evidenced

Decision: [ADR 0016](../../adr/0016-measure-phase-3-against-phase-2.md).

| Gate clause | Evidence | Closed by |
|---|---|---|
| Precision improves | Labelled PRs: per-unit precision of the context backfill against Phase 2's shadow report of the same PR **at the same head SHA**, both judged to ≥ 90% coverage (`pr compare`). Eval cases: credited ÷ final AI findings, per mode (`evals compare`) | S13, S7 compute; S15 records |
| Recall improves | Labelled PRs: pooled relative recall over accepted issues either run found. Eval cases: each AI bug's share of runs caught per mode, including `double-tax` through callers, a ticket criterion and a domain rule; cap-hit runs count as misses | S13, S7; S15 |
| Cost within budget | Every dispatched run's reported spend, re-runs included, summed against the $20 and each step's cap; mean cost per reviewed PR and per accepted finding beside Phase 2's; no review over `max_cost_usd` | S6, S8, S16; S15 |
| Injection still resisted | Phase 1's cases replayed in diff mode as today; their context variants and the tool-result and issue cases, live, every run passing | S5, S9; S15 |

**What it can't show.** A dozen PRs and nine cases are small samples; the labelled PRs were written by agents and merged by one person; few link issues, so tickets are measured mainly by eval cases; and the context cases were written to need context. The test report discloses this, as Phase 2's does.

### Phase 4 seams

- `ToolExecutor` and the `PreToolUse` hook → policy-as-code through hooks and `can_use_tool`.
- `max_cost_usd` and the step allowances → organisation cost caps (`max_budget_usd`).
- The ledger and per-finding evidence → tracing.
- `evals compare` and context recordings → the prompt eval gate.

## 3. Slices

Order of work; real dependencies are in each spec. Slices on one line can run in parallel, each on its own branch.

1. [P3-S1: Review a local diff with read-only repository tools, offline](slices/p3-s1-local-review-with-repo-tools.md)
2. [P3-S2: Keep the tools inside the commit on hostile paths and repositories](slices/p3-s2-tool-confinement.md)
3. [P3-S3: Let Claude drive the tools, locked down and cost-capped](slices/p3-s3-claude-drives-tools.md)
4. [P3-S4: See the context behind every finding](slices/p3-s4-finding-evidence.md)
5. [P3-S5: Score context-mode eval cases offline, including injection through a repository file](slices/p3-s5-context-eval-cases.md)
6. [P3-S6: Record live context evals within a dollar cap](slices/p3-s6-live-context-evals.md)
7. [P3-S7: Compare diff mode and context mode on the same eval cases](slices/p3-s7-compare-eval-modes.md)
8. [P3-S8: Review pull requests with repository context](slices/p3-s8-pr-review-with-context.md)
9. [P3-S9: Review against the acceptance criteria of linked issues](slices/p3-s9-linked-issue-criteria.md)
10. [P3-S10: Enforce domain rules configured per repository](slices/p3-s10-domain-rules.md)
11. [P3-S11: Route findings to code owners](slices/p3-s11-codeowners-routing.md)
12. [P3-S12: Offer suggested fixes in comments](slices/p3-s12-suggested-fixes.md)
13. [P3-S13: Compare Phase 3 against Phase 2 on the labelled pull requests](slices/p3-s13-compare-labelled-prs.md)
14. [P3-S14: Turn on context reviews in CI with one repository variable](slices/p3-s14-context-in-ci.md)
15. [P3-S15: Close Phase 3: measure the gate, release 0.4.0](slices/p3-s15-close-phase-3.md)
16. [P3-S16: Run the live smoke from a manual workflow, and accept CI only for such a dispatch](slices/p3-s16-live-steps-workflow.md)

**Parallel waves:** S1 ∥ S11 → S2 ∥ S4 → S3 ∥ S5 ∥ S12 → S16 ∥ S7 ∥ S10 → S6 ∥ S8 → S9 ∥ S13 → S14 → S15 (after S16 and P2-S12 AC1 to AC6, #67).

**Smoke stop rule.** S16's `smoke` runs as soon as S16 merges. If it shows any built-in tool available, or the three MCP tools missing, S6 and S8 pause and S3 goes back to design.

## 4. Needs a person

| Action | Blocks |
|---|---|
| Dispatch S16's `smoke` (≤ $0.60) once S16 merges, and confirm the record | Trusting the SDK wiring (S3); S6 and S8 under the stop rule |
| Finish Phase 2's shadow weeks and labelling, and commit the labelled set (P2-S12 AC1 to AC6, #67) | S15 |
| Approve and dispatch S15's live steps from `live.yml` | The gate |
| Approve any use of the reserve | Re-runs past a step's cap |
| Judge every new would-be comment the context backfill produces, with `pr label` | Precision coverage (S13, S15) |
| Allow `issues: read` for the shadow and comment jobs | Linked issues in CI (S14) |
| Decide when to set `REVIEW_BOT_CONTEXT=repo` | Context reviews in CI (S14) |

## 5. Known risks

| Risk | Handling |
|---|---|
| SDK behaviour unproven: built-ins off with `tools=[]` while MCP tools stay on, `max_budget_usd` stopping a query, `total_cost_usd` in the result | S3's fakes test our side; the init tool-list check and the hook stop any built-in that slips through; S16's smoke checks the CLI's side, under the stop rule above. An SDK upgrade also re-checks that the SDK still accepts our `mcp` server, and moves the `mcp` pin with it |
| A live step run from CI spends the key unattended | `live.yml` runs only on manual dispatch from `main`, with literal amounts and no cancel (S16) |
| Context reviews cost several times a diff review | Caps above; context off in CI until the owner decides |
| A repository file or issue carries an injection the model follows | Delimiters, the eval cases, and the model never triggering an action; a finding can still be wrong, which people judge |
| Suggested fixes put model-written code one click from a commit | Added lines only, short, secret-scanned, and a person commits it (S12) |

## 6. Open questions

All decided by the reviewer on the user's behalf, 2026-11-09, after round-1 review. The user may still overrule any of them.

**Q1. The README names the SDK's built-in `Read`, `Grep` and `Glob`. Use them, or our own read-only tools?** **Decided by the reviewer on the user's behalf, 2026-11-09: the recommendation.** Our own three tools over a git commit, through an in-process MCP server ([ADR 0013](../../adr/0013-read-only-repo-tools-over-a-commit.md)). The built-ins read a working tree, follow its symlinks, can't have their output bounded by us, and would need the head checked out in CI.

**Q2. The README says hooks record the context. Record in SDK hooks, or in the tools?** **Decided by the reviewer on the user's behalf, 2026-11-09: the recommendation.** The tools record every call themselves, for every provider; the SDK's `PreToolUse` hook enforces the allowlist ([ADR 0014](../../adr/0014-tools-as-a-provider-neutral-executor.md)). Hook-only recording would leave `MockProvider` runs, and so every offline test, with no record.

**Q3. What does "improve" mean?** **Decided by the reviewer on the user's behalf, 2026-11-09: the recommendation, amended.** On the labelled PRs, paired by head SHA (each candidate report against Phase 2's report with the same head SHA), the context run's precision point estimate and pooled recall are each strictly higher than Phase 2's, or equal at 100%, with both runs judged to ≥ 90% coverage. On the eval cases, no AI bug is caught in a smaller **share** of runs than in diff mode (shares, not counts, since run counts differ), and every injection run passes. Runs that hit a cost or turn cap count as misses and are reported by name. Intervals are reported beside the point estimates; a miss goes to the user with the numbers, never re-run until it passes.

**Q4. What is "within budget"?** **Decided by the reviewer on the user's behalf, 2026-11-09: the recommendation, amended.** The $20 covers every dispatched run, re-runs included, summed from reported spend; every step stays under its cap in [the table](#spend-and-caps), and no context review exceeds `max_cost_usd`. Mean cost per reviewed PR and per accepted finding are reported beside Phase 2's, but don't pass or fail the gate.

**Q5. The $20 allocation.** **Decided by the reviewer on the user's behalf, 2026-11-09:** 3 runs per case instead of Phase 1's 5, with the resized [table](#spend-and-caps) (9 cases); new cases set their thresholds for 3 runs.

**Q6. Context mode in CI.** **Decided by the reviewer on the user's behalf, 2026-11-09:** built in S14, off until the owner sets `REVIEW_BOT_CONTEXT=repo` after #67 closes, so Phase 2's precision isn't measured on a mix; `REVIEW_BOT_MAX_COST_USD` is set only in context mode.

**Q7. Suggested fixes.** **Decided by the reviewer on the user's behalf, 2026-11-09: the recommendation.** Context mode only, a replacement for the one anchored line (1 to 10 lines, at most 1,000 characters), only on an added line, rendered as a GitHub suggestion block in comment bodies and the shadow Markdown.

**Q8. Does CODEOWNERS routing notify?** **Decided by the reviewer on the user's behalf, 2026-11-09: the recommendation.** No. Owners appear in code spans, which notify nobody, in each comment's record and grouped in the summary. GitHub already requests code owners' reviews; a bot mention would be new noise. Revisit once comment-mode precision is known.

**Q9. Literal `grep`, or regular expressions?** **Decided by the reviewer on the user's behalf, 2026-11-09:** literal (`git grep -F`), with the literal after `-e`, the commit followed by `--`, and `path_glob` filtered in our code. A model-written regex can cost exponential time in some engines, and callers and definitions are found by name.

**Q10. Which labelled PRs in S15?** **Decided by the reviewer on the user's behalf, 2026-11-09:** labelled PRs whose final head SHA has a Phase 2 shadow report (`reviewed`), newest first, at most 12 by the budget.

**Q11. Building Phase 3 before Phase 2's gate.** **Decided by the reviewer on the user's behalf, 2026-11-09:** accepted as a documented exception to the README's "starts only when the previous gate is met". S1 to S14 and S16 change no finding of a Phase 2 shadow run unless a person turns them on, and S15 waits for P2-S12 AC1 to AC6 (#67).
