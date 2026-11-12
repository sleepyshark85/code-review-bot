# Usage reference

Every command on `main` ([short version](../README.md#usage)). `--help` lists every option; linked slice specs hold limits, edge cases and schemas. **(live)** spends real tokens.

## Reviewing a diff

`review-bot DIFF` reviews a UTF-8 unified diff (Windows: `git diff --output=change.diff`). Auth: Claude Code login, or `ANTHROPIC_API_KEY` if set.

- Options: `--format text|json|markdown` (`REVIEW_BOT_FORMAT`), `--threshold` (`REVIEW_BOT_THRESHOLD`), `--audit-log PATH` (`REVIEW_BOT_AUDIT_LOG`, default `.review-bot/audit.jsonl`), and `--triage` to accept, reject or edit each finding at a terminal (Windows: use Windows Terminal or `winpty`, not Git Bash's mintty).
- In a git diff, docs, lock, generated and binary files are skipped. Built-in rules flag swallowed exceptions and `open()` without an encoding.
- **Secrets block:** a diff with a detected secret is never sent (exit `16`); the value is never printed.
- The model has no tools without `--context-repo`.
- JSON is `report/v1`; Markdown can't form links, mentions or references.

Specs: [parsing](phases/phase-1/slices/p1-s2-parse-and-skip.md), [secrets](phases/phase-1/slices/p1-s3-secret-block.md), [rules](phases/phase-1/slices/p1-s7-swallowed-exception-rule.md), [dedupe](phases/phase-1/slices/p1-s9-dedupe.md), [JSON](phases/phase-1/slices/p1-s10-json-output.md), [Markdown](phases/phase-1/slices/p1-s11-markdown-output.md), [triage](phases/phase-1/slices/p1-s12-triage.md), [edit](phases/phase-1/slices/p1-s13-triage-edit.md).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | No findings at or above threshold |
| `1` | Findings (with `--triage`: any not rejected) |
| `2`–`6` | Usage or input errors |
| `7`–`13` | Provider failures (`8` authentication, `12` permanent, e.g. a cap reached) |
| `14` | Internal error |
| `15` | Input isn't a diff |
| `16` | Possible secret in diff |
| `17` | Audit log unwritable |
| `18` | GitHub call failed |
| `19` | Git failure, bad event file or missing commit |

`python -m review_bot.pr` exits `0` when the report's outcome is `reviewed`, `blocked_secret` or `skipped`, findings or not. `evals` exits `0` pass, `1` fail, `2` invalid input. Full tables: [Phase 0](phases/phase-0/design.md#exit-codes), [Phase 1](phases/phase-1/design.md#exit-codes), [Phase 2](phases/phase-2/design.md#commands-and-exit-codes), [settings](phases/phase-0/design.md#settings).

## Repository context

`review-bot DIFF --context-repo DIR [--context-rev REV]` gives the model three **read-only** tools (`read`, `grep`, `glob`) on the repository at a commit (default `HEAD`).

Caps (defaults): `--max-tool-calls` 12, `--max-tool-result-bytes` 16000, `--max-tool-total-bytes` 96000.

- Tools read git objects only and refuse `.git`, secrets files, symlinks, binaries and lines that may hold a secret. Results are untrusted data.
- Findings cite verified `evidence` spans and may carry a `suggestion` replacing the anchored line, never applied automatically.

Specs: [tools](phases/phase-3/slices/p3-s1-local-review-with-repo-tools.md), [confinement](phases/phase-3/slices/p3-s2-tool-confinement.md), [Claude](phases/phase-3/slices/p3-s3-claude-drives-tools.md), [evidence](phases/phase-3/slices/p3-s4-finding-evidence.md), [suggestions](phases/phase-3/slices/p3-s12-suggested-fixes.md).

## Policy and cost

- `--max-cost-usd USD` (`REVIEW_BOT_MAX_COST_USD`) caps one review's cost; reaching it exits `12`.
- `--policy PATH` (`REVIEW_BOT_POLICY`) applies a versioned TOML policy (allowed tools, cost ceiling) to `review-bot`, `pr shadow|run|backfill` and `evals live|replay`. Default `builtin-v1`. Reports record its version and hash. In a Claude context review, the SDK's hook and permission callback enforce the same tool list, and each denial is logged as `agent`/`denied`. A `[redaction] kinds` list (`email`, `phone`, `ipv4`) masks those values as `[REDACTED:<kind>]` in the diff, tool results and linked issues before any vendor call; documentation addresses are exempt, and reports count what was masked, never the values.

Specs: [policy](phases/phase-4/slices/p4-s1-policy-file.md), [Claude enforcement](phases/phase-4/slices/p4-s3-claude-enforces-tool-policy.md), [redaction](phases/phase-4/slices/p4-s4-redaction.md), [cost cap](phases/phase-3/slices/p3-s3-claude-drives-tools.md).

## Second provider

`--provider openai --model MODEL` reviews through any OpenAI-compatible endpoint, diff mode only. `--model` (or `REVIEW_BOT_MODEL`) is required; `--provider` also reads `REVIEW_BOT_PROVIDER`. `--openai-base-url` (`REVIEW_BOT_OPENAI_BASE_URL`) sets the endpoint, default `https://api.openai.com/v1`. The key, `OPENAI_API_KEY`, is removed from the environment at start-up and never written anywhere. Cost caps aren't supported yet (exit `2`). Spec: [p4-s2](phases/phase-4/slices/p4-s2-openai-compatible-provider.md).

## Audit log

Every run is traced in JSONL (`run_id=` on stderr): stage decisions, never diff lines, model text or secret values. Provider diagnostics are scrubbed. `pr` commands log to `DIR/audit.jsonl` unless `REVIEW_BOT_AUDIT_LOG` is set. Specs: [audit](phases/phase-1/slices/p1-s4-audit-log.md), [notices](phases/phase-1/slices/p1-s5-provider-notices.md).

## Evals

```
python -m review_bot.evals replay --cases tests/evals/cases [--mode context]
python -m review_bot.evals compare --cases tests/evals/cases
python -m review_bot.evals live --cases tests/evals/cases --runs 5 --max-calls 25 --max-usd X --max-cost-usd Y
```

`replay` scores recordings offline (planted bugs, noise, prompt injections). `compare` sets diff against context mode, offline. `live` **(live)** records fresh responses.

`live` refuses before any call if the plan exceeds `--max-calls` or `--max-usd`, a per-call cap is missing, `CI` is set (unless `--allow-dispatch` in a `workflow_dispatch`), or a diff holds a secret. Recordings never hold text the tools read.

Specs: [replay](phases/phase-1/slices/p1-s14-eval-sample.md), [injection](phases/phase-1/slices/p1-s15-eval-clean-injection.md), [live](phases/phase-1/slices/p1-s16-eval-live-command.md), [context cases](phases/phase-3/slices/p3-s5-context-eval-cases.md), [live context](phases/phase-3/slices/p3-s6-live-context-evals.md), [compare](phases/phase-3/slices/p3-s7-compare-eval-modes.md).

## Pull requests

```
python -m review_bot.pr shadow DIFF --out DIR
python -m review_bot.pr run --event EVENT.json --repo CLONE --out DIR
```

`shadow` writes would-be comments to `shadow.json` and `shadow.md`; nothing goes to GitHub. `run` reviews a PR as CI does (clone with `fetch-depth: 0`).

`pr run` options: `--mode shadow|comment` (default `shadow`). `pr run` and `backfill`: `--context off|repo` (`REVIEW_BOT_CONTEXT`, default `off`), `--max-review-bytes`, `--max-output-tokens`.

- One model call at most. Forks, docs-only changes and, in CI, a missing `ANTHROPIC_API_KEY` skip (exit `0`).
- `comment` mode posts comments through `GITHUB_TOKEN`, reviewing only new commits; the token never reaches the model.
- `--context repo` reads the PR head as git objects (nothing from the PR runs) and sends up to 3 closed issues as untrusted data.
- `CODEOWNERS` groups findings by owner without mentioning anyone. No secret value reaches the report.

Specs: [shadow](phases/phase-2/slices/p2-s1-shadow-report-local.md), [run](phases/phase-2/slices/p2-s2-review-pr-event.md), [comments](phases/phase-2/slices/p2-s5-comment-sync.md), [new commits](phases/phase-2/slices/p2-s7-review-new-commits.md), [context](phases/phase-3/slices/p3-s8-pr-review-with-context.md), [issues](phases/phase-3/slices/p3-s9-linked-issue-criteria.md), [owners](phases/phase-3/slices/p3-s11-codeowners-routing.md).

### Per-repository settings

`.review-bot.toml`, read from the PR's base commit, sets `threshold`, `exclude`, `max_open_comments` (default 10) and domain rules:

```toml
[[domain_rules]]
rule = "decimal-money"
paths = ["billing/**"]
```

It can't set the mode or any cap. A `gate = [<class>, ...]` key opts the repository into merge gating for classes the policy approves (`[[gate.classes]]`): shadow reports then say which findings would block, and comment-mode summaries say which block and how to appeal or override. Nothing enforces it until the `merge gate` check exists. Specs: [config](phases/phase-2/slices/p2-s9-repo-config.md), [domain rules](phases/phase-3/slices/p3-s10-domain-rules.md), [gate](phases/phase-4/slices/p4-s5-gate-decision.md).

## The `Review` workflow

Shadow-reviews same-repo PRs (never forks) with fixed caps and the `ANTHROPIC_API_KEY` secret. Never a required check.

Repository variables: `REVIEW_BOT_MODE` = `shadow` (default), `comment` or `off`; `REVIEW_BOT_CONTEXT` = `off` (default) or `repo`.

Reports: job summary and a 90-day artifact. Enabling comments or context is the owner's call. Specs: [workflow](phases/phase-2/slices/p2-s3-shadow-workflow.md), [comments on](phases/phase-2/slices/p2-s6-comments-on.md), [context](phases/phase-3/slices/p3-s14-context-in-ci.md).

## Feedback and metrics

```
python -m review_bot.pr collect --event EVENT.json --out DIR
python -m review_bot.pr label --reports DIR --labels labels.jsonl
python -m review_bot.pr metrics --reports DIR --labels FILE
python -m review_bot.pr baseline --repo OWNER/NAME --since DATE --until DATE --out baseline.json
python -m review_bot.pr compare --baseline DIR --candidate DIR --labels FILE
```

`collect` turns reactions on closed PRs into labels (the workflow runs it); `label` judges shadow findings at a terminal (as `--triage`); `metrics` reports outcomes, latency, tokens, cost and precision; `baseline` records pre-bot PR history; `compare` sets context against diff-only reviews.

No username, comment text or PR title is stored. Download: `gh run download --repo sleepyshark85/code-review-bot --pattern 'shadow-*'` (or `'feedback-*'`). Specs: [label](phases/phase-2/slices/p2-s4-label-shadow-findings.md), [reactions](phases/phase-2/slices/p2-s8-reactions-as-labels.md), [metrics](phases/phase-2/slices/p2-s10-metrics.md), [baseline](phases/phase-2/slices/p2-s11-value-baseline.md), [compare](phases/phase-3/slices/p3-s13-compare-labelled-prs.md).

## Backfill

**(live)** Shadow-reviews merged PRs:

```
gh pr list --state merged --limit 30 --json number,baseRefOid,headRefOid,mergedAt,isCrossRepository,body > pulls.json
python -m review_bot.pr backfill --pulls pulls.json --github-repo OWNER/NAME --repo CLONE --out DIR --max-calls N --max-usd X --max-cost-usd Y
```

Refuses on the same budget and `CI` rules as `evals live`; stops at the budget; resumes later. Spec: [p2-s13](phases/phase-2/slices/p2-s13-shadow-backfill.md).

## Live workflow

**(live)** `.github/workflows/live.yml` runs the `smoke` (a fixed 8 KB diff), `backfill`, `evals-diff` and `evals-context` steps with fixed dollar caps. Only users with write access start it: `gh workflow run live.yml -f step=smoke`. Spec: [p3-s16](phases/phase-3/slices/p3-s16-live-steps-workflow.md).
