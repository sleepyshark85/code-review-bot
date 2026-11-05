# P0-S4: Get clear, specific errors from Claude, with Claude as the default

- **Status:** Approved
- **Depends on:** P0-S3
- **Touches:** `providers/claude_agent`, `cli`, `settings`

## Value
A developer can run `python -m review_bot <diff>` with no flags and, when Claude fails, see what went wrong and what to do, with the matching exit code.

## Acceptance criteria
**Error mapping.** All raise `ProviderError` unless stated, over a fake `sdk_call` using the real SDK classes. *(AC1 to AC7 each replace part of P0-S2 AC13.)*
- **AC1:** Given a raised `CLINotFoundError` → `unavailable`; plain `CLIConnectionError` → `transient`, keeping the SDK's guidance in the message (accepted, though not always temporary); plain `ProcessError` → `permanent`; any other `ClaudeSDKError`, including `CLIJSONDecodeError` and `MessageParseError` → `permanent`. Subclasses map first.
- **AC2 (status rules):** Given a `ResultMessage` with `is_error=True` or a raised `ResultError`, with `api_error_status` 401 or 403 → `auth`; 429 → `rate_limit`; 408, 500, 503 or 529 (408 and any 5xx) → `transient`; 400, 404 or 413 (any other status) → `permanent`. With no status: `terminal_reason="api_error"` → `transient`; otherwise → `permanent`.
- **AC3:** Given a `ResultMessage` with `stop_reason="refusal"` and `is_error` true or false → `refusal`. *(Fakes only: never observed live.)*
- **AC4:** Given `AssistantMessage.error`, and then no successful result (defined in AC6): `authentication_failed` → `auth`; `rate_limit` → `rate_limit`; `server_error` → `transient`; `billing_error` → `permanent`, with a message naming billing; `invalid_request` or `unknown` → `permanent`; an unrecognised value → `permanent`, with a message naming the value.
- **AC5 (priority):** Given `AssistantMessage.error="authentication_failed"`, then `ResultMessage(subtype="success", is_error=True, stop_reason="stop_sequence", terminal_reason="api_error", api_error_status=401)`, then a raised `ResultError` with status 401, then `auth`. Given the same sequence with `AssistantMessage.error="rate_limit"` and status 500, then `rate_limit`. Given only that `ResultMessage` (status 401), then a raised `ProcessError`, then `auth`. The order is `AssistantMessage.error`, `ResultMessage`, exception.
- **AC6 (a successful result is the verdict):** A successful result is `is_error=False`, `stop_reason` not `refusal`, and `parse_findings` succeeding. Once one arrives, the provider stops reading, closes the stream and returns the findings. Parametrised over non-empty and empty findings: given `AssistantMessage.error` then a successful result, then `main` returns 1 or 0 for those findings, and stderr has a warning naming the overridden signal, without its text. This is a warning on a successful run, not an error line, so P0-S3 AC5 doesn't apply. Given a successful result then a stream that never ends and `--deadline-s 30`, then `review` returns the findings within 5 seconds and the stream has been closed. Otherwise the earlier signal wins: `authentication_failed` then `is_error=False` with prose returns 8; with no earlier signal, that prose returns 13.
- **AC7:** Given a stream ending with no `ResultMessage` and no earlier error signal → `permanent`. Given a raised non-SDK class such as `ValueError` and no earlier successful result or error signal, it stays uncategorised and `main` returns 14. The SDK's bare `Exception("Control request timeout...")` is accepted as exit 14 on the same path.
- **AC8:** Given a fake stream that never ends with no successful result and `--deadline-s 0.1`: with no earlier signal, `review` raises `transient` and `main` returns 11; with an earlier `AssistantMessage.error="authentication_failed"`, it reports that signal's category, `auth`, and `main` returns 8. Either way the stream has been closed within 5 seconds. *(Replaces P0-S2 AC7.)*

**Messages**
- **AC9:** Given a raised `CLINotFoundError` through `main`, then it returns 7 and stderr contains `install` and `PATH`.
- **AC10:** Given `auth`, then `main` returns 8 and stderr names both the Claude Code login and `ANTHROPIC_API_KEY`.
- **AC11:** Given `ANTHROPIC_API_KEY=test-key-sentinel` and a fake `ProcessError` whose `stderr` contains that value, then it appears in neither stdout nor stderr.
- **AC12:** Given `str(ResultError)`, `ResultMessage.result`, or a raised `CLIJSONDecodeError` or `MessageParseError` containing `MODEL-SENTINEL` and `\x1b`, then neither reaches stderr. Those two embed raw CLI output, so the "any other `ClaudeSDKError`" message in AC1 doesn't use their text.

**Default and conformance**
- **AC13:** Given neither `--provider` nor `REVIEW_BOT_PROVIDER`, then `parse_args` selects `claude`, and `build_provider` returns a `ClaudeAgentProvider` and starts no subprocess. *(Replaces P0-S1 AC6 and the "neither" case of P0-S1 AC4 for `--provider`.)*
- **AC14:** Given `tests/conformance/`, then `ClaudeAgentProvider` over a fake `sdk_call` passes the same `provider_factory(scenario)` suite as `MockProvider`, and no SDK type appears in a response.
- **AC15 (manual, by the user, recorded in the PR; never in CI):** Given `ANTHROPIC_API_KEY=invalid`, when `python -m review_bot Assignment/sample_diff.txt` runs, then it exits 8 with exactly one stderr line.

**Retry layer**
- **AC16:** Given an error `ResultMessage` with `api_error_status` 429 or 503, then `sdk_call` was called exactly once.

## Interfaces
Same seam as [P0-S2](p0-s2-claude-review.md#interfaces); exit codes in [design.md](../design.md#exit-codes).

## Out of scope
- Any retry, backoff or fallback in our code (fallback is Phase 4).
- Prompt-injection evals (Phase 1).

## Notes
- S3 AC4 applies to AC6's warning: with stderr closed or failing, the warning is dropped, the review still returns its findings, and `main` exits by them. Add a test for this. Keep the safe stderr write outside `cli` so the provider doesn't import it.
- Usage never affects the verdict: a missing, null or invalid count (non-integer, boolean or negative) counts as 0. Test that a successful result with bad usage still returns its findings.
