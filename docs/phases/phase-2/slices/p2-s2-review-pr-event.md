# P2-S2: Review a pull request from its event, within a per-run budget

- **Status:** Approved
- **Depends on:** P2-S1
- **Touches:** `pr` (`event`, `git`, `runner`, `cli`), `settings`, `review`, `providers/claude_agent`

## Value
One command reviews a PR exactly as CI will, from its event payload and a clone. It can't spend more than one bounded call, and it never runs for a fork.

## Acceptance criteria
`python -m review_bot.pr run --event PATH --repo DIR --out OUT`, with a real git repository built in `tmp_path`, and a scripted `MockProvider` unless a criterion says otherwise. Caps: [design](../design.md#ci-settings-and-caps).
- **AC1:** Given an `opened` event for a same-repo PR whose base and head commits exist in `DIR`, then `shadow.json` has `mode` `shadow` and `origin` `event`. Its `pr` holds the event's repo, number, base SHA and head SHA, `event_at` equals `pull_request.updated_at`, and findings are anchored on the head's lines. Exit 0.
- **AC2:** Given the base branch gained a commit after the PR branched, then only the PR's own changes are reviewed: the diff from the merge base to head.
- **AC3:** Given action `synchronize`, `reopened` or `ready_for_review`, then it is reviewed as in AC1. Given `closed`, `edited` or `labeled`, then `outcome` is `skipped` with `reason` `action`, and there's no git or provider call.
- **AC4:** Given an event whose head repository differs from the base repository, or is null, then `outcome` is `skipped` with `reason` `fork`, and neither git nor the provider is called.
- **AC5:** Given `CI` set, `--provider claude`, and `ANTHROPIC_API_KEY` unset or empty, then `outcome` is `skipped` with `reason` `no_credential`, and no provider is built or called. Given `CI` unset, then the run goes ahead and uses the local Claude Code login, as `review-bot` does.
- **AC6:** Given `REVIEW_BOT_MAX_REVIEW_BYTES=120000`, a 300,000-byte diff that is mostly a skipped docs file, and 50,000 bytes of reviewable files, then the run is reviewed. Given 130,000 reviewable bytes, then `outcome` is `skipped` with `reason` `too_large`, and there's no call. With `review-bot` and the same variable set, the second case exits 6.
- **AC7:** Given `REVIEW_BOT_MAX_OUTPUT_TOKENS=16000`, then the Claude provider passes that cap to the CLI, asserted over the fake SDK as Phase 0's lockdown tests are. Given `0`, `-1` or `abc` for either new variable, then exit 2 naming the variable. With both unset, `review-bot` behaves as before.
- **AC8:** Given any run, then the provider is called at most once, and `caps` lists the effective max diff bytes, max review bytes, max output tokens, retries and deadline.
- **AC9:** Given the mock reports 20,000 output tokens under a 16,000 cap, then `over_budget` is true and `outcome` is still `reviewed`.
- **AC10:** Given an event file that isn't JSON, lacks `pull_request`, or has a SHA that isn't 40 hex digits, a repo that isn't `owner/name`, or a number that isn't a positive integer, then exit 19 with one line naming the field, and no git or provider call.
- **AC11:** Given a head SHA missing from `DIR`, then exit 19 naming that SHA, with no provider call.
- **AC12:** Given a `.gitattributes` in `DIR`'s working tree that assigns `*.py` a diff driver, and `DIR/.git/config` defining that driver's `textconv` and `command` as programs that write a marker file, then no marker file is written, and the reviewed diff is git's plain text of the real change.
- **AC13:** Given a PR title and body holding a canary string and control characters, then the canary appears in neither `shadow.json` nor the audit log.
- **AC14:** Given `REVIEW_BOT_AUDIT_LOG` unset, then the audit log is written to `OUT/audit.jsonl`. Given `ANTHROPIC_API_KEY=sk-ant-canary…` and a provider failure whose diagnostics echo it, then no file under `OUT`, the audit log included, contains it.
- **AC15:** Given a draft PR, then it is reviewed in shadow mode ([Q10](../design.md#5-open-questions)).
- **AC16:** Given a PR that changes only docs, lock or generated files, then `outcome` is `skipped` with `reason` `nothing_to_review`, there's no provider call, and it exits 0.

## Limits
- An event file over 10 MB exits 19 unread.
- git output is read only up to max diff bytes plus one; beyond that the run is `skipped`, `too_large`.
- Each git command has a 60-second timeout, after which it exits 19.
- The event is parsed into SHAs, numbers, names and flags only; no free text from it is kept.

## Interfaces
`PullRequestRef`, `GitRepo`, `run_pull_request`: [design](../design.md#interfaces). Reasons added here: `action`, `fork`, `no_credential` and `nothing_to_review`, plus S1's. The audit log goes to `OUT/audit.jsonl` unless `REVIEW_BOT_AUDIT_LOG` is set.

## Out of scope
- The workflow (S3); `--mode comment` (S5); config (S9).

## Notes
- **README (main session):** describe `run`, and the two new settings.
