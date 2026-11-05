# Phase 1 test report

## P1-S1: Run mock reviews without loading the Claude SDK

Spec: [p1-s1-mock-without-sdk.md](slices/p1-s1-mock-without-sdk.md). Tests: [`tests/integration/test_p1_s1_mock_without_sdk.py`](../../../tests/integration/test_p1_s1_mock_without_sdk.py), 16 cases, trimmed from 32 in review round 1, with AC5's flag case then restored.

**Status: every criterion passes.** The run was against the uncommitted working tree on base `6f62f3a`, so a later `src/` change needs a re-run. The tests were written from the spec. Before writing them, the tester read `cli.py`'s imports and its committed `parse_args`, `build_provider` and `main`, to learn how `--help` ends. The developer's change wasn't read.

### Gate

| Check | Result |
|---|---|
| `ruff format --check .` | Pass |
| `ruff check .` | Pass |
| `mypy` (strict, `src` and `.claude/hooks`) | Pass |
| `uv run pytest --cov -q`, whole suite | 962 passed, 12 skipped (`/dev/full`, Windows), 100% coverage; 98.65 s in pytest, 104 s wall clock |
| `pre-commit`, `pip-audit` | Not run by the tester |

### Timing

Each figure is a **single run**, so a small difference may be noise. Both ran one after the other, with no other Python process at the start of each, and no `--cov`. They were taken at 15 cases, before AC5's flag case returned, and weren't repeated. The coverage run above isn't comparable, because it uses `--cov`.

| Run | Tests | In pytest | Against the baseline | Wall clock |
|---|---|---|---|---|
| Baseline before the slice, from the developer | 957, as given | 126.05 s | | Not given |
| Like-for-like: without the new file | 958 (946 passed, 12 skipped) | 85.98 s | -40.07 s (-31.8%) | 88 s |
| Whole suite, with the new file at 15 cases | 973 (961 passed, 12 skipped) | 93.26 s | -32.79 s (-26.0%) | 96 s |

- **The new file** costs about 7.3 s by difference between the two runs. It took about 18 s alone, in a run that also held one P0-S4 case.
- **The like-for-like count is one over the 957 given.** The coordinator identifies the extra test as the developer's new unit test. The tester didn't run the baseline, so its conditions are as the developer reported them.

### Criteria

Test names drop the `test_p1_s1_` prefix.

| AC | Test | Cases | Result |
|---|---|---|---|
| AC1 | `ac1_mock_review_of_the_sample_completes_without_the_sdk` | 1 (flag) | Pass |
| AC2 | `ac2_help_doesnt_load_the_sdk` | 3 (default, mock flag, Claude flag) | Pass |
| AC2 | `ac2_unknown_flag_doesnt_load_the_sdk` | 3 (the same) | Pass |
| AC2 | `ac2_missing_diff_with_mock_doesnt_load_the_sdk` | 1 (flag) | Pass |
| AC2, beyond its list | `ac2_invalid_provider_value_doesnt_load_the_sdk` | 1 | Pass |
| AC3 | `ac3_default_provider_loads_the_sdk_and_delivers_the_review` | 1 | Pass |
| AC3 | `ac3_failures_exit_as_in_p0_s4_when_the_sdk_loads_during_the_run` | 2 (crash, malformed; both exit 12) | Pass |
| AC4 | `ac4_mock_review_needs_no_importable_sdk` | 1 (flag) | Pass |
| AC5 | `ac5_claude_with_an_sdk_that_wont_import_exits_14_without_its_text` | 2 (flag, default) | Pass |
| AC6 | `ac6_build_provider_returns_claude_and_starts_no_subprocess` | 1 (default route) | Pass |

No failures to reproduce.

The fake CLI and its checks moved from `test_p0_s4_real_sdk.py` to `tests/integration/real_sdk_support.py`, so no test module imports another. P0-S4's test bodies didn't change.

### How the children differ from P0-S4's

- **AC3 can't reuse P0-S4's child.** That child imports the SDK to patch it, so the check would pass whatever `cli` does. This one patches from a meta-path finder when something else first loads the package.
- **AC4 and AC5 don't use a `sys.modules` entry of `None`.** It would make `'claude_agent_sdk' in sys.modules` true, and it can't carry `SENTINEL`. A meta-path finder in the child raises `ImportError("SENTINEL")` and records attempts. The real package isn't touched.
- **AC4 is stricter than the `sys.modules` check.** A failed import leaves no entry, so `ac4` also requires zero attempts. Otherwise a swallowed eager import would pass.
- **AC5 requires at least one attempt,** so an exit 14 from another cause can't pass.
- **AC6 uses `sys.addaudithook`,** which sees `subprocess`, `os.system`, `os.spawn` and the like without patching any of them.

### Would the tests fail on wrong code?

Re-proved after the trim: **all 21 cases of wrong code are caught.** The 20 in the first rows ran on 15 cases, and restoring AC5's flag case removed nothing from them. None changed a file: each ran from a script on stdin, prefixing the wrong behaviour to the child's `-c` code, against the tests in the last column.

**The committed eager `cli.py`** (`HEAD`), compiled in the child in place of the working copy, against AC1, AC4 and AC3's delivery case:

| Test | Result | Reason |
|---|---|---|
| `ac1` | Fail | In the 32-case run, `report["sdk_loaded"]` was `True`; the assertion is unchanged |
| `ac4` | Fail | In the 32-case run, the import raised while `cli` loaded, so there was no report |
| `ac3_default_provider...` | Pass | Correct: an eager import does load the SDK |

**Wrapped behaviour:**

| Wrong code | Failing cases | Scope run |
|---|---|---|
| `import claude_agent_sdk` as `cli` loads | 11 of 15, before the flag case returned: all of AC1, AC2, AC4, AC5; AC3 and AC6 rightly pass | All |
| The same, swallowing `ImportError` | 2 of 3: AC1, and AC4 by its attempts check alone | AC1, AC4, AC5 |
| `build_provider` always loads the SDK | 3 of 10: AC1, AC4, AC2's missing diff | AC1, AC2, AC4 |
| A usage error loads the SDK | 7 of 9: AC2's help, unknown flag, invalid provider | AC1, AC2 |
| An input error loads the SDK | 1 of 9: AC2's missing diff | AC1, AC2 |
| `main` does nothing and returns 0 | 3 of 3 | AC1, AC4, AC5 |
| A completed review exits 7 | 2 of 2 | AC1, AC4 |
| The default provider is mock | 5 of 5 | AC3, AC5, AC6 |
| Delivered and provider failures exit 14 | 3 of 3 | AC3 |
| The SDK is gone from `sys.modules` after `main` | 3 of 3 | AC3 |
| The root logger's `NullHandler` isn't added | 2 of 3: crash, malformed | AC3 |
| `SENTINEL` printed on exit 14 | 1 of 1 | AC5 |
| A second stderr line on exit 14 | 1 of 1 | AC5 |
| An import failure exits 12 | 1 of 1 | AC5 |
| An import failure falls back to mock | 1 of 1 | AC5 |
| Exit 14 with a clean line, no import tried | 1 of 1, by the attempts check | AC5 |
| `build_provider` returns mock | 1 of 1 | AC6 |
| `build_provider` runs a `subprocess` | 1 of 1 | AC6 |
| `build_provider` calls `os.system` | 1 of 1 | AC6 |
| An explicit Claude selection builds mock | 1 of 16: `ac5...[flag]` alone. Without that case it passed all of P1-S1 | All, at 16 cases |

The same wrong code, applied in-process, also fails `test_p0_s2_ac9_build_provider_returns_claude_and_starts_no_subprocess` (1 of the 2 AC9 cases; the other selects by variable). So AC6 stays on the default route alone, as the coordinator's rule set out.

### Coverage

The whole suite covers 100% of statements and branches (400 statements, 56 branches), including `cli.py`. Child processes aren't measured, so the P1-S1 cases add none of this: it rests on in-process tests.

### Limits

1. **AC6 runs by the default route only.** Its explicit-flag route is covered by P0-S2's AC9 test, in-process, which doesn't show what a child process loads.
2. **AC3 covers 3 of P0-S4's real-SDK modes.** Exits 7, 8 and 13 no longer run where the SDK loads during the run. P0-S4's file still covers them, but its child imports the SDK first, so a fault in those mappings that only a deferred import causes would pass.
3. **Only missing diff stands for input errors.** A directory, empty or too-large diff that alone loaded the SDK would pass. AC2 names only the missing diff.
4. **Selecting mock by variable isn't run** for AC1, AC2 or AC4. `--help` and unknown flags end before the variable is read, so those cases can't differ today. If that order changes, they could.
5. **AC2's Claude with a missing diff isn't pinned,** because the spec allows either. Nor is the exit code when the SDK won't import and the diff is missing.
6. **AC6 doesn't pin whether `build_provider` loads the SDK.** The criterion doesn't say.
7. **`python -m review_bot` and the installed script aren't checked for the import.** The criteria name `main`, so an eager import in `__main__.py` alone would pass.
8. **AC4 and AC5 refuse the import at the package's name.** An `ImportError` raised partway through loading the SDK isn't exercised.
9. **Run on Windows only** by the tester. CI hasn't been checked.
10. **Children run from the repository root,** through `run_process`. Once P1-S4 defaults the audit log to the working directory, these mock runs would write there. That slice should decide how they handle it.

### For the designer

**Two findings from the P1-S1 review, outside this slice. They need a new slice, which the user must approve. The v0.1.0 release notes publicly say a fix for the first is planned for Phase 1.**

1. **The SDK's shutdown can put a block of asyncio traceback on stderr, breaking the one-line rule.** Severity `minor`. The SDK's `close()` waits for the process to exit, not for its pipes to close (`claude_agent_sdk/_internal/transport/subprocess_cli.py:1029-1046`), so `asyncio.run` can close the loop while they're still connected. When the transport is collected, its `__del__` raises, and CPython prints `Exception ignored in: ...` through `sys.unraisablehook`, which `main`'s `NullHandler` doesn't cover (the `addHandler` line in `cli.py`'s `main` is the gap on our side). **Repro:** a fake CLI whose helper process keeps the pipes open gave exit 11 with 36 stderr lines; the exit code and our error line stayed correct, and no CLI output appeared. It's the best explanation, not a proven cause, for one failure of `test_p0_s4_ac8_cli_that_never_exits_hits_the_deadline_with_one_line` at `p0_s4_support.py:36` in a local Windows run. The natural race wasn't reproduced in 51 runs, and Linux is untested. If it recurs, keep the full pytest output, which prints stderr. **Fix direction:** discard or log unraisable errors at the entry point, with the pipe-holding CLI as a regression test.
2. **The 1 s deadline test can fail under heavy load.** Severity `minor`. In `test_p0_s4_ac8_cli_that_never_exits_hits_the_deadline_with_one_line` and the `run_real_query` fixture's marker check, with 24 CPU burners on 12 cores, the fake CLI hadn't written its marker by the end of the run in 4 of 5 runs, so `assert marker.exists()` would fail. Stderr was still one line, so this is a test flake and a different one from finding 1. **Fix direction:** skip the marker check for this test, or use a longer deadline at the cost of extra seconds.


**Readings of this slice's criteria. Neither blocks the slice.**

1. **`--help` doesn't return from `main`.** It leaves through `SystemExit(0)`, so "after `main` returns" in the spec can't hold literally. The child reports after `main` ends either way, and the exit code is still asserted.
2. **AC3's "behaves as in those tests"** was read as the same exit codes and stream rules for the modes in limit 2, not all of them.
