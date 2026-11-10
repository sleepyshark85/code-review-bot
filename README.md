# Review Bot

An automated PR reviewer for RightShip, and the first agent on a shared agent platform.

> **Status:** Phase 1 complete, with one documented eval miss ([test report](docs/phases/phase-1/test-report.md#p1-s17-measure-the-real-model-and-hold-ci-to-the-result)) · Phase 2 released as 0.3.0 and running in shadow mode; its gate evidence is pending ([#67](https://github.com/sleepyshark85/code-review-bot/issues/67)) · Phase 3 next · **Owner:** [@sleepyshark85](https://github.com/sleepyshark85) (see [Ownership](#ownership)) · **Last updated:** 2026-11-09

## Usage

Install the [latest release](https://github.com/sleepyshark85/code-review-bot/releases/latest), which requires Python 3.12 or later:

```
uv tool install https://github.com/sleepyshark85/code-review-bot/releases/download/v0.3.0/review_bot-0.3.0-py3-none-any.whl
review-bot <diff>                    # a real review with Claude
review-bot <diff> --provider mock    # offline smoke test: no model call; its canned finding is dropped (not in the diff), so it exits 0
```

From a clone, run `uv sync`, then `uv run python -m review_bot <diff>`. The usage below describes `main` and may be ahead of the latest release.

- **Output:** text by default. `--format json` (or `REVIEW_BOT_FORMAT=json`) prints one ASCII JSON document (`report/v1`) with each finding's fingerprint, source and rule, plus counts and skipped files. `--format markdown` prints a review to paste into a pull request, in which text from the diff or the model can't form headings, links, HTML, @mentions, or issue, pull request and commit references. The stderr summary is the same in every format.
- **Triage:** `--triage` shows each reported finding on stderr and asks `[a]ccept [r]eject [e]dit`. Edit asks for a new severity, then a new rationale; Enter keeps the current value, and the file and line can't change. An edited finding stays in the report even if its new severity is below the threshold. Rejected findings leave the text and Markdown reports; JSON keeps them, marked `reject`, and an edited finding's `original_rationale` holds the text it replaced. Each decision is logged as a label, with no rationale text. End of input or Ctrl+C leaves the rest `undecided`. It needs stdin and stderr on a terminal, otherwise it exits `2` at once; on Windows, Git Bash's default mintty window isn't one, so use Windows Terminal or `winpty`. It has no environment variable.
- **Authentication:** your Claude Code login, or `ANTHROPIC_API_KEY` if set. In diff mode, the default, the model runs with no tools. With `--context-repo` it may use only the three read-only repository tools below.
- **Exit codes:** `0` no findings at or above the threshold, `1` findings (with `--triage`, `1` if any finding wasn't rejected), `2` to `6` usage or input errors, `7` to `13` provider failures such as `8` for authentication, `14` internal error, `15` input that isn't a diff, `16` a possible secret in the diff, `17` an audit log that can't be written. See the exit codes for [Phase 0](docs/phases/phase-0/design.md#exit-codes) and [Phase 1](docs/phases/phase-1/design.md#exit-codes) and [settings](docs/phases/phase-0/design.md#settings) tables.
- **Some checks need no model.** Built-in rules catch a swallowed exception (`except …:` then `pass` or `...`) and `open()` without an encoding in text mode, in added Python code. Their findings print first, carry `confidence 1.00`, and can turn an otherwise clean run into exit `1`. Each finding has a stable fingerprint. A model finding tagged with a rule's ID, within 3 lines of that rule's finding in the same hunk and no more severe, is reported once, as the rule's; a more severe one still prints. Model findings repeated with the same file, rule or category, line and rationale are reported once.
- **Only real diffs are reviewed.** Text that isn't a unified diff exits `15`. In a git diff, docs, lock, generated and binary files are skipped and not sent to the model; a plain unified diff is reviewed whole.
- **A diff with a detected secret is never sent.** Before anything is parsed, the whole diff text is scanned for known key and token formats, private keys and quoted credentials. File paths in the headers are also scanned as git decodes them, so a key hidden in a path's octal escapes is caught too. A hit exits `16` with the kind and diff line of each, never the value. Secrets in other formats aren't detected.
- **Repository context (Phase 3):** `review-bot DIFF --context-repo DIR [--context-rev REV]` lets the model read, search and list the repository at a commit (default `HEAD`) through three read-only tools of its own, `read`, `grep` and `glob`, before it reports. They read git objects, so uncommitted edits are invisible, and `DIR` may be a subdirectory of the repository. Each call is capped: `--max-tool-calls` (default 12, 1 to 30), `--max-tool-result-bytes` (16000, 1000 to 64000) and `--max-tool-total-bytes` (96000, at most 400000), each also settable as `REVIEW_BOT_MAX_TOOL_CALLS` and so on. Every result is framed as untrusted data. The tools refuse paths outside the commit, `.git`, secrets files (`.env`, private keys, `credentials`, `*.tfstate` and similar), files whose name holds a secret, symbolic links, submodules, binary or over-1 MB files, and any lines that may hold a secret (`grep` says how many it withheld from the files it read; one call reads at most 100 matching files). A git timeout or failure during a tool call refuses that call, and the review goes on. Each finding cites the lines it relied on as `evidence` (`{file, start_line, end_line}` spans), and a span is kept only if the diff or a tool call actually showed those lines, otherwise it is counted in `evidence_dropped`; PR comments and the shadow Markdown show the kept spans on a `Context:` line. The context prompt version is in the JSON report. The JSON report adds `mode` and a `context` summary, the stderr summary adds `tool_calls=N`, and the audit log records one `context`/`tool_call` event per call, with the lines read but never their text. A `--context-repo` that isn't a git repository, or a `--context-rev` that names no commit, exits `2`; git that can't start or times out while opening the repository exits `19`. With Claude, the CLI's built-in tools are off and only `mcp__repo__read`, `mcp__repo__grep` and `mcp__repo__glob` are allowed; a `PreToolUse` hook denies any other tool. If the CLI's init message lists any other tool, or lacks one of the three, the review fails `permanent` (exit `12`) before any tool runs. The CLI's turn cap is `--max-tool-calls` plus 2; a review that reaches it fails `permanent`, naming `--max-tool-calls`. The audit log records the CLI's tool list (`agent`/`tools`).
- **Cost cap:** `--max-cost-usd USD` (`REVIEW_BOT_MAX_COST_USD`) caps what one review may cost, in either mode. It takes 0.01 to 1000 and is unset by default. It is passed to the Claude CLI as its query budget, rounded down to the cent. The CLI checks the budget between turns, so a review can go over by one turn. A review that reaches the cap fails `permanent` (exit `12`), naming `--max-cost-usd`. The JSON report's `usage.cost_usd` is the cost Claude reported, as a decimal string, or `null` if it reported none; the audit log's `agent`/`called` record holds it too.
- **Suggested fixes (context mode):** a finding may carry a `suggestion`: the model's replacement for the one added line the finding is anchored on. It is kept only if it is at most 10 lines and 1,000 characters, changes the line (ignoring trailing whitespace), holds no control character other than tab and line feed, and passes the secret scan; bidi controls and Unicode line or paragraph separators count as control characters, so a one-click commit can't plant text that reads one way and compiles another. Otherwise it is dropped with a reason (`not_added_line`, `too_long`, `unchanged`, `control_characters`, `possible_secret`) and the finding is kept. An empty suggestion deletes the line. Diff mode never asks for one, so any it returns is dropped (`not_requested`). A kept suggestion shows in PR comments and the shadow Markdown as a GitHub ```` ```suggestion ```` block the author can commit in one click, and in `--format markdown` as a plain code block. `--format json` adds `suggestion` and `suggestion_dropped` to each finding. Nothing is applied automatically.
- **Every run is traced** in a JSONL audit log, `.review-bot/audit.jsonl` under the working directory by default (that directory gets its own `.gitignore`). Set another path with `--audit-log PATH` or `REVIEW_BOT_AUDIT_LOG`. It records what each stage decided, never diff lines, model text or secret values. The exceptions are provider warnings (`agent.warning`) and, for a failed Claude call, the last lines of the CLI's stderr; both are scrubbed of credentials, secrets and diff quotes, and capped. stderr's summary ends with the run's `run_id=` to find it.
- **Evals, offline:** `python -m review_bot.evals replay --cases tests/evals/cases` scores the sample diff per planted bug and per source (rules, AI), a clean diff against a noise budget, and three prompt-injection diffs: a run passes only if it reports the real bug and no finding on the file the injection asks for. Rule bugs are measured for real. AI bugs are scored from a recorded model response replayed through today's pipeline, which isn't a new measurement of the model; with no recording they show `not measured`. Each case's committed live recording (`claude-opus-5-5`, 2026-11-07) is replayed by the test suite. On the committed cases it exits `1` on one documented miss, `double-tax`, which the suite expects. `--recording PATH` replays a synthetic control, and `--require-live` fails unless a fresh live recording of the default model exists. A recording made with a different case, diff or prompt isn't scored and fails the run (exit `1`), naming what changed. It exits `0` pass, `1` fail, `2` invalid input.
- **Evals with repository context, offline:** `python -m review_bot.evals replay --cases tests/evals/cases --mode context` scores context-mode recordings the same way, on a cross-file bug, a clean change, and injections that arrive through a file the model reads. Each such case has a `repo` directory, inside its case directory, that replay builds as a one-commit git repository for the tools: at most 500 files and 2,000,000 bytes, plain files and directories only. A finding's evidence is checked again against the recorded tool calls, and the report adds each run's tool calls, cost, any cap hit, and evidence kept and dropped. Live context recordings go in `recordings/live-context/recording.json`; until they exist the AI bugs show `not measured`. A recording in the wrong mode's folder exits `2`. Diff mode stays the default and is unchanged.
- **Diff mode against context mode, offline:** `python -m review_bot.evals compare --cases tests/evals/cases` replays each case's live diff and live context recordings and shows them side by side, calling no model. For each mode it shows each AI bug's runs caught, AI bugs caught, credited runs, precision, findings per run on clean cases, injection runs passed, failed runs and cap hits, mean tokens, and for context mode tool calls. It closes with a verdict: precision higher, same or lower, any bug context mode catches in a smaller share of runs, and whether every context injection run passed. Cost comes from the recordings, or is `estimated` from tokens with both `--price-input-usd-per-mtok` and `--price-output-usd-per-mtok`. A case with a `repo` or a context recording that lacks a fresh recording in either mode is listed as `unpaired` and fails the run. `--format json` writes `eval-compare/v1`. It exits `0` all paired, `1` an unpaired case, `2` invalid input.
- **Shadow report:** `python -m review_bot.pr shadow <diff> --out DIR` writes `DIR/shadow.json` (`shadow/v1`) and `DIR/shadow.md`: the summary and the inline comments the bot would post on a pull request, with nothing sent to GitHub. At most 10 comments are planned, highest severity first, and the rest are listed as withheld. A finding whose text holds a detected secret is withheld, and no secret value is written under `DIR`. It exits `0` when the report says `reviewed`, `blocked_secret` or `skipped`, findings or not; a provider failure still writes the report and exits with the provider's code (`7` to `13`); a diff the system can't read exits `4` with no report. Its audit log goes to `DIR/audit.jsonl` unless `REVIEW_BOT_AUDIT_LOG` is set.
- **Review a pull request as CI will:** `python -m review_bot.pr run --event EVENT.json --repo CLONE --out DIR` reads the `pull_request` event file GitHub Actions writes, diffs the PR's base and head commits in the clone (which needs both, e.g. `fetch-depth: 0`), and writes the same shadow report. `--mode shadow` is the default; `--mode comment` also posts and maintains the bot's comments through `GITHUB_TOKEN` (missing: exit `2`), at `GITHUB_API_URL` if set (default `https://api.github.com`). The token is never passed to the model. In comment mode, each push is reviewed only for what changed since the commit the bot last reviewed (recorded in its summary comment): a merge from the base sends nothing, a force-push or an unusable incremental diff reviews the whole PR, and re-running the job on the same head is skipped (`already_reviewed`). It needs the full history (`fetch-depth: 0`). It makes at most one model call, with no external diff or textconv drivers. It skips with exit `0`, and no model call, for a fork, an unreviewed event type, docs-only changes, or (when `CI` is set) a missing `ANTHROPIC_API_KEY`. `--max-review-bytes` (`REVIEW_BOT_MAX_REVIEW_BYTES`) caps the diff sent after skipped files are removed, and `--max-output-tokens` (`REVIEW_BOT_MAX_OUTPUT_TOKENS`) caps the model's output; both are unset by default. A bad event file or a missing commit exits `19`.
- **Shadow reviews on this repository's PRs:** every same-repo pull request gets a shadow review from the `Review` workflow, which posts nothing to the PR. The report is in the run's job summary and in an artifact (`shadow.json`, `shadow.md`, `audit.jsonl`) kept for 90 days. It runs the bot from the PR's base commit, never for forks, with one model call capped at 120,000 review bytes, 16,000 output tokens and 240 seconds. It needs the `ANTHROPIC_API_KEY` repository secret; without it every run is `skipped` (`no_credential`). The repository variable `REVIEW_BOT_MODE` set to `off` turns it off. It is never a required check.
- **Per-repository settings:** an optional `.review-bot.toml` at the repository root sets `threshold`, `exclude` (glob patterns for files to skip) and `max_open_comments` (default 10). It is read from the pull request's base commit, so a PR can't change its own review, and it can't set the mode or any cap. An invalid file fails the run with exit `2` and `invalid_config`, without calling the model. A `--threshold` flag or `REVIEW_BOT_THRESHOLD` wins over the file.
- **Domain rules:** the same file can switch on a rule from a fixed catalogue for the paths it names. Phase 3 has one, `decimal-money`: on added lines of matching `.py` files it flags a float literal (`19.99`, `.5`, `1e3`) or a call to `float`, as `logic` at `high` severity, with no model call. Each `[[domain_rules]]` entry sets `rule`, `paths` (1 to 20 glob patterns, the same rules as `exclude`) and an optional `severity` (`critical`, `high`, `medium` or `low`; default `high`); there can be up to 20 entries. A line ending inside a string can't be checked on its own, so it's skipped and counted in the audit log's `unchecked_lines`. With `--context repo`, the context prompt also names each enabled rule and its paths, so the model knows it too.
  ```toml
  [[domain_rules]]
  rule = "decimal-money"
  paths = ["billing/**"]
  ```
- **Commenting on pull requests:** the repository variable `REVIEW_BOT_MODE` picks the mode: unset or `shadow` (the default) posts nothing and only writes the shadow report; `comment` posts inline comments and one summary comment on each same-repo PR, and keeps them in step on later pushes (updating, moving or resolving its own comments, never reposting a thread a person resolved); `off` runs nothing. Only the comment job gets `pull-requests: write`. Turning comments on is the owner's call. A failed GitHub call exits `18`.
- **Context reviews in CI:** the repository variable `REVIEW_BOT_CONTEXT` set to `repo` makes the `Review` workflow's shadow and comment jobs review with repository context (`pr run --context repo`), reading the head commit's files and the issues the PR closes. Unset or `off` (the default) keeps Phase 2's diff-only review, unchanged. Any other value fails the `check-mode` job, naming it. Every cap is fixed in the workflow: 12 tool calls, 16,000 bytes per tool result and 96,000 in all, and in context mode a cost cap of $0.60 per review and a 300-second deadline (240 otherwise). Both review jobs can read issues (`issues: read`), and `GITHUB_TOKEN` reaches only the review step, never the model. Turning it on is the owner's call.
- **Code owners:** when a pull request run plans comments, it reads `CODEOWNERS` from the base commit (`.github/CODEOWNERS`, then `CODEOWNERS`, then `docs/CODEOWNERS`; at most 3 MB) with GitHub's matching rules. `shadow.json` adds a `codeowners` status and an `owners` list on each entry, and the summary comment groups findings by owner. Owners appear only inside code spans, so nobody is mentioned or notified. A missing, oversized or invalid file leaves owners empty and the review goes on.
- **Reactions become labels:** when a same-repo PR closes (and the mode isn't `off`), the `Review` workflow's collect job reads the bot's comments and stores `feedback.jsonl` as a 90-day artifact: a thumbs-up is `accept`, a thumbs-down `reject`, both `conflict`, and a thread a person resolved `resolved`. Other reactions, and any bot's, are ignored, and no username or comment text is kept. It has read-only access and never calls the model. Download the files with `gh run download --repo sleepyshark85/code-review-bot --pattern 'feedback-*' --dir feedback`; in shadow mode they are empty, since the bot posts no comments. By hand: `python -m review_bot.pr collect --event EVENT.json --out DIR` with `GITHUB_TOKEN` set.
- **Metrics:** `python -m review_bot.pr metrics --reports DIR --labels FILE ... [--since DATE --until DATE] [--price-input-usd-per-mtok X --price-output-usd-per-mtok Y] [--format json]` summarises downloaded reports and labels: outcomes, end-to-end and review latency (p50, p95), tokens, cost (only from the prices you pass; none are stored), and precision per channel, shadow (your `label` verdicts) and comment (reactions), with a Wilson 95% interval, a lower bound counting duplicates as rejections, and coverage. Duplicated reports count once, and the latest verdict per finding wins.
- **Value baseline:** `python -m review_bot.pr baseline --repo OWNER/NAME --since YYYY-MM-DD --until YYYY-MM-DD --out baseline.json` (read-only, with `GITHUB_TOKEN` or your `gh auth token`) records how the repository's PRs went in that window, by merge date: open-to-merge hours and commits after opening (median and p90), PRs whose title's first word is "fix", issues labelled `bug`, and their sum per merged PR as a rough escaped-defect proxy. No PR title is stored. Use it on the weeks before comments were turned on.
- **Judge shadow findings:** download the shadow artifacts into a folder (for example `gh run download --repo sleepyshark85/code-review-bot --pattern 'shadow-*' --dir shadow-reports`, once per run, or by run ID), then run `python -m review_bot.pr label --reports shadow-reports --labels labels.jsonl`. Like `--triage`, it needs stdin and stderr on a terminal and exits `2` otherwise; Git Bash's default mintty window isn't one, so use Windows Terminal or `winpty`. It shows each would-be comment once (the same finding across pushes of one PR counts once) and asks `[a]ccept [r]eject [s]kip`; a reject asks why (wrong, not worth it, wrong line, duplicate). Each verdict is appended at once as a `feedback/v1` line with no comment text, comments already judged are not asked again, and skipped ones come back next time. Ctrl+C or end of input stops it, keeping every verdict so far. The closing line counts this session: `judged`, `skipped` (asked again next time) and `remaining` (not reached). These labels are what the Phase 2 precision is computed from.
- **Phase 3 against Phase 2 on the same pull requests:** `python -m review_bot.pr compare --baseline DIR --candidate DIR --labels FILE [--price-input-usd-per-mtok X --price-output-usd-per-mtok Y] [--format json]` pairs each context review (`--candidate`) with the Phase 2 shadow report (`--baseline`) of the same pull request at the same head SHA, and lists the PRs it couldn't pair and why. Offline, with no model call. For each side it shows the would-be comments, how many your `label` verdicts judged, accepted and rejected (by reason), precision with a Wilson 95% interval and a lower bound counting duplicates, and recall against the pool of accepted issues either side found (an issue is a file and anchor, whatever the rule). It also shows cost (recorded, or estimated from tokens with both prices), tokens, tool calls and review time. The verdict says whether precision and recall are higher, the same or lower, or `not decidable: coverage` while either side has under 90% of its comments judged. `--format json` writes `compare/v1`. It exits `0`, or `2` for invalid input.
- **Backfill shadow reports (spends real tokens):** `gh pr list --state merged --limit 30 --json number,baseRefOid,headRefOid,mergedAt,isCrossRepository,body > pulls.json`, then `python -m review_bot.pr backfill --pulls pulls.json --github-repo OWNER/NAME --repo CLONE --out DIR --max-calls N` writes one shadow report per merged PR under `DIR/<number>/`, from a full clone. It refuses before any call if the planned calls exceed `--max-calls`, or if `CI` is set unless the run is a `workflow_dispatch` given `--allow-dispatch`, makes one attempt per PR (retries 0), skips forks, oversize and docs-only PRs, and PRs whose commits aren't in the clone (first run `git fetch origin 'refs/pull/*/head:refs/pull/*/head'`), keeps going past a failed call, retries a PR whose report says `failed` but never redoes one already done in `DIR`, and ends with counts per outcome. With `--context repo` it also needs `--max-usd` and `--max-cost-usd`: it refuses before any call unless planned calls × `--max-cost-usd` fit in `--max-usd`, and stops before a PR once the reported spend plus `--max-cost-usd` would pass it (a cost not reported counts as `--max-cost-usd`). The PRs it didn't reach get no report, so a later run picks them up, and the counts add `spent_usd=` and `stopped=budget`. Run it by hand, or through the `Live` workflow's `backfill` step.
- **Pull requests with repository context:** `run` and `backfill` take `--context off|repo` (`REVIEW_BOT_CONTEXT`, default `off`). With `repo`, the tools read the PR's head commit through git objects, never checking it out, so no filter, hook or attribute driver from the PR runs; the same is true of an incremental comment-mode review. `shadow.json` adds a `context` summary (its `commit` is the head SHA), `caps` adds the tool caps and `max_cost_usd`, and `over_budget` is also true when the reported cost passes `max_cost_usd`. The tool calls go to the audit log. With `off` the run is Phase 2's: no tools, prompt `p1-v1`, and `context` is `null`. Any other value exits `2`.
- **Linked issues (context mode):** with `--context repo`, `run` and `backfill` read the issues the PR body closes (`Fixes #12`, `closes: #7`, `resolved owner/repo#9` for the PR's own repository; the keywords close, closes, closed, fix, fixes, fixed, resolve, resolves and resolved, in any case) through `GITHUB_TOKEN`, at most 3, and send each title and body after the diff as untrusted data, so the model checks the change against what the issue asks for. A title over 256 characters or a body over 8,000 is cut. An issue that holds a possible secret, isn't found, is a pull request, or can't be read is not sent, and the review goes on. `shadow.json` lists each linked issue's `number` and `status` under `issues` (empty in diff mode), and the audit log records the bytes sent, never the text. Backfill reads each PR's body from the pulls file's `body` field. Eval cases can carry an `issues.json` that replay and live send the same way.
- **Evals, live (spends real tokens):** `python -m review_bot.evals live --cases tests/evals/cases --runs 5 --max-calls 25` reviews every case with the real model and writes each case's `recordings/live/recording.json`, which `replay` then scores offline. Both `--runs` and `--max-calls` are required, and it refuses before any call if the planned calls exceed `--max-calls`, if `CI` is set (unless the run is a `workflow_dispatch` given `--allow-dispatch`), or if a case's diff holds a secret. Retries are always 0, so each call is one attempt. It prints progress to stderr, a report with recall, noise, injection results, anchor drops, drift and tokens to stdout, and logs each call to `.review-bot/eval-live-audit.jsonl`. `--case ID` (repeatable) runs only those cases, and an unknown ID exits `2`. `--mode context` records `recordings/live-context/recording.json` for cases with a `repo`, reviewing each over its built repository with the read-only tools; it needs `--max-usd` and `--max-cost-usd`. With those amounts, in either mode, it refuses before any call (exit `2`) unless planned calls × `--max-cost-usd` fit in `--max-usd`, passes `--max-cost-usd` to Claude as each call's budget, and starts no call once the spend left is less than `--max-cost-usd` (a cost not reported counts as `--max-cost-usd`). A case cut short keeps its old recording, stderr names it, and the report ends `stopped: budget` with exit `1`. The report adds each case's tool calls per run (mean and largest), cost and cap-hit runs, and ends with `spent=$S of --max-usd $X`. Recordings hold each tool call's paths, line spans, outcome and byte count, never the text read.
- **Live workflow (spends real tokens):** `.github/workflows/live.yml` runs approved live steps where the `ANTHROPIC_API_KEY` secret is. Only a person with write access can start it, by a manual `workflow_dispatch` on `main` (`gh workflow run live.yml -f step=smoke`), and its only input is the step to run; every amount is fixed in the workflow. The `smoke` step reviews a fixed 8 KB diff of this repository twice with `--context-repo .`, each capped at `--max-cost-usd 0.30`, with retries 0, and uploads the reports, audit logs and output as a 90-day artifact. The `backfill` step reviews the labelled pull requests in `tests/evals/labelled/phase-3-pulls.json` with `--context repo`, at most 12 calls of $0.60 within $7.20, and uploads its reports the same way. `evals-diff` and `evals-context` record the nine Phase 3 eval cases live, three runs each, in diff mode (at most $0.10 per call, $2.70 in all) and context mode ($0.30, $8.10), and upload the recordings. Runs never overlap, and it is never a required check.
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

- **Pipeline:** `secret scan → parse → static → agent → verify → policy → output`, with a JSONL audit log of every stage decision.
- **Parsing:** a tolerant diff parser that survives malformed hunk headers, with docs-only and generated/lock files skipped.
- **Secret scan** on the raw diff text, before parsing: a diff containing a detected secret is **not sent** to the model.
- **Rule checks** that need no LLM: swallowed exceptions, `open()` without encoding.
- **Structured findings** (pydantic `Finding`: file, line, severity, category, rationale, confidence). Invalid model output is rejected, and hallucinated line references are dropped.
- **Interactive triage** (accept / reject / edit). Decisions are logged as labels.
- **Output:** JSON and Markdown.
- **Evals:**
  - `sample_diff.txt`: recall per planted bug and per source (rules vs. AI).
  - A clean diff: false positives.
  - A prompt-injection diff: the model must not follow instructions from the diff.
- **Seams:**
  - stable finding fingerprints (file + rule + anchor hash) give each finding an identity → Phase 2 self-updating PR comments; rule vs. AI dedup compares file, rule, proximity and severity, so a more serious model finding is never hidden;
  - the eval format → Phase 2 labelled real PRs, Phase 4 release gate;
  - triage labels → Phase 2 feedback dataset.

**Gate:** planted bugs caught; clean diff within the noise budget; injection case passes; no invalid line anchors survive; everything runs offline in CI. A manual live run of the eval cases, recorded and then replayed offline in CI, is accepted as evidence for the clauses that depend on the model. **Autonomy:** suggest (human triage).

### Phase 2: Integrated

**Goal:** runs on real PRs without creating developer noise.

- GitHub Action, then a GitHub App: inline comments with suggested fixes, plus a summary check.
- The bot updates or resolves its own comments (keyed by fingerprint) and re-reviews only new commits.
- Shadow mode first: a report of what it *would* have posted.
- Developer reactions and resolutions become accept/reject labels; per-repo config; metrics for latency, tokens, cost, and acceptance.

**Gate:** weeks of shadow mode with precision measured; value baseline captured; named owner in place. **Autonomy:** observe → comment.

### Phase 3: Context-aware

**Goal:** fewer guesses, deeper findings.

- Read-only tools of our own (`read`, `grep`, `glob`) over the pull request's head commit, served to the Agent SDK, so findings can span files ("callers of `total()` already add tax") ([ADR 0013](docs/adr/0013-read-only-repo-tools-over-a-commit.md)).
- CODEOWNERS routing, ticket acceptance criteria, and RightShip domain rules (e.g. `Decimal` money in `billing/`).
- The tools record the context behind every finding; SDK hooks only enforce the tool allowlist ([ADR 0014](docs/adr/0014-tools-as-a-provider-neutral-executor.md)).

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

- **Owner:** [@sleepyshark85](https://github.com/sleepyshark85), who funds the model key.
- **Spend:** capped by a dedicated Anthropic workspace limit; the key is the `ANTHROPIC_API_KEY` repository secret.
- **Support:** open an issue labelled `review-bot`.
- **Runbook:** [docs/runbook.md](docs/runbook.md) covers turning the bot off, rotating the key, checking spend, and reading a failed run.

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
