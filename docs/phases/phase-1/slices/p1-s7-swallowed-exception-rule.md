# P1-S7: Catch swallowed exceptions with no model, with stable fingerprints

- **Status:** Done
- **Depends on:** P1-S6
- **Touches:** `rules` (new), `fingerprint` (new), `findings`, `verify`, `review`, `audit`

## Value
A developer gets a deterministic, reproducible finding for a swallowed exception, even when the model misses it, and each finding has an identity that survives moving lines.

## Acceptance criteria
With `--provider mock` and `MockProvider(findings=[])`, unless stated. Each hit renders as `{file}:{anchor} [medium/reliability] Exception swallowed: the handler discards it, so failures pass silently. Handle it, log it or re-raise it. (confidence 1.00)`.
- **AC1:** Given `Assignment/sample_diff.txt`, then stdout has that line for `billing/invoice.py:35`, it exits 1, and the summary has `rule_findings=1`.
- **AC2:** Parametrised over `except Exception:` then `pass`; `except:` then `...`; `except (A, B) as e:` then a comment, a blank line, then `pass`; one-line `except E: pass`, then one finding anchored at the `except` line.
- **AC3:** Given a handler whose first statement isn't `pass` or `...` (`pass` after `log(e)`, `raise`, `return None`), then no finding.
- **AC4:** Given the `except` as context and the `pass` added, then a finding at the `except` line. Given both as context, or both removed, then none.
- **AC5:** Given a hunk ending right after `except E:`, then none: the body isn't visible.
- **AC6:** Given the same lines in `x.js`, `x.pyi` or a skipped file, then none.
- **AC7:** Given a hunk with an unreadable header holding this pattern, then no finding is printed, `RuleResult.unanchored_hits` is 1, and the `static.rules` record has that count.
- **AC8:** Given a rule hit and a valid AI finding, then the rule line prints first, then the AI line, and both are subject to `--threshold`: `--threshold high` here keeps only an AI `high`.

**Fingerprints.** Definition: [design.md](../design.md#fingerprints-and-dedup).
- **AC9:** Given a fixed `file`, `rule`, `anchor_text` and `ordinal`, then `fingerprint_for` gives one value, pinned in a golden file, on every platform: 16 lowercase hex digits.
- **AC10:** Given the same diff with its hunk moved 50 lines down, or the anchored line reindented, then the fingerprint is unchanged. Given the anchored line's code changed, or the file renamed, it changes.
- **AC11:** Given two identical handlers in one file, then two findings with different fingerprints. Given two AI findings on one line, same category, different rationales, then both are kept with different fingerprints.
- **AC12:** Given two AI findings with the same file, category, anchored line and rationale, then one is kept, and the summary has `deduped=1`, although their ordinals would differ.
- **AC13:** Given a model output claiming `source`, `fingerprint` or `rule`, then it exits 13, as an extra field. The model can't set the first two at all, and P1-S9 opens only `rule`.

## Interfaces
`run_rules`, `RuleResult`, `fingerprint_for`, `ReviewedFinding`: [design.md](../design.md#interfaces). [ADR 0003](../../../adr/0003-finding-fingerprints.md).

## Out of scope
- `open()` (P1-S8), rule vs AI dedup and the model's `rule` tag (P1-S9).
- Text inside a multi-line string that looks like a handler: fragments can't be reliably parsed, so this false positive is accepted.
- An `except` clause split over lines, such as black's `except (` then `A,`, `B,`, `):` then `pass`: no finding and no unanchored hit, a known miss. The rule reads single-line clauses in diff fragments, which may hold only part of a clause. A later slice may widen it (changed in build, main session, 2026-11-06).
- A slice or `lambda` inside the `except` clause, such as `except errs[1:]:` or `except (lambda: E)():`: the clause match stops at the first `:`, so this is a known miss too (changed in build, main session, 2026-11-06).

## Notes
- **`rule` here:** a rule's identity lives on `ReviewedFinding.rule`, set by code. `Finding` doesn't gain a `rule` field until P1-S9, so the model's schema doesn't change in this slice.
- **Handover:** on the sample diff, P0-S1 AC2's `--threshold critical` still exits 0, so its test stands. P1-S9 replaces the `rule` case of AC13.
- **README (main session):** say that this slice and P1-S8 can turn a clean exit into exit 1.
- **Implementation choices (developer):** `AnchorCheck` becomes `ProposalCheck`, adding `duplicate_of` and `fingerprint`, with `VerifyResult.deduped`; `RuleId` holds only `swallowed-exception` until P1-S8; a rule hit on line 0 (new start `+0`) counts as unanchored; AC13 needs no code, as `Finding` already forbids extra fields.
