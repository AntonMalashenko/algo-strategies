---
name: strategy-modifiers
description: >-
  How to manage strategy modifiers (rule variants / presets), keep the frozen base version
  protected, keep the documentation in sync, and always keep the most profitable
  configurations surfaced and ranked. Use this skill whenever the task is to add, test,
  name, compare, promote, or retire a strategy modifier or preset; to protect or reproduce
  the baseline; to update the registry / passport / config docstrings after a variant lands;
  or to answer "which config is best / most profitable / best for prop" — even if the
  request only says "test this modification", "base untouched", "add a preset", "which
  setup wins", "is the new rule better", or "keep the base version". Read this before
  editing any strategy config or preset so the baseline stays frozen, every variant lands
  as a default-off flag with its verdict recorded, and the current champion configs stay
  clearly marked instead of getting lost among experiments.
---

# strategy-modifiers — variants, base protection, docs, and the profit leaderboard

This is the operating discipline for evolving a strategy without breaking it. It sits below
`strategy-lifecycle` (the end-to-end flow) and focuses on four things the maintainer cares
about repeatedly: **modifiers, a protected base, synced docs, and a clear pointer to the
most profitable configs.** Logging is the `strategy-logging` skill; the runner/UI is
`webapp`.

## 1. Modifiers are default-off flags + named presets — never engine edits

A "modifier" is any rule change on top of the baseline (a day filter, an alternative stop,
a reversal model, an add-validation rule). Encode every one the same way:

- **Add a tunable to the config dataclass, defaulting to the base behavior** (`False` / `0.0`
  / `None`). The engine reads it; the engine's default behavior never changes.
- **Express the variant as a named preset built with `.with_(...)`** off the baseline —
  never a second copy of the engine, never a mutated baseline. Example shape:

  ```python
  FILTERED_S007 = BASELINE_S007.with_(max_height=100.0)              # one modifier
  WORKING_S007  = BASELINE_S007.with_(max_height=100.0, b_reversal_to_A=True)  # stacked
  ```

- **A modifier is one flag doing one thing.** Stack modifiers by stacking `.with_()` args,
  so any combination is a preset and each flag can be toggled independently in a backtest.

This is what makes "база не трогаем" (base untouched) literally true: adding a modifier
cannot change what the baseline does, because the baseline preset simply doesn't set the
flag.

## 2. Protect the base version

The frozen baseline (`BASELINE_S007`) is the reference every variant is measured against.

- **Never edit the baseline preset or the engine defaults** to chase a better number. If a
  variant is better, it gets its own preset; the baseline stays put so comparisons remain
  honest across sessions and dates.
- **Pin exact reproductions as regression presets** (`REF_*`, guard-free) so any refactor
  can be proven byte-for-byte identical to the historical reference result (S007 reproduces
  `pyramid_duka.csv` / `pyramid_liq_duka.csv` to |diff| ≈ 1e-14). Run the regression check
  after any engine change before trusting new numbers.
- **The one allowed exception is a bugfix, and it's labeled as such** — e.g. the S007
  min-risk guard that removes a near-zero-risk R-explosion artifact. A bugfix corrects a
  measurement error; it is documented as a bugfix (not a rule change) and kept out of the
  `REF_*` regression presets so they still reproduce history exactly. If you can't honestly
  call a change a bugfix, it's a modifier — give it a flag.

## 3. Record every modifier's verdict in the code

A tested modifier that loses is as valuable as one that wins, but only if the verdict is
captured where the next person will see it: **in the flag's own comment, with numbers and a
date.**

```
# skip_A_entry_reaches_boundary:
# TESTED 2026-07-16 and REJECTED — those 37 A-days average +1.28R (78% win);
# enabling cuts A expectancy +0.53->+0.41R, worst-year +0.19->+0.06, deepens DD
# -40->-49R. Kept as a documented off-by-default option.
```

This stops a rejected idea from being silently re-litigated later. Winners get the same
treatment on their preset (why it's recommended, at what sizing).

## 4. Keep the documentation in sync — same task, not "later"

A modifier isn't done when the backtest prints a number; it's done when the docs reflect it.
The docs live in the AlgoTrading Claude **Project** (use the `Projects` tool —
`project_read` / `project_write` — not the local filesystem):

- `strategies-registry.md` — update the strategy's **best result** cell and `updated` date;
  the registry is the single source of truth for status and headline result.
- `strategy-passport-SXXX.md` — add/adjust the preset list and the gate results; bump the
  passport version (e.g. "Версия 0.7"). The passport §on presets should always name the
  current recommended config.
- `backtest-log.md` — the run and its metrics; `experiments-log.md` — the design/data
  choice as a numbered `E#`; `decisions-log.md` — any decision taken with the maintainer,
  dated.
- The **config docstring** in the repo (English) mirrors the same preset story so a reader
  of the code alone understands which preset is the baseline, which are experiments, and
  which are regression-only.

If you change code and skip the docs (or vice-versa), the two tracks drift and the next
session can't trust either. Update both in the same task.

## 5. Always surface the most profitable configurations

Experiments accumulate; the point of them is a small set of configs worth actually running.
Keep that set **explicitly ranked and labeled** so it never gets buried:

- **Name the champion.** There is a current best config for raw net expectancy (for S007:
  `WORKING_S007` = height filter + B-reversal→A, net ≈ +0.571R, all years positive). Its
  preset name, its docstring comment, and the passport should all say "recommended".
- **Rank on more than one axis — profit *and* drawdown/robustness.** The highest-expectancy
  config is not always the deployable one. `WORKING_S007_V2` (+ "meaningful CHoCH" add
  validation) trades a little expectancy for much lower drawdown (−40→−26R) and the best
  worst-year (+0.35) — so it's the preferred config for prop/drawdown-sensitive sizing.
  Say which config wins on which axis.
- **Tie the config to its deployment reality.** Note the sizing/context that makes the
  profitable config actually usable (S007: 0.25–0.33%/R for a −3%/−10% prop firm; the
  reversal helps average but worsens the daily tail, so use the base without reversal on a
  hard-daily-limit challenge). A "most profitable" number without its sizing/gate context is
  misleading.
- **Keep the leaderboard current.** When a new modifier beats the champion on its axis,
  promote it (rename/annotate the preset, update registry best-result + passport). When
  nothing beats the baseline, say so and keep the baseline as the recommendation — a
  variant that isn't clearly better is not an improvement.

## Quick loop for "test this modifier"

1. Add a default-off flag to the config; leave the baseline preset untouched.
2. Build a named `.with_()` preset for the variant (and stacked variants if relevant).
3. Backtest the variant against `BASELINE_S007` on equal footing — same gates, same real
   costs (see `strategy-lifecycle` for the gate ladder). Re-run the `REF_*` regression if
   the engine changed at all.
4. Winner → name it, mark it recommended, update registry best-result + passport + config
   docstring, and place it correctly on the profit/drawdown leaderboard.
   Loser → bake the verdict (numbers + date) into the flag's comment and move on.
5. Log the run in `backtest-log.md`, the choice as an `E#` in `experiments-log.md`, and any
   maintainer decision in `decisions-log.md`.

## Conventions

In-code artifacts (identifiers, comments, docstrings, commit messages, in-repo docs) are
**English**; chat with the maintainer is Russian (`AGENTS.md`). Archive, don't delete —
rejected modifiers stay as documented off-by-default flags, not removed code. Prefer honest,
quantified, dated claims over optimistic summaries.
