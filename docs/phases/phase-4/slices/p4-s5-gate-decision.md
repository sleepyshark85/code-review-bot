# P4-S5: See which findings would block a merge, per approved class

- **Status:** Approved
- **Depends on:** P4-S1
- **Touches:** `governance.gate` (new), `governance.policy`, `pr.config`, `pr.runner`, `pr.shadow`, `pr.comments`, `review`

## Value
A repository that opts in sees which findings of a policy-approved class would block its merge: shadow reports say "would block", and in comment mode the summary says "blocks" and carries that state between pushes. No check enforces it yet ([ADR 0019](../../../adr/0019-merge-gate-from-the-bot-summary.md)).

## Acceptance criteria
Policy section `[[gate.classes]]` (`rule`, `min_severity` `high` or `critical`, default `critical`; `min_confidence` 0.5 to 1.0, default 0.9; `approved_by`; `approved_on`). Repository opt-in: `gate = [<class>, ...]` in `.review-bot.toml`. Rules: [design, Merge gating](../design.md#merge-gating-appeal-and-override).
- **AC1:** Given a policy class `swallowed-exception` (`high`, 0.9), a repository with `gate = ["swallowed-exception"]`, and a shadow run whose PR diff has that rule's finding (high, confidence 1.0), then `shadow/v1` has `gate` `{"enabled": true, "decision": "block", "classes": ["swallowed-exception"], "blocking": [{"fingerprint", "class", "file", "line", "severity"}]}`, and `shadow.md`'s summary says "Would block the merge: 1 finding (swallowed-exception)".
- **AC2:** Given no `gate` key in `.review-bot.toml`, then `gate` is `{"enabled": false, "decision": "not_enabled", "classes": [], "blocking": []}`, and the summary body and marker are byte-identical to Phase 3's.
- **AC3:** Given `gate = ["decimal-money"]` under a policy that doesn't approve that class (the built-in policy approves none), then the run fails `invalid_config` with exit 2, naming the class and the policy's version, with no model call.
- **AC4:** Given an enabled class, then a finding of it at `medium`, at confidence 0.8, or below the run's threshold doesn't block. Given a class `ai:security` (`critical`, 0.9), then a model finding with category `security`, `critical` and 0.95 blocks.
- **AC5:** Given rule classes, then they are decided every run from the rules over the whole base...head diff, on incremental comment-mode runs too, and nothing about them is carried. A PR whose first push adds `except Exception: pass` and whose second push replaces `pass` with logging, the `except` line unchanged, has no rule blocker after the second run.
- **AC6:** Given `possible-secret` enabled, then each secret hit in the whole base...head diff blocks, every run, with a fingerprint built from its file, the rule part `possible-secret` and its line's hash, and no value anywhere. When a later push removes the secret, the next run's whole-diff scan doesn't find it and the blocker is gone.
- **AC7:** Given comment mode and a blocker, then the summary says "Blocks the merge" and lists each blocker by file, line and class. It says how to appeal (reply on the thread) and how a maintainer overrides: `/review-bot override: <reason>` on the thread, or for a secret, a PR review whose body starts `/review-bot override <fingerprint>: <reason>`. Its marker ends ` gate=<fingerprints>`, plus ` tampered` while AC9's rebuild is pending.
- **AC8:** Given an `ai:` class and a previous summary that the bot last edited, with `gate=A`, when an incremental run reports nothing new: if A's comment is kept, or a person resolved it, then A still blocks; if the bot resolved it because its anchored line is gone, then A is dropped.
- **AC9:** Given a previous summary last edited by anyone but the bot, then nothing is read from its marker. In the same run, the carried `ai:` blockers are rebuilt from the bot's own inline comments of enabled classes that are open or person-resolved and last edited by the bot, using the fingerprint and rule part in each comment's marker. If every such comment passes those checks, the rebuild is done: the run decides with the rebuilt blockers as carried, and the summary it writes is trusted from then on. If any of the bot's inline comments was last edited by someone else, the rebuild isn't done. The decision is `block`, reason `tampered`, and the bot writes ` tampered` into its marker, so later runs stay `tampered` until a rebuild is done. Two disclosed exceptions: a withheld `ai:` blocker has no inline comment, and an `ai:` blocker whose inline comment was deleted before the summary was edited can't be seen. Neither can be rebuilt, so both are lost, and the summary says a tamper occurred.
- **AC10:** Given a blocker whose comment is withheld (`over_limit`, `not_commentable` or `possible_secret`), then it still blocks, and the summary names it by file and line.
- **AC11:** Given outcome `failed` (and, from S8, `degraded`), or `skipped` for `too_large` or `nothing_to_review`, then the decision is `block` if any rule, secret or carried blocker is found, else `unjudged`; never `pass`. Given `skipped` with `already_reviewed`, it repeats the previous summary's decision. A refused run (fork, action, draft, `no_credential`) reads nothing, writes nothing and reports `unjudged`. With gating enabled, every comment-mode run that isn't refused (P2's refusals: fork, action, draft, `no_credential`) writes the summary, skips included. It keeps the previous `reviewed=` SHA and writes the run's `gate=` (and any ` tampered`). Phase 2's "a skip edits nothing" then holds only with gating off.
- **AC12:** Given any run, then the audit log has one `policy`/`gate` record with the decision, any reason, the blocking fingerprints, the classes and `policy_version`.

## Limits
The marker holds at most 25 fingerprints, most severe first. Beyond 25, the decision is still `block`, and the summary states the count. The summary stays under 60,000 characters. A marker's `gate=` part is untrusted: a value that isn't comma-separated 16-hex fingerprints counts as tampered (AC9).

## Interfaces
`RepoConfig.gate: tuple[str, ...]`. Classes are a `RuleId`, `ai:<category>` or `possible-secret`. `GateDecision`: `not_enabled`, `pass`, `block`, `unjudged`. Marker: [design, Summary marker](../design.md#interfaces).

## Out of scope
- The check that enforces it, and overrides (S10); approving classes (S11; the owner).

## Notes
