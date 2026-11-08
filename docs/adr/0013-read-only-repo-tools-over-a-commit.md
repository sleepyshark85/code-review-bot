# 0013: Give the model three read-only tools of our own over one git commit, not the SDK's built-ins

- **Status:** Accepted
- **Date:** 2026-11-09
- **Phase:** 3

## Context
[README Phase 3](../../README.md#phase-3-context-aware) lets the model read the repository so findings can span files, through the SDK's `Read`, `Grep` and `Glob`. Those built-ins read a working tree, follow its symbolic links, run regular expressions, and return output whose size we can't bound. CI checks out only the base commit and fetches the head without checking it out ([ADR 0008](0008-pull-request-action-with-github-token.md)), and every tool result is hostile input that must not carry a secret to the vendor.

## Decision
The model gets three tools named `read`, `grep` and `glob`, doing the read-only subset of the built-ins, served by an in-process MCP server and implemented by us over the git objects of one commit, the PR's head. `grep` searches for a literal, always passed after `-e` with the commit followed by `--`, and its path filter is applied in our code, so no model text becomes a git option or pathspec. Paths are validated, `.git`, symbolic links, submodules and secrets files are refused, each blob is secret-scanned whole, every result is size-capped and delimited as untrusted, and the built-in tools are switched off. [Design](../phases/phase-3/design.md#tool-safety).

## Consequences
- Confinement holds by construction: no working tree, checkout, filter or hook, and no path outside the commit, on every platform.
- The tools run offline under `MockProvider`, so every safety rule is tested without a model.
- We maintain three small tools, and they are thinner than the built-ins, with no regular expressions and no files outside git.
- A local review reads a commit, so uncommitted edits are invisible to the model.

## Alternatives considered
- **SDK built-ins with a `PreToolUse` path check:** symbolic links, output size and regex cost stay outside our control, and CI would have to check out the head.
- **A checkout of the head in a temporary worktree:** runs checkout's filters and hooks, and still needs symbolic-link handling.
- **Regular-expression `grep`:** more expressive, but a model-written pattern can take exponential time.
