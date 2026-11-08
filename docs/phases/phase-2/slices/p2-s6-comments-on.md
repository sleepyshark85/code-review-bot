# P2-S6: Turn comments on with one repository variable

- **Status:** Done
- **Depends on:** P2-S5, P2-S3
- **Touches:** `pr` (`github`: `RestGitHubClient`, `cli`, `runner`), `providers/claude_agent`, `.github/workflows/review.yml` (main session)

## Value
The owner switches this repository from shadow to commenting, and back, by setting `REVIEW_BOT_MODE`. It needs no code change, and each mode has the least privilege it needs.

## Acceptance criteria
AC2 to AC7 run `RestGitHubClient` against a local HTTP stub on `127.0.0.1`, never `api.github.com`. AC9 to AC11 parse the workflow. [Safety](../design.md#safety-on-the-pr-path).
- **AC1:** Given `--mode comment` with `GITHUB_TOKEN` set, then `run` uses `RestGitHubClient`. Given no `GITHUB_TOKEN`, then it exits 2 naming the variable, before the provider is called. This replaces P2-S5 AC15's interim exit.
- **AC2:** Given the stub's review comments over 3 pages, and review threads with who resolved them, then `list_bot_comments` returns only comments whose REST `user.login` is `github-actions[bot]` and `user.type` is `Bot`, with a valid marker. Each has its thread (found by comment ID), line (`None` when the stub says outdated), severity and resolver.
- **AC3:** Given `post_review` with 2 comments, then the stub receives one request creating a review with event `COMMENT`, the head SHA, and each comment's path, line, side `RIGHT` and body.
- **AC4:** Given `update_comment`, `resolve_thread` and `write_summary`, then the stub receives one edit, one thread-resolve mutation, and either a new summary comment or an edit of the bot's existing one.
- **AC5:** Given each of these responses: 401, 403, 404, 422, 500, a 403 with no rate limit remaining, 429, a timeout, a body that isn't JSON, or a body over 5 MB, then each raises the GitHub error (exit 18) with one line naming the operation and status, and nothing is retried.
- **AC6:** Given the token `ghs_canary`, then it is sent only in the `Authorization` header, and it appears in no error, stderr line, audit record or `shadow.json`.
- **AC7:** Given `GITHUB_API_URL` unset, then requests go to `https://api.github.com`. Given an `http://` URL whose host isn't `127.0.0.1` or `localhost`, then it exits 2 naming the variable.
- **AC8:** Given `GITHUB_TOKEN` in the environment, then the Claude provider's subprocess environment doesn't contain it, asserted over the fake SDK.
- **AC9:** The workflow has a comment job that runs only when `vars.REVIEW_BOT_MODE == 'comment'` and the head repository is this one. The shadow job still runs only when the variable is unset or `shadow`.
- **AC10:** The comment job's permissions are exactly `contents: read` and `pull-requests: write`. Its run step gets `GITHUB_TOKEN` and `ANTHROPIC_API_KEY` in its `env` and passes `--mode comment`, and no other step gets either. The action list in P2-S3's AC1, and its AC4 and AC6 to AC10, hold for this job too; the secret rule above replaces S3's AC5 here.
- **AC11:** The shadow job's permissions are still `contents: read` only.
- **AC12 (manual, recorded in the test report):** on a test PR, the user sets `REVIEW_BOT_MODE=comment` and sees inline comments and the summary. The user then pushes three changes, one at a time, and after each records whether GitHub marked the comment outdated and what the bot did:
  - **an edit to the line next to a commented one:** the comment must be kept or moved, never resolved or duplicated;
  - **an edit to the commented line itself:** the comment must be resolved, unless a finding pairs with it;
  - **lines inserted above it:** the comment must be kept or moved.

  Finally the user resets the variable. The stub can't reproduce GitHub's outdated logic, so what is observed here goes into the test report's known limits.

## Limits
List calls follow at most 10 pages of 100; more exits 18, naming the limit. Every request has a 30-second timeout. Responses are parsed with schemas: unknown fields are ignored, and missing ones exit 18.

## Interfaces
`GitHubClient`: [design](../design.md#interfaces). The token comes from `GITHUB_TOKEN` and the base URL from `GITHUB_API_URL`, as Actions sets them.

## Out of scope
- Re-reviewing only new commits (S7); reactions (S8).

## Notes
- **Main session:** add the comment job from this spec.
- **README (main session):** describe `REVIEW_BOT_MODE` (`off`, `shadow`, `comment`), and say that turning comments on is the owner's call.

Build choices (developer):
- `RestGitHubClient` lives in `pr/rest_github.py`, so the pure `plan` and `shadow` modules, which import `github`'s data types, never load `urllib`. It takes the repository at construction (REST edits need it), and `cli` builds it once the event is read; `GITHUB_TOKEN` and `GITHUB_API_URL` are checked before that.
- **Summary marker:** the client appends `<!-- review-bot:v1 summary reviewed=none -->` to every summary, and edits the bot's oldest issue comment whose last line matches `reviewed=\S+`. `none` isn't a SHA, so S7 (AC5) reviews the whole PR until it writes one. `read_summary` waits for S7. `shadow.json`'s `summary_body` has no marker.
- **Paging:** REST lists ask for `per_page=100` and stop at the first page that isn't full; a full 10th page exits 18, so exactly 1,000 items does too. Review threads page by GraphQL cursor, with the same cap.
- **Threads:** a comment's thread is the one whose first comment has its ID, read from GraphQL's `fullDatabaseId` (`databaseId` is 32-bit and deprecated). Its line is None when the thread's `isOutdated` is true. A resolved thread whose `resolvedBy` is null or `github-actions` counts as resolved by the bot (GraphQL's `resolvedBy` is a `User`); AC12 checks this on GitHub.
- **AC8:** the SDK lays `options.env` over the inherited environment and can't remove a variable, so `options.env` stays exactly as before (P0-S2 AC3). Instead every entry point (`review-bot`, each `review_bot.pr` subcommand, and `review_bot.evals`) first calls `settings.take_github_token`, which removes `GITHUB_TOKEN` from `os.environ` before any provider is built. Only `run --mode comment` keeps the value, for its client.
- Text from a response body (only GraphQL error messages are shown) has the token replaced by `[token]` and goes through `redact`, since GitHub may quote the request back.
- Redirects are refused (a 3xx exits 18), so the token never follows one. The 30-second timeout is urllib's per socket operation.
- `FakeGitHubClient` doesn't reject comments on lines outside the diff, as GitHub does with a 422: it holds no diff. A 422 still exits 18, naming `post_review`.
