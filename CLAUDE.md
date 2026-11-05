# Review Bot: project rules

## Sources of truth

Read these before working. Don't restate them in other files; link to them.

| File | Holds |
|---|---|
| `README.md` | Current state, north star, phases, risks |
| `CONVENTIONS.md` | Python coding conventions (binding for all code) |
| `docs/phases/phase-N/design.md` | Phase N goal, gate, architecture, and the ordered slice index |
| `docs/phases/phase-N/slices/pN-sM-*.md` | One slice's spec and acceptance criteria (from `docs/phases/slice-template.md`) |
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
designer ──► USER approves design.md + slice specs + ADRs   (docs PR: docs/phase-N-design)
  for each slice, in design order, on its own branch (feat/pN-sM-title):
    developer ∥ tester   (tester works from the acceptance criteria, not from the code)
    → automated gate: ruff format --check, ruff check, mypy, pytest (all green)
    → reviewer (given the slice spec + diff, not the developer's reasoning)
    → developer fixes → reviewer re-checks   (max 2 rounds, then escalate to USER)
    → USER accepts the slice and merges its PR
```

- **The spec comes first and leads the code.** No slice is implemented until its spec is `Approved`. To change behaviour, change the spec first (designer), then the tests and code. Code that disagrees with its spec is a bug in one of them, and never gets resolved silently.
- **One slice, one PR.** A slice's PR contains only that slice and sets its spec to `Status: Done`. The next slice branches from `main` after the previous one merges.
- **Slice specs stay concise:** aim for about 30 lines and stay under 60. They state observable behaviour, not implementation, and link to `design.md` and ADRs instead of restating them.
- Handoffs go through files, never only through chat. Each agent ends by listing the files it changed.
- Subagents don't commit or push. The main session does, and opens the PR once the work meets its definition of done. The user reviews and merges.
- `main` is protected: every change reaches it through a pull request from a feature branch. Direct pushes to `main` are rejected.
- If a slice's acceptance criteria are unclear or wrong, stop and send it back to the designer. Don't guess.

## Quality bar

Working code is the minimum, not the goal. A slice is approved only when its code is also in very good shape: a newcomer can follow it, change it safely, and find nothing worth cleaning up first.

- **Clear:** names say what things mean; each function does one thing at one level of abstraction; the main path reads top to bottom.
- **Well placed:** logic lives in the module that owns it, boundaries match `design.md`, and nothing is duplicated.
- **Honest failure handling:** every error path is deliberate, and its message tells the user what happened and what to do.
- **Tests that protect behaviour:** they would fail on a wrong implementation, read as specifications, and survive a refactor.
- **Nothing extra:** no dead code, leftovers, speculative options, or comments that restate the code.

A green gate doesn't meet this bar on its own. "It works" is never a reason to leave a finding open.

## Definition of done (per slice)

- [ ] Every acceptance criterion has a passing test that names it.
- [ ] CI `gate` is green: `ruff format --check`, `ruff check`, `mypy --strict`, `pytest --cov` (≥ 90%), pre-commit, `pip-audit`.
- [ ] Follows `CONVENTIONS.md`; any deviation is justified in the slice notes.
- [ ] The reviewer's verdict is `approve`: no finding of severity `blocker`, `major` or `minor` is open, and the code meets the quality bar above.
- [ ] No secrets, no live model calls in tests (use `MockProvider`).
- [ ] The README is updated if behaviour or usage changed.

## Releases (per phase)

Every finished phase ships a release notes file (`docs/phases/phase-N/release-notes.md`: what changed, not progress), a git tag, and a GitHub release with the wheel and source archive.

- Tag the commit where the phase is complete. Its `pyproject.toml` version matches the tag (Phase 0 `0.1.0`, Phase 1 `0.2.0`), so bump it in the phase's last PR.
- Update the README's install URL to the new tag.
- Before publishing: `gate` is green on that commit, and the wheel, built from a clean checkout with `uv build`, is installed in a clean environment and run.
- No PyPI unless the user asks.

## Commands

```
uv sync                         # install / update the environment
pre-commit install              # once per clone: run the hooks on every commit
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest --cov
pre-commit run --all-files      # everything CI's "gate" job runs, except tests and pip-audit
```

- CI (`.github/workflows/ci.yml`, job `gate`) is a required check on `main`.
- Claude Code hooks (`.claude/settings.json`) auto-format every edited `.py` file and block subagent writes outside their role's paths (`.claude/hooks/guard_paths.py`). The guard covers the Write and Edit tools only, not files written through Bash.
