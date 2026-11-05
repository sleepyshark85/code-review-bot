# P1-S6: Drop model findings whose file or line isn't in the diff

- **Status:** Approved
- **Depends on:** P1-S4
- **Touches:** `verify` (new), `policy` (new), `findings`, `review`, `cli`, `audit`

## Value
A developer never sees a finding pointing at a file or line that isn't in the diff, and can see how many were dropped.

## Acceptance criteria
Over a fixture with the sample diff's exact hunk headers and line numbers, but no rule hit (`except ValueError:` then `raise`; `open` with an encoding), so later rule slices don't change these results. With `MockProvider(findings=...)`, `file` being `billing/invoice.py`, unless stated. Rule: [design.md](../design.md#line-anchors).
- **AC1:** Parametrised over lines 12, 14, 30, 37 (blank context), 41, then each finding is printed in the unchanged line format and it exits 1.
- **AC2:** Parametrised over lines 1, 11, 15, 18, 29, 42, 44, then each is dropped, it exits 0, stdout is `No findings at or above low.`, and the summary has `dropped=1`. Lines 15, 18, 42 and 44 lie within the headers' declared ranges.
- **AC3:** Given `file` of `other.py`, `../billing/invoice.py`, `BILLING/INVOICE.PY`, or a path with `\x1b`, then it's dropped with reason `unknown_file`.
- **AC4:** Given `file` of `./billing/invoice.py`, `a/billing/invoice.py` or `b/billing/invoice.py`, then it's kept, and reported as `billing/invoice.py`. Given a real path `b/x.py` and a finding on `b/x.py`, it stays `b/x.py`.
- **AC5:** Given a diff with `README.md` and `app.py`, then a finding on `README.md` is dropped with `skipped_file`.
- **AC6:** Given a deleted file, then a finding on its old path is dropped with `unknown_file`. Given a hunk with an unreadable header, then any finding claiming its lines is dropped with `line_not_in_diff`. Given a file whose hunks hold only removed lines, then the same.
- **AC7:** Given 3 findings, 2 invalid, then only 1 is printed, in order, with `dropped=2`, and no warning. Given every finding invalid, then it exits 0 and stderr also has `review-bot: warning: all 3 model findings were dropped: their file or line isn't in the diff.`
- **AC8:** Given the threshold, then invalid findings are dropped first: one valid `low` and one invalid `critical` with `--threshold high` exits 0, `dropped=1`.
- **AC9:** Given any finding, then the log has a `verify` record for it with line, severity, outcome, reason and rationale SHA-256, with no rationale text; and a `policy` record for each kept finding. Given a `file` matching a diff path, the record has it. Given any other `file`, such as `SENTINEL-FILE.py` or 10,000 characters, the record has only its SHA-256 and length.
- **AC10 (no invalid anchor survives):** Given generated diffs and findings, including hostile headers and random line numbers, then every printed finding's file and line are an added or context line of `parse_diff`'s result.
- **AC11:** Given a diff with no rule hit and `--provider mock` with no scripted findings, then the canned finding is dropped, it exits 0, and the canned warning and `dropped=1` are on stderr.

## Interfaces
`verify_findings`, `VerifyResult`, `decide`: [design.md](../design.md#interfaces). `DropReason` values are the reasons above. `rule_findings` is empty until P1-S7.

## Out of scope
- Moving a finding to a near line ([ADR 0006](../../../adr/0006-line-anchors-drop-never-snap.md)).
- Checking the line is the right one for the issue: live evals only.

## Notes
- **Behaviour change:** verify has no provider exemption, so `--provider mock`'s canned `mock/example.py:1` never prints.
- **Handovers:**
  - P0-S1 AC1 is replaced by AC11.
  - `Bot.request_for` (`tests/integration/conftest.py:69`, 22 call sites) asserts exit 1 using the canned finding, and so does P0-S1 AC4's test at `test_p0_s1_cli.py:259`. Both must stop relying on it.
  - P0-S1 AC11 and AC19, P0-S2 AC10, P0-S3 AC2 and P0-S4 AC6 expect injected findings to print, so they need a diff containing those anchors. Move the default test finding (`src/app.py` line 7) and its diff into one shared helper.
  - P1-S5 AC1's test needs a diff containing its finding's anchor.
  - AC19's hostile `file` cases need a diff whose header has that path, or a direct `render_findings` test.
- **Test dependency:** AC10 uses `hypothesis`, added in P1-S2.
- **README (main session):** line 12's `--provider mock` canned output stops printing under this slice.
