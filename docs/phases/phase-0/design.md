# Phase 0: Stabilise — design

## 1. Goal and gate

Make the tool people rely on actually work and fail safely, with the model behind one narrow interface. Scope and gate: [README Phase 0](../../../README.md#phase-0-stabilise). `Assignment/` stays untouched, outside the gate: its `openai` import and placeholder key are an accepted exception.

**No secrets:** the gate relies on the CI step "Scan the whole tree for secrets (gitleaks)". Slice tests add only the narrow check in P0-S1.

## 2. Architecture

### Module map

Under `src/review_bot/`. `findings`, `prompt` and `render` are pure. `cli`, `settings`, `diff_input` and `providers` touch the outside world; `review` orchestrates the I/O through injected collaborators.

| Module | Responsibility | Key types |
|---|---|---|
| `__main__` | `python -m review_bot`, delegates to `cli.main` | |
| `cli` | Arguments, one `asyncio.run`, printing, error → exit code | `main`, `parse_args`, `build_provider` |
| `settings` | Environment → typed settings; read once | `Settings` |
| `errors` | Base exception, input errors, provider errors | `ReviewBotError`, `ProviderError`, `ErrorCategory` |
| `diff_input` | Read and gate the diff file | `load_diff` |
| `prompt` | Versioned system prompt; wraps the diff as data | `build_request`, `PROMPT_VERSION` |
| `findings` | `Finding` schema; parse raw model text | `Finding`, `Severity`, `Category`, `parse_findings` |
| `render` | Threshold filter and text output | `at_or_above`, `render_findings` |
| `review` | Orchestrates load → prompt → provider | `review_diff` |
| `providers/base` | The seam ([ADR 0002](../../adr/0002-llm-provider-interface.md)) | `LLMProvider`, `ReviewRequest`, `ReviewResponse`, `TokenUsage` |
| `providers/mock` | Scripted offline provider | `MockProvider` |
| `providers/claude_agent` | The only module importing the SDK ([ADR 0001](../../adr/0001-claude-agent-sdk-runtime.md)) | `ClaudeAgentProvider` |

Data flow: load → build prompt → call provider → filter by threshold → render. Any `ReviewBotError` becomes one stderr line and an exit code.

### Interfaces

```python
# cli: `provider` is injected by tests; None means build_provider(settings).
def main(argv: Sequence[str] | None = None, *, provider: LLMProvider | None = None) -> int: ...
def parse_args(
    argv: Sequence[str],
) -> tuple[Path, Settings]: ...  # Settings fields: flag names in snake_case
def build_provider(settings: Settings) -> LLMProvider: ...


# errors
class ErrorCategory(StrEnum):  # values are the lower-case names
    AUTH
    RATE_LIMIT
    REFUSAL
    TRANSIENT
    PERMANENT
    UNAVAILABLE
    INVALID_OUTPUT


class ProviderError(ReviewBotError):
    def __init__(self, category: ErrorCategory, message: str) -> None: ...


# providers/base
@dataclass(frozen=True, slots=True)
class ReviewRequest:
    system_prompt: str
    user_prompt: str
    model: str


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int  # including cache tokens: see P0-S2
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ReviewResponse:
    findings: tuple[Finding, ...]
    usage: TokenUsage


class LLMProvider(Protocol):
    async def review(self, request: ReviewRequest) -> ReviewResponse:
        """Return validated findings; raise ProviderError for every known failure.

        An unexpected exception is a bug: it propagates and `main` returns 14.
        """


# providers/mock: findings=None gives the canned finding; raw_output goes through parse_findings;
# usage is always TokenUsage(0, 0).
class MockProvider:
    requests: list[ReviewRequest]  # recorded, for tests

    def __init__(
        self,
        *,
        findings: Sequence[Finding] | None = None,
        raw_output: str | None = None,
        error: ProviderError | None = None,
    ) -> None: ...


# providers/claude_agent: the default sdk_call wraps query(prompt=..., options=...) and must close
# the inner query (aclosing or finally), so the deadline's aclose reaches it.
type SdkCall = Callable[[str, ClaudeAgentOptions], AsyncGenerator[object, None]]


class ClaudeAgentProvider:
    def __init__(self, settings: Settings, *, sdk_call: SdkCall | None = None) -> None: ...


# findings
class Severity(StrEnum):
    LOW
    MEDIUM
    HIGH
    CRITICAL  # in ascending order


class Category(StrEnum):
    LOGIC
    SECURITY
    RELIABILITY
    MAINTAINABILITY
    TESTING


class Finding(BaseModel):  # frozen; extra fields forbidden
    file: str  # non-empty
    line: int  # >= 1
    severity: Severity
    category: Category
    rationale: str  # non-empty
    confidence: float  # 0.0 to 1.0 inclusive


def parse_findings(raw: str) -> tuple[Finding, ...]:
    ...  # bare {"findings": [...]}
    # From P0-S3, raises ProviderError(INVALID_OUTPUT, ...) without embedding model text.


# tests/conformance
type Scenario = Literal["valid", "empty", "invalid"] | ErrorCategory


def provider_factory(scenario: Scenario) -> LLMProvider: ...  # fixture, one per provider
```

### Exit codes

A public contract: codes are never renumbered or reused, and new ones are appended. Python exits `1` on an uncaught exception, which would read as "findings found", so nothing may escape `main` (code 14). A failure before `main` runs, such as a broken install, is outside this contract.

| Code | Meaning | Raised for |
|---|---|---|
| 0 | No findings at or above threshold | |
| 1 | Findings at or above threshold | |
| 2 | Usage or configuration error | Bad flag, missing argument, invalid setting |
| 3 | Diff not found | Path doesn't exist |
| 4 | Diff unreadable | Directory, permission denied, not valid UTF-8 |
| 5 | Diff empty | 0 bytes, or whitespace only |
| 6 | Diff too large | Over the byte limit; never truncated |
| 7 | Provider unavailable (`unavailable`) | Claude CLI missing |
| 8 | Authentication failed (`auth`) | |
| 9 | Rate limited (`rate_limit`) | After the CLI's retries |
| 10 | Model refused (`refusal`) | |
| 11 | Temporary API or connection failure (`transient`) | After the CLI's retries, or deadline passed |
| 12 | Permanent API failure (`permanent`) | Non-retryable API error, billing, CLI crash |
| 13 | Invalid model output (`invalid_output`) | Not bare JSON, or fails the `Finding` schema |
| 14 | Internal error | Any unexpected exception |

### Settings

Precedence: CLI flag, then environment variable, then default. The bot never reads or stores `ANTHROPIC_API_KEY`.

| Flag | Environment variable | Default |
|---|---|---|
| `--provider` | `REVIEW_BOT_PROVIDER` | none until P0-S4, then `claude` |
| `--model` | `REVIEW_BOT_MODEL` | `claude-opus-5-5` |
| `--threshold` | `REVIEW_BOT_THRESHOLD` | `low` |
| `--max-diff-bytes` | `REVIEW_BOT_MAX_DIFF_BYTES` | `400000` |
| `--max-retries` | `REVIEW_BOT_MAX_RETRIES` | `2` |
| `--api-timeout-ms` | `REVIEW_BOT_API_TIMEOUT_MS` | `90000` |
| `--deadline-s` | `REVIEW_BOT_DEADLINE_S` | `300` |

`--max-diff-bytes` and `REVIEW_BOT_MAX_DIFF_BYTES` have an upper bound of 100,000,000 bytes. A value above it is an invalid setting: it exits 2 and names the flag or variable. The bounded read allocates its size up front, so an unbounded limit could raise `MemoryError` even on a tiny file.

`--deadline-s` is the only wall-clock bound: in a real run with `API_TIMEOUT_MS=2000` and one retry, a 12.8 s call still succeeded.

### Notes for implementers

- **Criterion handovers:** some criteria state interim behaviour and name the criterion that replaces them. The replacing PR retires the old test, and the designer marks the old criterion `Removed`.
- `tests/unit/test_main.py` asserts the stub's behaviour and has to go in S1.
- Tokens are tracked per call. Cost and latency (CONVENTIONS §13) wait for the Phase 2 metrics.
- Line numbers for the same issue varied between real runs of `sample_diff.txt`: Phase 1 anchor and eval territory.

## 3. Slices

Before S1, as process and not a slice: **PR `chore/p0-baseline`** copies `Assignment/review_bot.py` byte for byte to `src/review_bot/cli.py`, with temporary exclusions for that one file from ruff, mypy, coverage and pre-commit fixers. Copy with `cp`, never the Write or Edit tools, because the format hook rewrites the file. It isn't imported or run. S1 rewrites it and removes the exclusions. The pre-commit exclusion is outside the developer's write paths, so the user or main session both adds and removes it.

1. [P0-S1: Review a diff offline, end to end, with clear input errors](slices/p0-s1-offline-review.md)
2. [P0-S2: Get a real review from Claude with an explicit flag](slices/p0-s2-claude-review.md)
3. [P0-S3: Get a distinct exit code and a safe error message for every provider failure](slices/p0-s3-provider-failures.md)
4. [P0-S4: Get clear, specific errors from Claude, with Claude as the default](slices/p0-s4-claude-errors.md)

## 4. Open questions

None for the user.

### Remaining SDK unknowns

Facts checked against `claude-agent-sdk` 0.2.178 (bundled CLI 2.1.318) are built into the specs. Two aren't visible there: `options.env` merges over the process environment, and `setting_sources=[]` still picks up the login. Still unknown:

- **Refusal (the one unverified mapping):** `stop_reason="refusal"` wasn't observed and isn't in the SDK source. Tested with fakes only.
- **Plain JSON:** 3 of 3 responses parsed as bare JSON, a small sample. `output_format` is unused and untested.
- **`API_TIMEOUT_MS`:** whether it applies per attempt.
- **Cache token fields:** the sum in P0-S2 is inferred from `usage["input_tokens"]` being 2 on a much larger prompt.
- **Bundled CLI:** inferred from wheel size and `_find_bundled_cli`.
