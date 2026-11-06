# P1-S3: Stop a diff with a detected secret from reaching the model

- **Status:** Done
- **Depends on:** P1-S2
- **Touches:** `secret_scan` (new), `review`, `errors`, `cli`

## Value
A developer whose diff holds a recognisable secret finds out where it is, and the diff never leaves the machine.

## Acceptance criteria
Every secret here is assembled at test run time: see Notes. `V` is the assembled value.
- **AC1:** Parametrised over the seven kinds below: given an added line holding one, then it exits 16, stdout is empty, `MockProvider.requests` is empty, and stderr is exactly one `review-bot: error:` line naming the kind and the diff-file line number, without `V`. Also, with a `V` holding a digit, given a JSON line `"api_key": "V"`, `token: str = "V"`, YAML `password: "V"`, or `password: "V",`. Also `password = "P4ss'w0rd-1234567"`, the other quote inside (changed in review, main session, 2026-11-06).
- **AC2:** Parametrised over where `V` sits: an added line; a removed line; a context line; a commit-message preamble; a `+++ b/` path; a skipped file (`README.md`, `uv.lock`); a hunk with an unreadable header; a removed and an added line in a `diff --cc` section (`--V`, `++password: "V"`); an added line whose content starts with `-` (`+-V`) (these three changed in review, main session, 2026-11-06), then it exits 16 and `V` is in neither stdout nor stderr.
- **AC3:** Given a diff with 7 hits, then the line names the first 5 and states the total, and has no `V`.
- **AC4:** Given text that isn't a diff but holds `V`, then it exits 16, not 15. Given `V` in a diff over the size limit, it exits 6: the file was never read whole.
- **AC5:** Given `--threshold critical`, then a hit still exits 16: the threshold doesn't lower it.
- **AC6:** Given near misses, then each is reviewed normally and doesn't exit 16: `AKIA` plus 15 characters; `password = "changeme"`; `password = "short"`; `api_key = "your-api-key-goes-here"`; `api_key = os.environ["KEY"]`; `token = get_token()`; `openai.api_key = "sk-REPLACE-ME"` (too short); a SHA-256 hex digest; `def f(token: "AccessTokenProvider")`; `token: "AccessTokenProvider"`; `token: "Optional[AccessToken]"`; `token: "auth.AccessTokenClass"`; `tokenizer_name = "bert-base-uncased-v2"`; `TOKEN_URL = "https://auth.acme.com/oauth"`; `task-queue-worker-for-billing`; `secret = "a sentence with spaces in it"`.
- **AC7:** Given `redact` applied to text holding `V`, then each `V` becomes `[REDACTED]` and the rest is unchanged. Given a PEM block, the whole block, from `BEGIN` to `END`, or to the end of the text if `END` is missing, or from the start if only `END` is present, becomes one `[REDACTED]`. Given no secret, it returns the input unchanged.

## Limits
- **Input:** the raw diff text, before parsing, up to the size limit; a larger diff exits 6 unread (AC4). The largest case is one 400 KB line.
- **Cost:** near-linear, `n log n` at worst, for `scan_secrets` and `redact` on hostile lines: packed candidate keys or credential names, long marker or quote runs, unclosed PEM markers, and vendor keys nested in or overlapping credential values.
- **Malformed input:** scanned all the same. Non-diff text and unreadable hunks still exit 16 on a hit (AC2, AC4), an unclosed PEM block is redacted to the end (AC7), and CR-only line endings are a known YAML gap (Notes).

## Interfaces
`scan_secrets`, `redact`, `SecretHit`: [design.md](../design.md#interfaces). Error line, with no path: `review-bot: error: secret: 2 possible secrets in the diff (aws-access-key at diff line 12; private-key at diff line 40), so it was not sent to the model. Remove them and rotate any that are real, then run again.` With one hit (changed in review, main session, 2026-11-06): `review-bot: error: secret: 1 possible secret in the diff (aws-access-key at diff line 12), so it was not sent to the model. Remove it and rotate it if it is real, then run again.`

Every prefix below must not follow a letter, digit, `-` or `_`, and no value may continue into its alphabet. The `private-key` line has no prefix boundary. The leading run of diff marker characters (`-`, `+`, space) on a line counts as markers, not as part of a token, both for this boundary and for YAML's "first on its line"; a `diff --cc` line has one marker column per parent (changed in review, main session, 2026-11-06).

| Kind | Detected form |
|---|---|
| `aws-access-key` | `AKIA` then exactly 16 of `A-Z0-9` |
| `github-token` | `ghp_`, `gho_`, `ghu_`, `ghs_` or `ghr_` then exactly 36 of `A-Za-z0-9`; or `github_pat_` then 22 or more of `A-Za-z0-9_` |
| `anthropic-key` | `sk-ant-` then 20 or more of `A-Za-z0-9_-` |
| `openai-key` | `sk-` then 20 or more of `A-Za-z0-9_-`, including at least one digit, when not `anthropic-key` |
| `slack-token` | `xoxa-`, `xoxb-`, `xoxp-`, `xoxr-` or `xoxs-` then 10 or more of `A-Za-z0-9-` |
| `private-key` | A `-----BEGIN ... PRIVATE KEY-----` line, or the PGP form `-----BEGIN PGP ... BLOCK-----` with `PRIVATE KEY` in place of `...` (PGP changed in review, main session, 2026-11-06) |
| `assigned-credential` | See below |

`assigned-credential`: a name ending, case-insensitively, in `password`, `passwd`, `secret`, `token`, `api_key` or `apikey`, at a word start or after `_`, `-` or `.`; then a quoted literal of 16 or more characters. The literal has no whitespace and no copy of its own opening quote (the other quote character may appear inside), doesn't start `http://` or `https://`, and doesn't contain `your`, `example`, `placeholder`, `changeme`, `replace`, `xxx`, `<` or `${`. The placeholder words match in lowercase only, as listed, so a random token holding `XXX` or `Example` is still detected: a false block is the safer failure (both changed in review, main session, 2026-11-06). Assignment is `=`, optionally after a type (`token: str = "..."`), or `:` where the name is quoted (JSON) or first on its line (YAML). An unquoted `:` form doesn't match if its literal holds only letters, `.`, `[`, `]` and `|`, because it may be a type annotation. Neither does a `:` inside a parameter list on one line.

## Out of scope
- Entropy checks, PII, a bypass flag ([open question 7](../design.md#4-open-questions)).
- The audit log's no-leak check (P1-S4).

## Notes
- CI forbids `.gitleaks.toml` and `.gitleaksignore` and ignores inline allows, so no realistic secret can be committed, even under `tests/`.
- P0-S1 AC17's check scans `src/` for `sk-` followed by a letter, digit, `_` or `-`. A pattern written `sk-[A-Za-z0-9_-]{20,}` passes, but a literal `sk-ant-` prefix in a pattern or comment fails it, so write that prefix some other way, such as a non-capturing group around `sk`.
- **Known false-positive cost:** `    token: "Token2ProviderClass",` exits 16. A digit-bearing type-like string in a multi-line parameter list can't be told from a real `password: "abc123...",`, and a false block is the safer failure for a secret check. There's no bypass under the current default ([open question 7](../design.md#4-open-questions)).
- With stderr closed or failing, P0-S3 AC4's settled rule applies: a delivered review exits by its findings, and any other failure, such as 16, keeps its own code.
- **Known limit (changed in review, main session, 2026-11-06):** the YAML form is anchored at line starts as split on `\n`, so a file with CR-only line endings reads as one line and its later YAML credentials aren't detected. The `=` and vendor forms are unaffected.
- **Implementation readings (developer), settled in review:** the marker rule, the `private-key` boundary, lowercase placeholders and the single-hit line are now in Interfaces (main session, 2026-11-06).
- **README (main session, done):** exit 16 is listed, and the Phase 1 pipeline starts with the secret scan on raw text, before the parser.
