# Review Bot: project rules

## Sources of truth

Read these before working. Don't restate them in other files; link to them.

| File | Holds |
|---|---|
| `README.md` | Current state, north star, phases, risks |
| `CONVENTIONS.md` | Python coding conventions (binding for all code) |
| `docs/phases/phase-N/design.md` | Vertical slices and acceptance criteria for phase N |
| `docs/adr/` | Architecture decisions (only the important ones) |
| `Assignment/` | The inherited `review_bot.py` and `sample_diff.txt` |

## Roles

The four subagents are defined in `.claude/agents/`:

| Agent | Does | Writes to |
|---|---|---|
| `designer` | Splits a phase into vertical slices, designs the architecture, records key decisions | `docs/phases/`, `docs/adr/` |
| `developer` | Implements one slice, with unit tests | `src/`, `tests/unit/`, `pyproject.toml` |
| `tester` | Writes acceptance, integration and eval tests from the acceptance criteria, runs the suite, reports | `tests/` (not `tests/unit/`), `docs/phases/phase-N/test-report.md` |
| `reviewer` | Reviews design or code against criteria and conventions; **read-only** | Nothing: returns findings |

## Workflow (per phase)

```
designer ──► USER approves design.md + ADRs
  for each slice, in design order:
    developer ∥ tester   (tester works from the acceptance criteria, not from the code)
    → automated gate: ruff format --check, ruff check, mypy, pytest (all green)
    → reviewer (given the slice criteria + diff, not the developer's reasoning)
    → developer fixes → reviewer re-checks   (max 2 rounds, then escalate to USER)
    → USER accepts the slice
```

- Handoffs go through files, never only through chat. Each agent ends by listing the files it changed.
- Don't commit or push; the user does that.
- `main` is protected: every change reaches it through a pull request from a feature branch (`feat/p1-s3-anchor-check`, `docs/phase-1-design`). Direct pushes to `main` are rejected.
- If a slice's acceptance criteria are unclear or wrong, stop and send it back to the designer. Don't guess.

## Definition of done (per slice)

- [ ] Every acceptance criterion has a passing test that names it.
- [ ] `ruff format --check`, `ruff check`, `mypy --strict`, and `pytest` all pass.
- [ ] Follows `CONVENTIONS.md`; any deviation is justified in the slice notes.
- [ ] No reviewer finding of severity `blocker` or `major` is open.
- [ ] No secrets, no live model calls in tests (use `MockProvider`).
- [ ] The README is updated if behaviour or usage changed.

## Commands

Once `pyproject.toml` exists (Phase 0, first slice):

```
uv sync
uv run ruff format --check . && uv run ruff check . && uv run mypy src && uv run pytest
```
