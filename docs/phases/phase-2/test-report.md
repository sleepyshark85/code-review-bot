# Phase 2 test report

## P2-S1: Get a shadow report of what the bot would post, for a local diff

Spec: [p2-s1-shadow-report-local.md](slices/p2-s1-shadow-report-local.md). Tests: [`tests/integration/test_p2_s1_shadow_report_local.py`](../../../tests/integration/test_p2_s1_shadow_report_local.py), 47 cases. They were written from the spec and design.md (Interfaces, Comment state, Safety) before `src/review_bot/pr/` existed. Every run calls `review_bot.pr.cli.main(["shadow", <diff>, "--out", <dir>, "--provider", "mock", ...], provider=MockProvider(...))` in process, the seam the developer confirmed.

**Status: every criterion passes: 47 passed, from this file alone, on the developer's uncommitted working tree** (base `1c510ef`).

**Before the code:** 4 passed, 41 failed, 2 errors. Most were `ModuleNotFoundError: review_bot.pr`. The exception was AC11's `review-bot --format markdown` half, where 6 cases failed on issue #37. **First run on the developer's code:** 36 passed, 11 failed. All 11 were two errors in the tests, both checked against the docs before anything changed (Failures, below).

### Gate

Only this file was run, as the coordinator asked: `ruff format --check` and `ruff check` are clean on it. For the full gate, see the main session's run.

### How the output is read

- **`shadow.json`** must be pure ASCII, one line (at most one trailing `\n`), and strict JSON. Its keys must be exactly design.md's `shadow/v1` list, **in design.md's order**. Each `comments` and `withheld` entry must hold exactly its listed keys.
- **Expected fingerprints and anchors** are computed independently: fingerprints by P1-S9's `expected_fingerprint`, with rule part `ai:logic` for the untagged AI finding. Anchors are the first 16 hex digits of the SHA-256 of the whitespace-collapsed anchored line (ADR 0003).
- **Bodies and the summary** are parsed by `markdown-it-py` with linkify, as in P1-S11. Visible text is compared exactly, after the developer's layout was confirmed against the spec:
  - **A body** is three paragraphs then the HTML marker block. The first paragraph is `high logic (source ai, confidence 0.80)`, or `medium reliability (source rule, rule swallowed-exception, confidence 1.00)` for a rule finding. Then come the rationale, the fixed reaction sentence, and the marker.
  - **The summary** starts `Review bot: reviewed. N comments, M withheld.`, then `Withheld findings:`, then one `path:line: reason` list item each, ending with `and K more`. A clean run's summary is exactly `Review bot: reviewed. No findings at or above low.`
- **AC11** uses the parser for links and HTML: no `link_open`, and exactly one HTML token in a body, which must be the real marker on the last line. GitHub's references and mentions aren't CommonMark, so the spec's structural guarantee is also checked directly. No `@`, `://`, `www.`, `#<digit>`, `GH-<digit>`, `<!--` or 7-to-40-hex word may be left in text outside code, and each word of the value holding one must be inside a code span. A self-check shows the direct check fails a raw `\#12`.

### Criteria

Test names drop the `test_p2_s1_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_sample_with_one_ai_finding_is_a_local_reviewed_shadow_line_and_exits_0`. `ac1_report_is_review_bots_json_document_for_the_same_run`, compared without `run_id`. `ac1_three_create_comments_with_their_findings_fields_in_severity_order`: AI line 14 (high), rule 35, rule 39, exact fingerprint, rule part and anchor. Interfaces: `interfaces_shadow_md_is_the_summary_then_each_comment` | pass (4) |
| AC2 | `ac2_body_is_the_finding_then_its_rationale_then_the_reaction_then_the_marker` ×3 (AI 14, rule 35, rule 39). `ac2_last_line_is_exactly_the_marker_with_the_adr_0003_anchor` | pass (4) |
| AC3 | `ac3_no_findings_has_no_comments_and_the_clean_sentence` | pass |
| AC4 | `ac4_aws_key_blocks_the_review_without_a_call_or_the_value`: names `aws-access-key` and diff line 6. The key is in no file under `DIR`, nor on stdout or stderr | pass |
| AC5 | `ac5_unreviewable_diff_is_skipped_with_its_reason_and_no_call` ×4 (`too_large`, `empty`, `not_a_diff`, `unreadable`). `ac5_whitespace_only_diff_is_empty`. `ac5_missing_diff_exits_3_with_no_report` | pass (6) |
| AC6 | `ac6_auth_failure_is_outcome_failed_reason_auth_exit_8` | pass |
| AC7 | `ac7_first_10_in_order_are_created`: 12 findings over two files with mixed severities, so the file and line tie-breaks are exercised. `ac7_last_2_are_withheld_over_limit_and_listed_by_file_and_line`: exact header and list items | pass (2) |
| AC8 | `ac8_thousand_findings_list_20_withheld_then_the_rest_as_a_count`: 10 created, 990 withheld. The list is exactly the first 20 withheld then `and 970 more`, at most 60,000 characters, in under 60 s. Limits: `ac8_limits_long_paths_still_keep_the_summary_under_60000`, with 25 files on 3,500-character paths, so 20 entries listed in full would exceed the cap | pass (2) |
| AC9 | `ac9_precondition_scan_secrets_detects_the_token`. `ac9_token_in_a_rationale_is_withheld_possible_secret_and_written_nowhere`. `ac9_token_is_not_in_the_default_audit_log_under_dir`: with `REVIEW_BOT_AUDIT_LOG` unset, the log lands in `DIR/audit.jsonl`, and the token still appears in no file under `DIR` | pass (3) |
| AC10 | `ac10_rule_finding_on_a_non_utf8_path_is_withheld_and_listed_quoted`: `"caf\377.py"` line 3 | pass |
| AC11 | `ac11_rationale_token_is_inert_in_the_comment_body` ×8. `ac11_rationale_token_is_inert_in_review_bot_markdown` ×8 (fixes issue #37). `ac11_check_catches_a_raw_reference` | pass (17) |
| AC12 | `ac12_rationale_over_2000_is_cut_in_the_body_and_whole_in_the_report` | pass |
| AC13 | `ac13_missing_out_directory_is_created`. `ac13_unwritable_out_exits_2_naming_out_before_reading_the_diff` ×2 (`--out` is a file, or under one). The diff is missing, so reading it first would give 3 | pass (3) |
| AC14 | `ac14_same_run_twice_differs_only_in_run_id_and_timestamps`: also checks that every audit record's `run_id` is the document's | pass |

### Failures

None now. Two errors in the tests, found on the first run against the code, are kept here as the record:

1. **The rule part was wrong in the tests.** They expected `rule=logic` for an untagged AI finding. Phase 1 design.md ("Fingerprints and dedup") and ADR 0003's details define it as `ai:<category>`, so `ai:logic` is right. This one error accounted for 10 failures: AC1's comments test, AC9, and the eight AC11 body cases.
2. **AC1's reference run used a different provider setting.** The reference run used `--provider mock` and the shadow run didn't, so `report.provider` differed. The shadow runs now pass `--provider mock` too, which is what "the same run" means.

Issue #37 (AC11, `review-bot --format markdown`) failed before the code and passes now.

### Readings for the designer

1. **Settled in the spec's Notes (changed in build): AC9 wins over AC1.** Every string in the embedded `report` is redacted, so AC1's equality is tested only on a run without secrets.
2. **The audit log** goes to `DIR/audit.jsonl` unless `REVIEW_BOT_AUDIT_LOG` is set (a build choice in the spec's Notes). AC14 tests the variable. AC9 tests the default, because a log under `DIR` falls under "nowhere in DIR".
3. **AC14's "apart from `run_id`"** is read as the top-level `run_id` and `report.run_id`.
4. **AC11's forged marker** is a multi-word token. Its trigger words (`<!--` and `fingerprint=0000000000000000`, whose 16 zeros are a hex word) must be in code spans; `review-bot:v1` and `-->` may be plain text.
5. **The exact-layout checks** (body paragraphs, summary header and list items) pin wording the spec doesn't fix. A wording change needs these tests changed too, which keeps that change visible in review.
6. **Exact keys.** `shadow/v1`'s key set and order are checked exactly. S5, S7 and S9 add keys, and will need to extend `SHADOW_KEYS`.
7. **Not tested:**
   - A permission-denied `--out` directory, which can't be set up portably on Windows or under root in CI. AC13 uses a path that is a file, or under one.
   - `nothing_to_review` (every file skipped), which is a build choice rather than one of AC5's four reasons.

### Coverage

Not measured by the tester, who ran only this file. For coverage of `review_bot.pr` and `render`, see the main session's `pytest --cov`. Every criterion has at least one passing test.

## P2-S2: Review a pull request from its event, within a per-run budget

Spec: [p2-s2-review-pr-event.md](slices/p2-s2-review-pr-event.md). Tests: [`tests/integration/test_p2_s2_review_pr_event.py`](../../../tests/integration/test_p2_s2_review_pr_event.py), 71 cases. They were written from the spec, design.md (Interfaces, Safety, CI settings and caps) and ADRs 0008 and 0012, before the developer's code was read.

**Status: every criterion passes. All 71 cases pass, from this file alone, on the developer's uncommitted working tree** (base `8e9d8f6`).

**Before the code:** 10 passed and 57 failed, all on `invalid choice: 'run'`. **First run on the developer's code:** 64 passed and 3 failed. All 3 were an error in the tests (Failures, below). The tests were then tightened to the developer's exact outputs, where the spec leaves the form open; the readings below list each one.

### Seam and set-up

- **Every run** calls `review_bot.pr.cli.main(["run", "--event", PATH, "--repo", DIR, "--out", OUT, "--provider", "mock", ...], provider=...)` in process. This is the S1 seam with S2's subcommand. AC5 passes `--provider claude` with the scripted provider injected, and one case injects nothing.
- **Each PR is a real git repository** in `tmp_path`: a base commit on `main`, a head commit on `feature`, and `main` checked out, as in CI's base checkout. Each event file is a hand-written `pull_request` payload with GitHub's field names: `action`, `number`, `pull_request.{number,title,body,draft,updated_at}`, `pull_request.{base,head}.{sha,repo.full_name}` and `repository.full_name`.
- **"No git call"** (AC3, AC4, AC10) is observed by passing an empty directory that isn't a repository as `--repo`. A run that reached git there couldn't end `skipped` with exit 0, and couldn't name the event's field.
- **`CI` is removed** for every test, since GitHub Actions sets it. Only AC5's cases set it.
- **AC9's usage** is scripted with `MockProvider(usage=TokenUsage(...))`.

### Criteria

Test names drop the `test_p2_s2_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_opened_same_repo_pr_is_an_event_shadow_run_with_the_events_pr`: `mode`, `origin`, `outcome`, exact `pr`, `event_at` exactly `2026-11-08T10:11:12+00:00` for `updated_at` `…Z`, one call. `ac1_finding_is_anchored_on_the_heads_line`: head line 2 is `new_two = 2` (base line 2 is `beta = 2`), and the comment carries that line's anchor | pass (2) |
| AC2 | `ac2_only_the_prs_own_changes_are_reviewed_after_the_base_moves`: the event's base is the moved `main`. The prompt holds the PR's change, but not `main`'s later change or its file | pass |
| AC3 | `ac3_reviewable_action_is_reviewed_as_opened` ×3. `ac3_other_action_is_skipped_action_without_git_or_provider` ×3 (`closed`, `edited`, `labeled`). `ac3_ac4_refusal_is_recorded_in_the_audit_log_under_out[action]`: one `run.refused` record, `{"reason": "action"}` | pass (7) |
| AC4 | `ac4_fork_is_skipped_fork_without_git_or_provider` ×2 (another head repository, a null one). `ac3_ac4_refusal_is_recorded_in_the_audit_log_under_out[fork]` | pass (3) |
| AC5 | `ac5_ci_claude_without_a_key_is_skipped_no_credential` ×2 (unset, empty), with a provider injected. `ac5_ci_claude_without_a_key_builds_no_provider`: nothing injected, so a built Claude provider that ran would hit the suite's SDK guard. `ac5_ci_claude_with_a_key_goes_ahead` (control). `ac5_outside_ci_claude_without_a_key_goes_ahead` | pass (5) |
| AC6 | `ac6_precondition_diff_sizes`, measured with git: about 300,000 bytes in all with about 50,000 of Python, and about 130,000 for the second case. `ac6_large_diff_of_mostly_docs_is_reviewed_under_the_cap`. `ac6_reviewable_bytes_over_the_cap_are_skipped_too_large_without_a_call`. `ac6_review_bot_exits_6_over_the_reviewable_cap` | pass (4) |
| AC7 | `ac7_output_token_cap_is_passed_to_the_claude_cli`: over the fake SDK, `options.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16000"`. `ac7_both_caps_unset_leave_the_claude_env_as_before`: P0-S2's exact env, so the variable is absent. `ac7_both_caps_unset_review_bot_reviews_a_large_diff`. `ac7_invalid_cap_makes_review_bot_exit_2_naming_it` ×6 and `ac7_invalid_cap_makes_run_exit_2_naming_it` ×6 (`0`, `-1`, `abc` for each variable) | pass (15) |
| AC8 | `ac8_caps_list_the_effective_values`: the five keys in order with their values. `ac8_unset_new_caps_are_null_and_the_rest_are_the_defaults`. `ac8_provider_is_called_at_most_once` ×2 (reviewed, transient failure) | pass (4) |
| AC9 | `ac9_output_tokens_over_the_cap_set_over_budget_and_stay_reviewed` ×3 (20,000 and 16,001 are over; exactly 16,000 isn't). `ac9_no_output_cap_is_never_over_budget` | pass (4) |
| AC10 | `ac10_malformed_field_exits_19_with_one_line_naming_it` ×12: no `pull_request`; a base SHA of 39 hex digits or of non-hex; a head SHA of `--output=/tmp/pwned` (option injection) or null; a repo with no slash or three parts; a number of `0`, `-3`, `"7"`, `true` or `1.5`. `ac10_event_that_isnt_json_exits_19_with_one_line` ×3 (broken, empty, binary). `ac10_json_that_isnt_an_object_exits_19_with_one_line`. Each checks one `review-bot: error:` line naming `--event` and the field, and that no report is written | pass (16) |
| AC11 | `ac11_missing_head_sha_exits_19_naming_it`: `Commit <sha> isn't in --repo …`, one line, no report | pass |
| AC12 | `ac12_control_planted_driver_runs_under_plain_git_diff`: plain `git diff` runs the planted external command, and `--no-ext-diff` alone still runs the textconv, so the trap is real. `ac12_diff_driver_isnt_run_and_the_plain_change_is_reviewed`: no marker is written, and the prompt holds git's plain lines and no textconv output | pass (2) |
| AC13 | `ac13_title_and_body_text_is_in_neither_the_report_nor_the_audit_log`: a canary with NUL, an ANSI escape, U+202E and CR LF | pass |
| AC14 | `ac14_audit_log_defaults_to_out_audit_jsonl`: the records hold the report's `run_id` and a `run.pull_request` record. `ac14_key_echoed_by_a_failing_provider_is_in_no_file_under_out`: an auth failure whose diagnostics echo the key, exit 8 | pass (2) |
| AC15 | `ac15_draft_pr_is_reviewed_in_shadow_mode` | pass |
| AC16 | `ac16_only_docs_lock_and_generated_files_are_nothing_to_review`: `README.md`, `uv.lock`, `*.min.js`, `*_pb2.py` | pass |
| Limits | `limits_event_over_10_mb_exits_19`: a valid event padded to 11,000,000 bytes is refused with `is over 10000000 bytes`, in under 60 s. `limits_git_output_past_max_diff_bytes_is_skipped_too_large` | pass (2) |

### Failures

None now. One error in the tests, found on the first run against the code, is kept as the record:

1. **AC6's 130,000-byte fixture was too small.** `python_lines(2_550)` makes 119,981 diff bytes, under the 120,000 cap, so the run was reviewed rather than skipped. The test's own precondition caught it, which accounted for all 3 failures. The fixture now has 2,800 lines, about 131,600 bytes.

### Readings for the designer

1. **AC7's mechanism:** the cap reaches the CLI as `CLAUDE_CODE_MAX_OUTPUT_TOKENS` in `options.env`, the variable the Claude Code CLI reads, and only when the cap is set. The spec says only "passes that cap to the CLI".
2. **AC8's `caps`** are checked exactly, in order: `max_diff_bytes`, `max_review_bytes`, `max_output_tokens`, `max_retries`, `deadline_s`, with `null` for an unset cap. The spec lists the caps but fixes neither the names nor how an unset cap is shown.
3. **AC9's "exceeds"** is strict: exactly the cap isn't over budget, and with no output cap nothing is.
4. **Exit 19's wording** is pinned to the developer's: `The event file --event P: <field> isn't a 40-hex-digit SHA`, `lacks pull_request`, `isn't JSON`, `isn't a JSON object`, `is over 10000000 bytes`, and `Commit <sha> isn't in --repo D`. The repo-name field is still matched loosely (`repo`), since the coordinator didn't give its exact form. A wording change needs these tests changed too, which keeps that change visible in review.
5. **"10 MB"** is read as 10,000,000 bytes, as the developer's message says.
6. **Audit records** `run.pull_request`, and `run.refused` with `{"reason"}`, aren't in the spec. They are checked because the coordinator gave them as the developer's contract, and they're what lets a skipped event be traced.
7. **AC10's number** rejects the JSON string `"7"`, `true` and `1.5`, read as "isn't a positive integer". A bad repo name is set in all three places, so the PR still reads as same-repo and AC4 can't decide it first.
8. **AC14** checks files under `OUT` only, as the spec says, not stderr.
9. **Not tested:**
   - The 60-second git timeout in Limits, which needs a git that hangs. Planting one would mean replacing `git` on `PATH`, which isn't portable to Windows, where `subprocess` finds only `.exe` files.
   - A missing base SHA, which the spec doesn't name.
   - Free text in the event other than the title and body. AC13 covers those two fields, which are where it lives.

### Coverage

Not measured by the tester, who ran only this file. For coverage of `review_bot.pr`, `settings`, `review` and `providers/claude_agent`, see the main session's `pytest --cov`. Every criterion has at least one passing test.
