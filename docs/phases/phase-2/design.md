# Phase 2: Integrated — design

- **Status:** Draft, for the user's approval (docs PR `docs/phase-2-design`). Criterion IDs are stable once approved.
- **Review:** reviewer-approved on 2026-11-08 after four rounds. **Approved by the user on 2026-11-08** (PR #46), with the recommended defaults for every open question; Q7 and Q11 answered as below.

## 1. Goal and gate

Run the bot on this repository's own pull requests (`sleepyshark85/code-review-bot`) without creating noise. It starts in shadow mode, reporting what it would post, and posts and maintains its own comments once the user decides. Scope and gate: [README Phase 2](../../../README.md#phase-2-integrated). The user's decision of 2026-11-08 replaces "a RightShip repo" with this repository, starts with an Action posting through `GITHUB_TOKEN`, and leaves the GitHub App optional and later ([ADR 0008](../../adr/0008-pull-request-action-with-github-token.md)).

Read for dogfooding, the gate is: **weeks of shadow mode on this repo's PRs, with precision measured from the user's judgements; a value baseline from this repo's PR history, captured before comments are turned on; and the user named as owner.** [How the gate is evidenced](#how-the-gate-is-evidenced) says what closes each clause. Autonomy: observe, then comment.

## 2. Architecture

### Module map

A new package, `review_bot.pr`, with its own entry point `python -m review_bot.pr`, as `review_bot.evals` has. `review-bot <diff>` keeps its contract. `comments`, `plan`, `shadow`, `feedback`, `config` and `metrics` are pure; `git`, `github`, `label` and `cli` touch the outside world; `runner` orchestrates through injected collaborators.

| Module | Responsibility | Key types | From |
|---|---|---|---|
| `pr.cli` | Subcommands, flags, exit codes | `main` | S1 |
| `pr.shadow` | Builds the `shadow/v1` document and its Markdown | `ShadowReport`, `render_shadow_markdown` | S1 |
| `pr.comments` | A finding's comment body and marker; the summary body; reading markers back | `comment_body`, `summary_body`, `read_marker` | S1 |
| `pr.plan` | Decides what to create, update, resolve, move, keep or withhold | `plan_comments`, `CommentPlan` | S1, grows in S5, S7 |
| `pr.event` | Parses the `pull_request` event payload | `PullRequestRef`, `read_event` | S2 |
| `pr.git` | Diffs and file reads between commits in a local clone, by subprocess | `GitRepo` | S2 |
| `pr.runner` | One PR run: refuse, diff, review, plan, report, apply; and backfill | `run_pull_request`, `Outcome` | S2, S13 |
| `pr.github` | The GitHub API, behind a `Protocol`, and its one real client | `GitHubClient` (S5), `RestGitHubClient` (S6) | S5 |
| `pr.fake_github` | In-memory GitHub for tests, in `src/` as `MockProvider` is | `FakeGitHubClient` | S5 |
| `pr.feedback` | `feedback/v1` records, from judgements, reactions and resolutions | `FeedbackRecord`, `labels_from_signals` | S4, S8 |
| `pr.label` | Interactive judging of downloaded shadow reports | `run_labelling` | S4 |
| `pr.config` | Parses `.review-bot.toml` from the base commit | `RepoConfig` | S9 |
| `pr.metrics` | Latency, tokens, cost, outcomes, precision; the baseline | `summarise_runs`, `summarise_baseline` | S10, S11 |
| `render` (changed) | Issue, PR and commit references become inert too (fixes [#37](https://github.com/sleepyshark85/code-review-bot/issues/37)) | | S1 |
| `settings`, `review`, `providers/claude_agent` (changed) | A cap on the bytes sent to the model, after skipping; an output token cap passed to the Claude CLI | `max_review_bytes`, `max_output_tokens` | S2 |

`.github/workflows/review.yml` and `.review-bot.toml` are outside every subagent's paths. The main session writes them from the slice spec, as it did `ci.yml`, and the tester checks the workflow by parsing it.

### Data flow (one PR run)

event → **refuse** (fork, action, no credential in CI, draft in comment mode) → git diff of base...head (from S7, only the hunks changed since the last reviewed commit) → **the Phase 1 pipeline** (secret scan → parse → skip → rules → agent → verify → policy; S2 adds the reviewable-bytes cap after the skip step, S9 adds config exclusions and threshold) → `plan_comments` against the bot's existing comments (none in shadow mode) → `shadow/v1` and Markdown, always written → in comment mode only, apply the plan through `GitHubClient` → summary comment.

A secret hit, an oversize or unreadable diff, a skip, an invalid config or a provider failure still ends in a written report with that outcome, so every event leaves an explicit result ([README targets](../../../README.md#targets)).

### Interfaces

Slice specs list the fields; this is the shape.

```python
class Outcome(StrEnum):  # shadow/v1 "outcome"
    REVIEWED = "reviewed"
    BLOCKED_SECRET = "blocked_secret"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PullRequestRef:  # from the event; repo and SHAs validated by pattern
    repo: str
    number: int
    base_sha: str
    head_sha: str
    is_fork: bool
    is_draft: bool
    event_at: datetime


class GitRepo:  # subprocess, argument list, timeout, no external diff or textconv
    def __init__(self, path: Path, *, timeout_s: float) -> None: ...
    def diff(
        self, base: str, head: str
    ) -> str: ...  # three-dot, renames detected; raises GitError (19)
    def read_file(self, commit: str, path: str, *, max_bytes: int) -> bytes | None: ...  # S9
    def is_ancestor(self, older: str, newer: str) -> bool: ...  # S7


@dataclass(frozen=True, slots=True)
class BotComment:  # only comments by the bot with a valid marker
    comment_id: int
    thread_id: str
    fingerprint: str
    rule_part: str
    anchor: str
    path: str
    line: int | None  # None when GitHub marks it outdated
    severity: Severity
    resolved_by: Resolver | None  # None (open), BOT or PERSON


def plan_comments(
    findings: Sequence[ReviewedFinding],
    existing: Sequence[BotComment],
    *,
    pr_diff: ParsedDiff,
    changed: ParsedDiff | None,
    max_open_comments: int,
) -> CommentPlan: ...  # create, update, resolve, move, keep, withheld (each with a reason)


class GitHubClient(Protocol):  # sync; every call has a timeout; errors become GitHubError (18)
    def list_bot_comments(self, pr: PullRequestRef) -> tuple[BotComment, ...]: ...
    def read_summary(self, pr: PullRequestRef) -> Summary | None: ...  # reviewed_sha, from S7
    def post_review(
        self, pr: PullRequestRef, comments: Sequence[NewComment]
    ) -> None: ...  # one review
    def update_comment(self, comment_id: int, body: str) -> None: ...
    def resolve_thread(self, thread_id: str) -> None: ...
    def write_summary(self, pr: PullRequestRef, body: str) -> None: ...  # create or edit in place

    # S8: list_signals(pr) -> reactions and resolver per own comment
    # S11: list_merged_pull_requests(repo, since, until), count_issues(repo, label, since, until)


async def run_pull_request(
    pr: PullRequestRef,
    *,
    mode: Mode,
    git: GitRepo,
    provider: LLMProvider,
    github: GitHubClient | None,
    settings: Settings,
    audit: AuditLog,
    clock: Callable[[], datetime],
) -> ShadowReport: ...  # github is None in shadow mode
```

**`shadow/v1`**, one ASCII JSON document: `schema`, `run_id`, `mode` (`shadow` or `comment`), `origin` (`local`, `event` or `backfill`), `outcome`, `reason` (a skip reason or error category, else null), `pr` (`repo`, `number`, `base_sha`, `head_sha`, or null for a local run), `event_at`, `started_at`, `finished_at` (UTC), `report` (the [`report/v1`](../phase-1/slices/p1-s10-json-output.md#interfaces) object, or null if no review ran), `summary_body`, `comments` (each `action`, `fingerprint`, `rule_part`, `anchor`, `source`, `severity`, `file`, `line`, `body`), `withheld` (each `fingerprint`, `file`, `line`, `reason`), `caps`, `over_budget`. Later slices add keys and never remove them: each comment's `applied` (`done`, `failed` or `not_attempted`, S5), `scope` and `since` (S7), and `config` (S9).

**`feedback/v1`:** [ADR 0011](../../adr/0011-feedback-labels.md) and [P2-S4](slices/p2-s4-label-shadow-findings.md#interfaces).

### Commands and exit codes

| Command | Does | From |
|---|---|---|
| `python -m review_bot.pr shadow <diff> --out DIR` | A shadow report for a local diff, with no GitHub | S1 |
| `python -m review_bot.pr run --event PATH --repo DIR --out DIR [--mode shadow\|comment]` | One PR run | S2; `comment` from S5 |
| `python -m review_bot.pr backfill --pulls FILE --repo DIR --out DIR --max-calls N` | Shadow reports for past merged PRs | S13 |
| `python -m review_bot.pr label --reports DIR --labels FILE` | Judge would-be comments | S4 |
| `python -m review_bot.pr collect --event PATH --out DIR` | Reactions and resolutions as labels, with no model | S8 |
| `python -m review_bot.pr metrics ...` / `baseline ...` | Gate numbers | S10, S11 |

`review_bot.pr` uses the [Phase 0](../phase-0/design.md#exit-codes) and [Phase 1](../phase-1/design.md#exit-codes) codes, except that findings aren't a failure:

| Code | `review_bot.pr` meaning |
|---|---|
| 0 | A report was written with outcome `reviewed`, `blocked_secret` or `skipped` |
| 2 | Usage error, or an invalid `.review-bot.toml` (report written, outcome `failed`, reason `invalid_config`, S9) |
| 7–13 | The provider failed; report written, outcome `failed` |
| 14, 17 | As today |
| **18** (new) | A GitHub API call failed (S5) |
| **19** (new) | The event payload, the pulls file or a git command was unusable (S2) |

`review-bot` itself never returns 18 or 19.

### Comment state

Decision: [ADR 0009](../../adr/0009-comment-state-on-the-pr.md).

- **Identity:** the bot's comments are those whose REST `user.login` is `github-actions[bot]` with `user.type` `Bot`. Threads are found by comment ID, never by GraphQL login (which reads `github-actions`).
- **Markers:** an inline comment's last line is `<!-- review-bot:v1 fingerprint=<16 hex> rule=<rule part> anchor=<16 hex> -->`. `anchor` is the first 16 hex digits of the anchored line's hash, as [ADR 0003](../../adr/0003-finding-fingerprints.md) defines it. The summary comment's last line is `<!-- review-bot:v1 summary reviewed=<40 hex> -->`. Bodies are rendered inert ([safety](#safety-on-the-pr-path)), so model or diff text can't produce `<!--`.
- **Pairing, not ordinals:** the fingerprint's ordinal depends on which findings a run sees, so it shifts under incremental review, or when an identical line is added above. A comment's **key** is its path, its rule part and its anchor. The path follows renames in the PR diff, and from S7 also in the diff since the last reviewed commit. **Candidates** are open threads and threads a person resolved. A thread the bot resolved is never a candidate, so a moved comment's old thread, or a resolved comment whose line comes back, can't capture a finding. Each current finding pairs with at most one candidate, in this order:
  1. the same fingerprint;
  2. otherwise the same key, choosing the nearest current line (outdated comments last), and between candidates at equal distance, the one with equal severity;
  3. any remaining tie goes to an open thread over a person-resolved one, then to the oldest comment ID.

  A paired comment keeps its posted fingerprint, which stays its identity for labels.
- **GitHub's outdated flag is only a hint.** No GitHub document says it means the anchored line itself changed, so no comment is re-posted or resolved on that flag alone.
- **Plan rules for a paired comment:**
  - **keep:** open, not outdated; **update** instead if its severity changed.
  - **move:** open but outdated. The old thread is resolved, and the same body and fingerprint are posted on the **pairing finding's line**, which settles it when the anchor text appears on several lines. The move goes ahead only if that line can be commented on in the PR diff; otherwise nothing is resolved or posted, and the finding is withheld `not_commentable`.
  - **nothing:** a thread **a person resolved** is never posted again. A finding that only a bot-resolved thread matches is unpaired, so it is created like any other.
- **Plan rules for everything else:**
  - **create:** an unpaired finding, if its line can be commented on in the PR diff (otherwise withheld `not_commentable`).
  - **resolve or keep an unpaired open comment:** resolve it only if **its anchored line is gone**. If it isn't outdated, gone means the line at its current position is no longer in the PR diff, or no longer has its anchor. If it is outdated, gone means its anchor text is on no line of its file's PR diff (following renames). Otherwise it is kept. Code changing elsewhere in the hunk or file never resolves a comment, nor does a fixed issue whose anchored line stays, such as `pass` replaced by logging under an unchanged `except Exception:`; a person resolves those.
  - **duplicates:** two open comments with one fingerprint: keep the one that isn't outdated, else the older, and resolve the other.
- **Volume:** at most `max_open_comments` open own comments per PR (default 10). New ones are created in order of severity, then file, then line, until the cap; the rest are withheld `over_limit`. All creates and moves go in one review, so there is one notification per run.
- **Summary:** one issue comment, edited in place. It holds the outcome, the counts, at most 20 withheld findings then "and K more", and "No findings at or above `<threshold>`." on a clean PR. It never exceeds 60,000 characters, under GitHub's 65,536. It is the README's "no issues" check; the job itself is the check run, so no `checks: write` is needed.
- **Skips post nothing:** outcome `skipped` (fork, action, no credential, draft, nothing to review, already reviewed, too large) posts and edits nothing. A push with no new changes (S7) is `reviewed` with no model call; it has no findings to pair, so it only resolves comments whose anchored lines are gone. `blocked_secret` and `failed` update only the summary.

### Safety on the PR path

- **Trigger:** `pull_request` only (`opened`, `synchronize`, `reopened`, `ready_for_review`; `closed` for S8), never `pull_request_target`. The job runs only if the head repository is this repository. `run` refuses a fork again from the event, and treats a null head repository (a deleted fork) as a fork ([ADR 0008](../../adr/0008-pull-request-action-with-github-token.md)).
- **Audit log under `--out`:** `run` writes its audit log into `--out` unless `REVIEW_BOT_AUDIT_LOG` is set, so the artifact uploads it from a path the key-canary test covers.
- **Code from the base:** the base SHA is checked out with `fetch-depth: 0` and `persist-credentials: false`, and `uv sync --locked --no-dev` runs there. The head commit is fetched only for `git diff`, which runs with no external diff or textconv. Config is read from the base commit (S9).
- **No event text in shell:** `run:` blocks never interpolate `${{ github.event.* }}`. Only the PR number and SHAs reach steps, through `env`. Job `if:` expressions may also read the event's action and head repository name. `run` reads everything else from the event file.
- **Permissions:**
  - The shadow job has `contents: read` only.
  - From S6, a separate comment job adds `pull-requests: write`. It runs only when the repo variable `REVIEW_BOT_MODE` is `comment`.
  - An unknown `REVIEW_BOT_MODE` fails a check step visibly.
  - No job is named `gate`, none may be a required check, and each reports its own status honestly ([Q9](#5-open-questions)).
- **Secrets:**
  - `ANTHROPIC_API_KEY` reaches only the review step, and only for same-repo PRs ([ADR 0012](../../adr/0012-ci-credential-and-cost-caps.md)).
  - The provider's subprocess never receives `GITHUB_TOKEN`, and the key appears in no artifact.
  - The secret scan runs on the PR diff before any call, as in Phase 1. A hit gives outcome `blocked_secret` and, in comment mode, a summary naming the kinds and diff lines, never values.
- **Prompt injection:** the diff stays delimited data, the model has no tools, and it never triggers an action. `plan_comments` is deterministic policy, and anchors are verified twice, against the reviewed diff and against the PR diff.
- **What a comment can't do:**
  - Bodies follow [P1-S11's inert rules](../phase-1/slices/p1-s11-markdown-output.md#interfaces): no headings, HTML, images, links or mentions. Every `@`, URL and, from S1, `#123`, `GH-123`, `owner/repo#123` or 7-to-40-hex word goes in a code span, so nothing notifies, links or cross-references.
  - Each body is secret-scanned before it's planned, and a hit withholds it (`possible_secret`).
  - A rationale over 2,000 characters is cut and marked as cut.

### CI settings and caps

Decision: [ADR 0012](../../adr/0012-ci-credential-and-cost-caps.md). The workflow sets these as `REVIEW_BOT_*` variables; repo config never sets them.

| Cap | CI value | Why |
|---|---|---|
| Model calls per run | 1 (single-shot pipeline) | |
| `REVIEW_BOT_MAX_RETRIES` | 0 | One attempt per call, as in the live eval |
| `REVIEW_BOT_MAX_DIFF_BYTES` | 400,000 (the default) | Memory bound on the raw diff |
| `REVIEW_BOT_MAX_REVIEW_BYTES` (new) | 120,000 | Bounds input tokens, measured on the files sent to the model after docs, lock and generated files are skipped. Over it: outcome `skipped`, `too_large`, no call |
| `REVIEW_BOT_MAX_OUTPUT_TOKENS` (new) | 16,000 | Bounds output; Phase 1's largest run used 5,740 |
| `REVIEW_BOT_DEADLINE_S` | 240 | Keeps p95 under 5 min including set-up |
| Job `timeout-minutes` | 10 | |
| Concurrency | One group per PR, cancel in progress | A new push stops spending on the old one |

`over_budget` is true when a run's reported usage exceeds a cap. Both new settings are unset by default for `review-bot`, which then behaves as before.

### How the gate is evidenced

| Gate clause | Evidence | Closed by |
|---|---|---|
| Weeks of shadow mode | Shadow workflow on every same-repo PR from S3's merge; artifacts; the start date in the test report | S12, per [Q1](#5-open-questions) |
| Precision measured | The user's judgements in `feedback/v1`, and `metrics` output | S4 records, S10 computes, S12 reports |
| Value baseline captured | `baseline/v1` from merged PRs before comments are turned on, plus cost per accepted finding | S11, S10, S12 |
| Named owner | README Ownership names the user, a support channel, a runbook and who funds the key | S12, the user |

**PR supply (estimate, 2026-11-08).** 44 PRs have merged, about 19 of them in Phase 1's three days. Of the last 17, 6 exceed 120,000 raw diff bytes, which is why the cap counts only reviewable bytes. How many stay over it once docs, lock and generated files are skipped isn't measured yet: S13's backfill reports it, and S9's `exclude` can drop eval recordings and fixtures if they dominate. Docs-only PRs end `skipped` and add nothing. Live shadow after S3 should see about 9 slice PRs (S4 to S13), plus fixes. That alone may not reach Q1's sample, hence the backfill (S13) over about 30 recent merged PRs, and a person keeping PRs flowing through the window. With 1 to 3 would-be comments per reviewed PR, that's roughly 40 to 120 to judge.

**Precision, from shadow reports.**
- **Scope:** only reports with `mode` `shadow`, whether their `origin` is `event` or `backfill`.
- **Unit:** a would-be comment, which is a (repo, PR, key, index) group of `create` entries across that PR's reports, judged once. The index numbers a report's entries for one key in line order, so two identical lines are two units, as in comment mode. A key's unit count is the most entries any one report has for it. This is how comment mode would treat it, so repeated pushes and rewordings count once.
- **Verdicts:** the user judges each with `label`. **Accept** means correct and worth a comment. **Reject** comes with a reason: `wrong`, `not_worth_it`, `wrong_line` or `duplicate`. The latest judgement per unit wins.
- **Formula:** precision is accepted ÷ (accepted + rejected other than `duplicate`). Duplicates are artefacts of stateless re-review on neighbouring lines, so they are reported separately. A **lower bound** with duplicates counted as rejections, accepted ÷ (accepted + all rejected), is reported beside it.
- **Reported with it:** the count, a Wilson 95% interval, coverage (judged ÷ units), the number of PRs contributing and the largest PR's share, and splits by origin, source and rule part.
- **Clustering:** comments within a PR are correlated, so the interval is optimistic.
- **Thresholds:** the gate needs coverage of at least 90%, so the user can't judge only the easy ones. The README's 70% is read as the **point estimate**, and the Wilson lower bound is reported beside it ([Q2](#5-open-questions)). `wrong_line` counts the README's "hallucinated line references" that verification can't catch.

**Precision, from comments (after the switch).** The same formula on reactions ([ADR 0011](../../adr/0011-feedback-labels.md)), joined to the `mode` `comment` reports that created each comment, with reaction coverage beside it. Shadow and comment precision are never merged.

**Baseline.** Measured before the switch, per merged PR: open-to-merge hours, commits made after opening, and the escaped-defect proxy ([Q8](#5-open-questions)), as median and p90, with the window and PR count. After the switch the same numbers are comparable. Cost per accepted finding uses the user's token prices.

**Start-up cost (README risk).** `metrics` shows review time (`finished_at − started_at`) beside end-to-end time (`finished_at − event_at`), and S3 records one run's set-up steps from the Actions log, so the SDK's start-up and the job's set-up are measured against the review itself.

**Disclosures for the test report.** This repo's PRs are written and reviewed by agents and merged by one person, so precision and cycle time here may not carry over to a human team. Backfilled PRs had already been reviewed. The prompt is still `p1-v1`, tuned on Phase 1's cases.

### Phase 1 seams

- **Fingerprints:** consumed by comment state, through the marker and key above.
- **`report/v1` and Markdown rules:** embedded in `shadow/v1` and reused for comment bodies.
- **Audit log:** uploaded with each run.
- **Triage labels:** `feedback/v1` reuses their vocabulary (`accept`, `reject`) and keying by fingerprint for PR feedback. Local `--triage` labels have no PR, so Phase 2 doesn't aggregate them.
- **Eval format:** moves to Phase 3. Phase 2 produces the labelled set (labels plus would-be comment lists and SHAs), and Phase 3 turns it into eval cases.

## 3. Slices

Order of work; real dependencies are in each spec.

1. [P2-S1: Get a shadow report of what the bot would post, for a local diff](slices/p2-s1-shadow-report-local.md)
2. [P2-S2: Review a pull request from its event, within a per-run budget](slices/p2-s2-review-pr-event.md)
3. [P2-S3: See a shadow report on every same-repo PR, from an Action that posts nothing](slices/p2-s3-shadow-workflow.md)
4. [P2-S4: Judge would-be comments from shadow reports, and keep the labels](slices/p2-s4-label-shadow-findings.md)
5. [P2-S13: Backfill shadow reports over recent merged PRs](slices/p2-s13-shadow-backfill.md)
6. [P2-S5: Create, update, move and resolve the bot's own comments](slices/p2-s5-comment-sync.md)
7. [P2-S6: Turn comments on with one repository variable](slices/p2-s6-comments-on.md)
8. [P2-S7: Re-review only what changed since the last reviewed commit](slices/p2-s7-review-new-commits.md)
9. [P2-S8: Turn reactions and resolutions into labels](slices/p2-s8-reactions-as-labels.md)
10. [P2-S9: Set the review policy per repository](slices/p2-s9-repo-config.md)
11. [P2-S10: Measure latency, tokens, cost, outcomes and precision](slices/p2-s10-metrics.md)
12. [P2-S11: Capture the value baseline from PR history](slices/p2-s11-value-baseline.md)
13. [P2-S12: Close Phase 2: measure the gate, name the owner, release 0.3.0](slices/p2-s12-close-phase-2.md)

S1 to S4 and S13 start the shadow clock and the labels; everything after runs alongside the weeks of shadow mode. The switch to comments is the user's, and should wait for S7, without which every push re-reviews the whole PR.

## 4. Needs a person

| Action | Blocks |
|---|---|
| Add the `ANTHROPIC_API_KEY` repository secret | Any model call in CI. Until then every run is `skipped`, `no_credential`, and the shadow clock doesn't start |
| Set a monthly spend limit on that Anthropic account | The only monthly cost cap ([ADR 0012](../../adr/0012-ci-credential-and-cost-caps.md)) |
| Approve workflow permissions: keep fork workflows needing approval, allow `pull-requests: write` for the comment job, and never add review jobs to branch protection | S3 (read-only) and S6 (write) |
| Check the first live CI run: tokens under the output cap, latency, set-up time, the report | Trusting the caps (S3) |
| Approve the backfill's budget (about 30 calls) and run it with your login or key | Enough sample for Q1 (S13) |
| Keep producing PRs through the shadow window, including ordinary fixes, not only docs | Q1's sample; docs-only PRs count for nothing |
| Act as named owner: README Ownership, the support channel, the runbook, funding the key | The gate (S12) |
| Create the `review-bot` issue label, the support channel | S12's Ownership entry |
| Download artifacts within 90 days and judge would-be comments with `label`, weekly | Precision, the gate's main clause (S4, S12) |
| Give token prices when running `metrics` | Cost and cost per accepted finding (S10) |
| Decide when to switch from shadow to comments, and set `REVIEW_BOT_MODE=comment` | Autonomy "comment"; comment-mode precision (S6, S8) |
| React with thumbs up or down on bot comments once they post | Comment-mode precision (S8) |
| Answer Q7 and Q11, which change README scope | S12's closing task |
| Optionally, create a GitHub App | Nothing in Phase 2 |

## 5. Open questions

Q1 to Q6 and Q8 to Q10 have recommended defaults, accepted with the design unless you say otherwise. **Q7 and Q11 change the README's Phase 2 scope and need your explicit answer**; they have no default.

**Q1. How long is "weeks of shadow", and how big a sample?** **Default:**
- At least 3 weeks of live shadow mode from the first reviewed event.
- At least 12 reviewed PRs, of which at least 5 are live and the rest may be backfilled.
- At least 30 judged would-be comments, with coverage of at least 90%.

The gate closes when the last of these is met. If the window ends short, the shortfall goes to you with the numbers, rather than being extended silently.

**Q2. When to switch to comments?** **Default:** your call. It's recommended once S7 has merged and the shadow precision point estimate is at least 70% on Q1's sample, with the Wilson lower bound shown beside it. Below that, stay in shadow and report it.

**Q3. Retries in CI.** **Default:** 0. **Alternative:** 1, which loses fewer reviews to transient errors, at up to twice the attempts.

**Q4. Cap values.** **Default:** the [table above](#ci-settings-and-caps), and at most 10 open bot comments per PR.

**Q5. What the artifact holds.** **Default:** `shadow/v1`, its Markdown and the audit log, kept 90 days, and never the diff, which git rebuilds from the SHAs. **Alternative:** omit the audit log, whose scrubbing is best-effort.

**Q6. Where labels live.** **Default:** committed by the main session after each labelling session, as `tests/evals/labelled/labels.jsonl` with a text-free list of would-be comments. They hold PR numbers, keys and verdicts only, and become Phase 3's labelled set. **Alternative:** a local file only, which can be lost.

**Q7. Suggested fixes (decided by the user, 2026-11-08: deferred to Phase 3).** The README lists them for Phase 2. They need a new `Finding` field and a prompt change with a new live eval, and they put model-written code one click from a commit. **Option A:** defer them to Phase 3 and change the README. **Option B:** add a slice after S6.

**Q8. Escaped-defect proxy for the baseline.** **Default:** issues labelled `bug`, plus merged PRs whose title starts with the word `fix`, per merged PR in the window. **Alternative:** your own count, entered by hand.

**Q9. Does a bot failure turn the PR's review job red?** **Default:** yes, for outcome `failed` and for exits 18 and 19, so the owner notices; green for every other outcome, findings included. The job is never required, so red never blocks a merge.

**Q10. Drafts.** **Default:** shadow mode reviews drafts too, for more data; comment mode skips drafts until `ready_for_review`.

**Q11. The GitHub App (decided by the user, 2026-11-08: Option A, no App in Phase 2).** The README says "GitHub Action, then a GitHub App". **Option A:** no App in Phase 2, and the README line becomes "a GitHub Action (an App later)". **Option B:** keep an App slice in Phase 2.
