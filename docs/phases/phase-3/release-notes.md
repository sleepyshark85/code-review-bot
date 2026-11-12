# Phase 3 release notes (0.4.0)

The bot can now read the repository, not only the diff: it follows a change into its callers and definitions through read-only tools, cites the lines it relied on, checks a change against the issues it closes, routes findings to code owners, applies per-repository domain rules, and suggests fixes.

## What you can run

```
review-bot DIFF --context-repo DIR [--context-rev REV]                  # review with read-only repository tools
python -m review_bot.pr run --event EVENT.json --repo CLONE --out DIR --context repo
python -m review_bot.evals replay --cases tests/evals/cases --mode context
python -m review_bot.evals compare --cases tests/evals/cases              # diff mode against context mode, offline
python -m review_bot.pr compare --baseline DIR --candidate DIR --labels FILE
gh workflow run live.yml -f step=smoke                                    # approved live steps, by manual dispatch only
```

## Added

- **Repository context:** the tools `read`, `grep` and `glob` read git objects of one commit. They never check anything out, refuse secrets files, symlinks, `.git` and lines that may hold a secret, and are capped per review. Claude's built-in tools stay off, and only these three are allowed.
- **Evidence:** each finding cites the lines it relied on. A span is kept only if the diff or a tool call showed those lines.
- **Linked issues:** with `--context repo`, up to 3 issues the PR closes are sent as untrusted data, and the review checks the change against them.
- **Code owners:** findings are grouped by `CODEOWNERS` owner in the summary, without mentioning anyone.
- **Domain rules:** `.review-bot.toml` can switch on `decimal-money` (float money in `.py` files) for the paths it names.
- **Suggested fixes:** a finding may carry a replacement for its line, shown as a one-click GitHub suggestion. It is never applied automatically.
- **Context in CI:** the repository variable `REVIEW_BOT_CONTEXT=repo` turns on context reviews, with fixed caps ($0.60 and 300 s per review).
- **Cost cap:** `--max-cost-usd` caps one review's cost in either mode.
- **Eval tooling:** context-mode eval cases with their own repositories, live context recordings within a dollar cap, and `evals compare` and `pr compare` for side-by-side measurement.
- **Live workflow** (`.github/workflows/live.yml`): approved live steps with fixed amounts, by manual dispatch on `main` only.
- **This repository's own PRs:** test fixtures no longer trip the secret block, and eval recordings are excluded from review.

## Also in this build, from Phase 4 (off by default)

- **Policy file** (`--policy`): allowed tools and a per-call cost ceiling, recorded in every report. `evals live` and `pr backfill` now refuse without `--max-usd` and a per-call cap.
- **Second provider** (`--provider openai --model MODEL`): any OpenAI-compatible endpoint, in diff mode.
- **Redaction** (`[redaction] kinds`): email addresses, phone numbers and IPv4 addresses are masked before any vendor call.

## Changed

- The severity-threshold module is now `review_bot.threshold`, with no change in behaviour.
- `report/v1` gains `mode`, `context`, `policy` and `redaction`; `shadow/v1` gains `context`, `issues`, `codeowners` and `policy`.

## Gate

Phase 3 closes as an accepted exception. On the 9 eval cases, context mode raised precision from 33.8% to 49.1% and newly caught `double-tax` and the ticket criteria. But it caught three bugs in a smaller share of runs: `tax-discount-ordering` and `float-money` (0/3, misses the owner accepted) and `discount-dropped-on-export` (2/3, against 3/3). So the recall clause isn't met. The comparison on the labelled pull requests wasn't run, by the owner's decision. Live spend: $1.02 of $20. See the [test report](test-report.md#p3-s15-close-phase-3).
