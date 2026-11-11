# P4-S16: Close Phase 4: record the gate, release 0.5.0

- **Status:** Approved
- **Depends on:** P4-S1 to P4-S15 (S10's AC12 included); P3-S15 (Phase 3 closed, 0.4.0 released); the signed packet (S15 AC7); 13 weeks of S13's SLO window
- **Touches:** `docs/phases/phase-4/test-report.md`, `pyproject.toml`; the README, release notes and runbook by the main session

## Value
The phase closes on evidence: a signed security review, SLOs met over a quarter, merge gating on for approved classes, and a second provider that passed the same suite and eval gate, released as 0.5.0.

## Acceptance criteria
Mostly recorded evidence, as in [P3-S15](../../phase-3/slices/p3-s15-close-phase-3.md). Definitions: [How the gate is evidenced](../design.md#how-the-gate-is-evidenced). A miss goes to the user with the numbers. It is never re-measured until it passes.
- **AC1 (recorded):** The test report quotes the sign-off from `docs/security/review-packet.md`: the signer, date, commit and any conditions, and how each condition was met.
- **AC2 (recorded):** It includes the full `pr slo` output for the 13-week window, the weekly table and both verdicts, with the sample size and the disclosure that 99.5% over this many events is a weak claim.
- **AC3 (recorded):** It names each class approved in `.github/review-bot-policy.toml`, with its approver, date and `pr metrics --by-class` evidence at approval. It gives the date `REVIEW_BOT_GATE=on` was set, confirms `merge gate` is a required check on `main`, and counts blocks, overrides, `tampered` results and `open` passes over the window. If `possible-secret` is the only gating class, it says so plainly, since that class needed no labels ([Q4](../design.md#6-open-questions)).
- **AC4 (recorded):** It includes `evals gate` output passing under the committed policy, with the second provider's suite, and the conformance suite's result for the mock, Claude and the second provider.
- **AC5 (recorded):** It gives the Phase 4 spend table: every dispatch, re-runs and failures included, with its URL, step, cap and reported spend, summed against each cap and the approved $8.00. Any use of the reserve names its approver.
- **AC6:** Given the committed recordings, then `evals gate` and the Phase 0 to 3 suites pass, and diff-mode `replay` still exits 1 only for the accepted `double-tax`.
- **AC7:** `pyproject.toml` is at `0.5.0`, and `docs/phases/phase-4/release-notes.md` lists the changes. The tag and release follow [CLAUDE.md](../../../../CLAUDE.md).

## Limits
None beyond S13 and S14.

## Interfaces
None new.

## Out of scope
- Default-on across other repositories (Roadmap targets, Adoption).

## Notes
- **Closing designer task:** mark every spec `Done`. The main session updates the README's Phase 4 status, its risk rows on vendor lock-in and redaction, and the runbook.
