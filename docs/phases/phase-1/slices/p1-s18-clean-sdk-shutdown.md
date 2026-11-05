# P1-S18: Get clean stderr when the Claude SDK shuts down

- **Status:** Approved
- **Depends on:** P1-S1
- **Touches:** `cli`, `tests/integration/test_p0_s4_real_sdk.py`

## Value
A developer or CI job gets only the promised stderr from a Claude run, even when a CLI helper holds the pipes open, and the 1 s deadline test stops failing on machine load through its marker check.

## Acceptance criteria
**What is suppressed:** only reports that reach `sys.unraisablehook` (the `Exception ignored in` text), and only in a run where `main` built the Claude provider itself, from when the review has ended and its event loop starts shutting down until the process exits. They are discarded, not recorded. Everything else reaches its stream as today.
- **AC1:** Given a [pipe holder](#interfaces) that exits after reading the prompt with no result, and `--deadline-s 3`, then the run exits 11, stdout is empty, and stderr is exactly one `review-bot: error:` line, with no `Exception ignored in`.
- **AC2:** Given a pipe holder that delivers `HIGH_FINDING`, then the run exits 1, stdout starts `src/app.py:7`, and stderr is exactly one `provider=claude` summary line. The delivered result proves the helper started, whatever the machine load.
- **AC3:** Given a delivered Claude review and an object whose `__del__` raises `RuntimeError("SENTINEL")`, released after `main` returns, then the run still exits 1, and stderr has no `Exception ignored in` and no `SENTINEL`. No pipe race is involved.
- **AC4:** Given that object released during a Claude review, or after `main` returns in a `--provider mock` run, then its `Exception ignored in` report reaches stderr as today, and the exit code is the one the run has without the object. Given an in-process `main` with an injected provider or `--provider mock`, then `sys.unraisablehook` is the same object afterwards.
- **AC5:** Given P0-S3 AC4's cases, then its tests pass unchanged. Given AC2's pipe holder with stdout closed, or an unexpected `RuntimeError("SENTINEL")` raised in `main` after the review, then the run exits 14 and stderr is exactly one `review-bot: error:` line, with no `SENTINEL` and no `Exception ignored in`.
- **AC6:** Given a pipe holder that sends P0-S4 AC6's overridden `rate_limit` signal and then a successful result, then the run exits 1, the finding is on stdout, and stderr is exactly the `rate_limit` warning and the summary line.
- **AC7:** Given a never-ending fake CLI held back so its marker doesn't exist when a 1 s deadline passes, then the run exits 11, stdout is empty, stderr is one error line, and `test_p0_s4_ac8_cli_that_never_exits_hits_the_deadline_with_one_line` makes the same checks and no marker check. It no longer needs one: a CLI still starting and a hung one look the same to the bot, as neither has answered by the deadline. Every other `run_real_query` mode keeps its marker check, and `success_then_never_ends` still proves a never-ending CLI runs.
- **AC8:** Given Linux and Windows, then AC1 to AC7 hold with no live call, because every CLI is fake. Without the suppression, AC3 fails on both and AC1 fails on Windows. CI runs Linux only, so the test report gives a local Windows run, and states whether AC1 fails on Linux, where the noise hasn't been reproduced.

## Interfaces
`main` and `build_provider` keep their [signatures](../../phase-0/design.md#interfaces). Children patch the command as [P0-S4's real-SDK tests](../../phase-0/slices/p0-s4-claude-errors.md) do, then call `sys.exit(cli.main(argv))`, as the console script and `python -m review_bot` do. **Pipe holder:** a fake CLI that, before sending anything, starts a helper inheriting its stdin, stdout and stderr; the helper writes nothing and ends by itself within 30 s.

## Out of scope
- Closing the transport through private SDK API, or changing `--deadline-s`'s bound.

## Notes
- **Delivery order:** ships directly after P1-S1 and before P1-S2, so later slices don't inherit a flaky gate. Its number only reflects when it was added.
- **Source:** [the P1-S1 test report's findings](../test-report.md#for-the-designer). The noise is the best explanation, not a proven cause, of the one local deadline-test failure, so if it recurs keep the full pytest output. If AC1's noise appears before the suppression window, send this back to the designer rather than widening it.
