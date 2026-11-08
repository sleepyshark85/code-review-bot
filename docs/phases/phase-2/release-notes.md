# Phase 2 release notes (0.3.0)

The bot now runs on pull requests: in shadow mode by default, or posting and maintaining its own comments. Feedback is collected as labels, and its precision, cost and value can be measured.

## What you can run

```
python -m review_bot.pr shadow <diff> --out DIR                       # what it would post, locally
python -m review_bot.pr run --event EVENT.json --repo CLONE --out DIR [--mode comment]
python -m review_bot.pr label --reports DIR --labels labels.jsonl     # judge shadow findings
python -m review_bot.pr collect --event EVENT.json --out DIR          # reactions and resolutions as labels (run by CI, needs GITHUB_TOKEN)
python -m review_bot.pr backfill --pulls pulls.json --github-repo OWNER/NAME --repo CLONE --out DIR --max-calls N
python -m review_bot.pr baseline --repo OWNER/NAME --since DATE --until DATE --out baseline.json
python -m review_bot.pr metrics --reports DIR --labels FILE [--price-input-usd-per-mtok X --price-output-usd-per-mtok Y]
```

## Added

- **Shadow reports (`shadow/v1`):** the summary and up to 10 inline comments the bot would post, with the rest withheld, and no secret value written.
- **GitHub Actions workflow** (`.github/workflows/review.yml`): reviews every same-repo pull request from its base commit and uploads the shadow report. It never blocks a merge, skips forks (and drafts in comment mode), and skips with `no_credential` when the key is missing.
- **Comment mode** (`REVIEW_BOT_MODE=comment`): inline comments plus one summary comment. On later pushes the bot updates, moves or resolves its own comments by fingerprint and never reposts a thread a person resolved.
- **Incremental review:** in comment mode, each push is reviewed only for what changed since the last reviewed commit. A merge from the base sends nothing, and a force-push reviews the whole PR.
- **Per-repository settings** in `.review-bot.toml` (`threshold`, `exclude`, `max_open_comments`), read from the base commit.
- **Feedback as labels (`feedback/v1`):** `label` for shadow findings; when a PR closes, reactions and resolutions on the bot's comments are collected.
- **Backfill:** shadow reports for past merged PRs, capped by `--max-calls` and refused in CI.
- **Value baseline (`baseline/v1`):** open-to-merge time, commits after opening, and fix PRs and bug issues per merged PR.
- **Metrics (`metrics/v1`):** outcomes, latency, tokens, cost at the prices you pass, and precision per channel with a Wilson 95% interval.
- **Owner, support label and [runbook](../../runbook.md).**

## Exit codes added

| Code | Meaning |
|---|---|
| `18` | A GitHub API call failed |
| `19` | A bad event file, or a commit missing from the clone |

## Limitations

- Comments carry no suggested fixes, and there is no GitHub App.
