"""S009 paper runner (shadow, no live orders) — funding-carry crowding-reversal.

Runs once per day at ~00:00 UTC. Reuses the SAME engine as the backtest
(`strategies.funding_carry`) so the live target book cannot drift from the
research code. Each cycle:
  1. (optional) refresh recent funding + daily prices from public Bybit,
  2. compute the frozen deploy config's target book for the day ahead,
  3. book-keep the just-closed day's paper P&L (price move + REAL accrued
     funding − taker cost) into a ledger,
  4. log a human-readable summary + append reports/paper_s009/ledger.csv.

No orders are placed — this is the shadow stage that validates the live signal
and funding economics before demo execution. A Bybit execution adapter plugs in
later at the marked TODO in `run_once`.

Commands:
    python bot/s009_paper.py --once                 # one daily cycle (fetches data)
    python bot/s009_paper.py --once --no-fetch      # use local data as-is
    python bot/s009_paper.py --status               # current book + paper equity
    python bot/s009_paper.py --reconcile            # ledger vs a fresh backtest
    python bot/s009_paper.py --simulate 30          # replay last N days from local data (no network)

Deploy config (frozen champion + vol-target modifier): lb=7, short top-2 /
long bottom-2, dollar-neutral, daily rebalance, taker 0.055%/side, vol-target 20%/yr.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from strategies.funding_carry import (  # noqa: E402
    FundingCarryConfig, load_panels, run_backtest, MS_PER_DAY, DEFAULT_UNIVERSE,
)
from utils.trade_logger import StrategyLogger  # noqa: E402  (shared project logger)

DATA_DIR = REPO / "data" / "raw" / "crypto_funding"
STATE_DIR = REPO / "reports" / "paper_s009"
STATE_FILE = STATE_DIR / "state.json"
LEDGER_FILE = STATE_DIR / "ledger.csv"

# Frozen deploy config: champion mechanism + vol-target risk control.
DEPLOY = FundingCarryConfig(
    signal_lookback_days=7, top_n=2, bottom_n=2, min_universe=4,
    taker_fee_per_side=0.00055, vol_target_annual=0.20,
)

FUNDING_URL = "https://api.bybit.com/v5/market/funding/history"
KLINE_URL = "https://api.bybit.com/v5/market/kline"
REFRESH_LOOKBACK_DAYS = 45          # window pulled each refresh (covers lb7 + vol30 + buffer)


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

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_day": None, "equity": 1.0, "book": {}}


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2))


def append_ledger(row: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    hdr = not LEDGER_FILE.exists()
    pd.DataFrame([row]).to_csv(LEDGER_FILE, mode="a", header=hdr, index=False)


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
    records the fill price + slippage vs the reference price."""
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
        tgt_qty = math.copysign(_floor_step(w * equity / price, inst.qty_step), w) if w else 0.0
        cur = float(positions.get(sym, 0.0))
        delta = tgt_qty - cur
        if abs(delta) < inst.min_qty:
            continue
        side = "Buy" if delta > 0 else "Sell"
        qty = round(_floor_step(delta, inst.qty_step), 8)
        if qty < inst.min_qty:
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


def run_once(data_dir: Path, cfg: FundingCarryConfig, do_fetch: bool, drop_forming: bool = True,
             broker: str = "off", allow_mainnet: bool = False) -> None:
    if do_fetch:
        print("Refreshing data from Bybit (public)...")
        refresh_data(cfg.universe, data_dir)
    close, funding, out, w, price_comp, fund_comp = _engine(data_dir, cfg)

    days = list(out.index)
    if drop_forming:
        # drop the last daily bar if it belongs to the current (still-forming) UTC day
        today = int(time.time() * 1000) // MS_PER_DAY
        days = [d for d in days if d < today]
    if not days:
        print("No closed day to process."); return
    latest = days[-1]

    st = load_state()
    date = pd.to_datetime(latest * MS_PER_DAY, unit="ms", utc=True).strftime("%Y-%m-%d")

    # Route through the shared project logger (uniform with S004/S007). We only
    # USE its public API — the logger itself is unchanged. One cycle per daily run.
    log = StrategyLogger("S009", log_root=REPO / "reports" / "logs", console=False)
    cid = log.cycle_start(mode="paper-shadow", latest_day=date,
                          equity=round(st["equity"], 6), universe=len(cfg.universe),
                          fetched=do_fetch)

    # First run: seed the book to hold now, book NO already-closed day.
    # Subsequent runs: realise every day that closed since we last set a book.
    start = (st["last_day"] + 1) if st["last_day"] is not None else latest + 1
    equity = st["equity"]
    booked = 0
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
        append_ledger(row)
        log.event("day_pnl", cycle=cid, **row)
        booked += 1

    target = forward_target_book(close, funding, cfg)
    # Log the target book per symbol (one file per coin under positions/).
    for sym, wt in target.items():
        log.position(f"S009:{sym}", "desired", cycle=cid,
                     side="long" if wt > 0 else "short", weight=wt, for_date=date)
    st.update({"last_day": int(latest), "equity": round(equity, 6), "book": target})
    save_state(st)

    # Broker execution (optional): reconcile the target book to real positions.
    plan = []
    if broker != "off" and target:
        from bot.bybit_exec import BybitExec
        client = BybitExec(allow_mainnet=allow_mainnet)
        broker_eq = client.wallet_equity()
        log.event("broker", cycle=cid, env=client.env, equity=round(broker_eq, 2), mode=broker)
        plan = reconcile_to_target(client, target, broker_eq, log, cid, execute=(broker == "execute"))
        print(f"\nbroker[{client.env}] equity={broker_eq:.2f}  mode={broker}  orders={len(plan)}")
        for r in plan:
            extra = f" fill={r.get('fill')} slip={r.get('slippage')}" if "fill" in r else ""
            print(f"  {r['side']:>4} {r['qty']} {r['symbol']} @~{r['ref_price']}  (cur {r['cur_qty']} → tgt {r['target_qty']}){extra}")

    log.cycle_end(cid, status=f"paper-shadow: {len(target)} positions, {booked} day(s) booked, "
                             f"broker={broker} orders={len(plan)}",
                  equity=round(equity, 6))

    longs = {s: v for s, v in target.items() if v > 0}
    shorts = {s: v for s, v in target.items() if v < 0}
    print(f"\n=== S009 paper cycle {date} ===")
    print(f"paper equity: {equity:.4f}  (last day net {float(out.loc[latest,'net_ret']):+.4%})")
    print(f"TARGET BOOK (hold into next day):")
    print(f"  LONG : {longs}")
    print(f"  SHORT: {shorts}")
    print(f"  gross={sum(abs(v) for v in target.values()):.2f}  net={sum(target.values()):+.3f}  positions={len(target)}")
    print(f"logged → {LEDGER_FILE}")


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
    ap.add_argument("--simulate", type=int, metavar="N", help="replay last N days from local data")
    ap.add_argument("--broker", choices=["off", "dry", "execute"], default="off",
                    help="off=shadow only; dry=compute+log intended demo orders; execute=place demo orders")
    ap.add_argument("--allow-mainnet", action="store_true", help="DANGER: permit real mainnet orders")
    ap.add_argument("--data", type=Path, default=DATA_DIR)
    args = ap.parse_args()

    if args.simulate is not None:
        simulate(args.data, DEPLOY, args.simulate)
    elif args.status:
        status()
    elif args.reconcile:
        reconcile(args.data, DEPLOY)
    elif args.once:
        run_once(args.data, DEPLOY, do_fetch=not args.no_fetch,
                 broker=args.broker, allow_mainnet=args.allow_mainnet)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
