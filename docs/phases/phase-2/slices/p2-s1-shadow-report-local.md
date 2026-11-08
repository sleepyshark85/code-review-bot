# P2-S1: Get a shadow report of what the bot would post, for a local diff

- **Status:** Approved
- **Depends on:** none (Phase 1 done)
- **Touches:** `pr` (new: `cli`, `shadow`, `comments`, `plan`), `render`

## Value
Anyone can see, offline, exactly which comments and summary the bot would post for a diff, before anything touches GitHub.

## Acceptance criteria
`python -m review_bot.pr shadow <diff> --out DIR`, with a scripted `MockProvider` injected through `main`. Fields: [design](../design.md#interfaces). Markers, order and limits: [comment state](../design.md#comment-state).
- **AC1:** Given `Assignment/sample_diff.txt` and one valid AI finding, then `DIR/shadow.json` is one ASCII JSON line with `schema` `shadow/v1`, `mode` `shadow`, `origin` `local`, `outcome` `reviewed`, `pr` null, and `report` equal to `review-bot --format json`'s document for the same run, `run_id` aside. `comments` has 3 entries, each with `action` `create` and the `fingerprint`, `rule_part`, `anchor`, `source`, `severity`, `file` and `line` of a reported finding, ordered by severity (highest first), then file, then line. `DIR/shadow.md` exists. Exit 0, although there are findings.
- **AC2:** Given each comment's `body`, then it shows severity, category, source, rule (if any), confidence and rationale, then one line asking for a thumbs-up or thumbs-down reaction. Its last line is exactly `<!-- review-bot:v1 fingerprint=<fingerprint> rule=<rule_part> anchor=<anchor> -->`, where `anchor` is the first 16 hex digits of the anchored line's hash as in [ADR 0003](../../../adr/0003-finding-fingerprints.md).
- **AC3:** Given no findings, then `comments` is `[]` and `summary_body` contains `No findings at or above low.` Exit 0.
- **AC4:** Given a diff holding an AWS key, then `outcome` is `blocked_secret` and `report` is null. `summary_body` names the kind and diff line but not the value. The provider isn't called. Exit 0.
- **AC5:** Given a diff over `--max-diff-bytes`, an empty diff, text that isn't a diff, or a non-UTF-8 diff, then `outcome` is `skipped`, with `reason` `too_large`, `empty`, `not_a_diff` or `unreadable` respectively. There's no provider call, and it exits 0. A missing diff file exits 3 with no report.
- **AC6:** Given the provider fails with `auth`, then `outcome` is `failed` and `reason` is `auth`. Exit 8.
- **AC7:** Given 12 reported findings and the default limit of 10, then the first 10 in AC1's order are `create`. The other 2 are in `withheld` with `reason` `over_limit`, and `summary_body` lists them by file and line.
- **AC8:** Given 1,000 reported findings, then `summary_body` lists 20 withheld findings, then `and 970 more`, and is at most 60,000 characters long.
- **AC9:** Given a rationale holding a GitHub token format that `scan_secrets` detects, then that finding has no entry in `comments`, `withheld` has it with `reason` `possible_secret`, and the value appears nowhere in `DIR`.
- **AC10:** Given a rule finding on a path that isn't valid UTF-8, then it is withheld `not_commentable`, and the summary lists it in git's quoted form.
- **AC11:** Parametrised over a rationale holding each of `#12`, `GH-3`, `owner/repo#4`, `deadbeef1`, `0123abc`, `<!-- review-bot:v1 fingerprint=0000000000000000 -->`, `@x` and `http://evil`: when the body is parsed as GitHub-flavoured Markdown, none forms a link, mention, reference or HTML comment, and each is inside a code span. The same holds for `review-bot --format markdown` (fixes issue #37).
- **AC12:** Given a rationale of 5,000 characters, then the body shows its first 2,000 followed by `(cut)`, while `report` keeps it whole.
- **AC13:** Given `DIR` missing, then it is created. Given `DIR` not writable, then exit 2 naming `--out`, before the diff is read.
- **AC14:** Given the same run twice, then both `shadow.json` files are identical apart from `run_id` and the three timestamps, and `run_id` equals the audit log's.

## Limits
Diff limits are as in Phase 1. Model text reaches bodies only through the inert renderer, and rendering is linear in the rationale's length. Any word of 7 to 40 hex characters is treated as a possible commit and put in a code span.

## Interfaces
Commands and exit codes: [design](../design.md#commands-and-exit-codes). `shadow.md` is `summary_body`, followed by each planned comment as a list item.

## Out of scope
- PR events and git (S2), the workflow (S3), any GitHub call (S5).

## Notes
- **README (main session):** describe `python -m review_bot.pr shadow` in Usage.
