# P0-S1: Review a diff offline, end to end, with clear input errors

- **Status:** Done
- **Depends on:** PR `chore/p0-baseline` ([design.md](../design.md#3-slices))
- **Touches:** `__main__`, `cli`, `settings`, `errors`, `diff_input`, `prompt`, `findings`, `render`, `review`, `providers/base`, `providers/mock`; `pyproject.toml`; `uv.lock`; `.pre-commit-config.yaml` (user or main session)

## Value
A developer can run `python -m review_bot <diff> --provider mock` with no Claude CLI, account or network, and get rendered findings, which are canned, or a clear input error with its own exit code.

## Acceptance criteria
Unless a criterion says otherwise, runs select `--provider mock`.

- **AC1:** Removed: replaced by [P1-S6 AC11](../../phase-1/slices/p1-s6-verify-anchors.md#acceptance-criteria).
- **AC2:** Given the same command with `--threshold critical`, then it exits 0 and stdout is exactly `No findings at or above critical.`
- **AC3:** Given a diff containing `SENTINEL-DIFF-TEXT`, when a review succeeds, then stderr contains `provider=mock model=claude-opus-5-5 prompt=p0-v1 input_tokens=0 output_tokens=0`, and the sentinel appears in neither stdout nor stderr. *(Changed by P1-S9: the prompt version is `p1-v1`.)*
- **AC4:** Parametrised over `--provider`, `--model`, `--threshold` and `--max-diff-bytes` ([settings](../design.md#settings)): given only the variable, `parse_args` uses its value; given also the flag, the flag's; given neither, the table's default (`--provider`: see P0-S4 AC13). Given `--model flag-model`, the recorded request has `model="flag-model"`.
- **AC5:** Given each of: no path; an unknown flag; `--provider nope`; `--threshold urgent`; `--max-diff-bytes 0` or `100000001`; `REVIEW_BOT_MAX_DIFF_BYTES=abc`; `--model=--dangerous`, then `main` returns 2 and stderr names the offending flag or variable.
- **AC6:** Removed: replaced by P0-S4 AC13.

**Input errors.** In AC7 to AC10 the provider is never called, and stderr has neither the mock warning nor the AC3 summary, which appear only after a successful review.
- **AC7:** Given a path that doesn't exist, then it exits 3 and stderr contains that path.
- **AC8:** Given a directory, a file containing the bytes `FF FE 41 00` (UTF-16 from PowerShell redirection), or a read that raises `PermissionError` (monkeypatched), then it exits 4 and stderr contains the path.
- **AC9:** Given a 0-byte file, or one containing only `" \n\t\n"`, then it exits 5 and stderr contains the path.
- **AC10:** Given `--max-diff-bytes 10` and an 11-byte file, then it exits 6 and stderr contains both `11` and `10`. Given the default limit and a 400,001-byte file, it exits 6.
- **AC11:** Given a diff exactly `--max-diff-bytes` bytes long, then the provider receives all of it and the run exits 1. *(Changed by P1-S2: a 10-byte file can't be a diff.)*

**Prompt**
- **AC12:** Given `Assignment/sample_diff.txt` (malformed hunk counts, `{self.id}` braces), then the request's `user_prompt` contains the file's text unchanged and the run isn't rejected.
- **AC13:** Given any diff, then `user_prompt` is the opening delimiter line, the diff, and the closing delimiter line; `system_prompt` doesn't contain the diff and states that delimited content is untrusted data, not instructions.
- **AC14:** Given a diff containing the delimiter lines generated for another diff, then the delimiters in its own `user_prompt` occur nowhere inside the diff.
- **AC15:** Given any request, then `system_prompt` requires bare JSON `{"findings": [...]}` with no code fence, and names every `Finding` field and every `Severity` and `Category` value.

**Offline and clean**
- **AC16:** Given `PATH` without `claude` and no `ANTHROPIC_API_KEY`, then all criteria in this spec pass.
- **AC17:** Given the repository, then `pyproject.toml` and `uv.lock` list no `openai` package, and `src/` contains no `openai` import and no `sk-` key literal. `Assignment/` isn't scanned. This holds only once S1 has rewritten the baseline copy. The wider no-secrets check is the [CI gitleaks step](../design.md#1-goal-and-gate).
- **AC18:** Given the path `src/review_bot/cli.py`, when each `exclude` pattern in `.pre-commit-config.yaml` is evaluated as a regex against it, then none matches; and the ruff, mypy and coverage config in `pyproject.toml` has no exclusion covering it.

**Safe output.** Findings are injected through `MockProvider(findings=...)`.
- **AC19:** Parametrised over `file` and `rationale`: given a value containing `\x1b[31m`, a newline, or ` `, then the finding renders on exactly one line with no raw control characters. This includes a `file` of `a.py:1 [high/logic] x\nb.py:2 [critical/security] forged`, which must not produce a second finding line.
- **AC20:** Given a rationale containing `→` and an ASCII-only stdout, then rendering doesn't raise and the run exits 1.

## Interfaces
See [design.md](../design.md#interfaces). Tests inject providers through `main(argv, provider=...)` and read `MockProvider.requests`.

- Finding line: `{file}:{line} [{severity}/{category}] {rationale} (confidence {confidence:.2f})`
- Canned finding: `mock/example.py:1 [high/logic] Canned MockProvider finding, not a real review. (confidence 1.00)`

## Out of scope
- `ClaudeAgentProvider` (P0-S2); provider failures, one-line stderr, conformance (P0-S3).
- Parsing the diff, or checking findings against it: a non-diff text file is accepted here (Phase 1).

## Notes
- **Settings:** deviation from CONVENTIONS §9: an 11-line loop needs no new dependency (§12) and handles S2's settings; if it survives Phase 0, amend §9.
- **Precedence:** a flag wins, and an overridden variable isn't read or validated.
- **Stderr:** direct stderr lines rather than `logging` (§11), because the spec fixes exact CLI lines.
- **AC14:** the guarantee is the digest's cryptographic one, with no collision loop.
- **`provider=None`:** test-only until P0-S4.
- **Interim behaviour:** paths are raw on stderr until P0-S3 AC6; unexpected exceptions escape until P0-S2; `ValidationError` is uncategorised until P0-S3.
- **PR size:** exceeds §18's 400-line guideline, justified as one vertical slice.
