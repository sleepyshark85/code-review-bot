# 0009: Keep comment state on the PR, pair comments with current findings, and resolve only when the anchored line is gone

- **Status:** Accepted
- **Date:** 2026-11-08
- **Phase:** 2

## Context
[README Phase 2](../roadmap.md#phase-2-integrated) wants the bot to update or resolve its own comments, keyed by fingerprint ([ADR 0003](0003-finding-fingerprints.md)), and to re-review only new commits. Phase 1's live run found AI fingerprints recur in only 4 of 8 to 6 of 7 cases per run ([test report](../phases/phase-1/test-report.md#for-phase-2-ac9)). A fingerprint's ordinal also depends on which findings a run sees, so it shifts under incremental review, or when an identical line is added above.

## Decision
State lives only on the pull request. Each inline comment ends with a hidden marker holding its fingerprint, rule part and anchored-line hash, and one summary comment holds the last commit reviewed. Each current finding pairs with at most one of the bot's comments that is open or that a person resolved (never one the bot resolved): by fingerprint when the comment is on the finding's line, then by path (following renames), rule part and anchor, choosing the nearest line. A new comment takes a fingerprint no open or person-resolved comment holds. A paired comment keeps its posted fingerprint. A comment is moved only when a current finding pairs with it, and resolved only when its anchored line is gone. GitHub's "outdated" flag is a hint, never proof. Only threads a person resolved are never posted again. [Rules](../phases/phase-2/design.md#comment-state).

## Consequences
- No database, cache or artifact is needed to know what was posted, and deleting the bot's comments resets it.
- Shadow mode posts nothing, so it has no state: every shadow run reviews the whole PR, and labelling groups its repeats by the same key.
- A comment the model wouldn't make again stays open while its anchored line exists, even if the issue was fixed beneath it. A person resolves it, and that becomes a signal ([ADR 0011](0011-feedback-labels.md)).
- A comment is re-posted only where a finding stands now, and then with its original fingerprint, so its labels stay joined. A comment nobody re-found is never re-posted.
- A newly created comment never takes a fingerprint an open or person-resolved comment already holds; it takes the next free ordinal for its key. So two open comments never share a fingerprint once a PR settles, and the duplicates rule can't churn identical lines (added in build, P2-S5). A comment's fingerprint can therefore differ from its finding's in the embedded report.
- Changing the fingerprint scheme or the marker orphans every open comment, so both are versioned.
- A human can paste a marker, but it's ignored unless the bot wrote the comment.

## Alternatives considered
- **Match by exact fingerprint only:** simplest, but an ordinal shift swaps or orphans comments.
- **Fingerprint incremental findings against the full PR diff:** keeps ordinals stable for incremental runs, but not when an identical line is added above.
- **Resolve whenever a fingerprint disappears, or its hunk changes:** with today's recurrence, it churns comments on lines nobody touched, and a new file is one hunk.
- **Trust the outdated flag:** GitHub doesn't document it as "this line changed", and it fires on neighbouring edits.
- **State in a workflow artifact or cache:** expires, can be lost, and drifts from what is on the PR.
- **Delete and re-post each run, or move every outdated comment:** loses replies, reactions and history, and notifies for comments no current finding supports.
