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
| AC5 | `ac5_start_line_past_the_end_says_so_with_no_lines`. `ac5_line_count_400_is_accepted` (boundary). `ac5_invalid_arguments_are_refused_and_the_review_goes_on` ×22: `line_count` 401 or 3.5; unknown keys on each tool; wrong types (a number, `null`, a list, the string `"10"`, the bool `true` for an integer, `"yes"` for a bool); missing required keys on each tool; empty, 201-character and line-broken literals; empty and 201-character patterns; a `path_glob` over 1,000 characters. `ac5_read_path_over_1000_is_refused_invalid_path_and_the_review_goes_on`: superseded by P3-S2 AC1, so a `read` path over 1,000 characters is now `invalid_path`, not `invalid_arguments` (see P3-S2, Readings 1). `ac5_unknown_tool_is_refused_and_the_review_goes_on` ×5 (`write`, `bash`, `Read`, `read `, empty). `ac5_path_without_a_file_is_refused_and_the_review_goes_on` ×4 (`not_found` for a missing file and for a 1,000-character path, `directory` twice). `ac5_refusals_are_recorded_with_their_reason`. Each refusal is followed by a valid read that must succeed, then the finding | pass (35) |
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

## P3-S11: Route findings to code owners

Spec: [p3-s11-codeowners-routing.md](slices/p3-s11-codeowners-routing.md), as amended by the designer on 2026-11-09. Tests: [`tests/integration/test_p3_s11_codeowners_routing.py`](../../../tests/integration/test_p3_s11_codeowners_routing.py), 84 cases. They were written from the spec's criteria, Limits and Interfaces, design.md (the pipeline, report keys, Q8) and ADR 0008 while the developer built the slice, before any of its code was read.

**Status: every criterion passes. All 84 cases pass, from this file alone, on the developer's uncommitted working tree** (base `4e2b706`).

**History:** the first version had 71 cases against the approved spec. On the developer's tree, 70 passed. The one failure was AC1's own example: a finding on `billing/README.md` can't exist, because Phase 1 skips `.md` files as docs. That test was flagged rather than weakened, and the designer amended the spec (AC1 to AC3, AC5, AC6). The tests were then updated to the amendment:
- AC1 now uses `*.js @docs` on `billing/widget.js`, and the verbatim README test was removed.
- AC3 gains anchoring cases.
- AC2 and AC5 gain directory and symlink cases.

No test was changed to make it pass. The only other edit after a run shortened one Limits test's file name, which hit Windows' path limit.

### Gate

Only this file was run, as the coordinator asked (the full suite runs out of memory locally). One P2-S1 case was also run, to check the changed key lists against a local `shadow` run. `ruff format --check` and `ruff check` are clean on the new file. For the full gate, see the main session's run.

### Seam and set-up (assumed)

- **Every run** calls `review_bot.pr.cli.main(["run", "--event", PATH, "--repo", DIR, "--out", OUT, "--provider", NAME, "--mode", MODE], provider=MockProvider(findings=[...])[, github=FAKE])` in process, as P2-S2's, P2-S5's and P2-S9's suites do. `FAKE` is P2-S5's `GitHub` wrapper over `review_bot.pr.fake_github.FakeGitHubClient`. "Either mode" is checked by running AC1, AC2, AC5, AC6 and AC7 in both `shadow` and `comment`.
- **Each repository** is a real git repository in `tmp_path` (P2-S2's `GitRepo`). It has a base commit on `main` with `src/base.py` and the CODEOWNERS files under test, then a head commit on `feature`, left checked out so the working tree holds the head's CODEOWNERS. A symbolic link is staged in git's index as mode `120000`, as P2-S9 does, so no symlink support is needed on disk. A directory at a location is a file beneath it (`.github/CODEOWNERS/inner`). The head commit stages only its own files.
- **Findings** are high `logic` AI findings on lines the head adds, one per line. Owners are read per file from every `comments` and `withheld` entry.
- **`shadow/v1`** (spec Interfaces): `codeowners` is appended after `config`, and `owners` joins each comment and withheld entry. P2-S1's `load_shadow` pins exact key sets, so its lists were extended (see Earlier suites changed). `codeowners` is compared whole: `{"source": "<path>@<base SHA>", "status": "found", "rules": N, "invalid_lines": M}`. For `too_large` and `invalid`, only `status`, `rules` 0 and `invalid_lines` 0 are checked, not `source`. For `not_a_file`, the whole object is checked in AC2's test, with `source` naming the first non-file (spec Notes).
- **The summary** is `shadow.json`'s `summary_body`, parsed with P1-S11's parser. In comment mode the posted summary (`FakeGitHubClient.summary`) is checked too. The `Owners:` section is the list right after the line whose visible text is `Owners:`, bold or not. That line may end the paragraph before it, as `Withheld findings:` does. Each item's visible text is `<owner>: <count>`, with the owner as its only code span. The owners are followed by `and K more`, then `no owner: N`, both as list items.
- **AC7's check** is P2-S1 AC11's: no `@` (so no mention and no email) and no link outside a code span. "Bodies unchanged" compares each comment's body with the same PR's run without CODEOWNERS. In comment mode it also compares each posted body.
- **"Isn't read"** (AC8) is observed by wrapping `subprocess.Popen.__init__`, as P3-S1 did. Each refusal (`action`, `fork`, `no_credential`) runs on two copies of a PR, one whose base has `.github/CODEOWNERS` and one without it. Both must start the same git commands (repository paths normalised), and none may name CODEOWNERS. `action` and `fork` must start no git at all, and `codeowners` must be null or `absent` with no rules.
- **3 MB** is read as either 3,000,000 bytes or 3 MiB: the tests use 3,300,000 bytes (over both) and 2,900,000 bytes (under both).

### Criteria

Test names drop the `test_p3_s11_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_last_matching_line_gives_the_owners` ×2 (modes; the spec's example). `ac1_an_earlier_line_doesnt_win_over_a_later_broader_one` | pass (3) |
| AC2 | `ac2_only_the_first_existing_location_is_read` ×6 (all three, root and docs, docs only; both modes). `ac2_first_location_with_no_rules_still_hides_the_others`. `ac2_head_changing_codeowners_doesnt_change_its_own_routing`. `ac2_head_adding_a_higher_location_is_ignored`. `ac2_head_deleting_codeowners_doesnt_remove_the_owners`. `ac2_head_adding_codeowners_to_a_repo_without_one_is_ignored`. `ac2_non_file_location_is_skipped_for_the_next` ×6 (a directory, then root; a symlink, then root; a symlink and a directory, then `docs/`; both modes). `ac2_no_regular_file_at_any_location_is_not_a_file` ×3 (a directory alone, a symlink to a valid file alone, and non-files at every location) | pass (20) |
| AC3 | `ac3_pattern_matches_as_github_documents` ×9: `/docs/`, `logs/`, `apps/**` (not `src/apps/x.py`), `**/build` (at depth 1 and 4), `*.js`, `docs/*` (not `docs/x/a.py` or `src/docs/a.py`), `lib/util.py` and `src/*.py` (anchored by a middle `/`), and `**/api/views.py` (at the root and at depth). Each pattern is checked against paths it must and mustn't match | pass (9) |
| AC4 | `ac4_invalid_line_is_skipped_and_counted` ×10 (`!`, `[`, a bare word, `@org/a/b`, `@`, `@org/`, `alice@`, `@alice@example.com`, `<alice@example.com>`, one bad owner among good ones). `ac4_user_team_and_email_owners_are_kept` ×4. `ac4_pattern_without_owners_matching_last_gives_no_owners`. `ac4_comments_and_blank_lines_are_ignored_and_not_counted`. `ac4_all_invalid_kinds_together` | pass (17) |
| AC5 | `ac5_no_codeowners_is_absent_and_nobody_owns` ×2. `ac5_unusable_codeowners_has_its_status_and_no_owners` ×8 (over 3 MB, not UTF-8, a symlink, a directory; both modes; same exit and comments as without CODEOWNERS; time-bound). `ac5_just_under_3_mb_is_read` | pass (11) |
| AC6 | `ac6_every_comment_and_withheld_entry_has_its_owners` ×2 (10 comments, 3 withheld). `ac6_summary_counts_by_owner_then_name_then_no_owner` ×2 (counts that only add up with the withheld findings included, and a two-owner finding counted for each owner). `ac6_posted_summary_has_the_owners_section`. `ac6_more_than_10_owners_lists_10_then_and_k_more`. `ac6_exactly_10_owners_lists_all_10`. `ac6_no_codeowners_has_no_owners_section`. `ac6_codeowners_matching_no_finding_has_no_owners_section`. `ac6_no_findings_has_no_owners_section` | pass (10) |
| AC7 | `ac7_summary_has_no_mention_outside_a_code_span` ×2. `ac7_posted_summary_and_bodies_have_no_mention_outside_a_code_span`. `ac7_comment_bodies_are_the_same_as_without_codeowners` ×2. `ac7_hostile_owner_text_is_skipped_or_stays_in_code` ×4 (one or two backticks in an email, HTML in an email, `@_x_`). Each must be either skipped or shown in a code span, with no HTML | pass (9) |
| AC8 | `ac8_refused_run_doesnt_read_codeowners` ×3 (`action`, `fork`, `no_credential`) | pass (3) |
| Limits | `limits_many_double_star_rules_match_in_bounded_time`: 500 rules of 60 `**` segments against a 30-deep path. `limits_star_heavy_patterns_match_in_bounded_time`: 200 rules of 80 `*a` against an 80-`a` name. Both under 60 s | pass (2) |

### Failures

None.

### Earlier suites changed

- **`tests/integration/test_p2_s1_shadow_report_local.py`:** `SHADOW_KEYS` appends `codeowners`, and `COMMENT_KEYS` and `WITHHELD_KEYS` add `owners` (spec Interfaces; design.md's "Report keys, appended only"). Every suite that reads `shadow.json` through `load_shadow` needs this. No assertion was weakened. One P2-S1 case passes on the developer's tree, so local `shadow` runs carry the keys too.

### Readings for the designer

1. **`not_a_file`'s `source`** follows the spec's Build note: it names the first non-file found. `ac2_no_regular_file_at_any_location_is_not_a_file` checks the whole `codeowners` object, `source` included. An earlier Build note sentence, which said there was no fallback to the next location, contradicted the amended AC2. The designer has since removed it.
2. **AC6's "no owner: N" and "and K more"** are read as list items in the `Owners:` list, after the owners.
3. **AC6's "no owners at all"** is read as: no finding has an owner. So a CODEOWNERS that matches no finding shows no section, not even `no owner: N`.
4. **AC6's name order** is plain code-point order (`@org/api` before `alice@example.com`). The tests use only lowercase names, so case folding isn't decided.
5. **`rules`** counts valid lines, including a pattern with no owners (AC4's `/docs/`). Comments, blank lines and skipped lines aren't counted.
6. **AC4's owner forms** are tested only where they are clear-cut: `first.last@example.co.uk`, `@alice-b` and `@org/team-name` are expected to be kept. The spec doesn't decide edge forms such as `+` in an email or `_` in a user name. AC7's hostile cases accept either answer, as long as nothing escapes a code span. The Notes' length caps on owner parts aren't tested, as no criterion states them.
7. **Not tested:**
   - **`**/build` matching a root `build/` directory:** Phase 1 skips root `build/` as generated, so no finding can land there.
   - **`*.js` against a directory named `x.js`:** the spec doesn't cover it.
   - **`source` for files over 3 MB or not UTF-8**, and the git read's own limits (cap-plus-one, timeout), which need a git that misbehaves.

### Coverage

Not measured by the tester, who ran only this file. For coverage of `context.codeowners`, `pr.runner`, `pr.shadow` and `pr.comments`, see the main session's `pytest --cov`.

## P3-S2: Keep the tools inside the commit on hostile paths and repositories

Spec: [p3-s2-tool-confinement.md](slices/p3-s2-tool-confinement.md). Tests: [`tests/integration/test_p3_s2_tool_confinement.py`](../../../tests/integration/test_p3_s2_tool_confinement.py), 72 tests, 207 cases. AC14 and AC15 were added by the designer's 2026-11-09 amendment, and their 15 cases were written from it while the developer implemented them. The tests were written from the spec's criteria, Limits and Interfaces, and design.md ("Tool safety", "The tools", "Interfaces"), while the developer built the slice and before any of its code was read.

**Status: every criterion passes. All 207 cases pass, from this file alone, on the developer's uncommitted working tree** (base `020cec7`, after the developer's AC14 and AC15 work). Before the amendment, all 192 cases passed. With the lead's decision applied to P3-S1's suite (Readings 1), both P3 files passed together then: 316 passed (124 + 192).

**AC14 and AC15, history:** on the first run, before the developer's AC14 and AC15 code, 4 of 14 passed. The second run, after it, gave 3 failures. Two of them were assertions of mine that went beyond AC15, and I corrected them, not to match behaviour but to match the criterion:
- I had read "git processes bounded by the 100 files" as at most one process per file. The spec doesn't say that. The bound is now at most 5 per file plus 10 (510), still far below the 20,000 files. The developer's code starts 302 processes for 100 files, 3 per file.
- I had required outcome `ok` when a `path_glob` leaves few candidates. AC15 doesn't say that, and the outcome is `truncated` because of git's output cap. That expectation is now Readings 6, for the designer.

I added one case, `ac15_file_past_100_candidates_is_found_when_path_glob_names_it`, to test "after `path_glob`" directly. It failed at first: a file past git's output cap was lost before `path_glob` was applied. After a further change by the developer, a polling run passed all 15 AC14 and AC15 cases, and a full run of the file then passed all 207. No test was changed for that.

**Before the code:** against `020cec7`'s `src/` (P3-S1 only), 63 passed and 129 failed. The 63 are behaviour P3-S1 already had (symlinks and submodules refused, blobs over 1 MB refused with a capped read, literal grep with a bare `--`, exact tree-entry match, `../x`, `/etc/passwd` and `C:/x` refused) and the controls. **First run on the developer's tree:** 191 passed. The one failure was a test bug: `ac13_block_is_in_no_request_audit_record_or_report` required the line-15 token to be in no tool result, but the fixture puts the same token in `docs/other.txt`, whose line `grep` rightly returns. The check now leaves that one token out; every other block line is still checked, and `ac13_grep_for_a_line_inside_the_block_returns_nothing_from_it` still checks nothing comes from the block. No other test was changed.

### Gate

Only this file was run, as the coordinator asked. `ruff format --check` and `ruff check` are clean on it. For the full gate, see the main session's run.

### Seam and set-up (assumed)

- **The CLI**, exactly as P3-S1's suite drives it, reusing its `Harness`, `unwrap`, `numbered`, `grep_lines`, `assert_refused` and `assert_reported`: `review_bot.cli.main([DIFF, "--provider", "mock", "--audit-log", LOG, "--context-repo", DIR, *FLAGS], provider=MockProvider(tool_calls=[...], findings=[FINDING]))`. Each refusal is followed by a valid `read` that must work, then the finding, so every refusal also checks that the review goes on.
- **`RepoTools` directly** (design.md Interfaces), where a test must watch git itself: `RepoTools(RepoRef(git=GitRepo(DIR, timeout_s=T), commit=SHA), caps=ToolCaps(max_calls=12, max_result_bytes=16_000, max_total_bytes=96_000))`, then `await tools.call(name, arguments)`, reading `tools.ledger[i].reason`.
- **Repositories** are built with git plumbing only: `hash-object -w --stdin`, `mktree -z`, `commit-tree`, `update-ref refs/heads/main` (spec Notes). A symbolic link is a `120000` blob, a submodule a `160000` entry naming a real commit, and odd names are raw bytes in `mktree -z` input. Nothing is checked out, so every case runs on Windows with no skip. `HEAD` names `C`; its parent `OLD` holds `main.py`. `C` holds P3-S1's `billing/invoice.py` and `billing/checkout.py`, `main.py`, `README.txt`, and each test's own files.
- **"git isn't run"** (AC1) is observed by wrapping `subprocess.Popen.__init__`, as P3-S1 did: a call on a fresh `RepoTools` must start no git process. A control test checks the wrapper sees a valid read's git.
- **"At most 1 MB plus one byte is read"** (AC7) is observed through `tracemalloc`: refusing a 32 MiB blob must peak under 8 MiB of Python allocations. A read of the whole blob would hold 32 MiB.
- **"1 MB"** is read as either 1,000,000 bytes or 1 MiB: the tests use 1,048,577 bytes (over both) and exactly 1,000,000 bytes (not over either). AC9's line of `a` is 1,000,000 bytes with its line feed.
- **The timeout** (AC9) is produced by replacing every `git grep` process, at `Popen`, with a Python process that sleeps 90 s. The direct test builds `GitRepo(timeout_s=5)`. The CLI test sets `GIT_TIMEOUT_S` to 5 s in `review_bot.git` and, if it holds the name, `review_bot.cli`, as the CLI builds its `GitRepo` from that constant.
- **AC14's path** is `cfg_AKIA<16>.txt`, using the same test-only key as AC5, plus a directory variant `AKIA<16>/notes.txt` and a control file `docs/visible.txt`. All three hold the same marker. "Doesn't echo the path" is checked as: the refusal holds neither the key nor the file's text.
- **AC15's fixture** is the spec's own size: 20,000 distinct Latin-1 files, each holding the literal. Beside them are two UTF-8 files: `a_utf8.txt` sorts first, and `z_utf8.txt` sorts last, which is one more than the spec's "one UTF-8 file", so "after `path_glob`" can be tested. It is built once per module. Distinct blob contents stop an implementation from passing by caching on the blob's hash. The 20,000 blobs are written by one `git fast-import` that holds only blobs; the trees and the commit are still made with `mktree -z` and `commit-tree`. "Reads at most 100 candidate files" is checked two ways on one `RepoTools.call`. First, at most 5 git processes per file plus 10, so 510. Second, if the tools read files through `GitRepo.read_file`, at most 100 distinct calls to it. "Its withheld count says it covers only the files read" is read as: no number in the result's notes is over 100.
- **grep's argument list** (AC10) is the one git process whose argument list holds `grep`; everything after `-e` must be exactly `[literal, commit, "--"]`.
- **AC5's note** is read as a line of the `grep` body that isn't a match, containing `withheld` and the number `1` as a word.
- **AC8's C-quoted form** is Phase 1's: `"odd/new\nline.py"`, `"odd/esc\033name.py"`, `"odd/bell\a.py"`, `"odd/caf\377.py"`, quotes included.
- **AC11's canary** is a `#!/bin/sh` script in `tmp_path` that writes a trace file; the literal is `-O<script>`. The `-f` case points at a real file of patterns in `tmp_path` that would match a line of the commit, and `--no-index` has an untracked file beside the repository holding the literal.

### Criteria

Test names drop the `test_p3_s2_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_read_of_malformed_path_is_refused_invalid_path` ×14 (the spec's 13, plus a trailing `/`). `ac1_glob_pattern_is_refused_invalid_path` ×6 and `ac1_grep_path_glob_is_refused_invalid_path` ×6 (`../*`, `billing/../*.py`, `/etc/*`, `:(top)*`, `:!x`, `billing\*.py`). `ac1_git_is_not_run_for_an_invalid_path` ×26 (each of those on a fresh `RepoTools`, with no git process started). `ac1_the_git_watch_sees_a_valid_read` (control). `ac1_refusal_is_recorded_with_its_reason` | pass (54) |
| AC2 | `ac2_git_internals_are_denied` ×3 (the spec's paths, on a commit that really holds `sub/.git/HEAD` and `.GIT/config`, whose text mustn't be returned). `ac2_any_segment_named_git_is_denied` ×3 | pass (6) |
| AC3 | `ac3_reading_a_symlink_is_refused_symlink` ×2. `ac3_inner_symlink_target_text_is_not_returned`. `ac3_outer_symlink_target_text_is_not_returned`. `ac3_grep_leaves_out_symlinks` ×3 (literals matching each link's own text and the target's text). `ac3_grep_still_finds_the_link_target_itself`. `ac3_glob_leaves_out_symlinks_and_submodules` ×6. `ac3_glob_double_star_still_lists_the_regular_files`. `ac3_reading_a_submodule_is_refused_submodule`. `ac3_grep_leaves_out_the_submodule` | pass (16) |
| AC4 | `ac4_reading_a_secrets_file_is_refused_denied_path` ×13 (the spec's list). `ac4_design_deny_list_is_refused_denied_path` ×14 (design.md's other names and case variants). `ac4_grep_returns_no_line_from_a_secrets_file` (exactly the readable files' lines). `ac4_glob_double_star_does_not_list_a_secrets_file` (exactly the readable files). `ac4_glob_aimed_at_secrets_files_lists_none` ×5. `ac4_public_key_and_lookalikes_are_readable` ×6 (`id_ed25519.pub` and 5 look-alikes) | pass (40) |
| AC5 | `ac5_window_overlapping_the_key_is_refused_possible_secret` ×6 (whole file, 7 to 7, 5 to 9, 1 to 7, 7 onwards, 6 to 7). `ac5_lines_1_to_5_are_returned`. `ac5_window_after_the_key_is_returned`. `ac5_grep_drops_the_key_line_and_notes_one_withheld`. `ac5_grep_for_the_key_itself_returns_nothing`. `ac5_refusal_is_recorded_possible_secret`. `ac5_key_is_in_no_request_audit_record_or_report` (prompts, every tool result, the audit log, the JSON report, stderr) | pass (12) |
| AC6 | `ac6_reading_a_binary_or_non_utf8_file_is_refused_binary` ×2 (a NUL byte; Latin-1). `ac6_grep_does_not_search_binary_or_non_utf8_files` (git's `-I` alone wouldn't skip the Latin-1 file). `ac6_file_with_valid_utf8_beyond_ascii_is_readable` (control) | pass (4) |
| AC7 | `ac7_blob_over_1_mb_is_refused_too_large`. `ac7_window_of_a_blob_over_1_mb_is_refused_too_large`. `ac7_blob_of_exactly_1_000_000_bytes_is_readable`. `ac7_at_most_1_mb_plus_one_byte_of_a_large_blob_is_read` (32 MiB blob, peak under 8 MiB). `ac7_listing_over_50000_entries_is_refused` ×2 (`glob`, `grep`; 50,001 root entries; a `read` after still works; under 60 s). `ac7_listing_of_exactly_50000_entries_is_searched` | pass (7) |
| AC8 | `ac8_glob_lists_odd_names_in_c_quoted_form` ×2 (`odd/*`, `**`). `ac8_no_raw_control_character_reaches_a_result` ×3 (two globs and a grep; no raw control character and no U+FFFD). `ac8_grep_lists_the_plain_name_beside_the_odd_ones`. `ac8_reading_an_odd_name_is_refused_invalid_path` ×7 (3 raw names, 4 quoted forms) | pass (13) |
| AC9 | `ac9_catastrophic_regex_literal_completes_and_is_literal` (a 1,000,000-byte line of `a`; only the line holding `(a+)+$` matches; outcome `ok`). `ac9_git_timeout_refuses_that_call_and_the_next_works` (direct). `ac9_git_timeout_in_a_review_is_refused_and_the_review_goes_on` (CLI) | pass (3) |
| AC10 | `ac10_grep_argv_is_literal_commit_then_bare_double_dash` ×4 (no `path_glob`, and three globs that must appear in no argument). `ac10_path_glob_is_applied_to_the_results_in_our_code` ×3 | pass (7) |
| AC11 | `ac11_option_like_literal_is_found_as_plain_text` ×4 (the spec's literals). `ac11_pager_option_runs_no_program` (canary). `ac11_pattern_file_option_reads_no_file_outside_the_commit` (a real pattern file). `ac11_no_index_searches_only_the_commit` (an untracked file) | pass (7) |
| AC12 | `ac12_path_differing_from_the_tree_entry_is_not_found` ×8 (case of file, directory and suffix; leading or trailing space; a trailing `.`; a prefix; NFD for NFC). `ac12_exact_path_is_read` (control) | pass (9) |
| AC13 | `ac13_window_overlapping_the_block_is_refused_possible_secret` ×6 (1 to 12, 15 to 15, 18 to 30, the `BEGIN` line, the `END` line, the whole file). `ac13_lines_1_to_9_are_returned`. `ac13_grep_for_a_line_inside_the_block_returns_nothing_from_it` (the same string in another file is still found). `ac13_grep_for_the_block_markers_returns_nothing_from_it` ×2. `ac13_block_is_in_no_request_audit_record_or_report` | pass (11) |
| AC14 (amended 2026-11-09) | `ac14_glob_leaves_out_a_path_holding_a_secret` ×4 (`**`, `*.txt`, `cfg_*`, `**/notes.txt`). `ac14_glob_double_star_lists_exactly_the_other_files`. `ac14_grep_leaves_out_a_path_holding_a_secret` (only the control file's line). `ac14_read_is_refused_possible_secret_without_echoing_the_path` ×2 (`cfg_AKIA…txt`, and a key as a directory name, `AKIA…/notes.txt`). `ac14_path_and_key_are_in_no_result_report_or_audit` (prompts, every tool result, the audit log, the JSON report and its `files_read`, stderr) | pass (9) |
| AC15 (amended 2026-11-09) | `ac15_one_grep_starts_git_bounded_by_100_files` (at most 510 git processes and at most 100 distinct `GitRepo.read_file` calls, under 60 s). `ac15_grep_over_100_candidates_is_truncated_with_a_note` (only `a_utf8.txt`'s line, outcome `truncated`, a note). `ac15_withheld_count_covers_only_the_files_read` (no number in the notes over 100). `ac15_candidates_are_counted_after_path_glob` (a `path_glob` naming `a_utf8.txt`: its line). `ac15_file_past_100_candidates_is_found_when_path_glob_names_it` (a `path_glob` naming `z_utf8.txt`, after 20,000 Latin-1 matches: its line). `ac15_path_glob_with_few_candidates_reads_few_files` (10 Latin-1 candidates: no line, at most 60 git processes) | pass (6) |
| Limits | `limits_secret_scan_of_a_near_1_mb_blob_is_bounded` (25,000 near-miss key prefixes and PEM-like headers, read and searched, under 60 s). `limits_digest_of_inputs_is_recorded_not_the_paths` | pass (2) |

### Failures

None. The AC15 failure seen during the developer's work, a `path_glob` naming a file past git's output cap and getting no line back, is fixed on the current tree (see Status).

### Readings for the designer

1. **Conflict with P3-S1, decided.** S2 AC1 says a 1,001-character path is refused `invalid_path`. P3-S1's suite expected `invalid_arguments` for a `read` path over 1,000 characters. On the developer's tree, which follows S2, that S1 case failed with `refused: invalid_path`. **Decision (lead, to be checked by the reviewer):** S2 owns path validation and supersedes S1 here. P3-S1's case moved out of the `invalid_arguments` table into its own test, `test_p3_s1_ac5_read_path_over_1000_is_refused_invalid_path_and_the_review_goes_on`, which expects `invalid_path`. I searched S1's suite for other cases that S2 moves from `invalid_arguments` to `invalid_path` and found none. `grep_path_glob_over_1000` stays `invalid_arguments`: S2 AC1 refuses a glob for `..`, a leading `/` or `:`, or a backslash, not for its length. The 1,000-character `read` path stays `not_found`. Type, key and literal errors are unchanged.
2. **Strictness beyond the spec's examples, flagged for review:**
   - `billing/` (a trailing `/`) is expected `invalid_path`, as design.md forbids an empty segment.
   - `.git` alone, `sub/.Git/config` and `a/b/.git` are expected `denied_path` ("any path segment named `.git`", case-insensitive).
   - design.md's whole deny-list is tested, not only the spec's 13 names, including case variants (`ID_RSA`, `config/.ENV.local`, `aws/Credentials.INI`). Look-alikes the list doesn't cover are expected readable: `pem.py`, `.envrc`, `env/settings.py`, `credentials_test.py`, `keys.txt`.
   - A `read` window after the key (lines 8 to 12) is expected to be returned, as only an overlapping window is refused. The PEM block's `BEGIN` and `END` lines are each part of its span.
   - AC12 adds a trailing or leading space, a trailing `.`, a prefix of the name, case changes in the directory or suffix, and an NFD spelling of an NFC name, with `core.ignorecase` and `core.precomposeunicode` set to `true` as Windows and macOS do.
3. **Symlinks in `grep`:** git grep searches a link's own text, so a link whose target is `billing/invoice.py` would match the literal `billing/invoice.py`. The test checks that no link path appears in any result.
4. **`.git`-named entries planted in a commit** (`sub/.git/HEAD`, `.GIT/config`) are only checked through `read`. No criterion says `grep` and `glob` must leave them out, so they aren't tested there.
5. **Not tested:**
   - Malformed git output stopping only that call (Limits): it needs a git that writes bad output, which would mean faking git rather than driving it.
   - That the secret scan runs once per file and commit, not once per call: it isn't observable through the public interfaces. Its linear time is checked only as a bound (a near-1 MB blob of near-miss key prefixes and PEM-like headers, read and searched within 60 s).
   - Byte counts read from git for `grep` and `ls-tree` output beyond their caps: only the blob's cap-plus-one is checked.
   - AC15's withheld note wording: the spec doesn't give it. The test only checks that no number in the notes is over 100.
6. **AC15, the note when `path_glob` leaves few candidates.** With `path_glob` `a_utf8.txt` (one candidate) or `l0000*.txt` (10 candidates), the developer's code returned, before the fix described in Status, outcome `truncated` and the note "more lines match than are shown; narrow the search with path_glob or a longer literal". AC15 doesn't say these calls must be `ok`, so no test requires it. But the note is misleading: the model already narrowed with `path_glob`. Its cause is the same as the failure above. The designer should say whether truncation by git's output cap counts toward AC15's "stops there with the truncated note", or whether only the 100-candidate cap may truncate. I haven't rechecked this note since that fix.
7. **AC14, untested variants:** only the AWS key format is used in a path. A key split across two path segments isn't tested, as the spec says "a path in which the secret scan finds anything", and the scan runs on the whole path.

### Coverage

Not measured by the tester, who ran only this file. For coverage of `context.paths`, `context.tools` and `git`, see the main session's `pytest --cov`.

## P3-S3: Let Claude drive the tools, locked down and cost-capped

Tests are in `tests/integration/test_p3_s3_claude_drives_tools.py` and `tests/conformance/test_p3_s3_tool_loop_conformance.py`, with helpers in `tests/fake_mcp.py`. They were written from the spec, design.md's "Tool safety", "Spend and caps" and "Interfaces", and ADR 0015, before the provider's code was read. They were then updated to the designer's amendments of 2026-11-09 (AC5, AC6, AC8, AC10, Limits, Interfaces) and to reviewer round 1. AC9 moved to P3-S16 and isn't tested here.

### Gate

All green:
- `ruff format --check`, `ruff check` and `mypy`.
- `pytest --cov`: 6,103 passed, 2 skipped, after the round-1 fixes.
- Total coverage 99.38%.
- The last test added, `limits_huge_unexpected_tool_names_give_a_bounded_message`, was written while that run was going. It passed when this slice's files were re-run alone (259 passed).

### Seam and set-up (assumed)

- `ClaudeAgentProvider` runs over `fake_sdk.FakeSdk`, so no CLI is launched. In context mode, each script starts with the CLI's init `SystemMessage`, which lists exactly the three tools unless the test is about that list.
- **The tools are driven the CLI's way.** `fake_mcp.tool_call` sends `initialize`, then `tools/call`, to the `repo` server named in `options.mcp_servers`, through the SDK's own `SdkMcpBridge`. `list_server_tools` sends `tools/list` the same way.
- **The hook is called the CLI's way.** It gets a `PreToolUse` input carrying `tool_name`. A deny means `hookSpecificOutput.permissionDecision == "deny"` with a reason. The matcher must be empty or `*`, so the hook runs for every name.
- **Executors.** The real `RepoTools` runs over a one-commit repository holding `billing/checkout.py`. `RecordingTools` records what reaches it.
- **Through `main`.** These tests use `--provider claude`, with `--context-repo <repo>` in context mode, and the provider injected. They read the report with `--format json` and the audit log with `--audit-log`.

### Criteria

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_system_prompt_is_the_context_prompt` (and not diff mode's). `ac1_builtin_tools_are_off`. `ac1_allowed_tools_are_exactly_the_three_full_names`. `ac1_only_the_in_process_repo_server_is_configured`. `ac1_repo_server_offers_exactly_read_grep_and_glob` (`tools/list` through the bridge). `ac1_strict_mcp_config_no_settings_no_agents_dont_ask`. `ac1_max_turns_is_max_tool_calls_plus_2` ×3 (default, 12, and 5 giving 7). `ac1_hooks_hold_only_one_pre_tool_use_hook` (one matcher for every name, one callback). `ac1_env_is_the_same_as_diff_mode` ×2. `ac1_env_stays_exact_with_a_key_in_the_environment`. `ac1_stderr_is_still_held_by_the_provider` | pass (14) |
| AC2 | `ac2_diff_mode_options_are_phase_0s_exactly`: every `ClaudeAgentOptions` field is Phase 0's or the SDK default. `ac2_budget_is_the_cap_rounded_down_to_the_cent` ×16 (8 values in each mode: `0.309`→`0.30`, `0.01`, `0.019999`→`0.01`, `0.999`→`0.99`, `0.60`, `1`, `2.5`, `999.999`→`999.99`; the value is a `float` and never above the cap). `ac2_no_budget_without_the_flag` ×2. `ac2_variable_sets_the_budget` ×2. `ac2_diff_mode_with_a_budget_changes_nothing_else`. `ac2_diff_mode_through_main_stays_locked_down`. `test_p0_s2_lockdown.py` passes unchanged | pass (23) |
| AC3 | `ac3_read_is_recorded_in_the_executors_ledger`. `ac3_fake_receives_the_executors_text` (equal to a fresh `RepoTools`' text for the same call). `ac3_findings_of_the_final_result_are_returned`. `ac3_arguments_reach_the_executor_unchanged` ×5 (read, grep, glob, a `..` path, an unknown key). `ac3_refusal_text_is_passed_back_as_the_result`. `ac3_several_calls_are_recorded_in_order`. `ac3_review_through_main_reads_and_reports` (one `context.tool_call` record) | pass (11) |
| AC4 | `ac4_hook_denies_other_tools_naming_the_allowed_ones` ×9 (the spec's names; the reason holds all three full names). `ac4_hook_denies_lookalikes_of_the_allowed_names` ×14 (bare `read`, `grep` and `glob`; changes of case, spacing or suffix; `mcp__repo__write`; `mcp__repox__read`; an empty name). `ac4_hook_does_not_deny_the_three` ×3 | pass (26) |
| AC5 | `ac5_cap_hit_is_permanent_naming_its_flag_and_cap` ×8 (context mode; turns and cost; the result alone, with `is_error` false, holding valid findings, and followed by the SDK's `ResultError`). `ac5_cost_cap_hit_in_diff_mode_is_the_same_error`. `ac5_turn_cap_hit_in_diff_mode_is_phase_0s_permanent_error`: cap None, no `--max-tool-calls`, the same text as Phase 0's permanent error, and no planted text. `ac5_message_holds_no_cli_or_model_text` ×2. `ac5_cap_hit_through_main_exits_permanent_naming_the_flag` ×2 (exit 12; neither stream holds the planted text). `ac5_audit_failed_record_carries_the_cap` ×2. `ac5_audit_failed_record_has_a_null_cap_for_other_errors`. `ac5_audit_failed_record_has_a_null_cap_for_diff_mode_turns`. `ac5_every_other_error_has_no_cap` ×7. `ac5_a_provider_error_has_no_cap_by_default` | pass (26) |
| AC6 | `ac6_cost_is_the_exact_decimal_of_the_reported_value` (`Decimal("0.1234")`, not the binary expansion). `ac6_cost_is_read_in_diff_mode_too`. `ac6_report_usage_cost_is_the_string`. `ac6_report_usage_cost_in_context_mode`. `ac6_report_usage_cost_is_null_in_context_mode_when_unknown`. `ac6_audit_called_record_has_the_cost_in_diff_mode` ×3 and `…_in_context_mode` ×3 (the string, or a present null). `ac6_unusable_cost_is_none_and_the_review_succeeds` ×12 (negative, −1, NaN, ±infinity, `true`, `false`, text, numeric text `"0.1234"`, a list, a mapping, `None`). `ac6_absent_cost_is_none`. `ac6_unusable_cost_is_null_in_the_report` ×12 (the key present). `ac6_mock_provider_reports_a_null_cost` | pass (37) |
| AC7 | `ac7_tool_loop_returns_the_scripted_findings` ×2 (mock, claude). `ac7_tool_loop_leaves_one_read_in_the_ledger` ×2 (one `ok` `read` with a span on `billing/checkout.py`). The suite's new `tool_loop` scenario is in `tests/conformance/conftest.py` | pass (4) |
| AC8 | `ac8_bad_max_cost_exits_2_naming_the_flag` ×15 (the spec's `0`, `0.009`, `-1`, `1000.01`, `nan` and `abc`, plus `1000.001`, `1e4`, `0.0099999`, `-0.01`, `inf`, `-inf`, empty, `0.00`, and 10^40; each a `review-bot: error:` line that isn't argparse's). `ac8_bad_variable_exits_2_naming_it` ×6. `ac8_bad_max_cost_reaches_no_provider`. `ac8_bounds_are_accepted` ×3 (`0.01`, `1000`, `1000.00`). `ac8_unset_by_default`. `ac8_bounds_run_with_that_budget` ×2 (`0.01`, `1000.0`) | pass (28) |
| AC10 | `ac10_unexpected_tool_list_fails_permanent_naming_them` ×6 (with `Bash`, without `mcp__repo__grep`, with `mcp__other__read`, with `ToolSearch`, the built-ins only, empty). `ac10_no_tool_handler_runs_after_a_bad_list` ×6. `ac10_tool_call_after_a_wrong_list_is_refused_without_running` (the server is called after the review failed; the executor isn't). `ac10_second_init_listing_bash_after_a_good_one_fails`: a read runs after the first, good init; the second init lists `Bash` and fails `permanent`; a later server call doesn't reach the executor. `ac10_exactly_the_three_in_any_order_proceed`. `ac10_bad_list_through_main_exits_permanent`. `ac10_tool_list_is_kept_in_the_agent_tools_record`. `ac10_context_stream_without_init_message_proceeds`. `ac10_no_init_message_has_no_agent_tools_record`. `ac10_diff_mode_neither_checks_nor_records_the_list` ×3 (with `Bash`, built-ins, the three). Missing or non-list init lists are under Limits | pass (22) |
| Limits | `limits_hook_denies_a_tool_name_that_isnt_text` ×4 (`None`, a number, a list, a mapping). `limits_init_tool_list_that_isnt_a_list_fails_permanent` ×3 (missing, text, a mapping). `limits_invalid_arguments_reach_the_executor_and_its_ledger` ×4 (an unknown key, `path` 5, `line_count` 401, no `path`: each gets the executor's own delimited refusal and its ledger entry). `limits_malformed_calls_count_towards_max_tool_calls` (two malformed calls under a cap of 2, then a valid `read` refused `call_limit`). `limits_huge_unexpected_tool_names_give_a_bounded_message`: a 100,000-character name and 1,000 other names; no more than 100 characters of the long name appear | pass (13) |

### Failures

None. One failure found during the run is now fixed:
- **What it was:** the first build used the SDK's `create_sdk_mcp_server`, which checks arguments against the schema before the handler runs. A malformed call got the SDK's undelimited `Input validation error: …`, never reached the executor, and was missing from the ledger and the audit log (`limits_invalid_arguments_reach_the_executor_and_its_ledger`, 4 red).
- **The fix:** the developer replaced it with a hand-built `mcp` server.
- **The spec:** the designer folded the fix into Limits.

### Earlier suites changed

- **`tests/integration/test_p1_s10_json_output.py`:** two exact `usage` assertions now also expect `"cost_usd": None`, as AC6 makes the key always present. Their providers (a usage-only stub, and a run with every file skipped) report no cost. The assertions stay exact.
- **`tests/conformance/conftest.py`:** a `tool_loop` scenario was added to both factories, for AC7. `mock_provider` is now driven by `MOCK_SCRIPTS`, as `claude_provider` is by `CLAUDE_SCRIPTS`, so `tool_loop` is one more entry rather than a seventh early return (reviewer round 1). The existing scenarios build the same providers.

### Readings for the designer

1. **AC5, a diff-mode `error_max_turns` result whose `is_error` is false.** It gets Phase 0's reading of the result text: `invalid_output`, or success if the text is valid findings. It doesn't get the permanent error. The test covers only `is_error` true, which is how the SDK documents a capped run. In context mode, the same shape fails with cap `turns`, as tested.
2. **AC2's "never above the cap"** is checked at the float's shortest decimal form (`repr`). No binary float equals 0.01 exactly, so a check on the exact binary value would fail the spec's own accepted `0.01`.
3. **AC4's "naming the allowed tools"** is read as naming all three full names (`mcp__repo__read` and the others).
4. **Strictness beyond the spec, flagged for review:**
   - A hook `tool_name` that isn't text must be denied, not raise.
   - The hook's matcher must cover every name.
   - A long unexpected tool name is shown cut to 100 characters (the developer's round-1 fix).
   - A context-mode cap-hit result whose `is_error` is false, or which holds valid findings, still fails with its cap.
   - `1e4` and 10^40 must exit 2.
5. **Not tested:**
   - The real CLI building its init tool list from `tools=[]` plus the MCP server, and honouring `max_budget_usd`. Both are S16's live smoke.
   - Whether the real CLI itself checks arguments against the advertised schema before calling the server. If it does, a malformed call would never reach our server; only S16 can show it.
   - `main` building its own Claude provider with `--context-repo`. That would reach the real SDK, which the suite's guard stops.

### Coverage

From the full `pytest --cov` run:
- `providers/claude_tools.py`, `providers/claude_agent.py`, `settings.py`, `report.py`, `render.py` and `review.py`: 100% (omitted by `skip_covered`). The branch `claude_tools.py` 53->55, missed in the first run, has been covered since the round-1 change to the init check.
- Total: 99.38%.

## P3-S4: See the context behind every finding

Spec: [p3-s4-finding-evidence.md](slices/p3-s4-finding-evidence.md). Tests: [`tests/integration/test_p3_s4_finding_evidence.py`](../../../tests/integration/test_p3_s4_finding_evidence.py), 104 cases. They were written from the spec's criteria, Limits and Interfaces, and design.md ("Interfaces", "Prompt versions", "Report keys, appended only"), while the developer built the slice and before any of its code was read.

**Status: every criterion passes. All 104 cases pass, from this file alone, on the developer's uncommitted working tree** (base `6380d95`).

**Before the code:** with only `src/review_bot/findings.py` changed (the schema accepts `evidence`), 27 passed and 77 failed, each on a missing feature: no `evidence` in the report or the shadow entries, no `evidence_kept` in the `verify` record, no `is_diff_head` on `ReviewContext`, no `Context:` line, `prompt_version` still `p3-v1`. The 27 were AC5's schema checks and controls, and checks that hold while nothing renders evidence. **On the developer's later tree:** 102 passed. The 2 failures were a test bug: `assert_context_after_rationale` looked for the raw rationale `The total is computed twice.`, but the body escapes Markdown punctuation (`twice\.`), as P2-S1 requires. The helper now matches the line's visible text, as parsed by P1-S11's parser. No assertion changed. Then all 104 passed.

### Gate

Only this file and the earlier suites it changes were run, as the coordinator asked. `ruff format --check` and `ruff check` are clean on all four changed test files. This file gives 104 passed. The changed earlier suites, `test_p1_s10_json_output.py`, `test_p2_s1_shadow_report_local.py` and `test_p3_s1_local_review_with_repo_tools.py`, give 295 passed and 2 skipped (no `/dev/full` on Windows). Before the developer's code, those three gave 30 failures, all on the keys and prompt version S4 adds. For the full gate, see the main session's run.

### Seam and set-up (assumed)

- **The CLI**, as P3-S1's suite drives it, reusing its `make_repo`, `unwrap`, `numbered` and `grep_lines`: `review_bot.cli.main([SAMPLE, "--provider", "mock", "--audit-log", LOG, "--format", "json", ("--context-repo", DIR,) *FLAGS], provider=MockProvider(tool_calls=[...], raw_output=JSON))`. The model's answer is raw JSON, so it goes through `parse_findings`. A real model's answer goes through the same strict parse, which is where AC5's schema lives. The finding anchors on the sample's added line `billing/invoice.py:14`.
- **The model's span** is `{"file", "start_line", "end_line"}`, in each finding's optional `evidence` list (spec Interfaces). The report's finding entries and `shadow/v1`'s comment entries carry two keys. `evidence` is a list of those objects, with exactly those keys. `evidence_dropped` is an int.
- **The sample diff's lines**, read with `parse_diff`: the diff changes only `billing/invoice.py`. Its new-side lines are 12 to 14 (context, context, added), then 30 to 41 (context at 30, 31 and 37, added otherwise). Lines 15 to 29, and 42 onwards, are outside the diff.
- **P3-S1's repository** holds `billing/invoice.py` (50 lines) and `billing/checkout.py` (30 lines). Checkout's line 12, the spec's line, is `charged = invoice.total() * (1 + invoice.tax_rate)`, and grep finds it only there.
- **PR mode** (AC4) runs through `review_diff` directly, as the spec's Interfaces say: `ReviewContext(repo=RepoRef(git=GitRepo(DIR, timeout_s=30), commit=C), is_diff_head=True)`, imported from `review_bot.review`, `review_bot.context.tools` and `review_bot.git`. Kept spans come from each reported model finding's `Finding.evidence` (`file`, `start_line`, `end_line`). The drop count comes from the run's `verify` `checked` record (AC8). "Otherwise" is checked three ways: with `is_diff_head=False`, with the field left out, and through the CLI.
- **Comment bodies** (AC7) come from `review-bot-pr run` in shadow and comment mode, through P3-S11's `make_pr` and `run_pr` and P2-S5's fake GitHub.
  - Until S8 puts PR runs in context mode, a PR run is a diff-mode run. So a span is kept if its lines are in the PR's diff (AC6).
  - The head adds `billing/invoice.py` as a new 20-line file, so lines 1 to 20 are added lines.
  - Findings with spans are built with `Finding.model_validate({..., "evidence": [...]})`.
  - Shadow bodies are read from `shadow.json`'s `comments[].body`, and comment-mode bodies from the `post_review` call.
- **The `Context:` line** is the one body line that starts `Context: `.
  - It is parsed alone as CommonMark, with P1-S11's parser. Its `code_inline` contents, in order, must each be `path:N` or `path:A-B`.
  - Only spaces, commas and semicolons may sit between the spans.
  - It must come after the line holding the rationale and before the last line, which must be the marker.
- **The `verify` record** is the audit log's record with `stage` `verify` and `event` `checked`. The model finding's record is the one with `file` `billing/invoice.py` and `line` 14.
- **The prompt version** is pinned at `p3-v2`. Main holds `p3-v1` (P3-S1), and no merged slice has bumped it since. If another slice that bumps the context prompt merges first, the pin moves on by one (design.md, Prompt versions).

### Criteria

Test names drop the `test_p3_s4_` prefix.

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_span_inside_a_read_of_lines_1_to_30_is_kept`. `ac1_span_of_the_whole_read_is_kept`. `ac1_span_shown_by_two_adjoining_reads_is_kept` (lines 5 to 20, shown by reads of 1 to 10 and 11 to 30) | pass (3) |
| AC2 | `ac2_grep_match_precondition_is_line_12_only`. `ac2_span_of_the_grep_matched_line_is_kept`. `ac2_span_beyond_the_grep_matched_line_is_dropped` ×5 (11 to 13, 11 to 12, 12 to 13, 11, 13). `ac2_kept_and_dropped_spans_in_one_finding` | pass (8) |
| AC3 | `ac3_span_of_a_file_no_tool_returned_is_dropped`. `ac3_span_of_a_file_only_listed_by_glob_is_dropped`. `ac3_span_past_the_lines_returned_is_dropped` ×4 (around a read of 10 to 14). `ac3_span_past_the_end_of_the_file_is_dropped`. `ac3_span_of_a_refused_not_found_read_is_dropped`. `ac3_span_of_a_read_refused_over_the_call_limit_is_dropped` (a file that exists). `ac3_truncated_read_keeps_its_last_returned_line`. `ac3_truncated_read_counts_only_the_lines_it_returned` ×4: 400 lines of about 78 bytes, against the 16,000-byte default, with the last returned line read from the result. Each checks that the finding is kept and its `evidence_dropped` is 1 | pass (14) |
| AC4 | Through the CLI, whose `--context-repo` isn't known to be the diff's head: `ac4_cli_context_repo_ignores_ledger_lines_of_a_changed_file`. `ac4_cli_context_repo_keeps_added_and_context_lines` ×4. `ac4_cli_context_repo_drops_a_span_leaving_the_diff_even_if_read` ×4. `ac4_cli_context_repo_ignores_ledger_lines_even_at_head` (`--context-rev HEAD`). Through `review_diff`: `ac4_pr_mode_counts_diff_lines_and_lines_a_tool_returned` ×5 (a span partly in the diff and partly read, the union 12 to 20, read lines only, a context line, diff lines only). `ac4_pr_mode_drops_lines_neither_in_the_diff_nor_returned` ×4. `ac4_otherwise_ledger_lines_of_a_changed_file_dont_count` ×2 (`is_diff_head` false, and left out). `ac4_pr_mode_leaves_ac1_to_ac3_unchanged_for_other_files` | pass (22) |
| AC5 | `ac5_malformed_evidence_in_diff_mode_exits_13` ×11 (6 spans, start after end, start 0, end 0, a negative line, 4 good spans and 1 bad, an empty `file`, a missing `end_line`, an extra key, a line given as a string, `evidence` that isn't a list). `ac5_malformed_evidence_in_context_mode_exits_13` ×4. `ac5_one_bad_finding_rejects_the_good_ones_too`. `ac5_valid_evidence_is_accepted` ×4 (left out, `[]`, exactly 5 spans, line 1). Each rejection checks exit 13 and an empty stdout | pass (20) |
| AC6 | `ac6_report_keeps_spans_in_the_models_order` (5 spans, unsorted, one dropped in the middle). `ac6_each_span_has_exactly_file_start_line_and_end_line`. `ac6_every_finding_has_evidence_and_evidence_dropped` (rule findings too: `[]` and 0). `ac6_diff_mode_checks_spans_against_the_diff_alone`. `ac6_diff_mode_evidence_is_empty_when_the_model_gives_none` ×2. `ac6_context_mode_evidence_is_empty_when_the_model_gives_none`. `ac6_text_output_is_unchanged_by_evidence` | pass (8) |
| AC7 | `ac7_body_has_a_context_line_of_code_spans_after_the_rationale` ×2 (shadow and comment mode: `path:10-14`, then `path:12`). `ac7_body_without_kept_spans_has_no_context_line` ×2. `ac7_body_whose_spans_were_all_dropped_has_no_context_line`. `ac7_dropped_spans_are_left_out_of_the_context_line`. `ac7_path_with_a_backtick_stays_one_code_span` (a diff path holding a backtick) | pass (7) |
| AC8 | `ac8_checked_record_counts_kept_and_dropped_spans` (2 kept, 2 dropped). `ac8_every_checked_record_has_both_counts` (rule findings' records too). `ac8_finding_citing_nothing_counts_zero`. `ac8_diff_mode_records_have_the_counts`. `ac8_no_span_text_or_unseen_path_reaches_the_log`: neither the cited lines' text nor the model's unseen path appears in the log, and the record holds no `evidence` list | pass (5) |
| AC9 | `ac9_context_prompt_asks_for_evidence` (it names `evidence`, `start_line` and `end_line`). `ac9_context_prompt_version_is_the_next_free_number` (in the report and in the `run` `start` record). `ac9_diff_mode_prompt_is_byte_for_byte_phase_1s` (P3-S1's goldens) | pass (3) |
| Interfaces | `interfaces_shadow_comment_entries_have_evidence` | pass (1) |
| Limits | `limits_path_kept_only_if_it_equals_a_returned_path` ×9 (`./`, `//`, a case change, a backslash, a leading `/`, a leading space, a trailing space, `/./`, `lib/../`): each is dropped and not echoed. `limits_unseen_path_appears_in_no_output` (stdout, stderr and the log). `limits_huge_span_is_dropped_quickly_in_context_mode`, `..._in_diff_mode` and `..._in_pr_mode`: spans that end at line 10^12 must be dropped within 60 s, so a check that walks a span line by line fails. The context-mode case adds a ledger of 11 calls, 10 of them greps of 50 matches each | pass (13) |

### Failures

None. The two failures seen on the way were a test bug, described in Status.

### Earlier suites changed

S4 appends `evidence` and `evidence_dropped` to every `report/v1` finding and every `shadow/v1` comment entry, and bumps the context prompt's version. So these pins were updated. No assertion was weakened.

- **`tests/integration/test_p1_s10_json_output.py`:** `FINDING_KEYS` adds `evidence` and `evidence_dropped`. Two expected entries add `"evidence": []` and `"evidence_dropped": 0`, since neither cites anything: `rule_entry`'s, and the untagged model finding's in `ac2_untagged_ai_finding_follows_the_rules_with_rule_null`.
- **`tests/fixtures/p1_s10/sample_report.golden.json`:** both rule findings add `"evidence": []` and `"evidence_dropped": 0`.
- **`tests/integration/test_p2_s1_shadow_report_local.py`:** `COMMENT_KEYS` adds `evidence` and `evidence_dropped`. `WITHHELD_KEYS` is unchanged, as the spec names comment entries only.
- **`tests/integration/test_p3_s1_local_review_with_repo_tools.py`:** `CONTEXT_PROMPT_VERSION` moves from `p3-v1` to `p3-v2` (S4 AC9). That suite's AC10 test checks the report's `prompt_version` against it.
- **No other pin was found:**
  - P1-S11, P1-S12 and P1-S13 use P1-S10's `report()`, whose `REPORT_KEYS` don't change.
  - P3-S11 reads `owners` by key.
  - No other suite pins the keys of `verify` `checked` records exactly.

### Readings for the designer

1. **"Each finding" (AC6) is read as every finding, rule findings included,** with `evidence` `[]` and `evidence_dropped` 0. The Interfaces sentence ("`report/v1` and `shadow/v1` comment entries add …") could also be read as model findings only. If that is meant, rule findings must lose the keys in four places: `ac6_every_finding_has_evidence_and_evidence_dropped`, P1-S10's `FINDING_KEYS`, `rule_entry` and the golden file.
2. **Lines count, not paths (AC3).** A span of a file that `glob` only listed is expected to be dropped. The model saw the path but not its lines (the spec's Value: "only lines the model was actually shown").
3. **Lines shown by more than one call (AC1).** A span is expected to be kept when two different calls together showed all its lines. "Shown every line of it" doesn't say "by one call".
4. **A refused call on a file that another call showed** isn't tested. AC3 says a span of a refused call's file is dropped. But a span that an `ok` read already showed should presumably still count, and the designer may want to say so.
5. **AC5 beyond its examples.** These also exit 13, read from "as any schema violation is" and the `Finding` model's strictness: an empty `file`, a missing key, an extra key, a line given as a string, and `evidence` that isn't a list.
6. **AC7's separator and position.** The spec gives no separator between spans, so spaces, commas and semicolons are all accepted. "After the rationale" is checked as after the rationale's line and before the marker. The tests don't pin where the line sits relative to the "React with a thumbs-up …" request.
7. **AC8 pins counts only for kept model findings.** A finding dropped by its anchor, and a rule finding, must have both counts as ints, but their values aren't pinned. "No span text" is read as covering two things: the cited lines' text, and any model-written path that matched nothing. Neither may reach the log (the Limits: "no model-written path is shown").
8. **AC9's "asks for `evidence`"** is read as the prompt naming `evidence`, `start_line` and `end_line`. A prompt that didn't name the fields would leave the model unable to comply.
9. **Not tested:**
   - That `pr.runner` sets `is_diff_head`: this lands in S8 (spec Interfaces), so PR mode is tested through `review_diff` only.
   - The Limits' "linear in the ledger's spans": no public interface can measure it. It is checked as a bound instead: only a check that doesn't walk a span line by line can drop spans ending at line 10^12 in time.
   - The `--format markdown` output: no criterion names it.
   - Shadow's `shadow.md`: P2-S1 already checks that it holds the same bodies as `shadow.json`.

### Coverage

Not measured by the tester, who ran only this file and the changed suites. For coverage of `findings`, `context.evidence`, `prompt`, `review`, `report`, `render`, `pr.comments` and `pr.shadow`, see the main session's `pytest --cov`.

## P3-S16: Run the live smoke from a manual workflow, and accept CI only for such a dispatch

Tests are in `tests/integration/test_p3_s16_live_workflow.py` (AC1 to AC6, and AC7 for `pr backfill`) and `tests/evals/test_p3_s16_evals_live_dispatch.py` (AC7 for `evals live`), sharing the refused events in `tests/dispatch_support.py`. They were updated to the designer's amendment of AC2 (the diff of `src/`, and the 20 KB guard) and to reviewer round 1. They were written from the spec, design.md's "Spend and caps" and ADR 0015, in the style of the P2-S3 tests of `review.yml`, whose evaluator, YAML readers and script runner they import. No test makes a live call or reaches the network.

### Gate

All green, on the developer's AC7 code and the main session's `live.yml`:
- `ruff format --check`, `ruff check` and `mypy`.
- `pytest --cov`: 6,392 passed, 2 skipped (both Windows-only, from earlier slices), 99.38% coverage, after reviewer round 1.
- Total coverage 99.38%.
- This slice's own files alone, after reviewer round 1: 111 passed, none skipped (this clone holds the smoke's commits), with `ruff format --check`, `ruff check` and `mypy` clean. The full `pytest --cov` figures above are from before round 1, which changed only tests.

### Seam and set-up (assumed)

- **Reading `live.yml`.** PyYAML, as for P2-S3. Job `if:` expressions go through P2-S3's evaluator with `github.ref` and `inputs.step` set, so "main only" and "one choice" are checked by evaluating them, not only by matching text.
- **Running the smoke job's steps.** The diff step runs with bash and the real git in this clone, with the job's literal env and `RUNNER_TEMP` in `tmp_path`: as written, and with its `-- src` pathspec removed, so the guard sees the whole 80,551-byte diff between the same SHAs. A clone without the two SHAs skips the tests that need them, except in CI, where they fail: CI's checkout has the full history. The review step runs with bash and a fake `uv` first on `PATH`: it records each call's argv and environment, writes one marker line to stdout and one to stderr, and exits with a chosen code. So the loop, the redirections and the exit handling are executed, not parsed.
- **Each recorded `review-bot` command line** goes through the real parser (`review_bot.cli.parse_args`, with the call's `REVIEW_BOT_*` env), and through `review_bot.cli.main` with `MockProvider` on the real diff, from the repository root (ADR 0015's "the real CLI parser accepts its arguments").
- **AC7** runs `review_bot.evals.main(["live", ...], sdk_call=...)` with P1-S16's `ScriptedSdk`, and `review_bot.pr.cli.main(["backfill", ...], provider=...)` with P2-S13's `History`, `Backfiller` and `PerPullProvider`. `CI` and `GITHUB_EVENT_NAME` are removed for every test and set only on purpose.

### Criteria

| AC | Tests | Result |
|---|---|---|
| AC1 | `ac1_only_trigger_is_workflow_dispatch`. `ac1_exactly_one_input_step_a_required_choice`. `ac1_the_only_option_is_smoke`. `ac1_no_free_form_input_has_a_default_or_other_type`. `ac1_no_other_input_is_read_anywhere`. `ac1_every_choice_an_if_compares_is_an_option`. `ac1_no_job_runs_for_a_value_that_isnt_an_option` ×5 (empty, `evals-diff`, `backfill`, `SMOKE-x`, `smoke `). `ac1_no_other_event_name_appears_in_the_triggers` ×4 | pass (15) |
| AC2 | `ac2_smoke_checks_out_main_with_full_history`. `ac2_diff_is_written_with_git_diff_output`. `ac2_both_shas_are_literals_in_the_workflow`. `ac2_diff_step_writes_a_diff_under_20_kb` (runs the step: 8,028 bytes, starts with `diff --git`). `ac2_diff_holds_only_src` (every file header is under `src/`). `ac2_diff_of_20_kb_or_more_fails_the_step_naming_its_size` (without the pathspec: non-zero exit, the byte count and "20 KB" in the output). `ac2_no_review_runs_after_a_failed_diff_step` (the review step comes later and has no `always()`). `ac2_head_sha_is_in_mains_history`. `ac2_review_bot_runs_exactly_twice`. `ac2_each_review_reads_the_diff_step_output`. `ac2_each_review_has_context_repo_dot_and_the_head_sha`. `ac2_each_review_caps_cost_at_030_with_retries_0` (parsed settings: `max_cost_usd` 0.30, `max_retries` 0, provider claude). `ac2_flags_are_literals_on_the_command_line`. `ac2_total_spend_is_at_most_the_design_cap` (2 × 0.30 ≤ 0.60). `ac2_real_cli_runs_each_command_on_the_real_diff` (exit 0 with `MockProvider`) | pass (15) |
| AC3 | `ac3_no_job_runs_off_main` ×4 (a feature branch, `refs/heads/main-old`, a tag, a PR merge ref). `ac3_every_job_if_compares_the_ref_with_main`. `ac3_every_job_shares_one_concurrency_group_that_never_cancels` (one literal group, `cancel-in-progress: false`, and no job group equal to a workflow-level group, which would deadlock). `ac3_every_job_has_a_timeout_of_at_most_60_minutes`. `ac3_every_job_has_exactly_contents_read`. `ac3_every_checkout_persists_no_credentials` | pass (9) |
| AC4 | `ac4_the_only_secret_is_the_api_key`. `ac4_api_key_is_only_in_the_review_steps_env` (the one path in the YAML tree holding `secrets.`). `ac4_the_key_reaches_the_review_command`. `ac4_no_run_block_interpolates_event_or_inputs`. `ac4_no_env_or_with_carries_event_or_inputs`. `ac4_choice_is_read_only_in_if` | pass (6) |
| AC5 | `ac5_one_upload_runs_always_and_is_kept_90_days`. `ac5_every_review_captures_stdout_and_stderr_to_uploaded_files` (two files each, under the upload path). `ac5_each_reviews_output_goes_to_its_own_files`. `ac5_every_audit_log_is_uploaded` (absolute, under the upload path). `ac5_a_failed_review_still_writes_its_output_and_fails_the_step` (fake exit 12: the step fails, stderr is on disk for the `always()` upload) | pass (5) |
| AC6 | `ac6_no_job_is_named_gate`. `ac6_ci_yml_still_holds_the_gate_job`. `ac6_live_yml_can_never_report_on_a_pull_request` | pass (3) |
| AC7 | **evals live:** `ac7_evals_live_runs_in_ci_for_a_dispatch_with_the_flag` ×3 (`CI` `true`, empty, `false`). `ac7_evals_live_with_the_flag_refuses_any_other_ci_event` ×12. `ac7_evals_live_for_a_dispatch_without_the_flag_refuses` ×3. `ac7_evals_live_without_ci_runs_whatever_the_flag_and_event` ×8. `ac7_evals_live_without_ci_the_flag_changes_no_result`. **pr backfill:** `ac7_backfill_runs_in_ci_for_a_dispatch_with_the_flag`. `ac7_backfill_with_ci_set_to_any_value_and_the_flag_runs_for_a_dispatch` ×3. `ac7_backfill_with_the_flag_refuses_any_other_ci_event` ×12 (no `--out` created). `ac7_backfill_for_a_dispatch_without_the_flag_refuses` ×3. `ac7_backfill_without_ci_runs_whatever_the_flag_and_event` ×8. The 12 refused events: the spec's `pull_request`, `push`, `schedule` and unset, plus empty, `pull_request_target`, `repository_dispatch`, `workflow_call`, `WORKFLOW_DISPATCH`, a leading space, a trailing newline and `workflow_dispatch_x`. Each refusal: exit 2, no call, no recording or report, and the message names CI | pass (54) |
| Limits | `limits_dispatched_evals_live_still_refuses_over_its_budget`. `limits_dispatched_backfill_still_refuses_over_its_budget` (the dispatch passes the CI guard only) | pass (2) |
| Interfaces | `interfaces_replay_has_no_allow_dispatch`. `interfaces_pr_run_rejects_allow_dispatch` (exit 2) | pass (2) |
| AC8 | Recorded after merge (below) | pending dispatch |
| AC9 | Recorded after merge (below) | pending dispatch |

Mutation check of the workflow tests: each of these one-line changes to `live.yml` turns at least one test red: `cancel-in-progress: true`, `--max-cost-usd 0.31`, `persist-credentials: true`, `${{ inputs.step }}` in a `run:`, a third loop pass, dropping the stderr redirect, dropping the `github.ref` check, `timeout-minutes: 90`, `retention-days: 30`, a second option, the size check removed or raised to `-ge 200000`, a second pathspec (`-- src tests`), and a workflow-level `live` group.

### AC8: the smoke's runs (pending dispatch)

Filled in from each `smoke` run's artifact once the slice has merged. **Stop rule:** if any run's init tool list holds a built-in tool or is missing any of `mcp__repo__read`, `mcp__repo__grep` and `mcp__repo__glob`, S6 and S8 pause and S3 goes back to design.

| Run (URL) | Review | SDK version | Init tool list (audit `agent_tools`) | Tool calls and outcomes | Reported cost (USD) | Findings | Exit | Stop rule |
|---|---|---|---|---|---|---|---|---|
| pending | 1 | | | | | | | |
| pending | 2 | | | | | | | |

### AC9: every dispatch of `live.yml` (pending dispatch)

Every run, re-runs and failures included. S6, S8 and S15 add their runs here.

| Date | Run (URL, attempt) | Step | Outcome | Reported spend (USD) | Step total / step cap | Phase 3 total / $20.00 |
|---|---|---|---|---|---|---|
| pending | | `smoke` | | | / 0.60 | / 20.00 |

### Failures

None.

### Earlier suites changed

- **`tests/evals/test_p1_s16_eval_live_command.py`**, `test_p1_s16_ac6_ci_set_exits_2_before_any_call_even_with_every_flag`: it pinned the old refusal text, "live never runs in CI, and CI is set in the environment.", which AC7 makes untrue. It now checks that the message names CI as a word. Exit 2, no call and no recording are still asserted.

### Readings for the designer

1. **AC2, retries 0** comes from the step's `REVIEW_BOT_MAX_RETRIES: "0"`, not a flag. The tests check the parsed setting, so either form passes.
2. **AC3, concurrency.** The one job has job-level group `live` and no workflow-level group. The test requires every job to have the same literal group (job-level, or the workflow's if a job has none), and no job to repeat a workflow-level group. S6 and S8 must give their jobs the same group.
3. **AC7, `GITHUB_EVENT_NAME` is compared exactly.** Case variants, whitespace and longer names refuse. GitHub always sets the lower-case name, so this costs nothing.
4. **Beyond the spec, flagged for review:** a job must not run for a value that isn't an option (`ac1_no_job_runs_for_a_value_that_isnt_an_option`), and `--allow-dispatch` isn't on `evals replay` or `pr run`. The spec's Notes settle the single loop step and exit codes above 1 failing the step; `ac2_review_bot_runs_exactly_twice` and `ac5_a_failed_review_still_writes_its_output_and_fails_the_step` follow them.

### Gaps

- **A clone without the smoke's commits** (a shallow local clone) skips the five tests that run git on them. In CI they can't skip: `ci.yml` now checks out the full history, and a missing commit fails them.
- **AC6, "isn't a required check".** Branch protection can't be read offline. The tests check what makes `live.yml` unable to be the required check: no job named `gate`, and no `pull_request` trigger.
- **Actions themselves** (`actions/checkout` honouring `ref: main`, the upload, the secret's injection, the concurrency queue) are only exercised by the first dispatch, AC8.

### Coverage

From the full `pytest --cov` run:
- `settings.py` and `evals/cli.py`: 100% (omitted by `skip_covered`), including `live_run_refused`.
- `evals/live.py` 98% and `pr/cli.py` 99%. Their missed lines (`live.py` 190 and 192; `pr/cli.py` 259->261, 284->exit and 590) are outside this slice's changes; every line the slice changed is covered.
- Total: 99.38%.
