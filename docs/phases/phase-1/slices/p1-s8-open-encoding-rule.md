# P1-S8: Catch `open()` without an encoding with no model

- **Status:** Done
- **Depends on:** P1-S7
- **Touches:** `rules`, `findings`, `fingerprint` (changed in build, main session, 2026-11-06)

## Value
A developer gets a deterministic finding for a platform-dependent text file, which is what this repository's own conventions require.

## Acceptance criteria
With `MockProvider(findings=[])`. Each hit renders as `{file}:{line} [low/reliability] open() without encoding uses the platform default. Pass encoding="utf-8", or open in binary mode. (confidence 1.00)`.
- **AC1:** Given `Assignment/sample_diff.txt`, then stdout has exactly two lines, in order: `billing/invoice.py:35`, from P1-S7, and this finding at `billing/invoice.py:39`; `rule_findings=2`.
- **AC2:** Parametrised over `open(p)`; `open(p, "w")`; `open(p, mode="a+")`; `with open(p) as f:`; `io.open(p)`; a call spread over 3 added lines, then one finding, at the line holding `open(`.
- **AC3:** Parametrised over `open(p, encoding="utf-8")`; `open(p, "w", encoding=enc)`; `open(p, "rb")`; `open(p, mode="wb")`; `open(p, "r+b")`; a multi-line call with `encoding=` on its last line, then none.
- **AC4:** Parametrised over `os.open(p, flags)`; `self.open(p)`; `Path(p).open()`; `reopen(p)`; `# open(p)`; `x = "open(p)"`; `open(p, mode)` with a non-literal mode; a call still unclosed at the hunk's end, then none: when unsure, stay silent. A call still unclosed at the hunk's end, or spanning more than 20 lines, isn't a hit, so it isn't counted in `unanchored_hits` either (changed in build, main session, 2026-11-06).
- **AC5:** Given the call as a context or removed line, or in `x.js` or a skipped file, then none.
- **AC6:** Given a line holding two calls, one with an encoding and one without, then one finding. Given two without, then two findings on that line with different fingerprints.
- **AC7 (changed in build, main session, 2026-11-06):** Given an added `open(p)` in a hunk with an unreadable header, then no finding is printed, `RuleResult.unanchored_hits` is 1, and the `static.rules` record has that count, as in [P1-S7 AC7](p1-s7-swallowed-exception-rule.md#acceptance-criteria).

## Limits
Changed in build, main session, 2026-11-06.
- **Input:** a diff up to the [size limit](../../phase-0/design.md#settings); the largest case is one 400 KB added line, such as 20,000 `open(p)` calls.
- **Lookahead:** a call is read over at most 20 new-version lines from its `open(`; a longer one isn't a hit (AC4).
- **Cost:** linear in the hunks' text on hostile input: thousands of calls on one line, deeply nested or unbalanced brackets, many unclosed calls, and an unclosed triple-quoted string.
- **Malformed input:** unbalanced brackets, unclosed strings and unreadable headers stay silent or count as unanchored (AC7); none fails the run.
- **Ambiguous strings:** a hunk that ends with a triple-quoted string still open, as when it starts inside a docstring, gives no hit at all, since which text is code can't be told (changed in build, main session, 2026-11-06).
- **Known false positives, contrived (changed in build, main session, 2026-11-06):** `open(p)` inside a Python 3.12 f-string with nested quotes of the same kind (`f"{d["open(p)"]}"`), inside a triple-quoted string holding an escaped quote, inside a single-quoted string continued with a backslash, or in a hunk that starts inside one docstring and ends inside another, so its triple quotes balance.

## Interfaces
`run_rules`, `RuleId`: [design.md](../design.md#interfaces).

## Out of scope
- `Path.open`, `read_text` and `write_text`: a later rule, if the evals show a need.
- `builtins.open(p)`: silent like any attribute call (AC4) (changed in build, main session, 2026-11-06).

## Notes
- **Handover (changed in build, main session, 2026-11-06):** AC1 replaces P1-S7 AC1's `rule_findings=1` on the sample diff. P1-S7's tests now use a variant sample with `encoding="utf-8"` for their exact-output checks.
- **Implementation choices (developer):** the rule tokenises the hunk's new-version lines in one pass (strings, triple-quoted strings across lines and comments skipped) and matches brackets on a stack, so its work is linear. A call spanning more than 20 lines, closed by a mismatched bracket, with unpacked arguments, or with no file argument is not reported. A fourth positional argument counts as an encoding. A backslash ending a line separates tokens like a space. A hunk that ends inside a triple-quoted string gives no `open` hit at all, counted or not, as it may have started inside one.
- **Deviation (developer):** `fingerprint` is touched, with no interface change: `Fingerprinter` hashes each anchor text once, as 20k calls on one long line made fingerprinting quadratic.
