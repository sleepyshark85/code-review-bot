# P2-S5: Create, update, move and resolve the bot's own comments

- **Status:** Approved
- **Depends on:** P2-S2
- **Touches:** `pr` (`github` with the `GitHubClient` protocol, `fake_github`, `plan`, `runner`, `cli`)

## Value
In comment mode the bot keeps one comment per finding, never repeats itself, and resolves a comment only when its line is gone. This is proven against an in-memory GitHub.

## Acceptance criteria
`run --mode comment` through `main`, with an injected `FakeGitHubClient`, a scripted `MockProvider`, and git repositories in `tmp_path`. Rules: [comment state](../design.md#comment-state).
- **AC1:** Given no own comments and 2 findings, then one review on the head SHA posts 2 comments on their files and lines (new side), with S1's bodies. A summary is written, and both entries are `create` with `applied` `done`.
- **AC2:** Given the same findings again, then nothing is posted, both are `keep`, and the summary is edited in place. Given a severity change, then only that comment is edited (`update`).
- **AC3:** Given an identical line added above a commented one, so the ordinals shift, then the comment stays paired with its own line, and only the new line's finding is created. Given two candidates with one key on the same line, one with the finding's severity, then that one pairs. A nearer candidate wins over an equal-severity one further away. Any remaining tie goes to an open thread over a person-resolved one, then to the oldest comment ID. A thread the bot resolved is never a candidate, even with the same fingerprint.
- **AC4:** Given an unpaired open comment, then it is resolved if its own line was deleted or edited. It is kept if only other lines changed, including in a new file, or if a rule's issue was fixed below an unchanged anchored line (`pass` replaced by logging under `except Exception:`).
- **AC5:** Given an outdated open comment paired with a current finding, then its thread is resolved, and the same body and fingerprint are posted on that finding's line (`move`), even if the anchor text is on several lines. The old, bot-resolved thread can't pair again. Given that finding's line can't be commented on in the PR diff, then nothing is resolved or posted, and the finding is withheld `not_commentable`. Given an outdated comment with no pairing finding, then it is resolved if its anchor is on no line of its file's PR diff; otherwise it is kept, and nothing is posted.
- **AC6:** Given a file renamed in the PR, with findings found again under the new path, then the outdated comments move there, keeping their fingerprints. Comments not found again are kept, and none is created twice.
- **AC7:** Given a finding paired with a thread **a person** resolved, then nothing is posted, including after a rename. Given a finding whose only match is a thread **the bot** resolved, then it is unpaired, so it is created.
- **AC8:** Given two open comments with one fingerprint, then the one that isn't outdated is kept (else the older one), and the other is resolved.
- **AC9:** Given a comment by another user that holds a valid marker, a bot comment with no marker, and a bot comment with a malformed marker (15 hex digits, or text after it), then none is touched, and none blocks a create.
- **AC10:** Given 8 open own comments, a limit of 10 and 5 new findings, then 2 are created and 3 are withheld `over_limit`.
- **AC11:** Given outcome `skipped` for any reason (fork, action, `no_credential`, draft, `nothing_to_review`), then the fake records no call.
- **AC12:** Given a secret in the diff, then only the summary changes, naming the kinds and diff lines, never values. Given the provider fails with `transient`, then only the summary changes, saying the review is unavailable. Exit 11.
- **AC13:** Given the fake fails the second of three operations, then it exits 18, naming the operation. `shadow.json` is still written, with `applied` `done`, `failed` and `not_attempted` on the matching entries.
- **AC14:** Given a draft PR, then `outcome` is `skipped` with `reason` `draft`.
- **AC15:** Given `--mode comment` from the command line with no injected client, then it exits 2 saying comment mode needs the GitHub client, before any provider call; P2-S6 AC1 replaces this. Given `--mode shadow`, then no client is built and no token is read.
- **AC16:** Given a move (AC5), then two further runs with the same finding post, move and resolve nothing; the moved comment is `keep`. Given a commented line deleted (so the comment is resolved) and then restored with the finding back, then the first run after the restore creates one comment, and two further runs post nothing new.

## Limits
Own comments are recognised only by author plus an exact marker on the body's last line, so other people's text is never parsed further. Planning is linear in comments plus findings per key.

## Interfaces
[Design](../design.md#interfaces). `FakeGitHubClient` holds comments, threads (with their resolver) and a summary. It can mark a comment outdated, records every call, and can fail a named operation.

## Out of scope
- The real client and the comment job (S6); only new commits (S7).

## Notes
