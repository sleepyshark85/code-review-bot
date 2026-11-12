# 0022: Redact personal data in place with typed placeholders in everything sent to a vendor, while secrets still stop the review

- **Status:** Accepted
- **Date:** 2026-11-12
- **Phase:** 4

## Context
The [north star](../roadmap.md#2-north-star) asks for secrets and personal data to be redacted before any vendor call, and the [risks](../roadmap.md#4-risks-and-non-goals) defer redaction to Phase 4. Since Phase 1 a diff holding a secret is never sent, and since Phase 3 the tools refuse secret-bearing files and lines. Personal data, such as an email address in a fixture or a phone number in a comment, is still sent as written. Line numbers and anchors must survive whatever is done to the text ([ADR 0006](0006-line-anchors-drop-never-snap.md)).

## Decision
The policy picks kinds from a fixed catalogue (email, E.164 phone number, IPv4 address). Each match in the diff, a tool result or issue text is replaced on its own line by a placeholder naming its kind, before the request reaches any provider. Reserved documentation addresses are left alone. Only counts by kind are recorded. Secrets keep their Phase 1 behaviour: the review stops, because a masked key is still a leaked key that must be rotated. [Details](../phases/phase-4/design.md#redaction).

## Consequences
- Anchors, fingerprints and evidence are unchanged, because they come from the original text.
- The model may reason less well about code that handles personal data, and a suggestion holding a placeholder is dropped.
- Forms outside the catalogue (names, postal addresses) still go out, so the security packet says so.

## Alternatives considered
- **Block the review, as for secrets:** personal data is common in fixtures, so most such PRs would get no review.
- **A named-entity model or service:** another vendor call, or a heavy dependency, that isn't deterministic.
- **Redact secrets too, instead of blocking:** the author still needs to remove and rotate the secret, and a placeholder hides that.
