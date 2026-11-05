# Phase 0 release notes

Changes from the baseline, `Assignment/review_bot.py`.

## Changed

- **Model:** Claude through `claude-agent-sdk`, replacing the removed `openai.ChatCompletion` call.
- **Credentials:** your Claude Code login or `ANTHROPIC_API_KEY`, replacing the key in source.
- **Command:** `python -m review_bot <diff>`, replacing `python review_bot.py <diff>`.
- **Output:** one validated finding per line, replacing free text. Invalid model output is rejected.
- **Exit codes:** `1` now means findings were reported. Each failure has its own code, up to `14` ([table](design.md#exit-codes)).
- **Errors:** one safe error line, replacing Python tracebacks.
- **Prompt:** the diff is delimited and declared untrusted.

## Added

- **Lockdown:** the model runs with no tools, MCP servers, hooks or subagents.
- **Input checks:** missing, unreadable, empty, non-UTF-8 and oversized diffs are rejected.
- **Limits:** an overall deadline and bounded retries.
- **Settings:** `--provider`, `--model`, `--threshold`, `--max-diff-bytes`, `--max-retries`, `--api-timeout-ms` and `--deadline-s`, each with an environment variable ([settings](design.md#settings)).
- **Offline mode:** `--provider mock` returns canned output.
- **Tests and CI:** a test suite, plus linting, type checks, a dependency audit and a secret scan.

## Removed

- The OpenAI dependency.
