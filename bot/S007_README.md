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
- `bot/s007_paper.py` — CLI runner (`--dry-run/--accounts/--check/--live`).
- `strategies/ger40_lonfra/` — the validated strategy engine + presets.
- Spec: `strategy-spec-S007.md` (project) / `GER40-london-frankfurt/algo/docs/`.
