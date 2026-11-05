# P1-S1: Run mock reviews without loading the Claude SDK

- **Status:** Done
- **Depends on:** none
- **Touches:** `cli`

## Value
A developer or test can run `--provider mock`, or get a usage or input error, without paying to load the SDK, while a Claude review behaves as before.

## Acceptance criteria
Each runs in a child process that reports `'claude_agent_sdk' in sys.modules` after `main` returns.
- **AC1:** Given `Assignment/sample_diff.txt` and `--provider mock`, then the review completes with exit 0 or 1 and non-empty stdout, and the SDK was not imported. Later slices change which of the two it is, so this criterion doesn't pin it.
- **AC2:** Given `--help` or an unknown flag (exit 2), with any provider selection, or a missing diff (exit 3) with `--provider mock`, then the SDK was not imported. A missing diff with Claude selected may load it, because the provider is built before the diff is read.
- **AC3:** Given no `--provider` and a fake CLI ([P0-S4's real-SDK tests](../../phase-0/slices/p0-s4-claude-errors.md)), then the SDK was imported and the run behaves as in those tests.
- **AC4:** Given `--provider mock` and an SDK whose import raises `ImportError`, then the run behaves as in AC1.
- **AC5:** Given `--provider claude` and an SDK whose import raises `ImportError("SENTINEL")`, then it exits 14 with exactly one `review-bot: error:` stderr line and no `SENTINEL`.
- **AC6:** Given `build_provider` with Claude selected, then it returns a `ClaudeAgentProvider` and starts no subprocess, as P0-S4 AC13 requires.

## Interfaces
`main` and `build_provider` keep their [signatures](../../phase-0/design.md#interfaces).

## Out of scope
- Any time target in a test: timing is machine-dependent.
- Restructuring existing real-process tests.
- Measuring the CLI's start-up, which the README leaves to Phase 2.

## Notes
- **Why this slice:** the import costs 1 to 2 s per run (Phase 0 release notes), and `cli.py` imports the provider at module level, so every real-process test pays, including mock runs. Every later slice's gate benefits.
- Record the whole suite's time before and after in the PR. The saving is unmeasured until then, and no criterion depends on it. New Phase 1 tests prefer in-process `main`.
- The import inside the function needs a line-level lint suppression with its reason.
