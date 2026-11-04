# P0-S3: Get a distinct exit code and a safe error message for every provider failure

- **Status:** Approved
- **Depends on:** P0-S2
- **Touches:** `errors`, `cli`, `findings`, `providers/mock`

## Value
A developer or CI job gets one safe stderr line for every failure. With `--provider claude`, invalid output now exits 13 and other SDK failures still exit 14; the remaining exit codes work for any provider that raises them, shown here by injection, and P0-S4 maps real SDK errors to them.

## Acceptance criteria
**Exit codes**
- **AC1:** Given an injected `MockProvider(error=ProviderError(category, "detail"))`, for each of the 7 categories, then `main` returns the [design.md](../design.md#exit-codes) code (7 to 13), stdout is empty, and stderr contains the category value and `detail`.
- **AC2:** Given `MockProvider(raw_output=...)` with each of: `not json`; `{}`; a finding missing `line`; `line` 0; severity `urgent`; confidence 1.5; an extra field; one valid plus one invalid finding; bare JSON inside a code fence, then it exits 13 and stdout is empty. Given `{"findings": []}`, it exits 0.
- **AC3:** Given `ClaudeAgentProvider` over a fake `sdk_call` and a `ResultMessage` with `is_error=False` and `result` of `None`, empty, not JSON, or in a code fence, then `main` returns 13. *(Replaces P0-S2 AC14.)*
- **AC4:** Given an injected provider raising `RuntimeError("SENTINEL")`, then `main` returns 14, not 1, and stderr has no traceback and no `SENTINEL`.

**Safe stderr**
- **AC5:** Given any failure with exit code 3 to 14, then stderr has exactly one line, starting `review-bot: error:`, with no traceback. Given exit code 2, usage text may follow that line.
- **AC6:** Given a `ProviderError` message containing a newline and `\x1b[31m`, or a missing diff path containing `\x1b`, then stderr is still one line with no raw control characters.
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
