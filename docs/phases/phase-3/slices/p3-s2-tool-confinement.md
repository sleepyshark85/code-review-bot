# P3-S2: Keep the tools inside the commit on hostile paths and repositories

- **Status:** Done
- **Depends on:** P3-S1
- **Touches:** `context` (`paths`, `tools`), `git`

## Value
The model can't use the tools to read outside the commit, follow a link, open `.git` or a secrets file, inject a git option, or send a secret to the vendor, whatever it asks and whatever the repository holds.

## Acceptance criteria
As P3-S1, with fixture commits built for each case. Rules: [Tool safety](../design.md#tool-safety).
- **AC1:** Given `read` with any of these paths, then it's refused `invalid_path` and git isn't run: `../x`, `/etc/passwd`, `C:/x`, `billing\invoice.py`, `a//b`, `./a`, `a/./b`, `a/../b`, `""`, a path holding NUL, a line feed or `\x1b`, and a 1,001-character path. `glob` patterns and `grep`'s `path_glob` with a `..` segment, a leading `/`, a leading `:` (such as `:(top)*` or `:!x`) or a backslash are refused the same way.
- **AC2:** Given `.git/config`, `sub/.git/HEAD` or `.GIT/config`, then it's refused `denied_path`.
- **AC3:** Given a commit with a symbolic link `out` to `../../etc/passwd` and one `in` to `billing/invoice.py`, then reading either is refused `symlink`, neither target's text is returned, and neither appears in `grep` or `glob` results. A submodule entry is refused `submodule` and is likewise left out.
- **AC4:** Given a commit holding each of `.env`, `config/.env.production`, `deploy/server.PEM`, `id_rsa`, `.npmrc`, `credentials.json`, `.git-credentials`, `putty.ppk`, `.htpasswd`, `.pgpass`, `infra/terraform.tfstate`, `prod.tfvars` and `vault.kdbx`, each containing `MARKER`, then reading each is refused `denied_path`, `grep` for `MARKER` returns no line from them, and `glob` `**` doesn't list them. `id_ed25519.pub` is readable.
- **AC5:** Given `app/settings.py` with an AWS-format access key on line 7, then reading the whole file, or any window overlapping line 7 (such as lines 7 to 7 or 5 to 9), is refused `possible_secret`, while lines 1 to 5 are returned. `grep` matching lines 6 to 8 returns lines 6 and 8, and notes 1 line withheld. The key's value is in no request, audit record or report.
- **AC6:** Given a file holding a NUL byte, or bytes that aren't UTF-8, then reading it is refused `binary`, and `grep` doesn't search it.
- **AC7:** Given a blob over 1 MB, then reading it is refused `too_large`, and at most 1 MB plus one byte of it is read from git. Given a commit with more than 50,000 entries, then `glob` and `grep` are refused `listing_too_large`.
- **AC8:** Given a file whose name holds a line feed, a control character or bytes that aren't UTF-8, then `glob` lists it in git's C-quoted form ([Phase 1 audit paths](../../phase-1/design.md#audit-log)), no raw control character reaches any result, and reading it is refused `invalid_path`.
- **AC9:** Given `grep` with the literal `(a+)+$` on a commit holding one 1 MB line of `a`, then it completes within the git timeout and is treated as a literal. Given a git command that exceeds the timeout, then that call is refused `timeout` and the review goes on.
- **AC10:** Given a `grep` call, then git's argument list has the literal right after `-e`, then the commit, then `--` with nothing after it: `path_glob` is never passed to git, and is applied to the results in our code with P2-S9's matcher.
- **AC11:** Given `grep` literals `-Ofoo`, `--open-files-in-pager=x`, `-f/etc/passwd` and `--no-index`, then each is searched as plain text: a fixture line holding it is found, no program runs (a canary script named by `-O` leaves no trace), and no file outside the commit is read.
- **AC12:** Given `read` of `billing/Invoice.py` when the commit holds only `billing/invoice.py`, or of a path git would resolve to a different tree entry, then it's refused `not_found`: the tree entry's path must equal the requested path exactly.
- **AC13:** Given a file holding a PEM private key block on lines 10 to 20, then reading lines 1 to 12, 15 to 15 or 18 to 30 is refused `possible_secret`, reading lines 1 to 9 is returned, and `grep` for a string on line 15 returns nothing from that block.
- **AC14:** Given a commit holding `cfg_AKIA` followed by 16 key characters `.txt` (a path in which the secret scan finds anything), then `glob` `**` and `grep` for a string inside it leave that path out, as for a denied file; a `read` of it is refused `possible_secret` with a message that doesn't echo the path; and neither the path nor the key appears in any result, `files_read`, the report or the audit log.
- **AC15:** Given a commit of 20,000 Latin-1 files each holding the literal, plus one UTF-8 file holding it, then one `grep` call reads at most 100 candidate files (files with a match, after `path_glob`), stops there with the truncated note, so its git processes are bounded by the 100 files and not by the repository, and its withheld count says it covers only the files read.

## Limits
Largest input: a commit of 50,000 entries, blobs up to 1 MB, and a literal of 200 characters. The secret scan runs on each whole blob once per file and commit, in time linear in its size. `git grep -F` runs in time linear in the bytes searched, bounded by the timeout, and one call reads at most 100 candidate files whatever the repository holds; every output is read up to its cap plus one byte. Malformed git output stops that call with a refusal, never the review.

## Interfaces
Refusal reasons added: `invalid_path`, `denied_path`, `symlink`, `submodule`, `binary`, `too_large`, `listing_too_large`, `possible_secret`, `timeout`. The deny-list is the one in [Tool safety](../design.md#tool-safety), matched case-insensitively on the last path segment.

## Out of scope
- Redacting a secret in place of refusing it (Phase 4).

## Notes
- **Fixtures run on Windows without skips:** symbolic links, submodules and odd names are built with git plumbing (`hash-object -w`, `mktree`, `update-index --cacheinfo`, `commit-tree`), never created in the file system.
- **Changed in build:**
  - A git failure other than a timeout or an oversized listing, such as unreadable output, refuses the call `git_failed`. The Limits require a refusal, and no listed reason fits.
  - `read`'s `path` has no schema limits now, so AC1's empty, control-character and 1,001-character paths give `invalid_path`, not S1's `invalid_arguments`. A drive (`C:/`, or `C:` alone) is refused like a leading `:`; `x:notes.md` is a name. `context.paths` names its checks `is_valid_path` and `is_valid_glob`, since nothing is normalised. SSH key names match `id_rsa` and `id_rsa.*`, except a name ending `.pub`.
  - git runs every command with `--literal-pathspecs`. `read_file` keeps only the `ls-tree` entry whose path equals the one asked for (AC12). `GitTimeoutError` and `ListingTooLargeError` subclass `GitError`. `GitError.exit_code` and `GIT_FATAL_EXIT_CODE` are gone, since nothing reads them now.
  - `secret_scan.secret_line_spans` gives the merged line spans of each hit and each private key block. C-quoting moved from `report` to `path_quoting`. A result C-quotes a path holding a control character, a byte that isn't UTF-8, `"` or `\`.
  - `context.tools` is split to stay under 400 lines: `context.tool_args` holds the argument schemas and tool definitions, `context.tool_text` shapes result text, and `context.commit_files` holds the files a commit may show, their text and their secret spans. The schemas' `title` now reads `ReadArguments` and so on, without the leading underscore.
  - `grep` leaves out every file `read` would refuse, and stops reading files once 51 matches are kept or 100 files were read (AC15); its withheld note says the count covers the files read.
  - After review round 1: `GitRepo.grep` streams and parses its own `-z` output, keeps only the matches in files a `keep` predicate accepts (the listing, deny and secret-path filters and `path_glob`, still never passed to git), and applies the 1 MB cap to the kept matches, so a file past the raw output's first megabyte is still found. It returns `GrepResult` (matches, and whether the kept matches reached the cap). `TreeEntry.is_regular_file` names the regular-file modes once. `git_output` holds git's output formats (`TreeEntry`, `GrepMatch`, `GrepResult`, the `ls-tree` and `grep` readers), split from `git` to stay under 400 lines; its unreadable-listing message no longer names the clone. `CommitFiles.text` returns the text with its secret spans (`FileText`).
  - AC14: `secret_scan.path_holds_secret` runs `scan_secrets` once over the path, then each vendor key pattern once with a start that may also follow `/`, `_`, `-` or `.`, so `cfg_<key>.txt` is found in linear time; OpenAI's digit check is done in code on the matched run. `CommitFiles.listed` lists and scans the commit once per review. Accepted false positive: a name such as `my-sk-runner-2024-abcdefghijk.py` reads as an OpenAI key and is hidden (round 3).
- **Amended by the designer, 2026-11-09, after the code review:** AC14 (a path in which the secret scan finds anything is never shown, in any result, report or audit record) and AC15 (one `grep` call reads at most 100 candidate files, a fixed cap in [the caps table](../design.md#spend-and-caps)) are added. No criterion is renumbered.
