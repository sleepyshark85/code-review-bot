# Review Bot

An automated PR reviewer for RightShip, and the first agent on a shared agent platform.

> **Status:** Phase 1 complete, with one documented eval miss ([test report](docs/phases/phase-1/test-report.md#p1-s17-measure-the-real-model-and-hold-ci-to-the-result)) · Phase 2 released as 0.3.0 and running in shadow mode; its gate evidence is pending ([#67](https://github.com/sleepyshark85/code-review-bot/issues/67)) · Phase 3 built; it closes once that evidence is in · **Owner:** [@sleepyshark85](https://github.com/sleepyshark85) · **Last updated:** 2026-11-11

## Usage

Install the [latest release](https://github.com/sleepyshark85/code-review-bot/releases/latest), which requires Python 3.12 or later:

```
uv tool install https://github.com/sleepyshark85/code-review-bot/releases/download/v0.3.0/review_bot-0.3.0-py3-none-any.whl
review-bot <diff>                    # a real review with Claude
review-bot <diff> --provider mock    # offline smoke test: no model call; its canned finding is dropped (not in the diff), so it exits 0
```

Authentication is your Claude Code login, or `ANTHROPIC_API_KEY` if set. The diff must be UTF-8 (`git diff --output=change.diff`).

| Command | Does |
|---|---|
| `review-bot DIFF [--format text\|json\|markdown] [--triage]` | Reviews a diff: built-in rules, then the model. Exits `0` clean, `1` findings, `2` or more on an error |

From a clone (`uv sync`, then prefix each with `uv run`), which includes unreleased Phase 3 commands:

| Command | Does |
|---|---|
| `python -m review_bot <diff>` | The same as `review-bot` |
| `python -m review_bot DIFF --context-repo DIR` | Lets the model read the repository through three read-only tools |
| `python -m review_bot.pr shadow\|run` | Writes the review a pull request would get, or posts it (`--mode comment`) |
| `python -m review_bot.pr label\|collect\|metrics\|compare\|baseline` | Judges findings, gathers reactions, and measures precision, cost and value |
| `python -m review_bot.evals replay\|compare` | Scores recorded model runs offline |
| `python -m review_bot.evals live`, `python -m review_bot.pr backfill` | Live runs that spend real tokens, each capped; CI runs them through the `Live` workflow |

A diff holding a possible secret is never sent (exit `16`). The diff and tool results are treated as untrusted data. Every run is traced by default in `.review-bot/audit.jsonl`, which never holds diff lines or secret values. Every same-repo PR on this repository gets a shadow review that posts nothing, unless the `REVIEW_BOT_MODE` variable says otherwise.

**Full reference:** [docs/usage.md](docs/usage.md) covers every option, exit code, setting and workflow. It describes `main`, which may be ahead of the latest release.

## Plan

**A review bot that developers trust, auditors can trace, and the team can operate.** It replaces `Assignment/review_bot.py`, a stopgap that doesn't run and has no tests or owner. Each phase starts only once the previous gate is met by evidence:

| Phase | Goal | Gate | Autonomy |
|---|---|---|---|
| [0 Stabilise](docs/phases/phase-0/design.md) | Works and fails safely | No OpenAI, no secrets in source, clear failures, offline tests | suggest |
| [1 Trustworthy core](docs/phases/phase-1/design.md) | Findings that can be trusted, triaged and measured | Planted bugs caught, noise budget, injection eval, valid anchors | suggest |
| [2 Integrated](docs/phases/phase-2/design.md) | Real PRs without noise | Weeks of shadow mode with precision measured, baseline, owner | observe → comment |
| [3 Context-aware](docs/phases/phase-3/design.md) | Fewer guesses, deeper findings | Precision and recall beat Phase 2; cost within budget | comment |
| 4 Governed | Safe at organisation scale | Security sign-off; SLOs met for a quarter | gate |
| 5 Catalogue | More agents on one platform | Own evals and acceptance target per agent | act |

**Full plan:** [docs/roadmap.md](docs/roadmap.md): the inherited tool, the north star and targets, who owns what, each phase's scope and seams, and the risks.

## Ownership

- **Owner:** [@sleepyshark85](https://github.com/sleepyshark85), who funds the model key.
- **Support:** open an issue labelled `review-bot`.
- **Runbook:** [docs/runbook.md](docs/runbook.md): turning the bot off, rotating the key, checking spend, reading a failed run.
