# P3-S10: Enforce domain rules configured per repository

- **Status:** Done
- **Depends on:** P3-S5
- **Touches:** `rules`, `findings` (`RuleId`), `pr.config`, `prompt` (next context version), `review`, `pr.runner`, `evals` (`replay`), `tests/evals/cases/`

## Value
A repository switches on a RightShip domain rule, such as `Decimal` money in `billing/`, for the paths it names; code enforces it with no model, and in context mode the model is told the rule too.

## Acceptance criteria
`run`, as in [P2-S9](../../phase-2/slices/p2-s9-repo-config.md), with `.review-bot.toml` at the base holding `[[domain_rules]]` `rule = "decimal-money"`, `paths = ["billing/**"]`. Decision: [ADR 0017](../../../adr/0017-domain-rules-from-a-catalogue.md).
- **AC1:** Given added lines in `billing/invoice.py`, then each of these gets one rule finding, `decimal-money`, category `logic`, severity `high`, confidence 1.00, on its line: `price = 19.99`, `rate = .5`, `big = 1e3`, `amount = float(raw)`, `pay = float(1.5) * 2.0` (one line, one finding however many hits). None of these does: `tax = Decimal("0.2")`, `count = 3`, `# rate 0.5`, `label = "0.5"`, `z = 2j`, `mask = 0xE5`, `x = obj.float(raw)`, `def float(self):`.
- **AC2:** Given the same lines in `api/views.py`, in a removed line, or in `billing/rates.json`, then there's no `decimal-money` finding. Given no `domain_rules`, then there's none anywhere.
- **AC3:** Given `severity = "medium"` in the entry, then its findings are `medium`. Given a second entry enabling `decimal-money` with `severity = "critical"` and `paths = ["billing/invoice.py"]`, then `price = 19.99` there gets one finding, `critical`, with the entries in either order: the most severe matching entry wins.
- **AC4:** Given an unknown `rule`, a missing or empty `paths`, a pattern breaking P2-S9's pattern rules, more than 20 entries, an invalid `severity` or an unknown key in an entry, then the run fails `invalid_config`, naming the entry and key, with no provider call, and exits 2.
- **AC5:** Given an added line that ends inside a string, `s = """0.5` or `s = "0.5`, then it's skipped without error, and the `static` `rules` record counts it in `unchecked_lines`. Given `total = (19.99,` or `total = 1.5 + \`, then each gets its finding and isn't counted. Given a 300 KB added line of `1.5,` repeated, then it's checked in time and memory linear in its length, with one finding.
- **AC6:** Given context mode (the only mode whose prompt offers the tag) and a model finding tagged `decimal-money`, within 3 lines of the rule finding in the same hunk and no more severe, then it's reported once, as the rule's ([Phase 1 dedup](../../phase-1/design.md#fingerprints-and-dedup)).
- **AC7:** Given context mode, then the context prompt names each enabled entry's rule with its fixed description and its path patterns written as one JSON list (quoted, control characters escaped), so a pattern holding `\n` adds no prompt line; with no `domain_rules` it offers no domain rule tag. Its version is `p3-v4`. Given diff mode, then the system and user prompts stay byte-for-byte `p1-v1`, and offer only the two built-in rule tags, though `RuleId` has a new value.
- **AC8:** Given any run, then `shadow.json`'s `config` lists `domain_rules`, and rule findings carry `rule` `decimal-money` in `report/v1`, the comment marker and the fingerprint's rule part.
- **AC9:** Given the eval case `domain-money` (a diff adding float money under `billing/`, and a `repo` whose `.review-bot.toml` switches the rule on), then replay in either mode scores its rule bug caught, from the case repository's config, and Phase 1's cases are unaffected. Given that file invalid as in AC4, not TOML, or over 64 KB, then replay in either mode scores no case and exits 2, naming the case file, `.review-bot.toml` and the problem.

## Limits
Each added line of a matched Python file is tokenised alone, in time and memory linear in its length (not with the standard library's `tokenize`, which copies the line per token); only a line ending inside a string is skipped (AC5). Lines are checked one by one, so a line inside a multi-line string elsewhere may be flagged. Patterns are bounded as in P2-S9.

## Interfaces
`[[domain_rules]]`: `rule` (only `decimal-money` in Phase 3), `paths` (1 to 20 patterns, P2-S9's glob rules), optional `severity` (default `high`). The check: on added lines of `.py` files whose new path matches, a number token with a decimal point or exponent, or the name `float` called.
`pr.config.parse_config_file(data: bytes, *, source: str) -> RepoConfig` parses a `.review-bot.toml` with the same schema and limits as `run`, recording `source` as the config's `source`; it raises `ConfigFileError` naming the entry and key or the problem, and the caller says where the file is. Replay uses it for a case repository's config (AC9).

## Out of scope
- Other rules, regular expressions or free-text guidance from config ([ADR 0017](../../../adr/0017-domain-rules-from-a-catalogue.md)); domain rules for `review-bot` without a repository config.

## Notes
- **README (main session):** document `[[domain_rules]]`.
- **Build:** the tokeniser is `python_tokens`, split from `rules` (one possessive regular expression); on CPython 3.12.3 `tokenize` ran out of memory on one long line. Context prompt `p3-v3` is on `main` (P3-S12), so this slice takes `p3-v4`.
