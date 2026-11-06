# P1-S11: Get the review as Markdown

- **Status:** Approved
- **Depends on:** P1-S10
- **Touches:** `render`, `cli`

## Value
A developer can paste a readable review into a pull request, and text from the diff or the model can't forge content in it.

## Acceptance criteria
With a scripted `MockProvider` and `--format markdown`. Golden files pin the layout.
- **AC1:** Given the sample diff's two rule findings plus one AI finding, then stdout matches the golden: a `## Review` heading, the counts, then one list item per finding with severity, category, `file:line` in a code span, rationale, confidence and source. Exit 1.
- **AC2:** Given no findings, then stdout is the heading and `No findings at or above low.` Exit 0.
- **AC3:** Given skipped files, then a `Skipped files` section lists each with its reason. Given dropped or deduped findings, then the counts say so.
- **AC4:** Parametrised over a rationale holding each of: `\n## Forged`; `<img src=x onerror=y>`; `![x](http://evil)`; `[click](http://evil)`; `` ` `` fences; `@someone`; `|`; `*`, then it's inert: no new heading, HTML tag, image, link or mention when parsed as CommonMark, the finding count is unchanged, and the visible text is preserved.
- **AC5:** Parametrised over a path, from a diff header, holding a backtick, a newline escape or `<`, then the same.
- **AC6:** Given `\x1b` or U+2028 in any field, then stdout has no raw control character.
- **AC7:** Given `--format text`, then it's unchanged. Given any failure, stdout is empty.

## Interfaces
`render_markdown`: [module map](../design.md#module-map).

## Out of scope
- Inline comments, suggested fixes, or a GitHub check (Phase 2).

## Notes
- AC4 needs a CommonMark parser in the tester's checks only: a `dev` dependency, not a runtime one. The developer adds it in `pyproject.toml`, because the tester can't edit it.
- **README (main session):** describe `--format markdown` in Usage.
- **Non-UTF-8 paths (changed before build, main session, 2026-11-06):** a finding's `file`, or a skipped file's path, can hold lone surrogates for bytes that aren't valid UTF-8 (rule findings on such paths are built with `model_construct`). Show it in git's quoted form, as the audit log writes it ([paths in `audit/v1`](../design.md#audit-log)), so stdout can always be encoded. Trap: `Finding.model_dump_json()` raises `PydanticSerializationError` on such a path, turning exit 1 into 14.
