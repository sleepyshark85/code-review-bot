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
