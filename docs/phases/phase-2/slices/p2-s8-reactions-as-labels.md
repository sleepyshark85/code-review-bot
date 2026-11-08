# P2-S8: Turn reactions and resolutions into labels

- **Status:** Done
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
API limits are as in S6. Only reaction content values and resolver identities are read, then reduced to labels. Identities are compared with the bot (for reactions, with any bot) and dropped.

## Interfaces
A run writes at most one reaction record per fingerprint, combining every own comment that carries it. Adds to `feedback/v1`: the channels `reaction` and `resolution`, and the labels `conflict` and `resolved`.

## Out of scope
- Computing precision and the join (S10); replies as signals ([ADR 0011](../../../adr/0011-feedback-labels.md)).

## Notes
- **Main session:** add the `closed` job from this spec.

Build choices (developer):
- `GitHubClient.list_signals(pr)` returns each own comment (`BotComment`, with `resolved_by`) and the set of reaction contents by people. **Reactions by any bot (REST user type `Bot`, such as `dependabot[bot]`) are ignored**, not only this bot's (coordinator's decision, review round 1); a reaction with no user (a deleted account) counts as a person's. Reactor identities are checked inside the client and dropped. `RestGitHubClient` lists the comments and threads as `list_bot_comments` does, then `GET /repos/{o}/{r}/pulls/comments/{id}/reactions` per own comment, paged and capped as in S6; errors name `list_signals`.
- `feedback/v1` is two models told apart by `channel`: `ShadowFeedback` (S4's record, labels `accept` or `reject`) and `SignalFeedback` (`reaction` or `resolution`, with `index`, `source`, `severity`, `reason`, `run_id`, `model` and `prompt_version` null). `parse_feedback_line` reads `channel` first, then validates with that model, so errors name plain keys (`repo: Field required`) and an unknown channel says `Input should be 'shadow', 'reaction' or 'resolution'` without quoting it. A shadow record with another label says `a shadow record's label is 'accept' or 'reject'`.
- A fingerprint's records name its newest comment's file, rule part and anchor. A fingerprint gets at most one `resolution` record too, even if two of its threads were resolved by a person. Records come in order of each fingerprint's oldest comment, reaction before resolution, all with one `recorded_at`.
- `collect` always writes `feedback.jsonl`, empty when nothing is labelled, and prints `reaction=N resolution=M`. Without `GITHUB_TOKEN` it exits 2 with its own message, saying to run it in the collect job or set a token that can read the pull request; an unwritable `--out` exits 2. It writes no audit log, as it calls no model.
- `GITHUB_TOKEN` and `GITHUB_API_URL` format errors now say "so nothing ran" instead of "so nothing was reviewed", since `collect` shares them.
