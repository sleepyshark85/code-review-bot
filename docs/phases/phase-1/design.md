# Phase 1: Trustworthy core — design

Approved by the user on 2026-11-05 (PR #14), with the defaults for Q5 and Q9. The user approved P1-S17 on 2026-11-05, with the defaults for Q1 to Q4. Criterion IDs are stable.

## 1. Goal and gate

Make findings trustworthy, triageable and measurable, with every stage decision on record. Scope and gate: [README Phase 1](../../../README.md#phase-1-trustworthy-core). [What the evals prove](#what-the-evals-prove) says how each gate clause is evidenced, because passing CI alone can't close it.

## 2. Architecture

### Module map

New or changed modules under `src/review_bot/`; the rest of [Phase 0's map](../phase-0/design.md#module-map) stands. `diff_parser`, `secret_scan`, `rules`, `verify`, `fingerprint`, `policy`, `report` and `render` are pure; `render` imports neither `review` nor `audit` (changed in build, main session, 2026-11-06). `audit`, `triage`, `cli`, `evals` and `providers` touch the outside world. `review` orchestrates through injected collaborators.

| Module | Responsibility | Key types |
|---|---|---|
| `cli` | New flags and exit codes, triage in the shell, deferred SDK import | `main`, `build_provider` |
| `review` | Runs the stages in order and records each one | `run_pipeline`, `PipelineResult` |
| `diff_parser` | Tolerant parse, new-version line numbers, skip classification | `parse_diff`, `ParsedDiff` |
| `secret_scan` | Detect and redact secrets in raw text | `scan_secrets`, `redact`, `SecretHit` |
| `rules` | Checks with no LLM, on added lines of Python files | `run_rules`, `RuleResult` |
| `verify` | Anchor check; dedup | `verify_findings`, `VerifyResult` |
| `fingerprint` | Stable finding identity ([ADR 0003](../../adr/0003-finding-fingerprints.md)) | `fingerprint_for` |
| `policy` | Applies the threshold; `cli` exits 1 if anything stays reported | `decide`, `Decision` |
| `render` | Text (unchanged), JSON, Markdown | `render_findings`, `render_json`, `render_markdown` |
| `audit` | Append JSONL; scrub diagnostics ([ADR 0005](../../adr/0005-audit-log.md)) | `AuditLog`, `scrub_diagnostics` |
| `report` | A review's result, and git C-quoting of [paths](#audit-log), with no redaction; used by `render`, `review`, `audit` and `cli` (changed in build, main session, 2026-11-06) | `PipelineResult`; from P1-S12, `TriageDecision`, `TriagedFinding`, `not_rejected` (changed in build, main session, 2026-11-07) |
| `triage` | Interactive accept / reject / edit; `cli` injects its streams from `console` (changed in build, main session, 2026-11-07) | `run_triage`, `TriageResult`, `Label` |
| `prompt` | `rule` and anchor guidance; `PROMPT_VERSION` becomes `p1-v1` | `build_request` |
| `findings` | The model's `rule` field, and the pipeline's wrapper type | `Finding`, `RuleId`, `ReviewedFinding` |
| `providers/*` | Warnings and diagnostics as values ([ADR 0004](../../adr/0004-provider-notices-as-values.md)) | `ReviewResponse`, `ProviderError` |
| `evals` | Case format, scorer, replay and live ([ADR 0007](../../adr/0007-evals-replay-and-live.md)) | [P1-S14](slices/p1-s14-eval-sample.md#interfaces) |

### Data flow

load → **secret scan on raw text** → parse → **secret scan on decoded header paths** (before any parse record) → rules → agent → verify → policy → triage (with `--triage`) → output. A secret hit exits 16 and nothing later runs. The agent isn't called when every file is skipped. The raw scan precedes the parser, so a parser bug or an unattributed line can't carry a secret past it; the decoded scan follows it, so a path the parser decodes from escapes can't either ([P1-S19](slices/p1-s19-decoded-path-secret.md)). Otherwise the README's order stands.

### Interfaces

Parser types are in [P1-S2](slices/p1-s2-parse-and-skip.md#interfaces), triage in [P1-S12](slices/p1-s12-triage.md#interfaces), the report in [P1-S10](slices/p1-s10-json-output.md#interfaces). `main(argv, provider=...)` keeps its signature, and tests still inject through it.

```python
def parse_diff(text: str) -> ParsedDiff: ...  # raises DiffNotParsableError (15)
def scan_secrets(text: str) -> tuple[SecretHit, ...]: ...  # SecretHit: kind, diff_line; never the value
def redact(text: str) -> str: ...  # replaces each value, or whole PEM block, with [REDACTED]

class RuleId(StrEnum):  # in `findings`
    SWALLOWED_EXCEPTION = "swallowed-exception"
    OPEN_WITHOUT_ENCODING = "open-without-encoding"

class Finding(BaseModel):  # Phase 0 fields, plus, from P1-S9:
    rule: RuleId | None = None  # the model's tag; an unknown value rejects the output (13)

@dataclass(frozen=True, slots=True)
class ReviewedFinding:  # set by code, so the model can't forge them
    finding: Finding
    source: Source  # RULE or AI
    rule: RuleId | None  # from P1-S7, the rule's identity; from P1-S9, also the model's tag
    fingerprint: str  # from P1-S7

def run_rules(parsed: ParsedDiff) -> RuleResult: ...  # findings; unanchored_hits: int
def fingerprint_for(*, file: str, rule: str, anchor_text: str, ordinal: int) -> str: ...
def verify_findings(
    proposed: Sequence[Finding], rule_findings: Sequence[ReviewedFinding], parsed: ParsedDiff
) -> VerifyResult: ...  # kept (rules by file and line, then AI as proposed); ai_anchor_valid (before
# dedup, for eval scoring); dropped (with DropReason); deduped (with the fingerprint it duplicated)
def decide(kept: Sequence[ReviewedFinding], threshold: Severity) -> Decision: ...  # reported, below_threshold

class ReviewResponse:  # adds warnings: tuple[str, ...] = ()
class MockProvider:  # adds warnings: Sequence[str] = ()
class ProviderError(ReviewBotError):  # diagnostics are raw; only `audit` reads them, after scrubbing
    def __init__(self, category: ErrorCategory, message: str, *, diagnostics: Sequence[str] = ()) -> None: ...

class AuditLog:
    def __init__(self, path: Path, *, clock: Callable[[], datetime]) -> None: ...  # raises AuditError (17)
    def record(self, stage: Stage, event: str, data: Mapping[str, JsonValue]) -> None: ...

def scrub_diagnostics(lines: Sequence[str], *, diff_text: str, environ: Mapping[str, str]) -> tuple[str, ...]: ...

async def run_pipeline(  # PipelineResult: Decision, VerifyResult, skipped files, usage, warnings
    diff_path: Path, settings: Settings, provider: LLMProvider, audit: AuditLog
) -> PipelineResult: ...
```

### Exit codes

The [Phase 0 table](../phase-0/design.md#exit-codes) stays as it is. Appended: **15** no diff structure (S2), **16** secret detected and not sent (S3), **17** audit log unwritable (S4). `--triage` without a terminal uses the existing 2. The eval command has its own codes, 0 pass, 1 fail, 2 usage, outside this contract.

### Settings

Same precedence as [Phase 0's table](../phase-0/design.md#settings). `--format` / `REVIEW_BOT_FORMAT`: `text` (default), `json`, `markdown`. `--audit-log` / `REVIEW_BOT_AUDIT_LOG`: default `.review-bot/audit.jsonl`, under the working directory. `--triage` is a flag only, so an environment can't turn on prompting.

### Line anchors

- **The hunk body wins over its header:** numbering starts at the header's new start and advances on added and context lines. Declared counts are only compared, never used. See [P1-S2](slices/p1-s2-parse-and-skip.md) for the sample diff.
- **Valid anchor:** the `file` is a reviewable new path (exact, then once more without one leading `./`, `a/` or `b/`), and the `line` is an added or context line in it. Context counts because the prompt sends removed-line findings to the nearest line, and PR comments can anchor there.
- **Unreadable new start:** the hunk still reaches the prompt, but nothing can anchor to it.
- **Drop, never snap:** a near miss is dropped and logged ([ADR 0006](../../adr/0006-line-anchors-drop-never-snap.md)).
- **What this proves:** the line exists in the diff, not that it's the right line for the issue.

### Fingerprints and dedup

Decision: [ADR 0003](../../adr/0003-finding-fingerprints.md).

- **Input:** version `v1`, file, rule part, SHA-256 of the anchored line with whitespace collapsed and ends stripped, and an ordinal. The rule part is the rule ID, or `ai:<category>` for an untagged AI finding. Output: the first 16 hex digits of the SHA-256 of those parts.
- **Encoding (changed in build, main session, 2026-11-06):** Phase 2 keys comments on fingerprints, so this is fixed for `v1`:
  - **File:** the diff path the finding matched, as `verify` reports it.
  - **Rule part:** the rule ID (`swallowed-exception`) for a rule finding, and for an AI finding the model tagged; `ai:` then the category value (`ai:reliability`) for an untagged one.
  - **Anchored line:** its text, without the diff marker. Every run of Unicode whitespace (Python's `str.split()`) becomes one space, both ends are stripped, and the result is encoded as UTF-8, with a lone surrogate written as its own 3 bytes (`surrogatepass`). Its SHA-256 is written as 64 lowercase hex digits.
  - **Joined:** a JSON array in that order, `["v1", file, rule part, anchor hash, ordinal]`, with the ordinal a JSON integer. Separators are `", "` between items, with no space inside the brackets. Every non-ASCII character is a `\uXXXX` escape, with lowercase hex, a surrogate pair above U+FFFF and a lone surrogate as its own escape, as Python's `json.dumps` with its defaults writes it. The text is encoded as ASCII, and the fingerprint is the first 16 lowercase hex digits of its SHA-256.
- **Ordinal:** 0-based among findings sharing the other parts, in `kept` order, so two AI findings on one line stay separate.
- **Rule vs AI:** an AI finding is a duplicate if a rule finding has the same file and `rule`, in the same hunk, within 3 new-version lines, **and the AI severity is at or below the rule's**. Otherwise both print, so a model tag can't suppress a more serious finding.
- **AI vs AI:** only when file, rule part, anchored line and rationale are all identical. The ordinal isn't compared.

### Secrets

Seven kinds, scanned on raw text and detected by vendor format plus one assignment pattern, with no entropy check. Unlisted formats do reach the model. Forms, boundaries and near misses are in [P1-S3](slices/p1-s3-secret-block.md).
If the raw text has no hit, the decoded header paths are scanned too: every path and old path the parser yields, quoted or not, each scanned on its own, and every other quoted string in a file's header lines, so a secret spelt in octal escapes is caught ([P1-S19](slices/p1-s19-decoded-path-secret.md)). `diff_parser` decodes them; `review` runs the scan.
A hit exits 16 with no provider call and one stderr line of kinds and diff lines. It isn't a `Finding`, so no path or value is reported, and every path written to the log or stderr passes `redact`. Stdout isn't redacted: the text and JSON reports show the real diff path (changed in build, main session, 2026-11-06).

### Audit log

One JSON object per line: `schema` (`audit/v1`), `run_id`, `ts` (UTC), `stage`, `event`, `data`. Rules: [ADR 0005](../../adr/0005-audit-log.md), [P1-S4](slices/p1-s4-audit-log.md), [P1-S5](slices/p1-s5-provider-notices.md).

| Stage | Events and data |
|---|---|
| `run` | `start`: version, provider, model, prompt version, threshold, format, diff SHA-256, size. `end`: exit code, plus an error category or exception class name, never exception text |
| `static` | `secrets`: kinds and diff lines. `rules`: fingerprint, rule, file, line, severity; unanchored hit count |
| `parse` | `parsed`: `files` and `hunks` counts; `skipped_files` (each `file`, `reason`) and `flagged_hunks` (each `file`, `index`, `new_start`), for mismatched or unreadable headers, at most 50 each, with `skipped_files_total` and `flagged_hunks_total` |
| `agent` | `called`: tokens, duration. `skipped`: reason. `warning`: scrubbed text. `failed`: category, scrubbed diagnostics |
| `verify` | `checked`, for each proposal: `fingerprint` or null, `file` (the diff path it matched) if it matches one, otherwise `file_sha256` and `file_length`; `line`, `severity`, `outcome` (`kept`, `dropped`, or from P1-S7 `duplicate`), `reason` (null when kept or `duplicate`), `rationale_sha256`; from P1-S7, `duplicate_of` (the fingerprint it duplicated, or null) (changed in build, main session, 2026-11-06) |
| `policy` | `decided`, for each kept finding: `file`, `line`, `outcome` (`reported` or `below_threshold`) |
| `triage` | `labelled`, one per decision: the [Label fields](slices/p1-s12-triage.md#interfaces), plus `model` and `prompt_version` (changed in build, main session, 2026-11-07) |

**Paths in `audit/v1`** (decided by the user, 2026-11-06). A diff path in a record's `file` is written so every line is pure ASCII JSON, with no lone surrogates, readable in any language:
- **Valid UTF-8:** the plain path, JSON-escaped to ASCII, with no flag.
- **Not valid UTF-8:** git's C-quoted form, as git shows it with its default `core.quotePath`: wrapped in double quotes; `"` and `\` escaped with a backslash; `\a`, `\b`, `\t`, `\n`, `\v`, `\f`, `\r` for those control characters; every other byte below 0x20, 0x7F, or above 0x7F as a three-digit octal escape. The object holding that `file` gets `path_quoted: true`. Byte 0xFF in `docs/caf…md` gives the 17-character value `"docs/caf\377.md"`, quotes included.
- **Deleted file:** its old path, from `--- a/`, never `null`.

### Compatibility

- **Exit codes** are append only.
- **Text stays the default and byte-for-byte the same.** JSON and Markdown come only through `--format`. Rule findings use the same line. Stderr's summary keeps Phase 0's fields in order and appends: `rule_findings=`, `dropped=`, `deduped=`, `skipped_files=`, `model_called=`, `run_id=`.
- **Stdout on failure** stays empty in every format, except when exit 17 follows a delivered report.

Each slice names the Phase 0 criteria it replaces, under the [Phase 0 handover rule](../phase-0/design.md#notes-for-implementers).

### What the evals prove

Decision: [ADR 0007](../../adr/0007-evals-replay-and-live.md). Format and exit rule: [P1-S14](slices/p1-s14-eval-sample.md).

| Evidence | Runs | Provider | Proves | Does not prove |
|---|---|---|---|---|
| **Pipeline tests** | CI, every PR | `MockProvider`, scripted | Parser, rules, verify, policy, audit and output behave | Anything about a model |
| **Replay** | CI, every PR | `MockProvider` fed a recording | Rule recall on the sample, for real, as rules are deterministic. Rule false positives only on one clean fixture written to avoid the rules. A past response still scores the same through today's pipeline | That the model still behaves so, or that a new model or prompt would |
| **Live run** | Manual, never CI | `ClaudeAgentProvider` | AI recall, clean-diff noise and injection resistance, for that model and prompt, on these few cases | Other diffs or later models. Five runs is a small sample, the prompt was tuned on these same cases, and matching is approximate, so a person confirms credits |

| Gate clause | CI (offline) | Live (manual, proposed thresholds) |
|---|---|---|
| Planted bugs caught | Rule bugs, every run; AI bugs by replay | Each AI bug in at least 3 of 5 runs |
| Clean diff within the noise budget | Zero rule findings, plus replay | 5 of 5 runs within the budget |
| Injection case passes | Replay; controls show a compromised response fails | 5 of 5 runs for each of 3 variants |
| No invalid anchors survive | Verify tests, hostile and malformed input | Same code on real output |
| Everything runs offline in CI | Whole suite, under Phase 0's guard | Not applicable |

**The gate closes only with both columns.** *Phase 1 closed on 2026-11-07 with one exception the user accepted: `double-tax` was caught in 0 of 5 live runs and stays a documented miss ([test report](test-report.md#p1-s17-measure-the-real-model-and-hold-ci-to-the-result)).* CI can be green while AI recall and injection resistance stay unmeasured. "Everything runs offline in CI" is read as: nothing in CI needs a model.

### Known limits carried from Phase 0

| Limit | Phase 1's handling |
|---|---|
| Refusal never seen live; only the 401 confirmed live; bare JSON rests on a handful of runs; finding counts and lines vary | The live run reports errors by category, the `invalid_output` rate, spread, drift and fingerprint recurrence |
| Invalid token counts read as 0 with no warning | Usage can read low, so `live` bounds calls, not tokens |
| A later valid result overrides a failure signal, even at exit 0 | The warning reaches the audit log (S5) and each recorded run (S16) |
| A stream hung after a failed result waits for the deadline; `--deadline-s` isn't a hard bound | Unchanged; `live`'s time bound is approximate |
| `CLIConnectionError` always exits 11 | Unchanged; recorded as `transient` |
| Ctrl+C during a review exits 14 | Unchanged; during triage it exits 1, because the review already exists (S12) |
| SDK import costs 1 to 2 s per run; the README leaves start-up to Phase 2 | S1 defers the import; measuring CLI start-up stays in Phase 2 |
| Bundled CLI, cache token sum and `API_TIMEOUT_MS` inferred or unknown; termination mid-call and Windows grandchild processes untested; `/dev/full` cases run only in CI | Unchanged, not in scope |
| The SDK's shutdown can put an asyncio traceback on stderr, breaking the one-line rule | [P1-S18](slices/p1-s18-clean-sdk-shutdown.md) addresses it |

## 3. Slices

**All done:** every slice below is `Done`. P1-S17's PR sets the last one and carries the closing task.

- [P1-S1: Run mock reviews without loading the Claude SDK](slices/p1-s1-mock-without-sdk.md)
- [P1-S18: Get clean stderr when the Claude SDK shuts down](slices/p1-s18-clean-sdk-shutdown.md)
- [P1-S2: Skip docs, lock and generated files, and reject text that isn't a diff](slices/p1-s2-parse-and-skip.md)
- [P1-S3: Stop a diff with a detected secret from reaching the model](slices/p1-s3-secret-block.md)
- [P1-S4: Trace every run in a JSONL audit log](slices/p1-s4-audit-log.md)
- [P1-S19: Stop a secret spelt in a quoted path's escapes from reaching stderr or the model](slices/p1-s19-decoded-path-secret.md) (fixes issue #24)
- [P1-S5: See provider warnings and failure diagnostics in the audit log](slices/p1-s5-provider-notices.md)
- [P1-S6: Drop model findings whose file or line isn't in the diff](slices/p1-s6-verify-anchors.md)
- [P1-S7: Catch swallowed exceptions with no model, with stable fingerprints](slices/p1-s7-swallowed-exception-rule.md)
- [P1-S8: Catch `open()` without an encoding with no model](slices/p1-s8-open-encoding-rule.md)
- [P1-S9: See one finding when a rule and the model report the same problem](slices/p1-s9-dedupe.md)
- [P1-S10: Get the review as JSON](slices/p1-s10-json-output.md)
- [P1-S11: Get the review as Markdown](slices/p1-s11-markdown-output.md)
- [P1-S12: Accept or reject findings interactively, with labels logged](slices/p1-s12-triage.md)
- [P1-S13: Edit a finding during triage](slices/p1-s13-triage-edit.md)
- [P1-S14: Score the sample diff per planted bug and per source, offline](slices/p1-s14-eval-sample.md)
- [P1-S15: Score the clean and injection diffs, offline](slices/p1-s15-eval-clean-injection.md)
- [P1-S16: Record a live eval run with one explicit command](slices/p1-s16-eval-live-command.md)
- [P1-S17: Measure the real model and hold CI to the result](slices/p1-s17-measure-real-model.md)

**Closing designer task, after the slices merge:** mark replaced Phase 0 criteria `Removed`, and correct Phase 0's "the bot never reads `ANTHROPIC_API_KEY`", which S5 makes untrue. README changes go through the main session, as each spec's Notes list. **Done (designer, 2026-11-07):** [P0-S1 AC1](../phase-0/slices/p0-s1-offline-review.md#acceptance-criteria) is `Removed`, P0-S1 AC3 and [P0-S3 AC5](../phase-0/slices/p0-s3-provider-failures.md#acceptance-criteria) note their Phase 1 changes, and [Phase 0's settings](../phase-0/design.md#settings) say when the credential variables are read.

## 4. Open questions

**Decided by the user at approval.**

**Q5.** **CLI stderr in the audit log. Decided: the default.** **Default:** a scrubbed tail, on failed calls only. **Limit:** a fragment shorter than a diff line, a reworded diff line, or an unlisted secret format can still pass. **Alternative:** keep discarding it, which leaves failed calls undiagnosable. This matters most if logs become shared CI artifacts.

**Q9.** **Rejected and edited text. Decided: the default.** The log holds no rationale text, only whether it changed ([Label](slices/p1-s12-triage.md#interfaces)). **Default:** JSON keeps rejected findings marked `reject`, and edits keep `original_rationale`. Text and Markdown runs store no rejected text, so Phase 2's feedback data requires saved JSON, joined by `run_id`. **Alternative:** keep the text in the log, which can quote the diff or an undetected secret.

**Decided by the user on 2026-11-05, approving P1-S17 with these defaults.** The account wasn't stated: the main session assumed the user's current Claude Code login and told the user, who didn't object. The user can change it before the run.

**Q1.** **Live eval budget, and whose account? Decided: the default.** Approved: 5 runs of 5 cases, so 25 calls and about 85,000 tokens, extrapolated from Phase 0's two sample runs. There's no price in the repository. Default retries would allow up to 75 API attempts, so `live` sets retries to 0, because token counts can read 0 and can't enforce a total. Deadlines still allow about 2 hours at worst. **Approved 2026-11-05: 25 calls, retries 0; anything more needs the user.**

**Q2.** **Does the gate accept a manual live run as evidence? Decided: the default.** **Default: yes.** The README's gate gets a sentence saying so.

**Q3.** **Live thresholds. Decided: the default.** **Default:** each AI bug in at least 3 of 5 runs; each injection variant 5 of 5. If each AI bug has a true 70% catch rate, it passes 3 of 5 about 84% of the time, but all six pass only 34% of the time (independent runs assumed); at 80% that's about 70%, at 90% about 95%. **Kept.** The rule is reachable reliably only at about 90% per bug, so a miss isn't proof of a bad model. It goes to you with the numbers, and is never re-rolled, meaning agents don't re-run it; P1-S17 AC3 lets you choose a re-run. Injection needs all 15 runs: a true per-run pass rate of 99% clears about 86% of the time, 95% about 46%, and 90% about 21%, and with retries at 0, one transient failure fails a variant.

**Q4.** **Noise budget. Decided: the default.** **Default:** at most 1 finding per run, none `high` or above, 0 from rules, counted at `low`, on a clean diff of 40 to 60 changed lines. Recalibrate against the Phase 2 baseline.

**Defaults accepted by review; overrule if you disagree.**

**Q6.** **Audit log on by default, failing closed.** It writes a file on every run, and an unwritable log stops the review.

**Q7.** **No bypass for a secret false positive.** "Not sent" stays absolute, so AWS's documented example key blocks a diff, and so does a digit-bearing type-like string in a multi-line parameter list, with no bypass under this default. The reverse risk also stands: under an unquoted name, a letters-only YAML value such as `password: "correcthorsebatterystaple"`, where "letters-only" also allows `.`, `[`, `]` and `|`, isn't detected, so a real password can reach the model.

**Q8.** **"Docs-only" read per file.** Docs files are skipped in a mixed diff too, so the model doesn't see them. A file's path, which the author controls, decides this, so the audit log and JSON report list skipped files; a mixed text run shows only a count.
