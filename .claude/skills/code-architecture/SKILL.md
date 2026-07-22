---
name: code-architecture
description: >-
  The architecture and clean-code rules for the bot codebase — the shared base layer
  (broker adapter, logger, data/metrics utilities, config) that every strategy reuses, the
  no-magic-values / named-constants rule, and the governance rule that any skill documenting
  code you change must be updated too, but ONLY with the maintainer's explicit consent. Use
  this skill whenever the task adds or refactors bot code, wires a new strategy into the
  shared engines, touches the cTrader adapter / utils / config, introduces a numeric literal
  or threshold, or when a code change overlaps what another skill documents — even if the
  request only says "clean this up", "add a broker method", "refactor", "share this between
  strategies", or "hardcode this value". Read this before writing bot code so shared logic
  stays single-source-of-truth, constants stay named, and no skill silently drifts out of
  sync with the code without the maintainer approving it.
---

# code-architecture — clean code, shared engines, constants, and skill governance

This skill owns *how the bot code is structured and changed*. Four rules the maintainer
cares about: reuse the shared base, keep the code clean, never hardcode magic values, and
never let a skill drift from the code it documents without his say-so.

## 1. Layered architecture — shared base, per-strategy leaves

The codebase is a **common foundation reused by every strategy**, with strategy-specific
logic layered on top. Do not fork or copy a base engine per strategy; extend or compose it.

- `bot/` — the shared trading runtime:
  - `ctrader.py` — `CTraderAdapter`, the cTrader Open API base (connect / auth / trendbars /
    orders / reconcile). **All strategies reuse this.** S007's `ctrader_s007.py` does
    `class CTraderS007(CTraderAdapter)` and only adds S007-specific symbol/signal glue — the
    connection, auth, and order plumbing are inherited, not re-written.
  - `risk.py` — `lots_for_risk(...)`, equal-dollar-risk position sizing for lot/point-based
    (cTrader CFD/FX) instruments. Pure math, no broker I/O: the caller (a strategy's `decide`)
    fetches `balance` and `money_per_point_per_lot` once per cycle via the adapter (see
    `CTraderAdapter._get_balance_step`, `CTraderS007.run_live_cycle`) and passes them in. Added
    for S007 (2026-07-21); any future cTrader strategy sizing by dollar risk reuses this, not a
    private copy.
  - `config.py` — shared bot config / `.env` credential loading (never read or log secret
    *values*; refer to them by variable name).
  - `signals.py` / `paper.py` — base signal + paper-trading scaffolding; a strategy adds its
    own thin variant (`s007_signals.py`, `s007_paper.py`) that calls into the shared pieces.
- `utils/` — shared, strategy-agnostic helpers used across the project:
  - `trade_logger.py` — `StrategyLogger` (see the `strategy-logging` skill). Every bot logs
    through it; no strategy invents its own logging.
  - `data.py`, `metrics.py`, `report.py` — data loading, R/expectancy metrics, reporting.
    A new strategy computes its stats with these, not with a private copy.
- `strategies/` — per-strategy logic: a single `strategies/<name>.py`, or a package like
  `ger40_lonfra/` (`config/data/structure/setups/engine`) once it grows variants.
- `backtest/` — per-strategy backtest/validation scripts on top of the shared utils.

**The test before writing new code:** "does a base engine or util already do this?" If two
strategies would need the same thing, it belongs in `bot/` or `utils/` as one implementation
they both call — not copy-pasted. If you find yourself duplicating a base engine to tweak
it, subclass it or add a config flag instead (the modifier discipline is in the
`strategy-modifiers` skill).

## 2. Clean code

- **Single source of truth.** One implementation per concept; shared logic lives in the base
  layer and is imported, never duplicated.
- **Engines are pure state machines that read config.** Behavior comes from the config
  dataclass, not from branches buried in the engine. Keep functions small and single-purpose
  (`pick_stop`, `liquidity_tp`, `find_setup`, `structure_levels` are the reference grain).
- **Clear module boundaries.** Broker I/O in `bot/`, strategy math in `strategies/`, cross-
  cutting helpers in `utils/`. Don't put broker calls in strategy math or strategy math in
  the adapter.
- **Prove behavior-preserving refactors.** After any change to a shared engine, re-run the
  regression checks (the `REF_*` presets / no-look-ahead assertions) so "cleanup" can't
  silently change results.
- **English for all in-code artifacts** (identifiers, comments, docstrings, commit
  messages); Russian only in chat (`AGENTS.md`).

## 3. No magic values — always named constants

Every meaningful number, threshold, window, or string enum gets a **name**, defined once,
close to where it's configured — never a bare literal in the middle of logic.

- **Strategy tunables** (risk %, caps, filter thresholds, session windows, k) live in the
  strategy's **config dataclass** with a comment explaining the unit and meaning — that is
  the project's constants home for anything a backtest might vary. A literal like `100.0` in
  engine code is a bug; it should be `cfg.max_height`.
- **Fixed technical constants** (price scale, timeouts, retry counts, protocol periods, safe
  filename patterns) get a module-level `UPPER_SNAKE_CASE` constant with a comment, e.g.
  `PRICE_SCALE = 100_000  # cTrader price = raw / 1e5`. Reuse the constant everywhere; don't
  restate the literal.
- **String modes are named too** (`stop_mode="mid_range"`, `tp_mode="liquidity"`): the set of
  valid values is documented on the field, and the engine dispatches on the name.

Rationale: a named constant is self-documenting, greppable, changeable in one place, and
comparable across sessions. A magic literal is an undocumented decision that rots. If you
must introduce a number, name it in the same edit.

## 4. Skill ↔ code governance — update the skill, but only with consent

Several skills document specific parts of this codebase. Keeping them accurate matters, but
**changing a skill without the maintainer's knowledge creates exactly the chaos this rule
exists to prevent.** So both halves are mandatory:

- **When you change code that a skill documents, the skill must be brought back in sync** —
  otherwise the next session reads stale guidance.
- **You may not edit a SKILL.md without Anton's explicit consent.** Propose the skill update
  (say which skill, what changed in the code, and the exact edit you'd make), then wait for
  his approval before writing it. Never silently rewrite, add, or delete a skill.

Ownership map (which skill to flag when its code changes):

| Skill | Code / area it documents |
|-------|--------------------------|
| `code-architecture` | `bot/` and `utils/` shared base, overall structure, constants rule, this governance rule |
| `strategy-logging` | `utils/trade_logger.py` (`StrategyLogger`) and the `reports/logs/` layout |
| `webapp` | `webapp/` (UI, models, crypto, security, runner, cli) |
| `strategy-lifecycle` | the research flow, `strategies/`, `backtest/`, and the Project docs |
| `strategy-modifiers` | strategy config/preset discipline, base protection, the profit leaderboard |

Workflow when a change lands in an owned area: (1) make the code change; (2) identify the
affected skill(s) from the map; (3) tell Anton the skill is now out of date and propose the
specific update; (4) apply the skill edit **only after he agrees.** If he declines or defers,
leave the skill as-is and note the drift so it isn't forgotten. This same consent rule
applies to editing *this* skill.

## Quick loop for "add / change bot code"

1. Check whether a base engine or util already covers it; if two strategies would share it,
   put it in `bot/` or `utils/` once, and have both call it.
2. Extend the shared base by subclass/compose or a config flag — don't fork it.
3. Name every constant (tunables → config dataclass; technical → module `UPPER_SNAKE_CASE`);
   no bare literals in logic.
4. Keep boundaries clean and functions small; re-run regression/no-look-ahead after touching
   a shared engine.
5. If the change touches an area a skill owns, propose the skill update to Anton and apply it
   only with his consent.
