# 0010: Publish each shadow run as a versioned JSON artifact plus a job summary

- **Status:** Accepted
- **Date:** 2026-11-08
- **Phase:** 2

## Context
[README Phase 2](../../README.md#phase-2-integrated) starts in shadow mode, a report of what the bot would have posted, and its gate needs weeks of these reports judged by a person to measure precision. The report needs to be readable at a glance on the PR and also machine-readable for labelling and metrics, and the format is consumed by later phases, so it is costly to change.

## Decision
Every run, in either mode, writes one versioned JSON document (`shadow/v1`) that embeds the `report/v1` review and lists each comment the bot would create, update, move, resolve or withhold, with its exact body. It also writes a Markdown rendering. The workflow puts the Markdown in the job summary and uploads both, with the audit log, as an artifact kept for 90 days. Judgements and metrics live outside the artifact, in files the user keeps. [Format](../phases/phase-2/design.md#interfaces).

## Consequences
- Both modes come from one plan and one renderer, so each body is exactly what would be posted. Shadow mode keeps no state, though, so across pushes it lists repeats and rewordings that comment mode would keep as one comment; labelling groups them ([precision](../phases/phase-2/design.md#how-the-gate-is-evidenced)).
- The report holds model text, so whoever can read the repository's Actions artifacts can read it, the same people who can read the PR.
- Artifacts expire after 90 days, so the user downloads reports and labels them within that window.
- The PR diff isn't stored: the report holds the base and head commits, and git rebuilds the diff for Phase 3's labelled set.
- The audit log becomes a shared artifact, which [Phase 1's Q5](../phases/phase-1/design.md#4-open-questions) flagged: its diagnostics are scrubbed but best-effort.

## Alternatives considered
- **Job summary only:** readable, but not machine-readable, and lost with the run's logs.
- **Commit reports to the repository:** permanent, but needs write access to contents and adds a commit per push.
- **A comment in shadow mode:** visible, but it is exactly the noise shadow mode exists to avoid.
- **An external store:** durable, but a service, credentials and an owner before anything has been proven.
