# Phase 1 release notes

Reviews can now be trusted, triaged and measured. Rules catch two bug classes with no model, secrets never leave the machine, made-up line references are dropped, and every run is traced. A developer accepts, rejects or edits each finding, and an offline eval scores the bot against a recorded live run.

## What you can run

```
review-bot <diff>                         # text, as before
review-bot <diff> --format json           # or --format markdown
review-bot <diff> --triage                # accept, reject or edit each finding (needs a terminal)
python -m review_bot.evals replay --cases tests/evals/cases --require-live      # offline
python -m review_bot.evals live --cases tests/evals/cases --runs 5 --max-calls 25   # spends real tokens
```

## What changed from Phase 0

| | Phase 0 | Now |
|---|---|---|
| **Input** | Any text sent to the model | Only unified diffs; docs, lock, generated and binary files in a git diff are skipped |
| **Secrets** | Sent with the diff | The raw text and decoded paths are scanned first; a hit is never sent and exits `16` |
| **Rules** | None | Swallowed exceptions and `open()` without an encoding, found with no model |
| **Line references** | Trusted | A finding whose file or line isn't in the diff is dropped |
| **Duplicates** | Possible | A model finding repeating a rule's is reported once, unless it's more severe |
| **Identity** | None | A stable fingerprint per finding |
| **Output** | Text | Text, JSON (`report/v1`) or Markdown |
| **Triage** | None | `--triage`: accept, reject or edit each finding; each decision is logged as a label |
| **Audit** | None | JSONL log of every stage decision, with no diff text or secrets (`.review-bot/audit.jsonl`) |
| **Provider diagnostics** | Discarded | Warnings and a scrubbed tail of a failed call's stderr reach the audit log |
| **Evals** | None | Planted-bug, clean and prompt-injection cases; `replay` offline in CI, `live` by hand |
| **SDK shutdown** | Could print an asyncio traceback | stderr stays one line |

## Exit codes added

| Code | Meaning |
|---|---|
| `15` | The input isn't a diff |
| `16` | A possible secret is in the diff, so nothing was sent |
| `17` | The audit log can't be written |

With `--triage`, the run exits `1` if any finding wasn't rejected, and `2` without a terminal. The eval command has its own codes: `0` pass, `1` fail, `2` invalid input.
