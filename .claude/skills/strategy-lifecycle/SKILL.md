---
name: strategy-lifecycle
description: >-
  The end-to-end workflow and conventions for creating, researching, validating, and
  managing trading strategies in this project — the two-track system (code in the repo,
  research narrative in the AlgoTrading Claude Project), the status lifecycle, the
  parametric-config code pattern, and the validation gates. Use this skill whenever the
  task is to add a new strategy, prototype or backtest one, propose or test a rule
  variant, promote/archive a strategy, update the registry / passport / spec / logs, or
  decide whether an edge is real — even if the request only says "new strategy idea",
  "let's test this modification", "backtest it", "is there an edge", "walk-forward",
  "add a preset", or "update the passport". Read this before touching strategy code or
  the strategy docs so a new strategy lands in the same shape as S004/S005/S007 instead
  of a one-off, and so the base-untouched / archive-don't-delete discipline is preserved.
---

# strategy-lifecycle — creating & managing trading strategies

Strategies here live on **two tracks that must stay in sync**:

1. **Code — in the repo** (`strategies/`, `backtest/`). The runnable engine, presets, and
   backtest scripts. English only (see `AGENTS.md`).
2. **Research narrative — in the AlgoTrading Claude Project** (the `claude/*.md` docs, read
   and written with the `Projects` tool — `project_read` / `project_search` /
   `project_write`, *not* the local filesystem). Status, rules, results, decisions, honest
   caveats. Russian is fine here; this is the maintainer-facing story.

A strategy that has code but no registry row, or a passport but no reproducible backtest,
is only half-done. When you change one track, update the other in the same task.

## Where each thing lives

Project docs (`Projects` tool):

- `strategies-registry.md` — **single source of truth for status.** One row per strategy:
  `ID | name | market | timeframe | type | status | best result | file | updated`, plus a
  prose description block per strategy below the table. Every new strategy starts as a row.
- `strategy-passport-SXXX.md` — the full spec of one strategy: exact rules, decisions with
  dates, architecture pointer, gate results, risk limits, stop criteria, and **honest
  caveats**. This is the document you'd hand someone to run or audit the strategy. Created
  once a strategy is worth validating; versioned (e.g. "Версия 0.7").
- `strategy-spec-SXXX.md` — the formalized rule spec (the bridge from a discretionary/video
  idea to mechanical rules) when one is kept separately from the passport.
- `backtest-log.md` — per-run backtest details and metrics.
- `experiments-log.md` — numbered experiments `E0, E1, ...` (data choices, engine changes,
  robustness probes) with what each tested and concluded.
- `decisions-log.md` — decisions made with the maintainer (Anton), dated.
- `roadmap.md` — what's next across strategies.

Repo (filesystem / device):

- `strategies/<name>.py` for a single-file strategy (`donchian.py`, `fvg_mtf.py`,
  `fx_carry.py`), or a package `strategies/<pkg>/` when the engine grows (see
  `ger40_lonfra/` for S007 — `config/data/structure/setups/engine`).
- `backtest/run_<name>.py` and any `walkforward` / `gate2_costs` / `prop_sim` scripts.
- `docs/EXPERIMENTS.md`, `docs/STRATEGY_S004.md` — in-repo technical docs where kept.

## Status lifecycle

```
idea → prototype → backtested → validated → paper → live → archived
```

`archived` is a first-class outcome, not deletion — **a negative result is a result**
(S001/S002/S004i/S006 are archived with *why*). Never delete a strategy's row or passport;
move it to `archived` and record what killed the edge. This is how the project avoids
re-testing dead ideas. IDs are `S001, S002, ...` assigned in order; a variant on the same
core can take a suffix (`S004i` = S004 on indices).

## The code pattern: one parametric engine, presets as configs

The S007 engine is the reference shape and the pattern to copy for any strategy with
tunables and variants:

- **A frozen `@dataclass` config is the single source of truth** for every tunable
  (`StrategyConfig` in `ger40_lonfra/config.py`). The engine is a pure state machine that
  reads the config — no magic numbers in the engine.
- **Variants are presets, built with `.with_(...)`**, never forks of the engine. The chosen
  frozen baseline is one named preset (`BASELINE_S007`); every experiment is another
  (`FILTERED_S007`, `WORKING_S007`, ...). "Base untouched" is literal: a new rule is a new
  default-off flag, and the baseline preset does not set it.
- **Off-by-default experimental flags carry their verdict in the code comment.** When a rule
  is tested and rejected, keep the flag but document *why* it lost, with numbers and date
  (see `skip_A_entry_reaches_boundary`: "TESTED 2026-07-16 and REJECTED ... cuts expectancy
  +0.53→+0.41R"). This stops the same idea from being re-litigated.
- **Regression presets pin exact reproductions** of any prior reference result
  (`REF_*`, guard-free) so a refactor can be proven byte-for-byte identical (S007 engine
  reproduces `pyramid_duka.csv` to |diff| ≈ 1e-14).
- **No look-ahead, ever**, and prove it (slice off the future, assert max|Δ| = 0). R-based
  accounting: one position = 1R, a day/trade sums R.

For a brand-new strategy without heavy parametrization, a single `strategies/<name>.py`
with a clear config block at top is fine — but the moment it grows variants, refactor to
the config-dataclass pattern rather than copy-pasting the engine.

## Validation gates (promotion criteria)

A strategy earns `validated` only by surviving, in order (S007's passport §4–6 is the
worked example):

1. **Gate 0 — regression / no-look-ahead.** The engine reproduces any reference it claims
   to, and provably doesn't peek at the future.
2. **Gate 1 — walk-forward.** Positive across *every* year and on a true out-of-sample tail
   the config never touched. An edge that lives in one lucky year is not an edge (this is
   exactly what failed S006: IS +0.153 → OOS −0.066).
3. **Gate 2 — costs.** Re-run at the *real* measured spread/commission, not gross. The edge
   must survive (S007: +0.567R gross → +0.415R net at 1.27pt spread). State the death
   threshold.
4. **Gate 3 — deployment reality.** For prop-firm accounts, simulate the firm's rules
   (daily loss, max drawdown, payout) and report cash-out vs bust rates and the right
   sizing. A strategy can pass Gate 1–2 and still be unusable at a given risk % (S007 at
   0.5%/R busts 43% on the daily limit; 0.25–0.33% fixes it).

**Reserve a true OOS slice and do not look at it** while tuning — it's the honest test kept
untouched until a promotion decision (done for S004 and S005).

## Adding a new strategy — checklist

1. `project_read` `strategies-registry.md`; pick the next `SXXX`; add a row with status
   `idea`/`prototype` and a one-line description, and a description block below. `project_write` it back.
2. Formalize the rules (a `strategy-spec` / passport draft) if the idea comes from a video
   or discretionary source — separate the mechanical, testable rules from the parts that
   are "feel" and say which you dropped and why.
3. Write the code in the repo: `strategies/<name>.py` (or a package), config block up top,
   `backtest/run_<name>.py`. English, per `AGENTS.md`.
4. Run the gates in order; log each meaningful run in `backtest-log.md` and each
   design/data choice as an `E#` in `experiments-log.md`.
5. Update the registry row's status + best result, and the passport, as evidence lands.
   Record maintainer decisions in `decisions-log.md` with the date.
6. Keep the honest caveats section current — single instrument, single source, short
   sample, smooth equity, martingale profile, etc. Under-claiming is the house style.

## Modifying / testing a variant of an existing strategy

Add a default-off config flag + a named preset; **never edit the baseline preset or the
engine's default behavior**. Backtest the variant against the baseline on equal footing
(same gates, same costs). If it wins, name it (`WORKING_S007`) and note it in the passport;
if it loses, bake the verdict into the flag's comment and move on. When the maintainer says
"base untouched" (база не трогаем), this is the mechanism that honors it.

## Conventions that apply throughout

- In-code artifacts (identifiers, comments, docstrings, commit messages, in-repo docs,
  logs) are **English**; chat with the maintainer is Russian (`AGENTS.md`).
- Archive, don't delete. Every strategy — dead or alive — keeps its row and its "why".
- Prefer honest, quantified caveats over optimistic summaries; state sample size, source,
  and the conditions under which the edge dies.
- Logging for any bot goes through `StrategyLogger` (see the `strategy-logging` skill); the
  multi-account runner / UI is the `webapp` skill. This skill is the research-and-lifecycle
  layer above both.
