# P1-S9: See one finding when a rule and the model report the same problem

- **Status:** Approved
- **Depends on:** P1-S8
- **Touches:** `findings`, `prompt`, `verify`, `review`, `audit`

## Value
A developer doesn't read the same problem twice, a different problem on the same line isn't lost, and a model's tag can't hide something more serious.

## Acceptance criteria
Over `Assignment/sample_diff.txt`, whose rule hits are line 35 (`medium`) and line 39 (`low`), with `MockProvider(findings=...)` or `raw_output`. Rule: [design.md](../design.md#fingerprints-and-dedup).
- **AC1:** Given an AI finding on line 35 with `rule` `swallowed-exception` and severity `medium`, then only the rule's line prints, `deduped=1`, and `verify` records its outcome as a duplicate, naming the rule finding's fingerprint.
- **AC2:** Parametrised over lines 32 and 36, 3 and 1 lines away, severity `low`, then the same.
- **AC3:** Parametrised over the same `rule` on line 31 (4 away), line 30 (5 away), and line 14 (another hunk), then both print: it isn't the same report.
- **AC4:** Given an AI finding on line 39 with no `rule`, or with `swallowed-exception`, about an unvalidated path, then both print. Two problems share a line.
- **AC5:** Parametrised over an AI finding on line 35, `rule` `swallowed-exception`, severity `high` or `critical`, then both print, so a tag can't suppress a more serious finding; `deduped=0`. With `--threshold high`, only the AI line prints.
- **AC6:** Given an AI finding on line 39, `rule` `open-without-encoding` and an invalid `line`, such as 42, then it's dropped as an anchor failure, not deduped.
- **AC7:** Given raw output with `"rule": "made-up"`, then it exits 13 and stdout is empty. Given `"rule": null`, or no `rule`, then it's a normal AI finding. Given `"rule": "secret"`, then it exits 13: the model can't claim a secret rule. *(Replaces the `rule` case of P1-S7 AC13.)*
- **AC8:** Given an AI finding with `rule` and no matching rule finding, then it prints, and its fingerprint uses the rule ID. With no `rule`, it uses `ai:<category>`.

**Prompt**
- **AC9:** Given any request, then `system_prompt` names `rule` and both rule IDs, says when to omit it, says `line` must be an added or context line shown in a hunk, and says a hunk header's counts may be wrong.
- **AC10:** Given any successful run, then the summary has `prompt=p1-v1`, and `run.start` has `p1-v1`.
- **AC11:** Given a test holding a SHA-256 of the `p1-v1` system prompt plus the `user_prompt` built for a fixed diff, then a change to the prompt or its framing with an unchanged version fails it: the test's comment says to bump `PROMPT_VERSION` and re-record evals.

## Interfaces
`Finding.rule`, `RuleId`, `verify_findings`: [design.md](../design.md#interfaces). [ADR 0003](../../../adr/0003-finding-fingerprints.md).

## Out of scope
- Passing rule findings into the prompt, or telling the model to avoid those classes: the evals need its independent view.

## Notes
- **Handover:** P0-S1 AC3's `prompt=p0-v1` becomes `p1-v1`. P0-S1 AC15's schema check now requires `rule` in the prompt. P0-S3 AC2's "an extra field" must not use the name `rule`. P0-S3's `many_findings` child process (`tests/integration/test_p0_s3_stream_failures.py:63`) repeats one identical finding 5,000 times, which AI-vs-AI dedup collapses to one, so its output would no longer outgrow the stream buffers: give each finding a distinct anchored line or rationale (changed before build, main session, 2026-11-06).
- **README (main session):** line 110 says fingerprints dedupe rule and AI findings; dedup actually compares file, rule, proximity and severity.
