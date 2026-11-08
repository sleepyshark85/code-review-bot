# P2-S9: Set the review policy per repository

- **Status:** Done
- **Depends on:** P2-S2
- **Touches:** `pr` (`config`, `git`, `runner`), and `review` or `diff_parser` (a skip reason)

## Value
A repository sets its threshold, excluded paths and comment limit in one committed file, and a PR can't loosen the rules it is reviewed under.

## Acceptance criteria
`run`, with a git repository in `tmp_path` and a scripted `MockProvider`. The file is `.review-bot.toml` at the root, read from the **base** commit ([ADR 0008](../../../adr/0008-pull-request-action-with-github-token.md)).
- **AC1:** Given no file at the base, then the defaults apply: the threshold from the settings, no exclusions, and `max_open_comments` 10. `shadow.json`'s `config` shows them with `source` `default`.
- **AC2:** Given `threshold = "medium"` at the base, then `low` findings aren't planned, and `config.source` is `.review-bot.toml@<base SHA>`. Given the head changes it to `"critical"`, then the base's value still applies.
- **AC3:** Given `exclude = ["generated/**", "*.snap", "src/*.json"]`, then exactly these files are skipped, with reason `excluded_by_config`:
  - **Skipped:** `generated/a.py`, `generated/x/y.py`, `a.snap`, `tests/x/a.snap`, `src/a.json`.
  - **Not skipped:** `src/generated/a.py`, `src/x/a.json`.

  Skipped files aren't sent to the model, don't count towards reviewable bytes, and are listed in `skipped_files`.
- **AC4:** Given `max_open_comments = 3` and 5 new findings on a PR with no open comments, then 3 are created and 2 are withheld `over_limit`.
- **AC5:** Given any of the following, then `outcome` is `failed` with `reason` `invalid_config`, the summary names the key or problem, there's no provider call, and it exits 2:
  - invalid TOML;
  - an unknown key, caps included;
  - a value of the wrong type;
  - `max_open_comments` of 0 or 26;
  - `threshold = "urgent"`;
  - more than 50 patterns, or a pattern over 200 characters;
  - a file over 64 KB;
  - a symlink or a directory at that path.
- **AC6:** Given `REVIEW_BOT_THRESHOLD=high` and the file's `medium`, then `high` applies. The order is flag, then environment, then file, then default.

## Limits
The file is read through git from the base commit only, at most 64 KB, and parsed with `tomllib` into a schema that forbids unknown keys. Glob matching is linear in paths times patterns, and both are bounded.

## Interfaces
Keys:
- `threshold`: `low`, `medium`, `high` or `critical`.
- `exclude`: globs on the new path. `*` matches within one path segment, and `**` matches any number of segments, including none. A pattern with no `/` matches the file name at any depth, and one with a `/` is anchored at the root.
- `max_open_comments`: 1 to 25.

The mode and the caps stay in the workflow ([design](../design.md#ci-settings-and-caps)).

## Out of scope
- Config for the local `shadow` command; per-rule switches; other repositories.

## Notes
- **README (main session):** document `.review-bot.toml`, read from the base branch.
- **Build:** a deleted file has no new path, so `exclude` matches its old path, as the built-in skips do. A built-in skip reason (`docs`, `lock`, `generated`, `binary`) wins over `excluded_by_config`. `config` in `shadow.json` is null when the run read none: refused, or the file is invalid. An `exclude` pattern with an empty segment (a leading or trailing `/`, `//`, or an empty pattern) matches no path, so it is invalid (`invalid_config`), naming the pattern.
