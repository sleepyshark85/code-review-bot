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

## P2-S5: Create, update, move and resolve the bot's own comments

Spec: [p2-s5-comment-sync.md](slices/p2-s5-comment-sync.md). Tests: [`tests/integration/test_p2_s5_comment_sync.py`](../../../tests/integration/test_p2_s5_comment_sync.py), 53 tests, 79 cases. They were written from the spec, design.md (Comment state, Interfaces, Commands and exit codes) and ADR 0009 while the developer built the slice, before any of the developer's code was read. The only source read was the public `pr/github.py` protocol. The seam was assumed, then confirmed by the main session, and matched. They ran against the developer's finished, uncommitted work (base `35c8f65`), then again after review round 1. That round added the reworded-move test, at the main session's decision.

**Status: AC1 to AC16 pass. All 79 cases pass, from this file alone, on Windows. The AC3 failure found in the first run (below) was fixed in build.**

### Seam and set-up

- **Seam:** `review_bot.pr.cli.main([... "run", "--mode", "comment" ...], provider=P, github=FAKE)`, with `FAKE = review_bot.pr.fake_github.FakeGitHubClient()`. The fake provides:
  - `add_comment`, `mark_outdated`, `set_line`, `resolve(id, by=)` and `fail_on(name)`;
  - `calls`, `comments` and `summary`.

  All of this sits in one adapter class, `GitHub`. AC5's not-commentable case calls `plan_comments(findings, existing, *, pr_diff, max_open_comments)` directly.
- **Sequences of runs:** each PR is a real git repository in `tmp_path`, with one commit per push on `feature` and one fake for the whole PR. Between pushes, the test plays GitHub. It says where GitHub now shows each comment (`set_line`), when it marks one outdated, and when a person resolves a thread.
- **Reading a run:** the fake's calls during that run (writes are `post_review`, `update_comment`, `resolve_thread`, `write_summary`), its comments, and `shadow.json`'s `comments` and `withheld` entries.
- **Seeded comments** are used only where runs can't produce the state: candidates whose fingerprints no current finding has (AC3), duplicates (AC8), comments that aren't the bot's own (AC9), and bot-resolved threads (AC3, AC7). Their bodies follow S1's form, with the severity first and the marker last.
- **`applied` in earlier suites:** S5 adds `applied` to every comment entry, as design.md's `shadow/v1` says it will. S1's shared `COMMENT_KEYS` (`test_p2_s1_shadow_report_local.py`) now includes it. It had failed 27 cases across the S1, S2 and S13 files, among them `test_p2_s13_ac1_each_pr_reviews_only_its_own_changes_anchored_on_its_heads_lines`, which checks keys through S1's `load_shadow`. All 158 cases in those three files pass again.

### Criteria

| AC | Tests (`test_p2_s5_…`) | Result |
|---|---|---|
| AC1 | `ac1_first_run_posts_one_review_on_the_head_with_both_comments`: one `post_review` on the head SHA, 2 comments on (file, line, new side), one summary, both entries `create`/`done`. `ac1_posted_bodies_are_s1s_bodies_with_their_markers`: bodies equal a shadow run's bodies, and the marker holds the independently computed fingerprint, rule part and anchor | pass (2) |
| AC2 | `ac2_same_findings_again_post_nothing_and_edit_the_summary_in_place` (after an unrelated push). `ac2_severity_change_edits_only_that_comment_and_keeps_its_fingerprint`. `ac2_after_the_update_the_same_severity_again_posts_nothing` | pass (3) |
| AC3 | `ac3_identical_line_added_above_keeps_the_comment_and_creates_only_the_new_one`, and `ac3_identical_lines_settle_and_a_third_run_posts_nothing` | pass (2), after the fix below |
| AC3 | `ac3_same_key_same_line_pairs_the_candidate_with_the_findings_severity`. `ac3_nearer_candidate_wins_over_an_equal_severity_one_further_away`. `ac3_tie_goes_to_the_open_thread_over_a_person_resolved_one`. `ac3_remaining_tie_goes_to_the_oldest_comment_id`. `ac3_bot_resolved_thread_with_the_same_fingerprint_is_never_a_candidate`. The tie tests make the pairing visible: the finding's severity matches neither candidate, so whichever pairs is updated | pass (5) |
| AC4 | `ac4_comment_whose_own_line_is_deleted_or_edited_is_resolved` ×2. `ac4_edited_line_github_still_shows_current_is_resolved` (the outdated flag is only a hint). `ac4_only_other_lines_changed_in_a_new_file_keeps_it` ×6 (line above edited, lines inserted above, line below edited; GitHub following the line or marking it outdated). `ac4_only_other_lines_changed_in_an_existing_file_keeps_it`. `ac4_rule_issue_fixed_below_an_unchanged_anchored_line_is_kept` ×2: a real `swallowed-exception` comment, then `pass` replaced by logging | pass (12) |
| AC5 | `ac5_outdated_paired_comment_is_resolved_and_reposted_on_the_findings_line` (identical rationale, so the same body and fingerprint; one review, `move`/`done`). `ac5_move_goes_to_the_findings_line_when_the_anchor_is_on_several`. `ac5_moved_comment_takes_the_reworded_rationale_and_keeps_its_fingerprint`: the model rewords the rationale between pushes; the moved body holds the new rationale and not the old, and its marker is the old one, fingerprint included. `ac5_the_old_bot_resolved_thread_cant_pair_again`. `ac5_move_to_a_line_not_commentable_resolves_nothing_and_posts_nothing` (plan level: only `withheld:not_commentable`, no resolve, no move). `ac5_outdated_comment_with_no_pairing_finding` ×2 (anchor on no line: resolved; anchor moved to another line: kept). `ac5_anchor_only_in_another_file_doesnt_keep_an_outdated_comment` | pass (8) |
| AC6 | `ac6_comments_found_again_under_the_new_path_move_there_with_their_fingerprints`: 3 comments, 2 found again after a rename; 2 moves with their old fingerprints, no create, and the third is kept. `ac6_after_the_move_nothing_is_created_twice` | pass (2) |
| AC7 | `ac7_finding_paired_with_a_person_resolved_thread_posts_nothing`. `ac7_person_resolved_outdated_thread_isnt_moved_or_reposted`. `ac7_person_resolved_thread_isnt_reposted_after_a_rename`. `ac7_finding_whose_only_match_is_bot_resolved_is_created` | pass (4) |
| AC8 | `ac8_the_one_that_isnt_outdated_is_kept_and_the_other_resolved`. `ac8_both_current_keeps_the_older_and_resolves_the_newer`. `ac8_both_outdated_with_no_finding_keeps_the_older` | pass (3) |
| AC9 | `ac9_not_own_comment_doesnt_block_a_create` ×6 and `ac9_not_own_comment_on_a_gone_line_isnt_resolved` ×6. The 6 cases: another user with a valid marker; another bot (`dependabot[bot]`) with a valid marker; the bot with no marker; a 15-hex fingerprint; text on a line after the marker; text after the marker on its line | pass (12) |
| AC10 | `ac10_eight_open_and_five_new_creates_two_and_withholds_three`: the 8 come from a real run and stay on valid lines; of 5 new findings (critical, high, 3 low), critical and high are created, and the 3 low are withheld `over_limit`. `ac10_resolved_threads_dont_count_towards_the_limit`. `ac10_creates_never_take_a_pr_past_the_limit_in_one_run` (12 findings on a fresh PR: 10 created) | pass (3) |
| AC11 | `ac11_skipped_outcome_records_no_call` ×6 (fork, action, `no_credential`, draft, `nothing_to_review`, and `too_large` from design.md's list). Each run has a seeded own comment whose line is gone, so any plan would resolve it; the fake records no call at all, not even a read | pass (6) |
| AC12 | `ac12_secret_in_the_diff_changes_only_the_summary_naming_kind_and_line`: only `write_summary`, no provider call, the summary names `aws-access-key` and the secret's line in the PR diff, and the value appears in no call. `ac12_transient_failure_changes_only_the_summary_and_exits_11` | pass (2) |
| AC13 | `ac13_precondition_the_run_has_three_entry_operations` (update, resolve and create, plus the summary). `ac13_failing_the_second_operation_exits_18_and_reports_what_was_applied`: a rehearsal run learns the write order, then the second write fails; exit 18, stderr names the operation, and each entry is `done`, `failed` or `not_attempted` by its place in that order. `ac13_any_failed_operation_still_writes_the_shadow_report` ×3 | pass (5) |
| AC14 | `ac14_draft_pr_in_comment_mode_is_skipped_draft`. `ac14_the_same_draft_in_shadow_mode_is_still_reviewed` | pass (2) |
| AC15 | `ac15_comment_mode_without_a_client_exits_2_before_any_provider_call`. `ac15_shadow_mode_reads_no_token`: `os.environ` is wrapped to record reads by name, with `GITHUB_TOKEN` set. `ac15_shadow_mode_uses_no_client_even_when_one_is_given` | pass (3) |
| AC16 | `ac16_two_runs_after_a_move_post_move_and_resolve_nothing` (one rerun on the same head, one after an unrelated push; the moved comment is `keep` with its fingerprint). `ac16_restored_line_creates_one_comment_then_nothing` ×2: GitHub keeps the resolved comment outdated, or shows it current on the restored line again | pass (3) |
| Limits | `limits_huge_foreign_body_full_of_marker_text_is_ignored_quickly`: a 1.3 MB marker-like body, from another user and from the bot, within 60 s. `limits_thousands_of_comments_on_one_key_pair_quickly`: 2,000 identical lines, each with an open comment under another fingerprint and a finding; all pair, nothing is posted, within 60 s | pass (2) |

### Failures

None open.

**Fixed in build: AC3. An identical line added above a commented one made the bot resolve and re-post a comment on every run.** In the first run against the build, the run right after the insert did what AC3 says: it kept the old comment and created only the new line's. But the new comment carried the kept comment's fingerprint. Fingerprint ordinals follow line order, so the new line 2 gets ordinal 0, which the comment posted on old line 3 already has.

From then on, two open comments share one fingerprint, so the duplicate rule (AC8) resolves the newer one. Its finding is then unpaired, since a bot-resolved thread is never a candidate, and it is created again. Each run posts one comment and resolves one, which is the noise AC3 and ADR 0009 exist to prevent. It also gives two comments one identity for labels.

Minimal reproduction:
1. Push `src/app.py` with `value = eval(data)` on line 3, and run with a finding on line 3. One comment is created, with fingerprint `a41c5ba3d21f1182` (ordinal 0).
2. Push the file with the same line also inserted as line 2. GitHub shows the comment at line 4. Run with findings on lines 2 and 4.
   - Entries: `create` line 2 and `keep` line 4, both with fingerprint `a41c5ba3d21f1182`.
   - Ordinal 1 would be `89762c7d5e31e7c0`.
3. Run again with the same findings.
   - Writes: `post_review`, `resolve_thread`, `write_summary`.
   - Entries: `create` line 2, `keep` line 4, `resolve` line 2.
   - Each later run repeats this.

The developer fixed it after the first run: a created comment now takes the next ordinal no other comment holds, and both AC3 tests pass. They stay as the regression check.

### Readings for the designer

1. **Pairing step 1** is now the main session's decision: the same fingerprint pairs first only when the comment's current line is the finding's line. A create takes the next free ordinal (the fix above). design.md's pairing list should say both.
2. **A move re-renders the body** from the pairing finding and keeps the posted fingerprint (the main session's decision; AC5 is being reworded to say so).
3. **AC13's "three operations".** A comment-mode run always writes the summary too, and it is still written after a failure unless the summary write itself failed (the main session's description). The tests use three entry operations plus the summary and fail the second write in the run's own order, `post_review` then the moves' resolves and the updates, then the resolves, then the summary. The spec could record this order, and that the summary is still written.
4. **AC5's not-commentable move can't be reached end to end in S5**: the reviewed diff is the PR diff, so verify drops the finding first. It is tested through `plan_comments`; S7's incremental diff will make it reachable.
5. **"New side" has no field.** `NewComment` documents the new side but carries no `side`. S6's real client has to send `side: RIGHT`, and its tests should check it.
6. **Behaviour outside the ACs, not tested here:**
   - A failed `list_bot_comments` exits 18 with no report.
   - Shadow-mode entries are `applied` `not_attempted`, and `keep` entries are `done`.
   - `resolve` entries have null `source` and `body`.

   The spec's Interfaces could record all three.

### Gaps

- **GitHub's line tracking is played by hand.** The fake can't know how GitHub re-maps a comment after a push, so each test states it with `set_line` or `outdate`. Where GitHub's choice is uncertain, both are tested: a comment that follows its line and one marked outdated (AC4), and a resolved comment that stays outdated or shows current again (AC16).
- **The whole-project 90% coverage threshold** fails on any run of one or two files, so it says nothing about this slice.

### Coverage

From this file plus P2-S13's, with `--cov=review_bot.pr`: `pr/plan.py` 97%, `pr/runner.py` 96%, `pr/fake_github.py` 94%, `pr/comments.py` 91%, `pr/cli.py` 85%. `cli.py` also holds `label` and parts of `backfill`, which these files don't exercise.

## P2-S6: Turn comments on with one repository variable

Spec: [p2-s6-comments-on.md](slices/p2-s6-comments-on.md). Tests: [`tests/integration/test_p2_s6_comments_on.py`](../../../tests/integration/test_p2_s6_comments_on.py), 37 tests, 110 cases, and the stub [`tests/integration/github_stub.py`](../../../tests/integration/github_stub.py). They were written from the spec, design.md (Interfaces, Comment state, Safety on the PR path) and ADRs 0008 and 0012 while the developer built `RestGitHubClient`. Of the developer's code, only the client's public constructor was read, to fix the seam. They ran against the developer's uncommitted work on `feat/p2-s6-comments-on` (base `094cf2b`). They ran again after the developer fixed both findings below, with AC8 rewritten for the main session's decision. They ran a third time after review round 1, which links threads by `fullDatabaseId` and removes `GITHUB_TOKEN` in every entry point.

**Status: AC1 to AC11 pass, so all 110 cases pass on Windows. AC12 is manual and still pending. With the P0-S2 lockdown, P2-S3 and P2-S5 files, 291 of 291 cases pass. P2-S2's `ac7_both_caps_unset_leave_the_claude_env_as_before` passes again too.**

### Seam and set-up

- **Seam:** `review_bot.pr.rest_github.RestGitHubClient(repo, GitHubAccess(api_url, token), *, timeout_s=30.0)`, built only in `make_client`. The tests first assumed `RestGitHubClient(token=, api_url=, repo=)`. The repository has to come from somewhere, because `update_comment` takes no PR.
- **Command-line runs:** `python -m review_bot.pr run --mode comment` runs in process with `--provider mock` (AC8 uses `claude` over the fake SDK) and no injected client, over a real git PR.
  - `GITHUB_TOKEN` and `GITHUB_API_URL` are set as Actions sets them.
  - `GITHUB_GRAPHQL_URL`, `GITHUB_REPOSITORY`, the proxy variables and `CI` are unset, so the run reads only the variables the spec names.
  - `REVIEW_BOT_AUDIT_LOG` is unset, so the audit log goes to `--out`.
- **The stub** is a `ThreadingHTTPServer` on `127.0.0.1:0`.
  - **It serves:**
    - REST review comments with GitHub's paging (`per_page`, `page`, `Link`);
    - GraphQL review threads, paged by `first` and `after`, with `resolvedBy`. Each comment's `fullDatabaseId` is a string, as GitHub's BigInt is, and its `databaseId` is null above 2^31 - 1, as GitHub's 32-bit field is;
    - review creation, comment edits, the `resolveReviewThread` mutation, and issue comments.
  - **It records** every request with its headers and body.
  - **Its objects** carry many more fields than a client needs.
  - **`fail(route, kind)`** fails every request to one route, so a retry shows up as a second request.
- **Timeouts are tested without waiting.** The held-response cases build the client with `timeout_s=0.5`, and the stub holds its answer for 20 s, 40 times longer. Every other case keeps the client's default timeout, so it can't time out. The 30 s default is checked separately: `socket.socket.settimeout` is wrapped to record the values the client sets, without changing them. An earlier version capped every socket timeout at 1 s instead, and the developer saw those cases fail once. The current version passed 8 runs in a row.
- **AC7's default URL:** `socket.getaddrinfo` records each lookup and fails any non-loopback one. The test sees `api.github.com:443`, and nothing leaves the machine.
- **AC8:** a full `run --mode comment` through `main`, with no injected client. `GITHUB_TOKEN` is set, and `GITHUB_API_URL` points at the stub. The provider is `ClaudeAgentProvider` over the fake SDK. When the provider runs, the fake SDK's script step builds the environment the way `SubprocessCLITransport.connect` does in SDK 0.2.178: the inherited `os.environ` without `CLAUDECODE`, with `options.env` laid on top. The token is restored when the test ends, because `monkeypatch` set it.
- **Workflow:** AC9 to AC11 reuse P2-S3's helpers and `if:` evaluator. P2-S3's AC6, AC9 and AC10 tests walk every job, so they cover the comment job without changes.
- **Earlier suites changed:**
  - **P2-S3** (`test_p2_s3_shadow_workflow.py`), all 60 cases pass:
    - `shadow_job` is now the job that runs `--mode shadow`, because two jobs now run `review_bot.pr run`.
    - Two whole-file AC5 checks are scoped to that job. The key must be in that job's run step `env` and nowhere else in the job. That is stricter than before, because the step is now named. Neither `GITHUB_TOKEN` nor `github.token` may appear anywhere in the job, keys included.
    - The helpers now take a job, so S6 can reuse them.
  - **P2-S5** (`test_p2_s5_comment_sync.py`): `ac15_comment_mode_without_a_client_exits_2_before_any_provider_call` is removed, because AC1 replaces that interim exit. The other two AC15 tests stay.

### Criteria

| AC | Tests (`test_p2_s6_` prefix) | Result |
|---|---|---|
| AC1 | `ac1_comment_mode_with_a_token_posts_through_the_rest_client`: no client is injected. The stub sees the listing, one review holding both comments, and one summary write, and each request carries the token. `shadow.json` is `comment`, with both entries `done`. `ac1_no_token_exits_2_naming_it_before_the_provider` ×2 (unset, empty): exit 2 naming `GITHUB_TOKEN`, with no provider request and no request to the stub | pass (3) |
| AC2 | `ac2_lists_only_own_marked_comments_over_three_pages_with_their_threads`: 250 comments in pages of 100, 100 and 50, and 249 threads served in reverse order, so threads must be matched by comment ID. Excluded: a person with a valid marker, the bot's login with `type: User`, GraphQL's login `github-actions` used as the REST login, the bot with no marker, an unknown rule part, text after the marker, and a person's reply carrying a marker. Read back from page 3: an outdated comment (`line` None), a thread resolved by the bot, one resolved by a person, a comment with another path and severity, and comment `2^31 + 1250`, which only `fullDatabaseId` links to its thread. Before review round 1, the client failed on that last one with `no review thread holds comment 2147484898`. The test compares all nine `BotComment` fields | pass (1) |
| AC3 | `ac3_post_review_sends_one_comment_review_with_path_line_side_and_body`: exactly one request, `POST .../pulls/7/reviews`, with `event: COMMENT`, the head SHA, and each comment's path, line, `side: RIGHT` and body, in order | pass (1) |
| AC4 | `ac4_update_comment_sends_one_edit` (`PATCH .../pulls/comments/1001` with `{"body": ...}`). `ac4_resolve_thread_sends_one_resolve_mutation`. `ac4_write_summary_creates_one_when_the_bot_has_none`. `ac4_write_summary_edits_the_bots_existing_one_in_place`: the summary is on page 2 of 120 issue comments, after two decoys (another workflow's bot comment, and a person quoting the summary with its marker). The client makes exactly one write, to the summary's ID | pass (4) |
| AC5 | `ac5_failure_is_one_github_error_line_naming_call_and_status_and_isnt_retried` ×50, one case for each of the 10 responses below on each of the 5 operations. Each raises `GitHubError` as one line naming the operation, and makes one request to the failing route. The message gives the status in the 7 status cases, and says the request timed out in the held case. The 10 responses: 401, 403, 404, 422, 500, a 403 with `X-RateLimit-Remaining: 0`, a 429 with `Retry-After`, a held response, HTML, and a well-formed JSON body of 5 MB + 4 KB. `ac5_failed_call_in_a_run_exits_18_naming_call_and_status` ×2 (listing 500, review 422) | pass (52) |
| AC6 | `ac6_token_is_sent_only_in_the_authorization_header`: all five operations. Every request carries the token in `Authorization`, and no path, query, body or other header holds it. `ac6_token_is_in_no_error_even_when_github_quotes_it_back` ×10: the stub's REST failure bodies quote the `Authorization` header back. `ac6_token_is_in_no_stderr_line_audit_record_or_report` ×2 (a posted review, and a review that fails with 500 from the quoting stub): checks stderr and every file in `--out` (`shadow.json`, `shadow.md`, `audit.jsonl`) | pass (13) |
| AC6 | `ac6_token_is_in_no_error_from_a_graphql_error_quoting_it`: the GraphQL `errors[0].message` quotes the `Authorization` header back | pass (1), after the fix below |
| AC7 | `ac7_unset_api_url_sends_requests_to_api_github_com`: a lookup of `api.github.com:443`, then exit 18. `ac7_plain_http_to_a_host_other_than_loopback_exits_2_naming_the_variable` ×6: `api.github.com`, a private IP, `127.0.0.1.evil.example`, `localhost.evil.example`, `127.0.0.1@evil.example` (userinfo) and `evil.example#@127.0.0.1` (fragment). `ac7_plain_http_to_localhost_is_accepted` | pass (8) |
| AC8 | `ac8_shadow_run_with_a_token_set_keeps_it_from_claude_too`: the same check over a `run --mode shadow`, with `GITHUB_TOKEN` set. It failed before review round 1, which removes the token in every entry point. `ac8_claude_subprocess_environment_has_no_github_token`: a comment-mode run with `GITHUB_TOKEN=ghs_canary`. The stub sees the token on every request, so the client read it. `GITHUB_TOKEN` is absent from the merged environment the Claude CLI would get, and no value holds the canary. To check that the test can fail, a throwaway run replaced the CLI's token removal (`_take_github_access`) with a plain read; the test failed as it should | pass (2) |
| AC9 | `ac9_comment_job_runs_for_a_same_repo_pr_when_mode_is_comment`. `ac9_comment_job_doesnt_run_for_any_other_mode` ×4 (unset, `off`, `shadow`, `bogus`). `ac9_comment_job_doesnt_run_for_a_fork` ×3 (a fork, a similar name, a deleted fork). `ac9_at_most_one_review_job_runs_per_mode` ×4. P2-S3's AC3 tests, still green, cover the shadow half | pass (12) |
| AC10 | `ac10_comment_job_permissions_are_exactly_contents_read_and_pull_requests_write`. `ac10_run_step_gets_both_credentials_and_passes_mode_comment_and_out`. `ac10_no_other_step_gets_either_credential`: in every job, each key or value naming `secrets.`, `github.token`, `GITHUB_TOKEN` or `ANTHROPIC_API_KEY` sits in the `env` of either the comment run step (both credentials) or the shadow run step (the key only). There is no workflow or job `env`. S3's rules, checked for this job: `ac10_comment_job_runs_only_for_the_shadow_jobs_four_actions` (AC1), `ac10_comment_job_runs_the_bot_from_the_base_and_only_fetches_the_head` (AC4), `ac10_comment_job_has_the_design_caps_timeout_and_concurrency` (AC7), `ac10_comment_job_shows_the_report_and_uploads_it_always` (AC8, running the summary script with bash), and `ac10_run_command_and_caps_are_accepted_by_the_cli` (the step's argv and env get as far as the event, exit 19). S3's whole-file tests cover AC6, AC9 and AC10 | pass (8) |
| AC11 | `ac11_shadow_job_permissions_are_still_contents_read_only`, with S3's `ac2_shadow_job_sets_exactly_contents_read` | pass (1) |
| AC12 | Manual, for the user: set `REVIEW_BOT_MODE=comment` on a test PR and check the comments and summary. Then push three changes, one at a time: an edit next to a commented line, an edit to the commented line itself, and lines inserted above it. After each, record GitHub's outdated flag and what the bot did. Finally, reset the variable | **pending** |
| Limits | `limits_more_than_10_pages_of_comments_exits_18_naming_the_limit` (an endless listing, at most 11 requests). `limits_every_request_has_a_timeout_of_at_most_30_s`. `limits_a_comment_missing_a_field_is_a_github_error` (no `user`). `limits_a_graphql_error_answer_is_a_github_error`. Every stub object carries extra fields, so every test also shows unknown fields are ignored | pass (4) |

### Failures

None open. Both findings from the first run were fixed in build.

**Fixed: AC6. A GraphQL error message carried the token into the error.** `RestGitHubClient._graphql` copied GitHub's `errors[0].message` into the `GitHubError` it raised. It collapsed whitespace and cut the length, but didn't redact anything. A message quoting the request's `Authorization` header therefore reached the error, and a run writes that error to stderr. GitHub isn't known to echo credentials, so this needed a hostile or broken endpoint.

Minimal reproduction:
1. Start `GitHubStub` with `state.echo_authorization = True`. For an unknown thread, its `resolveReviewThread` answer is `{"data": {"resolveReviewThread": null}, "errors": [{"message": "Could not resolve to a node. Bearer ghs_canary"}]}`.
2. Call `resolve_thread("PRRT_missing")`.
3. The message ended `... Could not resolve to a node. Bearer ghs_canary. Nothing was retried; run the job again.`

The developer now removes the token from every message built from a response. The test stays as the regression check.

**Fixed: AC8's first build broke earlier suites.** The provider passed `GITHUB_TOKEN: ""` in `options.env`, because the SDK can't remove an inherited variable. That broke 5 integration cases:
- 4 in `test_p0_s2_lockdown.py`, whose AC3 says `options.env` is *exactly* the retry and timeout keys;
- 1 in `test_p2_s2_review_pr_event.py` (`ac7_both_caps_unset_leave_the_claude_env_as_before`).

The main session decided the fix: in comment mode, `pr/cli.py` reads `GITHUB_TOKEN` for the client, then deletes it from `os.environ` before any provider is built. `options.env` is exact again, and all 5 cases pass. The AC8 test was rewritten to go through `main`, because the provider alone no longer hides the token.

### Readings for the designer

1. **"Naming the status" when there is no failing status.** A timeout has no status, and a non-JSON or oversized body comes with 200. The tests require the timeout case to say it timed out (`no response within N s`), and require only the operation's name in the other two. The build's messages for those two say `the response isn't JSON` and `the response is over 5 MB`.
2. **An empty `GITHUB_TOKEN` counts as missing** (exit 2). AC1 says "no `GITHUB_TOKEN`". The tests read that as including an empty value, and the build agrees.
3. **AC7 doesn't say when a rejected URL exits**, so the tests don't check whether it happens before the provider call. In the build, the URL is checked alongside AC1's token check, before anything else runs.
4. **P2-S5 AC15 and the token removal now disagree.** AC15 says shadow mode reads no token. Since review round 1, every entry point calls `take_github_token()`, which does `os.environ.pop("GITHUB_TOKEN")`. `test_p2_s5_ac15_shadow_mode_reads_no_token` still passes, but only because its watched environment records `__getitem__`, `get` and `__contains__`, not `pop`. AC15 should be reworded, for example "shadow mode removes the token and never uses it". The test should then follow the new wording, rather than pass by accident.
5. **Behaviour outside the ACs**, from the build notes and not tested here:
   - The client refuses redirects.
   - A malformed token exits 2.
   - The summary is recognised by `reviewed=\S+` on its last line, and the first summary carries `reviewed=none`.
   - REST paging stops at the first page that isn't full, so a full 10th page exits 18 even when nothing follows it.
   - A resolved thread whose `resolvedBy` is null counts as resolved by the bot. This is a guess about GitHub's data, and AC12 is where it gets checked.

### Gaps

- **The stub can't reproduce GitHub's outdated logic or real permissions.** That is AC12's job, and what it observes goes into the known limits.
- **AC8 is checked against the SDK's merge rule, not a spawned CLI.** The rule is copied from SDK 0.2.178. The fix now removes the token from the process environment, which every merge rule inherits from, so an SDK upgrade is less of a risk than before. AC8 is covered only for runs through `python -m review_bot.pr run`. A caller that builds the provider some other way, with the token in its environment, isn't covered.
- **The whole-project 90% coverage threshold** fails on any run of a single file, so it says nothing about this slice.

### Coverage

From this file alone, with `--cov=review_bot.pr`: `pr/rest_github.py` is at 95%, and `pr/cli.py` at 61%. The uncovered branches are a malformed token, a URL that doesn't parse, a PR from another repository, GraphQL `data` that fails its schema, and a comment that has no thread. `pr/cli.py` is low because it also holds `shadow`, `backfill` and `label`.

## P2-S7: Re-review only what changed since the last reviewed commit

Spec: [p2-s7-review-new-commits.md](slices/p2-s7-review-new-commits.md). Tests: [`tests/integration/test_p2_s7_review_new_commits.py`](../../../tests/integration/test_p2_s7_review_new_commits.py), 26 tests, 32 cases. They were written from the spec, design.md (Comment state, Interfaces, `shadow/v1`) and ADR 0009 while the developer built the slice, before any of its code was read. They reuse P2-S5's `Session` (a real git PR in `tmp_path`, one `FakeGitHubClient` per PR, in-process `run --mode comment` with `MockProvider`).

**Status after review round 1: all 32 cases pass, with the developer's round-1 fix (the full-review fallback for an unusable diff since A, and the AC6 guard applied only to files that existed at A and still do). Together with the P2-S5 and P2-S6 files, 220 of 220 cases pass.** Before the fix, the 4 cases added for round 1 failed on the behaviour (see Failures). Before round 1, the first 28 cases passed against the developer's uncommitted build on `feat/p2-s7-review-new-commits` (base `9c6e720`).

### Seam and set-up

- **Runs:** `review_bot.pr.cli.main(["run", ..., "--mode", "comment"], provider=MockProvider, github=FakeGitHubClient)`, as in P2-S5. What the model was asked is the `user_prompt` of each `MockProvider.requests` entry.
- **Summary:** read back from the fake's `summary`, and its marker from the last line by `<!-- review-bot:v1 summary reviewed=<40 hex> -->`. Tests that need a summary the bot didn't write set `FakeGitHubClient.summary`, which the fake's `read_summary` parses (confirmed). The summary's final wording, as the coordinator passed it on, is checked by phrase: `Changes since \`<sha7>\` reviewed.`, the `no_new_changes` sentence, `No new findings at or above <threshold>.` (the run's `report.threshold`, `low` by default, not the `medium` of the example), and `Open bot comments on this pull request: N.`. The plural in "1 comments" isn't asserted, since review may fix it.
- **`shadow.json`:** `scope` (`full` or `incremental`) and `since` (a SHA or null) are the last two keys, after `over_budget` (confirmed). P2-S1's `SHADOW_KEYS` now ends with them, so every suite that loads `shadow.json` checks them.
- **`fail_on`** fails every later call to the operation, as in S5. AC10's GitHub case recovers with a new `FakeGitHubClient` holding the old one's `comments` and `summary`.
- **Pushes:** besides P2-S5's commits and renames, the base branch can move on and be merged into the PR (`git merge --no-ff main`), with the event's `base.sha` at main's tip, as GitHub sends it; and a force-push resets the branch to the base before committing.

### Criteria

| AC | Tests (`test_p2_s7_` prefix) | Result |
|---|---|---|
| AC1 | `ac1_only_the_hunk_changed_since_a_is_sent_and_scope_is_incremental`: an existing file with two PR hunks 17 lines apart, plus a new file; the push edits the lower hunk only. The prompt holds the edit, not the upper hunk or the other file; `scope` `incremental`, `since` A; summary `reviewed=B`. `ac1_base_branch_changes_merged_in_arent_sent`. `ac1_reviewed_marker_advances_with_each_push`: A, B, C in turn | pass |
| AC2 | `ac2_merge_from_main_alone_calls_no_model_and_changes_no_comment`: no request, `reviewed`/`no_new_changes`, only the summary written, `reviewed=` the merge commit, and the summary's `no_new_changes` sentence with `Open bot comments on this pull request: 1.` | pass |
| AC3 | `ac3_rerun_on_the_reviewed_head_is_skipped_already_reviewed`: no request, no write, summary unchanged | pass |
| AC4 | `ac4_force_push_reviews_the_whole_pr`, `ac4_reviewed_sha_not_in_the_clone_reviews_the_whole_pr`: `full`, null `since`, an untouched file in the prompt | pass |
| AC5 | `ac5_summary_without_a_valid_marker_reviews_the_whole_pr` ×6 (no summary, no marker, S5's `reviewed=none`, 39 hex, 41 hex, a non-hex digit). `ac5_first_run_with_no_summary_is_full`. `ac5_valid_sha_with_text_after_the_marker_isnt_read` | pass |
| AC6 | `ac6_comment_whose_own_line_changed_since_a_is_resolved`, whose summary says `Changes since` A and a count of 0 after the resolve. `ac6_unchanged_line_inside_a_hunk_changed_since_a_is_kept` ×2 (GitHub follows it, or marks it outdated), whose summary is the whole incremental no-findings sentence with a count of 1. `ac6_unchanged_line_in_a_file_added_since_a_is_kept`: an S5 regression check only, because the line is still there | pass |
| AC6 (round 1) | `ac6_edited_line_in_a_file_added_since_a_is_resolved`: a comment on a file added since A, posted by a run whose `write_summary` then failed (`reviewed` stays A); its line is edited, so it's resolved. `ac6_after_a_rename_an_edit_to_the_commented_line_resolves_it`: AC12's comment stays on `a.py` with no pairing; the next push edits its line in `b.py`, so it's resolved | pass (2) |
| Notes: unusable diff since A | `unusable_diff_since_a_over_the_byte_cap_falls_back_to_full`: a 300 KB file merged from main, with `REVIEW_BOT_MAX_DIFF_BYTES=5000` over a PR diff under 5,000 bytes. `unusable_diff_since_a_not_utf8_falls_back_to_full`: a Latin-1 file merged from main. Each must be `reviewed`, `full`, null `since`, with the whole PR in the prompt | pass (2) |
| AC7 | `ac7_twin_found_at_ordinal_0_pairs_with_its_ordinal_1_comment`: identical lines 2 and 20, both commented (ordinals 0 and 1); a push next to line 20 sends only its hunk; the finding there pairs with the ordinal-1 comment: `keep`, nothing posted | pass |
| AC8 | `ac8_rename_only_push_calls_no_model_and_keeps_every_comment` | pass |
| AC9 | `ac9_finding_on_a_line_only_the_changed_diff_has_is_withheld_not_commentable`, through `plan_comments(..., pr_diff=, changed=)`: `changed` holds a base-branch line merged in, which the PR diff lacks; the finding there is withheld `not_commentable`. Not end to end: see reading 3 | pass |
| AC10 | `ac10_provider_failure_keeps_reviewed_at_a` (the next push is reviewed from A, the failed push's edit in the prompt). `ac10_github_failure_keeps_reviewed_at_a` (`post_review` fails; the re-run reviews from A and posts; its summary says `Changes since` A, `0 withheld.` and a count of 2: 1 open plus 1 created) | pass |
| AC11 | `ac11_shadow_mode_is_full_every_time_and_uses_no_client`: two shadow runs after a reviewed comment run, with a client given; the summary body has no open-comment count | pass |
| AC12 | `ac12_only_the_edit_is_reviewed_and_the_comment_isnt_resolved`, `ac12_pairing_finding_moves_it_to_b_with_its_fingerprint` (old thread resolved, posted on `b.py` with the old body, no `create`) | pass |
| Limits | `limits_huge_summary_of_marker_text_is_read_quickly_and_ignored`: about 3 MB of marker-like text, a 1,000,000-digit SHA on the last line; full review within 60 s | pass |

### Failures

Both found in review round 1 and fixed in build; the tests stay as regression checks.

**Fixed: AC6. A gone line on a path the diff since A doesn't name wasn't resolved.** Both cases end with `resolved_threads == []`.
1. **Rename:** set up `renamed_after_a` (an `a.py` comment, with `a.py` renamed to `b.py` after A). Run once with no findings, which keeps the comment. Then push `b.py` with line 3 (`EVAL`) replaced, and run with no findings.
2. **Added file:** after A, push `src/added.py`. Run with a finding on line 2 while `fail_on("write_summary")` is set, which exits 18 with `reviewed` still at A. Then push line 2 edited, mark the comment outdated, and run with no findings.

**Fixed: an unusable diff since A ended `skipped`, not `full`.** A merge from main with a 300 KB file under a 5,000-byte cap, or with a Latin-1 file, gives outcome `skipped`. The spec's Notes now say such a run reviews the whole PR (the main session's decision in review round 1).

### Readings for the designer

1. **AC1 and AC12, "hunk" of a new file.** AC1 sends the PR diff's hunks that hold a changed line. For a file new in the PR (AC12's `b.py`), that hunk is the whole file, so "only that edit's hunk" can't exclude the rest of `b.py`. The tests check only that the edit is sent and that another file isn't, and put AC12's edit within 3 lines of the anchor, so the pairing finding is in the reviewed diff under either reading.
2. **AC6, "in a file added since A".** Every line of a file added since A is changed since A, so the literal rule ("resolved only if its own line changed") would allow resolving. The test reads the clause as "kept while its anchored line is still there" (S5's rule) and seeds an own comment on such a file, as a run whose summary write failed would leave it.
3. **AC9 can't be reached end to end.** The reviewed diff is a subset of the PR diff, and verify drops any finding outside it, so a finding on a line only the incremental diff has never reaches the plan. The end-to-end version (a base-branch change merged in, a finding on it) got an empty `withheld`. The test now checks the plan alone; AC9 could be reworded as a plan rule, or dropped as unreachable.
4. **AC10 for GitHub failures** doesn't say whether the summary is still written after a failed `post_review`. The test only requires that, if it is, its marker stays A. The build records the head only after a reviewed outcome where every earlier GitHub call succeeded.

### Gaps

- **The summary's owner check** ("read only from the bot's own summary comment") belongs to `RestGitHubClient`; the fake holds one summary, so it isn't tested here.
- **The git timeout and the byte caps on the second diff** aren't attacked: a slow `git diff` can't be produced in a temp repo without patching `src`.
- **A reviewed SHA missing from a shallow clone in CI** is simulated by a SHA absent from the repository.

### Earlier suites changed

- **P2-S1** (`test_p2_s1_shadow_report_local.py`): `SHADOW_KEYS` gains `scope` and `since`.
- **P2-S6** (`test_p2_s6_comments_on.py`): `GitHubClient.write_summary` now takes a required keyword, `reviewed_sha`, so the two AC4 tests and AC5's `write_summary` cases pass `reviewed_sha=None`.
- **P2-S5** (`test_p2_s5_comment_sync.py`): 12 cases re-ran a reviewed head (now `already_reviewed`), or pushed an unrelated or rename-only commit and expected findings outside it (now incremental, or `no_new_changes`). `Session.forget_reviewed()` drops the summary, so such a run reviews the whole PR again (S7 AC5), and each test keeps its intent: planning against the whole PR diff. Changed: AC2 ×3, AC3 settle, AC5 bot-resolved thread, AC6 ×2 (in `push_rename`, and before the second run), AC10 ×2, AC16 ×3. AC7 and AC11 needed no change.

### Coverage

From this file alone, with `--cov=review_bot.pr`: `plan.py` 93%, `fake_github.py` 87%, `runner.py` 82%, `comments.py` 81%, `git.py` 69%. `git.py`'s uncovered lines are mostly its error paths (timeout, a missing binary, a failed `git diff`), which S2's suite covers.

## P2-S9: Set the review policy per repository

Suite: `tests/integration/test_p2_s9_repo_config.py`, written from the spec, design.md and ADR 0008 while the developer built the slice, before any of its code was read. Run against the developer's work in progress (uncommitted, before review): **34 of 34 pass**. The seam is still to be confirmed by the main session.

### Seam and set-up

- Each run is `review_bot.pr.cli.main(["run", "--event", ..., "--repo", ..., "--out", ..., "--provider", "mock", "--mode", MODE, *FLAGS], provider=MockProvider(...)[, github=FakeGitHubClient])`, as in S2 and S5 (`run_pr` is the one place to change).
- Each PR is a real repository: `.review-bot.toml` at the base commit (as a file, a symlink or a directory, built through git's index so no symlink support is needed on disk), then one head commit. **The head stays checked out**, so reading the working tree instead of the base commit would fail AC2.
- `shadow.json`'s `config` is read as an object with `threshold`, `exclude`, `max_open_comments` and `source`. The tests read those keys by name and don't check the order inside `config`.

### Criteria

| AC | Tests (`test_p2_s9_…`) | Result |
|---|---|---|
| AC1 | `ac1_no_file_applies_the_defaults_and_reports_them` (`low`, `[]`, 10, `default`; all four severities planned). `ac1_a_file_only_the_head_adds_is_ignored` (a head-only file with `critical`, `exclude = ["*.py"]` and broken TOML changes nothing) | pass |
| AC2 | `ac2_base_threshold_medium_drops_low_findings` (`source` is `.review-bot.toml@<base>`), `ac2_head_raising_the_threshold_doesnt_change_its_own_review` | pass |
| AC3 | `ac3_exactly_the_matching_files_are_skipped_and_not_sent` (the spec's 5 skipped and 2 kept paths; each file holds its own canary, checked in the prompt). `ac3_excluded_files_dont_count_towards_reviewable_bytes` (a 2,000-byte cap: the same PR is `too_large` without the file, `reviewed` with it excluded). `ac3_a_finding_on_an_excluded_file_isnt_planned` | pass |
| AC4 | `ac4_max_open_comments_3_creates_3_and_withholds_2` (comment mode: 3 posted, 2 withheld `over_limit`) | pass |
| AC5 | `ac5_invalid_config_fails_invalid_config_without_a_call_and_exits_2`, with 18 cases: invalid TOML, unknown key, caps (`max_review_bytes`, `max_output_tokens`, `mode`, a `[caps]` table), wrong types (threshold int, exclude string, exclude item int, `max_open_comments` string and bool), `max_open_comments` 0 and 26, `threshold = "urgent"`, 51 patterns, a 201-character pattern, 65,559 bytes, non-UTF-8. `ac5_symlink_or_directory_at_the_path_fails_invalid_config` (2 cases). `ac5_limits_a_huge_file_fails_fast` (4 MB within 60 s). `ac5_limits_boundaries_are_accepted` (50 patterns of 200 characters, 25 comments, about 60,000 bytes) | pass |
| AC6 | `ac6_flag_then_environment_then_file`: env `high` over file `medium`; `--threshold critical` over env and file; `--threshold low` over file `medium` | pass |
| Limits | `limits_nested_double_star_patterns_match_in_bounded_time`: 50 patterns of 60 `**/` segments each, none matching, against a 31-segment path; within 60 s, the file reviewed | pass |

### Readings for the designer

1. **"64 KB"** may mean 64,000 or 65,536 bytes; the build uses 65,536. The tests stay clear of the boundary: 65,559 bytes is refused and about 60,000 is accepted.
2. **The summary names "the key or problem".** The tests read the summary as GitHub shows it (Markdown escapes removed, through S1's `summary_text`). They match the key's name for schema errors, and loose patterns for the rest: `TOML` for a parse error (the file name is lowercase, so it doesn't match), `65,?536|64 ?KB|over the limit` for size, `link|regular file|not a file` and `director|not a file` for a symlink and a directory.
3. **AC6 doesn't say what `config.threshold` shows** when a flag or the environment wins. The AC6 test checks only which findings are planned. AC1 and AC2 check `config.threshold`, where nothing overrides it.
4. **Non-UTF-8 content** isn't listed in AC5. It's tested as `invalid_config`, since `tomllib` can't parse it.
5. **AC3, a finding on an excluded file.** That file wasn't reviewed, so the test expects no comment on it. Verify should drop it, since the file isn't in the reviewed diff.

### Gaps

- **What the failed report holds besides the outcome** (`report`, `comments`, `config`) isn't specified, so isn't asserted.
- **Invalid config in comment mode** (whether the summary comment is written) isn't specified, so it isn't tested. Every AC5 case runs in shadow mode.

### Earlier suites changed

- **P2-S1** (`test_p2_s1_shadow_report_local.py`): `SHADOW_KEYS` gains `config`, last, after `since`. The build's `shadow.json` passes `load_shadow` with it there.

### Coverage

From this file alone, with `--cov=review_bot`, against the work in progress: `pr/config.py` 100%, `diff_parser.py` 82%, `pr/git.py` 71%, `pr/runner.py` 68%. The rest of `git.py` and `runner.py` is error paths and comment mode, which S2, S5 and S7's suites cover.

## P2-S8: Turn reactions and resolutions into labels

Suite: `tests/integration/test_p2_s8_reactions_as_labels.py`, written from the spec, ADR 0011 and design.md while the developer built the slice, before any of its code was read. Re-run against the developer's final code (before review) and the main session's revised workflow: **83 of 83 pass**, and the P2-S3, P2-S4 and P2-S6 suites pass with it (300 of 300 across the four files). The main session confirmed the seam below.

### Seam and set-up

- AC1 to AC7 and AC11: `review_bot.pr.cli.main(["collect", "--event", PATH, "--out", DIR], github=FakeGitHubClient)`, with P2-S2's event and action `closed`. Comments are seeded with the fake's `add_comment` and `resolve(by="bot"|"person")`, and reactions with `react(comment_id, content, *, by, by_type="User")`. AC7 fails each operation a healthy run called, which is `list_signals`. `ClosedPr` is the one place to change.
- AC8: `collect` with no injected client, so it builds `RestGitHubClient` from `GITHUB_TOKEN` and `GITHUB_API_URL` and talks to `github_stub.GitHubStub`. The stub now serves `GET /pulls/comments/{id}/reactions` (the build asks for `per_page=100&page=N`; paged, with an endless mode), puts the same reactions in each GraphQL comment node, and counts them in the REST comment's `reactions`. Resolvers come from GraphQL `resolvedBy`, as in S6.
- Canaries: two reacting usernames, a resolver login, a rationale and another person's comment body. None may appear in any file under `DIR`.

### Criteria

| AC | Tests (`test_p2_s8_…`) | Result |
|---|---|---|
| AC1 | `ac1_one_thumbs_up_from_a_person_is_one_reaction_accept_record` | pass |
| AC2 | `ac2_thumbs_combine_into_one_label`, 6 cases: down only, both from one person, both from two people, a down then ups, two ups (one `accept`), two downs | pass |
| AC3 | `ac3_no_thumbs_from_a_person_is_no_reaction_record`, 5 cases: heart, laugh and eyes; the bot's up; the bot's down; the bot's both plus a heart; hooray, rocket and confused. `ac3_the_bots_thumbs_up_doesnt_turn_a_persons_reject_into_conflict` | pass |
| AC4 | `ac4_a_person_resolved_thread_is_one_resolution_record`, `ac4_a_resolution_record_comes_with_the_reaction_record`, `ac4_a_bot_resolved_thread_gives_no_resolution_record` | pass |
| AC5 | `ac5_a_comment_by_someone_else_gives_no_record`, 4 cases: a person with a valid marker, the bot's login typed `User`, GraphQL's login, another bot. `ac5_a_bot_comment_without_a_valid_marker_gives_no_record`, 5 cases: no marker, an unknown rule, a short fingerprint, text after the marker, a summary marker. Each has thumbs from two people and a person's resolution. `ac5_no_own_comments_writes_no_record` | pass |
| AC6 | `ac6_records_have_s4s_keys_marker_fields_and_null_join_fields` (the exact key set; fingerprint, file, rule part and anchor from the marker; the 7 join fields null; `recorded_at` UTC). `ac6_no_username_or_body_reaches_dir` (canaries) | pass |
| AC7 | `ac7_each_failing_github_call_exits_18_with_no_feedback_file`: every operation a healthy `collect` called is made to fail in turn | pass |
| AC8 | `ac8_rest_client_reads_reactions_and_resolvers` (a person's and the bot's resolution, the bot's reaction, a person's comment; canaries and token absent). `ac8_reactions_over_two_pages_are_all_read`. `ac8_more_than_10_pages_of_reactions_exits_18_naming_the_limit`. `ac8_a_failed_read_exits_18_once_with_one_line_and_no_file`: 7 failure kinds × reactions, threads and comments, with GitHub quoting the token back. `ac8_no_token_exits_2_naming_it_and_sends_nothing`. `ac8_collect_makes_no_write_call` | pass |
| AC9 | `ac9_closed_is_added_to_the_pull_request_types`, `ac9_collect_runs_for_a_closed_same_repo_pr_unless_mode_is_off`, `ac9_collect_doesnt_run_for_a_fork` (3), `ac9_collect_doesnt_run_for_any_other_action` (4), `ac9_shadow_and_comment_jobs_dont_run_on_closed` (4 modes), `ac9_collect_permissions_are_exactly_contents_and_pull_requests_read`, `ac9_only_the_collect_step_gets_the_token_and_no_other_secret`, `ac9_collect_step_passes_the_event_and_out`, `ac9_feedback_is_uploaded_for_90_days_named_by_pr_and_attempt`, `ac9_collect_has_its_own_concurrency_group_that_never_cancels` (no workflow-level group; collect's group holds the PR number with `cancel-in-progress: false`, and differs from the shadow and comment jobs' groups, which cancel in progress per PR), `ac9_collect_command_is_accepted_by_the_cli` (exit 19 on a broken event). P2-S3's AC6, AC9, AC10 and AC11 tests walk every job, the collect job included | pass |
| AC10 | `ac10_label_reads_collected_records_and_still_asks_about_the_same_comment`: `collect`'s own file, whose records carry K1's fingerprint, file, rule part and anchor, is `label`'s labels file. K1 is still asked about and the answer is appended | pass |
| AC11 | `ac11_a_moved_comment_and_its_old_thread_give_one_reaction_record`, 4 cases (up/down `conflict`, up/up `accept`, down/down `reject`, heart/up `accept`), with the old thread bot-resolved so it adds no resolution record. `ac11_different_fingerprints_stay_separate` | pass |
| Limits | `limits_many_comments_and_reactions_are_reduced_quickly`: 2,000 own comments with 5 reactions each, within 60 s | pass |

### Findings against the workflow (main session)

Both are resolved in the revised workflow:

1. **minor (fixed): a shared concurrency group let `closed` and `collect` cancel each other's work.** The workflow-level group is gone. The shadow and comment jobs each have `review-<PR>` with `cancel-in-progress: true`, and the collect job has `collect-<PR>` with `cancel-in-progress: false`. `ac9_collect_has_its_own_concurrency_group_that_never_cancels` pins this.
2. **question (fixed): the collect step set an unused `PR_NUMBER`.** It's removed.

### Readings for the designer

1. **Settled by the spec's build notes:** an empty run writes an empty `feedback.jsonl` and exits 0 (asserted by the AC5 empty-PR test).
2. **Settled by the spec's build notes:** comments sharing a fingerprint give records naming the newest comment's file, rule part and anchor (unit test `test_a_findings_records_name_its_newest_comments_file_and_anchor_and_null_the_rest`).
3. **Settled by the spec's build notes:** at most one resolution record per fingerprint (unit test `test_a_moved_comment_and_its_person_resolved_old_thread_give_one_resolution_label`).
4. **Settled in review round 1 (main session): reactions by any bot** (user type `Bot`, such as `dependabot[bot]`) are ignored; only people's reactions become labels.

### Earlier suites changed

- **`github_stub.py`:** reactions per review comment (`add_reaction`, the REST reactions route, GraphQL `reactions` nodes, REST counts) and `endless_reactions`. Resolvers per thread were already there.
- **P2-S3** (`test_p2_s3_shadow_workflow.py`): `job_runs` takes an `action` (default `synchronize`), and `collect_step_in` and `collect_job_name` are added. `ac1_shadow_job_runs_only_for_the_four_actions` now evaluates the shadow job's `if:` for every trigger type: it runs for the four and not for `closed`. Before, it only checked that `github.event.action` appeared in the `if:`.
- **P2-S3 AC7 and P2-S6 AC10 concurrency tests:** unchanged. They already read the job's own `concurrency` before falling back to a workflow-level one, so they hold for the per-job groups.
- **P2-S6** (`test_p2_s6_comments_on.py`): `ac10_comment_job_runs_only_for_the_shadow_jobs_four_actions` evaluates the same way. `ac10_no_other_step_gets_either_credential` adds exactly the collect step's `env` and `env.GITHUB_TOKEN` to the allowed holders, which the new workflow had made it fail. The shadow and comment entries are unchanged.

### Coverage

From the S3, S4, S6 and S8 files together, with `--cov=review_bot.pr`, against the final code: `pr/feedback.py` 97%, `pr/rest_github.py` 95%. `pr/cli.py` and `pr/fake_github.py` are covered further by the S5 and S7 suites.
