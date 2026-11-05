# P0-S3: Get a distinct exit code and a safe error message for every provider failure

- **Status:** Approved
- **Depends on:** P0-S2
- **Touches:** `errors`, `cli`, `findings`, `render`, `providers/mock`

## Value
A developer or CI job gets one safe stderr line for every failure. With `--provider claude`, invalid output now exits 13 and other SDK failures still exit 14; the remaining exit codes work for any provider that raises them, shown here by injection, and P0-S4 maps real SDK errors to them.

## Acceptance criteria
**Exit codes**
- **AC1:** Given an injected `MockProvider(error=ProviderError(category, "detail"))`, for each of the 7 categories, then `main` returns the [design.md](../design.md#exit-codes) code (7 to 13), stdout is empty, and stderr contains the category value and `detail`.
- **AC2:** Given `MockProvider(raw_output=...)` with each of: `not json`; `{}`; a finding missing `line`; `line` 0; severity `urgent`; confidence 1.5; an extra field; one valid plus one invalid finding; bare JSON inside a code fence, then it exits 13 and stdout is empty. Given `{"findings": []}`, it exits 0.
- **AC3:** Given `ClaudeAgentProvider` over a fake `sdk_call` and a `ResultMessage` with `is_error=False` and `result` of `None`, empty, not JSON, or in a code fence, then `main` returns 13. *(Replaces P0-S2 AC14.)*
- **AC4:** Given an unexpected exception anywhere in `main`, including an injected provider raising `RuntimeError("SENTINEL")`, or stdout closed (`--threshold critical >&-`) or failing on write or flush (such as `/dev/full`, or a pipe whose reader has closed), then the process exits 14, not 1 or 120, and stderr, if available, has no traceback and no `SENTINEL`. An unavailable stderr (closed or failing) never changes the exit code: a delivered review exits by its findings, and any other failure keeps its own code. With stderr unavailable, `main` doesn't raise. *(Extends P0-S2 AC17.)*

**Safe stderr**
- **AC5:** Given any failure with exit code 3 to 14, then stderr has exactly one line, starting `review-bot: error:`, with no traceback. Given exit code 2, usage text may follow that line. `main`'s own success lines (the mock warning and summary) are written only after stdout has been written and flushed, so a stdout failure leaves only the error line. P0-S4 AC6's overridden-signal warning may precede the error line, because a provider writes it during the review.
- **AC6:** Given a `ProviderError` message containing a newline and `\x1b[31m`, a missing diff path containing `\x1b`, or an unknown flag or invalid flag value containing `\x1b` and a newline, then `main` writes one `review-bot: error:` line, whichever path produced the message, and stderr has no raw control characters. The path case exits 3 or 4 depending on the platform (Windows reports "Invalid argument"), so tests don't assert which.
- **AC7:** Given `raw_output` containing `MODEL-SENTINEL` and `\x1b`, both as invalid JSON and as a field value failing the schema, then it exits 13 and neither reaches stderr: `invalid_output` messages never embed model text or pydantic's `input_value`.

**Conformance**
- **AC8:** Given `tests/conformance/`, using `provider_factory(scenario)` ([design.md](../design.md#interfaces)), when run against `MockProvider`, then: `valid` returns `Finding` objects and non-negative usage; `empty` returns none; `invalid` raises `invalid_output`; each `ErrorCategory` raises `ProviderError` with that category; and responses contain only `review_bot` or built-in types.
- **AC9:** Given `PATH` without `claude` and no `ANTHROPIC_API_KEY`, then all of the above pass.

## Interfaces
See [design.md](../design.md#interfaces) for `ProviderError`, `ErrorCategory`, `parse_findings`, `Scenario` and the `MockProvider` parameters.

## Out of scope
- Mapping real SDK errors to categories, and conformance for `ClaudeAgentProvider` (P0-S4).
- Repairing or partly accepting invalid model output: it is rejected whole.

## Notes
CPython retries the flush at shutdown and overrides the exit code with 120 even after `sys.exit(14)` (verified on Python 3.12 on Windows), so the failed stream must also be dropped (set to `None` or `dup2` to devnull), and AC4 needs a subprocess test.
