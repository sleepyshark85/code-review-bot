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

**Two findings from the P1-S1 review, outside this slice, now P1-S18, which the user approved. The v0.1.0 release notes publicly say a fix for the first is planned for Phase 1.**

1. **The SDK's shutdown can put a block of asyncio traceback on stderr, breaking the one-line rule.** Severity `minor`. The SDK's `close()` waits for the process to exit, not for its pipes to close (`claude_agent_sdk/_internal/transport/subprocess_cli.py:1029-1046`), so `asyncio.run` can close the loop while they're still connected. When the transport is collected, its `__del__` raises, and CPython prints `Exception ignored in: ...` through `sys.unraisablehook`, which `main`'s `NullHandler` doesn't cover (the `addHandler` line in `cli.py`'s `main` is the gap on our side). **Repro:** a fake CLI whose helper process keeps the pipes open gave exit 11 with 36 stderr lines; the exit code and our error line stayed correct, and no CLI output appeared. It's the best explanation, not a proven cause, for one failure of `test_p0_s4_ac8_cli_that_never_exits_hits_the_deadline_with_one_line` at `p0_s4_support.py:36` in a local Windows run. The natural race wasn't reproduced in 51 runs, and Linux is untested. If it recurs, keep the full pytest output, which prints stderr. **Fix direction:** discard or log unraisable errors at the entry point, with the pipe-holding CLI as a regression test.
2. **The 1 s deadline test can fail under heavy load.** Severity `minor`. In `test_p0_s4_ac8_cli_that_never_exits_hits_the_deadline_with_one_line` and the `run_real_query` fixture's marker check, with 24 CPU burners on 12 cores, the fake CLI hadn't written its marker by the end of the run in 4 of 5 runs, so `assert marker.exists()` would fail. Stderr was still one line, so this is a test flake and a different one from finding 1. **Fix direction:** skip the marker check for this test, or use a longer deadline at the cost of extra seconds.


**Readings of this slice's criteria. Neither blocks the slice.**

1. **`--help` doesn't return from `main`.** It leaves through `SystemExit(0)`, so "after `main` returns" in the spec can't hold literally. The child reports after `main` ends either way, and the exit code is still asserted.
2. **AC3's "behaves as in those tests"** was read as the same exit codes and stream rules for the modes in limit 2, not all of them.

## P1-S18: Get clean stderr when the Claude SDK shuts down

Spec: [p1-s18-clean-sdk-shutdown.md](slices/p1-s18-clean-sdk-shutdown.md). Tests: [`tests/integration/test_p1_s18_clean_sdk_shutdown.py`](../../../tests/integration/test_p1_s18_clean_sdk_shutdown.py), 12 cases, and AC7's change to [`test_p0_s4_real_sdk.py`](../../../tests/integration/test_p0_s4_real_sdk.py).

**Status: every criterion passes on Windows.** AC8's Linux half isn't shown (see AC8 below). The run was against the uncommitted working tree on base `4bbeb2e`, so a later `src/` change needs a re-run. The tests were written from the spec. The developer's `cli.py` change wasn't read. To build the children, the tester read the committed `cli.py`'s `main`, the provider's deadline and connection messages, and how the SDK spawns the CLI.

### Gate

| Check | Result |
|---|---|
| `ruff format --check .` | Pass |
| `ruff check .` | Pass |
| `mypy` (strict) | Pass |
| `uv run pytest --cov -q`, whole suite, after fix round 1 | 975 passed, 12 skipped (`/dev/full`, Windows), 100% coverage (407 statements, 56 branches); 126.54 s in pytest, 133 s wall clock |
| The same, before fix round 1 | 975 passed, 12 skipped, 100%; 106.38 s in pytest, 112 s wall clock |
| `pre-commit`, `pip-audit` | Not run by the tester |

**Timing.** A single run, so a small difference may be noise. The new file took 27.45 s alone (30.8 s wall clock, with `--durations`); AC1 is the slowest at 5.1 s, because of its 3 s deadline. Each other case is one child process of 2 to 4 s, mostly the SDK's import. Against P1-S1's coverage run (98.65 s, 962 passed), the whole suite is 7.7 s slower, with the developer's unit tests included. That is less than the file's standalone time, so the two runs' conditions differ, and the figure shouldn't be read as the file's cost. The 20 s rise in fix round 1 isn't explained by its change, a one-line fixture. It came straight after a full probe run, so it is more likely machine load than cost. It wasn't re-measured.

### Criteria

Test names drop the `test_p1_s18_` prefix.

| AC | Test | Cases | Result |
|---|---|---|---|
| AC1 | `ac1_pipe_holder_without_a_result_hits_the_deadline_with_one_line` | 1 | Pass |
| AC2 | `ac2_pipe_holder_delivering_a_finding_gives_one_summary_line` | 1 | Pass |
| AC3 | `ac3_unraisable_after_main_is_discarded` | 1 | Pass |
| AC4 | `ac4_unraisable_during_a_claude_review_still_reaches_stderr` | 1 | Pass |
| AC4 | `ac4_unraisable_after_a_mock_run_still_reaches_stderr` | 1 | Pass |
| AC4 | `ac4_main_leaves_the_unraisable_hook_unless_it_built_claude` | 3 (injected mock, injected Claude, `--provider mock`), in-process | Pass |
| AC5 | P0-S3 AC4's tests in `test_p0_s3_stream_failures.py` and `test_p0_s3_safe_stderr.py`, unchanged | all | Pass |
| AC5 | `ac5_failure_after_a_pipe_holders_review_exits_14_with_one_line` | 2 (stdout closed, `RuntimeError("SENTINEL")` from stdout's `write`) | Pass |
| AC6 | `ac6_pipe_holder_signal_then_success_gives_the_warning_and_the_summary` | 1 | Pass |
| AC7 | `ac7_held_back_cli_hits_the_deadline_not_a_connection_failure` | 1 | Pass |
| AC7 | `test_p0_s4_ac8_cli_that_never_exits_hits_the_deadline_with_one_line`, changed | 1 | Pass |
| AC8 | This report: Windows run above; Linux below | | Windows pass; Linux not shown |

No failures to reproduce.

### How the tests work

- **The pipe holder** is the spec's: before sending anything, the fake CLI starts a helper with descriptors 0, 1 and 2 passed explicitly, since Windows passes standard handles on only when they are given. The helper writes nothing and ends after 20 s, or sooner when the test's teardown asks through a stop file. The CLI waits until the helper is running before it answers. Teardown then requires the helper to have ended within 10 s, so a helper can't outlive its test unnoticed. After every subset and the full run, no `python.exe` was left.
- **The bot's stdout and stderr go to files, not pipes.** The helper may hold a pipe it inherited, and `communicate` would wait for the helper.
- **AC1 asserts the helper was still running when the run ended,** so a pass can't come from a helper that had already let go of the pipes.
- **AC3 and AC4's object** raises `RuntimeError("SENTINEL")` from `__del__`. "After `main` returns" is a release between `cli.main(argv)` and `sys.exit(code)`, so the child ends with `code = cli.main(argv)` and then `sys.exit(code)`. With nothing to release, that is the spec's `sys.exit(cli.main(argv))`. "During a Claude review" is a release from the patched `_build_command`, which the SDK calls as it connects.
- **AC5's `RuntimeError`** comes from a stdout whose `write` raises, so it's raised in `main` after the review without patching the bot.
- **AC7's held-back CLI** reads stdin until it closes and writes its marker only after 20 s. So it hasn't answered by the 1 s deadline, it can't outlive the bot, and AC7 asserts the marker doesn't exist. The P0-S4 test now asserts the deadline message and no connection-failure message, and its fixture skips the marker check for `never_ends` alone. Every other mode keeps it, and `success_then_never_ends` still checks its marker.
- **AC4's exit code "without the object"** is asserted as 1, the code a delivered finding and the mock's canned finding give. There's no paired run without the object.

### Would the tests fail on wrong code?

**All 8 kinds of wrong code are caught,** and the last row is a control that rightly passes. None changed a repository file. A driver script in the scratchpad ran pytest in-process and inserted the wrong behaviour into the children's code after their `from review_bot import cli`, or into `cli.main` in-process.

| Wrong code | Failing cases | Scope run |
|---|---|---|
| **No suppression:** the committed `cli.py` (`HEAD`), compiled in the child in place of the working copy | 6 of 12: AC1, AC2, AC3, AC5 (both), AC6. AC4 and AC7 rightly pass | P1-S18 |
| A suppression installed for every run, in the child and in-process | 5 of 6: all of AC4. AC3 rightly passes | AC3, AC4 |
| A suppression from when `main` starts a Claude run | 1 of 6: AC4's release during the review | AC3, AC4 |
| A suppression that ends when `main` returns | 1 of 6: AC3. Also 5 of 5 pipe-holder cases: AC1, AC2, AC5 (both), AC6 | AC3, AC4; then AC1, AC2, AC5, AC6 |
| Stderr dropped once the review's loop has ended | 7 of 12: AC1, AC2, AC4's mock case, AC5 (both), AC6, AC7 | P1-S18 |
| An extra stderr line at exit | 6 of 12: AC1, AC2, AC5 (both), AC6, AC7 | P1-S18 |
| The deadline reported with the connection-failure message | 3 of 3: AC1, AC7, the changed P0-S4 test | AC1, AC7, P0-S4 AC8 |
| A CLI held back 20 s, with the committed P0-S4 test | 1 of 1: fails at `assert marker.exists()`, with stderr the one correct deadline line | Committed P0-S4 AC8 |
| The same CLI, with the changed P0-S4 test | 0 of 1: correct, it passes. `signal_then_success`, run alongside as a control, waited out the 20 s and passed its marker check | Changed P0-S4 AC8 and AC6 |

Under no suppression, AC1's stderr is our deadline line, then `Exception ignored in: <function _ProactorBasePipeTransport.__del__ ...>` and its traceback. The noise comes after the error line, so inside the spec's window, not before it. The suppression that ends when `main` returns shows it is raised after `main` has returned, so the window must last until the process exits, as the spec says.

**These results were measured on subsets only,** each in a fresh pytest process, so none of them shows whether the tests also catch wrong code in the full suite's order. The fix below covers that for AC4's in-process check.

### Fix round 1: the suppression leaked into pytest's session

**Finding (reviewer, major):** `test_p0_s2_ac15_control_default_provider_route_fires_the_guard` runs `main` in-process with Claude built, so the suppression set `sys.unraisablehook` to `_discard_unraisable`. pytest 9.2 installs its own hook once, in `pytest_configure`, so the suppression stayed for the rest of the session. Every later test silently lost its `__del__` errors. Also, AC4's in-process identity check passed for a "suppression for every run" mutant in a full run, because the same function was already installed. The table above missed this: its mutant installed a fresh `lambda`, and it ran on a subset.

**Fix:** an autouse fixture, `own_unraisable_hook` in [`tests/conftest.py`](../../../tests/conftest.py), runs `monkeypatch.setattr(sys, "unraisablehook", sys.unraisablehook)`, so every test starts and ends with pytest's hook. The developer removes the manual restore in `tests/unit/test_cli_main.py`.

**Proof, in the full suite's order:** one run of all 987 cases, without `--cov`, from a driver script on stdin. A probe plugin recorded `sys.unraisablehook` as each test's call began. A mutant wrapped `cli.main` in-process so that every run first installed the developer's own `cli._discard_unraisable`, the function that leaked.

| Check | Result |
|---|---|
| Tests whose call began under a hook other than pytest's | 0 of 975 run |
| AC4's 3 in-process cases: began under pytest's hook | Yes, all 3 |
| The P0-S2 AC15 control ran before them | Yes, for all 3 |
| The mutant fails AC4's in-process cases | 3 of 3 fail; the other 972 pass, 12 skipped |

The leak itself wasn't re-run without the fixture by the tester. The reviewer's probe showed it.

### AC8: Windows and Linux

- **Windows (local, Python 3.12.16):** AC1 to AC7 pass with no live call; every CLI is fake. Without the suppression, AC1 and AC3 fail, as AC8 requires, and so do AC2, AC5 and AC6.
- **Linux: not shown.** The P1-S18 tests weren't run on Linux, and CI wasn't checked. WSL's Python 3.12.11 lacks the dependencies, and installing them needs the network. Two cheap probes without the SDK were tried instead, each with a child whose helper holds the pipes and a loop closed before they close, one with a pending stdout read. **Neither made any noise on Windows either,** so they don't reproduce the mechanism and say nothing about Linux. Whether AC1 fails on Linux without the suppression is still unknown. AC3 doesn't depend on the pipes, so it should fail on Linux without the suppression, but that wasn't run either.

### Coverage

The whole suite covers 100% of statements and branches, `cli.py` included. Child processes aren't measured, so P1-S18's tests add none of it. It rests on in-process tests, including the developer's unit tests.

### Limits

1. **Linux is unshown,** as above. CI runs Linux only, so CI's pass of AC1 won't show that the suppression is needed there.
2. **"Discarded, not recorded" is checked on stderr only.** A suppression that wrote the report to a file or a log would pass.
3. **AC2's "whatever the machine load"** wasn't run under load. The helper's start is proved by the CLI waiting for it before it answers.
4. **AC6 doesn't pin the order** of the warning and the summary line. The spec doesn't give one.
5. **AC4's reports are checked by their text,** `Exception ignored in` and `SENTINEL`, not their whole form.
6. **AC3 releases the object between `main` and `sys.exit`.** An object collected later, at interpreter shutdown, isn't exercised.
7. **AC4's in-process check** covers three routes that return 1. A failing injected or mock run isn't checked for the hook.
