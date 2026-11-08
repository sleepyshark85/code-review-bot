# 0012: Give CI one repo-secret model key, never to forks, and bound every run by calls and size

- **Status:** Accepted
- **Date:** 2026-11-08
- **Phase:** 2

## Context
[README Phase 0](../../README.md#phase-0-stabilise) says anything shared uses `ANTHROPIC_API_KEY`; the user will add it as a repo secret. Every push to a PR now spends real tokens with nobody watching, Phase 1 found that reported token counts can read zero ([known limits](../phases/phase-1/design.md#known-limits-carried-from-phase-0)), and spend caps by budget wait for Phase 4.

## Decision
The key is a repository Actions secret passed only to the review step, and only for same-repo PRs. With no key the run is skipped and says so. Each run makes at most one model call with no retries, refuses before any call when the files it would send, after docs, lock and generated files are skipped, exceed a fixed byte limit, caps output tokens through the provider, has a wall-clock deadline, and a newer push cancels an older run for the same PR. The monthly spend limit is set on the user's Anthropic account, outside the repository. [Values](../phases/phase-2/design.md#ci-settings-and-caps).

## Consequences
- The worst case per push is one call on a bounded diff, and the worst case per month is the account limit.
- A transient API failure loses that push's review; the next push reviews again, and the report says why.
- Dependabot PRs have no access to Actions secrets, so they are skipped as having no key.
- The output cap is enforced by the Claude CLI, not by us; a run reporting more tokens than the cap is flagged, and a live run checks it once.
- A large PR isn't reviewed at all rather than reviewed in part, matching Phase 0's "never truncated".

## Alternatives considered
- **Retries as the CLI defaults:** fewer lost reviews, but up to three attempts' cost per push.
- **A token budget per run:** what the user asked for, but token counts aren't reliable enough to enforce one; bytes and the output cap stand in.
- **An environment with required reviewers:** a person approves each run, which defeats unattended shadow mode.
- **Per-PR or per-day call counters:** need state across runs that shadow mode doesn't keep.
