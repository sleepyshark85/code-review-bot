# 0017: Domain rules are named checks from a built-in catalogue, switched on per path in `.review-bot.toml`

- **Status:** Proposed
- **Date:** 2026-11-09
- **Phase:** 3

## Context
[README Phase 3](../../README.md#phase-3-context-aware) asks for RightShip domain rules, such as `Decimal` money in `billing/`, and the README makes domain rules deterministic. Phase 2's per-repo config ([P2-S9](../phases/phase-2/slices/p2-s9-repo-config.md)) is read from the base commit and forbids unknown keys. Its format is a public interface that repositories commit, so it is costly to change.

## Decision
`.review-bot.toml` gains a list of domain rules, each naming a rule from a catalogue built into the bot, the path patterns it applies to, and an optional severity. Phase 3's catalogue holds one rule, `decimal-money`. Enabled rules run with the other rules, with no model, in both modes, and in context mode their fixed descriptions are also given to the model. [Spec](../phases/phase-3/slices/p3-s10-domain-rules.md).

## Consequences
- Each rule is reviewed, tested and explainable code, with stable rule IDs for fingerprints, dedup and labels.
- A repository can't add a rule without a bot release.
- The model's guidance is fixed text, so config can't carry instructions to it.

## Alternatives considered
- **Regular expressions in config:** any repository could write a pattern with exponential cost, and matches on raw text can't tell code from comments or strings.
- **Free-text guidance for the model:** not deterministic, and config text reaching the prompt is a new injection path.
- **Rules as plugins:** a plugin system for one rule.
