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

## P1-S2: Skip docs, lock and generated files, and reject text that isn't a diff

Spec: [p1-s2-parse-and-skip.md](slices/p1-s2-parse-and-skip.md). Tests: [`tests/integration/test_p1_s2_parse_and_skip.py`](../../../tests/integration/test_p1_s2_parse_and_skip.py), 114 cases after the plain-diff decision (112 after fix round 1, 87 before it). All but two run in-process: `parse_diff` directly, and `main` with an injected `MockProvider`. The two parse-time cases run a child process, so a regression fails at a timeout instead of blocking the suite.

**Status: every criterion passes, after fix round 1 and the plain-diff decision.** The run was against the uncommitted working tree on base `a308085`. The developer's last `src/` change was at 00:21:55 on 2026-11-06, and their last unit-test change at 00:22:09, both before the run started at 00:22:27. A later `src/` change needs a re-run. The tests were written from the spec's criteria and Interfaces before the developer's code was read. Only `diff_parser`'s public dataclass fields were read afterwards, to build the wrong-code checks, and `review.py`'s import of `parse_diff`, to know where to patch it.

### Gate

| Check | Result |
|---|---|
| `ruff format --check .` | Pass |
| `ruff check .` | Pass |
| `mypy` (strict) | Pass |
| `uv run pytest --cov -q`, whole suite, after the plain-diff decision | 1143 passed, 12 skipped (`/dev/full`, Windows), 100% coverage (619 statements, 114 branches); 115.49 s in pytest, 120 s wall clock |
| The same, after fix round 1 | 1138 passed, 12 skipped (`/dev/full`, Windows), 100% coverage (619 statements, 114 branches); 107.15 s in pytest, 111 s wall clock |
| The same, before fix round 1 | 1099 passed, 12 skipped, 100% (614 statements, 120 branches); 108.56 s in pytest, 112 s wall clock |
| `pre-commit`, `pip-audit` | Not run by the tester |

**Timing.** A single run, so a small difference may be noise. The new file took 1.1 to 3.3 s alone across the wrong-code runs. Against P1-S18's run after fix round 1 (126.54 s, 975 passed), the suite is 18 s faster with 124 more cases, which is more likely machine load than the slice's effect. It wasn't re-measured.

### Criteria

Test names drop the `test_p1_s2_` prefix.

| AC | Test | Cases | Result |
|---|---|---|---|
| AC1 | `ac1_sample_is_one_file_with_two_mismatched_hunks` | 1 | Pass |
| AC1 | `ac1_sample_added_and_context_lines_are_12_to_14_and_30_to_41` | 1 | Pass |
| AC1 | `ac1_sample_added_lines_are_14_32_to_36_and_38_to_41` | 1 | Pass |
| AC1 | `ac1_blank_before_the_second_header_is_not_a_body_line` | 1 | Pass |
| AC1 | `ac1_blank_before_export_csv_is_context_line_37` | 1 | Pass |
| AC1 | `ac1_sample_is_reviewed_whole` | 1 | Pass |
| AC1, beyond its text | `ac1_numbering_follows_the_body_not_the_declared_counts` (Hypothesis, 60 examples) | 1 | Pass |
| AC2 | `ac2_header_with_no_counts_numbers_from_its_start` | 1 | Pass |
| AC2 | `ac2_no_newline_marker_is_not_a_line` | 1 | Pass |
| AC2 | `ac2_crlf_endings_leave_no_carriage_return` | 1 | Pass |
| AC2 | `ac2_file_headers_inside_a_hunk_without_a_hunk_header_are_body_lines` | 1 | Pass |
| AC3 | `ac3_unreadable_new_start_gives_no_line_numbers` | 2 (`@@ garbage @@`, `@@ -1,2 +x,2 @@`) | Pass |
| AC3 | `ac3_unreadable_hunk_still_reaches_the_prompt` | 1 | Pass |
| AC4 | `ac4_path_is_the_new_path_or_none_when_deleted` | 7 (rename, new, deleted, `--no-prefix`, quoted octal, plain unified, `b/b/q.py` keeps one `b/`) | Pass |
| AC4 | `ac4_new_file_numbers_from_line_1` | 1 | Pass |
| AC5 | `ac5_parse_diff_raises_for_text_with_no_file_header` | 3 (`+x\n`, prose, orphan hunks) | Pass |
| AC5 | `ac5_text_with_no_file_header_exits_15_naming_the_path` | 3 (the same) | Pass |
| AC6 | `ac6_preamble_is_not_rejected_and_stays_in_reviewable_text` | 1 | Pass |
| AC6 | `ac6_preamble_reaches_the_prompt` | 1 | Pass |
| AC7 | `ac7_prompt_holds_only_the_preamble_and_app_section` | 1 | Pass |
| AC7 | `ac7_skip_reasons_follow_the_path_rules` | 1 | Pass |
| AC7, from Compatibility | `ac7_summary_counts_skipped_files_and_the_model_call` | 1 | Pass |
| AC8 | `ac8_crlf_diff_reaches_the_prompt_byte_for_byte` | 1 | Pass |
| AC9 | `ac9_all_skipped_exits_0_with_warning` | 1 | Pass |
| AC9 | `ac9_all_skipped_summary_says_the_model_wasnt_called` | 1 | Pass |
| AC9 | `ac9_warning_names_ten_paths_then_a_count` | 1 (12 paths) | Pass |
| AC10 | `ac10_generated_marker_in_content_is_still_reviewed` | 2 (`# @generated`, `# DO NOT EDIT`) | Pass |
| AC10 | `ac10_binary_section_is_skipped_as_binary` | 1 | Pass |
| AC10 | `ac10_binary_section_leaves_the_prompt` | 1 | Pass |
| AC11 | `ac11_path_is_not_skipped`, `ac11_path_is_reviewed` | 6 each | Pass |
| AC11 | `ac11_path_is_skipped`, `ac11_skipped_path_leaves_the_prompt` | 5 each | Pass |
| AC11, the Interfaces table | `ac11_interfaces_table_path_is_skipped_with_its_reason` | 25, after fix round 1 (see below) | Pass |

No failures to reproduce.

**How the prompt is compared.** The criteria's "byte for byte" is checked as equality, not containment. The diff between the opening and closing delimiter lines of `user_prompt` must equal the expected text. In AC7, `app.py` sits between skipped sections, so a removal that takes too much or too little fails.

### Handovers (spec Notes)

1. **The shared constant.** The developer's `tests/unit/diff_samples.py` holds `MINIMAL_DIFF` (one hunk adding `x` to `app.py`) and `section(path, body)`. [`tests/integration/diff_support.py`](../../../tests/integration/diff_support.py) re-exports them as `unit.diff_samples`. `tests/` is always on the path, but `tests/unit` is there only when unit tests are collected, and it has no `__init__.py`, so it imports as a namespace package. One change replaced 59 `write_diff("+x\n")` and `write_text("+x\n", ...)` sites, `claude_argv` among them, across 12 files. It also replaced the 2 lambda sites in P0-S3's `INPUT_FAILURES`. Untouched: the `"+x"` user prompts given straight to a provider (`p0_s4_support.py`, conformance), which are never parsed, and `b"\xff\xfe+x\n"`, which is invalid UTF-8 on purpose.
2. **P0-S1 AC12's non-diff case is retired** in favour of AC5, with a comment in `test_p0_s1_prompt.py`. **AC13's `single_line` case**, `"unique-diff-marker, not even a diff"`, is the same input, so it was retired too (2 cases).
3. **P0-S1 AC11's limit** is now each diff's own size. The three variants stay: ASCII, multi-byte and CRLF, each as a real diff.
4. **`prompt_injection.diff`** now targets `app/handler.py` instead of `notes.md`. P0-S1 AC13's two tests over it pass unchanged.
5. **Breakage the Notes didn't list.** These are the same handover, for other headerless inputs. No assertion was weakened.
   - P0-S1 AC13 and AC15: 8 `write_diff("+x = 1\n")` sites now use `MINIMAL_DIFF`. They only read the system prompt.
   - P0-S1 AC14: the diffs whose delimiters are reused now use `section`.
   - P0-S1 AC3: `f"+{SENTINEL}\n"` is now `section("app.py", ...)`.
   - P0-S1 AC4's flag-beats-variable test: `b"x" * 11` at limit 11 became `MINIMAL_DIFF` at its size, with the variable at one byte less.
   - P0-S1 AC10's exact-default-limit file is now a real diff padded to exactly 400,000 bytes.
   - P0-S2 AC15's victim test, run in a separate pytest, wrote `"+x"`. It now writes `MINIMAL_DIFF`'s value inline.
   - **P0-S3 AC6's `stderr.endswith("output_tokens=N\n")`** contradicts Phase 1's Compatibility rule, which appends `skipped_files=` and `model_called=`. It now requires the exact value to end a field, `output_tokens=N` then a space or the line's end. That is the same value check, without pinning the line's last field. **For the designer:** this is a Phase 0 criterion's test changed by a Phase 1 rule, but it isn't in the slice's Notes.

### Would the tests fail on wrong code?

**All 24 kinds of wrong code are caught,** and a no-op control passes all 87 cases. A driver ran pytest in-process against the P1-S2 file and patched `parse_diff` in every `review_bot` module that held it, `diff_parser` and `review`, or filtered `cli`'s stderr lines. It lived in `tests/` while it ran and was then deleted: the path guard blocks the scratchpad, and the shell mangled it on stdin. No repository file was changed by it.

| Wrong code | Failing cases |
|---|---|
| **A parser that trusts the declared counts**, consuming each hunk's body until they are met, written from scratch | 52 of 87, including all 6 of AC1 and the Hypothesis case (it doesn't skip, so most skip cases fail too) |
| **A skip rule that matches `docs/conf.py`** (any path under `docs/`) | 2: AC11's `docs/conf.py`, parse and prompt |
| **A skip rule that matches `src/build/x.py`** (`build/` or `dist/` at any depth) | 4: AC11's `src/build/x.py` and `src/dist/x.py`, parse and prompt |
| `vendor/` matched at the root only | 1: AC11's `src/vendor/x.py` |
| Every `.txt` skipped | 6: AC11's `requirements*.txt` and `CMakeLists.txt` |
| A file opted out by `@generated` or `DO NOT EDIT` in its content | 2: AC10 |
| Nothing skipped | 42: AC7, AC9, AC10's binary cases, AC11's skipped paths, the table |
| Binary reported as `generated` | 1: AC10 |
| Every leading `a/` and `b/` stripped | 1: AC4's `b/b/q.py` |
| A deleted file keeps its old path | 1: AC4 |
| Octal escapes not decoded | 1: AC4 |
| CRLF normalised in `reviewable_text` | 1: AC8 |
| Preamble dropped | 4: AC6 (both), AC7 (two) |
| Removed lines advance the new numbering | 8: AC1 (4, Hypothesis included), AC2 (4) |
| `has_count_mismatch` always false | 4: AC1, Hypothesis, AC3 (both) |
| An unreadable start numbered from 1 | 2: AC3 |
| The blank before the second `@@` kept as context, line 15 | 2: AC1 |
| Text with no file header accepted | 6: AC5, parse and `main` |
| The error line doesn't name the path | 3: AC5 |
| No "nothing was reviewed" warning | 2: AC9 |
| `model_called=true` when nothing was called | 2: AC9 |
| `skipped_files=0` always | 3: AC7's summary, AC9 (two) |
| The warning names all 12 paths | 1: AC9's ten-path case |
| The warning has no count after the tenth path | 1: AC9's ten-path case |

These ran on the P1-S2 file only, not the whole suite. "The provider isn't called" in AC5 and AC9 is shown by `provider.requests == []`. The "nothing skipped" row shows that it fails when the provider is called.

### Fix round 1: regression tests

The review found seven issues, which the developer fixed in `src/`. The spec's Interfaces table and Notes changed with it; the criteria didn't. Before the fix landed, the tester probed the unfixed parser. **Every item below misbehaved there as the review said:**
- A faked header split `q.sql` in two, with and without `diff --git`, and dropped `DROP TABLE users;`.
- Both 5000-digit headers raised `ValueError`, which `main` turns into exit 14.
- The crafted lines took **53 s at 120 KB** (escaped quotes) and **74 s at 300 KB** (`and`s).
- Deleted `uv.lock`, `README.md` and `app.py` were all reviewed.
- A `GIT binary patch` section wasn't binary.
- `CHANGELOG.py` and `LICENSE.js` were skipped as docs, and `x.generated.py` as generated.

New and changed tests, with names shortened as above:

| Item | Test | Cases | Result |
|---|---|---|---|
| 1. AC7, faked header with `diff --git` | `ac7_faked_file_header_in_a_git_diff_stays_in_one_file`, `ac7_faked_file_header_in_a_git_diff_cant_hide_lines` | 2 | Pass |
| 1. AC7, faked header in a plain unified diff, after the decision below | `ac7_plain_unified_faked_header_cant_hide_lines`, `ac7_plain_unified_faked_header_reaches_the_prompt` | 2 | Pass |
| The decision: a plain unified diff skips no files | `ac9_plain_unified_readme_is_reviewed_not_skipped`: a plain diff of only `README.md` calls the model, exits 1, and reaches the prompt whole | 1 | Pass |
| 2. AC3, 5000-digit new start and count | `ac3_5000_digit_header_number_gives_no_line_numbers`, `ac3_5000_digit_header_number_is_reviewed_not_exit_14` | 2 each | Pass |
| 3. Parse time near the 400,000-byte default limit | `crafted_line_near_the_size_limit_parses_quickly` (a `diff --git` line of repeated `\"`; a `Binary files ` line of repeated ` and `) | 2 | Pass |
| 4. Deleted files | `ac11_deleted_file_is_skipped_by_its_old_path` (`uv.lock`, `README.md`), `ac11_deleted_skipped_file_leaves_the_prompt`, `ac11_deleted_code_file_is_still_reviewed`, `ac9_only_deleted_skipped_files_warn_as_deleted_file` | 5 | Pass |
| 5. `GIT binary patch` | `ac10_git_binary_patch_is_skipped_as_binary`, `ac10_git_binary_patch_leaves_the_prompt` | 2 | Pass |
| 6. Tightened table | `ac11_code_named_like_docs_or_generated_is_not_skipped` and `..._is_reviewed` (`CHANGELOG.py`, `LICENSE.js`, `x.generated.py`, `api/client.generated.ts`); the table gains `LICENSE.md` and `CHANGELOG.rst`, keeps `CHANGELOG`, and loses `api/client.generated.ts` | 8 new; table 25 | Pass |
| 7. P0-S1 AC11's new text | `test_p0_s1_ac11_file_at_the_limit_reaches_the_provider_whole`: already a real diff exactly `--max-diff-bytes` long, all of it reaching the provider, exit 1. Only its comment changed | 3 | Pass |

- **Item 1's plain form was first pinned as the developer's "residual risk",** then flipped by the main session's decision in the spec's Notes: a plain unified diff skips no files. The flipped tests require `DROP TABLE users;` in the prompt, the whole input in `reviewable_text`, and no skipped file. They don't pin how many files the plain form splits into, since the decision is about skipping, not splitting.
  - **Evidence against the earlier code:** the 00:12:15 parser passed the old pin, so it skipped the plain-form `README.md` as docs and kept `DROP TABLE users;` out of `reviewable_text`. The two flipped tests and the README case assert the opposite. Unlike the other items, they weren't rerun against a rebuilt version, because the developer's change landed (00:21:48) before they could run.
  - **Every other skip case uses `diff --git` input,** so the decision doesn't touch them: `section()`, `deleted()`, `BINARY` and `GIT_BINARY` all start with a `diff --git` line. The plain-form inputs (AC2, AC3, AC4 and the property test) all use `.py` paths and assert no skip reason, so they don't depend on the decision.
- **Item 3's bound is 1 s,** for the parse alone, timed inside the child. The fixed parser takes about 1 ms on each input, so the margin is about 1000 times. The child is killed after 60 s. A first bound of 5 s was too loose: a quadratic scan that only copies each suffix took 4.5 s at 400 KB and passed it.
- **Moving `MINIMAL_DIFF` to a root `tests/` module wasn't cheap.** The developer's unit tests import it as `diff_samples` from `tests/unit`, and the tester can't edit them. It stays where it is.

**Would they fail on the unfixed behaviour?** Yes, every new test of fix round 1. The plain-form tests were then flipped (see above). No copy of the unfixed parser survives: the Git index holds only an intent-to-add entry. So each pre-fix behaviour was rebuilt in memory around the fixed parser, from the probe's results. A temporary driver in `tests/`, deleted after use, patched `parse_diff` in `diff_parser` and `review`. For item 3 it prefixed the child's code instead. A no-op control passed all 110 non-timing cases.

| Unfixed behaviour, rebuilt | New tests that fail |
|---|---|
| `diff --git` lines ignored for file starts (old splitting) | 12 new: all 4 of items 1 (`diff --git` form) and 5, item 2's 2 `main` cases, 2 of item 4's, and item 6's 4 prompt cases. Also 22 older cases, since this wrong version breaks most prompts |
| `int()` on a 5000-digit number raises `ValueError` | All 4 of item 2 |
| A quadratic scan of long lines: suffix copy, and copy plus count | Both of item 3: 2.3 s and 4.2 s, then 55 s and 25 s, against 1 s |
| Deleted files not skipped | 4 of item 4. `ac11_deleted_code_file_is_still_reviewed` rightly passes; it guards against over-skipping |
| `GIT binary patch` not binary | Both of item 5 |
| The old table: `CHANGELOG*` and `LICENSE*` at any extension, `*.generated.*` | All 8 of item 6's new reviewed cases. `LICENSE.md` and `CHANGELOG.rst` were skipped before too, so they guard against over-loosening |

### Coverage

The whole suite covers 100% of statements and branches, `diff_parser.py`, `review.py`, `cli.py` and `errors.py` included. This slice's in-process tests count toward it, alongside the developer's unit tests. The two parse-time children aren't measured.

### Limits

1. **AC9's "then a count"** could mean the unnamed paths (2) or all of them (12). The test accepts either number after the tenth path, and doesn't pin the separator or the order beyond "the first 10 in diff order".
2. **A `diff --git` line with no hunk** (`diff --git a/x.py b/x.py\n+marker\n`, used by P0-S1 AC12 and AC13) is accepted today. AC5 says "no file header", and the spec doesn't say whether `diff --git` alone counts as one, so this isn't pinned.
3. **Not pinned, because the spec doesn't say:** removed lines' `new_line`, `has_count_mismatch` for a header with no counts, `Hunk.header`'s exact text, the case of paths (`README.MD`), and a pure rename with no hunks.
4. **AC4's plain unified diff has no timestamps.** GNU `diff -u` writes `--- a.py\t2024-01-01 ...`, which isn't exercised.
5. **AC5 and AC9 use `--provider mock` only.** A Claude run over a non-diff or an all-skipped diff isn't run.
6. **The shared constant crosses into `tests/unit`.** If `tests/unit` gains an `__init__.py`, or the developer moves `diff_samples.py`, `diff_support.py` must follow.
7. **Hypothesis writes `.hypothesis/`** in the repository root. It is ignored by Git.
8. **Run on Windows only** by the tester. CI hasn't been checked.
9. **Plain unified diffs skip nothing** (the main session's decision). A plain diff of docs or lock files now costs tokens. Whether the faked header still splits the file in the plain form isn't pinned.
10. **Parse time is checked for two crafted lines only,** at one size. Other long-line shapes, such as hunk headers and quoted `---` paths, aren't timed.

## P1-S3: Stop a diff with a detected secret from reaching the model

Spec: [p1-s3-secret-block.md](slices/p1-s3-secret-block.md). Tests: [`tests/integration/test_p1_s3_secret_block.py`](../../../tests/integration/test_p1_s3_secret_block.py), 144 cases after review round 1 (124 before it), all in-process: `main` with an injected `MockProvider`, and `scan_secrets` and `redact` called directly.

**Status: every criterion passes.** The run was against the uncommitted working tree on base `c1afc69`, so a later `src/` change needs a re-run. The tests were written from the spec and design before the developer's code existed. When it first ran, `secret_scan.py` existed but wasn't wired in: the 74 direct and near-miss cases passed and the 47 cases that need exit 16 failed. After the developer wired `review.py` (last `src/` change 07:17:40), all passed. Only `review.py`'s import of `scan_secrets` was read afterwards, to know where to patch it for the wrong-code checks.

**No secret is committed.** Every value is assembled at run time: prefixes are split (`"AK" + "IA"`, `"sk" + "-ant-"`, `"-----BEGIN " + "RSA " + "PRIVATE" + " KEY-----"`), and bodies come from a stride over an alphabet, so no two neighbours repeat and no placeholder word appears. gitleaks 8 (the pre-commit copy, with `--ignore-gitleaks-allow`, as CI runs it) finds nothing in the file, and it found a positive control. P0-S1 AC17's `sk-` pattern finds nothing in it either.

### Gate

| Check | Result |
|---|---|
| `ruff format --check .` | Pass |
| `ruff check .` | Pass, at 07:21. At 07:19 it failed on the developer's `tests/unit/test_secret_scan.py` (two `RUF001`, a literal U+2028), since fixed |
| `mypy` (strict) | Pass |
| `uv run pytest --cov -q`, whole suite, after review round 1 (07:40, after the developer's last `src/` change at 07:37:13) | 1389 passed, 12 skipped (`/dev/full`, Windows), 100% coverage (710 statements, 132 branches); 110.37 s, with all 144 P1-S3 cases |
| `git add -A && pre-commit run gitleaks --all-files`, after review round 1 | Pass |
| The whole suite, before review round 1, at 07:24 | 1350 passed, 12 skipped (`/dev/full`, Windows), 100% coverage (707 statements, 130 branches); 109.42 s. That was at 122 P1-S3 cases; the 2 cases of the removed line that starts with `V` were added after it and pass in the file's own run (124 passed) |
| gitleaks over `tests/` | Pass at 07:26. At 07:20 it failed on the developer's unit tests, since fixed: see For the developer below |
| `pre-commit`, `pip-audit` | Not run by the tester |

### Criteria

Test names drop the `test_p1_s3_` prefix.

| AC | Test | Cases | Result |
|---|---|---|---|
| AC1 | `ac1_vendor_kind_on_an_added_line_exits_16_naming_kind_and_line` | 5 (AWS, GitHub `ghp_`, Anthropic, OpenAI, Slack `xoxb-`) | Pass |
| AC1 | `ac1_private_key_on_added_lines_exits_16_naming_kind_and_line` | 1 | Pass |
| AC1 | `ac1_assigned_credential_on_an_added_line_exits_16_naming_kind_and_line` | 1 | Pass |
| AC1 | `ac1_assignment_form_with_a_digit_is_an_assigned_credential` | 4 (JSON, typed, YAML, YAML with trailing comma) | Pass |
| AC1, from Interfaces | `ac1_interfaces_form_is_detected_as_its_kind` | 9 (`gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`, `xoxa-`, `xoxp-`, `xoxr-`, `xoxs-`) | Pass |
| AC1, from Interfaces | `ac1_credential_name_forms_are_assigned_credentials` | 5 (`DB_PASSWORD`, `self.api_key`, YAML `client-secret`, `APIKEY`, `passwd`) | Pass |
| AC1 | `ac1_crlf_diff_names_the_same_diff_line` | 1 | Pass |
| AC1, Interfaces | `ac1_interfaces_example_line_is_exact`: AWS at diff line 12, PEM at 40, the Interfaces line byte for byte | 1 | Pass |
| AC1 | `ac1_scan_secrets_returns_kind_and_line_but_never_the_value`, `ac1_scan_secrets_finds_nothing_in_a_clean_diff` | 2 | Pass |
| AC2 | `ac2_value_anywhere_in_the_file_exits_16_without_the_value` | 26 (13 places × AWS, GitHub; the 4 places from review round 1 are below): added, removed, removed with `V` right after the `-` marker, context, commit preamble, `+++ b/` path, `README.md`, `uv.lock`, `@@ garbage @@` | Pass |
| AC3 | `ac3_seven_hits_name_the_first_five_and_the_total` | 1 | Pass |
| AC4 | `ac4_non_diff_text_with_a_secret_exits_16_not_15` | 1 | Pass |
| AC4 | `ac4_secret_in_an_oversized_diff_exits_6` | 2 (`V` inside the first 64 bytes; `V` past them), `--max-diff-bytes 64` | Pass |
| AC5 | `ac5_threshold_critical_still_exits_16` | 1 | Pass |
| AC6 | `ac6_near_miss_is_reviewed_normally` | 16, every near miss in the criterion | Pass |
| AC6 | `ac6_near_miss_gives_no_hit` (`scan_secrets` returns no hit) | 16 | Pass |
| AC6 | `ac6_bare_task_queue_name_is_reviewed_normally` | 1 | Pass |
| AC6, from Interfaces | `ac6_interfaces_boundary_is_reviewed_normally` | 19 (see below) | Pass |
| Notes | `notes_type_like_string_with_a_digit_in_a_multiline_list_exits_16` | 1 | Pass |
| AC7 | `ac7_redact_replaces_each_vendor_value` | 7 (the five vendor kinds, `github_pat_`, `xoxp-`), each value twice | Pass |
| AC7 | `ac7_redact_replaces_an_assigned_credential_value_only` | 4 (AC1's four forms) | Pass |
| AC7 | `ac7_redact_replaces_a_whole_pem_block_with_one_marker`, `..._in_added_lines_...` | 2 | Pass |
| AC7 | `ac7_redact_pem_block_without_end_runs_to_the_end_of_the_text`, `ac7_redact_pem_end_without_begin_runs_from_the_start_of_the_text` | 2 | Pass |
| AC7 | `ac7_redact_returns_text_without_a_secret_unchanged` | 4 (empty, plain diff, all near misses, CRLF) | Pass |

No failures to reproduce.

### Review round 1: the amended spec

The spec's criteria and Interfaces changed in review (marked "changed in review, main session, 2026-11-06"). The tests below were written from the amendments while the developer fixed the code. **Against the code before the fix, 17 of the 20 new cases failed**, every one except the three that check an existing rule from a new angle (the `diff --cc` YAML place under both kinds, and the private key with no boundary). **After the fix (07:37), all 144 pass.**

| Amendment | Test | Cases | Before the fix | After |
|---|---|---|---|---|
| AC1: the other quote inside a literal | `ac1_literal_holding_the_other_quote_is_an_assigned_credential` | 2 (a double-quoted literal holding `'`, a single-quoted one holding `"`) | Fail | Pass |
| AC2: `diff --cc` removed line (`--V`) | `ac2_value_anywhere_...[combined_removed_line-*]` | 2 | Fail | Pass |
| AC2: `diff --cc` added YAML line (`++password: "V"`) | `ac2_value_anywhere_...[combined_added_yaml_line-*]` | 2 | Pass (the vendor value is found anyway) | Pass |
| AC2: the same, with a credential-only value, so only the "first on its line" rule can find it | `ac2_combined_added_yaml_credential_counts_as_first_on_its_line` | 1 | Fail | Pass |
| AC2: `+-V` | `ac2_value_anywhere_...[added_line_starting_with_a_hyphen-*]` | 2 | Fail | Pass |
| AC2, from the coordinator: ` -V` (a context line) | `ac2_value_anywhere_...[context_line_starting_with_a_hyphen-*]` | 2 | Fail | Pass |
| Interfaces: the single-hit line, byte for byte | `ac1_interfaces_single_hit_line_is_exact` | 1 | Fail | Pass |
| Interfaces: placeholder words in lowercase only | `ac1_placeholder_word_not_in_lowercase_is_still_detected` (`XXX`, `Example`, `YOUR` inside a token) | 3 | Fail | Pass |
| Interfaces: PGP private key | `ac1_pgp_private_key_block_is_a_private_key` | 1 | Fail | Pass |
| Interfaces: no prefix boundary for `private-key` | `ac1_private_key_line_has_no_prefix_boundary` (BEGIN right after a letter) | 1 | Pass | Pass |
| AC7: PGP redacted as one block | `ac7_redact_replaces_a_whole_pgp_block_with_one_marker` | 1 | Fail | Pass |
| AC7: the other quote, redacted | `ac7_redact_replaces_a_literal_holding_the_other_quote` | 2 | Fail | Pass |

Earlier lowercase placeholder cases (`example`, `placeholder`, `xxx` in `ac6_interfaces_boundary_is_reviewed_normally`, and AC6's own near misses) still pass, so the change didn't loosen them. **The CR-only Known limit isn't pinned.** The Notes describe it as a limit, not a required behaviour, so a fix that detected YAML credentials in a CR-only file shouldn't fail a test.

**What every exit-16 case asserts** (`assert_blocked`): exit 16, empty stdout, `provider.requests == []`, stderr exactly one line starting `review-bot: error: `, no value (`V`, and for PEM the BEGIN, body and END lines) in stdout or stderr, and no diff path in the line. "Diff-file line number" is the 1-based line of the file, by `\n`. Each AC6 near miss is "reviewed normally" when the run exits 1 (the mock's canned finding), the provider got one request, and the line is in its prompt.

**AC6's boundary cases from Interfaces:** AKIA with 17 characters, after a letter, after `_`; a `ghp_` token of 37, or after `-`; `sk-` with no digit, or after a letter; `xoxb-` with 9; a 15-character credential; a credential that starts `http://`, or holds `example`, `placeholder`, `xxx`, `<`, or `${`; the name `tokens`; the name `mytoken`; an unquoted `token:` mid-line; and a digit-bearing type string annotating `token` in a one-line parameter list.

### Would the tests fail on wrong code?

**All 6 kinds of wrong code are caught,** and a control passes. A driver on stdin ran pytest in-process over the 84 `main` cases and patched `review.scan_secrets`. No repository file was changed.

| Wrong code | Failing cases |
|---|---|
| No scan (`scan_secrets` returns `()`) | 47 of 84: every exit-16 case. AC4's size cases rightly pass |
| Scan after the parser, on `reviewable_text` (15 on a non-diff) | 5: AC2's `README.md` and `uv.lock` (both kinds), AC4's non-diff |
| Scan of added lines only | 9: AC2's removed, context, preamble and path (both kinds), AC4's non-diff |
| Every hit's line one too high | Every case that names a line, AC1's exact line included |
| Only the first 5 hits returned | 1: AC3, by its total |
| Control: the real scan | 0 of 84 |

The direct `scan_secrets` and `redact` cases weren't run against wrong code.

### Coverage

The whole suite covers 100% of statements and branches, `secret_scan.py`, `review.py`, `cli.py` and `errors.py` included. These in-process tests count toward it, alongside the developer's unit tests.

### For the developer

**Resolved by the developer by 07:26: `tests/unit/test_secret_scan.py` failed CI's gitleaks step. Severity `blocker` while open.** CI scans the whole tree with `--ignore-gitleaks-allow` and no allowlist, before the tests run. gitleaks 8's `generic-api-key` rule flagged four near-miss lines in it (99, 109, 110 and 112): quoted literals of 16 or more characters, each after a credential-like name. Near misses are not exempt: gitleaks doesn't know they're near misses. **Repro:** `gitleaks dir tests/unit --redact --no-banner --ignore-gitleaks-allow -v`. **Fix direction:** assemble those literals at run time, as this file does.

**Not an AC, for the reviewer:** with one hit the line read, at 07:20, `1 possible secret in the diff (...), so it was not sent to the model. Remove them and rotate any that are real`. "Remove them" follows the plural Interfaces example. **Settled in review round 1:** Interfaces now gives the single-hit line, and `ac1_interfaces_single_hit_line_is_exact` pins it.

**The developer's Notes reading, that a line's first `-` is the removed-line marker,** is covered by AC2's new `removed_line_starting_with_the_value` case. Without that reading, `-AKIA...` would fail the "doesn't follow `-`" boundary and reach the model. That case wasn't run against wrong code.

### Readings of this slice's criteria

None blocks the slice; each is a choice the tests make where the spec could be read two ways.

1. **AC6's SHA-256 digest** is tested under a name that isn't a credential name (`checksum = "<hex>"`). Under `token = "<hex>"` the 64-character literal would meet `assigned-credential`'s rule, so the criterion can only mean a non-credential name.
2. **"At a word start or after `_`, `-` or `.`"** is read as applying to the name's ending, so `mytoken = "..."` is a near miss and `DB_PASSWORD` a hit. The developer's unit tests read it the same way. If it means the whole name, `mytoken` should block.
3. **AC7's PEM block ends at `END ... KEY-----`,** keeping the newline after it, so `before\n<block>\nafter\n` becomes `before\n[REDACTED]\nafter\n`. With no `END`, "to the end of the text" takes the trailing newline too, giving `before\n[REDACTED]`.
4. **AC7's assigned credential keeps its quotes:** only the literal's value is `V`, so `password = "V"` becomes `password = "[REDACTED]"`.
5. **AC3's "states the total"** is checked as `7 possible secrets`, the Interfaces line's form, and the 6th and 7th line numbers must not appear.
6. **AC4's "the file was never read whole"** is shown only by the outcome: exit 6 even when `V` sits inside the first 64 bytes, so a scan of a partial read would have given 16. How much was read isn't measured.

### Limits

1. **Hits on the same line, or two kinds overlapping** (an AWS key assigned to `token`), aren't pinned: the spec doesn't say whether that is one hit or two.
2. **Stderr closed or failing** with a hit (the Notes' P0-S3 AC4 rule) isn't run.
3. **A Claude run** with a hit isn't run; every exit-16 case uses the mock. The scan precedes the provider, so it shouldn't differ.
4. **Only two kinds run through AC2's places** (AWS, GitHub); the others run on added lines only.
5. **`redact`'s use on paths** ("every path written anywhere passes `redact`", design.md) belongs to the slices that write paths, and isn't tested here.
6. **Run on Windows only** by the tester. CI hasn't been checked.
7. **The review round 1 cases weren't run against made-up wrong code.** Their evidence is the code before the fix, which 17 of the 20 failed.
8. **CR-only line endings** (the Notes' Known limit) aren't tested either way, as above. A `diff --cc` section with three or more parents isn't run.
9. **A literal holding its own opening quote** isn't tested. The rule says no such literal is a match, but it only shortens the literal, so the result depends on what follows.

## P1-S4: Trace every run in a JSONL audit log

Spec: [p1-s4-audit-log.md](slices/p1-s4-audit-log.md). Tests: [`tests/integration/test_p1_s4_audit_log.py`](../../../tests/integration/test_p1_s4_audit_log.py), 145 cases, all in-process: `main` with an injected `MockProvider` or a raising provider. Two of them use `git` or a child interpreter. Golden output is in [`tests/fixtures/p1_s4/`](../../../tests/fixtures/p1_s4/).

**Status: every criterion passes.** The run was against the uncommitted working tree on base `707afb8`, so a later `src/` change needs a re-run. The tests were written from the spec, its Limits and design.md before the developer's code was wired in. On the first full run, 124 passed and one failed: an unwritable `--audit-log` whose path held a secret echoed that secret on stderr (see below). The developer's next change put the path through `redact`, and then all 125 passed.

**How the tests read records.** The spec fixes the envelope but not the key names inside `data`. The tests look for the values a criterion names, wherever they sit in `data`: a path with its reason, a file with its hunk index and start, `51` twice outside the lists. So they don't depend on key names, except where design.md fixes them: the [paths in `audit/v1`](design.md#audit-log) tests read `skipped_files` and `flagged_hunks` and compare whole entries. Every read checks the whole file: it is pure ASCII, and each line is one `\n`-terminated JSON object holding the envelope. The file must also split into as many `str.splitlines()` lines as records, which catches a raw U+2028 or `\x85`. And no decoded string may hold a surrogate code point, which only a lone `\ud800`-`\udfff` escape produces.

**The suite's own log.** `tests/conftest.py` now points `REVIEW_BOT_AUDIT_LOG` at a session temp file for the whole run, including child processes (spec Notes). The default-path tests unset it and `chdir` into `tmp_path`. `ac4_child_process_default_is_redirected_by_the_suite` checks that a child process gets a path outside the repository.

### Gate

`ruff format --check`, `ruff check` and `mypy` (strict) are all clean. `pytest --cov`: 1571 passed, 12 skipped, 100% of statements and branches. The pre-commit gitleaks hook passes on the new files, because every secret is assembled at run time, as in P1-S3.

### Criteria

| AC | Tests (`test_p1_s4_…`) | Result |
|---|---|---|
| AC1 | `ac1_every_line_is_an_audit_v1_record_of_one_run`, `ac1_start_parsed_called_end_appear_in_that_order`, `ac1_timestamps_never_go_backwards`. Limits: `ac1_limits_hostile_skipped_path_keeps_one_record_per_line` ×10 (newline, CR, ESC and BEL, quotes, backslash, NUL, U+2028, `\x85`, `\x1c`, a forged record) and `ac1_limits_hostile_mismatched_hunk_path_…` | pass (14) |
| AC2 | `ac2_run_start_holds_the_settings_and_the_diffs_hash_and_size` (CRLF and non-ASCII, so bytes ≠ characters), `ac2_run_start_hash_changes_with_one_byte_of_the_diff` | pass (2) |
| AC3 | `ac3_second_run_appends_with_its_own_run_id`, `ac3_appends_to_a_log_that_already_holds_records` (earlier bytes kept, 4 run IDs) | pass (2) |
| AC4 | With neither flag nor variable, the log goes under the working directory. The variable sets the path, and the flag wins over it. An overridden unwritable variable doesn't stop the run. A relative flag resolves under the working directory. The suite redirects child processes | pass (6) |
| AC5 | A skipped file is listed with its reason, and a mismatched hunk by file, index and start. An unreadable header gives `null`, with no header text and no `@@`. The index counts within its file. Each of the 4 reasons comes from the fixed set. 51 of each lists the first 50 in order, plus totals of 51 and 51, and exactly 50 lists all. All skipped gives `nothing_to_review` and no `agent.called`. Paths: `ac5_skipped_path_decoding_byte_377_is_written_in_git_quoted_form` checks that the entry equals AC5's exact JSON, its value is 17 characters, the file is ASCII, and no line holds a `\ud8`-`\udf` escape. The same is checked for a flagged hunk and for the only file (`\377\376`). `ac5_valid_utf8_path_is_plain_with_no_path_quoted_key` ×2 (`docs/café.md` plain, and git-quoted as `\303\251`). Limits: `ac5_limits_non_utf8_path_uses_the_designs_exact_escaping` ×6 (the seven named controls; `"`, `\`, `\001`, `\037` and `\177`; a valid `é` beside `\377`, both written as octal; a truncated sequence; an encoded surrogate `\355\240\200`; an overlong `/`). Deleted files: `ac5_skipped_deleted_file_is_logged_by_its_old_path`, `ac5_flagged_hunk_in_a_deleted_file_is_logged_by_its_old_path`, `ac5_no_file_in_parse_parsed_is_null_for_deleted_files`, `ac5_skipped_deleted_file_with_a_non_utf8_old_path_is_quoted` | pass (26) |
| AC6 | Kinds and lines for two kinds, `secret_detected`, and 16. `V` in a `+++ b/` path. `V` in the diff's own name, with a hit and without one. Limits: an octal-quoted `V` that the raw scan can't see is written as `[REDACTED]`, in a skipped path (aws, github) and in a flagged hunk's path. C-quoting writes a non-UTF-8 path's ASCII bytes as they are, so a `V` there would be readable. `ac6_limits_secret_in_a_non_utf8_skipped_path_is_redacted` ×6 covers aws and github, with `\377` after `V`, before it (so the digit `7` touches it), and beside `\"` and `\\`. The same is checked in a flagged hunk's path and in a deleted file's old path. Each entry keeps `path_quoted: true` and holds `[REDACTED]` | pass (15) |
| AC7 | Inputs 3, 4 (directory, not UTF-8), 5, 6 and 15. Each provider category 7 to 13, with `agent.failed` and no message text. Invalid model output gives 13, with no model text. `RuntimeError("SENTINEL")` gives 14 and `RuntimeError`, and a subclass is named by its own class. A secret in a provider message isn't written | pass (17) |
| AC8 | Findings, no-findings and all-skipped runs: no `SENTINEL-RATIONALE` or `SENTINEL-DIFF`, and stdout and stderr equal the golden output plus ` run_id=<id>`. A below-threshold rationale isn't written. No body line of `sample_diff.txt` is written | pass (5) |
| AC9 | A directory, a parent that is a file, and a grandparent that is a file each give exit 17, one error line naming the path and `--audit-log`, empty stdout and no request. Each still gives 17 with the diff missing, a directory, too large, holding a secret, or not UTF-8. Also: the variable's path, a default `.review-bot` that is a file, a newline in the path (still one line), and a secret in the path (not echoed) | pass (24) |
| AC10 | A record failing at `run.start`, `parse.parsed` or `agent.called` gives 17 with empty stdout. Failing before the call means no request. A failing `run.end` keeps codes 7 to 13, 14, 3, 6, 15 and 16 | pass (16) |
| AC11 | `run.end` failing after stdout gives 17, with stdout equal to the golden output, for findings, no findings and all skipped | pass (3) |
| AC12 | The summary ends with the log's `run_id`, both reviewed and all skipped. Three runs print three IDs, in the log's order | pass (3) |
| AC13 | The default path creates a `.gitignore` holding `*`, and `git status` in a fresh repository shows nothing. An existing directory gets none, and an existing `.gitignore` is left alone. A custom flag path, a custom path inside `.review-bot/` and a variable path get none | pass (7) |
| Notes | A usage error (2) and `--help` write no record | pass (2) |
| Limits | 400 kB at the default limit, half skipped and every hunk mismatched, runs under 30 s (about 1 s here). 5 MB under a raised limit runs under 120 s. Both keep every list at 50 or fewer, and the log under 64 kB. Over the limit exits 6 with no `parse.parsed` | pass (3) |

### Review round 1: the amended spec

The user's decisions of 2026-11-06 changed AC5 and the Limits. A non-UTF-8 path is now written in git's C-quoted form with `path_quoted: true`, and a deleted file is logged by its old path. The tests changed to match:

- The loose `kept_ff` check, which accepted five forms, is replaced by exact entries, including AC5's own JSON. A valid UTF-8 path is plain, with no `path_quoted` key. No line holds a `\ud8`-`\udf` escape.
- New tests: the exact escaping of six hostile non-UTF-8 paths, four deleted-file cases, and eight cases of a secret in a non-UTF-8 path.
- The unused `timed` fixture and its `Iterator` import are removed (reviewer finding).

Against the developer's fixed working tree, `uv run pytest tests/integration/test_p1_s4_audit_log.py -q` gives 145 passed. As asked, this round didn't re-run the full gate or coverage. The Gate section, the coverage figures and the mutant table below come from the first round.

### Checked against wrong code

The slice tests were run against four mutants, applied by a throwaway pytest plugin outside the repository that monkeypatched the implementation. Each was caught:

| Mutant | Failed |
|---|---|
| List 51 entries, not 50 | 3: AC5's 51 and 51, and both size Limits |
| `audit` writes paths without `redact` | 4: the octal-quoted secret cases (AC6) and the secret in an unwritable path (AC9) |
| JSON written with `ensure_ascii=False` | 5: the three `\377` cases (AC5), plus U+2028 and `\x85` (AC1) |
| Every run gets the same `run_id` | 3: both AC3 cases and AC12's three runs |

### How the tests simulate a failing log

AC10 and AC11 need a log that fails partway through a run. On Windows, a file held open can't be made to fail portably. So `fail_record` wraps `AuditLog.record`: from a named `stage.event` on, it raises a genuine `AuditError`, obtained from the public constructor opened on a directory. Each test also asserts that the failing record was attempted. This proves the CLI's handling of a failing `record`, not that `record` turns a real `OSError` into `AuditError`. The developer's unit tests and the 100% branch coverage of `audit.py` cover that.

### For the designer and the main session

1. **Fixed during the slice: an unwritable `--audit-log` echoed a secret held in its path** in the exit-17 line. Repro: `--audit-log <file>/AKIA<16 chars>/a.jsonl`. AC9 asks the line to name the path, and design.md says every path written anywhere passes `redact`. The developer now redacts the path, and `ac9_unwritable_log_with_a_secret_in_its_path_doesnt_echo_it` pins that. If naming the path verbatim was intended, the test and the design disagree, and the designer should settle it.
2. **Outside this slice and still open: P1-S2's "nothing was reviewed" warning prints a decoded secret path on stderr. Severity `major` for P1-S2 and P1-S3.** A skipped file whose quoted path is octal escapes (`"b/docs/\101\113\111\101….md"`) hides `V` from the raw-text scan. The parser decodes the path, and the warning prints `docs/AKIA….md`. The audit log has `[REDACTED]` there. This contradicts design.md's "every path written anywhere passes `redact`". This slice's tests check only the log, so they don't fail on it. **Repro:** that diff, alone, with `--provider mock`. It exits 0, and stderr holds `V`.

### Readings of this slice's criteria

1. **Non-UTF-8 paths** (AC5) were first read loosely, accepting `\udcff`, `\377` or `\xff`. The user's decision of 2026-11-06 fixed a single form, so the tests now compare whole entries against design.md's C-quoting, and a `\udcff` escape fails them.
2. **"Totals of 51 and 51"** is checked as at least two values of `51` reachable through `data`'s mappings, outside its lists.
3. **AC8's "as without the log"** is pinned by golden files captured from `main`'s code (`707afb8`) before this slice changed `src/`.
4. **AC9's "missing parent that can't be created"** is a parent or grandparent that is a regular file. A read-only directory isn't used, because Windows doesn't honour that for directories.
5. **The secret in an unwritable path** (AC9) and **the secret in a provider message** (AC7) go beyond the criteria. They rest on design.md's rule that every path passes `redact`, and on the Limits' "exception text … never written".

### Limits

1. **A real disk failure partway through a run** isn't produced. See "How the tests simulate a failing log".
2. **Claude runs aren't run with the log.** Every case uses the mock or a raising provider. For `agent.called`, only the record's presence is checked, not its tokens or duration.
3. **"No rescan of the diff per record"** (Limits, cost) is shown only by the time bounds, which have wide headroom. Linear growth isn't measured.
4. **Concurrent runs appending to one file** aren't tested. The spec promises nothing for them.
5. **Run on Windows only** by the tester. CI on Linux hasn't been checked, and the `\377` and hostile-path cases may behave differently there.

## P1-S19: Stop a secret spelt in a quoted path's escapes from reaching stderr or the model

Spec: [p1-s19-decoded-path-secret.md](slices/p1-s19-decoded-path-secret.md). Tests: [`tests/integration/test_p1_s19_decoded_path_secret.py`](../../../tests/integration/test_p1_s19_decoded_path_secret.py), 70 cases, all in-process: `main` with an injected `MockProvider`. AC11 also calls `parse_diff` and `scan_secrets` as its oracle. Every `V`, and its escaped form `E(V)`, is assembled at run time.

**Status: every criterion passes** against the developer's uncommitted working tree on base `0e3aeae`, so a later `src/` change needs a re-run. The tests were written from the spec before the developer's code could be imported. Run against a clean worktree of `0e3aeae` (before the fix), 58 of the 70 fail, each for the expected reason: the run isn't blocked, or a path isn't redacted. The 12 that pass there are the criteria that hold already: AC5, AC6's raw-hit case, the generator guard, malformed quoting, and the cost bounds.

### Gate

`ruff format --check`, `ruff check` and `mypy` (strict) are clean. `pytest --cov`, re-run after review round 1 against the developer's finished working tree: 1713 passed, 12 skipped, 0 failed, 100% of statements and branches. The first run's one failure, a 0.5 s unit bound that flaked under load, is fixed (see "For the developer").

### Criteria

| AC | Tests (`test_p1_s19_…`) | Result |
|---|---|---|
| AC1 | `ac1_issue_24_repro_exits_16_naming_three_hits` (the exact P1-S3 line for lines 1, 1 and 5), `ac1_quoted_repro_line_equals_the_plain_paths_line` | pass (2) |
| AC2 | `ac2_quoted_path_anywhere_in_the_header_exits_16_naming_its_line` ×24: both `diff --git` paths, `diff --cc`, `---`, `+++`, rename from and to, copy from and to, both `Binary files` paths, and a plain unified diff, each for a reviewed `src/…py` file and a skipped `docs/…md` one. Each checks that every hit names that line, and that `V` and `E(V)` are on neither stream. Also `ac2_reviewed_file_src_path_exits_16`. Beyond the list, `ac2_beyond_quoted_path_exits_16_naming_its_first_line` ×4: `diff --combined`, an unknown header line (`note "…"`), the quoted path in a second file, and CRLF endings | pass (29) |
| AC3 | `ac3_fully_escaped_form_of_each_kind_exits_16_naming_it` ×7: every byte of a form of each kind, including a PEM `BEGIN` line and `token = "L"`, escaped in a skipped file's `+++` path | pass (7) |
| AC4 | `ac4_hit_names_the_line_holding_the_quoted_path` ×3: only the first byte escaped, `\377` before `E(V)`, `\n` before `E(V)`. Each has exactly one hit, on the `+++` line | pass (3) |
| AC5 | `ac5_clean_decoded_path_in_a_skipped_file_is_p1_s2_ac9` ×2 (`caf\303\251`, `AKIA` plus 15): exit 0, `No findings at or above low.`, `model_called=false`, and the warning names exactly the decoded path, `docs/café.md (1 in total)` and the near miss's `docs/AKIA….md (1 in total)`, so a warning that redacted every path would fail. `ac5_clean_decoded_path_in_a_reviewed_file_is_reviewed` ×2: one request holding the added line | pass (4) |
| AC6 | `ac6_raw_hit_alone_is_named_when_a_path_also_holds_one` (`1 possible secret`, line 5 only), `ac6_without_the_raw_hit_the_paths_hits_are_named` (lines 6, 6 and 10) | pass (2) |
| AC7 | `ac7_records_match_a_raw_hit_and_hold_no_value`: exactly `run.start`, `static.secrets`, `agent.skipped`, `run.end`, with no `parse.parsed`. `static.secrets` equals the plain-path run's, `secret_detected`, 16. The file holds neither `V` nor `E(V)`, raw or JSON-escaped | pass (1) |
| AC8 | `ac8_end_marker_path_is_redacted_from_its_start_in_the_warning`: exit 0, and the warning names `[REDACTED].md`, with no `docs/` and no `PRIVATE` | pass (1) |
| AC9 | `ac9_command_line_path_holding_v_is_redacted_in_the_error_line` ×6 (2: stray extra argument; 3: missing; 4: directory; 5: empty; 6: `--max-diff-bytes 8`; 15: prose): one error line holding `[REDACTED].diff`, and no `V` | pass (6) |
| AC10 | `ac10_parsers_pick_of_the_new_path_is_scanned`: `diff --git "x" "y\" "b/src/E(V).py"`, exit 16, hits on line 1 only | pass (1) |
| AC11 | `ac11_property_yielded_path_with_a_detected_form_always_exits_16` (Hypothesis, 300 examples, derandomised). It generates nine header shapes with a closed, unclosed or bare token: `V` plain, fully or partly escaped, beside `\377`, `\n`, `\t`, `\q`, `\8`, `\400`, `\"`, a space or a trailing `\`. Whenever a yielded `path` or `old_path` scans as a hit, the run must exit 16 with no request. `ac11_generator_yields_paths_with_detected_forms` checks that at least 20 of 200 examples reach that assertion | pass (2) |
| AC12 | `ac12_unquoted_rename_to_path_exits_16_naming_its_line` ×2 (`rename to -V.md`, `rename to password: "L".md`). Each first asserts that `scan_secrets` misses the raw text, then that line 4 is named | pass (2) |
| Limits | Escapes keep their character: `\q`, `\8` and `\400` complete a form that the raw text lacks (×3, exit 16). Malformed quoting: no closing quote, and a trailing lone `\`, don't fail the run (×2). Cost: a ~400 KB header line of `\101`, of unclosed `\"` and of `" "` strings (×3), and the `\101` line ending in `/E(V)` (exit 16), each under 20 s; 10,000 files with quoted paths under 60 s (exit 0, `skipped_files=10000`). Measured here: 0.1 to 0.15 s for each 400 KB line, about 1 s for 10,000 files | pass (10) |

### P1-S4's tests changed to match

P1-S4's `ac6_limits_…` tests expected exit 0 for an octal-quoted `V` in a skipped path, a flagged hunk's path, three non-UTF-8 paths and a deleted file's old path, with `[REDACTED]` in `parse.parsed`. P1-S19 AC2 and AC4 make each of those exit 16 before any parse record, so those eight tests would fail once this slice lands. In `tests/integration/test_p1_s4_audit_log.py`:

- They are now one parametrised test, `ac6_limits_octal_quoted_secret_path_is_blocked_and_not_written` ×12 (6 shapes × aws and github). It checks exit 16, no request, no `V` in the log, no `parse.parsed`, and `run.end` with 16.
- P1-S4's Limits rule that a path passes `redact` before it's written is still covered, now with an input that reaches `parse.parsed`: an `END` marker alone, which `redact` replaces but the scan doesn't flag (P1-S19 AC8). `ac6_limits_end_marker_in_a_skipped_path_is_redacted` ×2 compares whole entries: `[REDACTED].md`, and `"[REDACTED]\377.md"` with `path_quoted: true`. `ac6_limits_end_marker_in_a_flagged_hunk_path_is_redacted` covers a hunk's path.

Before the fix, the 12 blocked cases fail and the 3 `END` cases pass. After it, all 15 pass.

### Would the tests fail on wrong code?

The run before the fix is the main check: 58 failures cover every criterion except AC5, which holds by design. Some wrong implementations a reviewer might worry about:

- **Scanning decoded paths even after a raw hit** would double-count. `ac6_raw_hit_alone…` asserts one hit.
- **Only scanning the parser's paths** misses `diff --git`'s old side, `rename from`, `copy from` and `Binary files`' old side (AC2), and the unknown header line.
- **Only scanning C-quoted strings** misses AC10's pairing and AC12's unquoted paths.
- **Naming the decoded line after an escaped `\n`** fails AC4's exact `[(aws-access-key, 4)]`.
- **Dropping the backslash but not keeping the character** (`\q` gives nothing) fails the `\q`, `\8` and `\400` cases.

### For the developer

1. **Flaky unit bound, fixed in review round 1.** `tests/unit/test_diff_parser.py::test_parse_diff_reads_a_long_hostile_header_line_in_linear_time` asserted under 0.5 s, and `[many-short-strings]` failed in a full `pytest --cov` run on Windows while passing alone. The bound is now 5 s, which still catches quadratic work on a 400 KB line.

### Readings of this slice's criteria

1. **"Naming the diff line that holds the quoted path"** (AC2, AC4) is read as every hit naming that line. Where `V` sits in more than one string (AC1, the second-file case), only the first line is checked against the spec's list.
2. **AC8's warning** is checked to start with `[REDACTED].md`. The count suffix is P1-S2's choice.
3. **AC9's stray argument** goes after a valid diff path, with `--provider mock` after it.
4. **AC11's "holds a detected form"** is `scan_secrets(path)` returning a hit. `parse_diff` may yield a path holding a lone surrogate for a non-UTF-8 byte (P1-S2 Interfaces), and that's what's scanned.
5. **Malformed quoting** (Limits) only promises that the run doesn't fail. With no closing quote, the parser may or may not yield `V`, so exits 0, 1 and 16 are all accepted, and `V` must not reach stderr.

### Limits

1. **Escaped secrets in hunk lines and the preamble** are out of scope and aren't tested.
2. **Cost is shown by time bounds only**, with wide headroom. Growth isn't measured.
3. **Run on Windows only** by the tester. CI on Linux hasn't been checked.

## P1-S5: See provider warnings and failure diagnostics in the audit log

Spec: [p1-s5-provider-notices.md](slices/p1-s5-provider-notices.md). Tests: [`tests/integration/test_p1_s5_provider_notices.py`](../../../tests/integration/test_p1_s5_provider_notices.py), 122 cases, and [`tests/conformance/test_p1_s5_notices_conformance.py`](../../../tests/conformance/test_p1_s5_notices_conformance.py), 26 cases. All run in process: `main` with an injected `MockProvider`, or `ClaudeAgentProvider` over a fake `sdk_call`. The fake plays the CLI's stderr through `options.stderr`, using the new `cli_stderr` step in [`tests/fake_sdk.py`](../../../tests/fake_sdk.py). AC1's warning text is a golden file in [`tests/fixtures/p1_s5/`](../../../tests/fixtures/p1_s5/), taken from `main`'s code (`a0759ca`). One case starts a child interpreter. Every secret is assembled at run time.

**Status: every criterion passes, 148 of 148, and review round 1 is resolved.** During review, the new AC9 Limits case failed until the developer made diff matching near-linear (see Review round 1). The first run had 143 of 145: two cases failed from one regression in the developer's code, found by a Limits test (see below), and later changes fixed it. The tests were written from the spec, ADR 0004 and design.md, not from the code.

**How the tests read the log.** They reuse P1-S4's `read_records`, so every read checks that the file is pure ASCII, holds one record per line and has no lone surrogates. The spec doesn't name the keys inside `agent.failed.data`, so the kept diagnostics are taken as the one list of strings in that record. `scrub_diagnostics` has no module in design.md. The tests import it from `review_bot.audit`, because ADR 0004 says only `audit` reads diagnostics.

### Gate

The tester's run on the developer's final tree after review round 1, with nothing else running: `ruff format --check` and `ruff check` are clean, `mypy` (strict) is clean on 20 files, and `pre-commit run --all-files` is clean. `pytest --cov`: 1908 passed, 12 skipped, 0 failed, 100% of statements and branches, in 154.7 s. This slice's two files: 148 passed. An earlier run by the main session, before the round-1 cases were added, gave 1900 passed. After review round 2 the developer's full run gave 1913 passed, 12 skipped, 0 failed (five new unit cases for the chunk boundary and the memory fix); CI re-runs the suite on the PR.

### Criteria

| AC | Tests (`test_p1_s5_…`) | Result |
|---|---|---|
| AC1 | `ac1_overridden_signal_warns_on_stderr_as_in_phase_0` ×2 (found, none): exits 1 or 0, and the golden line appears exactly once. `ac1_overridden_signal_is_logged_as_agent_warning` ×2. `ac1_response_warnings_hold_the_warning` checks that `warnings` is exactly the golden text. `ac1_no_signal_gives_no_response_warning`. Limits: `ac1_limits_signal_with_a_lone_surrogate_still_exits_by_the_findings` | pass (7); failed on the first run |
| AC2 | `W` is a stderr line and an `agent.warning`. Two warnings are written in order. With no warnings, there's no extra line and no record. A warning never goes to stdout. Beyond the criterion: a hostile warning (ESC, `\n`, a forged `review-bot: error:`, U+2028) stays one escaped line. A warning holding a key is scrubbed in the log, as design.md's "scrubbed text" says | pass (6) |
| AC3 | `ac3_claude_warning_with_stderr_lost_stays_in_the_log` ×8 (found or none, with stderr `None`, closed, failing on write or failing on flush): exit by the findings, stderr empty, record kept. `ac3_mock_warning_with_stderr_lost_stays_in_the_log` ×4 | pass (12) |
| AC4 | An AST scan of each `providers/*.py` finds no import of `console`, no `print()` and no `sys.stdout` or `sys.stderr` (×3). Importing the three provider modules in a child process doesn't load `review_bot.console`. With `capfd`, `ClaudeAgentProvider.review` writes nothing at file-descriptor level for a warned success, an error result or a raised `ResultError`, all with CLI stderr. `MockProvider` with warnings writes nothing either | pass (9) |
| AC5 | Over a raised `ResultError`, an error result and the deadline (`--deadline-s 0.1` on a hung stream, inside a 30 s bound): `CLI-SENTINEL`'s line is in `agent.failed` (×3). The line is on neither stream, stdout is empty and stderr is one error line (×3). The code and the stderr bytes equal a run without CLI output (×3). The `ProviderError` carries the raw lines. Lines are kept in the order written | pass (11) |
| AC6 | CLI text before a successful result, before a warned success, and after the result: none of it is in the log, and there's no `agent.failed` | pass (3) |
| AC7 | 50 lines keep exactly the last 20, and none of the first 30. Exactly 20 are all kept. The total is 2,000 characters or fewer for one 10,000-character line, 50 of them, and 20 lines of 302. A key straddling the cut, placed for a cut 2,000 characters from the end, 2,000 from the start, or 100 from the end of a line: no 8-character piece of it is kept (×3). A 42-line private-key block of about 2,700 characters, longer than both caps, leaves no `PRIVATE` and no 8-character piece of its body. Limits: 200,000 CLI lines finish within 30 s (0.15 s here), and the last line is kept | pass (10) |
| Interfaces | `interfaces_provider_holds_at_most_the_last_1000_lines`: after 5,000 lines, `diagnostics` holds between 20 and 1,000 lines, and they are the input's tail | pass (1) |
| AC8 | In the log and through `scrub_diagnostics`: an AWS key, a GitHub token, an Anthropic key, a private-key block split over lines, and a tail holding only the body and the `END` line all give `[REDACTED]` and leave no value (×10). Each of the three credential variables set to `test-key-sentinel` is redacted in the log and by the function (×6). Each set to an empty value leaves the line exactly unchanged, in the log and by the function (×6). Notes: with all three set, no value is in the log | pass (23) |
| AC9 | `ac9_cli_line_quoting_the_diff_is_replaced_whole` ×11, by the function and ×11 in the log, where the line next to it is kept. The cases: an added, removed or context line without its marker, with its marker, without leading whitespace (added and removed), and JSON-escaped with `\"` (with and without whitespace), `\\` and `\n`, plus a line of exactly 8 characters. In the log, no word of the diff survives. Lines under 8 characters aren't matched, by the function (×6) and in the log. Among them, `total=1` is 7 characters, 8 with its marker and 10 with its outer whitespace. It isn't matched alone or quoted with its marker and whitespace, as the spec Notes count without both. Limits: whitespace-only diff lines (12 spaces, 8 tabs) match nothing. Only the quoting line of three is replaced. Scrubbing 1,000 lines against an 8,000-line diff, and 200 KB lines of `\`, `\"`, `-----` and `a`, each finish within 30 s (0.3 s at most here). Review round 1: `ac9_limits_large_diff_and_long_cli_lines_add_bounded_time_to_a_failed_call` | pass (34) |
| AC10 | The `d` sentinel is in `agent.failed`, and not on stderr or stdout. Stderr is one error line. The spec's literal `d` is kept as `["d"]`. The orchestrator scrubs a mock's diagnostics: a key becomes `[REDACTED]`, and a line quoting the diff is removed. Hostile diagnostics (`\n`, ESC, U+2028, `\x85`) keep the log valid. The message is byte-for-byte the same as without diagnostics. Beyond the criterion: `ac10_beyond_criterion_lone_surrogate_in_diagnostics_is_escaped` | pass (6); failed on the first run |
| AC11 | The P0-S3 suite still passes for both providers (55 conformance cases in all). New cases, each for both providers: a plain success has no warnings (×2), a warned success holds one `str` warning, every failure category's `diagnostics` holds only `str` (×7), a failure with CLI output carries it as `str`, and invalid output holds only `str`. Also: both fields default to empty, and a list of diagnostics keeps its order. The conformance `conftest.py` gains a `warned` and a `diagnosed` scenario for each provider | pass (26) |

### Review round 1 (resolved)

Two reviewer items changed the tests:

- **Nit:** the conformance case no longer copies the `diagnosed` scenario's CLI line. It takes it from a new `diagnostic_line` fixture in `tests/conformance/conftest.py`.
- **New Limits case, which stays red until the developer's fix lands:** `test_p1_s5_ac9_limits_large_diff_and_long_cli_lines_add_bounded_time_to_a_failed_call`. The earlier bounded-time cases paired long lines with a small diff, or a big diff with short lines. This one has both, end to end, on a failing call. The diff is about 390 KB of 9,000 distinct added lines, under the default `--max-diff-bytes`. The CLI writes 20 lines of 20,000 characters, each full of near misses that share a prefix with many diff lines. The bound is 5 s on the time the CLI lines add, measured against the same run with one short line, so parsing the diff doesn't count.
- **Result:** before the fix, the long lines added 15.8 s (16.0 s against 0.2 s), and the case failed. The developer made diff matching near-linear, and the case now passes, taking about 0.4 s in all. Its first version also had a bug of its own: both runs wrote to one log, but the helper expects exactly one `agent.failed` record. The log is now deleted between the two runs.

After the fix, this slice's files give 148 passed, and the full suite is green (see Gate).

### First-run failure, since fixed: a lone surrogate in a warning or a diagnostic turned the run into exit 14

**Severity `major` when found: a regression against AC1 and AC5, reachable from real CLI output.** It passes on the latest tree, and these notes stay so a reviewer can check the fix is deliberate. Before this slice, a warning went only to stderr through `write_diagnostic`, which escapes it. Now it's also written to the audit log, and writing that record failed on a lone surrogate. The run then exited 14 with the generic "did not complete" line. It should exit by the findings (AC1), or with the call's own code (AC5, AC10).

The signal's name in the warning comes from the CLI's JSON `error` field, passed through unchecked (P0-S4 notes). JSON can spell a lone surrogate as `"\udcff"`, so `AssistantMessage.error` can hold one. The CLI's stderr itself can't, because the SDK decodes it strictly. So the diagnostics route is reachable only from a provider that builds diagnostics itself, such as the mock or Phase 4's provider.

**Minimal reproduction.** With `diff` holding `MINIMAL_DIFF`:

```python
fake = FakeSdk(
    assistant_error("rate_limit" + chr(0xDCFF)), result_message(findings_json(HIGH_FINDING))
)
argv = [diff, "--provider", "claude", "--audit-log", log]
cli.main(argv, provider=ClaudeAgentProvider(cli.parse_args(argv)[1], sdk_call=fake))
```

On `main` (`a0759ca`), this returns 1 and prints the finding. On the branch's first run, it returned 14 with empty stdout. Two more cases failed the same way. Passing `MockProvider(error=ProviderError(AUTH, "m", diagnostics=("bad" + chr(0xDCFF) + "byte",)))` to `main` returned 14, not 8. `MockProvider(findings=[], warnings=(that text,))` returned 14, not 0.

**Tests that pin it:** `ac1_limits_signal_with_a_lone_surrogate_still_exits_by_the_findings` and `ac10_beyond_criterion_lone_surrogate_in_diagnostics_is_escaped`. The AC1 case is a plain regression. The AC10 case goes beyond the criterion's text: P1-S4 fixed "no lone surrogates" for paths only, The developer escapes lone surrogates inside `scrub_diagnostics`, so the case is now pinned there. The designer may still want to say in design.md that the rule covers diagnostics and warnings too.

### Readings of this slice's criteria

1. **`scrub_diagnostics`'s module**, which design.md's Interfaces don't name, is `review_bot.audit`. The developer confirmed this, and the direct-call cases import it from there.
2. **AC7's caps:** "the last 20" is checked as an exact tail. "At most 2,000 characters" is the sum of the lengths of the kept strings. Where the cut falls inside a long line isn't specified, so the straddling key is placed for three plausible cuts. The tests don't require a 10,000-character line to keep anything. The developer applies the cap inside `scrub_diagnostics`, after scrubbing, and the tests allow that.
3. **AC9's "8 or more characters"** is counted without the marker and the outer whitespace, as the developer recorded in the spec Notes. The tests pin both sides of the boundary: content of exactly 8 characters is matched, and `total=1` (7 characters, 8 with its marker) isn't, even when the CLI line quotes it with its marker and whitespace.
4. **AC9's `\n` form** can't hide a single diff line, because a diff line holds no newline. It's read as a JSON string joining diff lines with `\n`.
5. **AC8's private-key tail:** the body lines must be gone, and `[REDACTED]` must be present. Whether the `END` line survives isn't asserted in AC8. AC7's longer block asserts that no part survives, markers included.
6. **AC2's "neither"** allows Phase 0's own canned-mock notice and the summary line. Any other line fails the test.
7. **AC10's `d`** is checked both as the literal `d` (kept as `["d"]`, and not a stderr line) and as a sentinel, because the letter `d` appears in every error message.

### Limits

1. **The final full gate was run by the tester** after review round 1 (see Gate).
2. **The SDK's real stderr reader** isn't exercised: the fake calls `options.stderr` directly. P0-S4's real-process tests cover the callback being set.
3. **The 1,000-line memory bound** is shown by the size of `ProviderError.diagnostics` and a time bound, not by measuring memory.
4. **Cost is shown by time bounds only**, with wide headroom. Growth isn't measured.
5. **Run on Windows only** by the tester. CI on Linux hasn't been checked.

## P1-S6: Drop model findings whose file or line isn't in the diff

Spec: [p1-s6-verify-anchors.md](slices/p1-s6-verify-anchors.md). Tests: [`tests/integration/test_p1_s6_verify_anchors.py`](../../../tests/integration/test_p1_s6_verify_anchors.py), 96 cases. Every run goes through `main` with an injected `MockProvider`. The exception is AC11, which uses `--provider mock`'s own provider, once in process and once in a child interpreter. Two AC10 cases are Hypothesis properties with a fixed seed. The fixtures are in [`tests/fixtures/p1_s6/`](../../../tests/fixtures/p1_s6/). `anchors.diff` is the sample diff with its exact hunk headers and bodies, but with `except ValueError:` then `raise`, and `open(path, "w", encoding="utf-8")`, as the spec's preamble asks, so later rule slices don't change these results. `kept_findings.golden.txt` holds the five AC1 lines in Phase 0's format.

**Status: every criterion passes, 96 of 96 after review round 1, on the developer's finished code, and the full suite is green after the handover edits (see Gate).** The tests were written from the spec, design.md's [line anchors](design.md#line-anchors) and its Interfaces, not from the code.

### Gate

The tester ran the gate once, with nothing else running, on the developer's finished tree plus the handover edits below: `ruff format --check .` clean (120 files), `ruff check .` clean, `mypy` (strict) clean on 22 files. `pytest --cov`: 2053 passed, 12 skipped (no `/dev/full` on Windows), 0 failed, 100% of statements and branches, in 194 s. `pre-commit` and `pip-audit` weren't run by the tester. Before the handover edits, 294 integration and conformance cases failed. After the shared-helper changes (items 1 and 2 below), 143 were left.

### Handover edits (spec Notes)

The slice's own tests were left alone. Each earlier test keeps what it protects: a test that needs a finding to print now injects one over a diff holding its anchor, and a test that only needs a run to succeed expects exit 0 and `No findings at or above low.`.

1. **Shared helpers.** `tests/integration/diff_support.py` re-exports `FINDING_FIELDS`, `FINDING_DIFF` and `FINDING_LINE` from the developer's `tests/unit/diff_samples.py`. `Bot.finding` and `fake_sdk.HIGH_FINDING` take their fields from `FINDING_FIELDS`. `claude_argv` writes `FINDING_DIFF`. `Bot.request_for` uses `mock(findings=[])` and asserts exit 0.
2. **P0-S1 AC1** (`test_p0_s1_cli.py`): replaced by P1-S6 AC11. The two cases are renamed `ac1_replaced_by_p1_s6_ac11_…` and expect exit 0 and `No findings at or above low.`. The module docstring says so. `tests/fixtures/p0_s1/canned_finding.golden.txt` had no users left, so it was deleted.
3. **P0-S1 AC4** (`:259` and the provider-variable case): the canned finding is dropped, so they expect exit 0, no findings and the canned notice, which shows the mock ran. The threshold, overridden-variable and `--max-diff-bytes` cases inject `bot.finding()` over `FINDING_DIFF`, so the threshold still changes the outcome. The flag `--max-diff-bytes 100` becomes `1000`, which fits that diff.
4. **P0-S1 AC2, AC3 and the Interface line format** inject over diffs holding their anchors: `FINDING_DIFF`, seven-line sections for `low.py` and `critical.py`, a 17-line `billing/invoice.py`, and a high finding on line 14 of the sample diff for the `--threshold critical` cases. AC3's "findings printed" case now prints a finding again, instead of passing on an empty run.
5. **P0-S1 AC19 and AC20** (`test_p0_s1_safe_output.py`): each hostile `file` is a real diff path. A new `anchored_diff` writes it git-quoted, with octal escapes, in the `diff --git`, `---` and `+++` lines, so the parser decodes it back to the exact value. All 34 cases print and pass. AC20's `docs/→.md` became `docs/→.py`, because a `.md` file is skipped.
6. **P0-S1 AC5 (upper-bound flag), AC10, AC11, AC12 and AC16; P1-S2 AC7 and AC9**: these runs need only to complete, so they use `mock(findings=[])` and exit 0. AC16's offline run keeps the CLI-built mock and checks its canned notice.
7. **P0-S2 AC17** (`test_p0_s2_failures.py`): the ids `would_exit_0_otherwise` and `would_exit_1_otherwise` were no longer true. They're renamed by threshold, with a comment pointing to P0-S3's `closed_with_findings` for the case with a finding.
8. **P0-S3** (`exit_codes`, `safe_stderr`, `stream_failures`): AC2's control, AC4's `findings_1` and AC5's in-process "findings" case inject the default finding over `FINDING_DIFF`. In the child-process cases, the injected `FINDING` moves to `billing/invoice.py:14` on the sample diff. A new `one_finding` provider backs `closed_with_findings`, `findings_1` and AC5's merged-stream case, which now expects the rendered finding instead of the canned golden.
9. **P0-S4** (`messages` AC13, `real_sdk` AC6): the Claude runs review `FINDING_DIFF`. The overridden-signal child, which reviews the sample diff, uses `SAMPLE_FINDING`, the default finding moved to `billing/invoice.py:14`.
10. **P1-S1 AC3 and P1-S18**: the fake-CLI runs write `FINDING_DIFF`. P1-S18's mock routes expect exit 0, because the canned finding is dropped. The Claude route still expects 1.
11. **P1-S3**: the `assert_reviewed` docstring no longer says exit 1. **P1-S19**: no edit. Its two failures went away with the shared helpers.
12. **P1-S4** (`test_p1_s4_audit_log.py`): the 27 failing tests ran on the canned mock and asserted exit 1 only to show that the run completed. They now expect 0, and the module docstring says why. The three AC8 stderr goldens in `tests/fixtures/p1_s4/` gain `dropped=0` before `skipped_files=`, the field design.md's compatibility rule appends.
13. **P1-S5** (`test_p1_s5_provider_notices.py`): `claude_logged` reviews `FINDING_DIFF` by default. AC3's mock case injects `Finding(**FINDING_FIELDS)` over `FINDING_DIFF` and expects `src/app.py:7`, instead of the canned `mock/example.py`.

No failure looked like a code bug. Each one came from the canned finding being dropped, from a finding with no anchor in its diff, or from the new `dropped=` field.

**Before the developer's code.** In a worktree of `main` (`035607c`), 84 of the 95 fail. They include the AC10 CLI property, which printed `app.py:1000000000` for a deleted file. The 11 that pass there are controls that keep a valid finding (AC1's golden and design-rule lines, AC4's exact `b/x.py` and `x.py`, and the AC8 control), plus the AC9 rationale case, which now also asserts that 3 `verify` records exist. On the developer's first tree, one case failed: `verify_findings` took `(proposed, parsed)`, not design.md's `(proposed, rule_findings, parsed)`, and `kept` held `Finding`, not `ReviewedFinding`. The developer has since matched design.md, and the case passes.

**How the tests read the log.** They reuse P1-S4's `read_records`, so every read checks that the file is pure ASCII, holds one record per line and has no lone surrogates. The spec doesn't name the keys in `verify` and `policy` records, so most values are looked for wherever they sit in `data`. A digest is matched as a substring, so a `sha256:` prefix would also pass. Once the code was done, the AC9 cases were pinned to the developer's choices, which the spec leaves open: events `verify.checked` and `policy.decided`; `outcome` values `kept` and `dropped`, and `reported` and `below_threshold`; and a kept finding logged under the diff path it matched.

### Criteria

| AC | Tests (`test_p1_s6_…`) | Result |
|---|---|---|
| AC1 | `ac1_finding_on_a_diff_line_prints_in_the_unchanged_format_and_exits_1` ×5 (12, 14, 30, 37, 41): stdout is the golden line, and the summary has `dropped=0`. `ac1_all_five_findings_print_as_the_golden_file`. Design rule: `ac1_design_rule_every_other_added_or_context_line_is_kept` ×5 (13, 31, 32, 36, 38) | pass (11) |
| AC2 | `ac2_finding_off_the_bodys_lines_is_dropped_and_exits_0` ×7 (1, 11, 15, 18, 29, 42, 44): exit 0, `No findings at or above low.`, `dropped=1`, reason `line_not_in_diff`. Limits: lines `2**63 + 1` and `10**18` are dropped the same way (×2) | pass (9) |
| AC3 | `ac3_file_not_in_the_diff_is_dropped_as_unknown_file` ×5: `other.py`, `../billing/invoice.py`, `BILLING/INVOICE.PY`, and `\x1b` after or before the path. Design rule (exact, then one leading `./`, `a/` or `b/`): `ac3_design_rule_only_one_known_prefix_is_ever_stripped` ×10: `a/b/…`, `./b/…`, `.//…`, `c/…`, `/…`, backslashes, a trailing space or newline, `invoice.py`, and `billing/invoice` | pass (15) |
| AC4 | `ac4_prefixed_file_is_kept_and_reported_as_the_diff_path` ×3: `./`, `a/` and `b/` each print the golden line for `billing/invoice.py`. `ac4_real_path_starting_with_b_stays_as_it_is`. `ac4_exact_match_wins_over_the_stripped_one` ×4, over a diff holding both `x.py` and `b/x.py`: `b/x.py` and `x.py` stay as they are, `a/b/x.py` reports `b/x.py`, and `./x.py` reports `x.py` | pass (8) |
| AC5 | `ac5_finding_on_a_skipped_file_is_dropped` ×3 (`README.md`, `uv.lock` and `src/pb/x_pb2.py`, each beside `app.py`): reason `skipped_file`, `dropped=1`. `ac5_reviewed_file_beside_it_still_prints` | pass (4) |
| AC6 | A deleted file's old path at lines 1 and 2: `unknown_file` (×2). `@@ garbage @@` over `+a`, `+b`, ` c`, with the parse checked to give no line numbers, at lines 1 to 3: `line_not_in_diff` (×3). A removed-only hunk at lines 1 to 3: `line_not_in_diff` (×3) | pass (8) |
| AC7 | `ac7_one_valid_of_three_prints_alone_with_dropped_2_and_no_warning`. `ac7_every_finding_dropped_exits_0_with_the_warning`: the exact warning line appears once, and `dropped=3`. `ac7_one_finding_dropped_warns_in_the_singular`: one dropped finding gives exactly the singular warning. Beyond the criterion: `ac7_beyond_kept_findings_keep_the_order_proposed` (findings on 41, 12 and 30, among two drops, print in that order, per design.md's "AI as proposed"), and `ac7_beyond_no_findings_at_all_gives_no_dropped_warning` | pass (5) |
| AC8 | `ac8_invalid_critical_doesnt_count_toward_the_threshold`: exit 0, `No findings at or above high.`, `dropped=1`. Control: the same `critical` on line 14 exits 1 | pass (2) |
| AC9 | `ac9_each_proposal_has_a_verify_record_in_order`: three records, each with its line, severity and rationale SHA-256. `ac9_verify_records_hold_the_reason_and_an_outcome`: reasons none, `line_not_in_diff` and `unknown_file`, and outcomes `kept`, `dropped` and `dropped`. `ac9_no_rationale_text_reaches_the_log`: no sentinel in the raw or decoded log. `ac9_policy_record_for_each_kept_finding_only`, with `--threshold high`: two records, none for the dropped line 15, with line 12 `below_threshold` and line 14 `reported`. `ac9_a_file_matching_a_diff_path_is_in_the_record` ×2: for both `billing/invoice.py` and `./billing/invoice.py`, `file` is `billing/invoice.py`. `ac9_any_other_file_is_only_its_sha256_and_length` ×3 (`SENTINEL-FILE.py`, 10,000 characters, a path with `\x1b`): the digest and length are present, and no piece of the name is, either raw or as `\u001b` | pass (9) |
| AC10 | `ac10_property_every_printed_finding_is_on_an_added_or_context_line`: 300 examples through `main`, seed `20261106`. Every printed line parses. Its file and line are an added or context line of a reviewed path in `parse_diff`'s result, and its line equals the proposed line, so nothing moved. Beyond the CLI: `ac10_property_verify_findings_keeps_exactly_the_valid_anchors`, 500 examples. `kept` equals the proposals with valid anchors, in order, with the file resolved by design.md's rule, and `dropped` holds the rest | pass (2) |
| AC11 | `ac11_canned_mock_finding_is_dropped_and_exits_0`: exit 0, `No findings at or above low.`, the canned warning, `dropped=1`, reason `unknown_file`. `ac11_canned_finding_is_dropped_in_a_child_process` (`python -m review_bot`) | pass (2) |
| AC12 | `ac12_finding_in_a_combined_hunk_is_line_not_in_diff` ×12: `diff --cc` and `diff --combined`, with and without `index`/`---`/`+++` lines, at lines 1 to 3. The parse is checked to list `src/app.py`, so `unknown_file` would be wrong. `ac12_added_line_of_the_normal_file_still_prints_and_none_moves` ×8 (the combined section first or last): only `app.py:1` prints, with `dropped=3`, and the reasons are `line_not_in_diff` ×3, then none | pass (20) |
| Limits | `limits_many_findings_over_a_large_diff_finish_in_bounded_time`: 4,000 findings over a 10,000-line diff, half of them valid, finish within 30 s (3.2 s here). The printed count and `dropped=2000` are exact | pass (1) |

### AC10's generator and its guards

- **Diffs:** one to four distinct paths among `app.py`, `src/app.py`, `x.py`, `b/x.py`, `pkg/mod.py` and `README.md` (skipped). Each section is unified, combined (`diff --cc` or `diff --combined`, with `@@@` headers and two-column markers, with or without file lines) or deleted. A third of unified hunk headers are hostile: no new start, a sign or a letter, 20 digits, an Arabic-Indic digit (U+0663, which `\d` and `int` accept), `@@@` in a normal section, a missing closing `@@`, or no spaces. Bodies hold unmarked lines, bare blanks, `\ No newline`, and lines that become `--- a/evil.py`, `+++ b/evil.py` or `@@ -1 +1 @@` once they have their marker.
- **Findings:** up to eight per example. Files are each diff path, that path with `./`, `a/` or `b/`, or an unknown name. Lines are random (1 to 45, or `10**9`), a real numbered line of any file with or without a prefix, or the line beside one: the near miss a snap would take.
- **Guards** are counted from the oracle rather than the output, so a wrong `verify` can't starve them. CLI property (300 examples): at least 250 reach the assertion (300 here), 50 have a valid proposal (77), 100 have an invalid one (202), 30 propose on a combined section's path (69), and 50 hold a hostile header and a finding (148). Direct property (500 examples): 80 with a valid proposal (133) and 250 with an invalid one (315).

### Readings of this slice's criteria

1. **Event names, outcome values and the logged path** aren't named in the spec or design.md. Before the code was done, the tests required only that a kept and a dropped proposal's records differ. They're now pinned to the developer's choices (see "How the tests read the log"). The designer may want to write them into the spec.
2. **The drop reason** is found as the one `DropReason` value among a record's scalars. A kept proposal's record holds none.
3. **"A `verify` record for it"** is read as one record per finding, in proposal order, and the tests count them. The AC5 and AC12 cases also rely on that order.
4. **AC9's "a `file` matching a diff path"**, for `./billing/invoice.py`, is pinned to the matched diff path, `billing/invoice.py`, as the developer chose.
5. **`dropped=0`** is required in AC1's runs, from design.md's compatibility rule that the summary always appends the field.
6. **AC7's warning** isn't required for zero findings: "every finding invalid" is read as needing at least one finding. A beyond-criterion case pins that no warning appears then. With a single finding, the developer writes the singular, `the 1 model finding was dropped: its file or line isn't in the diff.`, which `ac7_one_finding_dropped_warns_in_the_singular` pins (review round 1). AC7 states the singular too (changed in build). The no-warning checks match either form.
7. **"Printed" in AC10** is checked with design.md's rule that a skipped file isn't a valid anchor, so the oracle is stricter than "an added or context line of `parse_diff`'s result" alone. AC5 needs this anyway.

### Observation outside this slice

`parse_diff` numbers a hunk from 0 when its header's new start is 0 but its body has lines (`@@ -1 +0 @@` then `+x` gives `new_line` 0). Git never writes that. No finding can name line 0 (`line ≥ 1`), so verify isn't affected, and the generator clamps its lines to 1. It isn't reported as a bug.

### Coverage

This file alone covers `verify.py`, `policy.py` and `render.py` fully, statements and branches. The whole suite covers 100% of `review_bot`.

### Limits

1. **`pre-commit` and `pip-audit`** weren't run by the tester. CI runs them.
2. **A future dedup slice** may collapse P0-S3's `many_findings` child, which repeats one identical finding 5,000 times. Today nothing dedups AI findings, so the case still outgrows the stream buffers.
3. **Cost is shown by one time bound**, with wide headroom. Growth isn't measured.
4. **Run on Windows only** by the tester. CI on Linux hasn't been checked.

## P1-S7: Catch swallowed exceptions with no model, with stable fingerprints

Spec: [p1-s7-swallowed-exception-rule.md](slices/p1-s7-swallowed-exception-rule.md). Tests: [`tests/integration/test_p1_s7_swallowed_exception_rule.py`](../../../tests/integration/test_p1_s7_swallowed_exception_rule.py), 100 cases. Runs go through `main` with an injected `MockProvider(findings=[])` unless a criterion scripts model output. AC1 also runs the documented `python -m review_bot Assignment/sample_diff.txt --provider mock` in a child. `run_rules`, `fingerprint_for` and `verify_findings` are called directly where a criterion names them. Fixture: [`tests/fixtures/p1_s7/fingerprints.golden.json`](../../../tests/fixtures/p1_s7/fingerprints.golden.json), seven inputs and their fingerprints, including an indented anchor, ordinal 1, an `ai:logic` rule part, a non-ASCII path and line, a lone surrogate in the path and the anchor, and a path holding an astral character, a tab and a `"`.

**Status: every criterion passes, 100 of 100 after review round 1 (98 of 98 before it).** The tests were written from the spec, design.md's [fingerprints and dedup](design.md#fingerprints-and-dedup), its Interfaces and ADR 0003, before the code existed. When the file first ran, `rules.py` and `fingerprint.py` were already present: 94 passed, and the 4 AC9 golden cases failed only because the golden values weren't written yet (see "For the designer", item 3). The main session's full gate on the finished tree: ruff format and check clean, mypy strict clean (24 files), pytest --cov 2224 passed, 12 skipped, 0 failed, 100% coverage, pre-commit clean (after review round 1; 2218 before it).

### Gate

The main session ran the full gate on the finished tree, with the result in the status line above: 2224 passed, 100% coverage. The tester ran only its own files. Before review round 1, this slice's file and the handover files passed together, 551 passed and 12 skipped. Before the handover edits, 19 cases had failed across those files (976 run, with `tests/conformance`). After round 1, this file and `test_p1_s18_clean_sdk_shutdown.py` pass together, 112 of 112, and `ruff format --check` and `ruff check` are clean on `tests/integration`.

### Review round 1

1. **A rule finding on a path that isn't UTF-8**, end to end (`ac6_limits_non_utf8_python_path_is_reported_escaped_and_logged_quoted`). Git's `"b/caf\377.py"` holds a swallowed handler. The run exits 1, and stdout is the rule line for `caf\udcff.py:3`, as the renderer escapes it. The `static.rules` entry and the `policy.decided` record each hold `file: "\"caf\\377.py\""` and `path_quoted: true`.
2. **Golden rows for each encoding branch** in design.md's [Encoding](design.md#fingerprints-and-dedup): file `caf\udcff.py` with anchor `x\udcff` (`surrogatepass`, and a lone surrogate as its own `\udcff` escape), and a file holding U+1F600, a tab and a `"`, with an anchor holding the same character among whitespace to collapse. Each value was computed step by step from the Encoding text, checked against `fingerprint_for`, and only then written. The five earlier values were recomputed the same way and are unchanged.
3. **Limits through `main`:** 40,000 `+except:` lines, about 360 KB, under the 400 KB default (`limits_40k_bare_except_lines_near_the_size_limit_finish_in_bounded_time`). Every clause looks ahead for a body. This exits 0 with no findings in 0.4 s here, within 30 s. The developer's fix (indexing, not slicing) had already landed when this case first ran. The developer's unit test notes that the quadratic version took about 4 s, so this bound catches a hang or worse growth but wouldn't catch that regression. The developer's unit test guards that one.
4. **P1-S18 nit:** `main_leaves_the_unraisable_hook_unless_it_built_claude` now asserts stdout per route: Claude's anchored finding, `FINDING_LINE`, on the Claude route, and only `SAMPLE_RULE_LINE` on the mock routes, so it shows again that the canned finding is dropped.

### Criteria

| AC | Tests (`test_p1_s7_…`) | Result |
|---|---|---|
| AC1 | `ac1_sample_diff_prints_the_rule_line_at_line_35_and_exits_1`: stdout is exactly the spec's line for `billing/invoice.py:35`, `rule_findings=1`. `ac1_documented_command_in_a_child_process_reports_the_rule`. `ac1_run_rules_gives_a_rule_sourced_finding`: `Source.RULE`, `RuleId.SWALLOWED_EXCEPTION`, medium/reliability, confidence 1.0, 16-hex fingerprint, 0 unanchored. Design rules: `ac1_design_rule_summary_appends_its_fields_in_order` (`rule_findings`, `dropped`, `deduped`, `skipped_files`, `model_called`, `run_id`), `ac1_design_rule_static_rules_record_holds_the_finding` (fingerprint, rule, file, line, severity), `ac1_logged_fingerprint_is_the_rule_findings_fingerprint` | pass (6) |
| AC2 | `ac2_swallowing_handler_gives_one_finding_at_the_except_line` ×4, the spec's four forms. Limits ×7: in a method, tab-indented, `except E: ...`, `except E:pass`, a comment after the colon, a comment after `pass`, `except os.error:`. A CRLF diff. Nested handlers give two findings, at the inner and the later `except` | pass (13) |
| AC3 | `ac3_handler_doing_something_first_gives_no_finding` ×3, the spec's three bodies. Lookalikes ×8: `except E: raise`, `except E: log(e); pass`, `passed = True`, `pass_through(e)`, `excepted = 1` then `pass`, a commented-out handler, `finally: pass`, `else: pass` | pass (11) |
| AC4 | `ac4_context_except_with_added_pass_gives_a_finding`. `ac4_handler_not_added_gives_no_finding` ×2 (both context, both removed). Limits: a removed `log(e)` between a context `except` and an added `pass` still gives a finding. A hunk at `+57` anchors at 59 | pass (4) |
| AC5 | `ac5_hunk_ending_right_after_except_gives_no_finding`. Limits: only a removed line, or only a comment, after the `except`. A `pass` in the next hunk isn't taken as the body | pass (4) |
| AC6 | `ac6_same_lines_in_a_file_not_reviewed_as_python_give_no_finding` ×4: `x.js`, `x.pyi`, `src/pb/x_pb2.py` (generated, skipped), `docs/notes.md`. Control: the same lines in `x.py` beside `x.js` give one finding. Limits: `x.py.txt`, `x_py`, `x.pyc`, and a deleted `old.py`. A `.py` path that isn't UTF-8 is reported escaped and logged quoted (review round 1) | pass (10) |
| AC7 | `ac7_hit_under_an_unreadable_header_prints_nothing` (exit 0, `rule_findings=0`). `ac7_run_rules_counts_it_as_an_unanchored_hit` (`findings == ()`, `unanchored_hits == 1`). `ac7_static_rules_record_has_the_count`. Limits: a readable hunk beside it still prints, and the count stays 1 | pass (4) |
| AC8 | `ac8_rule_line_prints_first_then_the_ai_line` (AI on line 14, so the rule isn't first by line number). `ac8_threshold_high_keeps_only_the_ai_high`. Also: `--threshold high` with an AI medium exits 0. `--threshold medium` keeps the rule and drops an AI low. `--threshold critical` on the rule alone exits 0 (the spec's handover note). Design rule: rule findings ordered by file then line (`a.py:5`, `b.py:3`, though `b.py` comes first in the diff), then AI | pass (6) |
| AC9 | `ac9_fingerprint_matches_the_golden_file` (7 cases). `ac9_fingerprint_is_16_lowercase_hex_digits`. `ac9_fingerprint_doesnt_depend_on_the_process` ×3: a fresh interpreter with `PYTHONHASHSEED` 0, 1 and 4242. `ac9_each_part_changes_the_fingerprint`. Limits: moving a `/` between `file` and `rule` changes the value | pass (7) |
| AC10 | `ac10_hunk_moved_50_lines_down_keeps_the_fingerprint` (line 85, same value), and the same through the `static.rules` record. `ac10_reindented_handler_keeps_the_fingerprint` ×4 (4, 2 or 12 spaces, a tab). `ac10_whitespace_inside_the_line_is_collapsed`. `ac10_changed_code_on_the_anchored_line_changes_the_fingerprint` (`BaseException`). `ac10_renamed_file_changes_the_fingerprint`. Beyond: an AI finding moved with its hunk keeps its fingerprint | pass (10) |
| AC11 | `ac11_two_identical_handlers_print_two_findings` (`rule_findings=2`, `deduped=0`), with fingerprints equal to `fingerprint_for(…, ordinal=0)` and `ordinal=1`, and both logged. `ac11_two_ai_findings_on_one_line_with_different_rationales_both_print`, with fingerprints equal to `fingerprint_for(rule="ai:logic", …)` at ordinals 0 and 1. An AI finding and the rule on one line differ | pass (6) |
| AC12 | `ac12_identical_ai_findings_print_once_with_deduped_1`. `ac12_verify_records_one_duplicate_of_the_kept_fingerprint`. Limits: differing only in confidence is still a duplicate. Three identical give `deduped=2`. Another category, another line, or a rationale differing only in case keeps both (×3) | pass (7) |
| AC13 | `ac13_model_output_claiming_a_code_set_field_exits_13` ×3 (`source`, `fingerprint`, `rule`): exit 13, empty stdout. Control: the same output without the field prints. Limits: a claim over the sample diff, which has a rule hit, still exits 13 with empty stdout | pass (5) |
| Limits | 5,000 handlers in one file: 5,000 lines, `deduped=0`, within 30 s. Through `run_rules`, within 30 s each: an `except` tuple of 100,000 names, 200,000 spaces with and without a colon, and 50,000 `except` words on one line. Through `main`: 40,000 `+except:` lines, about 360 KB (review round 1) | pass (6) |

### Handover edits (spec Notes)

The sample diff now gets a rule finding, the summary gains two fields, and identical AI findings are deduplicated. Each earlier test keeps what it protects. P0-S1 AC2's `--threshold critical` tests stand unchanged and pass. `tests/integration/diff_support.py` gains `SAMPLE_RULE_LINE`, the sample's rule line.

1. **`Bot.request_for`** (`conftest.py`) takes `exit_code=0`. The sample-diff callers pass 1, so the guard stays exact everywhere else. Used by P0-S1 AC12 and AC13 (`test_p0_s1_prompt.py`) and by P1-S2 AC1 through `reviewed_text`, which takes the same keyword.
2. **P0-S1 AC1** (`test_p0_s1_cli.py`): `…_replaced_by_p1_s6_ac11_command_exits_0` is renamed `…_replaced_by_p1_s7_ac1_command_exits_1`. The canned-finding case expects stdout to be `SAMPLE_RULE_LINE` only, which still proves the canned finding is dropped. The module docstring says so.
3. **P0-S1 AC12** (`sample_diff_is_not_rejected`) and **AC16** (offline run): exit 1. AC16's stdout is `SAMPLE_RULE_LINE`, and it keeps the canned notice check.
4. **P0-S3** (`test_p0_s3_stream_failures.py`): the `many_findings` child repeated one finding 5,000 times, which P1-S7 AC12 now collapses to one, as P1-S6's report foresaw. The rationales are now distinct (`0000xxx…` to `4999xxx…`), so stdout still outgrows the buffers. AC5's "findings" case checks both stdout lines, the rule line and then the finding, before the summary, not only the first.
5. **P0-S4 AC6's note** (`test_p0_s4_real_sdk.py`): the "none" case must exit 0, so the overridden-signal child now reviews `tests/fixtures/p1_s6/anchors.diff`, the sample with no rule hit and the same line 14.
6. **P1-S18 AC4**: the mock routes over the sample exit 1. Every route now expects 1, and the hook checks are unchanged. Since review round 1, the route test also asserts stdout: `FINDING_LINE` for Claude, and only `SAMPLE_RULE_LINE` for the mock routes.
7. **P1-S4 AC8**: the three stderr goldens in `tests/fixtures/p1_s4/` gain `rule_findings=0` before `dropped=` and `deduped=0` after it. The sample-diff log case expects exit 1. It still finds no added or context line in the log, now that `static.rules` has data.

No failure looked like a code bug.

### For the designer (all resolved)

All three were resolved in build by the main session on 2026-11-06. Item 1 is now in the spec's [Out of scope](slices/p1-s7-swallowed-exception-rule.md#out-of-scope) as a known miss. Item 2 is in design.md's [audit table](design.md#audit-log): `verify.checked` gains outcome `duplicate` and `duplicate_of`. Item 3 is in design.md's [Encoding](design.md#fingerprints-and-dedup), which the golden rows now follow. The original notes:

1. **Multi-line `except` clauses aren't caught.** `except (\n    A,\n    B,\n):\n    pass`, the form black gives a long tuple, gives no finding and no unanchored hit. No criterion covers it, so no test is red for it. It's a real-world false negative. Should the spec name it in scope, or in Out of scope beside multi-line strings?
2. **A duplicate's `verify` outcome.** A deduplicated AI proposal is logged as `outcome: "duplicate"`, with `duplicate_of` holding the kept fingerprint and `fingerprint: null`. The developer records this in the spec's Notes, but design.md's audit table still lists only `kept` or `dropped` for `verify.checked`, so the table should catch up. No test pins it.
3. **AC9's encoding isn't in the spec.** design.md lists the parts, not how they're joined. The developer joins them as a JSON array, `["v1", file, rule, sha256(collapsed line), ordinal]`, ASCII-escaped. The tester recomputed every golden value with `hashlib` from that reading, and each matched `fingerprint_for` before it was pinned. So the golden file guards against drift, not against the choice itself. Since Phase 2 keys comments on these values, the designer may want to write the encoding into design.md.

### Readings of this slice's criteria

1. **The `static.rules` keys** aren't named. Values are looked for wherever they sit, except the unanchored count, which is found by a key containing `unanchored`, as design.md's "unanchored hit count" suggests. The developer uses `findings` and `unanchored_hits`.
2. **AC10's reindent** is checked through `run_rules`. design.md's "whitespace collapsed" is also checked directly on `fingerprint_for`, which was read as normalising its own `anchor_text`. The developer's does.
3. **AC12's `deduped` record** is checked by finding the kept fingerprint in `repr(result.deduped)`, because its shape isn't fixed.
4. **The AC2, AC3 and AC6 Limits cases** are the tester's readings of "a handler whose first statement is `pass` or `...`" and "Python files". A comment after `except E:` or `pass` still counts, `pass_through()` doesn't, and `.pyc` or `x.py.txt` isn't Python.

### Coverage

This file alone covers `rules.py`, `findings.py` and `policy.py` fully, and `fingerprint.py` at 92%. The uncovered line is the negative-ordinal `ValueError`, which no criterion reaches. The whole suite covers 100%, in the main session's gate.

### Limits

1. **The full gate** was run by the main session, not the tester, and is green (see the status line). `pip-audit` runs in CI.
2. **Run on Windows only.** AC9's child-process cases are the cross-platform guard, and they run on CI's Linux too.
3. **Rule vs AI dedup and the model's `rule` tag** are P1-S9's. AC13's `rule` case will be replaced there, as the spec notes.

## P1-S8: Catch `open()` without an encoding with no model

Spec: [p1-s8-open-encoding-rule.md](slices/p1-s8-open-encoding-rule.md). Tests: [`tests/integration/test_p1_s8_open_encoding_rule.py`](../../../tests/integration/test_p1_s8_open_encoding_rule.py), 94 cases. Runs go through `main` with an injected `MockProvider(findings=[])`. AC1 also runs the documented `python -m review_bot Assignment/sample_diff.txt --provider mock` in a child. `run_rules` and `fingerprint_for` are called directly where a criterion is about a finding's identity. No fixture files: every diff is built in the test.

**Status: every criterion passes, 94 of 94, after review round 1 (87 of 87 before it).** The tests were written from the spec and design.md before the code. Against `main` at 17228ae, 34 of the first 79 failed (every case expecting a finding) and 45 passed, most of them vacuously. On the developer's code, one of mine failed: `ac1_design_static_rules_record_holds_the_finding` took the whole `static.rules` list as this rule's entries. It now keeps only this rule's entries. After that, 8 cases were added to pin the developer's Notes (below), and 7 more in review round 1. The full gate after review round 1: ruff format and check clean, mypy strict clean (24 files), pytest --cov 2381 passed, 12 skipped, 0 failed, 100% coverage, pre-commit clean (2366 passed before round 1). The per-record audit-log cost noted below is tracked as [#33](https://github.com/sleepyshark85/code-review-bot/issues/33).

### Criteria

| AC | Tests (`test_p1_s8_…`) | Result |
|---|---|---|
| AC1 | `ac1_sample_diff_prints_line_35_then_line_39`: stdout is exactly `SAMPLE_RULE_LINE`, then the spec's line for `billing/invoice.py:39`; `rule_findings=2`. `ac1_documented_command_in_a_child_process_prints_both_lines`. `ac1_run_rules_gives_a_low_reliability_rule_finding_at_39`: `Source.RULE`, `RuleId.OPEN_WITHOUT_ENCODING`, low/reliability, confidence 1.0. Design rules: `ac1_design_fingerprint_uses_the_rule_id_and_the_anchored_line` (`fingerprint_for(rule="open-without-encoding", …, ordinal=0)`), `ac1_design_static_rules_record_holds_the_finding`. `ac1_threshold_medium_drops_the_low_open_finding` | pass (6) |
| AC2 | `ac2_call_without_encoding_gives_one_finding_at_the_open_line` ×6, the spec's six forms. The 3-line call starts on line 2, so its anchor isn't the hunk's first line. Limits ×9: `'w'`, `"rt"`, `newline=""` only, a nested call argument, a path string holding `encoding=`, `encoding=` only in a trailing comment, a string holding `open(` then a real call, a string holding `#` then a real call, `with open(` over 3 lines. A hunk at `+57` anchors at 59. A CRLF diff | pass (17) |
| AC3 | `ac3_call_with_encoding_or_binary_mode_gives_no_finding` ×6, the spec's six forms. Limits ×8: `encoding = "utf-8"` with spaces, `mode='rb'`, `"br"`, `"wb+"`, `"xb"`, `mode="rb", buffering=0`, a positional encoding `open(p, "r", -1, "utf-8")`, `with open(` over 4 lines with `encoding=`. Round 1: `encoding=` after a backslash continuation (`ac3_encoding_after_a_backslash_continuation_gives_no_finding`) | pass (15) |
| AC4 | `ac4_other_opens_and_unsure_calls_give_no_finding` ×8, the spec's eight forms. Limits ×9: `def open(self, p):`, `'open(p)'`, a string with an escaped quote before `open(p)`, a trailing comment, `open(*args)`, `open(p, **options)`, `mode=mode`, `fopen(p)`, `open_file(p)`, `builtins.open(p)` (Out of scope: silent like any attribute call). A call closed only in the next hunk. From the spec's Notes (`ac4_notes_…`): a call spanning 20 lines is found, but 21 or 25 lines aren't; `open()` with no file, `open(p]` and `open(p)` inside a triple-quoted string give none, and a real call after that string is found. Round 1: a hunk starting inside a docstring (` Use it like this:`, `+    open(p)`, ` """`) gives none (`ac4_hunk_starting_inside_a_docstring_gives_no_finding`). AC4's last sentence: an unclosed call and a 21-line call under `@@ garbage @@` give no finding and `unanchored_hits == 0`, from `run_rules` and in `static.rules` (`ac4_call_that_isnt_a_hit_isnt_counted_as_unanchored` ×2) | pass (29) |
| AC5 | `ac5_call_in_a_file_not_reviewed_as_python_gives_no_finding` ×3: `x.js`, `src/pb/x_pb2.py` (skipped), `docs/notes.md`. `ac5_call_on_a_context_or_removed_line_gives_no_finding` ×2. Control: the same line in `x.py` beside `x.js` gives one. Limits: removing an `encoding=` and re-adding the call without it gives one; a deleted `old.py`, `x.pyi` and `x.py.txt` give none | pass (10) |
| AC6 | `ac6_one_call_with_encoding_and_one_without_give_one_finding` ×4: encoded first, encoded second, binary first, and `open(p, encoding=detect(open(q)))`. `ac6_two_calls_without_encoding_on_one_line_give_two_findings`: two identical stdout lines, `rule_findings=2`, `deduped=0`. `ac6_two_calls_on_one_line_have_different_fingerprints`: `fingerprint_for(…, ordinal=0)` then `ordinal=1`. Both are logged in `static.rules` | pass (7) |
| AC7 | `ac7_hit_under_an_unreadable_header_is_counted_not_printed` (renamed in round 1 from `design_rule_…`): as P1-S7 AC7, with `open(p)` under `@@ garbage @@`. Nothing prints, `unanchored_hits` is 1, and so is the `static.rules` count | pass (1) |
| Limits | Through `main`: 10,000 `open(p)` lines give 10,000 lines and `deduped=0` (3.9 s here). One call with 40,000 argument lines, unclosed, and 40,000 nested unclosed `open(` lines, each 280 to 330 KB under the 400 KB default, give none (0.4 s each). Through `run_rules`: 50,000 unclosed `open(` on one line (0), 20,000 `open(p), ` on one line (20,000), 100,000-deep parentheses (1), and 30,000 calls inside one string (0). Each is bounded at 30 s. Round 1, from the spec's new Limits: an unclosed triple-quoted string over 25,000 `open(p)` lines gives none through `main`; one added line of 387 KB holding 43,000 `open(p)` calls gives 43,000 lines through `main` in 23 s here, bounded at 120 s (see Observation) | pass (9) |

### Handover edits

The sample diff now prints two rule lines. Each earlier test keeps what it protects. `tests/integration/diff_support.py` gains `SAMPLE_OPEN_LINE`, and `SAMPLE_RULE_LINES` (both lines, in print order).

1. **P0-S1 AC1** (`test_p0_s1_cli.py`, the canned finding is dropped) and **AC16** (`test_p0_s1_offline.py`, offline run): stdout is `SAMPLE_RULE_LINES`, still with no canned line. The module docstring names P1-S8 AC1.
2. **P0-S3 AC5** `[findings]` (`test_p0_s3_stream_failures.py`): both rule lines, then the model's finding, then the summary.
3. **P1-S18 AC4** `[injected_mock]` and `[mock_flag]`: stdout is `SAMPLE_RULE_LINES`. The Claude route still expects only `FINDING_LINE`.
4. **P1-S7** (17 cases): these stay about the swallowed-exception rule, not the sample's line count.
   - `logged_rule_entries` keeps only `swallowed-exception` entries. It had returned the whole `static.rules` list, the same bug as mine above. This fixes AC1's two log cases and AC10's moved-hunk log case.
   - `rule_fingerprints` and a new `swallowed_findings` keep only this rule's findings. This fixes AC1 `run_rules`, AC10 moved, changed and renamed.
   - AC1's sample cases (in-process and child) check that the rule line at 35 prints first, and only once. A new case, `ac1_sample_without_its_open_call_prints_only_the_rule_line`, keeps AC1's exact stdout and `rule_findings=1` on the sample with `encoding="utf-8"` added to its `open()`, so the line numbers are the same.
   - AC8's first-then-AI case, and AC11 and AC12's sample cases, use that same sample, so their `splitlines()[1:]` and `len(kept) == 2` still mean "after the one rule line".

None of the 22 handover failures was a code bug.

### Readings, and what the developer's Notes settled

1. **Settled by the Notes, now pinned:** a fourth positional argument counts as an encoding (AC3 limits). `def open`, `*args`, `**options` and `mode=mode` stay silent (AC4 limits). A call spanning more than 20 lines isn't reported, with the boundary at 20 found and 21 not. A call with no file argument, or closed by a mismatched bracket, isn't reported. Triple-quoted strings are skipped.
2. **Settled by the spec (AC7, round 1):** a hit under an unreadable header counts in `unanchored_hits`. The test is now named for AC7.
3. **Settled by the spec (AC4's last sentence, and Out of scope, round 1):** an unclosed call, or one spanning more than 20 lines, isn't counted in `unanchored_hits`; `builtins.open(p)` is silent. Both are pinned.
4. **Settled by the spec (Notes, Handover, round 1):** P1-S8 AC1 replaces P1-S7 AC1's `rule_findings=1` on the sample diff.
5. **Test gotcha:** padding a multi-line call with `p,` lines gives it a fourth positional argument, which counts as the encoding. The span tests pad with comment lines instead.

### Observation outside this slice

The 43,000-finding run takes 23 s on this Windows machine, and growth is linear: 5,000, 10,000 and 20,000 findings take 2.2, 5.1 and 11.3 s. `run_rules` takes 1.1 s of that. Most of the rest is the audit log: `audit.record` runs once per finding from `review._record_decision`, and each call reopens the log file (20,000 opens took 5.2 s of an 11.3 s profile). This is P1-S4's design, not a P1-S8 bug, so it's noted rather than reported against this slice. The case's bound is 120 s rather than 30 s, which still catches a quadratic step.

### Coverage

100% overall before round 1, so the slice's modules (`rules`, `fingerprint`, `findings`) are fully covered. Nothing in the criteria was left untested.

## P1-S9: See one finding when a rule and the model report the same problem

Spec: [p1-s9-dedupe.md](slices/p1-s9-dedupe.md). Tests: [`tests/integration/test_p1_s9_dedupe.py`](../../../tests/integration/test_p1_s9_dedupe.py), 72 cases. Runs go through `main` with an injected `MockProvider(findings=...)` or `MockProvider(raw_output=...)`. `verify_findings` and `build_request` are called directly where a criterion is about a finding's identity or the prompt. No fixture files: every diff other than the sample is built in the test.

**Status: every criterion passes, 72 of 72, on the developer's uncommitted working tree.** The tests were written from the spec and design.md, not the code. The developer's code was already in the shared working tree at my first run, so the "before" run used `main` (0ddad14) in a scratch worktree: 52 failed and 20 passed. The 20 are cases `main` already met: unknown or malformed `rule` values exit 13 (any extra field did), an absent `rule`, untagged fingerprints, and the encoding self-checks. On the developer's code, 3 failed, all my own mistakes, now fixed:
1. AC9's "says when to omit it" split the prompt into sentences, and "Omit it, or use null" doesn't repeat the word `rule`. It now checks the line that labels `rule:`.
2. AC11's fixed diff held `open(path)`, a rule hit, so its review exited 1. The diff now has none.
3. AC11's digest was a placeholder until the prompt existed. It's now recorded.

**Full gate, before review round 1, and again after it (developer's run: 2491 passed, 12 skipped, 100%)** (run by the coordinator on the working tree): ruff format and check clean, mypy strict clean, pytest --cov 2491 passed, 12 skipped, 0 failed, 100% coverage, pre-commit clean; the suite took about 376 s. After round 1's test change, only this file was re-run (72 of 72); the full suite waits for the go-ahead.

### Fingerprints, computed independently

`expected_fingerprint` writes out design.md's Encoding: the anchored line with whitespace runs collapsed and ends stripped, UTF-8 with `surrogatepass`, SHA-256; then `json.dumps(["v1", file, rule part, anchor hash, ordinal])` with its defaults, encoded as ASCII, SHA-256, first 16 hex digits. It reproduces all 7 rows of P1-S7's golden file and the rule findings' fingerprints in `static.rules`. Every expected AI fingerprint below comes from it, with the ordinal continuing from the rule findings (spec Notes), not from `fingerprint_for`.

### Criteria

| AC | Tests (`test_p1_s9_…`) | Result |
|---|---|---|
| Encoding | `expected_fingerprint_matches_the_p1_s7_golden_file`; `rule_findings_log_the_designs_fingerprints` | pass (2) |
| AC1 | `ac1_same_rule_line_and_severity_prints_only_the_rule`: stdout is exactly both rule lines, `deduped=1`, `dropped=0`, `rule_findings=2`; one `verify.checked` record with `outcome` `duplicate`, `duplicate_of` the rule's fingerprint, `reason` null. `ac1_verify_findings_names_the_rule_findings_fingerprint`: no AI finding kept, the rule's fingerprint in `deduped`. Design rules and Limits: another category still dedupes; `b/billing/invoice.py` dedupes as the diff path; `open-without-encoding` at 39, `low`, dedupes into that rule; two tagged findings, and two identical tagged findings, give `deduped=2` | pass (7) |
| AC2 | `ac2_nearby_line_at_low_severity_prints_only_the_rule` ×2 (32, 36). Limits ×3: 34, 37 (the blank context line), 38 (3 below) | pass (5) |
| AC3 | `ac3_far_or_other_hunk_line_prints_both` ×3 (31, 30, 14): rule lines then the AI line, `deduped=0`, `outcome` `kept`. Limits: in a two-hunk file, a tagged line 3 away in the previous hunk prints both, while line 2 away in the same hunk is deduped (control); the same line in another file prints both | pass (6) |
| AC4 | `ac4_unvalidated_path_on_line_39_prints_beside_the_rule` ×2 (no `rule`, `swallowed-exception`): both rule lines, then the AI line | pass (2) |
| AC5 | `ac5_more_serious_tagged_finding_prints_beside_the_rule` ×2 (high, critical), `deduped=0`. `ac5_threshold_high_prints_only_the_ai_line` ×2. Limits: `medium` tagged `open-without-encoding` on the `low` line 39 prints both. Spec Notes: the AI finding on 35 takes ordinal 1, from `verify_findings` and in its `verify.checked` record; two serious ones take 1 and 2; a deduped one before it takes no ordinal; with two identical handlers (ordinals 0 and 1), a tagged finding takes 2; in a mix of 18 tagged and untagged findings on 35, 36 and 39, plus 2 identical ones, every kept fingerprint is distinct | pass (11) |
| AC6 | `ac6_invalid_line_near_the_rule_is_dropped_not_deduped`: line 42, 3 from 39, same rule: `dropped=1`, `deduped=0`, `reason` `line_not_in_diff`, `duplicate_of` null. Limits: `billing/invoices.py` on line 35 is `unknown_file`, not deduped | pass (2) |
| AC7 | `ac7_unknown_rule_exits_13_with_empty_stdout` ×2 (`made-up`, `secret`). Limits ×8: `""`, `Swallowed-Exception`, `swallowed_exception`, a leading space, `ai:logic`, `1`, a list, an object. `made-up` on the sample, which has rule hits, still prints nothing. One bad `rule` among valid findings exits 13. `ac7_null_or_absent_rule_is_a_normal_ai_finding` ×2: `FINDING_LINE`, fingerprint `ai:logic`. `ac7_known_rule_id_is_accepted` ×2, replacing P1-S7 AC13's `rule` case | pass (16) |
| AC8 | `ac8_tagged_finding_with_no_rule_finding_uses_the_rule_id` ×2, from the `verify.checked` record. `ac8_untagged_finding_uses_ai_and_its_category` (`ai:security`). Through `verify_findings`: tagged on line 14 uses the rule ID with ordinal 0; untagged `high` on 35 uses `ai:reliability` with ordinal 0. Limits: a tagged and an untagged twin both print; identical tagged twins print once, the duplicate naming the kept one's fingerprint | pass (7) |
| AC9 | `ac9_system_prompt_labels_rule`; `ac9_system_prompt_names_each_rule_id` ×2; `ac9_system_prompt_says_when_to_omit_rule` (in the `rule:` line); `ac9_…_line_is_an_added_or_context_line_in_a_hunk` (one sentence with "added", "context", "hunk" and "must", "only" or "discard"; round 1 added the obligation word, since the line-counting sentence alone had satisfied it, and the test was checked to fail with the requirement sentence removed) and `ac9_…_hunk_header_counts_may_be_wrong` (one sentence). Out of scope: the system prompt is the same with and without rule hits, and no rule line reaches the user prompt | pass (7) |
| AC10 | `ac10_summary_and_run_start_carry_p1_v1` (sample, exit 1); `ac10_no_findings_run_carries_p1_v1` (exit 0) | pass (2) |
| AC11 | `ac11_prompt_digest_is_pinned_to_its_version`: SHA-256 of `json.dumps([system_prompt, user_prompt])` for a fixed diff, keyed by `PROMPT_VERSION`, with the comment to bump the version and re-record evals. `ac11_pinned_request_is_what_the_provider_receives`: the request through `main` has the same digest. `ac11_one_character_of_prompt_or_framing_changes_the_digest` | pass (3) |
| Limits | Through `main`: 2,000 handlers, with 2,000 `low` tagged duplicates and 2,000 `high` tagged findings, give `deduped=2000` and 4,000 lines in 7.7 s here. Through `verify_findings`: the 2,000 `high` ones give 4,000 distinct fingerprints, the last with ordinal 3,999. Bounded at 60 s | pass (2) |

### Handover edits

1. **P0-S1 AC3** (`test_p0_s1_cli.py`): `SUMMARY` has `prompt=p1-v1`, and the docstring names P1-S9 AC10.
2. **P1-S4 goldens** (`tests/fixtures/p1_s4/{all_skipped,findings,no_findings}.stderr.golden.txt`): `prompt=p1-v1`.
3. **P0-S1 AC15** (`test_p0_s1_prompt.py`): `FINDING_FIELDS` gains `rule`, so the prompt must label it, and the schema guard matches `Finding`'s fields.
4. **P0-S3 AC2** (`test_p0_s3_exit_codes.py`): the extra field was already named `extra`. A comment now says why it mustn't be `rule`.
5. **P1-S7 AC13**: the `rule` claim case is removed, since AC7 replaces it, and the section comment says so. `source` and `fingerprint` claims still exit 13.

The six touched files pass together: 477 cases. None of the handover failures was a code bug.

### Readings for the designer

1. **AC9 is tested on wording.** The checks look for `rule:`, both IDs, an "omit" or "null" phrase in the `rule:` line, a sentence with "added", "context" and "hunk", and one with "header", "count" and a word such as "wrong". A rewording that keeps the meaning could fail them. AC11 catches any change anyway.
2. **AC11 doesn't say how the two texts are joined.** The test hashes them as a JSON array, so text moved from one to the other changes the digest. The digest was recorded from the developer's working tree, so if the prompt changes again before merge, re-record it: `p1-v1` hasn't shipped.
3. **AC2 and AC3 leave the blank line 37 implicit.** P1-S6 counts it as a context line, so a tagged finding on it is 2 from 35 and is deduped. That case is pinned.

### Coverage

100% overall in the coordinator's gate before review round 1, so the slice's modules (`findings`, `fingerprint`, `policy`, `prompt`, `verify`) were fully covered. The developer's full gate after review round 1 gave 2491 passed, 12 skipped, 100% coverage again. Nothing in the criteria was left untested.

## P1-S10: Get the review as JSON

Spec: [p1-s10-json-output.md](slices/p1-s10-json-output.md). Tests: [`tests/integration/test_p1_s10_json_output.py`](../../../tests/integration/test_p1_s10_json_output.py), 126 cases. Golden: [`tests/fixtures/p1_s10/sample_report.golden.json`](../../../tests/fixtures/p1_s10/sample_report.golden.json), the sample's report without `run_id` and `usage`. Runs go through `main` with an injected `MockProvider`, or a small provider that reports non-zero token usage. AC8 and one AC11 case run a real process, as P0-S3 AC4's tests do. AC7 reuses the exit triggers from P1-S4's and P0-S3's tests, and the fingerprints come from P1-S9's `expected_fingerprint`.

**Status: every criterion passes, 123 passed and 2 skipped, on the developer's uncommitted working tree.** The tests were written from the spec and design.md, not the code. The developer's code was already in the shared working tree at my first run, so the "before" run used `HEAD` (babcdf2) in a scratch worktree: 96 failed and 27 passed. The 27 are the encoding and case-list self-checks, AC9's text goldens under the default and the variable (pre-slice behaviour, as AC9 requires), and `--format <anything>` exiting 2 because the flag didn't exist yet. On the developer's code, 1 failed, my own mistake, now fixed: a test built an AI `Finding` on a non-UTF-8 path, but `Finding` rejects a lone surrogate, so only rule findings and skipped files can carry one (as the spec Notes imply). The test now checks that only the object on that path gets `path_quoted`. The 2 skips are AC8's `/dev/full` cases, which run only in CI.

**Gate, before review round 1** (the coordinator's run on the working tree): ruff and mypy clean, pytest --cov 2634 passed, 14 skipped, 0 failed, 100% coverage, pre-commit clean.

**Review round 1.** Following the spec's "Changed in build" block, the tests now pin `usage` as exactly `input_tokens` and `output_tokens`, and require stdout to end in a single `\n`. Round 1 stopped JSON paths being redacted, so `notes_path_holding_an_end_marker_is_written_as_the_real_path` was added. The END marker is built at run time. `PipelineResult` moved to a new `report` module; no integration test imports it, so no import changed. After round 1, only this file was re-run: 124 passed, 2 skipped. One run taken while the developer was moving code failed the new test, and I didn't capture its message. It then passed alone and in two full runs of this file. The full suite waits for the go-ahead.

### Criteria

| AC | Tests (`test_p1_s10_…`) | Result |
|---|---|---|
| Golden | `golden_fingerprints_match_the_designs_encoding`: the golden file's fingerprints and entries match P1-S9's independent encoding of the sample's two rule findings | pass (1) |
| AC1 | `ac1_sample_with_no_ai_findings_matches_the_golden_report_and_exits_1`. `ac1_each_rule_finding_has_exactly_the_criterions_keys` (exactly AC1's 11 keys). `ac1_stdout_holds_nothing_but_the_document` (no text lines). `ac1_top_level_settings_are_the_runs` (`--model flag-model` shows). Every parse is strict: pure ASCII, one line followed by a single `\n`, exactly Interfaces' top-level and `counts` keys, no duplicate keys, no `NaN`, no lone surrogate | pass (4) |
| AC2 | `ac2_untagged_ai_finding_follows_the_rules_with_rule_null` (the whole entry, fingerprint `ai:logic`, ordinal 0). `ac2_tagged_ai_finding_carries_its_rule_and_rule_fingerprint`. `ac2_ai_findings_keep_the_order_the_model_gave` | pass (3) |
| AC3 | `ac3_counts_and_skipped_files_with_one_of_each_outcome`: the sample plus `README.md`, one dropped finding (line 999), one deduped (tagged, on 35), and `--threshold medium` putting the `low` rule finding below it. Counts are exactly AC3's, `findings` holds only the line-35 rule finding, and `skipped_files` is `README.md`/`docs`. `ac3_reported_counts_the_listed_findings`. `ac3_each_skipped_file_is_listed_with_its_reason` ×2 (docs, lock) | pass (4) |
| AC4 | `ac4_no_findings_gives_an_empty_list_a_complete_document_and_exit_0`. `ac4_only_below_threshold_findings_exit_0_with_none_listed`. `ac4_called_provider_usage_is_reported` (control: `usage` is exactly `{"input_tokens": 1234, "output_tokens": 567}`). `ac4_all_files_skipped_gives_a_complete_document_with_usage_zeros`: both 0, with that same non-zero provider never called | pass (4) |
| AC5 | The rationale and the path both hold `\x1b[31m`, a newline, U+2028, `→` and `"`. The path comes through an octal-quoted header and carries a rule finding and an AI finding. `ac5_hostile_rationale_and_path_give_one_ascii_line`; `ac5_parsed_values_equal_the_originals` (no `path_quoted`, as the path is valid UTF-8); `ac5_hostile_skipped_path_round_trips`. Limits ×7: NUL, DEL, U+0085, U+2029, BOM, an astral character, a backslash. Limits: 3,000 findings stay on one line, in about 2.7 s here (bound 60 s) | pass (11) |
| AC6 | `ac6_same_run_twice_gives_identical_documents`: equal values, equal key order at every level, and equal text with `run_id` replaced. `ac6_top_level_key_order_is_the_same_for_every_outcome` (findings, none, all skipped) | pass (2) |
| AC7 | `ac7_failure_leaves_stdout_empty_in_every_format_with_stderr_as_text` ×19: 2 (bad threshold, unknown flag), 3, 4 (directory, not UTF-8), 5, 6, 7 to 12, 13 (category, not JSON), 14, 15, 16, 17 (unwritable log). Each runs with no format, `--format text` and `--format json`: same code, empty stdout, and stderr equal with `run_id` aside. `ac7_cases_cover_every_code_from_2_to_17`. `ac7_log_failing_before_the_report_exits_17_with_stdout_empty` ×3. The Compatibility exception: `ac7_exception_log_failing_after_the_report_keeps_the_whole_document` ×2 (exit 17, full document) | pass (25) |
| AC8 | In a real process with `--format json`: `ac8_stdout_failure_exits_14_with_one_error_line` ×5 (closed, reader gone, `/dev/full`, and reader gone and `/dev/full` while writing 5,002 findings), with no traceback. Controls: the command delivers a report, and the large one outgrows the stream buffers | pass (5), skip (2: no `/dev/full` on Windows) |
| AC9 | `ac9_text_stdout_is_byte_identical_to_the_golden` ×28: 7 goldens (P0-S1 ×2, P0-S2, P1-S4 ×3, P1-S6) × 4 ways of choosing text (default, flag, variable, and the flag over `REVIEW_BOT_FORMAT=json`). `ac9_text_stderr_is_the_golden_plus_run_id` ×12. `ac9_json_summary_line_is_the_text_summary` ×3. `ac9_json_and_text_stderr_match_on_the_sample` | pass (44) |
| AC10 | `ac10_unknown_format_flag_exits_2_naming_the_flag` ×4 (`yaml`, empty, `JSON `, `markdown\n`), with no provider call. `ac10_unknown_format_variable_exits_2_naming_the_variable`. `ac10_unknown_format_error_is_one_safe_line` (ESC and a newline in the variable). `ac10_variable_alone_selects_json`. `ac10_flag_beats_an_invalid_variable`. `ac10_invalid_flag_over_a_valid_variable_names_the_flag`. `ac10_format_appears_in_the_logs_run_start` (design.md Audit log) | pass (10) |
| AC11 | `ac11_run_id_matches_the_summary_and_every_log_record` ×2 (reviewed, all skipped). `ac11_each_run_has_its_own_run_id`. `ac11_child_process_run_id_matches_its_summary` | pass (4) |
| Notes | Non-UTF-8 paths. `notes_rule_finding_and_skipped_file_on_non_utf8_paths_are_quoted`: a swallowed handler in `"caf\377.py"` plus `"docs/caf\377.md"` gives exit 1 (not 14), `file` `"caf\377.py"` (quotes included) with `path_quoted: true`, and the same for the skipped file. `notes_non_utf8_rule_finding_keeps_its_fingerprint` (over the decoded path, as `static.rules` logs it). `notes_only_the_object_on_a_non_utf8_path_is_flagged`. `notes_only_file_skipped_on_a_non_utf8_path_exits_0`. Deleted files: `notes_skipped_deleted_file_is_listed_by_its_old_path`, and `…_with_a_non_utf8_old_path_is_quoted`. Round 1, no redaction: `notes_path_holding_an_end_marker_is_written_as_the_real_path`. A swallowed handler in `x` + END marker + `.py` gives `file` equal to the real path, with no `path_quoted`, and the text run's line starts with that same `file:line` | pass (7) |

### Readings for the designer

Readings 1, 2, 4 and 7 are resolved by the spec's "Changed in build" block (main session, 2026-11-06), and the tests now pin what it says.

1. **Resolved: AC6's "fixed clock" has no public seam.** `main` takes no clock, and `report/v1` holds no timestamp, so the two runs are compared as they are. The spec now says the clock is moot. If a time field were ever added, AC1's exact key check would fail first.
2. **Resolved: `usage`'s keys weren't named.** The spec now names `input_tokens` and `output_tokens`, and the tests check `usage` exactly.
3. **Exact key sets are a reading.** The top level is exactly Interfaces' list, and each finding is exactly AC1's 11 keys, plus `path_quoted` only on a non-UTF-8 path. An added key fails AC1.
4. **Resolved: "one line" allows one final `\n`.** The spec now says one line followed by a single `\n`, and the tests require it.
5. **Key order** is checked as stable between runs and across outcomes, not against Interfaces' listing order, which the spec doesn't require. The developer's order matches the listing.
6. **AC7's "every format"** is tested for the default, `text` and `json`. `markdown` belongs to P1-S11, which should extend AC7's case list.
7. **Resolved: an AI finding can't be on a non-UTF-8 path.** `Finding` validation rejects a lone surrogate, so `path_quoted` reaches rule findings and skipped files only. The spec now says so.

### Coverage

100% overall in the coordinator's gate before review round 1, and again after it (2639 passed, 14 skipped, re-run by the reviewer in round 2), so the slice's modules (`render`, `settings`, `cli`) were fully covered. From this file alone: `render.py` and `settings.py` 100%, `cli.py` 85%; the uncovered lines are the Claude provider's paths and others outside this slice, covered elsewhere. Coverage after round 1, including the new `report` module, waits for the go-ahead. Nothing in the criteria was left untested; `/dev/full` runs only in CI.

## P1-S11: Get the review as Markdown

Spec: [p1-s11-markdown-output.md](slices/p1-s11-markdown-output.md). Tests: [`tests/integration/test_p1_s11_markdown_output.py`](../../../tests/integration/test_p1_s11_markdown_output.py), 159 cases. Goldens: [`tests/fixtures/p1_s11/`](../../../tests/fixtures/p1_s11/): `sample.golden.md` (AC1), `no_findings.golden.md` (AC2), `counts_and_skipped.golden.md` (AC3). Runs go through `main` with an injected `MockProvider`. The stdout failure cases, one Notes case and one control run a real process, as P1-S10 AC8's do. AC7 reuses P1-S10's failure triggers (`failures`, `FAILURE_CASES`) and text goldens (`GOLDEN_CASES`).

**Status: every criterion passes: 158 passed and 1 skipped, from this file alone, on the developer's uncommitted working tree after review round 1** (base `d82b241`). The skip is `/dev/full`, which runs only in CI.

**First run, on the in-progress code:** 130 passed, 6 failed, 1 skipped (137 cases). The 6 failures were AC4's: `@someone` was still a mention, and bare URLs were left for GitHub's autolinker (Failures, below). The spec then changed in build (main session, 2026-11-06): AC4 is judged as GitHub-flavoured Markdown too, and every word holding `@` or a URL goes in a code span. After that change, the parser gained linkify, the direct code-span check was added, and AC4 gained 21 cases. The separate bare-URL test was folded into them.

**Review round 1:** `ftp://evil.com/x` was left outside code (Failures, item 3). The tests widened the trigger to `://` for any scheme and gained 5 AC4 cases, for 159 in all.

### Gate

Gate: see the main session's run. The tester ran only this file after review round 1, as the coordinator asked. The tester's earlier full run, before round 1 with this file at 154 cases, was ruff and mypy clean, and `pytest --cov` gave 2842 passed, 15 skipped, 100.00% coverage.

**Before the code.** Against `HEAD` (`d82b241`) in a scratch worktree, with `markdown-it-py` from the developer's dev dependency: 88 failed, 17 passed, 1 skipped, 31 errors. The errors are AC4's control fixture, because `--format markdown` exited 2. The 17 passes are AC7's text goldens (pre-slice behaviour, as AC7 requires), AC7's two exit-2 failure cases, and AC3's "no `Skipped files`" case, which passes on any output without that phrase.

### How the output is read

Stdout is parsed by `markdown-it-py` (`[linkify]` extra): the CommonMark preset plus the table rule and linkify, GitHub's table and autolink extensions. Before the spec change, only the table rule was on. Every run checks that stdout has no lone surrogate, encodes as UTF-8, and holds no `Cc`, `Zl` or `Zp` character other than `\n`. A finding's list item is one whose code spans include one ending in `:<line>`.

"Inert" (AC4, AC5) is checked against a **control run** that differs only in using a plain value. The checks are: the same block structure (type, tag, nesting); the same count of `html_inline`, `image`, `link_open`, emphasis and strikethrough; the same headings; and the same number of finding items. The spec's code-span rule (Interfaces, changed in build) is **checked directly** as well as through the parser, because linkify doesn't link an escaped URL such as `http\:\/\/evil` and GitHub does. The direct check is `assert_code_span_rule`. It requires that no `@`, `://` (any scheme) or `www.`, in any case, is left anywhere in the document's text outside code, after escapes are undone. It also requires that each whitespace-delimited word of the hostile value holding one sits inside a code span. It runs in every AC4 and AC5 case and in the 3,000-findings case. "Visible text preserved" means each whitespace-separated word of the hostile value appears, in order, in the item's visible text (text plus code span contents, after Markdown escapes are undone). So a newline can show as a space or as `\n`, but nothing can be dropped or swallowed as syntax. Before any `src/` code existed, a self-check against hand-written renders showed that the checks pass a code-span render and a backslash-escaped one. It also showed they catch a raw render on every payload that breaks it. After the spec change, the same self-check fails the backslash-escaped render on all 27 payloads holding `@` or a URL, which is the developer's first version. A render with the value in a code span passes them all. After review round 1 it also fails that render on the 5 new scheme cases, with "`://` outside a code span".

### Criteria

Test names drop the `test_p1_s11_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_sample_with_one_ai_finding_matches_the_golden_and_exits_1`. `ac1_starts_with_the_review_heading_then_counts_then_the_findings`: the first block is h2 `Review`, then a reported count of 3 before the list. `ac1_one_list_item_per_finding_rules_first_with_location_in_a_code_span`: `billing/invoice.py:35`, `:39`, `:14`, in that order. `ac1_each_item_shows_severity_category_rationale_confidence_and_source` ×3. `ac1_rule_ids_dont_change_the_sources`: a tagged AI finding stays `ai` | pass (7) |
| AC2 | `ac2_no_findings_is_the_heading_and_the_sentence_and_exits_0`: exactly an h2 and one paragraph. `ac2_no_findings_matches_the_golden`. `ac2_sentence_names_the_threshold_in_use`: `--threshold critical` on the sample | pass (3) |
| AC3 | `ac3_skipped_files_section_lists_each_file_with_its_reason`: `README.md` docs and `uv.lock` lock, after the `Skipped files` heading. `ac3_no_skipped_files_section_without_skipped_files`. `ac3_all_files_skipped_still_gives_the_heading_and_the_section`, with no provider call. `ac3_counts_say_how_many_were_dropped_and_deduped`: 3 dropped and 1 deduped, distinct numbers so neither can match the other's count. `ac3_dropped_and_deduped_findings_arent_listed`. `ac3_matches_the_golden` | pass (6) |
| AC4 | `ac4_hostile_rationale_is_inert` ×52. The spec's first 9: `\n## Forged`, `<img onerror>`, an image, a link, a fenced block, inline backticks, `@someone`, `\|`, `*`. The task's 8: `#` first, `>` and `\n>`, `- ` and `\n- `, `1. ` and `\n1. `/`2)`, `<script>`. 9 more: a newline table, a setext heading, `---`, indented code, `<http://evil>`, entities (`&lt;b&gt; &#64;someone`), an HTML comment, a reference link, `(@someone)`. The spec's GitHub-flavoured list ×15 (`gfm_…`): `\@someone`, `a@b.com`, `http://evil`, `https://evil/x` and `www.evil.com`, each bare, in `[…]` and in `\[…\]`. Beyond that list ×6: `HTTP://EVIL`, `WwW.Evil.com`, a URL then a full stop, a lone `@`, a URL glued to a word by a comma, and four triggers in one rationale. Review round 1, any scheme ×5: `ftp://evil` bare, in `[…]` and in `\[…\]`; `FTP://EVIL`; and, beyond the request, `git+ssh://evil/x` | pass (52) |
| AC4, limits | `ac4_limits_large_hostile_rationale_is_inert_and_bounded` ×5: backtick runs of 1 to 60, 2,000 nested `[`/`](`, 2,000 `<`/`>`, 2,001 backslashes then `*x*`, and 500 numbered lines (bound 60 s). `ac4_limits_thousands_of_findings_stay_one_item_each_and_bounded`: 3,000 hostile findings (each with `@someone`) give 3,000 items, one h2, no HTML, and the code-span rule holds, in about 4 s here | pass (6) |
| AC5 | `ac5_hostile_path_of_a_finding_is_inert` ×10 and `ac5_hostile_path_of_a_skipped_file_is_inert` ×10. Paths come through a git-quoted (octal) header. The spec's: a backtick, a double backtick, a newline then `## Forged`, and `<img onerror>`. Beyond it: `<script>`, a link, `@someone`, `\|` and `*`, a leading `#`, and a leading backtick. `ac5_plain_path_is_shown_exactly_in_its_code_span` | pass (21) |
| AC6 | `ac6_control_in_a_rationale_isnt_raw_on_stdout` ×10: ESC and U+2028, plus NUL, BEL, CR, TAB, DEL, NEL, CSI (U+009B) and U+2029. `ac6_control_in_a_finding_path_isnt_raw_on_stdout` ×2 (rule finding), `…_in_an_ai_finding_path_…` ×2, `…_in_a_skipped_path_…` ×2 | pass (16) |
| AC7 | Text unchanged: `ac7_text_stdout_is_byte_identical_to_the_golden` ×14, P1-S10's 7 goldens × (default, and `--format text` over `REVIEW_BOT_FORMAT=markdown`). `ac7_markdown_summary_line_is_the_text_summary`. Handover: `ac7_failure_leaves_stdout_empty_in_markdown_with_stderr_as_text` ×19 covers P1-S10's exits 2 to 17, each with no format and with `--format markdown`: same code, empty stdout, and stderr equal with `run_id` aside. `ac7_log_failing_before_the_report_exits_17_with_stdout_empty` ×3. The Compatibility exception: `ac7_exception_log_failing_after_the_report_keeps_the_whole_report`. In a real process: `ac7_stdout_failure_exits_14_with_one_error_line` ×4 (closed, reader gone, `/dev/full`, and reader gone while writing 5,002 findings), and its control | pass (42), skip (1: no `/dev/full` on Windows) |
| Notes | Non-UTF-8 paths. `notes_non_utf8_finding_and_skipped_paths_are_shown_quoted_and_exit_1`: a swallowed handler in `"caf\377.py"` shows as `"caf\377.py":3` in a code span with source `rule`, and `"docs/caf\377.md"` is listed. `notes_only_file_skipped_on_a_non_utf8_path_exits_0`. `notes_non_utf8_path_is_written_through_a_real_stdout`: in a child process, exit 1, not 14 | pass (3) |
| Settings | design.md Settings: `settings_variable_alone_selects_markdown`, `settings_markdown_flag_beats_a_json_variable` | pass (2) |

### Failures

None now. All three are fixed in the developer's code; they're kept here as the record.

1. **Fixed. AC4: `@someone` was still a mention.** The renderer backslash-escapes it. CommonMark turns `\@someone` back into the text `@someone`, and GitHub finds mentions in rendered text outside code, so it would notify that user. To reproduce: review the sample diff with `--format markdown` and one AI finding whose rationale is `Before @someone after.`. That gives this item:

   ```
   - **high** logic at `billing/invoice.py:14`: Before \@someone after\. (confidence 0.80, source: ai)
   ```

   `(@someone)` fails the same way. A path holding `@someone` passes, because paths sit in a code span. A code span, or another form that keeps the visible text, would pass the test.
2. **Fixed. Bare URLs stayed linkable on GitHub.** `[click](http://evil)` rendered as `\[click\]\(http\:\/\/evil\)`. That's inert as CommonMark, but GitHub's autolinker would still make `http://evil` a live link. The question for the designer was answered by the spec change (main session, 2026-11-06): AC4 is judged as GitHub-flavoured Markdown too. The separate test `ac4_limits_no_bare_url_left_for_githubs_autolinker` is gone, because `assert_code_span_rule` now covers its 4 payloads inside `ac4_hostile_rationale_is_inert`.
3. **Fixed, found in review round 1, not by these tests. URLs with other schemes were left outside code.** `ftp://evil.com/x` wasn't put in a code span, so GitHub could autolink it. No test caught it, because `GFM_TRIGGER` had copied the code's narrow `https?://` rule instead of testing the risk. That was the tester's miss. The spec now says `://` for any scheme, in any case. `GFM_TRIGGER` is now `@|://|www\.` (case-insensitive), and AC4 gained `ftp://evil` (bare, in `[…]`, in `\[…\]`), `FTP://EVIL` and `git+ssh://evil/x`. On the current code, a rationale of `Before ftp://evil FTP://EVIL after.` renders as ``Before `ftp://evil` `FTP://EVIL` after\.``. The developer's fix was already in the working tree at the first run of these cases, so they were never seen failing against the code, only against the self-check's escaped render.

### Readings for the designer

1. **AC1's layout isn't specified beyond its elements.** So `sample.golden.md` and `counts_and_skipped.golden.md` were **captured from the developer's output**, after the structural tests passed on it, and checked by hand against AC1 and AC3. `no_findings.golden.md` was written from AC2 before any code existed. A layout change needs the goldens re-captured, and that change should be reviewed. Re-checked on the final code: all three goldens still match byte for byte, and none holds an `@` or a URL, so the code-span change didn't touch them. The independent checks are the structural tests, which don't depend on the layout. They check the order of heading, counts and list; `file:line` in a code span; severity, category, rationale, confidence (`1.00`, `1` or `100%` for 1.0) and source as words; and `N dropped` or `dropped: N` close together.
2. **AC2 is read strictly.** With nothing reported, dropped, deduped or below the threshold, stdout is the heading and the one sentence, with no counts line. When something is below the threshold, the sentence only has to be present (`ac2_sentence_names_the_threshold_in_use`).
3. **Settled by the spec change: mentions and URLs.** My reading was GitHub's mention rule (`@` then an alphanumeric, outside code). The spec's Interfaces now set a stricter rule, which the tests check: every whitespace-delimited word holding `@`, `http://`, `https://` or `www.` goes in a code span. So a lone `@` and an email address go in one too.
4. **"Visible text preserved"** is read as the hostile value's words, in order. Zero-width characters inserted inside a word would fail it.
5. **Readability, not a criterion.** Escaping every ASCII punctuation mark makes the raw Markdown hard to read (`re\-raise`, `encoding\=\"utf\-8\"`). The rendered review reads fine.

### Coverage

Before review round 1, coverage was 100.00% overall (1,486 statements, 362 branches, none missed), so the slice's modules (`render`, `cli`, `settings`) were fully covered. For coverage after round 1, see the main session's gate. Nothing in the criteria was left untested. `/dev/full` runs only in CI. GitHub's own renderer can't run offline, so "GitHub neither links nor notifies" is evidenced by linkify plus the direct code-span check, not by GitHub itself.

## P1-S12: Accept or reject findings interactively, with labels logged

Spec: [p1-s12-triage.md](slices/p1-s12-triage.md). Tests: [`tests/integration/test_p1_s12_triage.py`](../../../tests/integration/test_p1_s12_triage.py), 102 cases. In-process runs go through `main` with an injected `MockProvider`, and `sys.stdin`, `sys.stdout` and `sys.stderr` are replaced by `FakeStream`s. Each fake says whether it's a terminal through `isatty`, has no descriptor, and records every read and write on a shared timeline (AC10). Four cases call `run_triage` directly. AC5 also runs real child processes.

**Status: every criterion passes: 99 passed and 3 skipped, from this file alone, on the developer's uncommitted working tree during review round 1** (base `b754b09`). The 3 skips are the POSIX pseudo-terminal cases, which run only in CI (Gaps).

**Before the code.** The developer's code was already on disk at this file's first run, so the file was also run against `HEAD` in a scratch worktree: 81 failed, 11 passed, 3 skipped. The 11 passes are the cases without `--triage` (AC3's all-null case and AC7's ten), which pre-slice behaviour already satisfies. Every case with `--triage` failed; the AC5 ones failed because argparse's `unrecognized arguments: --triage` doesn't say a terminal is needed. On the working tree, the first run had 1 failure, a test bug: the baseline and triage runs share one log, so `run.start` appeared twice. Records are now scoped to the last run's `run_id`.

**Review round 1** added 6 cases: AC8's stdout-failure case ×3 and AC4's answer normalisation ×3. AC8's event name is now asserted too. All of them passed on their first run on the developer's working tree.

**Mutation check** (scratch pytest plugins, `src/` untouched). With the Windows console check removed, so that `isatty` alone decides, `ac5_stdin_from_the_null_device_exits_2` fails. With prompts written to stdout, 41 cases fail (AC1 to AC6, AC9, AC10, AC12). With labels recorded only after the report is written, the order before round 1, the 3 stdout-failure cases fail.

### Gate

The tester ran ruff (check and format) on this file and ran the file alone, as the coordinator asked. The full gate waits for the coordinator's go-ahead.

### Seam

The developer's `console.is_terminal` trusts `isatty` for a stream with no descriptor, and asks the Windows console (`GetConsoleMode`) when there is one. So the fakes stand for terminals, and a real `os.devnull` stream (`NUL` on Windows, where `isatty` is true) exercises the console check in-process. No adaptation was needed.

### Criteria

Test names drop the `test_p1_s12_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_each_finding_is_shown_once_with_its_fields`: a rule and an AI finding, each rationale on exactly one stderr line holding its file, line, severity, category and source. `ac1_accept_then_reject_reports_only_the_accepted_one_and_exits_1` ×2 (text, Markdown). `ac1_reject_then_accept_reports_only_the_second` | pass (4) |
| AC2 | `ac2_all_rejected_exits_0_with_the_no_findings_output` ×2: text is `No findings at or above low.`, Markdown equals `p1_s11/no_findings.golden.md`. `ac2_all_rejected_json_keeps_them_marked_reject_with_their_rationale`: `reject` ×2, rationales kept, `reported` 0, `rejected` 2 | pass (3) |
| AC3 | `ac3_accepted_finding_is_marked_accept`. `ac3_without_triage_every_finding_is_null`. `ac3_with_triage_no_finding_is_null` ×4: all accepted, mixed, EOF part-way, Ctrl+C at once | pass (6) |
| AC4 | `ac4_invalid_answer_asks_again` ×6: `x`, empty, spaces, `ar`, `accept?`, `q`. `ac4_x_then_empty_line_still_leaves_the_first_undecided`: 4 prompts, then `reject` ×2. `ac4_x_then_eof_leaves_the_finding_undecided`. `ac4_answers_are_matched_after_strip_and_lower` ×3 (round 1): `A` and ` r `, `a\n` and `R\n`, `\tA\t` and `  R` each give `accept, reject`. `ac4_limits_a_megabyte_answer_asks_again_within_the_bound`, and the answer isn't echoed in full. `ac4_run_triage_asks_again_with_the_prompt_each_time`: 3 prompts, each holding `[a]ccept [r]eject` | pass (13) |
| AC5 | In-process: `ac5_without_a_terminal_exits_2_naming_the_flag` ×3 (stdin, stderr, neither): exit 2, one `review-bot: error:` line holding `--triage` and "terminal", stdout empty, no stdin read, no provider request, under 5 s. `ac5_stdin_from_the_null_device_exits_2`: a real `os.devnull` stdin with a terminal stderr. `ac5_the_diff_is_not_read`: a missing diff gives 2, not 3. `ac5_terminal_stdin_and_stderr_are_enough` ×2 (stdout redirected, stdout a terminal). Child process, with a 5 s hard timeout on the `subprocess` wait: `ac5_real_process_without_a_terminal_exits_2_within_5_s` ×4 (a pipe held open with nothing written, a pipe holding `a\na\n`, `os.devnull`, and literal `NUL` on Windows): exit 2 under 5 s, one error line, no traceback, and the log, if written, has no `parse` or `agent` record. POSIX only: `ac5_real_process_with_a_terminal_stderr_but_not_stdin_exits_2` ×2 (stderr a pseudo-terminal, so only the stdin check can fire), and the control `ac5_real_process_with_terminal_stdin_and_stderr_triages` | pass (12), skip (3: POSIX only) |
| AC6 | `ac6_stopping_part_way_leaves_the_rest_undecided` ×2 (EOF, Ctrl+C): 2 prompts and 2 reads for 3 findings, `accept, undecided, undecided`, `counts.undecided` 2, exit 1, no traceback or `KeyboardInterrupt`. `ac6_undecided_findings_stay_in_the_text_report` ×2. `ac6_undecided_findings_get_no_label` ×2: one `triage` record, the accepted one. `ac6_stopping_at_the_first_prompt_exits_1_with_all_undecided` ×2. `ac6_rejected_then_stopped_still_exits_1` ×2. `ac6_the_run_ends_in_the_log_with_exit_1` ×2. `ac6_limits_a_broken_stderr_stops_asking_and_reads_nothing`. `ac6_run_triage_returns_on_ctrl_c_instead_of_raising` | pass (14) |
| AC7 | `ac7_without_the_flag_nothing_reads_stdin` ×9: text, JSON and Markdown × `REVIEW_BOT_TRIAGE` unset, `true` and `1`, all with terminal stdin and stderr. `ac7_the_variable_does_not_need_a_terminal`: no exit 2. `ac7_no_findings_with_triage_asks_nothing` ×3 | pass (13) |
| AC8 | `ac8_one_record_per_decision_with_the_label_fields`: 2 records, each with event `labelled`. Each `data` is exactly the Label's 6 fields (with `source`) plus `model` and `prompt_version`. Fingerprints and severities match the JSON report, `rationale_changed` is false, and `model` and `prompt_version` equal `run.start`'s. `ac8_triage_records_hold_no_rationale_text`: neither the rationale nor any of its words longer than 6 characters. `ac8_records_come_after_the_policy_and_before_the_end`. `ac8_labels_are_logged_even_when_stdout_fails` ×3 (round 1): stdout failing on write, failing on flush (the conftest `failing_stream` helper), or closed. Each exits 14 with no traceback, the log still holds one `triage`/`labelled` record with `accept`, and `run.end` has exit code 14. `ac8_run_triage_returns_a_label_per_decision` | pass (7) |
| AC9 | `ac9_hostile_rationale_is_shown_on_one_clean_line` and `ac9_hostile_path_is_shown_on_one_clean_line` (git-quoted octal header). Both use `\x1b[31m` and a newline. The start and end markers share a stderr line, and stderr has no `Cc`, `Zl` or `Zp` character other than `\n`. `ac9_limits_other_controls_in_a_rationale_stay_inert` ×8: CR, BEL and BS, U+2028, U+2029, NEL, NUL, DEL, and an OSC title sequence. `ac9_limits_a_long_rationale_stays_one_line_within_the_bound`: 20,000 escape-and-newline pairs. `ac9_run_triage_writes_and_prompts_single_clean_lines`: every `write_line` text and `read_line` prompt | pass (12) |
| AC10 | `ac10_prompts_go_to_stderr_and_the_report_follows_the_last_answer` ×3 (formats): no prompt on stdout, and the first stdout write comes after the last read on the timeline. `ac10_all_accepted_stdout_is_the_report_without_triage` ×2: byte-identical to a run without `--triage`. `ac10_json_stdout_is_one_document`. `ac10_stdout_is_untouched_when_stopped_by_ctrl_c` | pass (7) |
| AC11 | `ac11_secret_hit_exits_16_and_asks_nothing`, with a generated key and no provider call. `ac11_invalid_setting_exits_2_and_asks_nothing`: `--threshold bogus`, with the error naming `--threshold`. `ac11_provider_failure_keeps_its_code_and_asks_nothing` ×3: `auth` 8, `refusal` 10, `invalid_output` 13. All run with terminal stdin and stderr, so triage would otherwise run | pass (5) |
| AC12 | `ac12_below_threshold_findings_are_not_asked_about`: `--threshold high` with a high and a low finding. It gives 1 prompt and 1 read, the low rationale never reaches stderr, `below_threshold` is 1, and it exits 0 after `r`. `ac12_only_below_threshold_findings_asks_nothing` | pass (2) |
| Limits | `limits_hundreds_of_findings_are_each_asked_once_within_the_bound`: 300 findings, 300 prompts, all rejected, exit 0. `limits_answers_after_the_last_finding_are_not_read` | pass (2) |

### Failures

None on the current code.

### Readings for the designer

Answered in review round 1 by the main session, and pinned in the spec's Interfaces and design.md's Audit log table (changed in build, 2026-11-07).

1. **Answered: EOF is signalled by `EOFError` from `read_line`.** EOF is still tested through `main`, where stdin really ends.
2. **Answered: the attribute is `TriageResult.labels`.** Two direct tests read it, and one also reads `Label.source`.
3. **Answered: answers are matched after `.strip().lower()`.** Tested by `ac4_answers_are_matched_after_strip_and_lower`.
4. **Answered: the event is `labelled`, and `source` is one of the Label's 6 fields.** The record adds `model` and `prompt_version`, so each `data` still has 8 fields. AC8 asserts both.
5. **Open: AC5's 5 s also bounds interpreter start-up in the child-process cases.** These take about 1 s here. If slow CI flakes, the bound is the spec's, so changing it is a designer decision.
6. **Answered, not a criterion:** the Notes' help-line correction (`--triage` has no variable) is tested by `tests/unit/test_cli_main.py::test_parse_args_help_says_triage_has_no_variable`.

### Gaps

- **Windows child process.** Stderr is a pipe there, so the child is refused whatever stdin is. The child cases prove the CLI exits 2 without hanging. The stdin check itself, on a real `NUL`, is proven in-process by `ac5_stdin_from_the_null_device_exits_2`. A test can't attach a real Windows console to a child, so the positive case on Windows is in-process only.
- **POSIX pseudo-terminal cases (3)** are skipped on Windows and couldn't run locally: there's no Linux Python here. Their first run is CI. The control case writes `a\n` to the terminal before the child starts.
- **`NUL` (Windows only)** and the pty cases (POSIX only) never run on the same machine, as the spec's Notes say.

### Coverage

From this file alone: `triage` 100%. `console` 79%; the rest is pre-existing stream-failure handling, the non-Windows branch of the console check, and `get_osfhandle` failing. `cli` 78%; the rest is outside triage. Whole-suite coverage waits for the full gate.

## P1-S13: Edit a finding during triage

Spec: [p1-s13-triage-edit.md](slices/p1-s13-triage-edit.md). Tests: [`tests/integration/test_p1_s13_triage_edit.py`](../../../tests/integration/test_p1_s13_triage_edit.py), 80 cases. They reuse P1-S12's seam: `main` runs with an injected `MockProvider` under `test_p1_s12_triage`'s `run_bot` fixture and `FakeStream`s, and its fixtures are shared by name. AC7's line-splitting case uses a real `io.StringIO` stdin that says it's a terminal. Five cases call `run_triage` directly.

**Status: every criterion passes: 80 passed, from this file alone, on the developer's uncommitted working tree** (base `bb02fcc`).

**Before the code.** The developer's code was already on disk at the first run, so the file was also run against `HEAD` in a scratch worktree: **80 failed, 0 passed**. On a first pass 7 cases passed there. `HEAD` treats `e` as an invalid answer and asks again, and a later EOF leaves the finding undecided with its original values, which is also AC9's outcome. Each of those cases now also asserts that `e` starts an edit (the decision prompt is shown once, or the new text reaches the report), so none passes on pre-slice code. On the working tree, the first run had 3 failures, all test bugs. Two gave a `list_iterator.__next__` as `read_line`, which takes no prompt argument. In the third, AC6's "no new text in the log" check matched the two-letter word `in` in an unrelated record. Only words over 6 characters are checked now, as in P1-S12 AC8.

### Gate

The tester ran `ruff format --check` and `ruff check` on this file, and ran the file alone, as the coordinator asked. The full gate waits for the coordinator.

### Criteria

Test names drop the `test_p1_s13_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_edited_ai_finding_shows_the_new_severity_and_rationale` ×2 and `ac1_edited_rule_finding_shows_the_new_severity_and_rationale` ×2 (text, Markdown): the entry for that `file:line` has severity `low` and `Better text.`, and the old text is gone from stdout (AI case). `ac1_json_marks_edit_and_keeps_the_models_text`: `triage` `edit`, `low`, the new text, and `original_rationale` equal to the model's. `ac1_json_keeps_the_rules_text_and_leaves_the_accepted_one_alone`: a rule finding's `original_rationale` is the rule's text, and the accepted AI finding's is null. `ac1_the_prompt_offers_edit`: stderr holds `[a]ccept [r]eject [e]dit`. `ac1_e_is_matched_like_the_other_answers` ×3 (`E`, ` e `, `e\r`). `ac1_severity_is_matched_after_strip_and_lower` ×3 (`LOW`, ` low `, `Low\r`), with no second ask. `ac1_every_severity_can_be_chosen` ×4. `ac1_unicode_rationale_is_kept_exactly`. `ac1_the_next_finding_is_asked_as_usual_after_an_edit` | pass (19) |
| AC2 | `ac2_two_empty_answers_keep_the_values_with_label_edit`: `edit`, severity and rationale unchanged, `original_rationale` null. `ac2_two_empty_answers_log_an_unchanged_edit`: label `edit`, `severity_before` equal to `severity_after`, `rationale_changed` false. `ac2_an_unchanged_edit_renders_like_an_accept` ×2: 3 reads, and stdout byte-identical to accepting. `ac2_a_blank_rationale_counts_as_empty` ×2 (spaces, tab). `ac2_severity_only_edit_keeps_the_rationale`. `ac2_rationale_only_edit_keeps_the_severity` | pass (8) |
| AC3 | `ac3_unknown_severity_asks_again` ×6 (`urgent`, `info`, `crit`, `a`, `e`, `low!`): exactly one extra read, then `low` and the new text stand. `ac3_unknown_severity_leaves_the_other_findings_alone`: two invalid severities, then empty answers keep the values, and the next finding is accepted as usual. `ac3_unknown_severity_isnt_shown_back_in_full`: a megabyte severity, within 60 s, not echoed. `ac3_run_triage_asks_for_severity_again`: prompts 2 and 3 both name "severity" | pass (9) |
| AC4 | `ac4_edit_keeps_the_frozen_fields` ×2 (rule, AI): the finding is found by its baseline `fingerprint`, and `file`, `line`, `source`, `rule`, `category`, `confidence` and `fingerprint` equal an untriaged run's. `ac4_an_edit_reads_only_severity_and_rationale`: 3 reads for one finding, with spare answers left unread. `ac4_edit_prompts_ask_for_severity_then_rationale_only`: the two edit prompts name "severity" and "rationale", and neither names `file`, `source`, `rule`, `category`, `confidence`, `fingerprint` or `anchor`. `ac4_run_triage_label_keeps_fingerprint_and_source` | pass (5) |
| AC5 | `ac5_critical_lowered_to_low_stays_with_threshold_high` ×3 (formats): exit 1. Text and Markdown show `low`; JSON has `low`, `edit`, `reported` 1 and `below_threshold` 0. `ac5_a_finding_below_the_threshold_still_isnt_asked`: P1-S12 AC12 holds alongside an edit | pass (4) |
| AC6 | `ac6_triage_record_holds_the_change_but_not_the_text`: the edit's record is `labelled`, with exactly P1-S12's 8 fields, `label` `edit`, `high` to `low` and `rationale_changed` true. No record of the run holds a word of the new text. `ac6_run_triage_label_records_the_change`: `critical` to `medium`, and the new text isn't in the label's `repr` | pass (2) |
| AC7 | `ac7_a_newline_ends_the_answer_by_construction`: a real line-splitting stdin holding `StartMark\n## Forged EndMark`. The rationale is `StartMark`, the rest is the next finding's (invalid) answer, and the Markdown has only the `Review` heading. `ac7_escape_in_new_text_renders_safely` ×3 (formats): `\x1b[31m` and an OSC title. No raw control in stdout, JSON parses back to the exact text, and Markdown and text keep both markers in one entry. `ac7_limits_a_line_that_still_holds_a_newline_renders_safely` ×3: a fake stream that breaks the line contract gives `\x1b`, `\n## Forged`, `\r` and U+2028 in one answer. Still no raw control, no forged heading, and two entries. `ac7_escape_in_new_text_isnt_echoed_raw_to_stderr`. `ac7_markdown_in_new_text_is_inert` ×3 (`\n## Forged`, `<img …>`, a link) | pass (11) |
| AC8 | `ac8_secret_in_new_text_is_redacted_in_the_report` ×6 (an assembled AWS key and GitHub token, × formats): the value isn't in stdout, and `[REDACTED]` is in that finding's entry. `ac8_secret_in_new_text_reaches_neither_stderr_nor_the_log`. `ac8_secret_in_new_text_doesnt_stop_the_report`: exit 1 and `edit`, not 16 | pass (8) |
| AC9 | `ac9_stopping_part_way_leaves_it_undecided_and_unchanged` ×6 (EOF, Ctrl+C × at the severity prompt, at the rationale prompt, after an invalid severity): the decision prompt is shown once, both findings are `undecided` with their original severities and rationales, `original_rationale` is null, `counts.undecided` is 2, it exits 1, and there's no traceback and no `triage` record. `ac9_earlier_decisions_stand_when_an_edit_is_cut_short` ×2. `ac9_cut_short_edit_shows_the_original_in_text` ×2. `ac9_run_triage_drops_a_cut_short_edit`: no label, and `e` wasn't refused | pass (11) |
| Limits | `limits_a_megabyte_rationale_finishes_within_the_bound`, and isn't echoed in full. `limits_hundreds_of_edits_within_the_bound`: 200 edits, 200 records. `limits_edit_then_stdout_failure_still_logs_the_label`: exit 14, and the `edit` label is logged | pass (3) |

### Failures

None on the current code.

### Readings for the designer

1. **"An empty answer keeps the current value" (spec preamble):** read to include a rationale that is blank after stripping (spaces, a tab), because `Finding.rationale` must hold a non-space. Pinned by `ac2_a_blank_rationale_counts_as_empty`.
2. **The `e` and severity answers are matched after `.strip().lower()`,** extending P1-S12's pinned rule for `a` and `r`. Pinned by `ac1_e_is_matched_like_the_other_answers` and `ac1_severity_is_matched_after_strip_and_lower`. The rationale itself is kept as typed apart from leading and trailing whitespace, then redacted, so `ac1_unicode_rationale_is_kept_exactly` holds its case and quotes.
3. **"The report shows … that rationale" (AC1):** read as text and Markdown showing only the new text. The old text survives only in JSON's `original_rationale`, per open question 9.
4. **"No prompt offers them" (AC4):** read as an edit reading exactly two answers (one more per invalid severity), with prompts naming "severity" and "rationale" and none of the frozen fields. If the designer wants other wording, only `FROZEN_WORDS` and that test change.
5. **AC7's "by construction"** rests on the CLI reading one line per answer. A `read_line` that returns embedded newlines (an injected one, not the CLI's) is outside the criterion. `ac7_limits_a_line_that_still_holds_a_newline_renders_safely` shows the renderers still hold the line in that case.
6. **Decided in the spec notes (2026-11-07): an answer equal to the current rationale counts as unchanged,** so `rationale_changed` is false and `original_rationale` null. Pinned by the unit test `test_run_triage_edit_with_the_same_text_does_not_count_as_a_changed_rationale`.
7. **Decided in the spec notes (2026-11-07): `original_rationale` is unredacted.** Only the new text is redacted. With `--format json`, `original_rationale` holds the model's or rule's text as it was (design.md: stdout isn't redacted). Not tested either way.

### Gaps

- **AC7 on a real terminal:** a test can't type into a real console, so line splitting is shown with an `io.StringIO` that says it's a terminal. It still goes through the CLI's own `readline`.

### Coverage

From this file alone: `triage` 100%. `render` 87% and `report` 70%; the rest is output this slice doesn't touch (skipped files, quoted paths, count wording), covered by P1-S10's and P1-S11's files. Whole-suite coverage waits for the full gate.
