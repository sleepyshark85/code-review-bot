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

## P2-S3: See a shadow report on every same-repo PR, from an Action that posts nothing

Spec: [p2-s3-shadow-workflow.md](slices/p2-s3-shadow-workflow.md). Tests: [`tests/integration/test_p2_s3_shadow_workflow.py`](../../../tests/integration/test_p2_s3_shadow_workflow.py), 60 cases. They were written from the spec, design.md (Safety on the PR path, CI settings and caps) and ADRs 0008, 0010 and 0012, against the main session's uncommitted `.github/workflows/review.yml` (base `475ce55`).

**Status: AC1 to AC11 pass. All 60 cases pass, from this file alone, on Windows with Git Bash. AC12 is manual and stays pending until the first same-repo PR after merge.**

### How the workflow is read

- **PyYAML** (`safe_load`). YAML 1.1 reads `on:` as the key `True`, so the trigger is looked up under either key, and exactly one must exist.
- **Jobs are found by what they do, not by their ids.** The shadow job is the one whose step runs `review_bot.pr run`. The check job is the one whose step reads `vars.REVIEW_BOT_MODE` into its `env`.
- **Job `if:` expressions are evaluated**, not string-matched. A small evaluator covers the subset of GitHub's language they use: context names, string literals, `==`, `!=`, `&&`, `||`, `!` and parentheses. Like GitHub, it compares strings case-insensitively and reads an unset variable as `''`. An unknown context or a function call fails the test rather than passing silently.
- **Three things are executed.**
  - The check step's script (AC11) and the summary step's script (AC8) run under `bash -e`, as on an Ubuntu runner. Both are skipped with a reason if no POSIX bash is on `PATH`. WSL's `System32\bash.exe` launcher doesn't count.
  - The run step's argv and caps go to `review_bot.pr.cli.main`, with the step's literal `env`, `$GITHUB_EVENT_PATH` pointing at a file that isn't JSON, and `MockProvider`. Exit 19 (the event error) proves that `--mode shadow`, `--out` and every cap got through parsing. A bad flag or cap would exit 2.
- **The required check** is checked as a lasting property rather than "`ci.yml` unchanged", which holds only once and would break on any later legitimate edit: `ci.yml` still has a job named `gate`, and `review.yml` has none.
- **Mutation check (not kept in the suite).** 15 one-line breakages of the workflow were each killed by at least one test:
  - `closed` added with no `if` filter;
  - `persist-credentials: true`;
  - `${{ github.event.pull_request.title }}` in a `run:`;
  - `continue-on-error`;
  - `@v4` instead of a SHA;
  - a check that allows any mode;
  - `needs` removed;
  - `always() &&` on the shadow `if`;
  - 30-day retention;
  - the same-repo test replaced;
  - `--mode comment`;
  - `MAX_RETRIES` 3;
  - a head-SHA checkout;
  - `contents: write`;
  - the key in the job's `env`.

### Criteria

Test names drop the `test_p2_s3_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_only_event_is_pull_request`. `ac1_forbidden_event_names_appear_nowhere` ×2 (`pull_request_target`, `workflow_run`). `ac1_shadow_job_runs_only_for_the_four_actions`: the four types are listed, any extra type must be `closed`, and an extra type requires the shadow `if` to read `github.event.action` | pass (4) |
| AC2 | `ac2_workflow_permissions_are_contents_read_only`. `ac2_shadow_job_sets_exactly_contents_read` | pass (2) |
| AC3 | `ac3_same_repo_pr_runs_when_mode_is_unset_or_shadow` ×2. `ac3_other_mode_runs_no_shadow_job` ×3 (`off`, `comment`, `bogus`). `ac3_fork_pr_runs_no_shadow_job` ×6: another owner, a name that only starts with the repo's, and a deleted fork (null head repo), each with mode unset and `shadow`. `ac3_no_job_is_named_gate` (ids and `name:`). `ac3_ci_yml_still_has_the_gate_job` | pass (13) |
| AC4 | `ac4_base_sha_is_checked_out_with_full_history_and_no_credentials`. `ac4_head_sha_is_only_fetched`: one step reads the head SHA, and its script is only `git fetch … "$VAR"`. `ac4_head_sha_reaches_no_action_input_but_the_artifact_name`. `ac4_no_step_moves_the_work_tree_off_the_base`: no `git checkout`, `switch`, `reset`, `worktree`, `merge`, `pull`, `rebase` or `cherry-pick`. `ac4_dependencies_install_locked_without_dev_after_checkout`: exactly `uv sync --locked --no-dev`, between the checkout and the review | pass (5) |
| AC5 | `ac5_api_key_is_only_in_the_run_steps_env`: the only `secrets.` in the file. `ac5_no_github_token_is_passed_anywhere` ×2 (`GITHUB_TOKEN`, `github.token`). `ac5_no_job_or_workflow_env_carries_values_to_every_step`. `ac5_run_passes_mode_shadow_and_out`. `ac5_ac7_run_command_and_caps_are_accepted_by_the_cli` | pass (6) |
| AC6 | `ac6_no_run_block_interpolates_an_expression` (all jobs). `ac6_env_values_read_only_the_number_and_shas`. `ac6_action_and_head_repo_are_read_only_in_if`. `ac6_no_other_event_field_appears_anywhere`, which also catches a bare `github.event` and bracket access. `ac6_branch_name_context_is_never_read` (`github.head_ref`) | pass (5) |
| AC7 | `ac7_run_step_env_sets_the_design_caps`: the five values in design.md's table. `ac7_shadow_job_times_out_after_10_minutes`. `ac7_concurrency_is_one_group_per_pr_cancelling_in_progress`. The CLI also parses the caps (AC5's last test) | pass (3) |
| AC8 | `ac8_summary_step_runs_always`. `ac8_summary_step_writes_shadow_md_from_out`, run under bash. `ac8_artifact_uploads_the_three_files_from_out`, from the same directory as `--out`. `ac8_artifact_name_holds_pr_number_head_sha_and_attempt`. `ac8_artifact_is_kept_90_days_and_uploads_always` | pass (5) |
| AC9 | `ac9_every_uses_is_pinned_to_a_commit_sha` (all jobs) | pass |
| AC10 | `ac10_nothing_continues_on_error`: no such key at any level, and not in the text | pass |
| AC11 | `ac11_known_mode_passes_the_check_quietly` ×4 (unset as `''`, `off`, `shadow`, `comment`). `ac11_mode_absent_from_env_passes_the_check`. `ac11_unknown_mode_fails_with_one_line_naming_variable_and_values` ×9: `bogus`, `Shadow`, `SHADOW`, a leading and a trailing space, `on`, `*`, `shadow\|comment` (glob and alternation), and `$(touch pwned)`, which must not run. `ac11_every_other_job_needs_the_check_and_respects_its_failure`: every other job needs it, directly or through another job, and no `if` uses `always()`, `failure()`, `cancelled()` or `success()` to run after it fails | pass (15) |
| AC12 | Manual: the run, summary and artifact; tokens under the output cap; latency, and the time for checkout, `uv sync` and the review step; branch protection requiring only `gate`; a Dependabot or no-secret run showing `skipped`, `no_credential` and a green job | **pending** (after merge) |

### Failures

None.

### Readings for the designer

1. **"No `GITHUB_TOKEN` reaches any step" (AC5)** is tested as "the file passes none": neither `GITHUB_TOKEN` nor `github.token` appears in it.
   - `actions/checkout` still uses `github.token` as its default `token` input to clone, then removes it (`persist-credentials: false`).
   - Every Actions job also holds its runtime token.

   Neither reaches a `run:` step's environment, and neither can be avoided while keeping AC4's checkout. **Resolved in review round 1:** the main session is rewording AC5 to say this. Checkout uses `github.token` only to clone and doesn't persist it, and no `run:` step receives it.
2. **Fetching the head on a private repository.** With `persist-credentials: false`, the step `git fetch --no-tags origin "$HEAD_SHA"` has no credentials, so it would fail on a private repository. **Resolved in review round 1:** the main session verified with an anonymous `git ls-remote` that the repository is public.
3. **GitHub compares the mode case-insensitively.** In the shadow job's `if`, `SHADOW` equals `'shadow'`. The check's `case` is case-sensitive, so it fails on `SHADOW`. The shadow job `needs` the check, so it's skipped. The outcome matches AC11, but it depends on `needs`, which `ac11_every_other_job_needs_the_check_and_respects_its_failure` protects.
4. **The check job also runs for fork PRs**, with no secret and `contents: read`. That fits AC11 ("a check job fails"), and a fork can't change a repository variable.
5. **The action SHAs** are checked for form (40 hex digits) by the test, since checking them against their tags needs the network. **Resolved in review round 1:** the main session verified each against its tag: `actions/checkout` v4.4.0, `actions/upload-artifact` v4.6.2 and `astral-sh/setup-uv` v6.8.0.
6. **The summary step's fallback**, when there's no `shadow.md`, isn't in the spec, so it isn't tested.

### Coverage

The slice changes no module in `src/` except the `run --mode` flag, which the developer's unit tests cover. This file makes one in-process CLI call. The tester ran only this file, so coverage wasn't measured.

## P2-S4: Judge would-be comments from shadow reports, and keep the labels

Spec: [p2-s4-label-shadow-findings.md](slices/p2-s4-label-shadow-findings.md). Tests: [`tests/integration/test_p2_s4_label_shadow_findings.py`](../../../tests/integration/test_p2_s4_label_shadow_findings.py), 47 cases. They were written from the spec, design.md ("How the gate is evidenced", `shadow/v1`) and ADR 0011, before the developer's code was read. They were then tightened to the exact texts the main session pinned: the description line, the prompts, `Labelling stopped.`, the counts line, the skip lines and the two exit-2 errors. They ran against the developer's uncommitted work (base `fb9f525`), including the `run_id` dedupe.

**Status: AC1 to AC13 pass. All 47 cases pass, from this file alone, on Windows. No AC violations found.**

### Seam and set-up

- **Seam:** `review_bot.pr.cli.main(["label", "--reports", DIR, "--labels", FILE])`, in process. `sys.stdin`, `sys.stdout` and `sys.stderr` are P1-S12's simulated terminals (`FakeStream`). An answer can also be a callable, run at the moment of its read, so a test can look at `FILE` before the next question (AC3, AC4). Each segment must hold exactly one description line, `Would-be comment i of n: repo#pr file:line [severity] source S, rule R, in k report(s): body`, which the tests split into fields. The end-of-session line is matched exactly: `review-bot pr: label: judged=J skipped=S remaining=R files_skipped=F`.
- **Input reports are built by `run`**, with a scripted `MockProvider`, over a real git repository in `tmp_path`. The repository has a base, a head with K1 to K3 on lines 1 to 3 and two identical lines on 4 and 6, and a second push that inserts one line on top. The PR number is set in the event.
- **Reports are edited after `run` writes them only when a criterion needs a value no command writes yet.** That means `mode` `comment` (S5), `origin` `backfill` (S13), outcome `failed` with entries still present, an `action` of `keep` or `update`, a fingerprint that differs for one key, or a hostile `body` or `file`.
- **A question is identified by its body, not its position.** The stderr text written since the previous stdin read is that question's segment, and the rationale word (such as `K1r3`) says which would-be comment it is.
- **"Latest by `finished_at`"** is separated from file order: the oldest report sits at the deepest, last-sorting path and has the newest modification time.

### Criteria

Test names drop the `test_p2_s4_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_three_reports_ask_about_k1_k2_k3_each_once`: three reads, each key's latest body in exactly one segment, with one report at `DIR`'s top and one three levels down. `ac1_each_question_shows_the_latest_reports_fields`: repo and PR, `file:line`, severity, source and rule part, all from the latest report; older bodies never shown. `ac1_each_question_shows_how_many_reports_had_it` (3, 2, 1, with `report` singular for 1). `ac1_a_report_downloaded_twice_counts_once`: the same `shadow.json` in two folders is one report | pass (4) |
| AC2 | `ac2_a_reworded_entry_on_a_shifted_line_is_one_would_be_comment`: the second push moves K1 down a line and rewords it; one question shows the new line. `ac2_a_different_fingerprint_for_one_key_is_one_would_be_comment`: the record holds the latest fingerprint. `ac2_two_identical_lines_are_two_would_be_comments_indexed_in_line_order`: index 0 is line 4 and index 1 is line 6, by their fingerprints, although the mock lists line 6 first | pass (3) |
| AC3 | `ac3_accept_appends_one_shadow_accept_line_before_the_next_question`: one line already exists at the second read. `ac3_a_new_session_appends_to_the_existing_file`: the old bytes are kept as a prefix | pass (2) |
| AC4 | `ac4_reject_asks_for_a_reason_and_records_it` ×4 (`w`, `n`, `l`, `d`): the reason prompt is shown exactly as specified. `ac4_any_other_reason_answer_asks_again`: `x` and an empty line ask again, and nothing is written before a valid reason | pass (5) |
| AC5 | `ac5_skip_writes_nothing_and_asks_again_next_session` | pass |
| AC6 | `ac6_a_labelled_would_be_comment_is_not_asked_again`: a newer report re-lists K1, and only K2 is asked. `ac6_the_same_key_on_another_pr_is_asked_about`: PR 8, same anchor | pass (2) |
| AC7 | `ac7_excluded_reports_and_entries_contribute_nothing` ×4 (`mode` `comment`, outcome `failed`, action `keep`, action `update`). For an excluded report, K1 shows the older report's body and a count of 1. `ac7_backfill_reports_count_like_event_reports` | pass (5) |
| AC8 | `ac8_stopping_part_way_keeps_the_lines_and_exits_0` ×2 (end of input, Ctrl+C): exit 0, no traceback, one record kept, `Labelling stopped.`, then `judged=1 skipped=0 remaining=2 files_skipped=0`. `ac8_a_finished_session_shows_its_counts` (`judged=2 skipped=1 remaining=0`, no stop message). `ac8_ctrl_c_at_the_reason_prompt_writes_nothing_for_it` | pass (4) |
| AC9 | `ac9_without_a_terminal_exits_2_naming_label` ×3 (stdin, stderr, neither): exactly the pinned line, `review-bot: error: label needs a terminal: …`, within 5 s, nothing read, `FILE` not created. `ac9_stdin_from_the_null_device_exits_2` (P1-S12's console check on Windows) | pass (4) |
| AC10 | `ac10_an_accept_record_has_exactly_the_keys_and_the_latest_entrys_values`: every value is pinned, including `run_id`, `model` and `prompt_version` from the latest report, and `recorded_at` in UTC and within the run. `ac10_records_hold_no_body_or_rationale`: no rationale word, no body and no reaction text in `FILE`; a reject record has its reason | pass (2) |
| AC11 | `ac11_each_bad_file_is_skipped_with_one_line_naming_it`: not JSON, another schema, over 10 MiB (a valid report padded with whitespace, whose key is never asked about), 200,000-deep nesting, binary bytes, and `comments` not a list. One line each, `review-bot pr: skipped <path>: <problem>`, with the problem pinned per file (`not JSON`, `not a shadow/v1 report (…)` or `over 10000000 bytes`). No traceback, within 60 s, and the good report is still asked about. `ac11_the_count_of_skipped_files_is_shown`: `files_skipped=6`. `ac11_a_report_of_exactly_10_000_000_bytes_is_read` | pass (3) |
| AC12 | `ac12_a_malformed_labels_line_exits_2_naming_it` ×3 (not JSON, missing keys, not an object, each on line 2 after a valid line 1): exit 2 with `Line 2 of the labels file --labels <path> isn't a feedback/v1 record (…). Fix or remove that line, then run label again.`, no prompt, nothing read, `FILE` byte-identical | pass (3) |
| AC13 | `ac13_a_hostile_rationale_shows_no_raw_control_characters`, through `run`. `ac13_limits_a_tampered_body_shows_no_raw_control_characters` ×6 (CSI with a newline, OSC with BEL, C1 CSI, carriage return, U+2028, NUL) written straight into the report's `body`; the whole body stays on the description line. `ac13_limits_a_tampered_path_shows_no_raw_control_characters` | pass (8) |
| Limits | `limits_twenty_thousand_entries_for_one_key_are_read_within_the_bound`: 20,000 entries under one key in one report, so 20,000 would-be comments; within 60 s, `remaining 20000` | pass |

### Failures

None at the end. Three were bugs in the tests, fixed before this report:
- the labels file was put in a directory that didn't exist, and the spec doesn't promise to create one;
- one test looked for the wrong rationale word;
- one test's count pattern didn't match `files_skipped=6`.

### Readings for the designer

1. **"10 MB" is read as 10,000,000 bytes** by the implementation (`over 10000000 bytes`). The tests accept either reading: exactly 10,000,000 bytes must be read, and over 10 MiB must be skipped. The size between the two isn't pinned.
2. **Resolved in review round 1: the README now defines `remaining` as "not reached this session".** `remaining` (AC8) excludes would-be comments skipped this session. After `a`, `s`, `r w` on three, the line reads `judged=2 skipped=1 remaining=0`. The main session pinned this, and the tests now assert it. The spec doesn't define `remaining`, though. Since a skip is asked about again next session (AC5), a user could read `remaining=0` as "nothing left to judge". Consider defining it in the spec's Interfaces.
3. **Local reports** (`origin` `local`, `pr` null) contribute nothing, according to the main session ("`pr` is set"). AC7 doesn't list that case, so it isn't tested. Consider adding it to AC7.
4. **Every `*.json` file under `DIR` is read**, according to the main session, not only `shadow.json` as AC1 words it. An unrelated JSON file in a download folder, such as an event payload, would be reported as a skipped file and counted in `files_skipped`. The tests use only `shadow.json` names, so they don't pin this either way.
5. **A missing parent directory for `--labels`** exits 2 before any report is read (changed in review round 1): `The directory of the labels file --labels <path> doesn't exist. Create it, or pass a file in a directory that exists.` This isn't in the spec; it is a usage error.
6. **The question shows the body on one line**, with its newlines as literal `\n`. That meets AC13 and P1-S12 AC9's "one line", but long bodies are hard to read. This is a usability note, not a failure.

### Gaps

- **Ctrl+C while reports load** (the developer's second late change) has no test here. Raising `KeyboardInterrupt` part-way through loading needs a hook inside `label`'s file reading, which this suite deliberately doesn't patch. AC8's "part-way" is tested at the verdict and reason prompts. The developer's unit tests should cover the loading case.
- **The real-terminal path** (`console.read_answer` on a real console) runs only through simulated streams, as in P1-S12.

### Coverage

From this file alone, with `--cov=review_bot.pr`: `pr/feedback.py` 97%, `pr/label.py` 91%, `pr/cli.py` 79%, measured before review round 1 moved the reading model and unit rule into `pr/units.py` (whose unit tests are in `tests/unit/test_pr_units.py`) (`cli.py` also holds `shadow` and `run`). The developer's unit tests (`tests/unit/test_pr_label.py`, `test_pr_feedback.py`) cover the rest. The full suite's coverage wasn't measured here, because the tester ran only this file.

## P2-S13: Backfill shadow reports over recent merged PRs

Spec: [p2-s13-shadow-backfill.md](slices/p2-s13-shadow-backfill.md). Tests: [`tests/integration/test_p2_s13_shadow_backfill.py`](../../../tests/integration/test_p2_s13_shadow_backfill.py), 40 cases. They were written from the spec, design.md (Commands and exit codes, CI settings and caps, `shadow/v1`) and P2-S2's spec while the developer built the slice in parallel, before any of the developer's code was read. They were then updated to the main session's decisions: the `--github-repo` flag, the two closing stdout lines, the missing-commit hint, a missing base as `missing_commit`, a repeated number as exit 19, and `--max-retries` forced to 0 as well. Review round 1 then added: an existing `failed` report is reviewed again, counted and replaced, and an unreadable existing report counts as done. They ran against the developer's uncommitted work after review round 1 (base `baaf6cb`).

**Status: AC1 to AC11 pass. All 40 cases pass, from this file alone, on Windows. No AC violations found.**

### Seam and set-up

- **Seam:** `review_bot.pr.cli.main(["backfill", "--pulls", FILE, "--github-repo", "octo/review-bot", "--repo", DIR, "--out", OUT, "--max-calls", N, "--provider", "mock", ...], provider=P)`, in process. The merged-PR list comes only from `FILE`, with `gh pr list --json`'s field names. One provider serves every PR in the run.
- **`PerPullProvider`** is a test double for `LLMProvider`. It records each request and answers by the PR's file in the prompt, with findings or a `ProviderError`, so the results don't depend on the order of the calls. `MockProvider` gives the same answer to every call.
- **`History`** is one real git repository in `tmp_path`. Each PR is a branch off `main` adding `src/pr_<n>.py`, after which `main` moves on, so each PR's `baseRefOid` differs from its merge base.
- **AC11's real call** uses no injected provider: `--provider claude` reaches the suite's `real_query_guard`, which records the options the Claude CLI would get.
- **Pinned texts** (the main session's): the stdout lines `backfill: entries=E already_in_out=A run=R` and `backfill: reviewed=.. blocked_secret=.. skipped=.. failed=.. <reasons, sorted>=N .. too_large=N` are matched exactly. The missing-commit hint is matched by its opening (`review-bot pr: pull request N's base or head commit isn't in --repo, `), `wasn't reviewed` and `git fetch origin refs/pull/N/head:refs/pull/N/head` on that line. The developer reworded its tail while these tests ran: it now also says to fetch the base branch's full history when the base commit is missing.

### Criteria

| AC | Tests (`test_p2_s13_…`) | Result |
|---|---|---|
| AC1 | `ac1_each_pr_gets_a_backfill_shadow_report_with_its_number_shas_and_merged_at`: `OUT/<n>/shadow.json` and `shadow.md` for 3 PRs with `--max-calls 3` (the boundary), with `mode` `shadow`, `origin` `backfill`, outcome `reviewed`, `pr` exactly `{repo, number, base_sha, head_sha}`, and `event_at` = `mergedAt` in `+00:00` form. `ac1_each_pr_reviews_only_its_own_changes_anchored_on_its_heads_lines`: each prompt holds only its PR's file, with no `main`-only change and no other PR's file. Each finding is anchored on the head's line 2. `ac1_every_report_has_a_distinct_run_id_so_label_keeps_each`: 3 distinct `run_id`s, and `pr.units.would_be_comments` keeps one comment per PR, under `octo/review-bot`. `ac1_invalid_github_repo_exits_2_before_any_call` ×4 (no slash, three parts, empty, empty name): stderr names `--github-repo` | pass (7) |
| AC2 | `ac2_cross_repository_pr_is_skipped_fork_without_a_call`: the same-repo PR is still reviewed | pass |
| AC3 | `ac3_more_planned_calls_than_allowed_refuses_naming_both`: 5 entries with `--max-calls 3` exit 2, stderr names 5 and 3, and there are no calls and no reports. `ac3_planned_calls_count_entries_that_would_later_be_skipped`: a fork and a missing head still count, so 5 entries with `--max-calls 4` are refused, naming 5 and 4 | pass (2) |
| AC4 | `ac4_missing_head_is_skipped_and_stderr_says_to_fetch_its_pull_ref`: the PR with the missing head is `skipped`, `missing_commit`; the PRs before and after are reviewed; exit 0; one stderr line names PR 42, says it wasn't reviewed and gives `git fetch origin refs/pull/42/head:refs/pull/42/head`. `ac4_missing_base_is_also_skipped_missing_commit` | pass (2) |
| AC5 | `ac5_too_large_and_docs_only_prs_are_skipped_without_a_call`: with `REVIEW_BOT_MAX_REVIEW_BYTES=5000`, 20 kB of Python is `too_large` and a docs-only PR is `nothing_to_review`. Only the small PR is called | pass |
| AC6 | `ac6_ci_set_refuses_before_any_call`: exit 2, stderr names `CI`, no calls and no reports | pass |
| AC7 | `ac7_failed_pr_does_not_stop_the_rest_and_the_first_failure_sets_the_exit`: of 4 PRs, the 2nd fails with `rate_limit` and the 3rd with `auth`. 4 calls are made, both failures have `failed` reports with their reasons, the other two are reviewed, and the exit is 9 | pass |
| AC8 | `ac8_malformed_file_exits_19_before_any_call` ×3 (not JSON, an object, not UTF-8). `ac8_entry_missing_a_field_exits_19_naming_it_after_a_valid_entry`. `ac8_sha_that_isnt_40_hex_digits_exits_19_naming_the_entry` ×8 (39 or 41 hex, 40 non-hex, `HEAD`; in `baseRefOid` and in `headRefOid`). `ac8_repeated_pr_number_exits_19_naming_it`. In each, a valid entry comes first, stderr names `entry 2`, and there are no calls and no reports. Limits: `ac8_more_than_500_entries_exits_19_before_any_call` (501 entries) and `ac8_file_over_10_mb_exits_19_quickly_before_any_call` (a valid array padded with whitespace to 10,000,001 bytes, within 60 s) | pass (15) |
| AC9 | `ac9_stdout_ends_with_counts_per_outcome_and_reason`: one PR of each kind gives exactly `entries=6 already_in_out=0 run=6`, then `reviewed=1 blocked_secret=0 skipped=4 failed=1 fork=1 missing_commit=1 nothing_to_review=1 rate_limit=1 too_large=1`. `ac9_counts_say_how_many_were_too_large_even_when_none_were`: `… failed=0 too_large=0` | pass (2) |
| AC10 | `ac10_existing_reports_are_kept_and_not_counted_towards_max_calls`: an earlier backfill leaves real reports, `reviewed`, `skipped` (fork) and `blocked_secret` (an AWS key). A second run over 6 entries with `--max-calls 3` makes 3 calls, those three reports stay byte-identical, and stdout says `entries=6 already_in_out=3 run=3`. `ac10_an_unreadable_existing_report_counts_as_done`: non-JSON bytes are left untouched and not counted. `ac10_a_failed_report_is_reviewed_again_and_replaced`: with `--max-calls 1`, the one call is the failed PR's, its report becomes `reviewed` with a new `run_id`, and the done PR's report is byte-identical (`entries=2 already_in_out=1 run=1`). `ac10_a_failed_report_counts_towards_max_calls`: one done, one failed and one new PR make 2 planned calls, over `--max-calls 1`, so exit 2 naming 2, no calls, and existing reports untouched. `ac10_running_again_reviews_nothing_and_rewrites_no_report` | pass (5) |
| AC11 | `ac11_caps_record_zero_retries_in_every_report` ×2 (`REVIEW_BOT_MAX_RETRIES=2`, and `--max-retries 2`): `caps.max_retries` is 0 in reviewed and skipped reports. `ac11_the_claude_cli_is_called_with_zero_retries`: every call that reached the stopped `query` had `CLAUDE_CODE_MAX_RETRIES=0` | pass (3) |

### Failures

None that point to the product. Two failures in the first run against the implementation were in the tests, and were fixed:
- `Unit`'s PR field is `pr`, not `number`.
- The tests read "naming the entry" (AC8) as naming the PR number. The implementation names the entry's position (`entry 2`), which also names it. The tests now assert the position (see reading 2).

### Readings for the designer

1. **`caps.retries`** in AC11 is the existing `caps.max_retries` key. Consider rewording the spec.
2. **AC8 names entries by position, not by PR number.** The position always exists, even when the number is missing or invalid. For a bad SHA in a valid entry, giving the number too would save the user counting through `gh`'s array. This is a usability note, not a failure.
3. **The Limits, a repeated number and `--github-repo` are the main session's decisions** (exit 19, 19 and 2). The spec's Interfaces could record them, along with the two stdout lines.
4. **"First failure" (AC7)** is read in `FILE` order, as the main session confirmed. `gh pr list` writes the newest PR first.
5. **"Already exists" (AC10)** now means an existing report whose outcome isn't `failed`, or one that can't be read (the main session's decisions in review round 1). A `failed` report is reviewed again and replaced. The spec's AC10 doesn't yet say either; consider adding both.
6. **The missing-commit hint differs from the text the main session pinned:** it adds ", and if the base commit is missing, fetch the base branch's full history too". That is still within AC4. The tests don't pin the tail, so the main session can settle the wording without changing them.

### Gaps

- **A git failure part-way through a run** (exit 19, with the counts so far, even after a provider failure) isn't tested here. This suite can't make git fail without the commit being missing: a head SHA naming a blob is checked as `<sha>^{commit}` and ends `skipped`, `missing_commit`. The developer's unit test `test_backfill_stopped_by_git_after_a_provider_failure_exits_with_the_git_error` (`tests/unit/test_pr_backfill.py`) covers it, and P2-S2's tests cover git's own failures and timeout.
- **AC11's second test can't prove every call.** The guard raises `AssertionError`, not a `ProviderError`, so if that ends the run, only its first call is recorded. The `caps` test covers every report.

### Coverage

From this file alone, with `--cov=review_bot.pr`: `pr/shadow.py` 87%, `pr/event.py` 77%, `pr/cli.py` 76%, `pr/runner.py` 76%, `pr/git.py` 67%. These modules also hold `run`, `shadow` and `label`, which this file doesn't exercise. The full suite's coverage wasn't measured here, because the tester ran only this file.
