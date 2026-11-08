# P3-S11: Route findings to code owners

- **Status:** Done
- **Depends on:** none
- **Touches:** `context.codeowners`, `pr` (`runner`, `shadow`, `comments`), `pr.git` (or `git`, if S1's move has merged first)

## Value
Each would-be comment names the code owners of its file, and the summary groups findings by owner, so the right people see their share, without the bot notifying anyone.

## Acceptance criteria
`run`, as in [P2-S2](../../phase-2/slices/p2-s2-review-pr-event.md), in either mode, with CODEOWNERS read from the **base** commit like `.review-bot.toml` ([ADR 0008](../../../adr/0008-pull-request-action-with-github-token.md)).
- **AC1:** Given `.github/CODEOWNERS` with `* @org/core`, then `/billing/ @org/billing alice@example.com`, then `*.js @docs`, then findings in `billing/invoice.py`, `api/views.py` and `billing/widget.js` have `owners` `["@org/billing", "alice@example.com"]`, `["@org/core"]` and `["@docs"]`: the last matching line wins.
- **AC2:** Given files at `.github/CODEOWNERS`, `CODEOWNERS` and `docs/CODEOWNERS`, then only the first that exists, in that order, is read. Given the head changes it, then the base's still applies. Given a directory or symbolic link at `.github/CODEOWNERS`, then that location is skipped and the next is tried; if no location holds a regular file, `status` is `not_a_file` and owners are `[]` (AC5).
- **AC3:** Given patterns `/docs/`, `logs/`, `apps/**`, `**/build` and `*.js`, then they match as GitHub documents, by gitignore rules: a pattern with a `/` at its start or in its middle is anchored at the root (`/docs/`, `apps/**`: `apps/x.py` matches, `src/apps/x.py` doesn't); one whose only `/` is trailing, or none, matches at any depth (`logs/`, `*.js`); a leading `**/` matches at any depth (`**/build`); a trailing `/` matches everything under that directory; and `*` stays within one segment.
- **AC4:** Given a pattern with no owners, then paths it matches last have `owners` `[]`. Given a line using `!`, `[` or an owner that is neither `@user`, `@org/team` nor an email address, then that line is skipped, and `codeowners.invalid_lines` counts it. Comments and blank lines are ignored.
- **AC5:** Given no CODEOWNERS, then `codeowners` is `{"source": null, "status": "absent", "rules": 0, "invalid_lines": 0}` and every `owners` is `[]`. Given one over 3 MB, not UTF-8, or only non-files at every location (AC2), then `status` is `too_large`, `invalid` or `not_a_file`, owners are `[]`, and the run goes on with exit unchanged.
- **AC6:** Given comments and withheld findings, then each `shadow.json` entry has `owners`, and the summary adds `Owners:`, one item per owner, `` `@org/billing`: 2 ``, by count then name, at most 10 then `and K more`, then `no owner: N` if any. Counts include both comments and withheld findings, and a finding with two owners counts once for each; `no owner` counts findings whose `owners` is `[]`. With no owners at all, there's no such section.
- **AC7:** Given any summary or body, then no `@` or email address is outside a code span (checked as [P2-S1 AC11](../../phase-2/slices/p2-s1-shadow-report-local.md#acceptance-criteria)), and comment bodies are unchanged.
- **AC8:** Given a refused run, then CODEOWNERS isn't read.

## Limits
The file is read through git from the base commit, at most 3 MB, parsed line by line. Patterns are matched segment by segment with no backtracking pattern, in time linear in rules times path segments per finding.

## Interfaces
`shadow/v1` adds `codeowners` (`source` such as `.github/CODEOWNERS@<base SHA>` or null, `status`: `found`, `absent`, `too_large`, `invalid`, `not_a_file`; `rules`; `invalid_lines`) and `owners` on each comment and withheld entry.

## Out of scope
- Notifying owners or requesting their review ([Q8](../design.md#6-open-questions)); owners in local `shadow` runs.

## Notes
- **README (main session):** say the summary groups findings by CODEOWNERS owner, without mentions.
- **Build:** CODEOWNERS is read only by a run that plans comments, so `codeowners` is null when the run read none (refused, failed, nothing to review, or a local run), as `config` is; `owners` is then `[]`. A resolve entry has the `owners` of its path. A pattern with a `/` before its end is anchored, as in gitignore and P2-S9, so `apps/**` owns `apps/x.py` but not `src/apps/x.py`; one ending in a name owns everything under it, and one ending in a wildcard only what it matches (`docs/*`). An owner's parts are length-capped (login 39, team 100, email local part 64, domain label 63), so the summary stays bounded; an empty-segment pattern (`a//b`, `/`) is an invalid line. When only non-files are found, the `not_a_file` `source` names the first of them.
- **Amended by the designer, 2026-11-09, after approval:** AC1's third example is now `*.js @docs` on `billing/widget.js`, since Phase 1 skips `.md` files as docs and no finding could exist there. AC3 now follows GitHub's gitignore rules: a `/` at the start or middle anchors at the root, so `apps/**` doesn't match `src/apps/x.py`. AC2 settles a directory or symbolic link at `.github/CODEOWNERS` (skipped, next location tried); AC5 matches. AC6 settles the owner counts: comments and withheld findings, once per owner. No criterion is renumbered.
