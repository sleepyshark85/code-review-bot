# P1-S8: Catch `open()` without an encoding with no model

- **Status:** Provisional
- **Depends on:** P1-S7
- **Touches:** `rules`

## Value
A developer gets a deterministic finding for a platform-dependent text file, which is what this repository's own conventions require.

## Acceptance criteria
With `MockProvider(findings=[])`. Each hit renders as `{file}:{line} [low/reliability] open() without encoding uses the platform default. Pass encoding="utf-8", or open in binary mode. (confidence 1.00)`.
- **AC1:** Given `Assignment/sample_diff.txt`, then stdout has exactly two lines, in order: `billing/invoice.py:35`, from P1-S7, and this finding at `billing/invoice.py:39`; `rule_findings=2`.
- **AC2:** Parametrised over `open(p)`; `open(p, "w")`; `open(p, mode="a+")`; `with open(p) as f:`; `io.open(p)`; a call spread over 3 added lines, then one finding, at the line holding `open(`.
- **AC3:** Parametrised over `open(p, encoding="utf-8")`; `open(p, "w", encoding=enc)`; `open(p, "rb")`; `open(p, mode="wb")`; `open(p, "r+b")`; a multi-line call with `encoding=` on its last line, then none.
- **AC4:** Parametrised over `os.open(p, flags)`; `self.open(p)`; `Path(p).open()`; `reopen(p)`; `# open(p)`; `x = "open(p)"`; `open(p, mode)` with a non-literal mode; a call still unclosed at the hunk's end, then none: when unsure, stay silent.
- **AC5:** Given the call as a context or removed line, or in `x.js` or a skipped file, then none.
- **AC6:** Given a line holding two calls, one with an encoding and one without, then one finding. Given two without, then two findings on that line with different fingerprints.

## Interfaces
`run_rules`, `RuleId`: [design.md](../design.md#interfaces).

## Out of scope
- `Path.open`, `read_text` and `write_text`: a later rule, if the evals show a need.

## Notes
