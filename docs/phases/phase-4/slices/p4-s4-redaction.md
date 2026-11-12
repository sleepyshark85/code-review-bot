# P4-S4: Redact personal data before any vendor call

- **Status:** Done
- **Depends on:** P4-S1
- **Touches:** `governance.redaction` (new), `governance.policy`, `review`, `context.tools`, `context.issues`, `suggestion`, `report`, `evals.replay`

## Value
Under a policy that asks for it, email addresses, phone numbers and IPv4 addresses never leave the runner. Each is masked in place, so findings still anchor to the same lines ([ADR 0022](../../../adr/0022-redact-personal-data-at-the-vendor-boundary.md)).

## Acceptance criteria
Policy section `[redaction] kinds = [...]`, with kinds from `email`, `phone` and `ipv4`. An unknown kind exits 2, as in P4-S1 AC3. Rules: [design, Redaction](../design.md#redaction).
- **AC1:** Given `kinds = ["email"]` and an added line `contact = "jane.doe@rightship.com"`, when reviewed, then the request's user prompt holds `contact = "[REDACTED:email]"` and not the address. A finding the model anchors on that line is kept, with the same fingerprint as with no redaction.
- **AC2:** Given `user@example.com`, `ops@mail.example.org`, `a@b.test` and `x@y.invalid`, then none is masked.
- **AC3:** Given `kinds = ["phone"]`, then `+61 2 9999 0000`, `+12025550143` and `+1-202-555-0143` are masked as `[REDACTED:phone]`, and `12025550143` (no `+`), `+123` (too short), and a `+` followed by 16 or more digits are not.
- **AC4:** Given `kinds = ["ipv4"]`, then `10.0.0.12` and `8.8.8.8` are masked as `[REDACTED:ipv4]`, and `192.0.2.5`, `198.51.100.7`, `203.0.113.9`, `999.1.1.1`, `1.2.3.4.5` and `v1.2.3` are not.
- **AC5:** Given a context review with `kinds = ["email"]`, then a `read` of a file holding an address returns it masked, while the ledger's spans and byte counts describe the text as returned. A linked issue's title and body are masked before they are sent.
- **AC6:** Given any masked text, then `report/v1` has `redaction` with counts by source (`diff`, `tools`, `issues`) and kind, such as `{"diff": {"email": 1}}`. The audit log has one `static`/`redaction` record with the same counts, and no report, audit record, stderr line or Markdown holds a masked value.
- **AC7:** Given the built-in policy, then nothing is masked, `redaction` is `{}`, and every prompt is byte-identical to Phase 3's.
- **AC8:** Given a review whose model's suggestion holds `[REDACTED:`, then the suggestion is dropped and the finding is kept. The reason is `redacted`, unless diff mode has already dropped it as `not_requested`.
- **AC9:** Given `evals replay --policy p.toml`, then prompts are rebuilt with the same masking. A case with no match keeps its recordings fresh, and a case with a match reports them stale, naming `prompt_sha256`.
- **AC10:** Given a secret in the diff, then the review stops with exit 16 as before, whatever the policy says.

## Limits
Text is untrusted, up to `max_diff_bytes` for the diff and a tool's byte cap per result. Every pattern matches within one line, in linear time. A 400,000-character line of `@`, `+` or `.` is handled in time linear in its length. Text that already holds `[REDACTED:email]` is left as it is.

## Interfaces
`RedactionKind`: `email`, `phone`, `ipv4`. The placeholder is `[REDACTED:<kind>]`. `SuggestionDropReason.REDACTED` = `"redacted"`.

## Out of scope
- Other kinds (names, postal addresses, IPv6): [Q7](../design.md#6-open-questions).

## Notes
- A diff line's leading `+`, `-` or space is a marker, not text, so an added `12025550143` isn't read as `+12025550143`. Paths are masked like any text, so a path holding an address can't be read back by the model or anchored.
- Exempt domains, per the design: `example.com`, `.org`, `.net` and their subdomains, and the `.example`, `.test` and `.invalid` top-level domains. An email's top-level domain is letters only, so a pin such as `lib@1.2.3` isn't one.
- Issue text is masked after its cut (P3-S9), so a value the cut splits goes out only as its unmatched part. Tool lines are masked before theirs.
- A `[REDACTED:` suggestion is dropped whatever the policy, before any other check but diff mode's `not_requested`. `static`/`redaction` is written only under a policy with kinds, with `{"counts": ...}`; tool counts cover only the lines a result returns.
