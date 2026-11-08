# Phase 3 test report

## P3-S1: Review a local diff with read-only repository tools, offline

Spec: [p3-s1-local-review-with-repo-tools.md](slices/p3-s1-local-review-with-repo-tools.md). Tests: [`tests/integration/test_p3_s1_local_review_with_repo_tools.py`](../../../tests/integration/test_p3_s1_local_review_with_repo_tools.py), 124 cases. Golden files: [`tests/fixtures/p3_s1/`](../../../tests/fixtures/p3_s1/). They were written from the spec's criteria, Limits and Interfaces, and design.md ("The tools", "Tool safety", "Spend and caps", "Interfaces", report and audit keys), while the developer built the slice and before any of its code was read.

**Status: every criterion passes. All 124 cases pass, from this file alone, on the developer's uncommitted working tree** (base `4b506d4`).

**Before the code:** 3 passed and 121 failed, nearly all on `TypeError: MockProvider.__init__() got an unexpected keyword argument 'tool_calls'`. The 3 that passed are AC11 cases that already held (Phase 1's prompts, no `tool_calls=` in the summary, no `context` audit records). A run against the developer's half-built tree showed that argparse's `unrecognized arguments: --context-repo` passed a naive "exits 2 naming the flag" check, so AC12 and the range checks also require that the message isn't argparse's rejection. **First run on the developer's code:** all 124 passed, with no change to the tests.

### Gate

Only this file and the earlier suites it changed were run, as the coordinator asked (the full suite runs out of memory locally): `test_p3_s1_local_review_with_repo_tools.py`, `test_p1_s10_json_output.py`, `test_p1_s11_markdown_output.py`, `test_p1_s12_triage.py` and `test_p1_s13_triage_edit.py` give 585 passed and 6 skipped (no `/dev/full`, no POSIX pseudo-terminal on Windows). `ruff format --check` and `ruff check` are clean on the changed test files. For the full gate, see the main session's run.

### Seam and set-up (assumed)

These are the seams the tests rely on, written before the code. The developer has since confirmed them, including the spans' `{path, first_line, last_line}` form; each `tool_call` record also holds `spans_total`, which the tests allow but don't check.

- **Every run** calls `review_bot.cli.main([DIFF, "--provider", "mock", "--audit-log", LOG, "--context-repo", DIR, *FLAGS], provider=MockProvider(tool_calls=[...], findings=[FINDING]))` in process. `DIFF` is `Assignment/sample_diff.txt`. `FINDING` is `billing/invoice.py:14`, high logic, which anchors on the sample's added line, so a run that goes to the end exits 1 and prints it.
- **Flags** (spec): `--context-repo DIR`, `--context-rev REV`, `--max-tool-calls N`, `--max-tool-total-bytes N`. `--max-tool-result-bytes` isn't used: AC6 runs against its default, 16,000.
- **The mock** (spec Interfaces): `MockProvider(tool_calls=[(name, arguments), ...])` makes each call in order through `request.tools`, keeps each result in `provider.tool_results` as a `str` (the whole result, delimiters included), then answers as scripted with `findings=`. Without `tool_calls=`, the mock is built exactly as in Phase 1.
- **The request** (design.md Interfaces): `provider.requests[0].tools` is `None` in diff mode; in context mode it has `definitions`, each with `name`, `description` and `input_schema` (whose `type` is `object`).
- **The wrapping** (AC8, prompt.py's scheme for the diff): the result's first and last lines each carry exactly one 64-hex SHA-256, the same in both, equal to the SHA-256 of the UTF-8 text between them (the inner lines joined by `\n`). A single trailing `\n` after the closing line is tolerated. The delimiter label isn't assumed: the forgery test learns the closing line's form from a real result.
- **Result shapes:** `read` lines are `<line>\t<text>`, compared only among lines of that shape, so a header or note may sit beside them. `grep` lines are `path:line:text`, compared the same way. `glob`'s body, untruncated, is exactly the paths, one per line. A refusal's body is exactly one line, `refused: <reason>: <sentence>`.
- **Outcomes** come from the audit log: `stage` `context`, `event` `tool_call`, whose `data` holds at least `tool`, `outcome`, `reason`, `spans`, `result_bytes` and `input_sha256`. Each span is an object `{"path", "first_line", "last_line"}` (design.md's `LineSpan` fields); `reason` is `null` when the call succeeds.
- **The report** (`--format json`): `mode`, `prompt_version` and `context`, whose keys are exactly `commit`, `tool_calls`, `refused`, `result_bytes` and `files_read`. `context.result_bytes` is checked to equal the sum of the audit records' `result_bytes`. `files_read`'s shape isn't assumed: the path must appear among its strings.
- **The summary line** is stderr's last line, and ends ` tool_calls=3` ("appends").
- **"git isn't run"** (AC7) is observed by wrapping `subprocess.Popen.__init__`, which every route to a subprocess passes through (`subprocess.run`, `from subprocess import Popen`, and asyncio's Windows transport). Two runs on the same repository, one with 2 calls and one with the same 2 plus a third that is refused, must start the same git commands.
- **Each repository** is a real git repository in `tmp_path` (P2-S2's `GitRepo` helper): commit `OLD`, then the fixture commit `C`, checked out. `C` holds `billing/invoice.py` (50 lines), `billing/checkout.py` (30 lines, line 12 being the spec's line), `lib/deep/nested/util.py`, `main.py` and `README.txt`. At `OLD`, line 12 is `charged = invoice.total()` and only the two billing files exist. Tests needing long lines, many matches or many files add them to `C` in their own repository.
- **Phase 1's prompts** (AC11) are golden files generated from the committed `src/review_bot/prompt.py` at `4b506d4` (`PROMPT_VERSION` `p1-v1`), before the developer's changes: the system prompt, and the user prompt for the sample diff.

### Criteria

Test names drop the `test_p3_s1_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_request_tools_are_exactly_read_grep_and_glob`. `ac1_each_tool_has_a_description_and_an_object_schema`. `ac1_read_shows_lines_10_to_14_at_c_numbered`. `ac1_run_exits_1_with_the_finding`. `ac1_read_defaults_to_the_file_from_line_1` (design.md defaults) | pass (5) |
| AC2 | `ac2_uncommitted_edit_is_not_read`. `ac2_untracked_file_is_not_found`. `ac2_context_rev_reads_the_older_commit` ×2 (SHA, `HEAD~1`). `ac2_file_only_at_c_is_not_found_at_the_older_commit`. `ac2_report_commit_is_the_resolved_context_rev` | pass (6) |
| AC3 | `ac3_grep_lists_matches_as_path_line_text_by_path_then_line`. `ac3_path_glob_limits_the_files_with_p2_s9_rules` ×7 (exact path, bare name at any depth, `**/`, `billing/*.py`, `*.py`, `lib/*.py` matching nothing, an unanchored-looking `nested/util.py` matching nothing). `ac3_ignore_case_matches_upper_case`. `ac3_without_ignore_case_upper_case_does_not_match`. `ac3_literal_dot_is_not_a_wildcard`. `ac3_regex_metacharacters_are_literal` ×6. `ac3_overlapping_matches_give_each_line_once`. `ac3_no_match_returns_no_lines_and_the_review_goes_on` | pass (19) |
| AC4 | `ac4_glob_lists_both_billing_files_sorted_one_per_line`. `ac4_double_star_lists_files_at_any_depth` (including the root, as `**` matches no segment too). `ac4_pattern_without_slash_matches_the_name_at_any_depth`. `ac4_single_star_stays_within_one_segment` | pass (4) |
| AC5 | `ac5_start_line_past_the_end_says_so_with_no_lines`. `ac5_line_count_400_is_accepted` (boundary). `ac5_invalid_arguments_are_refused_and_the_review_goes_on` ×23: `line_count` 401 or 3.5; unknown keys on each tool; wrong types (a number, `null`, a list, the string `"10"`, the bool `true` for an integer, `"yes"` for a bool); missing required keys on each tool; empty, 201-character and line-broken literals; empty and 201-character patterns; a path and a `path_glob` over 1,000 characters. `ac5_unknown_tool_is_refused_and_the_review_goes_on` ×5 (`write`, `bash`, `Read`, `read `, empty). `ac5_path_without_a_file_is_refused_and_the_review_goes_on` ×4 (`not_found` for a missing file and for a 1,000-character path, `directory` twice). `ac5_refusals_are_recorded_with_their_reason`. Each refusal is followed by a valid read that must succeed, then the finding | pass (35) |
| AC6 | `ac6_read_line_over_2000_characters_is_cut_and_marked`. `ac6_line_of_exactly_2000_characters_is_not_cut`. `ac6_grep_line_over_2000_characters_is_cut_and_marked`. `ac6_result_over_the_byte_cap_ends_at_a_line_boundary_with_a_note`: 400 lines of about 78 bytes against the 16,000 default. `ac6_result_within_the_cap_is_ok`. `ac6_more_than_50_grep_matches_returns_the_first_50_with_a_note`. `ac6_exactly_50_grep_matches_is_ok`. `ac6_more_than_200_glob_paths_returns_the_first_200_with_a_note` (210 files) | pass (8) |
| AC7 | `ac7_third_call_over_max_tool_calls_2_is_refused_call_limit`. `ac7_git_is_not_run_for_the_refused_call`. `ac7_default_cap_is_12_calls_and_every_later_call_is_refused` (14 calls). `ac7_second_read_over_max_tool_total_bytes_is_refused_byte_limit`: two reads of about 1,900 bytes each under `--max-tool-total-bytes 3000` | pass (4) |
| AC8 | `ac8_every_result_is_wrapped_with_its_hash` ×7 (read, grep, glob, past the end, and three refusals). `ac8_call_limit_refusal_is_wrapped`. `ac8_forged_closing_delimiter_stays_inside_the_wrapping`: a file line copying the real closing line with another hash, followed by an instruction. `ac8_forged_delimiter_in_a_grep_match_stays_inside`. Every other test also unwraps each result it reads | pass (10) |
| AC9 | `ac9_three_calls_give_three_context_tool_call_records`. `ac9_spans_name_the_lines_each_call_returned`: the read's one span, the grep's spans covering exactly the matched lines, no span for glob. `ac9_refused_call_has_its_reason_and_no_spans`. `ac9_file_text_and_grep_literal_appear_nowhere_in_the_log` (and the model did see the file text) | pass (4) |
| AC10 | `ac10_report_has_mode_prompt_version_and_context`: 3 calls, one refused. `ac10_report_findings_are_unchanged_by_context`. `ac10_summary_line_appends_tool_calls`. `ac10_text_output_is_otherwise_unchanged`: stdout equals a diff-mode run's with the same finding | pass (4) |
| AC11 | `ac11_no_context_repo_means_no_tools`. `ac11_prompts_are_byte_for_byte_phase_1s` (golden). `ac11_report_mode_is_diff_with_null_context`. `ac11_summary_line_has_no_tool_calls`. `ac11_no_context_tool_call_records` | pass (5) |
| AC12 | `ac12_context_repo_not_a_git_repository_exits_2`. `ac12_context_repo_missing_exits_2`. `ac12_context_repo_naming_a_file_exits_2`. `ac12_context_rev_naming_no_commit_exits_2` ×7: an unknown name, 40 zeros, `HEAD~5`, a tree, a blob, `--all`, and `--output=<file>` (option injection, which must write nothing). `ac12_context_rev_without_context_repo_exits_2`. Each checks exit 2, no provider request, and a `review-bot: error:` line naming the flag that isn't argparse's `unrecognized arguments` | pass (11) |
| Interfaces | `interfaces_cap_outside_its_design_range_exits_2` ×4: `--max-tool-calls` 0, 31 or `two`; `--max-tool-total-bytes` 400,001 | pass (4) |
| Limits | `limits_huge_literal_is_refused_quickly`: a 1,000,000-character literal is `invalid_arguments` in under 60 s. `limits_option_like_literal_is_searched_as_text` ×4 (`--no-index`, `-O`, `-f`, `--open-files-in-pager`) | pass (5) |

### Failures

None.

### Earlier suites changed

P3-S1 appends `mode` and `context` to every `report/v1` document (AC10, AC11), so suites pinning the exact key set failed: `test_p1_s10_json_output.py` (44 failed) and `test_p1_s13_triage_edit.py` (43 failed, 6 errors), the latter through `report()`, which it imports from P1-S10's suite, as do P1-S11's and P1-S12's.

- **`tests/integration/test_p1_s10_json_output.py`:** `REPORT_KEYS` appends `mode` and `context`, and `report()` now also checks they are the last two keys, as design.md's "Report keys, appended only" requires. No assertion was weakened.
- **`tests/fixtures/p1_s10/sample_report.golden.json`:** appends `"mode": "diff"` and `"context": null`, which is what AC11 says a run without `--context-repo` reports.
- **No other pinned key list** of `report/v1` was found in `tests/integration` or `tests/evals`. The eval suites' key lists are of recordings (`RECORDING_FIELDS`) and runs, not of `report/v1`, and P2-S1 compares the shadow's embedded report with a real run's, so both gain the keys together.

### Readings for the designer

1. **AC6's cut line** is read as: the kept text is a prefix of the original, between 1,900 and 2,000 characters long, with `(cut)` beside it. The spec doesn't say whether the 2,000 includes the marker.
2. **AC6's byte cap** is checked on the text between the delimiters, not the whole result. If the developer counts the delimiters too, the check still holds.
3. **AC5's "says so"** for a `start_line` past the end is checked only as a non-empty body with no numbered line. It doesn't say whether that is a refusal; the tests accept either.
4. **AC10's "appends"** is read as the summary line's last field.
5. **AC12's missing directory and file paths** aren't named by the spec, which says "a directory that isn't a git repository". They are tested because any other answer would let a typo run a review without context.
6. **Strictness beyond the spec, flagged for review:** `read`'s `start_line` given as `true`, and `line_count` given as `3.5`, are expected to be refused (`invalid_arguments`), as JSON Schema's `integer` rejects both. Python's `isinstance(True, int)` accepts the first.
7. **Not tested:**
   - The git timeout and the cap-plus-one read in Limits: they need a git that hangs or a blob over the cap, and the first can't be planted portably on Windows.
   - `--max-tool-result-bytes` as a flag, which the spec names only as a setting.
   - Hostile paths, symlinks and secrets files: S2's.

### Coverage

Not measured by the tester, who ran only this file. For coverage of `git`, `context`, `providers.mock`, `prompt`, `review`, `report` and `cli`, see the main session's `pytest --cov`.
