# Phase 0 test report

## P0-S1: Review a diff offline, end to end, with clear input errors

Spec: [p0-s1-offline-review.md](slices/p0-s1-offline-review.md).

**Status: every criterion passes on the developer's final code.** The run was against the uncommitted working tree on base `6403a4f`, after fix round 2 of the review.

The tests were written from the spec and the design's public interfaces. `src/` wasn't read; the real prompts were read only as a provider receives them, through `MockProvider.requests`.

### Gate

| Check | Result |
|---|---|
| `ruff format --check .` | Pass |
| `ruff check .` | Pass |
| `mypy` (strict, `src` and `.claude/hooks`) | Pass |
| `pytest --cov`, whole suite | 318 passed, 100% coverage |
| `pytest tests/integration`, acceptance only | 212 passed, 97% coverage |
| `pre-commit run --all-files` | Clean, per the coordinator's last run; not run by the tester |
| `pip-audit` | Clean, per the coordinator's last run; not run by the tester |

The coordinator re-runs these two before committing.

### Criteria

Test names are shown without the `test_p0_s1_` prefix. Files are in `tests/integration/`. The count is the number of collected cases, including parametrised ones.

| AC | File `test_p0_s1_*` | Tests | Cases | Result |
|---|---|---|---|---|
| AC1 | `cli` | `ac1_command_exits_1`, `ac1_stdout_is_exactly_the_canned_finding_line`, `ac1_stderr_warns_mock_output_is_canned_and_not_a_real_review` (real command, subprocess) | 3 | Pass |
| AC2 | `cli` | `ac2_threshold_critical_exits_0`, `ac2_threshold_critical_stdout_is_exactly_no_findings`, `ac2_finding_is_kept_only_at_or_above_threshold`, `ac2_only_findings_at_or_above_threshold_are_printed`, `ac2_empty_findings_are_not_replaced_by_the_canned_one` | 20 | Pass |
| AC3 | `cli` | `ac3_successful_review_prints_the_summary_on_stderr`, `ac3_diff_text_appears_in_neither_stream` | 4 | Pass |
| AC4 | `cli` | `ac4_variable_alone_sets_the_value`, `ac4_flag_alone_sets_the_value`, `ac4_flag_beats_variable`, `ac4_flag_beats_invalid_provider_variable`, `ac4_overridden_variable_is_not_validated`, `ac4_neither_gives_the_default`, `ac4_parse_args_returns_the_path_given`, `ac4_model_flag_reaches_the_request`, `ac4_model_variable_reaches_the_request`, `ac4_model_flag_beats_variable_in_the_request`, `ac4_default_model_reaches_the_request`, `ac4_threshold_variable_changes_the_outcome`, `ac4_threshold_flag_beats_variable_in_the_outcome`, `ac4_max_diff_bytes_variable_is_enforced`, `ac4_max_diff_bytes_flag_beats_variable_when_enforced`, `ac4_provider_variable_selects_the_provider_for_a_real_run` | 28 | Pass |
| AC5 | `cli` | `ac5_bad_flag_returns_2_and_names_it` (includes `100000001` and `--model=--dangerous`), `ac5_bad_variable_returns_2_and_names_it` (includes `100000001` and `--dangerous`), `ac5_bad_provider_variable_returns_2_and_names_it`, `ac5_upper_bound_flag_is_accepted`, `ac5_upper_bound_variable_is_accepted`, `ac5_no_path_returns_2_and_names_the_missing_argument` | 21 | Pass; see open point 1 |
| AC6 | `cli` | `ac6_no_provider_selected_exits_2_and_names_the_flag`, `ac6_no_provider_selected_never_falls_back_to_canned_findings`, `ac6_empty_provider_variable_is_not_a_selection` | 3 | Pass |
| AC7 | `input_errors` | `ac7_missing_path_exits_3` | 2 | Pass |
| AC8 | `input_errors` | `ac8_directory_exits_4`, `ac8_utf16_from_powershell_redirection_exits_4`, `ac8_permission_error_on_read_exits_4`, `ac8_other_invalid_utf8_exits_4` | 6 | Pass |
| AC9 | `input_errors` | `ac9_empty_or_whitespace_only_exits_5` | 5 | Pass |
| AC10 | `input_errors` | `ac10_one_byte_over_the_flag_limit_exits_6_and_states_both_sizes`, `ac10_one_byte_over_the_default_limit_exits_6`, `ac10_the_limit_counts_bytes_not_characters`, `ac10_exactly_the_default_limit_is_accepted` | 4 | Pass |
| AC11 | `input_errors` | `ac11_file_at_the_limit_reaches_the_provider_whole` (ASCII, multi-byte, CRLF) | 3 | Pass |
| AC12 | `prompt` | `ac12_sample_diff_is_not_rejected`, `ac12_sample_diff_text_reaches_the_prompt_unchanged`, `ac12_template_metacharacters_reach_the_prompt_unchanged`, `ac12_crlf_line_endings_reach_the_prompt_unchanged`, `ac12_text_that_is_not_a_diff_is_accepted` | 5 | Pass |
| AC13 | `prompt` | `ac13_user_prompt_is_opening_line_diff_closing_line`, `ac13_sample_diff_is_framed_by_delimiter_lines`, `ac13_injection_attempt_stays_inside_the_delimiters`, `ac13_lookalike_delimiters_are_not_the_real_ones`, `ac13_system_prompt_does_not_contain_the_diff`, `ac13_system_prompt_says_delimited_content_is_untrusted_data`, `ac13_system_prompt_states_the_same_hash_rule`, `ac13_system_prompt_says_lookalike_delimiters_are_data` | 16 | Pass; see gap 2 |
| AC14 | `prompt` | `ac14_delimiters_from_another_diff_are_not_reused`, `ac14_delimiters_stay_unique_as_collisions_accumulate` | 4 | Pass; see gap 1 |
| AC15 | `prompt` | `ac15_system_prompt_requires_the_bare_json_shape`, `ac15_system_prompt_forbids_a_code_fence`, `ac15_system_prompt_labels_each_finding_field`, `ac15_system_prompt_names_each_enum_value`, `ac15_system_prompt_covers_everything_in_the_real_schema` | 18 | Pass; see gap 2 |
| AC16 | `offline` | `ac16_claude_is_not_on_path`, `ac16_no_path_entry_holds_a_claude_executable`, `ac16_api_key_is_absent`, `ac16_child_processes_get_the_same_environment`, `ac16_path_filter_removes_a_directory_holding_claude`, `ac16_external_connection_is_refused_in_tests`, `ac16_external_lookup_is_refused_in_tests`, `ac16_review_runs_with_no_subprocess_and_no_network` | 12 | Pass |
| AC17 | `repo_clean` | `ac17_package_files_list_no_openai`, `ac17_src_contains_no_openai_import`, `ac17_src_contains_no_key_literal`, and `ac17_control_*` | 8 | Pass |
| AC18 | `repo_clean` | `ac18_no_exclusion_covers_cli` (`pre-commit`, ruff, mypy, coverage), and `ac18_control_*` | 15 | Pass; see open point 3 |
| AC19 | `safe_output` | `ac19_hostile_file_renders_on_one_clean_line`, `ac19_hostile_rationale_renders_on_one_clean_line`, `ac19_forged_file_produces_no_second_finding_line`, `ac19_two_hostile_findings_render_on_two_lines`, `ac19_other_control_characters_are_not_rendered_raw` | 30 | Pass; see open point 2 |
| AC20 | `safe_output` | `ac20_arrow_in_rationale_on_ascii_stdout_exits_1`, `ac20_arrow_on_a_legacy_windows_stdout_exits_1`, `ac20_non_ascii_file_on_ascii_stdout_exits_1`, `ac20_the_simulated_stdout_really_rejects_the_arrow` | 4 | Pass |

One further case, `interface_finding_line_format`, pins the spec's finding line format against a golden file.

### Failures

None.

### Changes covered in fix round 2

- **Upper bound on the limit (AC5):** `--max-diff-bytes 100000001` and `REVIEW_BOT_MAX_DIFF_BYTES=100000001` exit 2 and name their source. Exactly `100000000` is accepted. The flag case runs a real review on a tiny diff, which briefly allocates about 100 MB, so it runs once; the variable case checks `parse_args` only.
- **`--model=--dangerous` (AC5):** exits 2 and names `--model`. `REVIEW_BOT_MODEL=--dangerous` is covered the same way, naming the variable.
- **Prompt's "report only" list:** it now includes maintainability problems. No acceptance test depends on that list's wording, and none needed changing; AC15 already requires `maintainability` as a category value.
- **Blank `file` or `rationale`:** now rejected, as confirmed through `Finding`. No acceptance test uses a blank value, so none needed changing. No acceptance test asserts the rejection either; it's covered in `tests/unit/test_findings.py`.

### Rulings recorded

- **Precedence:** a flag wins, and an overridden variable isn't read. `REVIEW_BOT_PROVIDER=nope --provider mock` exits 1 with the canned line. `ac4_flag_beats_invalid_provider_variable` covers this for the provider, and `ac4_overridden_variable_is_not_validated` for the model, threshold and limit.
- **`provider=None` for an injected provider:** acceptable and test-only. `main([path], provider=...)` with no `--provider` runs the review, and the summary reads `provider=None`. No test asserts this.
- **`parse_findings`:** stays strict.
- **AC14 delimiters:** a hash of the diff, with no collision loop, is acceptable (reviewer ruling; see gap 1).

### Can the tests fail?

| Check | Result |
|---|---|
| AC15: each field's bullet removed from the real prompt | The new label check fails for all six fields. The old check still passed for `file` and `line`, confirming the review finding |
| AC13: the same-hash sentence removed, then the lookalike sentence removed | Each check fails |
| AC4: `main` wrapped to exit 2 on `REVIEW_BOT_PROVIDER=nope` | `ac4_flag_beats_invalid_provider_variable` fails with `assert 2 == 1` |
| AC17, AC18 | `control` cases run in the suite, including the baseline's own exclusion spellings and a one-rule `per-file-ignores` entry |

**The 58-mutant result predates fix rounds 1 and 2.** Before those rounds, the behavioural tests ran against a temporary spec-only reference implementation: the clean version passed 173 of 173, and all 58 single-bug mutants were caught by a test of the intended criterion. That lab has been deleted and wasn't re-run, so it says nothing about the current code or the tests added since. Behavioural assertions have only been added since then, except for the `--provider` row of `ac4_flag_beats_variable`, which couldn't fail and is replaced by the invalid-variable test.

### Reduced in fix round 1

`test_p0_s1_repo_clean.py` went from 467 to 200 lines, as the review asked. It now uses `pyyaml` instead of a hand-written reader. These checks are gone: `include` and `files` settings that leave `cli.py` out, an `exclude` passed as a hook argument, and `[tool.uv.sources]` entries for `openai`. In exchange, AC18 is stricter in one way: any `per-file-ignores` entry covering `cli.py` now fails, including one for a single rule.

### AC16 environment

`tests/conftest.py` applies to the whole run, including `tests/unit` and child processes. It removes every `PATH` directory holding `claude`, removes `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` and any `REVIEW_BOT_*` variable, and refuses non-loopback connections and lookups.

The guarantee was exercised for real: `claude` is on this machine's `PATH` (`~/.local/bin`), and `ac16_claude_is_not_on_path` passes. The network guard patches Python's `socket` in the test process only, so a child process relies on the `PATH` and variable removal alone.

### Coverage

The whole suite covers 100% of the slice's modules, statements and branches. The acceptance tests alone reach 97%; the unit tests cover the rest:

| Module | Coverage | Missing | Note |
|---|---|---|---|
| `__main__.py` | 0% | 3-5 | Only run through AC1's subprocess, which isn't measured |
| `cli.py` | 98% | Branch 99->101 | |
| `errors.py` | 90% | 63-64 | |
| `findings.py` | 96% | 56 | |
| `providers/mock.py` | 90% | 51 | |

Everything else is at 100%.

### Open points

None of these blocks the slice.

1. **AC5, no name for the missing path.** "Names the offending flag or variable" doesn't apply to a positional. The test accepts `diff` or `path`, case-insensitively.
2. **AC19 beyond the three named characters.** The AC lists `\x1b[31m`, a newline and U+2028, then requires "no raw control characters". `ac19_other_control_characters_are_not_rendered_raw` reads that generally: CR, tab, VT, FF, NEL, U+2029, C1 CSI, NUL and DEL. Bidirectional overrides are tested in `tests/unit/test_render.py` (U+202E).
3. **AC18 covers configuration only.** Inline `# ruff: noqa`, `# type: ignore` and `# pragma: no cover` in `cli.py` are for the reviewer.

### Gaps

1. **AC14, collision resistance.** The delimiter comes from a hash of the diff, with no collision loop ([spec notes](slices/p0-s1-offline-review.md#notes)), and the reviewer ruled that acceptable. A test can't produce a real hash collision, so the guarantee rests on the hash. The tests do catch fixed delimiters, whole-line-only checks and reused delimiters.
2. **AC13 and AC15, meaning of the prompt.** Tests check for required words and shapes, such as `untrusted`, `same hash`, `fence` and `{"findings": [`. They can't show that a sentence says the right thing, or that a real model obeys it. AC1's warning check is similar.
3. **AC20 runs in-process.** Findings can only be injected through `main`, so the test swaps in a real `io.TextIOWrapper` with a strict ASCII or cp1252 encoding rather than a real terminal. The form of the replacement character isn't specified, so only the surrounding text is checked. Non-ASCII on stderr isn't covered.
4. **AC8, permission denied is simulated**, as the AC specifies. The test patches every standard way of opening the one file, so an implementation using some other route would need the test extending.
5. **Very large files have no acceptance test on purpose.** A real sparse-file test is deliberately replaced by the fake in `tests/unit/test_diff_input.py`: `truncate(2**40)` isn't sparse on this Windows disk, and an earlier experiment wrote about 30 GB. AC10's real files stop at 400,001 bytes.
6. **Eval cases aren't in place yet.** `tests/fixtures/p0_s1/prompt_injection.diff` is only used to check the framing and the lookalike delimiters in AC13.

### Observation outside the criteria

`main` raises `TypeError` when `sys.stderr` or `sys.stdout` has `encoding=None`, as `io.StringIO` does: `text.encode(stream.encoding, "backslashreplace")`, seen through `contextlib.redirect_stderr(io.StringIO())`. A real process stream has an encoding, so the command isn't affected, and no criterion covers this. It only matters if `main` is ever called with redirected streams.

## P0-S2: Get a real review from Claude with an explicit flag

Spec: [p0-s2-claude-review.md](slices/p0-s2-claude-review.md).

**Status: every criterion passes on the developer's final code, after review round 1.** AC16 is manual, and passed on the final code. AC17's subprocess cases have only run on Windows so far (gap 1).

The tests were written from the spec and the design's public interfaces, and `src/` wasn't read while writing them. For the round-1 regression test, the only part of `src/` read was the logging lines in `cli.py`. The SDK was read for its classes, for where `query` hands over to the transport, and for how it starts its CLI.

### Gate

| Check | Result |
|---|---|
| `ruff format --check .` | Pass |
| `ruff check .` | Pass |
| `mypy` (strict, `src` and `.claude/hooks`) | Pass |
| `pytest --cov`, whole suite | 428 passed, 100% coverage |
| `pytest tests/integration`, acceptance only | 311 passed, 97% coverage |
| P0-S2 acceptance tests, run three more times | 99 passed each time |
| `pre-commit run --all-files` | Clean on the final code, run by the main session |
| `pip-audit` | Clean on the final code, run by the main session |

### Criteria

Test names are shown without the `test_p0_s2_` prefix. Files are in `tests/integration/`. The count is the number of collected cases, including parametrised ones.

| AC | File `test_p0_s2_*` | Tests | Cases | Result |
|---|---|---|---|---|
| AC1 | `lockdown` | `ac1_claude_is_locked_down_for_any_review` (success and failure) | 2 | Pass |
| AC2 | `lockdown` | `ac2_options_carry_exactly_the_requests_prompts_and_model`, `ac2_model_flag_and_diff_reach_the_call_through_main` | 2 | Pass |
| AC3 | `lockdown` | `ac3_default_env_is_exact`, `ac3_flags_set_the_env_values`, `ac3_env_stays_exact_with_a_key_in_the_environment` | 3 | Pass |
| AC4 | `lockdown` | `ac4_variable_alone_sets_the_value`, `ac4_flag_beats_variable`, `ac4_neither_gives_the_default`, `ac4_flag_wins_over_an_invalid_variable`, `ac4_variables_reach_the_options` | 13 | Pass |
| AC5 | `lockdown` | `ac5_bad_flag_returns_2_and_names_it`, `ac5_bad_variable_returns_2_and_names_it` | 17 | Pass; see designer point 1 |
| AC6 | `lockdown` | `ac6_a_failing_call_is_made_exactly_once` (with `--max-retries 5`) | 3 | Pass |
| AC7 | `lockdown` | `ac7_never_ending_stream_exits_14_within_5s` (silent, and never stops sending), `ac7_deadline_variable_bounds_the_stream`, `ac7_stream_slower_than_a_short_deadline_would_allow_still_succeeds` | 4 | Pass, for the fake only; see the rulings |
| AC8 | `lockdown` | `ac8_stderr_callback_is_set_for_any_review`, `ac8_cli_noise_reaches_neither_stream` (success and failure, through `capfd`) | 3 | Pass |
| AC9 | `review` | `ac9_build_provider_returns_claude_and_starts_no_subprocess`, `ac9_provider_variable_also_selects_claude` | 2 | Pass |
| AC10 | `review` | `ac10_valid_finding_is_printed_and_exits_1` (golden file), `ac10_no_findings_exits_0`, `ac10_result_is_found_after_other_messages`, `ac10_threshold_applies_to_claude_findings`, `ac10_real_review_has_no_mock_warning` | 5 | Pass |
| AC11 | `review` | `ac11_usage_becomes_token_usage`, `ac11_summary_shows_provider_and_tokens` | 5 | Pass |
| AC12 | `review` | `ac12_no_api_key_still_calls_the_sdk` | 1 | Pass |
| AC13 | `failures` | `ac13_raised_exception_exits_14_without_the_text`, `ac13_exception_after_other_messages_exits_14`, `ac13_error_result_exits_14_without_the_text` (including `subtype="success"`), `ac13_error_result_with_empty_findings_is_not_a_clean_review`, `ac13_error_result_with_no_result_text_exits_14`, `ac13_stream_with_no_result_message_exits_14`, `ac13_sdk_log_line_stays_off_stderr_in_a_real_process`, `ac13_real_query_failure_leaves_only_our_one_line` (malformed JSON, non-zero exit) | 17 | Pass |
| AC14 | `failures` | `ac14_result_that_is_not_valid_findings_exits_14` | 7 | Pass; see designer point 2 |
| AC15 | `offline`, plus the guard in `tests/conftest.py` | `ac15_control_direct_call_fires_the_guard`, `ac15_control_default_provider_route_fires_the_guard`, `ac15_control_guard_fails_a_test_whose_assertions_hold`, `ac15_default_sdk_call_passes_the_lockdown_to_the_real_query`, and the whole suite | 4 | Pass; see gap 2 |
| AC16 | | Manual and never in CI. Run by the main session on the user's login while the user was away, not by the user as the criterion says: `python -m review_bot Assignment/sample_diff.txt --provider claude` exited 1 with 6 valid findings and stderr `provider=claude model=claude-opus-5-5 prompt=p0-v1 input_tokens=1765 output_tokens=1489`. Re-run on the final code: exited 1 with 5 valid findings and `input_tokens=1809 output_tokens=1451`. The finding count varies between runs | | Pass, manual |
| AC17 | `failures` | `ac17_misbehaving_provider_exits_14`, `ac17_exception_from_a_step_of_main_exits_14`, `ac17_closed_stream_exits_14_in_process`, `ac17_control_the_child_really_starts_with_stdout_closed`, `ac17_stdout_closed_exits_14`, `ac17_stdout_reader_gone_exits_14` | 11 | Pass on Windows; see gap 1 and designer point 3 |

### Failures

None.

### How the harder tests work

- **The guard (AC15)** patches below `query`, at `InternalClient.process_query` and `SubprocessCLITransport.connect`. A module that ran `from claude_agent_sdk import query` already holds the real function, so replacing the name wouldn't stop it. `main` turns the guard's exception into exit 14 (AC17), which a test could mistake for success, so the guard also records each call and an autouse fixture fails the test at teardown. Both patched names are private to the SDK; a renamed one fails loudly at the start of the run, because `setattr` raises.
- **The guard's control** runs a real child `pytest` under a copy of `tests/conftest.py`, with a test whose own assertion holds only because `main` swallowed the guard. The child must report `2 passed, 1 error` and exit 1.
- **Closed stdout (AC17)** uses a launcher that closes fd 1 and then starts the bot, so the bot's interpreter starts as it would under `>&-`. Calling `os.close(1)` also clears the Windows standard handle. A control proves the child sees `sys.stdout is None`. The launcher and its control were also run under Linux, through WSL.
- **AC7** runs `main` on a daemon thread, so an implementation that never returns fails the test instead of hanging the suite.
- **SDK log lines (AC13, review finding 1)** can only be seen in a child process, because pytest's own handlers keep `logging.lastResort` quiet. One test has an injected provider log `SENTINEL` at error level to `claude_agent_sdk._internal.query` and then fail. The other runs the real default `sdk_call`, `query` and transport against a small fake CLI, which is a real child process. It is started by replacing the transport's `_build_command`, not through `cli_path`, because the SDK refuses a batch file on Windows and a script needs one there. The child checks that the method exists before replacing it, and the test checks that the fake CLI really ran. No model or network is involved.

### Can the tests fail?

Before the temporary files were removed, a `sitecustomize.py` injected each bug from outside `src/`, and so reached child processes too. `src/` wasn't read or edited. The bugs wrap or replace public functions, so they simulate faults in the implementation rather than mutate its code. Each ran against the tests of its own criterion, and all 46 runs were caught. A run with no bug injected passed all 92 tests of those three files. These runs came before review round 1 and weren't repeated; the check for the round-1 tests follows the table.

| AC | Injected bugs (all caught) |
|---|---|
| AC1 | `tools=None`; `tools=["Read"]`; `setting_sources=None`; `max_turns=2`; an allowed tool; `strict_mcp_config=False`; an MCP server; a hook; an agent |
| AC2 | The Claude Code preset as system prompt; another model; another prompt |
| AC3 | An extra key in `env`; values ignoring the flags; the API key passed on |
| AC4 | Variables ignored; a variable beating its flag; wrong defaults |
| AC5 | Bad values dropped instead of rejected |
| AC6 | One retry; retries following `--max-retries` |
| AC7 | No deadline; a per-message timeout, which only `never_stops_sending` catches; a timeout before the stream is read |
| AC8 | `stderr=None`; noise printed to stderr or stdout; noise written straight to fd 2 |
| AC9 | `MockProvider` returned; a subprocess started while building |
| AC11 | Cache tokens not counted; one cache field not counted |
| AC12 | A required API key |
| AC13 | `is_error` ignored always, or only for `subtype="success"`; a stream with no result treated as empty; exception text printed; a traceback printed; 14 turned into 1 |
| AC14 | Bad JSON treated as no findings; JSON extracted from fences or prose; 14 turned into 1 |
| AC17 | Exception text printed; a traceback printed; 14 turned into 1; closed stdout tolerated (caught by the two subprocess cases, the reader-gone case and the in-process case) |

AC10 had no bug injected: it compares exact stdout and the exit code.

**Without the `NullHandler` in `cli.py`:** a temporary `sitecustomize.py` made `Logger.addHandler` ignore a `NullHandler`, with `src/` untouched, and all three new AC13 cases then failed: on the log record's `SENTINEL`, on the malformed line's `SENTINEL` through the SDK's `Fatal error in message reader`, and on that message's extra lines after a non-zero exit. On the real code, all three pass.

The guard was checked by editing `tests/conftest.py` for one run each, then restoring it. With the teardown check disabled, the child-`pytest` control fails. With the recording removed, all four AC15 tests fail. Removing the patches themselves was deliberately not tried, because the next layer down is a live call.

### Coverage

The whole suite covers 100% of statements and branches. The acceptance tests alone reach 97%; the unit tests cover the rest:

| Module | Coverage | Missing | Note |
|---|---|---|---|
| `__main__.py` | 0% | 3-5 | Only run through subprocesses, which aren't measured |
| `cli.py` | 98% | 163-165 | |
| `errors.py` | 90% | 63-64 | |
| `providers/claude_agent.py` | 96% | 113 | |
| `providers/mock.py` | 90% | 51 | |

Everything else is at 100%.

### Rulings recorded

- **AC7, "within 5 seconds":** the reviewer ruled that this holds for the fake only. [design.md section 4](design.md#4-open-questions) has the real behaviour.
- **`inf` and `nan` for `--deadline-s`:** rejecting them is an accepted addition, because they would defeat the only wall-clock bound ([spec notes](slices/p0-s2-claude-review.md#notes)).
- **Null token fields:** counting a null `usage` field as zero is an accepted addition. It is covered by `tests/unit/test_claude_agent_provider.py`, not by an acceptance test.

### For the designer

None of these blocks the slice. Each test beyond a criterion's literal text passes today, so no decision is forced yet.

1. **AC5 beyond its list.** `abc` on each setting, `--deadline-s nan` and `inf`, `REVIEW_BOT_API_TIMEOUT_MS=0` and `-1`, `REVIEW_BOT_DEADLINE_S=-1`, `REVIEW_BOT_MAX_RETRIES=abc` and `REVIEW_BOT_DEADLINE_S=nan` are tested as invalid. The AC names seven cases and the rest are inferred from the design's "invalid setting". `inf` and `nan` are settled in the rulings above.
2. **AC14 beyond "not JSON".** Valid JSON that fails the schema, `result=None`, fenced JSON, prose before JSON and `[]` are also required to exit 14, from the spec's "every failed call exits 14". The same tests also require a clean stderr with no `SENTINEL`, which AC14 doesn't state and AC17 implies. P0-S3 AC3 replaces this behaviour.
3. **AC17, "stdout closed".** The AC names `>&-`. `ac17_stdout_reader_gone_exits_14` also covers a reader that has already closed its end of the pipe, the case behind `| head`, because that is the likelier real failure. Its interest is in the exit code: a buffered write that only fails during interpreter shutdown would exit 120. The code passes, but the criterion doesn't state this.
4. **A stream that sends a successful result and then doesn't end, or sends two `ResultMessage`s.** P0-S2 doesn't say what happens, and P0-S4 AC6 settles the first. No test is written for either here.
5. **`usage` of `{}` or with no `output_tokens`.** AC11 doesn't name these. A one-off probe gave `TokenUsage(0, 0)` and `TokenUsage(2, 0)`, which look right, so no acceptance test pins them. Null fields are in the rulings above.

### Gaps

1. **AC17's bot runs have only run on Windows.** CI runs on `ubuntu-latest`. The launcher and its control were run on Linux through WSL, but the WSL Python has no dependencies, so the bot wasn't. The first Linux result for `ac17_stdout_closed_exits_14` and `ac17_stdout_reader_gone_exits_14` will come from CI.
2. **AC15, "whether or not a CLI is present".** Run both ways on this machine: normally, with `claude` installed (the `PATH` filter hides it, and the bundled CLI stays in the wheel), and once with the SDK's CLI lookup patched to find nothing. The first now gives 428 passed. The second gave 425 passed before round 1 and wasn't repeated; it is simulated, and that patch isn't part of the suite. The guard only applies in the test process. Child processes rely on the `PATH` and credential removal. Every child in the suite uses `--provider mock`, except the guard control, which runs the same guard, and the AC13 real-`query` cases, which replace the CLI command with the fake.
3. **Closing the stream is unit-tested against a single-layer fake only**, which can't show that the subprocess stops. The acceptance tests add `ac15_default_sdk_call_passes_the_lockdown_to_the_real_query`, which checks the prompt and four options at the guard, and the AC13 real-`query` cases, which cover failures only. [design.md section 4](design.md#4-open-questions) records what closing really does.
4. **AC7 uses the real clock.** The deadline of 0.1 s and the limit of 5 s are real time. The test that a slower stream succeeds pauses for 0.3 s against a deadline of 4 s, so a very slow machine could only fail it if `main` took over 5 s.
5. **AC16 can't be automated**, by design.

## P0-S3: Get a distinct exit code and a safe error message for every provider failure

Spec: [p0-s3-provider-failures.md](slices/p0-s3-provider-failures.md).

**Status: every criterion passes on Windows on the developer's final code, after fix round 2. Totals: whole suite 653 passed, 10 skipped, 100% coverage; P0-S3 alone 209 passed, 10 skipped. The round-2 additions have no bug-injection check (see "Can the tests fail?"). The 10 `/dev/full` cases skip here, so CI gives their first pytest run (gap 1).**

The tests were written from the spec and the design's public interfaces. `cli.py`, `settings.py` and parts of `findings.py`, `render.py`, `mock.py` and `claude_agent.py` were read only after the tests existed, to place the injected bugs and stand-ins below. No assertion changed as a result. The `--help` case was added after reading the spec's new Notes, written by the developer during this run; they changed no criterion.

### Gate

| Check | Result |
|---|---|
| `ruff format --check .` | Pass |
| `ruff check .` | Pass |
| `mypy` (strict, `src` and `.claude/hooks`) | Pass |
| `pytest --cov`, whole suite | 653 passed, 10 skipped, 100% coverage |
| P0-S3 tests alone | 209 passed, 10 skipped, 95% coverage |
| Acceptance only (`tests/integration`, `tests/conformance`) | 523 collected, 10 of them skipping; all others passed in the whole-suite run. Its coverage alone wasn't re-measured after round 1 (99% then) |
| `test_p0_s3_stream_failures.py` on the final tests | Green in both of the runs above |
| `pre-commit run --all-files`, `pip-audit` | Run by the main session, not by the tester |

### Criteria

Test names are shown without the `test_p0_s3_` prefix. Files are in `tests/integration/`, except AC8 and AC9. The count is collected cases; 10 of the 219 skip on Windows.

| AC | File | Tests | Cases | Result |
|---|---|---|---|---|
| AC1 | `test_p0_s3_exit_codes.py` | `ac1_the_table_names_every_category`, `ac1_category_returns_its_code_and_names_itself` | 8 | Pass |
| AC2 | `test_p0_s3_exit_codes.py` | `ac2_invalid_output_exits_13_with_empty_stdout`, `ac2_other_malformed_output_exits_13`, `ac2_empty_findings_exit_0`, `ac2_control_the_finding_the_cases_derive_from_is_valid` | 22 | Pass; see designer point 2 |
| AC3 | `test_p0_s3_exit_codes.py` | `ac3_successful_result_that_is_not_findings_returns_13` | 8 | Pass |
| AC4 | `test_p0_s3_stream_failures.py` (real processes) | `ac4_stdout_nothing_writes_to_does_not_change_the_code`, `ac4_provider_raising_makes_the_process_exit_14`, `ac4_provider_ending_the_process_exits_14_with_the_fixed_line`, `ac4_help_still_exits_0`, `ac4_unavailable_stderr_never_changes_the_exit_code`, `ac4_stdout_and_stderr_both_failing_exits_14`, and the `ac4_control_*` tests | 31, 8 skipped | Pass on Windows; see gap 1 |
| AC4, AC5 | `test_p0_s3_stream_failures.py` (real processes) | `ac4_ac5_stdout_failure_exits_14_and_leaves_only_the_error_line` | 7, 2 skipped | Pass on Windows; see gap 1 |
| AC4 | `test_p0_s3_safe_stderr.py` (`main` doesn't raise) | `ac4_unavailable_stderr_review_returns_its_own_code`, `ac4_unavailable_stderr_missing_diff_returns_3`, `ac4_unavailable_stderr_failed_review_keeps_its_code`, `ac4_stdout_failing_with_stderr_unavailable_returns_14`, `ac4_system_exit_from_a_provider_returns_14_in_process`, `ac4_control_the_failing_stream_fails_where_it_says` | 37 | Pass |
| AC5 | `test_p0_s3_stream_failures.py` (real processes) | `ac5_success_lines_come_after_stdout_is_flushed` | 2 | Pass |
| AC5 | `test_p0_s3_safe_stderr.py` | `ac5_input_failure_is_one_error_line`, `ac5_provider_failure_is_one_error_line`, `ac5_internal_error_is_one_error_line`, `ac5_usage_error_starts_with_the_error_line`, `ac5_missing_argument_starts_with_the_error_line`, `ac5_invalid_variable_starts_with_the_error_line`, `ac5_stdout_failure_leaves_only_the_error_line_in_process` | 29 | Pass |
| AC6 | `test_p0_s3_safe_stderr.py` | `ac6_hostile_provider_message_is_one_clean_line`, `ac6_other_controls_in_a_provider_message_are_not_raw`, `ac6_hostile_missing_path_is_one_clean_line`, `ac6_hostile_flag_gives_one_clean_error_line`, `ac6_hostile_variable_gives_one_clean_error_line`, `ac6_bad_usage_counts_as_0_and_the_review_is_delivered` | 32 | Pass; see designer point 2 |
| AC7 | `test_p0_s3_safe_stderr.py` | `ac7_model_text_never_reaches_stderr`, `ac7_invalid_output_message_embeds_no_model_text` | 28 | Pass |
| AC8 | `tests/conformance/test_p0_s3_provider_conformance.py` | `ac8_valid_returns_findings_and_non_negative_usage`, `ac8_empty_returns_no_findings`, `ac8_invalid_raises_invalid_output`, `ac8_each_category_raises_that_category`, `ac8_response_holds_only_our_types_or_built_ins`, `ac8_control_a_foreign_type_is_noticed` | 14 | Pass, for `MockProvider` |
| AC9 | `tests/conformance/test_p0_s3_provider_conformance.py`, plus the guard in `tests/conftest.py` | `ac9_conformance_runs_without_claude_or_a_key`, and the whole suite | 1 | Pass; see gap 2 |

`P0-S4 AC6`'s overridden-signal warning isn't tested here, as its own spec says.

### Failures

None.

### Tests changed in earlier slices

- **Retired:** `test_p0_s2_ac14_result_that_is_not_valid_findings_exits_14`, because P0-S3 AC3 replaces P0-S2 AC14. Its seven inputs now expect 13 in `ac3_successful_result_that_is_not_findings_returns_13`, which adds a whitespace-only result. The clean-stderr checks came across too. The file's docstring is updated.
- **Added to `tests/integration/conftest.py`:** the `failing_stream` fixture.
- **No other S1 or S2 test needed changing.** That includes the order of stdout and summary, and `str(path) in run.stderr` on Windows.
- **Moved:** `tests/integration/fake_sdk.py` to `tests/fake_sdk.py`, so `tests/conformance/conftest.py` can import it. `tests/integration` only joins `sys.path` once its own conftest loads, which is after `tests/conformance` in collection order; `tests/conftest.py` puts `tests/` there first. The existing `from fake_sdk import ...` lines needed no change. A temporary `import fake_sdk` in the conformance conftest failed collection with `ModuleNotFoundError` before the move. After it, the same import passed a whole-suite run (650 passed, 10 skipped) and `tests/conformance` alone (15 passed). The import was then removed, because `MockProvider` doesn't use it, so nothing keeps this proven until P0-S4 imports it for real.
- **For conformance in P0-S4:** add `ClaudeAgentProvider`'s factory to `PROVIDER_FACTORIES` in `tests/conformance/conftest.py`, importing the fake with `from fake_sdk import ...`. Nothing else in that directory names a provider.

### Rulings recorded

- **Usage never affects the verdict.** A missing, null or invalid count (non-integer, boolean or negative) counts as 0, and a `usage` that isn't a mapping counts as 0 for both. A valid finding with bad usage delivers: exit 1, the finding on stdout, one summary line. The test was flipped from its round-1 form, which required exit 14. It also requires the other, valid count to stay as sent, reading "a count" in the ruling per count, and adds a boolean case from the ruling's text.
- **Anything raised out of the review exits 14 with the fixed message.** That covers `SystemExit`, Ctrl+C during the review, a custom `BaseException` and a `BaseExceptionGroup`. `--help` keeps its exit 0.
- **A closed or failing stdout that nothing writes to doesn't change the code:** a missing diff stays 3, and a provider error keeps its own.

### Reductions in fix round 2

No assertion is dropped. The third item does narrow what runs in a real process:

- **Real-process `SystemExit` cases:** `SystemExit(1)` and `SystemExit(text)` now run in process only, and that test gained the exact fixed-line comparison the real-process cases had.
- **`fixed_line`:** folded into `ac4_provider_raising_makes_the_process_exit_14`, which now shares its one run.
- **`CLOSED` stderr cases:** six down to two, a delivered review and a missing diff. `no_findings_0` and `unexpected_14` still run with a real closed stderr's sibling, reader gone. `usage_2` and `provider_error_9` no longer run in a real process with stderr unavailable; only the in-process `None`, closed and failing streams cover them, and those can't show a shutdown exit code.

### Overlap with P0-S2 AC17

`ac4_ac5_stdout_failure_exits_14_and_leaves_only_the_error_line[closed]`, `[closed_with_findings]` and `[reader_gone]` run the same commands as `test_p0_s2_ac17_stdout_closed_exits_14` and `test_p0_s2_ac17_stdout_reader_gone_exits_14`, and assert more: exactly one error line. P0-S3 AC4 names these cases and the S2 tests were to stay, so this overlap is deliberate.

The review's duplicate is folded: `ac4_stdout_failure_makes_the_process_exit_14` and `ac5_stdout_failure_leaves_only_the_error_line` differed only by threshold and by assertion. The one test that replaces them asserts both on every case, so the `--help` and many-findings cases now also require one error line. Only `closed` still runs both thresholds, which removes the findings runs on `reader_gone` and `dev_full`; there, findings are exercised by the many-findings cases.

### Can the tests fail?

**Against the code before this slice.** The P0-S3 tests ran against a temporary copy of `src/` from `HEAD`: 111 failed, 84 passed, 9 skipped. That covers AC1, AC2, AC3, AC5, AC6, AC7 and AC8's `invalid`, including both `ac5_success_lines_come_after_stdout_is_flushed` cases, so the merged-pipe check does see the old order. Most AC4 stream cases passed, because P0-S2 already dropped a failed stream, so they needed the bugs below.

**Single bugs.** Each was applied to a temporary copy of the current `src/`, selected through `PYTHONPATH` so child processes got it too. All 13 were caught on Windows:

| Injected bug | Failing cases | Caught by |
|---|---|---|
| Stdout not dropped after a failure (exit 120) | 4 | AC4 stream, both failing, AC5 real process. In-process tests rightly can't see this |
| Stderr not dropped after a failure (exit 120) | 5 | AC4 `never_changes_the_exit_code`, both failing |
| Stdout not flushed before the success lines | 12 | AC4, AC5 in both forms, `success_lines_come_after_stdout_is_flushed` |
| A stderr failure not swallowed | 43 | All the AC4 stderr tests |
| Summary and warning written before stdout | 8 | AC5 real process and in process, `success_lines_come_after_stdout_is_flushed` |
| A stdout failure swallowed | 20 | AC4, AC5 |
| `--help` left to argparse | 1 | AC4 `reader_gone_help` |
| Error message not escaped | 19 | AC6 |
| Internal error shows the exception's text | 2 | AC4 real process, AC5 |
| Two exit codes swapped | 2 | AC1 |
| Pydantic's message embedded | 30 | AC7, AC3 |
| Code fence stripped before parsing | 2 | AC2, AC3 |
| `MockProvider` usage holding a foreign type | 2 | AC8 |

In the "not escaped" run, 5 of the 8 `hostile_flag` cases and the `hostile_variable` case still passed, because the code doesn't quote those values. They guard against a message that starts quoting them. The temporary copies are removed.

**Round 1 additions.** The table above predates the fold, so its AC4 and AC5 stream entries now mean the folded test. Each of these was also applied to a temporary copy of the final `src/`, against the tests named:

| Injected bug | Failing cases | Caught by |
|---|---|---|
| `SystemExit` and `KeyboardInterrupt` not wrapped | 7 of 7 | `ac4_provider_ending_the_process...`, `ac4_system_exit_from_a_provider...` |
| `KeyboardInterrupt` alone not wrapped | 1 of 4 | `ac4_provider_ending_the_process...[keyboard_interrupt]` |
| `main` also catching `--help`'s `SystemExit` | 1 of 1 | `ac4_help_still_exits_0` |
| Stdout probed first, so a failing one always exits 14 | 4 of 4 run | `ac4_stdout_nothing_writes_to...` (`dev_full` skipped) |
| Usage not checked | 3 of 4 | `ac6_forged_usage...`. Forged `input_tokens` then fails inside `sum` and still gives 14 with the fixed line, so that case rightly passes |
| Negative usage accepted | 2 of 4 | `ac6_forged_usage...[negative-*]` |
| Summary written before stdout | 4 of 5 run | The folded test; `--help` has no summary |
| Stdout not dropped after a failure | 2 of 5 run | The folded test, `reader_gone` and `reader_gone_help`. The two `closed` cases and `reader_gone_on_write` still exit 14 under this bug, so they don't detect it |
| **Not caught:** the invalid-usage message quoting the value | 0 of 4 | Not a defect in the test: that error reached `main`'s catch-all, which writes only the fixed line, so the value couldn't reach stderr |

The three usage rows tested the old behaviour, which exited 14. The usage ruling in round 2 replaced it, and the code those bugs were applied to has changed.

**Round 2 additions have no bug-injection check.** The run was stopped by the system for low memory (2.4 GB of 16 GB free afterwards) before any bug was applied, and it wasn't restarted. So these haven't been shown to fail against a bug: `ac6_bad_usage_counts_as_0_and_the_review_is_delivered`, and the `custom_base_exception` and `base_exception_group` cases. Nor has the reduced `CLOSED` stderr list, whose two cases have run only against the correct code. Planned bugs: a boolean counted as an int, a bad count failing the call, one bad count zeroing both, non-mapping usage not tolerated, only `SystemExit` and `KeyboardInterrupt` wrapped, and a stderr failure not swallowed. The reviewer then confirmed these by in-memory mutation in the final re-check; a Ctrl+C at a real console and detection on Linux remain unconfirmed.

**A vacuous test, caught while writing.** The in-process stderr cases first swapped `sys.stderr` in a fixture. pytest reinstates `capsys`'s streams when the call phase starts, so those cases passed without losing stderr at all. A failing in-process stdout test exposed it. The swap now happens in the test body, and each stderr case asserts that nothing reached the capture.

**Controls in the suite:**

- `ac4_control_each_broken_stream_really_is_broken`: a program taking no care sees each stream closed, or exits 120 after a failed write.
- `ac4_control_many_findings_outgrow_the_stream_buffers`: the "on write" cases really overflow the buffers.
- `ac4_control_the_failing_stream_fails_where_it_says`: the in-process stream fails on write, or only on flush.
- `ac8_control_a_foreign_type_is_noticed`: the type walk finds a foreign type in usage and in findings.
- `ac2_control_the_finding_the_cases_derive_from_is_valid`: each invalid case differs from a valid finding by its own fault.

### Linux check, outside pytest

WSL's Python 3.12 has no dependencies, so a temporary throwaway script drove the real `cli.py`, `mock.py` and the test file's own `run_process`, with `pydantic` and `claude_agent_sdk` stubbed. The stubs do no validation, so this says nothing about AC1, AC2, AC3 or AC7. All these gave the expected code, each with the exact error-line assertion where AC5 applies:

- The six stream controls, including `/dev/full`, for stdout and stderr.
- Stdout failing on closed, reader gone and `/dev/full`, including `--help` and the many-findings case.
- All six stderr outcomes on each of the three broken streams, and both streams broken together.
- The order of stdout and success lines through a merged pipe, by reading the output.

This ran before review round 1 and wasn't repeated, so the round-1 cases haven't run on Linux.

### Coverage

The whole suite covers 100% of statements and branches, including `cli.py`'s wrapping of `SystemExit`, through the in-process `ac4_system_exit_from_a_provider...` cases: real-process runs aren't measured. The P0-S3 tests alone reach 95%:

| Module | Coverage | Missing | Note |
|---|---|---|---|
| `__main__.py` | 0% | 3-5 | Only run through subprocesses, which aren't measured |
| `cli.py` | 95% | 78, 112, 117 | Line 78 is `--help`, run in a subprocess only. The rest belongs to S1 and S2 |
| `providers/claude_agent.py` | 80% | 60, 92->91, 94, 118-121 | Covered by S2's tests; AC3 only needs the result path |

Everything else is at 100%. The P0-S3 tests take about 60 s alone on this machine, nearly all of it in `test_p0_s3_stream_failures.py`, because each case starts real interpreters. The whole suite takes about 90 s, up from about 20 s before this slice.

### For the designer

None of these blocks the slice.

1. **Settled by the review rulings above**, all now tested. The design now also states `ProviderError.category`, which AC8's tests read.
2. **Beyond the criteria's lists.** These pass today but aren't literal text:
   - AC2: empty, whitespace, `null`, `[]`, `{"findings": null}`, an extra top-level key, prose before JSON, `NaN` confidence, nesting past the recursion limit, and an integer past the digit limit, from the design's "not bare JSON, or fails the schema".
   - AC6: CR, VT, FF, NEL, C1 CSI, NUL, DEL and U+2028 in a provider message, reading "no raw control characters" as P0-S1 AC19 was read; a hostile extra positional and a hostile `REVIEW_BOT_THRESHOLD`.
   - AC4: `--help` with stdout's reader gone, from the spec's Notes, and stdout and stderr both failing.
3. **The spec's note on exit 120 applies to stderr too.** `ac4_control_each_broken_stream_really_is_broken` shows that a failed stderr write also ends in 120, on Windows and Linux, so a failed stderr has to be dropped as well. The code does this; the note could say so.
4. **P0-S2 AC14** now reads `Removed`, per the design's handover rule. No action is left.

### Gaps

1. **`/dev/full` under pytest.** The 10 cases skip on Windows, and the real dependencies aren't in WSL, so their first full run is CI's. The Linux check above lowers the risk for 8 of them, but stubs the dependencies. The 2 `ac4_stdout_nothing_writes_to...` cases on `/dev/full` have not run anywhere yet.
2. **`ac9_conformance_runs_without_claude_or_a_key` can only fail if the guard in `tests/conftest.py` breaks.** AC9 is really a property of the run, and P0-S1's `ac16_*` tests, including a control that the filter removes a directory holding `claude`, already prove the guard. This test only shows that it reaches `tests/conformance/`. `claude` is on this machine's real `PATH`, so the guard is doing real work here.
3. **Ctrl+C.** A `KeyboardInterrupt` raised by a provider is tested, standing in for Ctrl+C during the review. A real SIGINT isn't sent, and an interrupt outside the review isn't covered, because the ruling only names the review.
4. **Eval cases** still aren't in place, as in P0-S1.

## P0-S4: Get clear, specific errors from Claude, with Claude as the default

Spec: [p0-s4-claude-errors.md](slices/p0-s4-claude-errors.md).

**Status: every criterion passes on Windows on the uncommitted working tree, after review round 1 of the tests. AC15 is manual, and was run by the main session. The whole suite gave 945 passed, 12 skipped, 100% coverage. The 12 skips are `/dev/full` cases, 2 of them new, pending their first pytest run in CI (gap 1).**

The first tests were written from the spec, the design's interfaces and the installed SDK. `src/` was read only after they existed, to place the injected bugs below. One assertion changed afterwards, and it got stricter: see "Can the tests fail?". The round-1 changes came from the reviewer's findings and the spec's updated AC5, AC7 and AC13, and `src/` had been read by then.

### Gate

| Check | Result |
|---|---|
| `ruff format --check .` | Pass |
| `ruff check .` | Pass |
| `mypy` (strict, `src` and `.claude/hooks`) | Pass |
| `pytest --cov`, whole suite | 945 passed, 12 skipped, 100% coverage, 146 s, on the final tests |
| P0-S4 tests with `tests/conformance` | 298 passed, 2 skipped, 96% coverage, 59 s, before round 1. Not re-measured as a group, to save memory: after round 1, `map` gave 212 passed, `msg` 50 and `tests/conformance` 29, and the `real` tests passed in the whole-suite run |
| `pre-commit run --all-files`, `pip-audit` | By the main session, not the tester |

The whole-suite run also confirms the handover from P0-S3: `tests/conformance/conftest.py` imports `fake_sdk`, and collection works.

### Criteria

Test names are shown without the `test_p0_s4_` prefix. Files are in `tests/integration/`: `map` is `test_p0_s4_error_mapping.py`, `msg` is `test_p0_s4_messages.py`, `real` is `test_p0_s4_real_sdk.py`, and `real` tests run the real `query` and transport against a fake CLI in a child process, with no provider flag. The count is collected cases.

| AC | File | Tests | Cases | Result |
|---|---|---|---|---|
| AC1 | `map`, `real` | `ac1_raised_sdk_error_maps_to_its_category`, `ac1_connection_error_keeps_the_sdks_guidance`, `ac1_kept_guidance_is_still_one_clean_line`, `ac1_subclass_maps_before_its_parent`, `ac1_cli_crash_exits_12_with_one_line`, `ac1_sdk_version_is_the_audited_one` | 15 | Pass; see designer point 1 |
| AC2 | `map` | `ac2_status_decides_the_category` (16 statuses, both routes), `ac2_no_status_falls_back_to_the_terminal_reason`, `ac2_a_status_beats_the_terminal_reason`, `ac2_status_that_is_not_an_integer_counts_as_no_status`, `ac2_error_result_is_never_parsed_as_findings` | 54 | Pass |
| AC3 | `map` | `ac3_refusal_stop_reason_is_a_refusal`, `ac3_refusal_with_a_status_is_still_a_refusal` (fakes only, as the spec says) | 12 | Pass |
| AC4 | `map` | `ac4_assistant_error_then_no_successful_result_maps` (7 values, 5 endings), `ac4_billing_error_message_names_billing`, `ac4_unrecognised_error_message_names_the_value`, `ac4_named_value_is_still_one_clean_line` | 38 | Pass |
| AC5 | `map`, `real` | `ac5_auth_signal_wins_over_result_and_exception`, `ac5_rate_limit_signal_wins_over_a_500_result_and_exception`, `ac5_result_status_wins_over_a_later_process_error`, `ac5_result_message_wins_over_any_later_exception`, `ac5_assistant_error_wins_when_the_others_disagree`, `ac5_earliest_signal_wins_in_arrival_order`, `ac5_result_401_then_the_clis_exit_exits_8_with_one_line`, `ac13_ac5_live_401_sequence_exits_8_with_one_line` | 14 | Pass |
| AC6 | `map`, `real` | `ac6_successful_result_overrides_the_signal_and_warns` (7 values, non-empty and empty), `ac6_no_signal_gives_no_warning`, `ac6_named_signal_is_still_a_clean_warning`, `ac6_nothing_after_a_successful_result_is_read`, `ac6_review_returns_within_5s_when_the_stream_never_ends`, `ac6_main_exits_by_findings_when_the_stream_never_ends`, `ac6_deadline_during_close_still_returns_the_findings`, `ac6_earlier_signal_wins_over_prose`, `ac6_prose_alone_returns_13`, `ac6_success_then_the_clis_exit_still_delivers`, `ac6_signal_then_success_warns_and_delivers`, `ac6_prose_alone_exits_13_with_one_line`, `ac6_success_from_a_cli_that_never_exits_is_delivered_before_the_deadline`; `msg`: `ac6_warning_may_precede_the_error_line_when_stdout_fails` | 43 | Pass |
| AC6 Notes | `map`, `msg`, `real` | `ac6_note_warning_with_stderr_lost_exits_by_findings_in_a_real_process` (closed, reader gone, `/dev/full`), `ac6_note_control_the_child_warns_when_stderr_works`, `ac6_note_warning_with_stderr_lost_still_delivers` (in process), `ac6_note_provider_does_not_import_cli`, `ac6_note_bad_usage_still_returns_the_findings`, `ac6_note_one_bad_count_leaves_the_other_alone`, `ac6_note_failed_result_then_a_successful_one_delivers_and_warns` | 37, 2 skipped | Pass on Windows; see gap 1 |
| AC7 | `map` | `ac7_stream_with_no_result_message_is_permanent`, `ac7_non_sdk_class_stays_uncategorised_and_exits_14` (including the bare control-request timeout), `ac7_non_sdk_class_after_other_messages_exits_14`, `ac7_non_sdk_class_after_an_error_signal_keeps_the_signal`, `ac7_timeout_error_raised_by_the_stream_exits_11` | 10 | Pass |
| AC8 | `map`, `real` | `ac8_deadline_with_no_signal_is_transient`, `ac8_deadline_with_no_signal_exits_11`, `ac8_deadline_after_an_auth_signal_reports_auth`, `ac8_deadline_after_an_auth_signal_exits_8`, `ac8_deadline_after_an_error_result_reports_that_result`, `ac8_deadline_variable_bounds_the_stream`, `ac8_stream_slower_than_a_short_deadline_would_allow_still_succeeds`, `ac8_cli_that_never_exits_hits_the_deadline_with_one_line` | 12 | Pass; the 5 seconds hold for the fake only |
| AC9 | `msg`, `real` | `ac9_cli_not_found_says_what_to_do`, `ac9_missing_cli_exits_7_with_one_line` | 2 | Pass |
| AC10 | `msg` | `ac10_auth_names_the_login_and_the_key` (assistant error, 401, 403, raised `ResultError`) | 4 | Pass; see designer point 2 |
| AC11 | `msg`, `real` | `ac11_key_in_a_process_error_reaches_neither_stream`, `ac11_control_the_fake_error_really_holds_the_key`, `ac11_key_written_by_the_cli_reaches_neither_stream` | 5 | Pass; see designer point 1 |
| AC12 | `msg`, `real` | `ac12_model_text_reaches_no_stream` (13 routes, including `str(ResultError)`, `ResultMessage.result`, `CLIJSONDecodeError` and `MessageParseError`), `ac12_message_does_not_use_the_text_of_either_class`, `ac12_other_sdk_text_reaches_no_stream` (`ClaudeSDKError`, `CLINotFoundError`), `ac12_connection_error_text_is_escaped`, `ac12_warning_on_a_successful_run_has_no_model_text`, `ac12_malformed_output_exits_12_without_its_text` | 21 | Pass |
| AC13 | `msg`, `real` | `ac13_parse_args_selects_claude`, `ac13_build_provider_returns_claude_and_starts_no_subprocess`, `ac13_a_run_with_no_flags_reaches_claude_not_canned_findings`, `ac13_summary_of_a_default_run_names_claude`, `ac13_missing_diff_with_no_flags_stays_3`, `ac13_empty_diff_with_no_flags_stays_5`, `ac13_provider_variable_still_beats_the_default`, `ac13_provider_flag_still_beats_the_default`, `ac13_blank_variable_exits_2_and_names_it`, `ac13_blank_flag_exits_2_and_names_it` (each empty and whitespace), and every `real` failure test, which select no provider | 12, plus the `real` cases | Pass |
| AC14 | `tests/conformance/` | The unchanged `test_p0_s3_ac8_*` suite with `[claude]` (12 cases); `ac14_mock_and_claude_are_both_in_the_suite`, `ac14_claude_factory_builds_the_claude_provider` | 14 | Pass |
| AC15 | | Manual and never in CI. Run by the main session, not by the user as the criterion says, as the user was away: `ANTHROPIC_API_KEY=invalid python -m review_bot Assignment/sample_diff.txt` exited 8 with one line: `review-bot: error: auth: Claude rejected the credentials. Check the Claude Code login or ANTHROPIC_API_KEY, then try again.` The tester didn't run it and reports the main session's result | | Pass, manual |
| AC16 | `map` | `ac16_sdk_call_is_made_exactly_once` (429 and 503, with and without a following `ResultError`, `--max-retries 5`) | 4 | Pass |

### Failures

None on the final code.

**One failure during the run, fixed by the developer.** `ac1_connection_error_keeps_the_sdks_guidance` failed while the provider replaced `CLIConnectionError`'s text with fixed guidance, and a spec note added during this run said that was intended. That note contradicted AC1's "keeping the SDK's guidance", so the test stayed red. The developer then changed the code and the note, and the test passed without being edited. Designer point 1 covers what remains.

### Tests changed in earlier slices

**Retired, per the spec:**

| Test | Why | Replaced by |
|---|---|---|
| `test_p0_s1_ac6_no_provider_selected_exits_2_and_names_the_flag` | P0-S4 AC13 | `ac13_parse_args_selects_claude`, `ac13_a_run_with_no_flags_reaches_claude_not_canned_findings` |
| `test_p0_s1_ac6_no_provider_selected_never_falls_back_to_canned_findings` | P0-S4 AC13 | The same; both assert no canned output |
| `test_p0_s1_ac6_empty_provider_variable_is_not_a_selection` | P0-S4 AC13 | `ac13_blank_variable_exits_2_and_names_it` |
| `test_p0_s2_ac7_never_ending_stream_exits_14_within_5s` (2 cases) | P0-S4 AC8 | `ac8_deadline_with_no_signal_exits_11`, same two streams |
| `test_p0_s2_ac7_deadline_variable_bounds_the_stream` | P0-S4 AC8 | `ac8_deadline_variable_bounds_the_stream`, expecting 11 |
| `test_p0_s2_ac7_stream_slower_than_a_short_deadline_would_allow_still_succeeds` | P0-S4 AC8 | `ac8_stream_slower_than_a_short_deadline_would_allow_still_succeeds`, unchanged |
| `test_p0_s2_ac13_raised_exception_exits_14_without_the_text[process_error]` | Now 12 | `ac11_key_in_a_process_error_reaches_neither_stream`, `ac12_model_text_reaches_no_stream[process_error]` |
| `test_p0_s2_ac13_raised_exception_exits_14_without_the_text[connection_error]` | Now 11, and AC1 keeps the text | `ac1_raised_sdk_error_maps_to_its_category[connection_error]` |
| `test_p0_s2_ac13_real_query_failure_leaves_only_our_one_line` (2 cases) | Now 12 | `ac1_cli_crash_exits_12_with_one_line`, `ac12_malformed_output_exits_12_without_its_text`, each early and late |
| `test_p0_s2_ac13_error_result_exits_14_without_the_text` (4 cases) | Now 12 | `ac2_error_result_is_never_parsed_as_findings` |
| `test_p0_s2_ac13_error_result_with_empty_findings_is_not_a_clean_review` | Now 12 | The same, `success_empty_findings` |
| `test_p0_s2_ac13_error_result_with_no_result_text_exits_14` | Now 12 | The same, `success_no_result_text` |
| `test_p0_s2_ac13_stream_with_no_result_message_exits_14` (2 cases) | Now 12 | `ac7_stream_with_no_result_message_is_permanent` |

**The "neither" case of P0-S1 AC4 had no test to retire:** `SETTINGS_WITH_A_DEFAULT` already left out `--provider`. Only its comment changed.

**Changed, not retired:** `test_p0_s2_ac6_a_failing_call_is_made_exactly_once` still asserts one call, the criterion it names. Its exit codes now follow P0-S4 AC1: 14 unexpected, 12 `ProcessError`, 11 `CLIConnectionError`. This wasn't on the retire list by name. It's the AC13 exit-14 expectation carried in another test.

**Kept in `test_p0_s2_failures.py`, still exit 14:** `ac13_raised_exception...` (`exception`, `value_error`, `group`), `ac13_sdk_log_line_stays_off_stderr_in_a_real_process`, `ac13_exception_after_other_messages_exits_14`, and all of AC17. Docstrings in the three files say what moved. The real-`query` child and fake CLI moved to `test_p0_s4_real_sdk.py`, and the fake CLI now answers `initialize`, so messages reach the stream.

**Added:** `result_message` takes any other `ResultMessage` field, and `error_result`, `raised_result_error` and `assistant_error` are new, all in `tests/fake_sdk.py`. Existing callers are unaffected. `tests/integration/p0_s4_support.py` holds the shared helpers, and `main_within_5s` moved there from the lockdown file.

**Moved in round 1:** `run_process`, its launcher and the stream constants went from `test_p0_s3_stream_failures.py` to the new `tests/integration/stream_support.py`, so no test module imports from another. The P0-S3 assertions are unchanged. `tests/conformance/conftest.py` gained a `provider_factories` fixture, which replaces re-executing that file to read its dict.

**S1 to S3 tests with no `--provider`, considered one by one. None needed changing:**

| Test | Why it's unaffected |
|---|---|
| `test_p0_s1_ac4_variable_alone_sets_the_value[--provider]` | Sets the variable |
| `test_p0_s1_ac4_provider_variable_selects_the_provider_for_a_real_run` | Sets the variable to `mock` |
| `test_p0_s1_ac5_bad_provider_variable_returns_2_and_names_it` | Sets it to `nope`, exits 2 before any provider |
| The `test_p0_s2_ac4_*` tests calling `parse_args("change.diff")` | Read other fields only |
| `test_p0_s2_ac9_provider_variable_also_selects_claude` | Sets the variable |
| `test_p0_s3_ac5_missing_argument_starts_with_the_error_line` (`bot.run()`) | No path, exits 2 |

All child processes in S1 to S3 pass `--provider mock` or inject a provider, so none could reach a real CLI by the new default. The `real` tests here select no provider on purpose, and replace the CLI command first.

### Real SDK path

The `real` tests cover what fakes can't, and all ask for exactly one stderr line where a failure occurs:

- **The 401 sequence:** assistant error, result with status 401, exit 1, so the SDK itself raises `ResultError`. Exits 8.
- **A CLI crash:** before `initialize` is answered and after the prompt. Exits 12, without the SDK's `Fatal error in message reader` log line.
- **Malformed JSON at both points, a missing CLI, a success followed by a crash or by a CLI that never exits, a signal followed by success, prose, and a CLI that never answers under `--deadline-s 1`.** The last exits 11.

Real timing on this machine: a success from a CLI that never exits took about 7 s with `--deadline-s 30`, and `--deadline-s 1` took about 8 s, each including interpreter start-up. This fits [design.md section 4](design.md#4-open-questions). The test asserts under 30 s, not 5 s, because AC6's 5 seconds hold for the fake only.

### Can the tests fail?

Each bug was applied in memory at import, with `src/` untouched, against `map`, `msg` and `tests/conformance` (279 cases, about 8 s a run). 35 of 36 were caught by a test of the intended criterion, one of them only after the tightening below. Temporary files were in `tests/` and removed by name.

| Injected bug | Failing cases | Caught by AC |
|---|---|---|
| Reading continues after a successful result | 41 | 6, 8, 13, and S3 AC8 |
| Stream not closed on return | 4 | 6 |
| No deadline | 10 | 8 |
| Latest signal wins, not earliest | 18 | 4, 5, 6, 12 |
| Exception beats signal | 12 | 4, 5, 16 |
| Deadline beats signal | 5 | 8 |
| No warning | 16 | 6 |
| Warning written as an error line | 16 | 6 |
| Only 500, 503, 529 count as 5xx | 4 | 2 |
| 408 not transient | 2 | 2 |
| 403 not auth | 3 | 2, 10 |
| A boolean status counted as a status | 1 | 2, after tightening; see below |
| `terminal_reason="api_error"` ignored | 4 | 2 |
| Terminal reason beats status | 10 | 2, 5, and S3 AC8 |
| Refusal only when `is_error` is false | 4 | 3 |
| Refusal ignored | 11 | 3, 5, 12, and S3 AC8 |
| `is_error` ignored | 51 | 2, 5, 6, 8, 10, 12, 16, and S3 AC8 |
| Billing message replaced by the generic one | 1 | 4 |
| Unrecognised value not named | 1 | 4 |
| `CLINotFoundError` treated as a connection error | 5 | 1, 9, and S3 AC8 |
| `ResultError` treated as a plain `ProcessError` | 15 | 1, 2, 10, 12 |
| Connection guidance dropped | 1 | 1 |
| SDK text in the permanent message | 8 | 11, 12 |
| `ResultMessage.result` in the message | 8 | 2, 10, 12 |
| Unexpected exception categorised | 6 | 7, 13 |
| Stream with no result treated as no findings | 3 | 7 |
| `sdk_call` called twice | 4 | 16 |
| A bad usage count raising | 19 | 6 notes, and S3 AC8 |
| Non-mapping usage raising | 48 | 6 notes, 8, 12, 13 |
| The SDK's usage dict in the response | 59 | 6, 8, 12, 13, and S3 AC8 (AC14's "no SDK type") |
| `PATH`, the key, or the login missing from a message (3 bugs) | 1, 4, 4 | 9, 10, 10 |
| A stderr failure raising | 8 | 6 notes |
| Default provider `mock` | 4 | 13 |
| **Not caught:** `billing_error` falling through to the unrecognised branch | 0 | Its message still names `billing_error` and it is still `permanent`, so it meets the criterion. The row above covers a message that really loses the word |

**One test tightened as a result.** The first version of `ac2_status_that_is_not_an_integer_counts_as_no_status` couldn't see a boolean counted as a status, because both outcomes gave `permanent`. It now sends `terminal_reason="api_error"`, where the two differ.

**In child processes,** three more were applied through a temporary `sitecustomize.py`:

| Injected bug | Failing cases | Caught by |
|---|---|---|
| The `NullHandler` on the SDK's logger ignored | 4 of 9 | `ac1_cli_crash...`, `ac12_malformed_output...`, which see the SDK's log line |
| A failed stderr not dropped (exit 120) | 2 of 9 | `ac6_note_warning_with_stderr_lost..._in_a_real_process`, `reader_gone` only (`found` and `none`). The two `closed` cases still exit by findings under this bug, so they don't detect it |
| Default provider `mock` | 5 of 9 | `ac13_ac5_live_401...`, `ac1_cli_crash...`, `ac12_malformed_output...` |

The `real` run used a subset of its tests. The full `real` file passes on the correct code, but the success, deadline and prose cases had no bug applied.

**Round 1 additions.** Each bug was applied in memory the same way, against only the new test it targets. All were caught. These runs didn't cover `tests/conformance`, so the `provider_factories` tests had no bug applied, and neither did the `run_process` move:

| Injected bug | Failing cases | Caught by |
|---|---|---|
| A blank variable treated as unset | 2 of 2 | `ac13_blank_variable_exits_2_and_names_it` |
| A blank flag treated as absent | 2 of 2 | `ac13_blank_flag_exits_2_and_names_it` |
| `claude_agent_sdk.__version__` moved to `0.2.179` | 1 of 1 | `ac1_sdk_version_is_the_audited_one` |
| Latest signal wins, not earliest | 1 of 1 | `ac5_earliest_signal_wins_in_arrival_order` |
| A `TimeoutError` not treated as the deadline | 1 of 1 | `ac7_timeout_error_raised_by_the_stream_exits_11` |
| A raised exception beats a delivered result | 2 of 2 | `ac6_deadline_during_close_still_returns_the_findings` |
| Closing the stream outside the deadline | 2 of 2 | The same, by its 5-second bound |
| A status beats a refusal | 4 of 4 | `ac3_refusal_with_a_status_is_still_a_refusal` |
| `CLINotFoundError`'s text shown | 1 of 2 | `ac12_other_sdk_text_reaches_no_stream[not_found]`; the other case is the other class |
| `ClaudeSDKError`'s text shown | 1 of 2 | `ac12_other_sdk_text_reaches_no_stream[other_sdk_error]`; the same |
| Stderr text not escaped | 1 of 1 | `ac12_connection_error_text_is_escaped` |

### Coverage

The whole suite covers 100% of statements and branches. The P0-S4 tests with `tests/conformance` alone reached 96% before round 1, which only added tests:

| Module | Coverage | Missing | Note |
|---|---|---|---|
| `__main__.py` | 0% | 3-5 | Only run through subprocesses, which aren't measured |
| `cli.py` | 91% | 71, 79, 110, 148-152, 162 | Mostly S1 and S3 |
| `diff_input.py` | 75% | 34-35, 40, 47-48 | S1 |
| `providers/claude_agent.py` | 99% | 265 | `_call_sdk`'s yield, run only by child processes |

`console.py`, `settings.py` and the rest are at 100%. The whole suite now takes about 145 s, up from about 90 s, because the `real` tests start interpreters and two wait on the SDK's close.

### For the designer

None of these blocks the slice unless the designer says so. Review round 1 settled the earlier points on AC5's order, the `TimeoutError` carve-out in AC7, an empty provider variable and a refusal with a status: each is now pinned by a test.

1. **A key in a `CLIConnectionError`.** That text reaches stderr, escaped, because AC1 keeps it. AC11 names `ProcessError` only, so a key there isn't tested and would be shown. The spec's note says every raise site in SDK 0.2.178 holds only the SDK's guidance plus OS errors or paths. `ac1_sdk_version_is_the_audited_one` now fails on any SDK move and tells the upgrader to re-audit, but it pins the version only and doesn't check the text. Two assistant errors in a row are also unpinned: the code keeps the first.
2. **AC10, "given `auth`".** Tested through `ClaudeAgentProvider`. `MockProvider(error=ProviderError(AUTH, "detail"))` prints `detail`, as P0-S3 AC1 requires, so the message belongs to the provider, not `main`. "Claude Code login" is checked as `Claude Code` plus `login` or `log in`.
3. **Beyond the criteria's text**, all passing:
   - AC2: a string or boolean status counts as no status.
   - AC2: 402, 418, 499, 502, 599 and 600, from "any other" and "any 5xx".
   - AC4 and AC6: a hostile signal value still gives clean output.
   - AC8: a failed result followed by a hang reports that result, as the note says.
   - AC6: a failed result followed by a success delivers and warns, as the note says.

### Gaps

1. **`/dev/full`:** the 2 new real-process cases skip on Windows, pending their first run in CI, like the 10 from P0-S3. A closed stderr doesn't detect an undropped stream, as the table above shows, so `reader_gone` is the only case detecting it here.
2. **The real path uses a fake CLI.** Its messages follow the SDK's parser and were written from reading it, not from a capture. AC15's manual run saw the real exit 8 and one line. The tests replace two private transport methods, `_build_command` and `_find_cli`; a renamed `_build_command` fails loudly.
3. **`ac13_a_run_with_no_flags_reaches_claude_not_canned_findings` expects 14,** because the guard's own exception reaches `main`. Its real assertion is the guard's call count.
4. **AC15 can't be automated**, by design.
5. **Real grandchild processes** on close remain an open question in [design.md section 4](design.md#4-open-questions). The fake CLI has none.
6. **Eval cases** still aren't in place, and prompt-injection evals are Phase 1.
