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
