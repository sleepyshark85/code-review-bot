# P2-S8: Turn reactions and resolutions into labels

- **Status:** Approved
- **Depends on:** P2-S4, P2-S6
- **Touches:** `pr` (`feedback`, `github`, `cli`), `.github/workflows/review.yml` (main session)

## Value
Developers' thumbs and resolved threads on the bot's comments become `feedback/v1` labels, so comment-mode precision is measured with no extra work.

## Acceptance criteria
`python -m review_bot.pr collect --event PATH --out DIR` with `FakeGitHubClient`. Mapping: [ADR 0011](../../../adr/0011-feedback-labels.md).
- **AC1:** Given an own comment with one thumbs-up from a user, then `DIR/feedback.jsonl` has one record with `channel` `reaction` and `label` `accept`.
- **AC2:** Given a thumbs-down only, then `label` is `reject`. Given both, from one person or two, then `label` is `conflict`.
- **AC3:** Given only other reactions (heart, laugh, eyes), or reactions by the bot, then there's no reaction record.
- **AC4:** Given a thread a person resolved, then there's one record with `channel` `resolution` and `label` `resolved`, whether or not there is also a reaction record. Given a thread the bot resolved, then there's no resolution record.
- **AC5:** Given comments by others, or bot comments without a valid marker, then there's no record, whatever their reactions.
- **AC6:** Given any record, then it has P2-S4's keys, with the comment's posted `fingerprint`, its `file`, and `rule_part` and `anchor` from its marker. `source`, `severity`, `reason`, `run_id`, `model` and `prompt_version` are null, and S10 fills them by joining to the report that created the comment. No username, comment body or rationale appears in `DIR`, which is asserted with canary values.
- **AC7:** Given the fake fails, then it exits 18, and no `feedback.jsonl` is written.
- **AC8:** Given the stub, then `RestGitHubClient` reads the reactions on each comment and each thread's resolver, with S6's limits, page cap and errors.
- **AC9:** The workflow adds `closed` to the `pull_request` types and gains a new collect job. The collect job:
  - runs only for `closed`, on same-repo PRs, whatever `REVIEW_BOT_MODE` is, except `off`;
  - has permissions of exactly `contents: read` and `pull-requests: read`;
  - passes `GITHUB_TOKEN` only to its `collect` step, and references no other secret;
  - uploads `feedback.jsonl` with `retention-days: 90`, under a name holding the PR number and `github.run_attempt`.

  The shadow and comment jobs still don't run on `closed`, so P2-S3's and P2-S6's job criteria hold unchanged. P2-S3's AC6 applies to this job too.
- **AC10:** Given these files, then P2-S4's `label` parses them and ignores records whose `channel` isn't `shadow`.
- **AC11:** Given two own comments sharing one fingerprint (a moved comment and its old thread), then their reactions combine into one reaction record for that fingerprint. A thumbs-up on one and a thumbs-down on the other give `conflict`; thumbs-ups on both give one `accept`.

## Limits
API limits are as in S6. Only reaction content values and resolver identities are read, then reduced to labels. Identities are compared with the bot and dropped.

## Interfaces
A run writes at most one reaction record per fingerprint, combining every own comment that carries it. Adds to `feedback/v1`: the channels `reaction` and `resolution`, and the labels `conflict` and `resolved`.

## Out of scope
- Computing precision and the join (S10); replies as signals ([ADR 0011](../../../adr/0011-feedback-labels.md)).

## Notes
- **Main session:** add the `closed` job from this spec.
