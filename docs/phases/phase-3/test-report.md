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
