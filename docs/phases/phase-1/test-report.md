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
