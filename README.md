# Review Bot

An automated PR reviewer for RightShip, and the first agent on a shared agent platform.

**Status** (last updated 2026-11-16; owner [@sleepyshark85](https://github.com/sleepyshark85)):

| Phase | State | Evidence |
|---|---|---|
| 0 Stabilise | Done (0.1.0) | [test report](docs/phases/phase-0/test-report.md) |
| 1 Trustworthy core | Done (0.2.0), with one accepted eval miss (`double-tax`) | [test report](docs/phases/phase-1/test-report.md#p1-s17-measure-the-real-model-and-hold-ci-to-the-result) |
| 2 Integrated | Done (0.3.0). Shadow precision on real PRs was 42/42 (Wilson 95% 91.6–100%). The owner waived the 3-week window. The bot now comments on PRs | [test report](docs/phases/phase-2/test-report.md#p2-s12-close-phase-2) |
| 3 Context-aware | Done (0.4.0), as an owner-accepted exception. The recall clause isn't met, and the labelled-PR comparison wasn't run. Eval precision (33.8% diff, 49.1% context) counts valid findings the answer key lacks as wrong, so it understates the bot | [test report](docs/phases/phase-3/test-report.md#p3-s15-close-phase-3), [eval plan §1.3](docs/eval-plan.md#13-precision-on-eval-cases) |
| 4 Governed | In progress: 9 of 16 slices merged, all off by default (policy file, second provider, tool policy, redaction, gate decisions, model fallback, rules-only reviews, eval gate in CI). Next is the required merge check (S10). Closing needs the owner's security sign-off and a quarter of SLO data | [slices](docs/phases/phase-4/design.md) |
| 5 Catalogue | Not started. Evals are planned for it | [eval plan](docs/eval-plan.md) |

## Usage

Install the [latest release](https://github.com/sleepyshark85/code-review-bot/releases/latest), which requires Python 3.12 or later:

```
uv tool install https://github.com/sleepyshark85/code-review-bot/releases/download/v0.4.0/review_bot-0.4.0-py3-none-any.whl
review-bot <diff>                    # a real review with Claude
review-bot <diff> --provider mock    # offline smoke test: no model call; its canned finding is dropped (not in the diff), so it exits 0
```

Authentication is your Claude Code login, or `ANTHROPIC_API_KEY` if set. The diff must be UTF-8 (`git diff --output=change.diff`).

| Command | Does |
|---|---|
| `review-bot DIFF [--format text\|json\|markdown] [--triage] [--context-repo DIR]` | Reviews a diff: built-in rules, then the model. Exits `0` clean, `1` findings, `2` or more on an error |

From a clone (`uv sync`, then prefix each with `uv run`), which may include unreleased commands:

| Command | Does |
|---|---|
| `python -m review_bot <diff>` | The same as `review-bot` |
| `python -m review_bot DIFF --context-repo DIR` | Lets the model read the repository through three read-only tools |
| `python -m review_bot.pr shadow\|run` | Writes the review a pull request would get, or posts it (`--mode comment`) |
| `python -m review_bot.pr label\|collect\|metrics\|compare\|baseline` | Judges findings, gathers reactions, and measures precision, cost and value |
| `python -m review_bot.evals replay\|compare\|gate` | Scores recorded model runs offline; `gate` fails CI when a prompt or an allowed model has no fresh, passing evals |
| `python -m review_bot.evals live`, `python -m review_bot.pr backfill` | Live runs that spend real tokens, each capped; CI runs them through the `Live` workflow |

A diff holding a possible secret is never sent (exit `16`). The diff and tool results are treated as untrusted data. Every run is traced by default in `.review-bot/audit.jsonl`, which never holds diff lines or secret values. Every same-repo PR on this repository gets a shadow review that posts nothing, unless the `REVIEW_BOT_MODE` variable says otherwise.

**Full reference:** [docs/usage.md](docs/usage.md) covers every command, its key options and exit codes, and links the specs for detail. It describes `main`, which may be ahead of the latest release.

## Plan

**A review bot that developers trust, auditors can trace, and the team can operate.** It replaces `Assignment/review_bot.py`, a stopgap that doesn't run and has no tests or owner. Each phase starts only once the previous gate is met by evidence:

| Phase | Goal | Gate | Autonomy |
|---|---|---|---|
| [0 Stabilise](docs/phases/phase-0/design.md) | Works and fails safely | No OpenAI, no secrets in source, clear failures, offline tests | suggest |
| [1 Trustworthy core](docs/phases/phase-1/design.md) | Findings that can be trusted, triaged and measured | Planted bugs caught, noise budget, injection eval, valid anchors | suggest |
| [2 Integrated](docs/phases/phase-2/design.md) | Real PRs without noise | Weeks of shadow mode with precision measured, baseline, owner | observe → comment |
| [3 Context-aware](docs/phases/phase-3/design.md) | Fewer guesses, deeper findings | Precision and recall beat Phase 2; cost within budget | comment |
| [4 Governed](docs/phases/phase-4/design.md) | Safe at organisation scale | Security sign-off; SLOs met for a quarter | gate |
| 5 Catalogue | More agents on one platform | Own evals and acceptance target per agent | act |

**Full plan:** [docs/roadmap.md](docs/roadmap.md): the inherited tool, the north star and targets, who owns what, each phase's scope and seams, and the risks.

## Ownership

- **Owner:** [@sleepyshark85](https://github.com/sleepyshark85), who funds the model key.
- **Support:** open an issue labelled `review-bot`.
- **Runbook:** [docs/runbook.md](docs/runbook.md): turning the bot off, rotating the key, checking spend, reading a failed run.
