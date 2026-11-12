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

## P4-S4: Redact personal data before any vendor call

Spec: [p4-s4-redaction.md](slices/p4-s4-redaction.md). Tests: [`tests/integration/test_p4_s4_redaction.py`](../../../tests/integration/test_p4_s4_redaction.py), 173 cases. Golden file: [`tests/fixtures/p1_s10/sample_report.golden.json`](../../../tests/fixtures/p1_s10/sample_report.golden.json), which gains `"redaction": {}`. The tests were written from the spec's criteria, Limits and Interfaces, design.md (Redaction, Policy as code, Interfaces) and ADR 0022, while the developer built the slice and before any of its code was read.

**Status: every criterion passes, as revised after review (AC3, AC5, AC6). All 173 cases pass on the developer's working tree** (base `510546f`, their fixes uncommitted). Before `review` was wired, 84 of the first 90 pattern and AC1 cases failed, so the cases do detect missing masking.

**Revised criteria:** the 12 cases added for them were run first against `510546f`. The 5 AC3 cases (groups past 15 digits) and the 2 AC5 cases (a value straddling the cut) failed there, as expected. The 4 AC6 path cases and the AC5 control already passed. After the developer's fix, all 12 pass.

### Gate

Only this file and the suites it touches ran, in small groups, as the coordinator asked (the machine is short of memory): this file, `test_p4_s1_policy_file.py`, `test_p1_s10_json_output.py`, `test_p1_s11_markdown_output.py`, `test_p1_s12_triage.py`, `test_p1_s13_triage_edit.py`, `test_p2_s1_shadow_report_local.py`, `test_p3_s9_linked_issue_criteria.py` (run again after the revision), `test_p3_s12_suggested_fixes.py` and `test_p3_s17_shadow_sample_supply.py`. All pass (6 skipped: no `/dev/full` or POSIX terminal on Windows). `ruff format --check` and `ruff check` are clean on `tests/integration`. `scan_secrets` reports nothing in the new file, which a test also checks. For the full gate, see the main session's run.

### Seams and readings (assumed)

- **review-bot** runs in process with `MockProvider`. The request it receives (`requests`, `tool_results`) is what is sent. Context mode uses `--context-repo` over a real git repository. **`pr shadow`** and **`pr run --context repo`** go through `review_bot.pr.cli.main`, and linked issues come from P3-S9's local GitHub stub. **`evals replay`** goes through `review_bot.evals.main`.
- **A pattern case** is one added line in a small diff. The prompt must hold `+` followed by the expected line. **Loose cases** (an address in a `mailto:` URL or a query string, and `jane+billing@…`) check only that the address is gone and that a placeholder of its kind is there. Exactly which neighbouring characters a local part takes isn't pinned.
- **Counts:** a source or kind with 0 may be left out or be 0, except in the spec's own example, which is compared as written: `{"diff": {"email": 1}}`. The `static`/`redaction` audit record holds the counts either as its data or under one key.
- **"The ledger describes the text as returned"** (AC5): spans equal the unmasked run's. Each call's `result_bytes` is the unmasked run's minus the bytes each address saves, and `context.result_bytes` is their sum.
- **"Left as it is"** (Limits): a placeholder already in the text counts nothing.
- **AC9:** a recording is made fresh for a copy of the clean case with `freshness_fields(case.json)`, which is P1-S14's reading. One copy reviews `clean.diff` (no match). The other reviews a two-line diff holding an address (a match). One test also reads `freshness_fields(..., policy=parse_policy(...))`, the developer's existing public signature.
- **Revised AC3:** the expected text keeps every digit group after the longest leading run of 8 to 15 digits, together with its separator: `+61 2 9999 0000 2024-11-05` is sent as `[REDACTED:phone]-11-05`.
- **Revised AC5:** a value straddles the cut when it starts 16 characters before the 256-character title cap or the 8,000-character body cap. Cutting before masking would send `jane.doe@rights…`. A control without a policy shows that the fixture does straddle the cut.
- **Revised AC6:** the path is `people/jane.doe@rightship.com/handlers.py`. The model must see `people/[REDACTED:email]/handlers.py`. The JSON and Markdown reports must name the real path, and an IP on a line of that file must stay out of every local output. A rule finding (P1-S7) names the path, so the check doesn't depend on the model.
- **A secret** for AC10 is assembled at run time by `aws_key` (the P3-S17 rule).

### Criteria

Test names drop the `test_p4_s4_` prefix.

| AC | Tests | Result |
|---|---|---|
| Policy (AC preamble) | `ac_policy_unknown_kind_exits_2_before_the_diff_naming_the_file` ×6 (`name`, `Email`, `ipv6`, one unknown of two, `""`, `1`; exit 2 for an absent diff, naming the file and the kind or `kinds`, with no request). `ac_policy_malformed_redaction_section_exits_2` ×2 (not a list, unknown key). `ac_policy_known_kinds_are_accepted` ×3 (`[]` included) | pass (11) |
| AC1 | `ac1_spec_example_prompt_holds_the_placeholder_not_the_address`. `ac1_only_the_address_changes_in_the_prompt` (line for line against the unmasked prompt). `ac1_finding_on_the_masked_line_keeps_its_fingerprint` (and the whole report but `policy` and `redaction`). `ac1_findings_after_the_masked_line_keep_their_lines`. `ac1_context_and_removed_lines_are_masked_too` ×2. `ac1_address_in_a_hunk_headers_section_text_is_masked`. `ac1_address_on_a_crlf_line_is_masked_and_the_cr_kept` | pass (8) |
| AC2, AC3, AC4 | `ac2_ac3_ac4_value_is_masked_in_place_by_kind` ×48. E-mail: AC1's line; a whole line; at line start and end; in `<>` and `()`; before `.`, `,` and `;`; in quotes; capitals; a subdomain; two on a line; in `mailto:` and query URLs; `+tag`; the look-alikes `notexample.com`, `example.com.au`, `examples.com`, `test.com` and `invalid.org`. Phone: AC3's three forms; at line start; mid-sentence; before `,`; in `()`; `tel:`; 8 and 15 digits; two on a line. Revised AC3 (groups past 15 digits; the longest leading run of 8 to 15 is masked): `phone-ac3-past-15-space-group` (`+12025550143 12345`), `phone-ac3-past-15-with-a-date` (`+61 2 9999 0000 2024-11-05`), `phone-ac3-past-15-hyphen-group` (`+61299990000-20241105`), `phone-past-15-exactly-15-first`, `phone-past-15-mid-sentence`. IPv4: AC4's two; a whole line; in a URL with a port; before `;`; in `()`; `255.255.255.255`; the neighbours of each documentation range (`192.0.3.5`, `198.51.101.7`, `203.0.114.9`); two on a line. `ac2_ac3_ac4_value_is_sent_as_written` ×33. Exempt: AC2's four; `example.net`, `host.example`, `docs.example.com`. Not e-mail: `@property`, `@app.route`, `@rightship.com`, `jane@`, `jane @ rightship.com`, `a @ b`, `@@@`. Not phone: AC3's three; 20 and 7 digits; `a + 12`; double spaces; `i++`. Not IPv4: AC4's six; both ends of 192.0.2.0/24; octets 256 and 300; `1.2.3`; `1.25`. Also `ac3_phone_at_the_start_of_the_added_line_follows_the_diffs_plus` (`++1202…`), `ac2_ac3_ac4_only_the_policys_kinds_are_masked` ×3, `ac2_ac3_ac4_all_three_kinds_on_one_line` | pass (86) |
| AC5 | `ac5_read_returns_the_file_masked` (a documentation address is kept). `ac5_grep_returns_matching_lines_masked` (including a grep for the address itself). `ac5_ledger_spans_and_bytes_describe_the_masked_result`. `ac5_read_lines_keep_their_numbers`. `ac5_report_counts_the_tool_source`. `ac5_linked_issue_title_and_body_are_masked_before_sending` (e-mail and phone, under `pr run --context repo`). Revised: `ac5_issue_is_masked_before_it_is_cut_so_no_part_of_a_value_is_sent` ×2 (title, body: no `jane`, `jane.doe` or `rightship` in the prompt, and one `issues` e-mail counted). Controls: `ac5_control_issue_cut_without_a_policy_sends_part_of_the_address`, `ac5_without_a_policy_the_tool_result_is_unchanged`, `ac5_without_a_policy_the_issue_is_sent_as_written` | pass (11) |
| AC6 | `ac6_spec_example_counts_by_source_and_kind`. `ac6_report_redaction_is_appended_after_policy`. `ac6_counts_every_kind`. `ac6_one_static_redaction_audit_record_with_the_same_counts`. `ac6_counts_add_up_across_diff_tools_and_issues` (one PR: `diff`, `tools` and `issues`, each with 2, in the report and the record). `ac6_no_masked_value_in_stdout_stderr_or_the_audit_log` ×3 (json, text, markdown, with findings anchored on the masked lines). `ac6_pr_shadow_writes_no_masked_value` (stdout, stderr, `shadow.json`, `shadow.md`, `audit.jsonl`). `ac6_context_review_writes_no_value_from_a_tool_result`. `ac6_counts_only_never_the_values_in_the_audit_record`. Revised (a path is exempt in local outputs only): `ac6_path_holding_an_address_is_masked_in_what_the_model_sees`, `ac6_local_outputs_keep_the_real_path_but_no_other_value` ×2 (json, markdown), `ac6_report_findings_name_the_real_path` | pass (15) |
| AC7 | `ac7_builtin_report_redaction_is_empty`. `ac7_builtin_sample_prompts_are_phase_3s_goldens` (P3-S1's prompt goldens). `ac7_builtin_sends_personal_data_as_written`. `ac7_policy_without_kinds_sends_the_same_bytes_as_builtin` ×2 (no section, `kinds = []`). `ac7_builtin_context_review_sends_the_same_bytes_as_no_kinds`. `ac7_policy_with_kinds_and_no_match_sends_the_same_bytes` | pass (7) |
| AC8 | `ac8_context_suggestion_with_a_placeholder_is_dropped_redacted` ×4 (email, ipv4, phone, a bare `[REDACTED:`). `ac8_verify_record_carries_redacted`. `ac8_diff_mode_keeps_not_requested`. `ac8_control_suggestion_without_a_placeholder_is_kept`. `interfaces_suggestion_drop_reason_redacted`, `interfaces_redaction_kind_values` | pass (9) |
| AC9 | `ac9_case_with_no_match_stays_fresh_under_the_policy`. `ac9_committed_sample_case_stays_fresh_under_the_policy` (same output as without the policy). `ac9_case_with_a_match_is_stale_naming_prompt_sha256` (exit 1; not `case_sha256` or `diff_sha256`). `ac9_a_kind_the_case_doesnt_hold_keeps_it_fresh`. `ac9_freshness_under_the_policy_is_the_masked_prompts_hash` | pass (5) |
| AC10 | `ac10_secret_in_the_diff_exits_16_whatever_the_policy` ×3 (built-in, email, all kinds; no request; the key in no output). `ac10_secret_next_to_an_address_is_not_masked_away` | pass (4) |
| Limits | `limits_hostile_line_is_masked_in_bounded_time` ×10, through `mask_personal_data` with all kinds, on 400,000 characters each: `@`, `+`, `.`, `a.`, `a@`, `1.`, `+1 `, `+1-1-…`, a long local part with no domain, and a long domain with no TLD. `limits_hostile_diff_line_is_reviewed_in_bounded_time` ×3 (`@`, `+`, `.` as a diff line through `review-bot`). `limits_text_already_masked_is_left_as_it_is` ×3. Each is bounded at 60 s. The slowest took 0.64 s, which is the whole CLI run | pass (16) |

### Failures

None.

### Earlier suites changed

Each keeps its intent. S4 makes `[redaction]` valid and appends a report key.

- `test_p4_s1_policy_file.py`: the `a-later-slices-section` case, which required `[redaction]` to be refused, is removed. `ac1_report_policy_is_appended_after_phase_3s_keys` now checks that `mode`, `context` and `policy` are consecutive, rather than that they are the last three keys.
- `test_p1_s10_json_output.py`: `REPORT_KEYS` gains `redaction`, and the append-order check now ends with `redaction`.
- `sample_report.golden.json` gains `"redaction": {}`.

### Coverage

From this file alone: `governance/redaction.py` 100%. The modules it touches are covered in part from here, as expected: `report.py` 91%, `review.py` 87%, `context/issues.py` 83%, `render.py` 79%, `governance/policy.py` 72%, `suggestion.py` 70%, `context/tools.py` 69%, `evals/replay.py` 65%. The unit tests and the earlier suites cover the rest. The 90% gate is the main session's full run.

### Gaps

- **AC8 under the built-in policy** (a model that writes `[REDACTED:` with nothing masked) isn't pinned. The spec doesn't say whether the drop depends on the policy.
- **A model finding that names the masked path** (`people/[REDACTED:email]/…`, as a real model would) isn't tested. The spec doesn't say whether it maps back to the real path. The path tests use a rule finding and a finding on the real path.
- **Groups past 15 digits with no leading run of 8 to 15** (such as `+1 234567890123456`) aren't tested. The revised AC3 masks only such a run, and here there is none.
- **Where a tool's byte cap applies** (before or after masking) isn't tested. AC5 pins only that the ledger describes the returned text.
- **Unicode digits and look-alike characters** (such as Arabic-Indic digits after `+`, or a full-width `@`) aren't tested. The catalogue doesn't say whether they count.
- **`evals live` under a redaction policy** isn't tested. AC9 names `replay` only.

## P4-S5: See which findings would block a merge, per approved class

Spec: [p4-s5-gate-decision.md](slices/p4-s5-gate-decision.md). Tests: [`tests/integration/test_p4_s5_gate_decision.py`](../../../tests/integration/test_p4_s5_gate_decision.py), 159 cases. Goldens: [`tests/fixtures/p4_s5/phase_3_story_*.json`](../../../tests/fixtures/p4_s5/), three of them. The tests were written from the revised spec's criteria, Limits and Interfaces, design.md (Merge gating, Summary marker, Blocker fingerprints) and ADR 0019, while the developer built the slice and before any of its code was read. Only the fake's new method names were taken from their code.

**Status: every criterion passes, as revised after review round 1 (AC9, AC12, Limits). All 159 cases pass on the developer's working tree** (base `69ff5bb`, their round-1 fix uncommitted). Before `runner.py` was wired, AC1 failed (`gate` was always `not_enabled`), so the cases do detect a missing gate.

**Review round 1:** the reviewer found two laundering paths, and a case was added for each. Both fail on `69ff5bb` (the slice as first built, run from a temporary worktree) and pass after the developer's fix:

- AC8: a withheld blocker on a line whose earlier comment the bot had resolved was dropped at the next README push, ending `unjudged` with no blocker.
- AC9: a line appended under the summary's marker made the bot write a fresh summary, losing a withheld-only blocker without a word.

### Gate

Only this file and the suites it touches ran, in small groups: this file, `test_p2_s1_shadow_report_local.py`, `test_p3_s8_pr_review_with_context.py`, `test_p2_s5_comment_sync.py`, `test_p2_s7_review_new_commits.py` and `test_p2_s13_shadow_backfill.py`. All pass: 159, 104 and 150 cases. The two earlier suites ran before round 1, and its fix touches neither. `ruff format --check` and `ruff check` are clean on `tests/integration`. `scan_secrets` reports nothing in the new file, which a test also checks. For the full gate, see the main session's run.

### Seams and readings (assumed)

- **Runs** go through `review_bot.pr.cli.main(["run", ...], provider=MockProvider, github=FakeGitHubClient)`, as P2-S5 and P2-S7 drive them, over a real git repository whose base commit holds `.review-bot.toml`. The policy comes from `REVIEW_BOT_POLICY`. `missing_commit` goes through `pr backfill` (P2-S13's `Backfiller`).
- **People** are played through the fake's `edit_summary(body, by=)`, `edit_comment(id, body, by=)`, `delete_summary()` and `delete_comment(id)` (the `People` class, the one seam). Since round 1, the fake keeps a bot issue comment that stopped being the summary in `other_bot_comments`, as GitHub would. A test that sets `FakeGitHubClient.summary` directly leaves the bot as its last editor, so a malformed marker there is one the bot "wrote".
- **The marker** is read under design.md's grammar, written out independently (`GATE_MARKER`), and every gating summary's last line must match it.
- **AC2's goldens** were recorded on this branch at `3c5b63d`, before any of the slice's code was in `src/`. For one PR's nine runs, they record what Phase 3 sends to GitHub: each call's name, the bodies of its writes, the exit code and the summary left behind. The runs are: the money line plus an AI finding, README only, a re-run, the fix, a secret, the secret removed, a draft, a provider failure, and an oversized diff. Commit dates are fixed and SHAs normalised. They come in three configs (no file, no `gate` key, invalid), and the first two also run under a policy that approves classes. They must never be re-recorded from new code.
- **Fingerprints:** a rule blocker's is P1-S9's encoding. A line-secret blocker's is `expected_fingerprint(path, "possible-secret", line, 0)` (design: "built as a finding's is"). A path-secret blocker's is only checked to be 16 hex.
- **"Names a blocker by file, line and class"**: the path with `:<line>` and the class appear on one line of the summary's visible text.
- **AC3 "the reason"**: the message holds `approve`, `domain_rules` or `below`/`critical` (the reason), the class and the policy version. It needn't name the entry's own severity.
- **`missing_commit` in `pr run`**: a head missing from the clone is a git error (exit 19, no report), as in Phase 3; `missing_commit` is only `pr backfill`'s outcome. AC11's "neither create nor edit" is therefore checked for `pr run` on `invalid_config` and on that git error. `missing_commit`'s `gate` is checked through backfill.
- **Secrets** are assembled at run time by `aws_key` (the P3-S17 rule).

### Criteria

Test names drop the `test_p4_s5_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_shadow_gate_is_block_with_the_money_finding` (the exact `gate` object). `ac1_blocker_fingerprint_is_the_rule_findings`. `ac1_shadow_md_summary_says_would_block` (`shadow.md` and `summary_body`). `ac1_shadow_run_uses_no_client_and_writes_no_summary` | pass (4) |
| AC2 | `ac2_gating_off_makes_phase_3s_github_calls_and_summaries_byte_for_byte` ×3 (no file, no `gate`, invalid config, against the Phase 3 goldens). `ac2_a_policy_approving_classes_changes_nothing_without_a_gate_key` ×2. `ac2_gate_is_not_enabled` ×4 (shadow and comment). `ac2_summary_marker_is_phase_3s_with_gating_off`. `ac2_gating_off_still_finds_a_summary_written_with_gating_on` (a `gate=` marker is still read for `reviewed=`) | pass (11) |
| AC3 | `ac3_unbacked_gate_class_is_invalid_config_exit_2_without_a_model_call` ×12, in shadow and comment mode: the built-in policy; another class approved; no `[[domain_rules]]`; an entry at `medium`; a default `high` entry under a `critical` class; the class's default `critical`. `ac3_domain_rule_at_the_class_severity_is_valid`. `ac3_unknown_gate_class_name_is_invalid` | pass (14) |
| AC4 | `ac4_ai_security_class_counts_only_critical_at_090` ×4 (critical 0.95 blocks; `high`, 0.85 and category `logic` don't). `ac4_model_finding_tagged_with_a_rule_blocks_under_no_class` (both classes enabled). `ac4_tagged_model_finding_in_billing_doesnt_add_a_second_blocker`. `ac4_threshold_critical_hides_and_unblocks_the_high_rule_finding`. `ac4_threshold_from_the_variable_unblocks_too` | pass (8) |
| AC5 | `ac5_unrelated_pushes_keep_blocking_and_the_fix_clears`: push 1 blocks, and the marker's fingerprint is the inline comment's. A README push and an unrelated code push (the money line isn't sent to the model) still block. The same-line fix passes, leaving no `gate=`. `ac5_a_fix_below_the_anchor_elsewhere_in_the_file_clears`. `ac5_after_the_twin_above_goes_the_blocker_keeps_its_comments_fingerprint` (the ordinal-1 comment's fingerprint, not the recomputed ordinal-0 one, which equals the resolved twin's) | pass (3) |
| AC6 | `ac6_secret_line_blocks_every_run_until_removed` (`blocked_secret`, then README and code pushes, then removed → `pass`; the exact blocker object). `ac6_secret_blocks_in_shadow_mode_too`. `ac6_two_secret_lines_are_two_blockers`. `ac6_no_value_appears_anywhere` (output files, every GitHub call's arguments, the summary, stderr; nor the value's tail). `ac6_secret_in_a_new_path_blocks_once_with_no_file_or_line` (two line secrets folded, "a file path holding a possible secret", no value or path anywhere, the audit fingerprints). `ac6_path_secret_and_a_line_secret_elsewhere_are_two_blockers`. Not blocking: `..._only_on_a_removed_line_...`, `..._only_on_a_context_line_...`, `..._only_in_an_old_path_...` (a pure rename), `ac6_secret_added_then_removed_inside_the_pr_doesnt_block` | pass (10) |
| AC7 | `ac7_summary_lists_the_blocker_and_says_how_to_appeal_and_override` ("Blocks the merge", `billing/prices.py:3` and the class on one line, a reply to appeal, `/review-bot override:`, the marker entry). `ac7_secret_blocker_shows_its_fingerprint_and_the_review_override`. `ac7_fingerprints_and_paths_are_inert_on_github` (no 16-hex or other reference trigger outside code). `ac7_a_summary_with_no_blocker_doesnt_say_it_blocks` | pass (4) |
| AC8 | A fixture whose push 1 has three `ai:logic` blockers, under `max_open_comments = 2`: A and C commented, B withheld `over_limit`. `ac8_open_withheld_and_deleted_ai_blockers_all_carry` (C's comment deleted). `ac8_withheld_and_deleted_are_listed_as_carried_with_no_comment` (by fingerprint). `ac8_a_person_resolving_the_thread_doesnt_clear_it`. `ac8_the_bot_resolving_it_because_its_line_went_drops_it`. `ac8_carried_blockers_survive_many_pushes`. `ac8_a_carried_blocker_whose_class_is_no_longer_enabled_isnt_carried`. Whole-diff checks unable to run: `ac8_oversized_push_fixing_the_money_line_carries_every_entry` and `ac8_unreadable_push_fixing_the_money_line_carries_every_entry` (Latin-1). In both, the rule entry carries although fixed, `reviewed=` is kept and only `write_summary` is written. `ac8_once_the_diff_is_readable_again_the_fixed_rule_blocker_drops`. Round 1: `ac8_withheld_blocker_on_a_line_whose_old_comment_the_bot_resolved_still_carries`. Push 1 flags an `ai:security` line, commented with fingerprint F. Push 2 removes the line, and the bot resolves the comment. Push 3 re-adds the identical line with a critical finding that takes the only open-comment slot, so F is withheld `over_limit`. Push 4 changes only the README, and F must still block | pass (10) |
| AC9 | `ac9_person_edited_summary_reviews_whole_diff_and_rebuilds` (`scope` `full`, the whole diff sent, A and C rebuilt, B lost, no ` tampered`, the summary mentions the tamper). `ac9_edited_summary_rewritten_to_clear_the_gate_still_rebuilds` (`reviewed=<head>` with no gate is ignored, so not `already_reviewed`). `ac9_rebuild_skips_bot_resolved_low_and_disabled_class_comments` (rebuilt: open and person-resolved; not rebuilt: 0.85, `medium` and a disabled class). `ac9_malformed_marker_reads_nothing_and_rebuilds` ×14: no marker; uppercase hex; an unknown class; an empty `gate=`; `more=` without 25 entries; `more=0`; `more=05`; a 7-digit `more=`; 26 entries; ` tampered` twice; fields out of order; a double space; a trailing comma; an unknown field. `ac9_marker_over_2000_characters_is_malformed`. `ac9_deleted_summary_with_bot_comments_reviews_whole_diff_and_rebuilds`. `ac9_deleted_summary_without_bot_comments_is_a_fresh_review_with_no_tamper` (the Limits gap: a withheld-only blocker is lost silently). `ac9_rebuild_cant_finish_without_the_whole_diff_checks` (`block`/`tampered`, ` tampered` written). `ac9_tampered_marker_makes_the_next_run_try_again_and_succeed`. `ac9_a_bot_written_tampered_marker_is_untrusted`. Round 1 (a bot issue comment last edited by someone else): `ac9_a_line_appended_under_the_marker_is_a_tamper_not_a_fresh_summary`. The PR's only `ai:` blocker is withheld, and a person appends a line under the marker. The run reviews the whole diff and is `tampered` or says a tamper occurred, and so is the next run, while the edited comment stands. Also `ac9_a_line_appended_under_the_marker_with_inline_comments_rebuilds` (A and C rebuilt) | pass (25) |
| AC10 | `ac10_over_limit_blocker_blocks_and_is_named_by_file_and_line`. `ac10_possible_secret_withheld_blocker_still_blocks` (no value written anywhere). `ac10_withheld_blocker_in_shadow_mode_would_block` | pass (3) |
| AC11 | `reviewed` → `pass`, `reviewed=` advances. `no_new_changes` decides and advances. `already_reviewed` is decided again, writing the summary with its headline kept and the same entries. A provider failure is `block` with a blocker (keeping `reviewed=`) and `unjudged` with none (`reviewed=none`, never `pass`), and still finds a new rule blocker. Skips `nothing_to_review` and `empty` are `unjudged` and write the summary. `too_large` with no summary and no bot comments is `unjudged` (Limits, as revised). `blocked_secret` is `block` with a money blocker and `unjudged` without one. `refused_run_is_unjudged_no_config` ×3 (draft, fork, action). `backfill_missing_commit_is_unjudged_no_config`. `no_credential_run_is_unjudged_no_config`. Never writing: with `REVIEW_BOT_GATE=on` nothing is created ×2 and a deleted summary isn't recreated ×2; a `gate=` marker alone stops an edit ×2; `more=` or ` tampered` alone stops it ×2. `without_gate_signals_a_no_config_run_writes_as_phase_3` | pass (25) |
| Interfaces | `interfaces_invalid_review_bot_gate_is_exit_2_every_run` ×10 (`yes`, `ON`, `1`, `""` and `true`, in shadow and comment mode; no model call, naming the variable). `interfaces_valid_review_bot_gate_values_run` ×2 (`on`/`off` don't change the decision) | pass (12) |
| AC12 (any run that reaches a decision) | `ac12_audit_records_the_gate_with_every_fingerprint` (exact keys). `ac12_a_run_ending_on_a_git_error_writes_no_report_or_gate_record` (exit 19: no `shadow.json`, no `policy`/`gate` record). `ac12_gating_off_has_a_not_enabled_record` (the exact record, `builtin-v1`). `ac12_refused_run_has_a_no_config_record`. `ac12_record_holds_no_diff_text_or_path_secret` | pass (5) |
| AC13 | `ac13_class_whose_rule_cant_reach_high_is_an_invalid_policy` ×4 (`swallowed-exception` names `medium`, `open-without-encoding` names `low`, at `high` and `critical`). Also `policy_class_outside_the_allowed_range_is_invalid` ×8 (`medium`, 0.4, 1.1, an unknown rule, an unknown category, an unknown key, no `approved_by`, a class twice). `policy_class_defaults_are_critical_and_090` | pass (13) |
| AC14 | `ac14_edited_inline_comment_blocks_tampered_and_keeps_carried` (A, B and C kept, ` tampered` written). `ac14_edit_lowering_the_shown_severity_doesnt_launder_it`. `ac14_the_next_run_takes_the_rebuild_path_and_stays_tampered`. `ac14_edited_comment_of_a_rule_blocker_is_tampered_too` | pass (4) |
| Limits | `limits_more_than_25_blockers_lists_25_ai_first_and_counts_the_rest` (28 money + 2 AI: 30 in `shadow/v1` and the audit log, 25 in the marker, AI first, `more=5`, the total stated). `limits_most_severe_first_among_the_listed`. `limits_ai_blockers_beyond_25_arent_carried_and_the_summary_says_how_many`. `limits_a_thousand_blockers_stay_under_60000_and_run_in_bounded_time` (paths over 100 characters; the marker line ≤ 2,000; `more=975`). `limits_whole_diff_checks_are_linear_on_a_large_diff` (20,000 money lines). `limits_huge_hostile_summary_is_read_quickly_as_malformed` (1 MB of text, then a 5.4 MB marker line). Each is bounded at 60 s. The slowest took 3.9 s, which is the whole CLI run | pass (6) |

### Failures

None.

### Earlier suites changed

Each keeps its intent. S5 appends `gate` to `shadow/v1`.

- `test_p2_s1_shadow_report_local.py`: `SHADOW_KEYS` gains `gate`, after `policy`.
- `test_p3_s8_pr_review_with_context.py`: `assert_phase_2_run` now requires `gate` to be AC2's `not_enabled` object (`GATE_NOT_ENABLED`), rather than null or empty. It treats `gate`, like `policy`, as never empty.

### Coverage

From this file alone: `pr/config.py` 86%, `pr/fake_github.py` 76%, `governance/policy.py` 73%, `governance/gate.py` 61%, `pr/carried.py` 55%, `pr/rest_github.py` 42% (no live GitHub in tests). Error branches of the pure modules are covered by the developer's unit tests. The 90% gate is the main session's full run.

### Gaps

- **`line` null when a hunk header has no readable start** (AC6) isn't tested end to end: git never writes such a header, so `pr run` can't produce one. The developer's unit tests are where it can be pinned.
- **`not_a_diff` skip** (AC11) isn't reachable through `pr run` over a git repository, for the same reason. `nothing_to_review`, `empty`, `too_large` and `unreadable` are tested.
- **`degraded`** (AC11) arrives with S8 and isn't tested here.
- **The REST client's last-editor reads** (`list_bot_issue_comments` and `read_inline_comments`) are tested only through the fake. The developer notes they are to be confirmed live in S10.
- **The exact wording** of the summary's gate section is pinned only by the spec's own phrases: "Would block the merge: 1 finding (decimal-money)", "Blocks the merge", "a file path holding a possible secret", "carried from an earlier review, no comment", `/review-bot override`. The rest isn't.
