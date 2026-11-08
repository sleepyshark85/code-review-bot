# P2-S7: Re-review only what changed since the last reviewed commit

- **Status:** Done
- **Depends on:** P2-S5; in CI, P2-S3's full-history checkout (`fetch-depth: 0`), without which `A` is never found and every run is full
- **Touches:** `pr` (`git`, `runner`, `plan`, `comments`, `github`)

## Value
In comment mode, each push is reviewed only for what it changed. Cost stays flat, and the model doesn't raise new comments on code it has already reviewed.

## Acceptance criteria
`run --mode comment` with a git repository in `tmp_path`, `FakeGitHubClient`, and a scripted `MockProvider` that records each request. State: [comment state](../design.md#comment-state).
- **AC1:** Given a summary with `reviewed=A`, head `B`, and `A` an ancestor of `B`, where `B` changes `x.py` only, then the model's request holds only the PR diff's hunks of `x.py` that contain a line changed since `A`. `shadow.json` has `scope` `incremental` and `since` `A`. After applying, the summary holds `reviewed=B`.
- **AC2:** Given that `B` only merges the base branch into the PR branch, and no commented line moved, then there's no provider call. `outcome` is `reviewed` with `reason` `no_new_changes`, nothing is posted, moved or resolved, and the summary holds `reviewed=B`.
- **AC3:** Given that `B` equals `A` (a re-run), then there's no provider call, `outcome` is `skipped` with `reason` `already_reviewed`, and nothing changes.
- **AC4:** Given that `A` isn't an ancestor of `B` (a force-push), or `A` isn't in the clone, then the whole PR is reviewed, with `scope` `full` and `since` null.
- **AC5:** Given no summary, a summary with no marker, or a marker whose SHA isn't 40 hex digits, then the whole PR is reviewed.
- **AC6:** Given an open own comment not found again, then it is resolved only if its own line changed since `A`. A comment on an unchanged line is kept, even inside a hunk changed since `A`, or in a file added since `A`.
- **AC7:** Given an incremental run that finds again, at ordinal 0, a finding whose comment was posted at ordinal 1 (because the line's identical twin above isn't in the incremental diff), then it pairs with that comment by key and nearest line, and nothing is created.
- **AC8:** Given a push that only renames a commented file, then there's no provider call, and `outcome` is `reviewed` with `reason` `no_new_changes`. The comments' keys follow the rename in the diff from `A` to `B`, so none is resolved, and with no pairing finding none is moved. The summary holds `reviewed=B`.
- **AC9:** Given a finding on a line that the incremental diff has but the PR diff doesn't, then it is withheld `not_commentable`.
- **AC10:** Given the provider or GitHub fails, then the summary's `reviewed` SHA is unchanged, so the next push reviews from `A` again.
- **AC11:** Given `--mode shadow`, then the whole PR is reviewed every time, with `scope` `full`, and no client is used.
- **AC12:** Given a file added in the PR at `a.py` and commented, then renamed to `b.py` after `A` with one line edited elsewhere in it, then only that edit's hunk is reviewed. A comment whose anchor is still in `b.py` isn't resolved. If a finding pairs with it, it moves to `b.py` with its fingerprint (as in P2-S5 AC6), and nothing is created twice.

## Limits
Two diffs per run, each bounded by the byte caps and by P2-S2's git timeout. The summary marker is read only from the bot's own summary comment, by exact pattern.

## Interfaces
`GitRepo.is_ancestor` and `GitHubClient.read_summary`: [design](../design.md#interfaces). Reasons added: `already_reviewed` (a skip), and `no_new_changes` (outcome `reviewed` with no model call: the plan only resolves comments whose anchored lines are gone). Renames are followed in both the PR diff and the diff from `A` to `B`.

## Out of scope
- Incremental shadow runs: shadow mode keeps no state ([ADR 0009](../../../adr/0009-comment-state-on-the-pr.md)).

## Notes
Changed in build, main session, 2026-11-08:
- **AC9** can't be reached end to end: the reviewed diff is a subset of the PR diff's hunks, so a line the PR diff lacks is never sent, and verify would drop such a finding before planning. AC9 is covered at the `plan_comments` level.
- **Secret scan:** an incremental run scans only the hunks it sends, not the whole PR diff. Every other line was scanned when it last changed: `reviewed` moves only after a `reviewed` run, and `blocked_secret` keeps `A`.
- **Unusable diff since `A`** (over `max_diff_bytes`, not UTF-8 or unreadable, after a large merge from the base, say): the run reviews the whole PR diff instead, scope `full`, so no problem of that diff alone can skip a PR for good (changed in review round 1, main session; reverses the earlier byte-cap note).
- **Known limit:** a merge commit that reverts a change from the base (an "evil merge") leaves a line that is unchanged since `A` but new to the PR diff, so an incremental run doesn't review it. A force-push or a full re-review covers it.
- **Summary wording.** An incremental run names its start and always counts the bot's open comments after the run, for example `` **Review bot:** `reviewed`. Changes since `abc1234` reviewed. No new findings at or above medium. Open bot comments on this pull request: 2. `` With findings, its counts read `N comments, M withheld.` instead. `no_new_changes` reads `` **Review bot:** `reviewed` (`no_new_changes`). No changes to review since `abc1234`, so nothing was sent to the model. Open bot comments on this pull request: 2. `` A full comment-mode run keeps P2-S5's wording and adds `Open bot comments on this pull request: N.` when N isn't 0. Shadow mode adds nothing. The count is the plan's: open comments, less resolves, plus creates.

Build choices (developer):
- **Order:** the PR diff comes first. A diff that can't be reviewed, or whose files are all skipped, ends with no GitHub call (keeping P2-S5 AC11). Otherwise comment mode calls `read_summary`: head equal to `reviewed` is `already_reviewed`; an ancestor of the head (`is_ancestor`, False for a commit missing from the clone) is incremental; anything else is reviewed whole.
- **Changed lines:** the diff from `A` to `B` is git's diff `A...B`, which equals `A..B` since `A` is an ancestor. A PR hunk is reviewed if one of its new-side lines (added or context) is added since `A`, or sits next to a line removed since `A`. An empty `A..B` (an empty commit) is `no_new_changes`.
- **AC6:** in an incremental run, an unpaired open comment whose file is in the PR diff and existed at `A` is resolved only if it is gone (P2-S5's rule) and the diff since `A` removes a line with its anchor in that file. Any other comment, such as one left on a path renamed before `A` or one in a file added since `A`, follows P2-S5's rule alone, so it is resolved once its anchored line is gone and never stays open for good.
- **Renames:** a comment's path follows the renames in the PR diff or in the diff since `A`.
- **Marker:** it records the head only when the outcome is `reviewed` and every GitHub call before the summary was done; otherwise it rewrites the commit read (`none` if there was none or it wasn't 40 hex). `write_summary` takes `reviewed_sha` as a required keyword; None writes `none`.
- **`shadow.json`:** `scope` and `since` come last. `since` is also set on an `already_reviewed` skip and on a failed incremental run.
- **`FakeGitHubClient.fail_on`** keeps P2-S5's behaviour: every later call to that operation fails.
