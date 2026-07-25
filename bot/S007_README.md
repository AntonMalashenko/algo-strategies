# S007 GER40 bot (cTrader) — paper/demo

Trades the frozen S007 base (GER40 London × Frankfurt) on cTrader, reusing the
S004 connection (`bot/ctrader.py`, `.env`). The strategy engine is the validated
`strategies/ger40_lonfra` package — the live position lifecycle is **identical to
the backtest by construction** (verified: bar-by-bar replay reproduces the backtest
entries/stops/target exactly).

## What it does

Each cycle it rebuilds the day's state from recent M1 bars and reconciles the
broker to the desired position set:
- Frankfurt range = 09:00–09:59 EET (the pre-open hour; DAX cash opens 10:00 EET).
- Entries A (0.5 break) / B (boundary breakout) from 10:00; direction fixed ("no flip").
- Pyramiding on 1M CHoCH up to 4 positions; **one common non-trailing stop = 0.5**
  attached to every order (server-side); take-profit = 100%/liquidity, also attached.
- Closes everything when the target is reached or at 16:59 (flat).
- Stateless: no long-lived process needed — schedule `--live` every 1 minute during
  the session (10:00–17:00 EET).

## Risk controls

Two signals guard the live bot beyond per-position sizing:

**Daily aggregate risk cap** (`DAILY_RISK_CAP_PCT` in `bot/s007_config.py`,
default 2% of current balance). `RISK_PCT` sizes each new entry/add
independently and does not look at what is already open — min-lot flooring
can push the REAL summed risk well past `max_positions x RISK_PCT`. Every
cycle, `decide()` (`bot/s007_paper.py`) sums the broker's own potential loss
across all open S007 positions — `|price - stopLoss| x volume`, read straight
from `ProtoOAReconcileReq`'s response (`CTraderS007._reconcile_step`), not our
nominal risk_amount — into `open_risk`. A new entry/add is skipped, logged as
a `skip_risk_cap` event (`open_risk`/`new_risk`/`risk_cap` fields), once
`open_risk + its own potential loss` would exceed `risk_cap`. `open_risk` and
`risk_cap` are also logged on every `state` event for visibility. This does
not force-close anything already open — it only blocks new entries.

**Manual stop-for-today** (kill switch, e.g. news event or discretionary
override):
```
python -m bot.s007_paper --stop-today     # close everything now, no new entries until tomorrow
python -m bot.s007_paper --resume-today   # cancel the stop early, same day
```
Backed by a control file, `reports/control/S007_STOP_TODAY`, holding today's
date — checked fresh every cycle (the bot is stateless). A stale file (an old
date left behind) is ignored automatically, no manual cleanup needed. While
active, `decide()` treats the rest of the day like `flat`: closes every open
S007 position (`reason=manual_stop`) and opens nothing new. The `STATUS` line
`--live` prints carries `manual_stop=True/False` for the scheduler.

The scheduler (`scripts/s007_tick.py`) treats `manual_stop` the same as
`day_done`/`filtered`: once seen, it logs `loop_settled` and stops running
full `--live` cycles for the rest of the session (heartbeat-only) — no point
polling every minute for an answer that cannot change until tomorrow.
`--resume-today` logs a `loop_resumed` event so the scheduler un-settles
(resumes running full cycles) if that happens later than the last
`loop_settled` — otherwise a resume mid-session would clear the stop file but
the scheduler would keep silently skipping cycles anyway.

## Setup

Credentials are the same `.env` as S004:
`CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET / CTRADER_ACCESS_TOKEN / CTRADER_ACCOUNT_ID`
(demo `ctidTraderAccountId`) `/ CTRADER_HOST` (default demo).

```
pip install ctrader-open-api python-dotenv pandas numpy
python -m bot.s007_paper --accounts     # confirm the DEMO account id
python -m bot.s007_paper --check         # auth + resolve GER40 symbol
python -m bot.s007_paper --dry-run --at "2024-05-10 10:45"   # offline sanity
```

Config in `bot/s007_config.py`: `PRESET` (BASELINE_S007 by default), `RISK_PCT`,
`SYMBOL_CANDIDATES` (GER40/DE40/…), `FIXED_LOT`.

## Run live (demo) — every minute in the session

Linux/mac cron (EET), 10:00–16:59:
```
* 10-16 * * 1-5  cd /path/to/algo && .venv/bin/python -m bot.s007_paper --live >> reports/paper_s007/live.log 2>&1
```
(cTrader Open API is a network API — a small Linux VPS is enough; no Windows/terminal needed.)

## First-run checklist (important)

The connection is reused from S004 and works; a few order-level details depend on
the SDK version and the broker — **verify the first live orders against the cTrader UI**:
1. `--check` resolves the right GER40 symbol name.
2. Place ONE manual-sized order via `--live` and confirm in the UI: side, that the
   **SL sits exactly at the 0.5 level** and TP at the target (absolute vs relative
   price handling can differ per broker — see `place_market` note).
3. Confirm `FIXED_LOT` size and, before switching to `RISK_PCT` sizing, calibrate the
   symbol's tick value (index contract size is broker-specific).
4. Watch one full session in demo and reconcile the fills against a same-day
   `--dry-run` printout — they should match.

## Logging (debugging)

Uses the reusable `utils/trade_logger.StrategyLogger` (shared across strategies).
Every cycle, position and order is logged under `reports/logs/S007/`:

    reports/logs/S007/
      S007.log                    # human-readable, rotating, all cycles (cycle id per line)
      events-YYYY-MM-DD.jsonl     # every structured event (parse/grep-able)
      cycles-YYYY-MM-DD.jsonl     # one snapshot per reconcile cycle (range, direction, ...)
      positions/<label>.jsonl     # EACH POSITION in its own file: desired/open/order/close

Cycle records carry the full day context (Frankfurt range rh/rl/mid, scenario, tp).
Order records carry the request AND the broker result/error, so a failed order or a
wrong SL/TP is fully reconstructable. To debug one trade, just read its position file.

Reuse in another strategy: `log = StrategyLogger("S00X"); log.cycle_start(...);
log.position(label, "open", ...); log.order(label, "place_market", request=..., result=/error=)`.

## Files
- `bot/s007_config.py` — config + credentials.
- `bot/s007_signals.py` — `plan_now()` desired-position set (reuses the engine).
- `bot/ctrader_s007.py` — cTrader adapter extension (M1 bars, market orders, close).
- `bot/s007_paper.py` — CLI runner (`--dry-run/--accounts/--check/--live/--stop-today/--resume-today`).
- `bot/risk.py` — `lots_for_risk()`, equal-dollar-risk position sizing.
- `strategies/ger40_lonfra/` — the validated strategy engine + presets.
- `scripts/s007_tick.py` — stateless scheduler tick, invoked every minute by
  launchd (`scripts/com.anton.algo.s007bot.plist`,
  `scripts/s007_loop_install.sh install|uninstall|status`) — see "Risk
  controls" above for how it consumes `day_done`/`filtered`/`manual_stop`.
- Spec: `strategy-spec-S007.md` (project) / `GER40-london-frankfurt/algo/docs/`.
