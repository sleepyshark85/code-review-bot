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
