# P3-S11: Route findings to code owners

- **Status:** Draft
- **Depends on:** none
- **Touches:** `context.codeowners`, `pr` (`runner`, `shadow`, `comments`), `git`

## Value
Each would-be comment names the code owners of its file, and the summary groups findings by owner, so the right people see their share, without the bot notifying anyone.

## Acceptance criteria
`run`, as in [P2-S2](../../phase-2/slices/p2-s2-review-pr-event.md), in either mode, with CODEOWNERS read from the **base** commit like `.review-bot.toml` ([ADR 0008](../../../adr/0008-pull-request-action-with-github-token.md)).
- **AC1:** Given `.github/CODEOWNERS` with `* @org/core`, then `/billing/ @org/billing alice@example.com`, then `*.md @docs`, then findings in `billing/invoice.py`, `api/views.py` and `billing/README.md` have `owners` `["@org/billing", "alice@example.com"]`, `["@org/core"]` and `["@docs"]`: the last matching line wins.
- **AC2:** Given files at `.github/CODEOWNERS`, `CODEOWNERS` and `docs/CODEOWNERS`, then only the first that exists, in that order, is read. Given the head changes it, then the base's still applies.
- **AC3:** Given patterns `/docs/`, `logs/`, `apps/**`, `**/build` and `*.js`, then they match as GitHub documents: anchored at the root with a leading `/`, at any depth without one, a trailing `/` matching everything under a directory, and `*` within one segment.
- **AC4:** Given a pattern with no owners, then paths it matches last have `owners` `[]`. Given a line using `!`, `[` or an owner that is neither `@user`, `@org/team` nor an email address, then that line is skipped, and `codeowners.invalid_lines` counts it. Comments and blank lines are ignored.
- **AC5:** Given no CODEOWNERS, then `codeowners` is `{"source": null, "status": "absent", "rules": 0, "invalid_lines": 0}` and every `owners` is `[]`. Given one over 3 MB, not UTF-8, or a symbolic link, then `status` is `too_large`, `invalid` or `not_a_file`, owners are `[]`, and the run goes on with exit unchanged.
- **AC6:** Given comments and withheld findings, then each `shadow.json` entry has `owners`, and the summary adds `Owners:`, one item per owner, `` `@org/billing`: 2 ``, by count then name, at most 10 then `and K more`, then `no owner: N` if any. With no owners at all, there's no such section.
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
