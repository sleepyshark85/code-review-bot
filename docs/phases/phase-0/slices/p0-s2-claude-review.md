# P0-S2: Get a real review from Claude with an explicit flag

- **Status:** Approved
- **Depends on:** P0-S1
- **Touches:** `providers/claude_agent`, `cli`, `settings`, `findings`; `pyproject.toml` (pin `claude-agent-sdk==0.2.178`); `uv.lock`

## Value
A developer can run `--provider claude` and get validated findings from a locked-down model. Failures aren't classified yet: every failed call exits 14 with a generic message, never 0 or 1.

## Acceptance criteria
**Lockdown and limits.** Asserted on the `ClaudeAgentOptions` passed to a fake `sdk_call`.
- **AC1:** Given any review, then `tools == []`, `setting_sources == []`, `max_turns == 1`, `allowed_tools` is empty, `strict_mcp_config is True`, `mcp_servers` is empty, and no hooks or subagents are configured.
- **AC2:** Given a request with `system_prompt="S"` and `model="M"`, then the options carry exactly `"S"` (not the Claude Code preset) and `"M"`, and the prompt passed is the request's `user_prompt`.
- **AC3:** Given default settings, then `options.env` is exactly `{"CLAUDE_CODE_MAX_RETRIES": "2", "API_TIMEOUT_MS": "90000"}`. Given `--max-retries 0 --api-timeout-ms 1000`, the values are `"0"` and `"1000"`.
- **AC4:** Parametrised over `--max-retries`, `--api-timeout-ms` and `--deadline-s` ([settings](../design.md#settings)): given only the variable, `parse_args` uses its value; given also the flag, the flag's; given neither, the defaults `2`, `90000` and `300`.
- **AC5:** Given each of `--max-retries -1`, `--api-timeout-ms 0`, `--api-timeout-ms -1`, `--deadline-s 0`, `--deadline-s -1`, `REVIEW_BOT_MAX_RETRIES=-1`, `REVIEW_BOT_DEADLINE_S=0`, then `main` returns 2 and stderr names the flag or variable.
- **AC6:** Given a fake that raises an exception, then `sdk_call` was called exactly once: the CLI is the only retry layer.
- **AC7:** Given a fake stream that never ends and `--deadline-s 0.1`, then within 5 seconds the stream has been closed and `main` returns 14. *(Replaced by P0-S4 AC8.)*
- **AC8:** Given any review, then `options.stderr` is set; given a fake that calls it with `CLI-NOISE-SENTINEL`, then that text doesn't reach our stdout or stderr.

**Success**
- **AC9:** Given `--provider claude`, then `build_provider` returns a `ClaudeAgentProvider` and starts no subprocess.
- **AC10:** Given a `ResultMessage` with `is_error=False` and `result` of `{"findings": [<valid high finding>]}`, then `main` prints that finding and returns 1. Given `{"findings": []}`, it returns 0.
- **AC11:** Given `usage={"input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 7}`, then `TokenUsage(152, 7)`, and stderr's summary shows `provider=claude`, `input_tokens=152` and `output_tokens=7`. Given `{"input_tokens": 2, "output_tokens": 7}`, then `TokenUsage(2, 7)`. Given `usage=None`, then `TokenUsage(0, 0)`.
- **AC12:** Given no `ANTHROPIC_API_KEY`, then the provider still calls `sdk_call`, leaving authentication to the CLI.

**What a failure does in this slice**
- **AC13:** Given a fake that raises an exception whose text contains `SENTINEL`, a `ResultMessage` with `is_error=True` (including `subtype="success"`) whose `result` contains `SENTINEL`, or a stream ending with no `ResultMessage`, then `main` returns 14, stdout is empty, `result` isn't parsed as findings, and stderr has no traceback and no `SENTINEL`. *(Replaced by P0-S4 AC1 to AC7.)*
- **AC14:** Given `is_error=False` and `result` that is not JSON, then `main` returns 14. *(Replaced by P0-S3 AC3.)*
- **AC17:** Given an unexpected exception anywhere in `main`, including an injected provider raising `RuntimeError("SENTINEL")` and writing output with stdout closed (`--threshold critical >&-`), then `main` returns 14, not 1, and stderr has no traceback and no `SENTINEL`. *(Extended by P0-S3 AC4.)*

**Offline and live**
- **AC15:** Given a session-wide guard failing any test that reaches the real `query`, then the full suite passes, whether or not a CLI is present.
- **AC16 (manual, by the user, recorded in the PR; never in CI):** Given a real login, when `python -m review_bot Assignment/sample_diff.txt --provider claude` runs, then it exits 0 or 1 with valid findings.

## Interfaces
- Offline seam: `ClaudeAgentProvider(settings, sdk_call=fake)` ([design.md](../design.md#interfaces)). The fake is an async generator that records `(prompt, options)` and scripts messages or a raised exception.
- `claude-agent-sdk` is installed in tests, so fakes use the real classes. The tester may read the installed package for exact field names and constructors.
- The default `sdk_call` wraps `query`; only AC16 exercises it.

## Out of scope
- Error categories and specific exit codes (P0-S3, P0-S4).
- Claude as the default provider: with none selected, P0-S1 AC6 still applies (P0-S4).
- Our own retries or backoff; `output_format`; cost tracking (Phase 2).

## Notes
