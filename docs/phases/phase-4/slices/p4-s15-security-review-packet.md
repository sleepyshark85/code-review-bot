# P4-S15: Hand over a security review packet for sign-off

- **Status:** Approved
- **Depends on:** P4-S1 to P4-S14 (their build ACs; S14's recorded ACs may follow)
- **Touches:** `docs/security/` (main session, from this spec), `tests/` (the tester checks the packet against the code and workflows)

## Value
A security reviewer can sign off the bot from one packet: what data goes where, which threats are handled and by which tested control, which terms govern each vendor, and what risk remains. Every claim about a control points to a test that proves it, and the tests keep the packet from going stale.

## Acceptance criteria
- **AC1:** Given `docs/security/review-packet.md`, then it has these sections: scope (the commit and version reviewed), data flow, assets and trust boundaries, threats and controls, credentials per workflow job, vendors and data retention, residual risks, and sign-off.
- **AC2:** Given the data flow, then it lists every destination data leaves the runner for (Anthropic through the Claude CLI, the second provider over HTTPS, the GitHub API), each with what is sent, what never is (secrets, masked personal data, `GITHUB_TOKEN`), and the policy key or ADR that governs it.
- **AC3:** Given the threats table, then it covers at least: prompt injection through a diff, a repository file and an issue; tool escape from the commit; secret exfiltration; personal data sent to a vendor; a tool or built-in enabled by accident; credential exposure to the model or to forks; runaway spend, live commands included; a tampered summary; a summary deleted to clear the gate (`tampered` when a comment-job run exists for the PR, the bot's inline comments exist or it reviewed the head); a summary edited, then a push, to drop a carried blocker (rebuilt from the bot's inline comments); a reply or review cancelling a push's gate run; an override reply edited by someone other than its author; a blocker laundered by an edit plus a push; a wrongful block; and an outage blocking merges. Each row names its control and at least one test as `path::test_name`.
- **AC4:** Given every `path::test_name` in the packet, then a test checks that it exists and is collected by pytest, so a renamed or removed test fails the gate.
- **AC5:** Given the credentials table (job → secrets and permissions), then a test parses `review.yml`, `live.yml` and `ci.yml` and checks that the table matches each job exactly.
- **AC6:** Given residual risks, then they include at least: admins can bypass branch protection; collaborators can edit comments; this repository's `self_override`, and that the main session posts as the owner, so only the rule that agents never post overrides (CLAUDE.md, the runbook, and any deny rule the user adds) stops an agent clearing its own blocker; fork PRs are never reviewed, so `merge gate` is skipped there and a skipped required check passes; an `ai:` blocker whose comment was withheld, or whose inline comment was deleted before the summary was edited, can't be rebuilt after a tampered summary (P4-S5 AC9), so it is lost, though the tamper is reported; once GitHub's run retention expires, a deleted summary on an old PR falls back to `open`; redaction's catalogue gaps; a model may still be wrong or be swayed, which people judge; and the SLO sample size.
- **AC7 (recorded):** The vendors section records, for Anthropic and the second provider, the terms and retention setting in force and the date the user confirmed them. The sign-off section records the signer named under [Q11](../design.md#6-open-questions), the date, the commit and any conditions. Agents leave both blank for the person to fill in.

## Limits
None: documentation and tests over committed files.

## Interfaces
`docs/security/review-packet.md`. Test references use the form `` `tests/…/test_x.py::test_name` ``.

## Out of scope
- An external penetration test; certification.

## Notes
