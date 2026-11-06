# P1-S6: Drop model findings whose file or line isn't in the diff

- **Status:** Done
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
- **AC7:** Given 3 findings, 2 invalid, then only 1 is printed, in order, with `dropped=2`, and no warning. Given every finding invalid, then it exits 0 and stderr also has `review-bot: warning: all 3 model findings were dropped: their file or line isn't in the diff.` Given 1 finding, invalid, then the warning is `review-bot: warning: the 1 model finding was dropped: its file or line isn't in the diff.` (changed in build, main session, 2026-11-06).
- **AC8:** Given the threshold, then invalid findings are dropped first: one valid `low` and one invalid `critical` with `--threshold high` exits 0, `dropped=1`.
- **AC9:** Given any finding, then the log has a `verify` record for it with line, severity, outcome, reason and rationale SHA-256, with no rationale text; and a `policy` record for each kept finding. Given a `file` matching a diff path, the record has it. Given any other `file`, such as `SENTINEL-FILE.py` or 10,000 characters, the record has only its SHA-256 and length.
- **AC10 (no invalid anchor survives):** Given generated diffs and findings, including hostile headers, combined (`diff --cc`) sections with `@@@` hunks (changed before build, main session, 2026-11-06), and random line numbers, then every printed finding's file and line are an added or context line of `parse_diff`'s result.
- **AC11:** Given a diff with no rule hit and `--provider mock` with no scripted findings, then the canned finding is dropped, it exits 0, and the canned warning and `dropped=1` are on stderr.
- **AC12 (changed before build, main session, 2026-11-06):** Given a diff holding a conflicted file `diff --cc src/app.py`, with the hunk `@@@ -1,2 -1,2 +1,3 @@@` and body lines `++x`, ` +y`, `  z`, and a normal `app.py` section, then a finding on `src/app.py` at line 1, 2 or 3 is dropped with `line_not_in_diff`, not `unknown_file`, and none is moved to a nearby line. The parser gives a combined hunk no line numbers, as for an unreadable header ([P1-S2 AC3](p1-s2-parse-and-skip.md#acceptance-criteria), [line anchors](../design.md#line-anchors)). The same holds for `diff --combined`. A finding on an added line of `app.py` is still printed.

## Interfaces
`verify_findings`, `VerifyResult`, `decide`: [design.md](../design.md#interfaces). `DropReason` values are the reasons above. `rule_findings` is empty until P1-S7.

Changed in build, main session, 2026-11-06:
- **Audit events:** `verify.checked`, one per proposal: `fingerprint` (null until P1-S7), `file` or else `file_sha256` and `file_length`, `line`, `severity`, `outcome` (`kept` or `dropped`), `reason` (a `DropReason`, or `null` when kept), `rationale_sha256`. `policy.decided`, one per kept finding: `file`, `line`, `outcome` (`reported` or `below_threshold`).
- **Matched path:** a kept finding is reported and logged under the diff path it matched, so `./billing/invoice.py` is logged as `billing/invoice.py` (AC4).
- **Summary:** `dropped=N` comes right before `skipped_files=` ([compatibility](../design.md#compatibility)).

## Out of scope
- Moving a finding to a near line ([ADR 0006](../../../adr/0006-line-anchors-drop-never-snap.md)).
- Checking the line is the right one for the issue: live evals only.

## Notes
- **Behaviour change:** verify has no provider exemption, so `--provider mock`'s canned `mock/example.py:1` never prints.
- **Handovers:**
  - P0-S1 AC1 is replaced by AC11.
  - `Bot.request_for` (`tests/integration/conftest.py:66`) asserts exit 1 using the canned finding, and so does P0-S1 AC4's test at `test_p0_s1_cli.py:259`. Both must stop relying on it.
  - P0-S1 AC11 and AC19, P0-S2 AC10, P0-S3 AC2 and P0-S4 AC6 expect injected findings to print, so they need a diff containing those anchors. Move the default test finding (`src/app.py` line 7) and its diff into one shared helper.
  - P1-S5 AC1's test, in `tests/integration/test_p1_s5_provider_notices.py`, runs on `MINIMAL_DIFF`, so it needs a diff containing its finding's anchor (changed before build, main session, 2026-11-06).
  - AC19's hostile `file` cases need a diff whose header has that path, or a direct `render_findings` test.
- **Test dependency:** AC10 uses `hypothesis`, added in P1-S2.
- **Implementation choices (developer):** `ReviewedFinding` holds only `finding` and `source` until P1-S7 adds `rule` and `fingerprint`; `VerifyResult` has `kept` and `checks` (one per proposal, giving `dropped`), with `ai_anchor_valid` and `deduped` left to P1-S9 and P1-S14; the summary gains only `dropped=`, before `skipped_files=`; one dropped finding warns `the 1 model finding was dropped: its file or line isn't in the diff.`
- **README (main session):** line 14's `--provider mock` canned output stops printing under this slice.
