# Phase 0 release notes: Stabilise

What changed from the baseline, [`Assignment/review_bot.py`](../../../Assignment/review_bot.py). `Assignment/` itself is untouched and stays as the inherited reference.

## In one line

The baseline couldn't run on a current OpenAI SDK and failed with tracebacks. The bot now gets a real review from Claude with no tools, validates what comes back, and gives every failure a clear message and its own exit code.

## Before and after

| | Baseline | Now |
|---|---|---|
| **Model** | `openai.ChatCompletion` with `gpt-4`, which was removed in `openai>=1.0`, so it doesn't run on a current SDK | Claude through `claude-agent-sdk==0.2.178`, default model `claude-opus-5-5` |
| **Credentials** | A key variable hardcoded in source (`sk-REPLACE-ME`) | Your Claude Code login, or `ANTHROPIC_API_KEY`. The bot never reads or stores the key |
| **Model access** | Not applicable | Locked down: no tools, settings, MCP servers, hooks or subagents, one turn, and our own system prompt |
| **Command** | `python review_bot.py sample_diff.txt` | `python -m review_bot <diff>`, or `review-bot <diff>` |
| **Output** | Free text printed to stdout | One validated finding per line: file, line, severity, category, rationale, confidence |
| **Model output** | Printed as received | Validated against a schema. Invalid output is rejected whole, exits 13, and is never repaired |
| **The diff** | Pasted straight into the prompt | Wrapped in delimiters derived from its hash, and declared untrusted in the system prompt |
| **Input checks** | None: reads `sys.argv[1]` | Missing, unreadable, empty, non-UTF-8 and oversized diffs each get a message and an exit code. Oversized diffs are rejected, never truncated |
| **Failures** | Python tracebacks | One escaped `review-bot: error:` line, with no traceback and no model or CLI text, apart from the SDK's own connection guidance |
| **Exit codes** | None defined | 15 codes, listed below |
| **Time limits** | None | An overall deadline (default 300 s), with the CLI as the only retry layer. The deadline isn't a hard bound: see Known limits |
| **Offline use** | None | `--provider mock` runs with no CLI, account or network. Its output is canned, not a real review |
| **Tests and CI** | None | 957 tests at 100% coverage in CI on Linux, with ruff, mypy strict, pre-commit, `pip-audit` and a whole-tree secret scan |
| **Code shape** | One 43-line script | A package split into load, prompt, provider and render, behind an `LLMProvider` interface with a shared conformance suite |

## Exit codes

| Code | Meaning | Code | Meaning |
|---|---|---|---|
| 0 | No findings at or above the threshold | 8 | Authentication failed |
| 1 | Findings at or above the threshold | 9 | Rate limited |
| 2 | Usage or configuration error | 10 | Model refused |
| 3 | Diff not found | 11 | Temporary API or connection failure, or deadline passed |
| 4 | Diff unreadable | 12 | Permanent API failure |
| 5 | Diff empty | 13 | Invalid model output |
| 6 | Diff too large | 14 | Internal error |
| 7 | Provider unavailable | | |

Full table and settings: [design.md](design.md#exit-codes).

## Behaviour changes to plan for

- **The old command is gone.** Anything calling `python review_bot.py` needs to move to `python -m review_bot`.
- **Exit 1 now means findings.** The default threshold is `low`, so any finding exits 1. Use `--threshold` to change it.
- **Output is no longer prose,** so anything reading the old comment text needs updating.
- **The diff must be UTF-8.** Windows PowerShell 5 `>` writes UTF-16, which exits 4. Use `git diff --output=change.diff`.
- **Claude is the default.** With no flag, the diff goes to a real model. A blank `REVIEW_BOT_PROVIDER` exits 2 rather than falling through to that default.
- **Install with `uv sync`.** The SDK wheel is about 150 MB and appears to bundle the Claude Code CLI.

## The Phase 0 gate

From the [README](../../../README.md#phase-0-stabilise):

| Gate item | Status | Evidence |
|---|---|---|
| No OpenAI dependency | Met | None in `pyproject.toml`, `uv.lock` or `src/`. `Assignment/` is the accepted exception |
| No secrets in source | Met | CI's whole-tree gitleaks scan finds no leaks, and its self-check confirms it detects a key. The `sk-REPLACE-ME` placeholder remains in `Assignment/` |
| Clear message and own exit code for every failure | Met, partly on fakes | Exit codes 2 to 14 are all mapped and tested. 7, 11, 12 and 13 ran through the real SDK against a fake CLI. 8 was also confirmed on a real invalid key. 9 and 10 ran through fakes only |
| Tests pass offline | Met | 957 pass in CI with no live model calls, under guards that fail any test reaching the real SDK call |

## Known limits

**Accepted behaviour**

- A `CLIConnectionError` exits 11 though it isn't always temporary. It's the only SDK text shown, escaped, after an audit of SDK 0.2.178. A test pins that version and asks for a re-audit on upgrade.
- The earliest failure signal wins. A later schema-valid result with `is_error=False` overrides it with only a stderr warning, including when it exits 0.
- A stream that hangs after a failed result waits for the deadline.
- `--deadline-s` isn't a hard bound: about 5 s over was observed against a fake CLI, and the SDK documents a worst case of about 20 s.
- Ctrl+C during a review exits 14 rather than a signal exit.
- Invalid token counts count as 0 with no warning, so usage figures can read low.
- Importing the SDK costs 1 to 2 s on every run, even `--provider mock` and `--help`. Phase 2 measures this.

**Not verified**

- A refusal has never been seen live, so `stop_reason="refusal"` and exit 10 rest on fakes.
- The 401 sequence is the only API failure confirmed live. The other status mappings rest on fakes and on a fake CLI written from reading the SDK's parser.
- Bare-JSON output rests on a handful of real runs.
- Whether `API_TIMEOUT_MS` applies per attempt is unknown. It didn't cap a 12.8 s call at 2 s.
- The cache token sum and the bundled CLI are inferred.
- Termination with an API call in flight, and surviving grandchild processes on Windows, are untested.
- Finding counts vary between runs on the same diff (5 and 6 seen). Phase 1's evals are what measure review quality; none exist yet.

## Not in Phase 0

The bot still sees only the diff. Diff parsing, rule checks, line-anchor checks, triage, the audit log, JSON and Markdown output, and evals are all [Phase 1](../../../README.md#phase-1-trustworthy-core).

## Decisions for you

1. **See the CLI's stderr?** It's discarded, so a failed call shows no CLI diagnostics. This is deferred to Phase 1's audit log, with redaction. You can overrule it.
2. **Specs changed while you were away.** Approved specs were edited after your approval, each disclosed in its PR: new P0-S2 AC17, changes to P0-S3 AC4 to AC6, and P0-S4 AC5, AC7 and AC13.
3. **Manual checks.** P0-S2 AC16 and P0-S4 AC15 say you run them. The main session ran them on your login while you were away.

## Pull requests

| PR | Change |
|---|---|
| #4 | Whole-tree secret scan in CI |
| #5 | Phase 0 design, specs and ADRs |
| #6 | Baseline copy of the original script |
| #7 | Main session commits and opens PRs |
| #8 | P0-S1: offline review, end to end |
| #9 | Spec follow-up on failure handling |
| #10 | P0-S2: real reviews from Claude |
| #11 | P0-S3: exit codes and safe errors |
| #12 | P0-S4: SDK error mapping, Claude as default |
