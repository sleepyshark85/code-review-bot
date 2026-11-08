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
