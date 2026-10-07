# mt5/ — MetaTrader 5 expert advisors

MQL5 execution layer for strategies whose prop firms only (or best) support MT5:
FundingPips $10K, The5ers, and FundedNext with the EA add-on. The Python bot in
`bot/` keeps running the cTrader accounts. Strategy **rules** are never
re-decided here. Every rule constant is generated from the Python source of
truth, and parity with the backtest engine is checked on the broker's own bars.

Ticket: ALGODEV-61. Design and spec: Claude Project doc
`claude/prompt-s021-mt5-ea-implementation.md`.

**Installation, setup and daily use on macOS, Windows and Linux: [`docs/SETUP.md`](docs/SETUP.md).**
Day to day: `mt5/tools/deploy.sh` (or `--watch` to redeploy on every save);
on Windows `mt5\tools\deploy.ps1`.

## Layout

The tree under `MQL5/` mirrors a terminal's `MQL5/` data folder, so it can be
linked into the terminal as is (`tools/install_mac.sh`).

```
mt5/
  MQL5/
    Include/
      AlgoCore/                  shared, strategy-agnostic (the MQL5 counterpart of bot/ + utils/)
        Clock.mqh                server time <-> UTC <-> strategy clocks (explicit tz rules)
        JsonLog.mqh              JSONL logs, record-compatible with utils/trade_logger.py
        Sessions.mqh             daily sessions from M1 + causal average range (engine.py port)
        Sizing.mqh               lots_for_risk + volume/price normalisation (bot/risk.py port)
        AccountGuard.mqh         account daily/max loss guard (bot/account_guard.py port)
        TradeOps.mqh             own positions/orders snapshot, place stop / cancel / close
        InstanceLock.mqh         one running copy per (login, symbol, magic)
        GeneratedCore.mqh        GENERATED constants from bot/risk.py, bot/account_guard.py
      Strategies/
        S021_ORB/
          Params.mqh             GENERATED from strategies/orb_intraday/config.py::ORB_BASE
          Levels.mqh             S021 levels: O, ADR14, U/L, stop -- pure functions
          Runtime.mqh            S021 state machine (decide() cases 1-7, event driven)
        S004_FVG/
          Params.mqh             GENERATED from strategies/s004_config.py::S004_INTRADAY
          Engine.mqh             S004 engine: port of strategies/fvg_mtf.py::run_backtest,
                                 pure (no orders, no account) so it can be diffed bar for bar
    Experts/AlgoTrading/
      S021_ORB.mq5               thin EA shell: inputs + event wiring
    Scripts/AlgoTrading/
      S021_SelfTest.mq5          runs the pure pieces against Python-made fixtures
      S004_SelfTest.mq5          replays Python-made fixtures through S004 Engine.mqh and
                                 compares every trade (entry/SL/TP/exit/reason/R)
      S021_PlaceToday.mq5        manual assist: today's levels/lot now, optional hand placement
                                 of the pair with the EA's magic (it then manages them)
      ExportM1.mq5               dumps the broker's M1 bars + symbol spec (any strategy)
      ImportM1CustomSymbol.mq5   builds an offline custom symbol from an M1 CSV (tester runs
                                 on histdata or on a saved broker export -- any strategy)
    Presets/AlgoTrading/*.set    EA input presets (offline tester, FundingPips eval/master, FTMO)
    Files/AlgoTrading/           GENERATED self-test fixtures + tester history (git-ignored)
  tools/                         Python side (run from the repo root: python -m mt5.tools.<name>)
    gen_params.py                writes the GENERATED headers; --check for CI
    clock.py                     the same tz rules as Clock.mqh (tested against zoneinfo)
    s021_fixtures.py             self-test fixtures from histdata + the engine
    s021_parity.py               engine vs EA on the broker's own bars
    s004_clock_probe.py          measures which timezone the S004 data is stamped in
    s004_fixtures.py             S004 self-test fixtures: the engine's bars + its trades
    run_selftest.sh              runs a *_SelfTest script headlessly in a throwaway terminal
    deploy.sh / deploy.ps1       one-command deploy: generate, test, copy, compile (--watch / -Watch)
    install_mac.sh               copy (default) or link sources/presets/fixtures into a terminal
tests/mt5/                       pytest for the tools (repo testpaths = tests/)
```

## Rules of the road

- **Never hand-edit a strategy number in MQL5.** Change the Python config, then
  run `python -m mt5.tools.gen_params`. `tests/mt5/test_gen_params.py` fails on
  a stale header.
- **The EA implements a frozen base only.** `gen_params` refuses to export a
  config with any modifier field off its default (breakeven, reversal, trail…).
  A modifier must first be implemented in the EA, then added to that strategy's
  `*_EA_FIELDS` whitelist.
- **Runtime and firm settings are EA inputs; strategy rules are not.** Examples
  of inputs: risk %, account guard, server timezone rule, the firm's day
  boundary, an early forced exit for a firm's auto-close window.
- **Decisions run on UTC-derived clocks, never on raw server time.** The server
  timezone is an explicit rule (`ENUM_TZ_RULE`), because the Strategy Tester has
  no real `TimeGMT()` and past DST cannot be observed. Live, the EA compares the
  rule with the terminal's actual offset and blocks new entries on a mismatch.
- **The strategy's own session clock is generated, not hand-written.**
  `mt5.tools.gen_params` reads `engine.SESSION_TZ` and emits the matching
  `ENUM_TZ_RULE` into `Params.mqh`, so the EA and the backtest cannot drift apart.
  S021 is anchored on `America/New_York`: the 09:30 open is 13:30 UTC under US DST
  and 14:30 UTC in standard time. A fixed offset here is a bug, not a shortcut --
  it silently moves the anchor an hour for half the year (see ALGODEV-61).
- **State is rebuilt from the broker on every reconcile.** Positions, orders and
  history are read using the magic number and the day encoded in the order
  comment and open time. A restart therefore never re-enters a day and never
  loses a time exit. In-memory state only de-duplicates logs.
- **Logs** go to `<Common>/Files/AlgoTrading/logs/<strategy>-mt5-acct<login>/`:
  `events-YYYY-MM-DD.jsonl`, `positions/<label>.jsonl`, `heartbeat.json`, and
  `<strategy>_days.csv` (the parity input). Labels match the Python bot's
  (`S021:YYYY-MM-DD:long`).
- English everywhere in code and docs (AGENTS.md). Named constants, no magic
  numbers (code-architecture).

## Adding a strategy

1. **Shared logic.** Anything that is not specific to the strategy (sessions,
   sizing, clocks, trade plumbing) goes in `Include/AlgoCore/`. Never copy it
   into the strategy folder.
2. **`Include/Strategies/<Sxxx_Name>/`:**
   - `Params.mqh`: add a `GeneratedHeader` plus a field whitelist to
     `tools/gen_params.py`;
   - pure rule functions (like `Levels.mqh`);
   - a `Runtime.mqh` that mirrors the Python bot's `decide()`.
3. **`Experts/AlgoTrading/<Sxxx_Name>.mq5`:** inputs plus event wiring only.
4. **Tests:**
   - a `Scripts/AlgoTrading/<Sxxx>_SelfTest.mq5` with fixtures from
     `tools/<sxxx>_fixtures.py`;
   - a `tools/<sxxx>_parity.py`;
   - pytest under `tests/mt5/`.
5. **Housekeeping:** add the new folders to `tools/install_mac.sh` (only if they
   live outside the existing roots) and record the change in
   `.claude/change-log/mt5.jsonl`.

## S021 workflow (summary -- full version in docs/SETUP.md)

```bash
# 1. regenerate headers + fixtures (repo root)
python -m mt5.tools.gen_params
python -m mt5.tools.s021_fixtures          # default window 2025-09..12, rule EET_US_DST

# 2. deploy + compile (macOS/Linux; Windows: mt5\tools\deploy.ps1)
mt5/tools/deploy.sh

# 3. in the terminal
#    - Scripts/AlgoTrading/S021_SelfTest -> Experts log "S021 self-test: N passed, 0 failed"
#    - Scripts/AlgoTrading/ExportM1 on the US100 chart (also dumps the symbol spec)
#    - Strategy Tester: Experts/AlgoTrading/S021_ORB, US100, "Every tick based on real ticks",
#      ServerTzRule = the broker's rule (EET_US_DST for most prop servers -- check the spec dump)

#    - offline alternative (no broker data needed): ImportM1CustomSymbol with
#      InpCsvPath=AlgoTrading/e2e/s021_m1.csv (python -m mt5.tools.s021_fixtures
#      --start 2025-06-01 --end 2025-12-31 --out mt5/MQL5/Files/AlgoTrading/e2e), then the
#      tester on the custom symbol NSXUSD_HD, model "1 minute OHLC", InpTimerSeconds=60.
#      After every re-import delete <terminal>/Tester/{bases,cache}: the tester replays its
#      own snapshot of the symbol, not the symbol (log must show ticks ~= 4 x bars).
#      The tester still needs the terminal to be logged in to some account.

# 4. parity on the broker's own bars
python -m mt5.tools.s021_parity \
  --bars  "<Common>/Files/AlgoTrading/exports/US100_M1_<server>.csv" \
  --ea-days "<Common>/Files/AlgoTrading/logs/S021-mt5-acct<login>/S021-mt5-acct<login>_days.csv" \
  --rule EET_US_DST --out reports/s021_mt5_parity.csv
```

### Live checklist (prop account)

- **One chart per symbol**, Algo Trading enabled, EA inputs:
  - `RiskPct`;
  - account guard: `InitialBalance`, `DailyGuardPct`, `MaxGuardPct`,
    `DayResetRule`;
  - `ServerTzRule`;
  - FundingPips summer: `ForceExitUtc = 20:44`, because their auto-close runs
    20:45–21:00 UTC.
- **Check the `init` event** in the events log: symbol spec, `money_per_point_per_lot`,
  min/step volume, the observed and expected server offset, and `margin_mode`.
  The hedging account must not end up with two opposite positions.
- **Keep the terminal running on this Mac.** Prop rules forbid VPS/VPN. Disable
  sleep (`pmset`/Energy settings) and add the terminal to login items.
  `heartbeat.json` updates every 60 s, so it can be monitored.
