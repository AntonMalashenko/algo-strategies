"""S009 paper runner (shadow, no live orders) — funding-carry crowding-reversal.

Meant to run once per closed UTC day, but the stateless tick that calls it
(`scripts/s009_tick.py`, launchd `StartCalendarInterval`) fires whenever the
machine happens to wake and check, not at a guaranteed ~00:00 UTC — in
practice cycles have landed anywhere from a few minutes to ~11h after
midnight, and some days ran with `--broker off` (no execution at all that
day). The `net_ret` this module books into `ledger.csv` is still computed as
an idealized close-to-close return as if the book WERE rebalanced exactly at
00:00 UTC (see `_engine`/`strategies.funding_carry.run_backtest`) — it is a
MODEL metric for validating the signal, not a forecast of real execution
P&L. `BROKER_LEDGER_FILE`/`--reconcile-broker` below track the real $ side
separately, over the actual (irregular) gaps between broker readings — see
decisions-log.md "S009: paper vs real equity reconciliation" (2026-08-10).

Reuses the SAME engine as the backtest (`strategies.funding_carry`) so the
live target book cannot drift from the research code. Each cycle:
  1. (optional) refresh recent funding + daily prices from public Bybit,
  2. compute the frozen deploy config's target book for the day ahead,
  3. book-keep the just-closed day's paper P&L (price move + REAL accrued
     funding − taker cost) into a ledger,
  4. log a human-readable summary + append reports/paper_s009/ledger.csv.

By default (--broker off) no orders are placed — this is the shadow stage that
validates the live signal and funding economics before demo execution. Demo
execution (`--broker dry` / `--broker execute`) reconciles the target book to
real Bybit positions via `bot.bybit_exec.BybitExec` (see `reconcile_to_target`)
— mainnet orders are refused unless `--allow-mainnet` is passed explicitly.

Commands:
    python bot/s009_paper.py --once                 # one daily cycle (fetches data)
    python bot/s009_paper.py --once --no-fetch      # use local data as-is
    python bot/s009_paper.py --status               # current book + paper equity
    python bot/s009_paper.py --reconcile            # ledger vs a fresh backtest (theory vs theory)
    python bot/s009_paper.py --reconcile-broker     # paper ledger vs real broker equity (theory vs $)
    python bot/s009_paper.py --simulate 30          # replay last N days from local data (no network)

Unattended scheduling: `--loop` below runs a long-lived foreground process —
fine for a manual/attended test, but NOT the recommended way to run this
unattended (see `run_loop`'s docstring for why). For an actual launchd-managed
deployment use `scripts/s009_tick.py` + `deployment/com.algo.s009-paper.plist`
(installed via `scripts/s009_tick_install.sh install`) instead — same daily
cadence, no long-lived process for macOS to throttle.

Deploy config (frozen champion + vol-target modifier): lb=7, short top-2 /
long bottom-2, dollar-neutral, daily rebalance, taker 0.055%/side, vol-target 20%/yr.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from strategies.funding_carry import (  # noqa: E402
    FundingCarryConfig, load_panels, run_backtest, MS_PER_DAY, DEFAULT_UNIVERSE,
)
from utils.trade_logger import StrategyLogger  # noqa: E402  (shared project logger)
from utils.strategy_state import FileStateStore  # noqa: E402

DATA_DIR = REPO / "data" / "raw" / "crypto_funding"
STATE_DIR = REPO / "reports" / "paper_s009"
STATE_FILE = STATE_DIR / "state.json"
LEDGER_FILE = STATE_DIR / "ledger.csv"

# Real-money counterpart to LEDGER_FILE (added 2026-08-10, see decisions-log.md
# "S009: paper vs real equity reconciliation"). `ledger.csv`/`state.json::equity`
# are a MODEL metric: net_ret is the engine's close-to-close return of a book
# assumed rebalanced exactly at 00:00 UTC. Real cycles run whenever the
# stateless tick happens to fire (see scripts/s009_tick.py) -- typically 4-11h
# after midnight, sometimes not at all that day (broker=off) -- so a single
# broker.equity() reading cannot be attributed to a calendar day the way a
# ledger row can. BROKER_LEDGER_FILE instead logs one row per cycle where the
# broker was actually queried, with the real elapsed wall-clock time since the
# PREVIOUS reading (`hours_since_prev`) and the real $ return over exactly that
# (irregular) window (`real_net_ret`) -- deliberately NOT forced into a
# per-day shape. Comparing this series to `ledger.csv::net_ret` is how you
# check paper vs. real; they are expected to diverge specifically on windows
# where `hours_since_prev` is far from 24h or spans a broker=off day.
BROKER_LEDGER_FILE = STATE_DIR / "broker_ledger.csv"

# configs/accounts.yml BYBIT entry dedicated to S009 (added 2026-08-06, $50 seed).
# MUST be passed explicitly to BybitExec(name=...): as of that date `username`
# alone no longer resolves unambiguously in accounts.yml (a second BYBIT row,
# "Bybit-tradebot1", shares the same username) — see bot/accounts_config.py's
# module docstring for the incident this constant fixes.
BYBIT_ACCOUNT_NAME = "Bybit-algo009"

# Deployment-only universe trim: exclude the highest-priced coins whose Bybit
# exchange minimum order size (BTC ~0.001, notional ~$60+; ETH ~0.01, notional
# ~$15-25) can exceed a whole leg's target allocation on this account's small
# equity (~$50-100, weight/leg ~0.25-0.35 typical) — see decisions-log.md
# 2026-08-06 "аудит готовности" for the arithmetic and reconcile_to_target's
# skip_min_qty log for the live symptom this prevents. This trims ONLY the
# deploy/live universe, not FundingCarryConfig.universe's frozen research
# default (DEFAULT_UNIVERSE, all 24 coins) — the validated backtest numbers
# stay byte-for-byte reproducible; only the paper/live book excludes these two.
# Revisit (re-include) once account equity comfortably clears BTC's min
# notional at typical (non-vol-capped) leg weight, roughly equity >= $250-300.
LOW_CAPITAL_EXCLUDED_SYMBOLS = ("BTCUSDT", "ETHUSDT")
DEPLOY_UNIVERSE = tuple(s for s in DEFAULT_UNIVERSE if s not in LOW_CAPITAL_EXCLUDED_SYMBOLS)

# Frozen deploy config: champion mechanism + vol-target risk control, on the
# capital-constrained deploy universe above.
DEPLOY = FundingCarryConfig(
    signal_lookback_days=7, top_n=2, bottom_n=2, min_universe=4,
    taker_fee_per_side=0.00055, vol_target_annual=0.20,
    universe=DEPLOY_UNIVERSE,
)

FUNDING_URL = "https://api.bybit.com/v5/market/funding/history"
KLINE_URL = "https://api.bybit.com/v5/market/kline"
REFRESH_LOOKBACK_DAYS = 45          # window pulled each refresh (covers lb7 + vol30 + buffer)

POLL_MINUTES_DEFAULT = 20           # loop wake-up interval
LOOP_SLEEP_CHUNK_SEC = 5            # sleep granularity so SIGTERM is honoured quickly

_STOP = {"flag": False}             # set by signal handlers to end the loop


# --------------------------------------------------------------------------
# Data refresh (public Bybit; runs on the user's machine — cloud has no network)
# --------------------------------------------------------------------------

def _merge_csv(path: Path, new: pd.DataFrame, key: str = "ts") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = pd.read_csv(path)
        new = pd.concat([old, new], ignore_index=True)
    new = new.drop_duplicates(key).sort_values(key).reset_index(drop=True)
    new.to_csv(path, index=False)


def refresh_data(universe, data_dir: Path, lookback_days: int = REFRESH_LOOKBACK_DAYS) -> None:
    import requests
    end_ms = None  # newest
    for sym in universe:
        d = data_dir / sym
        # funding (recent window)
        try:
            r = requests.get(FUNDING_URL, params={"category": "linear", "symbol": sym, "limit": 200}, timeout=30)
            lst = r.json().get("result", {}).get("list", [])
            if lst:
                fr = pd.DataFrame({
                    "ts": [int(x["fundingRateTimestamp"]) for x in lst],
                    "funding_rate": [float(x["fundingRate"]) for x in lst],
                })
                fr.insert(1, "datetime", pd.to_datetime(fr["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
                _merge_csv(d / "funding.csv", fr)
            # daily klines (recent)
            kr = requests.get(KLINE_URL, params={"category": "linear", "symbol": sym, "interval": "D", "limit": lookback_days + 5}, timeout=30)
            kl = kr.json().get("result", {}).get("list", [])
            if kl:
                kd = pd.DataFrame(kl, columns=["ts", "open", "high", "low", "close", "volume", "turnover"]).drop(columns="turnover")
                kd["ts"] = kd["ts"].astype("int64")
                for c in ("open", "high", "low", "close", "volume"):
                    kd[c] = pd.to_numeric(kd[c], errors="coerce")
                kd.insert(1, "datetime", pd.to_datetime(kd["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
                _merge_csv(d / "d1.csv", kd)
            time.sleep(0.1)
        except Exception as exc:
            print(f"  ! refresh {sym} failed: {exc}")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def _default_state() -> dict:
    return {"last_day": None, "equity": 1.0, "book": {}}


# The single-account CLI's state store (--once/--loop/--status/--reconcile,
# and scripts/s009_tick.py's `load_state`/`_expected_last_closed_day`
# imports below) -- unchanged file location/format from before
# run_cycle_for_account() existed. A DB-driven multi-account caller
# (webapp/runner.py) builds its own webapp/state_store.py::DBStateStore
# per account instead of using this module-level singleton.
_state_store = FileStateStore(STATE_FILE, default_factory=_default_state)


def load_state() -> dict:
    return _state_store.load()


def save_state(st: dict) -> None:
    _state_store.save(st)


def append_ledger(row: dict, ledger_file: Path | None = None) -> None:
    # `ledger_file: Path | None = None` (not `= LEDGER_FILE`) deliberately:
    # a bound default is captured once at def-time and would stop honoring
    # `monkeypatch.setattr(s009, "LEDGER_FILE", ...)` in tests -- look the
    # module global up fresh on every call instead.
    path = ledger_file if ledger_file is not None else LEDGER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr = not path.exists()
    pd.DataFrame([row]).to_csv(path, mode="a", header=hdr, index=False)


def _last_broker_ledger_row(broker_ledger_file: Path | None = None) -> dict | None:
    """Most recent row of BROKER_LEDGER_FILE, or None if it doesn't exist yet
    (first-ever broker reading) / is empty. Used to compute `real_net_ret` and
    `hours_since_prev` for the NEXT row without keeping a second copy of the
    last-known broker equity anywhere else (the CSV itself is the source of
    truth, same principle `append_ledger` already follows for the paper side)."""
    path = broker_ledger_file if broker_ledger_file is not None else BROKER_LEDGER_FILE
    if not path.exists():
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    return df.iloc[-1].to_dict()


def append_broker_ledger(row: dict, broker_ledger_file: Path | None = None) -> None:
    # Same late-binding-default reasoning as append_ledger() above.
    path = broker_ledger_file if broker_ledger_file is not None else BROKER_LEDGER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr = not path.exists()
    pd.DataFrame([row]).to_csv(path, mode="a", header=hdr, index=False)


# --------------------------------------------------------------------------
# Core: run engine on current panels, return per-day frame + weights + components
# --------------------------------------------------------------------------

def _engine(data_dir: Path, cfg: FundingCarryConfig):
    close, funding = load_panels(data_dir, cfg.universe)
    out, w = run_backtest(close, funding, cfg)
    price_ret = close.pct_change()
    price_comp = (w.shift(0) * price_ret).fillna(0.0).sum(axis=1)   # per-day price component of held book
    fund_comp = (w * (-funding)).fillna(0.0).sum(axis=1)
    return close, funding, out, w, price_comp, fund_comp


def _fmt_book(wrow: pd.Series) -> dict:
    return {s: round(float(v), 4) for s, v in wrow.items() if abs(v) > 1e-9}


def forward_target_book(close: pd.DataFrame, funding: pd.DataFrame, cfg: FundingCarryConfig) -> dict:
    """Book to hold for the day AHEAD, using funding through the last closed day.
    Selection needs only the funding signal (no forward price); vol-target scale
    uses trailing realised vol of the vol-off strategy."""
    sig = funding.rolling(cfg.signal_lookback_days, min_periods=1).mean().iloc[-1]
    last_close = close.iloc[-1]
    row = sig[sig.notna() & last_close.notna()].dropna()
    if len(row) < cfg.min_universe:
        return {}
    ordered = row.sort_values()
    lw = 0.5 * cfg.gross_leverage / cfg.bottom_n
    sw = 0.5 * cfg.gross_leverage / cfg.top_n
    book = {s: lw for s in ordered.index[:cfg.bottom_n]}
    book.update({s: -sw for s in ordered.index[-cfg.top_n:]})
    k = 1.0
    if cfg.vol_target_annual > 0:
        base = run_backtest(close, funding, cfg.with_(vol_target_annual=0.0))[0]["gross_ret"]
        realized = base.rolling(cfg.vol_lookback_days, min_periods=cfg.vol_lookback_days).std(ddof=0).iloc[-1]
        if realized and realized > 0:
            k = min(cfg.vol_scale_cap, (cfg.vol_target_annual / np.sqrt(365)) / float(realized))
    return {s: float(round(v * k, 4)) for s, v in book.items()}


def _floor_step(x: float, step: float) -> float:
    return math.floor(abs(x) / step) * step


def reconcile_to_target(client, target_book: dict, equity: float, log, cid, execute: bool) -> list[dict]:
    """Turn the target book (weights) into delta market orders vs current broker
    positions. dry (execute=False) logs the plan; execute places demo orders and
    records the fill price + slippage vs the reference price.

    A leg whose target weight is 0 but that still has an open position is a FULL
    CLOSE, not a sized adjustment — it is routed through `client.close_position`
    (Bybit's own qty=0 + reduceOnly + closeOnTrigger "close the whole position"
    order) instead of computing a closing qty ourselves via `_floor_step`. Sizing
    a full close locally is both unnecessary (the exchange already knows the
    exact position size) and was proven unsafe: `_floor_step`'s naive
    `floor(x / step) * step` can under-round by exactly one qty_step from
    IEEE-754 float imprecision (e.g. 24.2/0.1 == 241.99999999999997 -> floors to
    24.1, not 24.2) — on 2026-08-09 this left a stuck 0.1 ATOM dust position
    after every reconcile "closed" 24.1 of a 24.2 position. See decisions-log.md
    for the incident. Partial adjustments of a continuing leg (target weight
    != 0) are unaffected and still go through the sized `place_market` path.
    """
    positions = client.positions()
    plan: list[dict] = []
    for sym in sorted(set(target_book) | set(positions)):
        w = float(target_book.get(sym, 0.0))
        try:
            price = client.ticker_price(sym)
            inst = client.instrument(sym)
        except Exception as exc:
            log.error(f"price/instrument fetch failed {sym}", exc=exc, cycle=cid)
            continue
        if price <= 0:
            continue
        cur = float(positions.get(sym, 0.0))

        if w == 0.0 and cur != 0.0:
            side = "Sell" if cur > 0 else "Buy"
            rec = {"symbol": sym, "side": side, "qty": "ALL", "ref_price": price,
                   "target_qty": 0.0, "cur_qty": cur}
            plan.append(rec)
            if not execute:
                log.order(f"S009:{sym}", "plan_close", cycle=cid,
                          request={"side": side, "cur_qty": cur, "ref_price": price},
                          result="dry-run")
                continue
            try:
                res = client.close_position(sym, side)
                oid = res.get("orderId")
                fill = client.recent_fill_price(sym, oid) if oid else None
                slip = ((fill - price) / price) if (fill and price) else None
                log.order(f"S009:{sym}", "close_position", cycle=cid,
                          request={"side": side, "cur_qty": cur, "ref_price": price},
                          result={"orderId": oid, "fill": fill, "slippage": slip})
                log.event("fill", cycle=cid, symbol=sym, side=side, qty=abs(cur),
                          ref_price=price, fill=fill, slippage=slip)
                rec["fill"] = fill
                rec["slippage"] = slip
            except Exception as exc:
                log.order(f"S009:{sym}", "close_position", cycle=cid,
                          request={"side": side, "cur_qty": cur, "ref_price": price}, error=exc)
            continue

        tgt_qty = math.copysign(_floor_step(w * equity / price, inst.qty_step), w) if w else 0.0
        delta = tgt_qty - cur
        # Two INDEPENDENT exchange floors, not one: min_qty is a unit-count
        # minimum, min_notional (Bybit's own separate $5-ish floor) is a
        # dollar-value minimum -- a delta can clear the first and still fail
        # the second (e.g. TRXUSDT: min_qty=1 unit =~ $0.13, min_notional=$5).
        # Checking min_qty alone let a doomed order reach Bybit and get
        # rejected live (error 110094) on every cycle for the same legs.
        delta_notional = abs(delta) * price
        if abs(delta) < inst.min_qty or delta_notional < inst.min_notional:
            # A wanted leg (w != 0) that rounds to less than the exchange's min
            # order size/value is a real gap on a small account (e.g. BTC's
            # ~$65+ min notional can exceed this leg's whole target allocation
            # at equity=$50) — surface it instead of silently dropping the
            # leg, so a thin book isn't mistaken for "target == actual".
            if w and (abs(tgt_qty) < inst.min_qty or abs(tgt_qty) * price < inst.min_notional):
                min_notional = round(max(inst.min_qty * price, inst.min_notional), 2)
                log.event("skip_min_qty", cycle=cid, symbol=sym, target_weight=w,
                          target_notional=round(w * equity, 2), min_qty=inst.min_qty,
                          min_notional=min_notional, price=price)
                print(f"  SKIP {sym}: target notional ${w * equity:.2f} < exchange min "
                      f"${min_notional:.2f} (min_qty={inst.min_qty}, min_notional={inst.min_notional}) "
                      f"— leg not opened")
            continue
        side = "Buy" if delta > 0 else "Sell"
        qty = round(_floor_step(delta, inst.qty_step), 8)
        if qty < inst.min_qty or qty * price < inst.min_notional:
            continue
        rec = {"symbol": sym, "side": side, "qty": qty, "ref_price": price,
               "target_qty": tgt_qty, "cur_qty": cur}
        plan.append(rec)
        if not execute:
            log.order(f"S009:{sym}", "plan_market", cycle=cid,
                      request={"side": side, "qty": qty, "ref_price": price}, result="dry-run")
            continue
        try:
            res = client.place_market(sym, side, qty)
            oid = res.get("orderId")
            fill = client.recent_fill_price(sym, oid) if oid else None
            slip = ((fill - price) / price) if (fill and price) else None
            log.order(f"S009:{sym}", "place_market", cycle=cid,
                      request={"side": side, "qty": qty, "ref_price": price},
                      result={"orderId": oid, "fill": fill, "slippage": slip})
            log.event("fill", cycle=cid, symbol=sym, side=side, qty=qty,
                      ref_price=price, fill=fill, slippage=slip)
            rec["fill"] = fill
            rec["slippage"] = slip
        except Exception as exc:
            log.order(f"S009:{sym}", "place_market", cycle=cid,
                      request={"side": side, "qty": qty, "ref_price": price}, error=exc)
    return plan


def run_cycle_for_account(*, account_key: str, creds: dict | None, cfg: FundingCarryConfig,
                          state, logger: StrategyLogger, data_dir: Path = DATA_DIR,
                          do_fetch: bool = True, drop_forming: bool = True,
                          broker: str = "off", allow_mainnet: bool = False,
                          env: str | None = None,
                          ledger_file: Path | None = None,
                          broker_ledger_file: Path | None = None) -> dict:
    """One S009 daily cycle for an arbitrary account, reusing the exact
    engine/book/ledger logic `run_once()` below uses for the single
    accounts.yml-configured account -- so a DB-registered multi-account run
    (webapp/runner.py) and the original single-account CLI can never drift
    apart into two competing implementations of the same trading rules
    (mirrors bot/s007_paper.py::run_cycle_for_account()'s reasoning).

    `state`: any object with `.load() -> dict` / `.save(dict)` (see
    utils/strategy_state.py) -- a FileStateStore for the single-account CLI
    below, webapp/state_store.py's DBStateStore for the multi-account
    runner, so each enabled account keeps its own book/equity/last-booked-
    day instead of colliding on one shared file.
    `creds`: {"api_key", "api_secret"} passed straight to BybitExec, or None
    to fall back to accounts.yml resolution via `account_key` as the yml
    `name:` (what `run_once()` below still does, unchanged).
    `env`: "mainnet"/"testnet"/"demo", passed straight to BybitExec -- the
    DB-driven caller (webapp/runner.py::_worker_s009) MUST pass its
    Account.env here explicitly. Without it, BybitExec falls back to the
    BYBIT_TESTNET env var (default "true"/testnet, see that class's own
    docstring) -- found live 2026-08-13/14: the Ofelia dispatch container
    never sets BYBIT_TESTNET, so every DB-driven cycle silently ran against
    api-testnet.bybit.com with mainnet-only credentials (401 Client Error:
    API key is invalid), for a real-money mainnet account, for ~17h before
    anyone noticed -- None here (the old default) reproduces exactly that.
    `ledger_file`: append each booked day's row to this CSV, or write no
    ledger at all when `None` (the default). There is no per-account ledger
    file yet -- a bare filename passed in here would collide across
    accounts exactly like the old shared state.json did -- so the
    DB-driven multi-account caller (webapp/runner.py's _worker_s009)
    deliberately leaves this `None` and relies on the day_pnl events
    `logger` already writes (StrategyLogger is per-account by construction,
    see the `logger` param). Only `run_once()`'s single-account CLI path
    passes its own LEDGER_FILE, unchanged from before this refactor.
    `broker_ledger_file`: same per-account-collision reasoning as
    `ledger_file`, but for the REAL-money series (see BROKER_LEDGER_FILE's
    module-level docstring) -- `None` means "don't write it" (multi-account
    caller relies on the `broker` events instead), `run_once()` passes its
    own BROKER_LEDGER_FILE. Only written when `broker != "off"` (a broker
    reading was actually taken this cycle).

    Never raises: catches any exception from the engine/state/broker steps
    and reports it as `error` instead, mirroring bot/s007_paper.py's
    run_cycle_for_account so a future parallel multi-account fan-out can
    isolate one account's failure from the rest. cycle_start/cycle_end are
    always paired, even on failure, so log analysis never sees an orphaned
    cycle_start.

    Prints nothing -- purely a data function; `run_once()` below does the
    human-readable printing from the returned dict, same output as before
    this refactor.

    Returns dict(booked, target, equity, broker_orders, error, date,
    latest_net_ret, broker_env, broker_plan).
    """
    cid = logger.cycle_start(mode="paper-shadow", account=account_key, fetched=do_fetch)
    booked = 0
    target: dict = {}
    equity = None
    date = None
    latest_net_ret = None
    broker_env = None
    broker_equity = None
    broker_plan: list = []
    error = None
    try:
        # Cheap, network-free up-to-date check (added 2026-08-18, see
        # decisions-log.md same date): a missed exact cron minute (Ofelia/
        # container down at the scheduled slot) used to strand the account
        # un-rebalanced until the NEXT day's single slot, or a manual run --
        # cron has no "catch up on a missed minute" behavior the way the old
        # macOS launchd StartCalendarInterval design did (see
        # scripts/s009_tick.py's module docstring). Comparing the persisted
        # last-booked day against the pure, network-free
        # _expected_last_closed_day() lets deployment/schedule.yml tick this
        # account frequently (self-healing within one tick interval instead
        # of one calendar day) while keeping every no-op invocation free of
        # Bybit calls -- only the first invocation after a new UTC day
        # closes actually pays for refresh_data()/_engine().
        prior_state = state.load() or {}
        if do_fetch and prior_state.get("last_day") is not None \
                and prior_state["last_day"] >= _expected_last_closed_day():
            logger.cycle_end(cid, status=f"up-to-date (last_day={prior_state['last_day']})")
            return dict(booked=0, target=prior_state.get("book", {}),
                       equity=prior_state.get("equity"), broker_orders=0, error=None,
                       date=None, latest_net_ret=None, broker_env=None,
                       broker_equity=None, broker_plan=[])
        if do_fetch:
            refresh_data(cfg.universe, data_dir)
        close, funding, out, w, price_comp, fund_comp = _engine(data_dir, cfg)

        days = list(out.index)
        if drop_forming:
            # drop the last daily bar if it belongs to the current (still-forming) UTC day
            today = int(time.time() * 1000) // MS_PER_DAY
            days = [d for d in days if d < today]
        if days:
            latest = days[-1]
            latest_net_ret = float(out.loc[latest, "net_ret"])
            date = pd.to_datetime(latest * MS_PER_DAY, unit="ms", utc=True).strftime("%Y-%m-%d")

            st = state.load() or _default_state()

            # First run: seed the book to hold now, book NO already-closed day.
            # Subsequent runs: realise every day that closed since we last set a book.
            start = (st["last_day"] + 1) if st["last_day"] is not None else latest + 1
            equity = st["equity"]
            for d in [x for x in days if start <= x <= latest]:
                net = float(out.loc[d, "net_ret"])
                equity *= (1 + net)
                d_date = pd.to_datetime(d * MS_PER_DAY, unit="ms", utc=True).strftime("%Y-%m-%d")
                row = {
                    "day": int(d), "date": d_date, "net_ret": round(net, 6),
                    "price_comp": round(float(price_comp.loc[d]), 6),
                    "funding_comp": round(float(fund_comp.loc[d]), 6),
                    "turnover": round(float(out.loc[d, "turnover"]), 4),
                    "n_pos": int(out.loc[d, "n_pos"]), "equity": round(equity, 6),
                }
                if ledger_file is not None:
                    append_ledger(row, ledger_file)
                logger.event("day_pnl", cycle=cid, **row)
                booked += 1

            target = forward_target_book(close, funding, cfg)
            # Log the target book per symbol (one file per coin under positions/).
            for sym, wt in target.items():
                logger.position(f"S009:{sym}", "desired", cycle=cid,
                                side="long" if wt > 0 else "short", weight=wt, for_date=date)
            st.update({"last_day": int(latest), "equity": round(equity, 6), "book": target})
            state.save(st)

            # Broker execution (optional): reconcile the target book to real positions.
            if broker != "off" and target:
                from bot.bybit_exec import BybitExec
                client = (BybitExec(api_key=creds.get("api_key"), api_secret=creds.get("api_secret"),
                                    env=env, allow_mainnet=allow_mainnet) if creds
                         else BybitExec(name=account_key, env=env, allow_mainnet=allow_mainnet))
                broker_env = client.env
                broker_equity = client.wallet_equity()

                # Real-money series (see BROKER_LEDGER_FILE docstring): pair this
                # reading with the PREVIOUS one to get a real $ return over the
                # actual, irregular wall-clock gap between cycles -- never assume
                # that gap is 24h, it routinely isn't (late tick, or a broker=off
                # day with no reading at all in between).
                now_ts = datetime.now(timezone.utc)
                prev = _last_broker_ledger_row(broker_ledger_file)
                real_net_ret = None
                hours_since_prev = None
                if prev is not None and float(prev["broker_equity"]) > 0:
                    real_net_ret = broker_equity / float(prev["broker_equity"]) - 1.0
                    hours_since_prev = (now_ts - pd.Timestamp(prev["ts"]).to_pydatetime()).total_seconds() / 3600.0
                broker_row = {
                    "ts": now_ts.isoformat(), "cycle": cid, "date": date,
                    "broker_equity": round(broker_equity, 4),
                    "hours_since_prev": round(hours_since_prev, 2) if hours_since_prev is not None else None,
                    "real_net_ret": round(real_net_ret, 6) if real_net_ret is not None else None,
                }
                if broker_ledger_file is not None:
                    append_broker_ledger(broker_row, broker_ledger_file)
                logger.event("broker", cycle=cid, env=client.env, equity=round(broker_equity, 2), mode=broker,
                             real_net_ret=broker_row["real_net_ret"], hours_since_prev=broker_row["hours_since_prev"])
                broker_plan = reconcile_to_target(client, target, broker_equity, logger, cid,
                                                  execute=(broker == "execute"))
    except Exception as e:
        logger.error("S009 cycle failed", exc=e, cycle=cid)
        error = repr(e)[:500]

    logger.cycle_end(cid, status=f"paper-shadow: {len(target)} positions, {booked} day(s) booked, "
                               f"broker={broker} orders={len(broker_plan)}",
                    equity=equity)
    return dict(booked=booked, target=target, equity=equity, broker_orders=len(broker_plan),
               error=error, date=date, latest_net_ret=latest_net_ret,
               broker_env=broker_env, broker_equity=broker_equity, broker_plan=broker_plan)


def run_once(data_dir: Path, cfg: FundingCarryConfig, do_fetch: bool, drop_forming: bool = True,
             broker: str = "off", allow_mainnet: bool = False) -> None:
    """Thin CLI wrapper around run_cycle_for_account() for the single
    accounts.yml-configured account (BYBIT_ACCOUNT_NAME) and the module's
    file-backed state (_state_store / STATE_FILE) -- same account/state
    location/printed summary as before run_cycle_for_account() existed.
    scripts/s009_tick.py's `--once` subprocess call is unaffected."""
    if do_fetch:
        print("Refreshing data from Bybit (public)...")
    log = StrategyLogger("S009", log_root=REPO / "reports" / "logs", console=False)
    result = run_cycle_for_account(
        account_key=BYBIT_ACCOUNT_NAME, creds=None, cfg=cfg, state=_state_store, logger=log,
        data_dir=data_dir, do_fetch=do_fetch, drop_forming=drop_forming,
        broker=broker, allow_mainnet=allow_mainnet, ledger_file=LEDGER_FILE,
        broker_ledger_file=BROKER_LEDGER_FILE)

    if result["error"]:
        print(f"ERROR: {result['error']}")
        return
    if result["date"] is None:
        print("No closed day to process.")
        return

    target = result["target"]
    if result["broker_env"] is not None:
        print(f"\nbroker[{result['broker_env']}] equity={result['broker_equity']:.2f}  "
              f"mode={broker}  orders={result['broker_orders']}")
        for r in result["broker_plan"]:
            extra = f" fill={r.get('fill')} slip={r.get('slippage')}" if "fill" in r else ""
            print(f"  {r['side']:>4} {r['qty']} {r['symbol']} @~{r['ref_price']}  (cur {r['cur_qty']} → tgt {r['target_qty']}){extra}")

    longs = {s: v for s, v in target.items() if v > 0}
    shorts = {s: v for s, v in target.items() if v < 0}
    print(f"\n=== S009 paper cycle {result['date']} ===")
    print(f"paper equity: {result['equity']:.4f}  (last day net {result['latest_net_ret']:+.4%})")
    print(f"TARGET BOOK (hold into next day):")
    print(f"  LONG : {longs}")
    print(f"  SHORT: {shorts}")
    print(f"  gross={sum(abs(v) for v in target.values()):.2f}  net={sum(target.values()):+.3f}  positions={len(target)}")
    print(f"logged → {LEDGER_FILE}")


def _install_signal_handlers(log) -> None:
    import signal
    def _handler(signum, _frame):
        _STOP["flag"] = True
        log.info(f"signal {signum} received — stopping after current cycle")
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _handler)
        except Exception:
            pass


def _expected_last_closed_day() -> int:
    """Last fully-closed UTC day: the daily candle for day D closes at 00:00 D+1,
    so once we are inside day T the last closed day is T-1."""
    return int(time.time() * 1000) // MS_PER_DAY - 1


def run_loop(data_dir: Path, cfg: FundingCarryConfig, broker: str, allow_mainnet: bool,
             poll_minutes: int = POLL_MINUTES_DEFAULT) -> None:
    """Long-running daemon. Wakes every `poll_minutes`, and when a new UTC day has
    closed since the last processed one, runs a cycle (fetch → book → optional
    rebalance). Tolerant to laptop sleep: on wake it simply notices the missed day
    and processes it (the ledger books every not-yet-seen day; the broker reconcile
    is idempotent — drives positions to target, so a late run just rebalances once).
    Survives per-cycle errors and exits cleanly on SIGINT/SIGTERM (launchd stop).

    NOT recommended for unattended (launchd KeepAlive-style) production use: this
    is exactly the "one process alive ~24h/day, sleeping in short chunks" shape
    that S007's original scheduler used and that failed in production — a
    long-lived bash sleep silently stopped waking for ~17h under macOS
    App Nap / power-management throttling of an unsupervised background process
    (decisions-log.md 2026-07-23). S007's fix was to remove the long-lived-process
    premise entirely (a stateless launchd `StartCalendarInterval` tick instead of
    a sleeping loop) — see `scripts/s007_tick.py`. `scripts/s009_tick.py` applies
    the same fix here; use it (+ `deployment/com.algo.s009-paper.plist`) for any
    unattended deployment. Keep `run_loop`/`--loop` for manual, attended,
    foreground runs (e.g. testing on a terminal you're watching) where a stalled
    process is immediately visible."""
    log = StrategyLogger("S009", log_root=REPO / "reports" / "logs", console=True)
    _install_signal_handlers(log)
    log.info(f"S009 loop started: broker={broker} allow_mainnet={allow_mainnet} "
             f"poll={poll_minutes}m data={data_dir}")
    while not _STOP["flag"]:
        try:
            st = load_state()
            exp = _expected_last_closed_day()
            if st.get("last_day") is None or st["last_day"] < exp:
                log.info(f"loop: closed day {exp} > last_processed {st.get('last_day')} — running cycle")
                run_once(data_dir, cfg, do_fetch=True, broker=broker, allow_mainnet=allow_mainnet)
            else:
                log.debug(f"loop: up-to-date (last_day={st['last_day']}) — idle")
        except Exception as exc:
            log.error("loop cycle failed (retrying next poll)", exc=exc)
        waited = 0
        while waited < poll_minutes * 60 and not _STOP["flag"]:
            time.sleep(LOOP_SLEEP_CHUNK_SEC)
            waited += LOOP_SLEEP_CHUNK_SEC
    log.info("S009 loop stopped cleanly.")


def status() -> None:
    st = load_state()
    if st["last_day"] is None:
        print("No paper state yet — run --once first."); return
    date = pd.to_datetime(st["last_day"] * MS_PER_DAY, unit="ms", utc=True).strftime("%Y-%m-%d")
    print(f"S009 paper — as of {date}: equity {st['equity']:.4f}")
    print(f"current book: {st['book']}")


def reconcile(data_dir: Path, cfg: FundingCarryConfig) -> None:
    if not LEDGER_FILE.exists():
        print("No ledger yet."); return
    led = pd.read_csv(LEDGER_FILE)
    _, _, out, _, _, _ = _engine(data_dir, cfg)
    bt = out["net_ret"].reindex(led["day"].values)
    diff = (led["net_ret"].values - bt.values)
    md = float(np.nanmax(np.abs(diff))) if len(diff) else 0.0
    print(f"Reconcile paper ledger vs backtest over {len(led)} logged days:")
    print(f"  max|Δ net_ret| = {md:.2e}  -> {'MATCH' if md < 1e-9 else 'DIVERGENCE (investigate fills/data)'}")


def reconcile_broker() -> None:
    """Compare the theoretical paper ledger (LEDGER_FILE::net_ret, one row per
    UTC calendar day, close-to-close) against the real $ series read from the
    account (BROKER_LEDGER_FILE::real_net_ret, one row per cycle that actually
    queried the broker, over whatever irregular wall-clock gap separates it
    from the previous reading).

    This is NOT the same question `reconcile()` above answers -- that one
    checks the ledger against a fresh re-run of the backtest (theory vs. the
    same theory, catches drift/regressions in the engine itself). This checks
    theory against real dollars, which is what actually tells you whether the
    bot's paper equity is a usable forecast of real P&L. See decisions-log.md
    "S009: paper vs real equity reconciliation" (2026-08-10) for the first
    reconciliation run and its caveats (too few days for a verdict yet, one
    broker=off day with no reading, two $5-min-order execution gaps).
    """
    if not BROKER_LEDGER_FILE.exists():
        print("No broker ledger yet (bot has never run with --broker dry/execute)."); return
    bl = pd.read_csv(BROKER_LEDGER_FILE)
    led = pd.read_csv(LEDGER_FILE) if LEDGER_FILE.exists() else pd.DataFrame(columns=["date", "equity"])
    led_equity_by_date = led.set_index("date")["equity"] if not led.empty else pd.Series(dtype=float)

    def _equity_at(date):
        # Model equity is DEFINED as 1.0 before any day has ever been booked
        # (see _default_state()) -- so a broker reading taken before the
        # ledger's first row (e.g. the very first cycle, which only seeds a
        # target book and books zero days) still has a well-defined paper
        # baseline to compare against, not just a missing lookup.
        if date in led_equity_by_date.index:
            return float(led_equity_by_date[date])
        if not led_equity_by_date.empty and date < led_equity_by_date.index.min():
            return 1.0
        return None

    # `paper_window_ret` is the SAME window `real_net_ret` spans -- cumulative
    # model equity from the previous broker row's date to this one's, NOT a
    # single day's net_ret. Comparing real_net_ret to one day's net_ret would
    # silently misattribute it whenever hours_since_prev straddles more than
    # one UTC day (routinely does, e.g. a broker=off day in between) or less
    # than one -- this is the apples-to-apples version of that comparison.
    print(f"Broker vs paper — {len(bl)} broker reading(s) in {BROKER_LEDGER_FILE.name}:")
    print(f"{'ts':>26} {'date':>10} {'broker_eq':>10} {'hrs_gap':>8} {'real_ret':>10} {'paper_window_ret':>17}")
    prev_date = None
    for _, r in bl.iterrows():
        paper_window = np.nan
        eq_prev, eq_now = (_equity_at(prev_date) if prev_date is not None else None), _equity_at(r["date"])
        if eq_prev is not None and eq_now is not None:
            paper_window = eq_now / eq_prev - 1.0
        real_s = f"{r['real_net_ret']:+.4%}" if pd.notna(r["real_net_ret"]) else "     n/a"
        hrs_s = f"{r['hours_since_prev']:.1f}" if pd.notna(r["hours_since_prev"]) else "  n/a"
        paper_s = f"{paper_window:+.4%}" if pd.notna(paper_window) else "n/a"
        print(f"{r['ts']:>26} {str(r['date']):>10} {r['broker_equity']:>10.2f} {hrs_s:>8} {real_s:>10} {paper_s:>17}")
        prev_date = r["date"]
    print("\nNote: `real_net_ret` and `paper_window_ret` cover the SAME wall-clock window\n"
          "(from the previous broker reading to this one, `hours_since_prev` actual hours --\n"
          "not a clean UTC calendar day). A gap between them on a window close to 24h with no\n"
          "broker=off day in between is the interesting case; a gap on a window far from 24h,\n"
          "or one that swallowed a broker=off day, is largely explained by that alone -- see\n"
          "the passport's execution-timing caveat before treating either as a bug.")


def simulate(data_dir: Path, cfg: FundingCarryConfig, n: int) -> None:
    """Replay last n days from local data (no network) + no-look-ahead self-check."""
    close, funding, out, w, price_comp, fund_comp = _engine(data_dir, cfg)
    tail = out.tail(n)
    print(f"=== simulate: last {n} days (local data) ===")
    print(f"{'date':>10} {'net_ret':>9} {'price':>9} {'funding':>9} {'turn':>6} {'npos':>4}")
    eq = 1.0
    for d, r in tail.iterrows():
        eq *= (1 + r["net_ret"])
        dt = pd.to_datetime(d * MS_PER_DAY, unit="ms", utc=True).strftime("%Y-%m-%d")
        print(f"{dt:>10} {r['net_ret']:+9.4f} {price_comp.loc[d]:+9.4f} {fund_comp.loc[d]:+9.4f} {r['turnover']:6.2f} {int(r['n_pos']):4d}")
    print(f"tail equity mult: {eq:.4f}")
    print(f"\nforward TARGET BOOK (hold next day): {forward_target_book(close, funding, cfg)}")
    # no-look-ahead self-check: truncating the panel must not change past days
    cut = int(out.index[-5])
    c2, f2 = load_panels(data_dir, cfg.universe)
    o2, _ = run_backtest(c2[c2.index <= cut], f2[f2.index <= cut], cfg)
    common = [d for d in o2.index if d < cut]
    md = float(np.max(np.abs(out.loc[common, "net_ret"].values - o2.loc[common, "net_ret"].values))) if common else 0.0
    print(f"self-check (live path == backtest, no look-ahead): max|Δ|={md:.2e} -> {'OK' if md < 1e-12 else 'FAIL'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="S009 funding-carry paper runner (shadow).")
    ap.add_argument("--once", action="store_true", help="run one daily cycle")
    ap.add_argument("--no-fetch", action="store_true", help="skip Bybit refresh, use local data")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reconcile", action="store_true")
    ap.add_argument("--reconcile-broker", action="store_true",
                    help="compare paper ledger.csv net_ret against real broker_ledger.csv "
                         "real_net_ret (theory vs. actual dollars, see decisions-log.md 2026-08-10)")
    ap.add_argument("--simulate", type=int, metavar="N", help="replay last N days from local data")
    ap.add_argument("--loop", action="store_true", help="run as a daemon: process each new closed day")
    ap.add_argument("--poll-minutes", type=int, default=POLL_MINUTES_DEFAULT, help="loop wake interval")
    ap.add_argument("--broker", choices=["off", "dry", "execute"], default="off",
                    help="off=shadow only; dry=compute+log intended demo orders; execute=place demo orders")
    ap.add_argument("--allow-mainnet", action="store_true", help="DANGER: permit real mainnet orders")
    ap.add_argument("--data", type=Path, default=DATA_DIR)
    args = ap.parse_args()

    if args.simulate is not None:
        simulate(args.data, DEPLOY, args.simulate)
    elif args.loop:
        run_loop(args.data, DEPLOY, broker=args.broker, allow_mainnet=args.allow_mainnet,
                 poll_minutes=args.poll_minutes)
    elif args.status:
        status()
    elif args.reconcile:
        reconcile(args.data, DEPLOY)
    elif args.reconcile_broker:
        reconcile_broker()
    elif args.once:
        run_once(args.data, DEPLOY, do_fetch=not args.no_fetch,
                 broker=args.broker, allow_mainnet=args.allow_mainnet)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
