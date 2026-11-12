# Phase 4 test report

## P4-S1: Run under a versioned policy file that every report records

Spec: [p4-s1-policy-file.md](slices/p4-s1-policy-file.md). Tests: [`tests/integration/test_p4_s1_policy_file.py`](../../../tests/integration/test_p4_s1_policy_file.py), 138 cases. Golden file: [`tests/fixtures/p1_s10/sample_report.golden.json`](../../../tests/fixtures/p1_s10/sample_report.golden.json), which gains `policy`. They were written from the spec's criteria, Limits and Interfaces, design.md (Interfaces, `policy/v1`, Policy as code, report keys) and ADR 0018, while the developer built the slice and before any of its code was read.

**Status: every criterion passes. All 138 cases pass, from this file alone, on the developer's uncommitted working tree** (base `b63d961`).

**First runs:** the developer's code had mostly landed by the first run, so there is no "before the code" count. Four failures were the tests' own mistakes, fixed without changing what a criterion requires: `pr shadow` exits 0 once it writes its report (not 1); a diff-mode `caps` has no `max_cost_usd` key without a cap, as before; one hostile fixture was over 64 KB; and the over-64-KB case looked for `64`/`KB`/`too large` only, while the message says "over the limit of 65536 bytes", which names the problem. The accepted words now include `limit` and `65536`.

### Gate

Only this file and the earlier suites it changed were run, as the coordinator asked (the full suite runs out of memory locally): this file, `test_p1_s10_json_output.py`, `test_p2_s1_shadow_report_local.py`, `test_p2_s13_shadow_backfill.py`, `test_p3_s8_pr_review_with_context.py`, `test_p3_s16_live_workflow.py`, `test_p3_s9_linked_issue_criteria.py`, `test_p3_s6_live_evals_workflow.py`, and in `tests/evals/` `test_p1_s16_eval_live_command.py`, `test_p3_s6_live_context_evals.py`, `test_p3_s16_evals_live_dispatch.py`, `test_p3_s12_live_suggestion_scrub.py` and `test_p3_s9_linked_issue_evals.py`. All pass (2 skipped: no `/dev/full` on Windows). `ruff format --check` and `ruff check` are clean on `tests/integration` and `tests/evals`. For the full gate, see the main session's run.

### Seams and readings (assumed)

- **review-bot** runs in process: `cli.main([DIFF, "--provider", "mock", "--format", "json", ...], provider=MockProvider(...))`; context mode through P3-S1's `Harness` over a real git repository. **pr** `shadow`, `run` and `backfill` through `review_bot.pr.cli.main`, with P2-S13's `Backfiller` and P3-S8's `PrRunner`. **evals** `live` and `replay` through `review_bot.evals.main([...], sdk_call=fake)`.
- **The per-call cap** (AC6) is read where a real call would carry it: the run builds its own Claude provider, and the suite's guard stops the real SDK call and records its options, so `max_budget_usd` is checked. For `evals live`, the scripted `sdk_call` records the options. For `pr`, `caps.max_cost_usd` in `shadow/v1`.
- **"Before reading the diff"** (AC3): the diff path doesn't exist, so a run that read it would exit 3; exit 2 naming the policy shows it never did. **"No provider is built"**: `build_provider` in `cli` and `pr.cli` is replaced by a recorder that fails if called.
- **"64 KB"** is read as 64,000 or 65,536 bytes: 64,000 bytes must be accepted and 65,537 refused.
- **"Names the file and the key or problem"**: the message holds the file's name, and the key (`foo`, `version`, `max_cost_usd`, `allowed`, `redaction`, `models`) or a word for the problem (`UTF-8`, `TOML`, a size).
- **AC5's message** is matched with or without the backticks around the version.
- **Built-in hash**: the golden pins it, `a8c7bd7e…28431c`, so a change to the built-in policy's fixed text shows up as a golden change.

### Criteria

Test names drop the `test_p4_s1_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_report_without_a_policy_records_the_builtin_policy` (exactly `version`, `sha256`, `source`). `ac1_report_policy_is_appended_after_phase_3s_keys`. `ac1_builtin_sha256_is_fixed_across_runs_and_reports` (two runs, `shadow/v1` and its embedded report). `ac1_report_is_p1_s10s_golden_with_only_the_builtin_policy_added`. `ac1_request_is_byte_for_byte_phase_1s_without_a_policy` (P3-S1's prompt goldens). `ac1_context_review_without_a_policy_offers_all_three_tools`. `ac1_shadow_caps_hold_no_cost_cap_without_a_policy`. `ac1_audit_start_records_the_builtin_policy`. `ac1_explicit_policy_like_the_builtin_changes_only_the_policy_field` (report and request equal but for `policy`). The Phase 0 to 3 suites listed under Gate pass | pass (9) |
| AC2 | `ac2_report_records_the_files_version_sha256_and_source_as_given` (relative `p.toml`). `ac2_source_is_an_absolute_path_exactly_as_given`. `ac2_sha256_is_of_the_files_raw_bytes_crlf_included`. `ac2_shadow_and_its_report_record_the_same_policy`. `ac2_audit_start_has_policy_version_and_sha256` (review-bot) and `ac2_shadow_audit_start_has_policy_version_and_sha256`. `ac2_the_variable_alone_chooses_the_policy`. `ac2_the_flag_wins_over_the_variable`. `ac2_the_flag_wins_over_an_invalid_variable` (the variable's file is never read). `ac2_pr_run_records_the_policy`. `ac2_backfill_records_the_policy_in_every_report` | pass (11) |
| AC3 | `ac3_invalid_policy_exits_2_before_the_diff_naming_file_and_problem` ×30: unknown keys at the top, in `[tools]` and `[cost]`; a later slice's `[redaction]` and `[[models]]`; no `version` (a file with only `[tools]`, and an empty file); `version` with a space, a slash, empty, 65 characters, non-ASCII, a number; tools `bash`, `Read`, repeated `read`; `max_cost_usd` `0.009`, `0`, `-1`, `1000.01`, a float, an integer, `abc`, `NaN`, `Infinity`, empty; not UTF-8; two not-TOML files; 65,537 bytes. `ac3_missing_policy_file_exits_2_naming_it`. `ac3_unreadable_policy_exits_2_naming_it` (a directory). `ac3_invalid_policy_from_the_variable_exits_2_naming_it`. Controls: `ac3_file_of_64000_bytes_is_accepted`, `ac3_ceiling_at_or_inside_its_range_is_accepted` ×4 (`0.01`, `1000` included), `ac3_version_matching_the_pattern_is_accepted` ×4 (1 and 64 characters), `ac3_any_subset_of_the_three_tools_is_accepted` ×4 (`[]` included). Every command: `ac3_shadow_…`, `ac3_pr_run_…`, `ac3_backfill_…` and `ac3_evals_refuse_an_invalid_policy_before_any_call` ×2 (`live`, `replay`), each with no provider request and nothing written | pass (51) |
| Limits | `limits_a_long_unknown_key_is_shown_cut_to_100_characters` (20,000 characters). `limits_a_long_version_is_shown_cut_to_100_characters`. `limits_unprintable_characters_in_a_message_are_escaped` ×5 (ESC in `version`, BEL, a line feed and U+202E in quoted keys, NUL in a tool name). `limits_deep_nesting_within_64_kb_exits_2_in_bounded_time` ×2 (30,000 nested arrays, 15,000 nested inline tables: `tomllib` recurses, so an overflow must still be exit 2, not 14). `limits_many_keys_within_64_kb_exit_2_in_bounded_time` (4,000 unknown keys, with a message under 4,000 characters) | pass (10) |
| AC4 | `ac4_request_tool_definitions_are_exactly_read_and_glob`. `ac4_grep_call_returns_a_delimited_policy_denied_refusal` (unwrapped and hash-checked, one line). `ac4_audit_tool_call_record_is_refused_policy_denied`. `ac4_allowed_tools_still_run`. `ac4_denied_call_counts_towards_max_tool_calls` (`--max-tool-calls 2`: denied, ok, `call_limit`). `ac4_denied_call_runs_no_git`. `ac4_only_policy_denied_for_a_known_tool_not_allowed` ×2 (an unknown tool stays `unknown_tool`). `ac4_order_of_allowed_does_not_matter`. `ac4_pr_run_context_offers_only_the_allowed_tools`. `ac4_executor_ledger_entry_is_refused_policy_denied` (`RepoTools(..., allowed=...)` directly). `interfaces_refusal_reason_policy_denied` | pass (12) |
| AC5 | `ac5_context_repo_with_no_tools_exits_2_before_any_call` (the spec's sentence). `ac5_pr_run_context_repo_with_no_tools_exits_2_before_any_call` ×2 (`--context repo`, `REVIEW_BOT_CONTEXT=repo`). `ac5_diff_mode_with_no_tools_runs_as_usual`. `ac5_pr_run_in_diff_mode_with_no_tools_runs_as_usual` | pass (5) |
| AC6 | `ac6_diff_mode_call_is_capped_at_the_ceiling` and `ac6_context_mode_call_is_capped_at_the_ceiling` (`max_budget_usd` 0.60 on the real call). `ac6_control_no_policy_no_cap_in_diff_mode`. `ac6_lower_flag_caps_the_call_below_the_ceiling` (0.30). `ac6_flag_equal_to_the_ceiling_is_allowed`. `ac6_shadow_caps_show_the_ceiling_in_diff_mode`, `ac6_shadow_caps_show_the_lower_flag`, `ac6_pr_run_context_caps_show_the_ceiling`. Above the ceiling, exit 2 naming 1.00, 0.60 and the policy, with no request: `ac6_flag_above_the_ceiling_…`, `ac6_variable_above_the_ceiling_exits_2_too` (`REVIEW_BOT_MAX_COST_USD`), `ac6_context_mode_above_the_ceiling_…`, `ac6_shadow_above_the_ceiling_exits_2_and_writes_no_report`, `ac6_pr_run_above_the_ceiling_…` | pass (13) |
| AC7 | `ac7_evals_live_caps_each_call_at_the_ceiling`. `ac7_evals_live_lower_flag_caps_each_call`. `ac7_evals_live_flag_above_the_ceiling_exits_2_before_any_call`. `ac7_evals_live_allowance_uses_the_ceiling_as_the_cap` (3 × 0.60 over 1.79). `ac7_evals_live_from_the_variable_takes_its_ceiling`. `ac7_evals_replay_takes_policy` (same output with and without it). `ac7_backfill_caps_each_review_at_the_ceiling`. `ac7_backfill_flag_above_the_ceiling_exits_2_before_any_call`. `ac7_backfill_allowance_uses_the_ceiling_as_the_cap` | pass (9) |
| AC8 | `ac8_spec_example_diff_mode_without_either_flag_exits_2_naming_both` (three cases, `--runs 3 --max-calls 15`). `ac8_default_mode_is_refused_too` (no `--mode`). `ac8_evals_live_diff_mode_names_the_missing_flag` ×2. `ac8_evals_live_ceiling_without_max_usd_names_max_usd`. `ac8_evals_live_diff_mode_over_its_allowance_exits_2`. `ac8_evals_live_diff_mode_exact_fit_runs_capped`. `ac8_backfill_diff_mode_without_either_flag_exits_2_naming_both`. `ac8_backfill_diff_mode_names_the_missing_flag` ×2. `ac8_backfill_ceiling_and_max_usd_suffice`. `ac8_backfill_diff_mode_over_its_allowance_exits_2`. `ac8_backfill_diff_mode_with_both_flags_runs`. `ac8_live_yml_evals_steps_pass_both_flags_within_their_allowance` ×2 and `ac8_live_yml_backfill_step_passes_both_flags_within_its_allowance` (Decimal: calls × cap ≤ `--max-usd`). `ac8_live_yml_evals_steps_still_parse_with_the_real_parser` ×2. P3-S8's AC12 and P3-S6's AC11 tests also pass, parsing the backfill and evals steps with the real parsers | pass (18) |

### Failures

None.

### Earlier suites changed

Each keeps its intent.

- **AC8 (dollar flags):** `test_p2_s13_shadow_backfill.py`'s `Backfiller.run` now passes `--max-usd 1000 --max-cost-usd 0.10` by default (`dollar_flags`, before the test's own flags so its values win; `()` leaves them out). The budget then adds `spent_usd=` to the entries line, so the exact count checks compare `counts_without_spend`. `test_p1_s16_eval_live_command.py`'s `flags()` and its two AC5 refusal tests add the same amounts; `test_p3_s16_evals_live_dispatch.py` and `test_p3_s12_live_suggestion_scrub.py` use `flags()`. An unreported cost counts as the cap, so these amounts never stop a run. In `test_p3_s8_pr_review_with_context.py`, the AC7 tests of missing dollar flags and of the cap from its variable pass `dollar_flags=()`, and AC9 compares the counts without the spend.
- **Removed:** `test_p3_s6_ac6_diff_mode_without_the_flags_is_p1_s16s_command`, which required an uncapped diff-mode `evals live`. P4-S1 AC8 supersedes it, and a comment in its place says so.
- **AC1 (the new key; AC1 now allows key pins to gain it):** `REPORT_KEYS` and the append-order check in `test_p1_s10_json_output.py`, `SHADOW_KEYS` in `test_p2_s1_shadow_report_local.py`, and P3-S8's `assert_phase_2_run`, which requires every key beyond Phase 2's to be empty, now accept `policy` and require the built-in one. `sample_report.golden.json` gains the built-in `policy`.

### Coverage

From this file alone, measured before the two no-`version` cases were added: `governance/policy.py` 98%. Its one uncovered line, the missing-key message, is now reached by those cases. The commands it drives are covered in part from here, as expected for a slice that touches them: `cli.py` 79%, `context/tools.py` 79%, `evals/live.py` 78%, `pr/cli.py` 65%, `settings.py` 94%. The unit tests and the suites above cover the rest; the 90% gate is the main session's full run.

### Gaps

- **Claude's own tool list** (`allowed_tools`, the init check, the hook and `can_use_tool`) under a policy is S3's, so it isn't tested here; AC4 is checked through the executor, with the mock and `RepoTools`.
- **An unreadable file** is a directory, not a file without read permission: Windows can't remove read access with `chmod`.
- **Context-mode `evals live` under a ceiling** isn't run: it would need P3-S6's context fakes, and the cap and allowance arithmetic is the same code path diff mode exercises here.

## P4-S2: Review with a second, OpenAI-compatible provider that passes the conformance suite

Spec: [p4-s2-openai-compatible-provider.md](slices/p4-s2-openai-compatible-provider.md). Tests:
- [`tests/integration/test_p4_s2_openai_compatible_provider.py`](../../../tests/integration/test_p4_s2_openai_compatible_provider.py): 254 cases for AC1 to AC4 and AC6 to AC10.
- The `openai` factory in [`tests/conformance/conftest.py`](../../../tests/conformance/conftest.py), with [`test_p4_s2_openai_conformance.py`](../../../tests/conformance/test_p4_s2_openai_conformance.py): AC5.
- The shared fake, [`tests/fake_openai.py`](../../../tests/fake_openai.py).

The tests were written from the spec's criteria, Limits and Interfaces, design.md (Interfaces, "Second provider and routing") and ADRs 0002 and 0020, while the developer built the slice. Their code was read only for the injection point: `HttpResponse(status, headers, body)`, and a transport that raises the standard library's `OSError`s.

**Status: every criterion passes, AC10 included.** The run was on the developer's uncommitted working tree (base `849f3b8`): 352 passed, and 2 skipped (`tool_loop` for `openai`, which AC5 allows). The first run failed 2 AC4 cases, which the developer has since fixed (see Failures).

### Gate

The coordinator asked for a partial run, because the full suite runs out of memory locally. These ran: this slice's files, `tests/conformance`, `test_p0_s1_offline.py`, `test_p1_s5_provider_notices.py` and `test_p0_s3_safe_stderr.py`. `ruff format` and `ruff check` are clean on every file changed. `mypy` covers `src` only. For the full gate, see the main session's run.

### Seams and readings (assumed)

- **Every command builds its own provider.** No provider is injected for `--provider openai`. `fake_openai.use_fake_transport` replaces `openai_compat.UrllibTransport`. So `review-bot`, `pr shadow` and `pr run` each build the real `OpenAICompatibleProvider`, with the CLI's own base URL, key and checks, over a scripted `HttpTransport`. The script's last step repeats, so a retrying provider keeps getting the same answer.
- **The real `UrllibTransport`** also runs, unpatched, against a `ThreadingHTTPServer` on 127.0.0.1, which the suite's network guard allows. These runs cover redirects not followed, the 4 MB cap (with a `Content-Length`, without one, and with a lying one), a refused connection, a slow server under `--deadline-s`, and status mapping.
- **"Byte for byte" (AC1)** is checked against the request `MockProvider` gets for the same diff and `--model`, including non-ASCII text and CRLF.
- **"As Claude's are" (AC2)**: the same content goes through Claude's fake SDK and through `openai`, and both must give the same exit code and stdout. There are 9 contents: a finding, empty, a bad severity, confidence 1.5, a missing field, fenced JSON, prose, `[]`, and an unanchored line.
- **"One warning naming the retry"** is one stderr line holding "warning" and "retr". **"Fails at once"** is one request, done within 10 s.
- **"4 MB"** is read as 4 MiB or 4,000,000 bytes: 3,900,000 bytes are accepted, and 4 MiB + 1 is refused.
- **An empty `OPENAI_API_KEY`** counts as unset (AC7).
- **Every `openai` case passes a model** (`--model m1`, or `REVIEW_BOT_MODEL` where AC10's variable form is the point), so AC10 never decides another criterion's outcome.
- **AC9's variable** `REVIEW_BOT_MAX_COST_USD` is refused like the flag: exit 2, and no request. Its message isn't checked.
- **"A Claude subprocess never holds the key" (AC8)**: the fake `sdk_call` records whether `OPENAI_API_KEY` is in `os.environ` and in `options.env` when the subprocess would start. This covers `review-bot --provider claude` and `evals live`.
- **"Before a provider is built" (AC8)**: `build_provider` in `cli` and `pr.cli` is replaced by a recorder that checks `os.environ`.
- **`tests/conftest.py`** now also removes `OPENAI_API_KEY` for the whole session, so a developer's own key can't reach a test.
- **`test_p0_s4_ac14_mock_and_claude_are_both_in_the_suite`** now checks that `mock` and `claude` are among the factories, rather than the only two. `test_p4_s2_ac5_mock_claude_and_openai_are_all_in_the_suite` checks the exact set.

### Criteria

Test names drop the `test_p4_s2_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_one_post_to_the_default_base_with_the_bearer_key`. `ac1_body_model_is_the_flags_model`. `ac1_messages_are_the_system_and_user_prompts_byte_for_byte`. `ac1_prompts_hold_non_ascii_and_crlf_unchanged`. `ac1_no_output_cap_and_no_tools_without_the_flag`. `ac1_cap_is_max_completion_tokens_for_api_openai_com` ×2 (one with `:443`). `ac1_cap_is_max_tokens_for_any_other_host` ×4 (loopback, self-hosted, `api.openai.com.evil.example`, `eu.api.openai.com`). `ac1_base_with_a_path_prefix_gets_chat_completions_appended`. `ac1_base_from_the_variable`. `ac1_real_transport_posts_to_chat_completions` | pass (14) |
| AC2 | `ac2_finding_is_reported_as_claudes_would_be`. `ac2_report_names_provider_openai_and_model_m1`. `ac2_usage_comes_from_prompt_and_completion_tokens` (the summary line and `agent.called`). `ac2_summary_and_audit_name_openai_and_m1`. `ac2_same_content_gives_claudes_exit_code_and_report` ×9 | pass (13) |
| AC3 | `ac3_response_fails_with_its_category_and_exit_code` ×42 and `ac3_audit_records_the_failure_category` ×42, over one table. `auth`: 401, 403. `rate_limit`: 429. `transient`: 500, 502, 503, 504, 529, 599, a 503 HTML page, a timeout, a reset. `unavailable`: refused, DNS. `permanent`: 429 `insufficient_quota`, 400, 402, 404, 405, 409, 413, 418, 422, 451, 499, and the redirects 300, 301, 302, 303, 307, 308. `refusal`: `content_filter`, a refusal, a refusal beside findings. `invalid_output`: prose, empty content, null content, another object, `length`, `length` with cut JSON. Also `ac3_redirect_is_never_followed`, `ac3_quota_error_with_a_retry_after_is_still_permanent`, `ac3_empty_refusal_beside_findings_is_not_a_refusal`, `ac3_real_transport_never_follows_a_redirect` ×5, `ac3_real_transport_refused_connection_is_unavailable` and `ac3_real_transport_error_statuses_map_to_their_categories` ×3 | pass (92) |
| Limits | `limits_malformed_200_body_fails_invalid_output` ×15, each within 10 s: not JSON; HTML; a top-level array, `null` or string; no `choices`; empty `choices`; `choices` not a list; a choice without `message`; a `message` that isn't an object; content that isn't a string; invalid UTF-8; truncated; empty; 100,000-deep nesting. `limits_body_over_4_mb_fails_invalid_output`. `limits_valid_body_just_under_4_mb_is_read`. `limits_error_message_never_reaches_the_users_message`. `limits_200_with_bad_usage_still_fails_cleanly_not_internally`. `limits_real_transport_reads_at_most_4_mb` ×2 (with `Content-Length`, and read until close). `limits_real_transport_lying_content_length_is_still_capped`. `limits_real_transport_reads_a_body_under_4_mb` | pass (23) |
| AC4 | `ac4_503_then_200_with_one_retry_succeeds_with_one_warning`. `ac4_transient_and_rate_limit_are_retried` ×4 (429, 500, timeout, reset). `ac4_retries_send_the_same_request`. `ac4_other_categories_are_never_retried` ×9 (auth, 400, quota, redirect, refusal, invalid output, `length`, refused, DNS). `ac4_no_retry_with_max_retries_0`. `ac4_retries_stop_at_max_retries`. `ac4_rate_limit_after_its_retries_is_rate_limit`. `ac4_retry_after_within_30_s_is_honoured` (at least 0.95 s for `Retry-After: 1`). `ac4_retry_after_over_30_s_fails_at_once` ×6 (429 and 503, each with `31`, `3600`, and an HTTP date an hour ahead). `ac4_unreadable_retry_after_never_hangs` ×5 (`soon`, `-5`, `1e9`, empty, `NaN`). `ac4_deadline_bounds_a_call_that_never_answers`. `ac4_no_retry_waits_past_the_deadline` (`Retry-After: 20` with `--deadline-s 2`). `ac4_deadline_bounds_the_retries_together`. `ac4_transport_timeout_is_within_the_deadline`. `ac4_real_transport_slow_server_is_bounded_by_the_deadline` | pass (35) |
| AC5 | Every test in `tests/conformance` runs for `openai`: the P0-S3, P1-S5 and P3-S3 suites, with `tool_loop` skipped under a reason naming P4-S6. Also `ac5_mock_claude_and_openai_are_all_in_the_suite`, `ac5_openai_factory_builds_the_openai_compatible_provider` and `ac5_tool_loop_is_skipped_for_openai_naming_s6` | pass (29, 2 skipped) |
| AC6 | `ac6_refused_base_flag_exits_2_naming_it_before_any_request` ×11 and `ac6_refused_base_variable_exits_2_naming_it` ×11, over the same bases: `http` to a remote host, `http://127.0.0.1.evil.example`, `http://localhost.evil.example`, userinfo with and without a password, loopback with userinfo, a query, a fragment, a loopback query, `ftp`, no scheme. `ac6_refusal_never_shows_the_userinfos_password`. `ac6_http_on_loopback_is_accepted` ×3 (`127.0.0.1:8000`, `localhost`, `[::1]`) | pass (26) |
| AC7 | `ac7_no_key_with_a_remote_base_exits_2_naming_it_before_any_request` ×4 (the key unset or empty, with the default or a remote base). `ac7_no_key_with_a_loopback_base_sends_no_authorization` ×3. `ac7_mock_run_needs_no_openai_key` | pass (8) |
| AC8 | `ac8_review_bot_removes_the_key_before_building_a_provider` ×3 (`openai`, `claude`, `mock`). `ac8_claude_subprocess_never_holds_the_key`. `ac8_pr_removes_the_key_before_building_a_provider` ×2 (`shadow`, `run`). `ac8_evals_live_claude_call_never_holds_the_key`. `ac8_evals_replay_removes_the_key`. `ac8_key_is_in_no_report_message_or_audit_record_on_success` ×2 (a plain key and an `sk-`-shaped one, each in text, JSON and Markdown). `ac8_key_echoed_by_the_server_reaches_no_output` ×12 (the key in a 401's JSON or plain text, a 500, `error.code`, the content and a refusal, each with both keys). `ac8_key_echoed_in_a_retry_warning_reaches_no_output`. `ac8_error_body_is_kept_only_as_scrubbed_diagnostics` (it appears only in `agent.failed` diagnostics). `ac8_error_body_quoting_the_diff_is_scrubbed`. `ac8_pr_reviews_over_openai_and_never_write_the_key` ×2 (`shadow` and `run`; checks every file in `--out`, the audit log, stdout and stderr) | pass (27) |
| AC9 | `ac9_max_cost_usd_flag_exits_2_before_any_request` (the exact message). `ac9_max_cost_usd_variable_exits_2_before_any_request`. `ac9_policy_cost_ceiling_exits_2_before_any_request` (the exact message). `ac9_policy_without_a_cost_ceiling_runs`. `ac9_pr_max_cost_usd_exits_2_before_any_request` ×2 (`shadow`, `run`). `ac9_cost_refusal_is_before_the_key_check` | pass (7) |
| AC10 | `ac10_review_bot_without_model_exits_2_naming_it_before_any_request`. `ac10_provider_from_the_variable_without_model_exits_2` (`REVIEW_BOT_PROVIDER=openai`). `ac10_never_sends_claudes_default_model`. `ac10_model_from_the_variable_satisfies_it` (`REVIEW_BOT_MODEL=m2` is sent). `ac10_pr_without_model_exits_2_naming_it_before_any_request` ×2 (`shadow`, `run`). `ac10_pr_model_from_the_variable_satisfies_it` ×2. Control: `ac10_claude_and_mock_still_default_their_model` | pass (9) |

### Failures

None open.

**Fixed: a `Retry-After` an hour ahead, given as an HTTP date (AC4).** On the first run, `test_p4_s2_ac4_retry_after_over_30_s_fails_at_once[http_date_an_hour_ahead-429]` and `[…-503]` failed. RFC 9110 allows `Retry-After` as delay-seconds or as an HTTP date, and the provider read only the seconds form. A date was treated as if no header had been sent: the call was retried, and the run exited 0. The developer now reads both forms, and both cases pass. The test is unchanged.

Minimal reproduction, as first reported:

```python
transport = fake(
    api_error(429, retry_after=formatdate(time.time() + 3600, usegmt=True)), no_findings()
)
done = openai.review(transport, "--max-retries", "2")
# expected: exit 9 (rate_limit) after 1 request; observed then: exit 0 after 2 requests
```

### Coverage

From this slice's tests alone (the files above, `tests/conformance` and `test_p0_s1_offline.py`):
- `providers/openai_compat.py`: 98%. Lines 164 (a `URLError` whose reason isn't an `OSError`) and 234 (a request with tools) aren't covered.
- `providers/openai_wire.py`: 98%. Lines 191 and 223 aren't covered.

### Gaps

- **A `Retry-After` of exactly 30 s** isn't run: honouring it would wait 30 s per case. A 1 s wait is shown to be honoured, and 31 s fails at once.
- **A DNS failure and a reset connection** are scripted only through the fake transport (`socket.gaierror`, `ConnectionResetError`). A real DNS lookup would leave loopback, which the suite's network guard forbids.
- **`pr backfill` and `evals live` with `--provider openai`** aren't tested. Live commands measure one model, `evals live` builds only Claude, and the spec names `pr` and `evals` only for AC8.
- **A real 3xx with no `Location`** isn't run against the local server. The fake covers each redirect status.

## P4-S3: Let Claude's hook and `can_use_tool` enforce the tool policy

Spec: [p4-s3-claude-enforces-tool-policy.md](slices/p4-s3-claude-enforces-tool-policy.md). Tests: [`tests/integration/test_p4_s3_claude_enforces_tool_policy.py`](../../../tests/integration/test_p4_s3_claude_enforces_tool_policy.py), 55 cases, and [`tests/conformance/test_p4_s3_tool_policy_conformance.py`](../../../tests/conformance/test_p4_s3_tool_policy_conformance.py), 3 cases (one skipped for `openai`). They were written from the spec's criteria, Limits and Interfaces, design.md (Policy as code, the S3 risk row), ADR 0018 and Phase 3's Tool safety, while the developer built the slice and before any of its code was read.

**Status: AC1 to AC6 pass. AC7 is pending (a recorded live step).** Run on the developer's uncommitted working tree (base `5142aa8`).

**First runs:** 9 cases failed on the first run with exit 14. They were the tests' own mistake: `async` tests called `main`, which runs its own event loop. They now take the options from a synchronous fixture, or call the hook with `asyncio.run`. No assertion changed.

### Gate

Run in small groups, as the machine is short of memory: this file, `tests/conformance` (every scenario for `mock`, `claude` and `openai`), `test_p3_s3_claude_drives_tools.py` and `test_p0_s2_lockdown.py`, all green. `ruff format --check .`, `ruff check .` and `mypy` are clean on the whole tree. A full `pytest --cov` (without the developer's `tests/unit/test_claude_tools.py`, which was mid-edit and didn't import) had no failure up to 38%, when it hit the 25-minute local limit. For the full gate and the 90% coverage, see the main session's run.

### Seams and readings (assumed)

- **Every review runs through `cli.main`** with `--policy`, `--context-repo` over a one-commit repository, and a `ClaudeAgentProvider` over `FakeSdk`, so the policy reaches the provider however the pipeline passes it. A thin wrapper around the provider keeps its `ReviewResponse`, for `denied_tools`.
- **The CLI's side** is played by `fake_mcp`: the init message, `tools/list` and `tools/call` through the SDK's own MCP bridge, the `PreToolUse` hook (with the CLI's hook input) and `can_use_tool` (with a `ToolPermissionContext`).
- **"The same pure function as the executor" (AC2)** is checked by behaviour: for each of the 7 tool sets a context policy can name, the hook denies `mcp__repo__<tool>` exactly when `RepoTools(..., allowed=...)` refuses `<tool>` with `policy_denied`. Whether the code calls one function is for the reviewer.
- **"The same reason" (AC3)**: for each name, the deny message from `can_use_tool` equals the hook's reason, and holds the policy's version.
- **"The same user prompt text" (AC3)**: the streaming input's user messages, joined (string content or text blocks), equal the `user_prompt` that `MockProvider` gets for the same diff, repository and policy.
- **"Exactly as in Phase 3" (AC5)**: every `ClaudeAgentOptions` field is Phase 3's value or the SDK default, under the built-in policy and under `read`+`glob`, with `can_use_tool` and `permission_prompt_tool_name` unset, no hooks, and the prompt the same string as `MockProvider`'s. The options are also equal across the two policies.
- **"At most 100 characters, escaped" (AC4)**: a 10,000-character name is recorded as a prefix of at most 100 characters; a name with a line feed, ESC and U+202E is recorded printable, with `\n` escaped. A name over 100 characters that also holds control characters isn't checked for length, since escaping can lengthen it.
- **Limits, "shown capped and escaped"**: the hook's reason and the callback's message for a hostile name are printable and under 2,000 characters, within 5 s.

### Fake SDK changes (AC6)

- **`tests/fake_sdk.py`**: `FakeSdk` accepts a prompt given as streaming input (an async iterable of messages). As the SDK does, it reads the prompt in a task beside the script, into `input_messages`, and cancels the reader when the stream ends. A string prompt is handled exactly as before.
- **`tests/fake_mcp.py`**: `call_can_use_tool` and `permission_denial` (ask the callback, read its answer), `hook_step` and `can_use_tool_step` (script steps that run the hook or the callback mid-stream), and `PermissionLog`.

`tests/conformance/conftest.py` is unchanged: Claude's `tool_loop` already runs over `FakeSdk`, and now goes through the hook, `can_use_tool` and streaming input.

### Criteria

Test names drop the `test_p4_s3_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_allowed_tools_are_exactly_read_and_glob`. `ac1_server_lists_only_read_and_glob` (`tools/list` through the MCP bridge). `ac1_init_listing_exactly_read_and_glob_in_any_order_proceeds`. `ac1_init_listing_anything_else_fails_permanent` ×6 (all three tools, with `Bash`, with another server's tool, without `glob`, built-ins only, empty: exit 12, `permanent`, each unexpected or missing name in stderr, no tool call run). Control: `ac1_builtin_policy_still_allows_the_three` | pass (10) |
| AC2 | `ac2_hook_allows_read_and_glob`. `ac2_hook_denies_grep_bash_and_read_naming_the_version` ×3. `ac2_hook_denies_other_tools_and_lookalikes` (9 other tools and 19 near misses, such as `read`, `mcp__repo__Read`, a trailing space or NUL, `mcp__repo__*`, `*` and empty; each reason names the version). `ac2_hook_decides_as_the_executor_for_every_policy` ×7. `ac2_builtin_policy_hook_allows_the_three_and_names_builtin_v1` | pass (13) |
| AC3 | `ac3_options_carry_a_can_use_tool_callback`. `ac3_permission_prompt_tool_name_is_unset`. `ac3_can_use_tool_allows_read_and_glob_by_full_name` (and doesn't rewrite the input). `ac3_can_use_tool_denies_every_other_name_with_the_hooks_reason` (the same 28 names, and 4 that aren't text). `ac3_prompt_is_streaming_input_with_the_same_user_prompt_text`. `ac3_builtin_policy_can_use_tool_allows_the_three` | pass (6) |
| AC4 | `ac4_each_denial_is_in_the_response_and_the_audit_log` (hook `Bash`, callback `grep`, hook `grep` in order, with allowed calls between that add nothing; each record's `policy_version`). `ac4_review_goes_on_after_denials` (a later `read` runs, exit 1 with the finding). `ac4_no_denial_means_no_record_and_an_empty_tuple`. `ac4_one_record_per_denial_even_when_repeated` (80 denials, 80 records). `ac4_builtin_policy_records_builtin_v1`. `ac4_long_tool_name_is_recorded_capped_at_100` ×2 and `ac4_unprintable_tool_name_is_recorded_escaped` ×2 (one per layer; ESC and U+202E reach neither stdout nor stderr). `ac4_denials_before_a_failure_are_recorded_before_failed` ×3 (a hook and a `can_use_tool` denial, then a cost cap, a turn cap, or a second init listing `Bash`: exit 12, the agent events end `denied, denied, failed`, each `denied` with its layer and `policy_version`) | pass (12) |
| Limits | `limits_hook_denies_a_name_that_isnt_text` (`None`, 7, a list, a mapping). `limits_hostile_names_give_bounded_printable_reasons` ×3 (1,000,000 characters, control characters, 400,000 control characters). `limits_denying_a_name_that_isnt_text_is_recorded_and_the_review_goes_on` ×4 | pass (8) |
| AC5 | `ac5_diff_mode_options_are_phase_3s_exactly` ×2 (built-in and `read`+`glob`). `ac5_diff_mode_prompt_is_the_same_string` ×2. `ac5_diff_mode_options_dont_depend_on_the_policy`. `ac5_diff_mode_records_no_denial_and_an_empty_tuple`. Diff mode's lockdown tests pass unchanged: `test_p0_s2_lockdown.py` and P3-S3's `ac2_*` | pass (6, and the earlier suites) |
| AC6 | Every test in `tests/conformance` runs for `claude`, `tool_loop` included (`test_p3_s3_ac7_tool_loop_*[claude]`). New: `ac6_tool_loop_carries_no_denied_tool` ×3 (`mock`, `claude`; `openai` skipped until P4-S6) | pass (88 passed, 3 skipped: `openai`'s `tool_loop`) |
| AC7 | A recorded live step, not a test. **Pending:** it needs this slice merged and the Phase 4 budget approved ([Q10](design.md#6-open-questions)). Then `smoke` (≤ $0.60) records, for each run, the init tool list, outcome, tool calls, cost and any `agent`/`denied` records. Stop rule: if a run fails because of the wiring, this slice is reverted before any other context run | pending |

### Failures

None open.

### Coverage

From this file and `tests/conformance` alone: `providers/claude_tools.py` 97% (line 138 and one branch aren't reached), `providers/base.py` 100%, `providers/claude_agent.py` 79% and `review.py` 82%. The rest of those two files is diff mode's error mapping and other pipeline paths, which earlier suites and the unit tests cover.

### Gaps

- **What the real CLI does** isn't tested offline: whether it consults `can_use_tool` for tools that `allowed_tools` already approves, and whether it keeps streaming input open as the fake does. The design leaves this to the `smoke` (AC7).
- **Denials before a provider error other than a cap or the init check** (such as `auth`) aren't tested; AC4 names only those two failures.
- **`DeniedTool.layer`** is read as `hook` or `can_use_tool`, through its value if it is an enum.
