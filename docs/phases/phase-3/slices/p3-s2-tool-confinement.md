# P3-S2: Keep the tools inside the commit on hostile paths and repositories

- **Status:** Draft
- **Depends on:** P3-S1
- **Touches:** `context` (`paths`, `tools`), `git`

## Value
The model can't use the tools to read outside the commit, follow a link, open `.git` or a secrets file, or send a secret to the vendor, whatever it asks and whatever the repository holds.

## Acceptance criteria
As P3-S1, with fixture commits built for each case. Rules: [Tool safety](../design.md#tool-safety).
- **AC1:** Given `read` with any of these paths, then it's refused `invalid_path` and git isn't run: `../x`, `/etc/passwd`, `C:/x`, `billing\invoice.py`, `a//b`, `./a`, `a/./b`, `a/../b`, `""`, a path holding NUL, a line feed or `\x1b`, and a 1,001-character path. `glob` patterns and `grep`'s `path_glob` with a `..` segment, a leading `/` or a backslash are refused the same way.
- **AC2:** Given `.git/config`, `sub/.git/HEAD` or `.GIT/config`, then it's refused `denied_path`.
- **AC3:** Given a commit with a symbolic link `out` to `../../etc/passwd` and one `in` to `billing/invoice.py`, then reading either is refused `symlink`, neither target's text is returned, and neither appears in `grep` or `glob` results. A submodule entry is refused `submodule` and is likewise left out.
- **AC4:** Given a commit holding `.env`, `config/.env.production`, `deploy/server.PEM`, `id_rsa`, `.npmrc` and `credentials.json`, each containing `MARKER`, then reading each is refused `denied_path`, `grep` for `MARKER` returns no line from them, and `glob` `**` doesn't list them. `id_ed25519.pub` is readable.
- **AC5:** Given `app/settings.py` with an AWS-format access key on line 7, then reading the whole file is refused `possible_secret`, while reading lines 1 to 5 returns them. `grep` matching lines 6 to 8 returns lines 6 and 8, and notes 1 line withheld. The key's value is in no request, audit record or report.
- **AC6:** Given a file holding a NUL byte, or bytes that aren't UTF-8, then reading it is refused `binary`, and `grep` doesn't search it.
- **AC7:** Given a blob over 1 MB, then reading it is refused `too_large`, and at most 1 MB plus one byte of it is read from git. Given a commit with more than 50,000 entries, then `glob` and `grep` are refused `listing_too_large`.
- **AC8:** Given a file whose name holds a line feed, a control character or bytes that aren't UTF-8, then `glob` lists it in git's C-quoted form ([Phase 1 audit paths](../../phase-1/design.md#audit-log)), no raw control character reaches any result, and reading it is refused `invalid_path`.
- **AC9:** Given `grep` with the literal `(a+)+$` on a commit holding one 1 MB line of `a`, then it completes within the git timeout and is treated as a literal. Given a git command that exceeds the timeout, then that call is refused `timeout` and the review goes on.

## Limits
Largest input: a commit of 50,000 entries, blobs up to 1 MB, and a literal of 200 characters. `git grep -F` runs in time linear in the bytes searched, bounded by the timeout; every output is read up to its cap plus one byte. Malformed git output stops that call with a refusal, never the review.

## Interfaces
Refusal reasons added: `invalid_path`, `denied_path`, `symlink`, `submodule`, `binary`, `too_large`, `listing_too_large`, `possible_secret`, `timeout`. The deny-list is the one in [Tool safety](../design.md#tool-safety), matched case-insensitively on the last path segment.

## Out of scope
- Redacting a secret in place of refusing it (Phase 4).

## Notes
