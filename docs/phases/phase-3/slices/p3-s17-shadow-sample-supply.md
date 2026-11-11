# P3-S17: Let this repository's own pull requests reach a shadow review

- **Status:** Done
- **Depends on:** none
- **Touches:** `tests/integration/` (six files' stand-in credentials; the guard and AC tests), `.review-bot.toml` (new, repository root), README, `CONVENTIONS.md`. `src/` is unchanged.

## Value
This repository's PRs stop being blocked by test-fixture "secrets" or skipped `too_large` because of eval recordings, so Phase 2's Q1 sample ([P2 design Q1](../../phase-2/design.md#5-open-questions)) can fill ([#98](https://github.com/sleepyshark85/code-review-bot/issues/98)).

## Acceptance criteria
In the tests, every value in AC2 and AC3 is assembled at test run time, as `V` is, so that S17's own test files produce no scanner hit. `V` is 20 or more characters, with a digit and no placeholder word ([P1-S3 Notes](../../phase-1/slices/p1-s3-secret-block.md#notes)).
- **AC1:** Given a diff adding each of `API_KEY = "test-placeholder-not-a-key"`, `"ANTHROPIC_API_KEY": "test-placeholder-not-a-key"`, `"GITHUB_TOKEN": "test-placeholder-not-a-key"` and YAML `token: "test-placeholder-not-a-key"`, when `review-bot` reviews it with a scripted `MockProvider`, then the provider is called once and the exit code isn't 16.
- **AC2 (still fail-closed):** Given an added line holding any of `API_KEY = "fake-key-for-the-test"`, `API_KEY = "test-key-sentinel"`, `"GITHUB_TOKEN": "fake-token-for-the-test"`, `PLACEHOLDER_API_KEY = "V"`, `TEST_PLACEHOLDER_TOKEN: "V"`, or `"ANTHROPIC_API_KEY": "<an assembled anthropic-key>"`, then it exits 16 and the provider isn't called. A credential's name never exempts it; only its value can.
- **AC3:** At the slice's head, no tracked file under `tests/` gets a `scan_secrets` hit unless it is on the guard's fixed list of files that test secret detection, blocking, refusal or redaction. These six files aren't on the list and have no hit: `test_p0_s4_messages.py`, `test_p0_s4_real_sdk.py`, `test_p3_s6_live_evals_workflow.py`, `test_p3_s8_pr_review_with_context.py`, `test_p3_s14_context_in_ci.py` and `test_p3_s16_live_workflow.py`. When the guard's check is applied to a file set that includes an unlisted path holding `API_KEY = "fake-key-for-the-test"`, it fails and names the path, line and kind, never the value.
- **AC4:** Removed: it was a review check, not behaviour (see Out of scope).
- **AC5:** Given this repository's committed `.review-bot.toml` copied to the base of a temporary repo, when `run` reviews a PR that changes `tests/evals/cases/sample-context/recordings/synthetic/double_tax_1_of_3.json`, `tests/evals/cases/sample-context/case.json`, `tests/evals/cases/sample-context/repo/billing/invoice.py` and `src/review_bot/review.py`, then only the recording is skipped, with reason `excluded_by_config`. The other three are reviewed, and `shadow.json`'s `config.source` is `.review-bot.toml@<base SHA>`.
- **AC6:** Given that config and `REVIEW_BOT_MAX_REVIEW_BYTES=120000`, a PR changing 150,000 bytes of recordings plus a 2,000-byte source change is reviewed. With no config at the base, the same PR is `skipped` with reason `too_large`.
- **AC7:** Given that config, a PR whose excluded recording holds an assembled `aws-access-key` is still `blocked_secret`. An excluded file is skipped from review, never from the secret scan ([data flow](../design.md#data-flow-one-context-review-of-a-pr)).
- **AC8:** The committed `.review-bot.toml` sets only `exclude = ["tests/evals/cases/*/recordings/**"]`. The threshold and `max_open_comments` stay at their defaults.
- **AC9:** P0-S4 AC11 and P1-S5 AC8 still pass, still using the value `test-key-sentinel`.

## Limits
None new. The secret scan and its limits are unchanged ([P1-S3 Limits](../../phase-1/slices/p1-s3-secret-block.md#limits)). The one `exclude` glob is bounded as [P2-S9](../../phase-2/slices/p2-s9-repo-config.md#limits) specifies.

## Interfaces
- **The rule:** no stand-in credential in a test may take a form that `scan_secrets` reports, unless its file is on the guard's list. A value can avoid a hit by containing a placeholder word, as `test-placeholder-not-a-key` does under P1-S3's existing rule ([Interfaces](../../phase-1/slices/p1-s3-secret-block.md#interfaces)), or by a form the scan doesn't report, such as a name that isn't a credential name. This slice adds no allowance to the scan.
- **`exclude` glob rules:** [P2-S9](../../phase-2/slices/p2-s9-repo-config.md#interfaces).
- **Who does what:** the main session commits `.review-bot.toml`, the README line (this repo excludes eval recordings from review) and the rule above in `CONVENTIONS.md` first. The tester then rewrites the six integration files, adds the guard and the AC tests, and reports. The developer has no work: the only unit-test hit is `test_secret_scan.py`, which belongs on the list.

## Out of scope
- Changing the secret scan, including narrowing P1-S3's placeholder-word rule to one exact value.
- Excluding large test files: they are code, so a PR still over the cap stays `too_large`.
- Proving that the Q1 sample is reached: P2-S12 measures it (#67).
- **Review check, not a test:** no file under `tests/evals/cases/**` or `tests/fixtures/**` changes; they stay byte-identical to `main`.
- **Still blocked, by design:** removed and context lines are scanned ([P1-S3 AC2](../../phase-1/slices/p1-s3-secret-block.md#acceptance-criteria)). So S17's own PR, any later PR whose hunks include this spec's quoted fake values (AC2, AC3), any PR whose hunks include an old fake value, and any PR whose hunks include a hit line in a listed file or in `docs/phases/phase-1/slices/p1-s3-secret-block.md` stay `blocked_secret`.
- The config applies only to PRs whose base holds it ([ADR 0008](../../../adr/0008-pull-request-action-with-github-token.md)).

## Notes
