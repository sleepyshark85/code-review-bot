# 0008: Run as a `pull_request` Action with `GITHUB_TOKEN`, with only the diff taken from the PR

- **Status:** Accepted
- **Date:** 2026-11-08
- **Phase:** 2

## Context
[README Phase 2](../roadmap.md#phase-2-integrated) asks for a GitHub Action, then an App. The user decided on 2026-11-08 to dogfood on this repository with an Action first, using the workflow's own token, and to leave the App optional and for later. A pull request is hostile input, the job holds a model credential, and this repo's PRs change the bot itself, so whichever code reviews a PR must not come from that PR.

## Decision
The review runs in its own workflow on the `pull_request` event only, never `pull_request_target`, for PRs from this repository, never forks. It posts with `GITHUB_TOKEN`, with the narrowest permissions each mode needs. The bot's code, its lock file and its repo config come from the PR's base commit, and the PR contributes only its diff, read through git as data. The review job is never a required check. [Workflow rules](../phases/phase-2/design.md#safety-on-the-pr-path).

## Consequences
- No App to register, host or secure in Phase 2; comments appear as `github-actions[bot]`.
- A PR that changes the bot is reviewed by the bot on `main`, not by its own changes.
- Fork PRs get no review. This repo's contributors work on branches, so that loses nothing today.
- A PR can still edit the workflow file itself, which `pull_request` runs from the PR. Only people with write access can open same-repo PRs, and they could add any workflow anyway.
- Other workflows using `GITHUB_TOKEN` post as the same identity, so own comments are recognised by a marker as well as the author ([ADR 0009](0009-comment-state-on-the-pr.md)).
- Moving to an App later changes the identity and permissions, but not the plan or the comment state.

## Alternatives considered
- **GitHub App first:** a stable identity and finer permissions, but hosting, a private key and webhooks before the bot has shown any value.
- **`pull_request_target`:** gives secrets to fork PRs, and checking out their code under it is the classic way to leak them.
- **Run the PR's own copy of the bot:** dogfoods changes sooner, but lets any PR change what reviews it and what it does with the key.
- **Fetch the diff from the API:** avoids git, but GitHub truncates large PR diffs, and the base checkout is needed anyway.
