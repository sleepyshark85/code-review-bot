# Python Coding Conventions

Rules for all Python code in this repository. Where a tool can enforce a rule, the tool is the source of truth; this document explains the rest.

**Golden rule:** code is read far more often than it is written. Optimise for the next reader.

## 1. Tooling (enforced in CI)

| Concern | Tool | Setting |
|---|---|---|
| Python version | CPython | 3.12+ |
| Environment and dependencies | `uv` | `pyproject.toml` + committed `uv.lock` |
| Formatting | `ruff format` | Line length 100 |
| Linting | `ruff check` | Rule sets below |
| Type checking | `mypy --strict` (or `pyright` strict) | No untyped defs |
| Tests | `pytest` (+ `pytest-asyncio`, `pytest-cov`) | Coverage ≥ 85% on changed code |
| Dependency audit | `pip-audit` | Fails CI on known vulnerabilities |
| Local gate | `pre-commit` | Runs ruff and mypy before each commit |

```toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "W", "F", "I", "N", "UP", "B", "A", "C4", "SIM", "RET", "PTH",
          "TRY", "BLE", "S", "DTZ", "T20", "ERA", "PL", "RUF", "ANN", "D"]
ignore = ["D203", "D213"]

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101", "ANN", "D", "PLR2004"]

[tool.mypy]
strict = true
```

A warning is either fixed or suppressed on that line with a reason: `# noqa: S603 - args are a fixed list`. Never suppress a rule for a whole file to make CI pass.

## 2. Project layout

```
pyproject.toml
src/<package>/
    __init__.py        # public API only, via __all__
    __main__.py        # entry point: python -m <package>
    cli.py             # argument parsing, exit codes; no business logic
    <domain>/          # one package per concept, not per layer type
tests/
    unit/  integration/  fixtures/
```

- Use a `src/` layout, so tests run against the installed package, not the working directory.
- Each module has one responsibility. Split it when it grows past about 400 lines or mixes concerns.
- Avoid modules named `utils.py` or `helpers.py`. Name the module after what it does (`diff_parser.py`, `fingerprint.py`).
- No logic at import time: no I/O, network calls or reading config at module level.

## 3. Naming

| Kind | Style | Example |
|---|---|---|
| Module, package | `snake_case`, short | `diff_parser` |
| Function, variable | `snake_case`, verb for functions | `parse_diff`, `line_count` |
| Class, type alias, `Protocol` | `PascalCase`, noun | `Finding`, `LLMProvider` |
| Constant | `UPPER_SNAKE_CASE` | `MAX_DIFF_BYTES` |
| Private | Leading `_` | `_normalise_path` |
| Boolean | `is_` / `has_` / `should_` prefix | `is_generated` |

- Names state intent and units: `timeout_s`, `size_bytes`, `price_usd`.
- No abbreviations except well-known ones (`id`, `url`, `cfg`). No single letters except loop indices and maths.
- Don't shadow built-ins (`id`, `type`, `list`, `input`).

## 4. Formatting and imports

- `ruff format` decides formatting. Don't argue style in review.
- Import order (handled by ruff `I`): standard library → third-party → first-party, one group each.
- Use absolute imports. No `from x import *`.
- Import modules, not many names, when it reads better: `from pathlib import Path` is fine; `import json` beats `from json import loads, dumps, load`.
- Use f-strings for formatting. Logging is the exception (see §11).

## 5. Type hints

- Every public function, method and attribute is fully typed. mypy strict must pass.
- Use modern syntax: `list[str]`, `X | None`, `type Alias = ...` (3.12).
- Accept abstract types, return concrete ones: take `Iterable[str]` or `Mapping[str, int]`, return `list[str]` or `dict[str, int]`.
- Use `Protocol` for interfaces (structural typing) rather than ABCs, unless you need shared implementation.
- Use `Literal` or `Enum` for closed sets of values, never bare strings.
- `Any` needs a comment explaining why. Prefer `object` or a `TypeVar`.
- Use `typing.cast` only at proven-safe boundaries, with a comment.
- Use `@override` (3.12) on overriding methods, and `Final` for constants.

## 6. Design principles

- **KISS:** the simplest design that meets today's requirement.
- **YAGNI:** no speculative abstractions, parameters or plugin systems. An interface needs a real second implementation, or a test double, to justify it.
- **DRY, by the rule of three:** duplicate once, extract on the third occurrence. Wrong abstractions cost more than duplication.
- **Single responsibility:** a function does one thing at one level of abstraction. A class has one reason to change.
- **Functional core, imperative shell:** pure logic (parsing, rules, policy) in pure functions; I/O (files, network, LLM, clock) at the edges. Pure code is trivially testable.
- **Dependency injection:** pass collaborators (providers, clocks, sinks) in through constructors or parameters. No hidden globals or singletons, and no constructing them deep inside logic.
- **Composition over inheritance:** inheritance depth ≤ 1 outside framework base classes.
- **Explicit over implicit:** no magic, no monkey-patching outside tests, no `**kwargs` pass-through in public APIs.
- **Fail fast:** validate at boundaries and raise immediately; never continue in an invalid state.
- **Immutability by default:** `@dataclass(frozen=True, slots=True)`, `tuple` and `frozenset`. Mutate only where it is measurably needed.
- **Law of Demeter:** talk to collaborators, not to their internals (`a.b.c.d()` is a smell).
- **SOLID** applies, but read through the rules above. Don't add layers to satisfy an acronym.

## 7. Functions and classes

- Aim for ≤ 40 lines per function and ≤ 5 parameters. Use keyword-only parameters (`*`) for anything after the obvious ones.
- Prefer early returns over nested `if` blocks; keep nesting ≤ 3 levels.
- **No mutable default arguments.** Use `None` and create the value inside, or `field(default_factory=list)`.
- Return values rather than mutating arguments. A function either changes state or answers a question, not both.
- No boolean flag parameters that switch behaviour. Write two functions.
- Data holders:

  | Need | Use |
  |---|---|
  | Validating external or untrusted input (JSON, config, LLM output) | `pydantic.BaseModel` |
  | Internal value objects | `@dataclass(frozen=True, slots=True)` |
  | Fixed set of named values | `enum.Enum` / `StrEnum` |

- Use `@property` only for cheap, side-effect-free reads.
- Use `__slots__` (via `slots=True`) on frequently created objects.

## 8. Errors and exceptions

- **Never write a bare `except:`, and never swallow errors** (`except Exception: pass`). Catch the narrowest exception you can actually handle.
- Define one base exception per package (`class ReviewBotError(Exception)`) with specific subclasses. Callers catch the base class; code raises subclasses.
- Chain causes: `raise ConfigError("...") from exc`.
- Error messages say what failed, with which input, and what to do next.
- Translate exceptions at boundaries: SDK, HTTP or OS errors become domain errors in the adapter; the CLI maps domain errors to exit codes and messages.
- Prefer EAFP (`try` around the one statement that can fail) for I/O and races; prefer LBYL for cheap, local checks.
- Use `assert` only for internal invariants, never for input validation (asserts are stripped by `-O`).
- Keep `try` blocks minimal; put success-path code in `else`.
- Clean up with context managers (`with`), not `finally` blocks that do it by hand.

## 9. Data, I/O and resources

- **Parse, don't validate:** convert raw input into typed objects at the boundary once, and pass typed objects inward.
- **Money:** `decimal.Decimal`, never `float`. Round explicitly with the rounding mode stated.
- **Time:** timezone-aware `datetime` (`datetime.now(UTC)`). Store and compare in UTC. Inject a clock where tests need control.
- **Files:** `pathlib.Path`; always pass `encoding="utf-8"`; always open with `with`.
- **Structured formats:** use `csv`, `json`, or `tomllib`. Never build CSV, JSON, or SQL strings by hand.
- **Network:** every call has a timeout. Retry only transient errors, with backoff and a cap, in one place.
- **Configuration:** environment variables → a typed settings object (`pydantic-settings`), overridable by CLI flags. Read once, at startup.
- Prefer generators and iterators for large or streaming data.

## 10. Async

- Use async for I/O-bound concurrency only. Never block the event loop: no `time.sleep`, synchronous HTTP or heavy CPU work inside `async def`. Use `asyncio.to_thread` if you must.
- One `asyncio.run()` at the entry point. Libraries never start their own event loops.
- Use `asyncio.TaskGroup` for structured concurrency, and `asyncio.timeout()` for deadlines.
- Never leave a task un-awaited, and never leave a coroutine running in the background without someone holding a reference to it.

## 11. Logging and output

- Use `logging` with `logger = logging.getLogger(__name__)` per module. Configure handlers only in the entry point.
- `print` is reserved for the CLI's actual output (ruff `T20` enforces this). Diagnostics go to logs on stderr.
- Use lazy formatting: `logger.info("parsed %d hunks", n)`, not f-strings.
- Prefer structured fields (JSON logs) for anything machines read. Include correlation IDs (run ID, PR number).
- Levels: `DEBUG` internals · `INFO` milestones · `WARNING` degraded but continuing · `ERROR` the operation failed. Log an error once, where it is handled, not at every layer.
- **Never log secrets, tokens or full untrusted payloads.** Redact them.

## 12. Security

- No secrets in code, tests, logs or git history. Read them from the environment or a secret manager.
- Treat all external input as hostile: files, diffs, HTTP bodies, environment variables, and **LLM output**.
- Never call `eval`, `exec`, `pickle.load` or `yaml.load` on untrusted data. Use `yaml.safe_load` and `json`.
- `subprocess`: pass an argument list, never `shell=True`, and always set a timeout.
- Resolve paths and check they stay inside the allowed root before reading or writing.
- Pin dependencies through the lock file, and audit them in CI. Add a dependency only when it earns its weight.
- Use `secrets`, not `random`, for anything security-related.

## 13. LLM and agent code

- Keep prompts as versioned constants or files, never assembled ad hoc across modules. Record the prompt version with each output.
- Wrap untrusted content in clear delimiters and tell the model it is data, not instructions.
- **The model proposes, code disposes.** Validate every model output against a schema (pydantic), check facts deterministically (line numbers, file paths), and reject what fails. Never repair it by hand.
- Vendor SDKs live behind a narrow interface (`Protocol`). No SDK types leak past the adapter.
- Model output never triggers a side effect directly; a policy layer decides.
- Grant agent tools by explicit allowlist, deny everything else, and assert the lockdown in tests.
- Tests use a mock provider and never call a live model. Behaviour changes are measured with evals, not by eyeballing output.
- Track tokens, cost and latency per call.

## 14. Testing

- Use `pytest`, with plain `assert` statements and fixtures over setup methods.
- Name tests for behaviour: `test_<unit>_<condition>_<expected>`, e.g. `test_parse_diff_with_bad_hunk_header_recovers`.
- Structure each test as Arrange, Act, Assert, with one behaviour per test. Use `@pytest.mark.parametrize` for variations.
- Test behaviour through public APIs, not private functions or implementation details.
- Unit tests never touch the network, the real clock or shared state, and they run in any order. Use `tmp_path` for files.
- Use fakes (a simple working implementation) over mocks. Mock only at the system's edges.
- Every bug fix comes with a regression test that fails without the fix.
- Golden files under `tests/fixtures/` for parsers and renderers; property-based tests (`hypothesis`) for parsers and pure logic.
- Mark slow and integration tests (`@pytest.mark.integration`) so the default run stays fast.

## 15. Documentation and comments

- Public modules, classes and functions get a Google-style docstring: a one-line summary, then `Args`, `Returns` and `Raises` where they aren't obvious from the types.
- Comments explain **why**, not what. If code needs a "what" comment, rename or extract instead.
- No commented-out code (ruff `ERA`); git remembers it.
- A `TODO` comment includes an owner or an issue: `# TODO(#42): ...`.
- The README covers what the project is, how to run it, how to test it, and its design decisions.

## 16. Performance

- Measure before optimising (`cProfile`, `py-spy`). Readability wins until a profile says otherwise.
- Choose the right data structure first: `set` or `dict` for membership checks, `collections.deque` for queues.
- Avoid repeated work in loops; hoist invariants, and cache pure functions with `functools.cache`.
- Join strings with `"".join(...)`, not `+=` in loops.

## 17. Idioms checklist

- `if x is None`, never `== None`. Use the truthiness of a container (`if items:`), not `len(items) > 0`.
- `enumerate()` instead of `range(len())`. `zip(a, b, strict=True)`.
- Comprehensions over `map`/`filter` with lambdas. Keep them to one level, or use a loop.
- `dict.get` / `setdefault` / `collections.defaultdict` / `Counter` instead of manual bookkeeping.
- `match` for structural dispatch on shapes, not as a replacement for a simple `if`.
- Unpacking: `first, *rest = items`. Walrus `:=` only when it removes duplication.
- `__all__` in every public module. No wildcard re-exports.
- Use `functools`, `itertools` and `contextlib` from the standard library before writing your own.
- Keep globals to constants only.

## 18. Git and review

- Small PRs, ideally under 400 changed lines, each doing one thing. CI is green before review.
- Commit messages: an imperative subject of 72 characters or fewer, and a body explaining *why*. Use Conventional Commits prefixes (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`).
- Reviewers check correctness, tests, naming and design. Formatting is the tools' job.
- Deviations from this document are allowed when justified in the PR description. Repeated deviations mean the document should change.
