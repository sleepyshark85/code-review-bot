# P4-S2: Review with a second, OpenAI-compatible provider that passes the conformance suite

- **Status:** Done
- **Depends on:** none
- **Touches:** `providers.openai_wire`, `providers.openai_compat` (new), `settings`, `cli`, `pr.cli`, `evals.cli`, `tests/conformance` (factory)

## Value
A diff can be reviewed by any model behind an OpenAI-compatible Chat Completions endpoint, OpenAI's or a self-hosted server's, with the same prompts, validation and error categories as Claude ([ADR 0020](../../../adr/0020-openai-compatible-second-provider.md)).

## Acceptance criteria
All offline, over a fake `HttpTransport`. Shape: [design, Interfaces](../design.md#interfaces).
- **AC1:** Given `--provider openai --model m1` and `OPENAI_API_KEY` set, when a diff is reviewed, then exactly one POST goes to `<base>/chat/completions` with `Authorization: Bearer <key>`, a JSON body whose `model` is `m1` and whose `messages` are the system and user prompts byte for byte, the output cap only when `--max-output-tokens` is set, and no `tools`. The output cap is sent as `max_completion_tokens` when the base URL's host is `api.openai.com`, and as `max_tokens` for any other host ([design](../design.md#second-provider-and-routing); Q2's answer confirms the field). The default base is `https://api.openai.com/v1`.
- **AC2:** Given a 200 response whose first choice's content is `{"findings": [...]}`, then findings are validated by the shared parser as Claude's are, usage comes from `usage.prompt_tokens` and `usage.completion_tokens`, and the report's `provider` is `openai` and its `model` `m1`.
- **AC3:** Given these responses, then the review fails with these categories and exit codes: 401 or 403 → `auth`; 429 → `rate_limit`, but 429 with `error.code` `insufficient_quota` → `permanent`; 500 to 599, a timeout or a reset connection → `transient`; refused connection or DNS failure → `unavailable`; other 4xx, or any 3xx (never followed) → `permanent`; `finish_reason` `content_filter`, or a non-empty `message.refusal` → `refusal`; content that isn't the findings JSON, `finish_reason` `length`, or a body over 4 MB → `invalid_output`.
- **AC4:** Given `--max-retries 1` and a 503 then a 200, then the review succeeds with one warning naming the retry. A `Retry-After` of up to 30 s is honoured, in seconds or as an HTTP date, and a longer one fails at once; an unparsable value is ignored. Retries happen only for `transient` and `rate_limit`, and never past `deadline_s`, which bounds the whole call.
- **AC5:** Given `tests/conformance`, then every scenario that passes for the mock and Claude passes for `openai` over the fake transport, except `tool_loop` (S6).
- **AC6:** Given `--openai-base-url` (`REVIEW_BOT_OPENAI_BASE_URL`) with `http` to a non-loopback host, userinfo, a query or a fragment, then exit 2. `http://127.0.0.1:8000/v1`, `http://localhost/v1` and `http://[::1]/v1` are accepted.
- **AC7:** Given no `OPENAI_API_KEY` and a non-loopback base, then exit 2 naming the variable, before any request. With a loopback base, the request has no `Authorization` header.
- **AC8:** Given any entry point, then `OPENAI_API_KEY` is removed from the process environment before a provider is built, as `GITHUB_TOKEN` is, so a Claude CLI subprocess never holds it. The key never appears in a message, diagnostics, a report or the audit log, and an error body's text is kept only as scrubbed diagnostics.
- **AC9:** Given `--provider openai` with `--max-cost-usd`, or (if P4-S1 has merged) under a policy whose `[cost]` ceiling would apply, then the command exits 2 before any request: "--max-cost-usd isn't supported with --provider openai yet". Nothing would enforce the cap until S6.
- **AC10:** Given `--provider openai` with no `--model` (or `REVIEW_BOT_MODEL`), then the command exits 2 before any request, naming `--model`: Claude's default model name is never sent to another provider.

## Limits
Response bodies are untrusted: at most 4 MB are read, then the call fails `invalid_output`. JSON is parsed once, and a body that isn't a JSON object, or that lacks `choices`, fails `invalid_output`. `error.message` never reaches the user's message.

## Interfaces
`--provider openai`, `--openai-base-url URL`. The env var `OPENAI_API_KEY`. `HttpTransport` and `UrllibTransport`, per the design.

## Out of scope
- Tools and cost (S6); routing (S7); live calls (S14).

## Notes
- A request with tools (context mode) fails `permanent` before any request, rather than being sent without them; S6 adds the tool loop.
- With no `Retry-After`, a retry waits 1 s, then 2 s, doubling up to 30 s. A wait that would reach `deadline_s` fails at once with the last error.
- Without a response, a timeout, a dropped connection or a malformed response is `transient`; any other OS error (a refused connection, a failed DNS lookup, an unreachable network or a TLS failure) is `unavailable`.
- AC10: "no `--model`" means neither the flag nor `REVIEW_BOT_MODEL` was given (the setting isn't in the settings' fields set), whatever the value. So `--model claude-opus-5-5` given explicitly is sent as is. The check runs in `cli.check_provider_settings`, which `review-bot` and `pr shadow|run|backfill` call before anything is sent. `evals` takes no `--provider` and always reviews with Claude, so it has no openai path to check.
- `UrllibTransport` and `HttpResponse` live in `providers.urllib_transport`, and `openai_compat` re-exports them as the design names them, because the deadline handling would take `openai_compat` past about 400 lines. A timer shuts the request's socket once `timeout_s` has passed, so the whole request is bounded and not just each read. The redirect refusal is shared with `pr.rest_github` through `review_bot.no_redirect`.
- A base URL or key that a request can't carry (whitespace, a control character, non-ASCII, or a bad port) exits 2, rather than failing later at send time.
- The provider removes an echoed key from an error body's diagnostics itself, since `OPENAI_API_KEY` is no longer in the environment that `scrub_diagnostics` reads.
