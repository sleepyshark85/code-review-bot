# Phase 0 release notes

The inherited script couldn't run on a current OpenAI SDK. It now gets a real review from Claude with no tools, validates the result, and gives every failure a clear message and its own exit code.

## What you can run

```
uv sync
uv run python -m review_bot <diff>                    # a real review with Claude
uv run python -m review_bot <diff> --provider mock    # offline; canned output, not a real review
```

## What changed from the baseline

| | Baseline | Now |
|---|---|---|
| **Model** | `openai.ChatCompletion`, removed in `openai>=1.0` | Claude through a pinned Agent SDK |
| **Credentials** | Key variable hardcoded in source | Your Claude Code login or `ANTHROPIC_API_KEY` |
| **Model access** | Not applicable | No tools, settings, MCP servers, hooks or subagents; one turn |
| **Output** | Free text, printed as received | Validated findings, one per line; invalid output is rejected whole |
| **The diff** | Pasted straight into the prompt | Hash-delimited and declared untrusted |
| **Input** | No checks | Missing, unreadable, empty, non-UTF-8 and oversized diffs are rejected |
| **Failures** | Python tracebacks | One escaped error line and a distinct exit code |
| **Limits** | None | An overall deadline and bounded retries |
| **Tests** | None | A test suite in CI, plus linting, type checks, a dependency audit and a secret scan |
| **Command** | `python review_bot.py <diff>` | `python -m review_bot <diff>` |

## Exit codes

The baseline defined none. See the [settings](design.md#settings) for the new flags.

| Code | Meaning | Code | Meaning |
|---|---|---|---|
| 0 | No findings at or above the threshold | 8 | Authentication failed |
| 1 | Findings at or above the threshold | 9 | Rate limited |
| 2 | Usage or configuration error | 10 | Model refused |
| 3 | Diff not found | 11 | Temporary failure, or deadline passed |
| 4 | Diff unreadable | 12 | Permanent API failure |
| 5 | Diff empty | 13 | Invalid model output |
| 6 | Diff too large | 14 | Internal error |
| 7 | Provider unavailable | | |
