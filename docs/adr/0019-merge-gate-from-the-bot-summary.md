# 0019: Gate merges with a separate check that reads the bot's own summary, for approved classes only, failing open on unreviewed code and allowing an override by reply

- **Status:** Accepted
- **Date:** 2026-11-12
- **Phase:** 4

## Context
[Roadmap Phase 4](../roadmap.md#phase-4-governed) moves autonomy to "gate" for critical, high-confidence finding classes, with a documented appeal and override. Review jobs today are never required checks, and they turn red on a bot failure ([Phase 2 Q9](../phases/phase-2/design.md#5-open-questions)). Comment state already lives on the PR, in the bot's markers ([ADR 0009](0009-comment-state-on-the-pr.md)). A gate that blocks wrongly, or blocks every merge during a model outage, would lose developers' trust fast.

## Decision
Each review run decides the blockers for the approved classes the repository opts in to. Rule and secret classes are decided afresh from the whole PR diff. Model classes are carried in the summary marker, read only when the bot last edited the summary, and otherwise rebuilt from the bot's own inline comments, with the run staying `tampered` until that rebuild succeeds. A separate `merge gate` job, with no model and no key, decides on the PR's head from that summary and from a maintainer's override, given by a reply, or by a PR review for a secret. It fails on tampering, and passes open only where the head wasn't reviewed ([details](../phases/phase-4/design.md#merge-gating-appeal-and-override)).

## Consequences
- The gate check never depends on the model being up, and re-evaluates on override replies and reviews without a new review. Its concurrency group includes the event name, so a reply never cancels a push's gate run.
- A deterministic blocker clears as soon as the code is fixed. A model blocker clears when its line goes, or by override; a withheld one, or one whose comment is gone, clears only by override, given by a PR review naming its fingerprint, as for a secret.
- Rule classes count only the rules' own findings, so a model finding tagged with a rule's ID gates under no class, and a rule that can't reach `high` can't be a class. The marker tags each blocker with its class, so only model blockers carry, except when the whole diff can't be checked.
- Model blockers beyond the marker's 25 entries aren't carried, and when a diff can't be checked the listed blockers carry but no new one is found; both are disclosed.
- The gate job needs `actions: read`, a read-only permission, to tell a deleted summary from a PR the bot never reviewed.
- Overrides use review comments and reviews, never `issue_comment`, whose check runs land on the default branch rather than the PR's head.
- Each class earns gating on shadow evidence first, and each override is recorded per finding.
- Some risks remain, and the security packet records them:
  - Admins can still bypass branch protection.
  - The main session posts as the owner, so agents must never post overrides.
  - A model blocker can't be rebuilt after a tampered summary if its comment was withheld, or if its inline comment was deleted before the summary was edited.
- With gating on, every comment-mode run that isn't refused writes the summary, skips included, so a missing summary means a deletion. A PR stuck in `tampered` has a runbook way out: a recorded admin bypass, or a new PR.
- Comment mode must be on before anything can gate.

## Alternatives considered
- **Make the review job required:** a model outage or bot failure would block every merge.
- **Commit statuses through the API:** needs `statuses: write`, and still needs state for overrides.
- **Override by label:** a label can't say which finding it waives, and it outlives later blockers.
- **Fail closed when unreviewed:** an outage or an oversized PR would block merges with no finding behind the block.
- **Carry every blocker in the marker:** a fixed deterministic blocker below its anchor would never clear, and an edit plus a push could launder one.
