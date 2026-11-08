# P2-S7: Re-review only what changed since the last reviewed commit

- **Status:** Approved
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
