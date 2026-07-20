"""Paper-trading runner (S004 v1.0).

Dry-run mode works entirely from local CSVs and prints the desired order
set — use it to eyeball signals before wiring the broker:

    python -m bot.paper --dry-run                # latest local fresh data
    python -m bot.paper --dry-run --at "2026-06-25 03:00"

Live-paper mode (after bot/ctrader.py is implemented) reconciles the
broker state to the desired state every run; schedule it every 15 minutes:

    python -m bot.paper --live
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from bot import config as C
from bot.signals import desired_orders

ROOT = Path(__file__).resolve().parent.parent


def load_local_m15(sym: str) -> pd.DataFrame:
    """Latest local data (fresh file preferred) in raw points."""
    base = ROOT / "data" / "raw" / sym
    path = base / f"{sym}m15fresh.csv"
    if not path.exists():
        path = base / f"{sym}m15.csv"
    d = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
    d = d.rename(columns={"tick_volume": "volume"})
    for c in ["open", "high", "low", "close"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    return d.dropna(subset=["close"])[["open", "high", "low", "close", "volume"]]


def dry_run(at: str | None):
    out = []
    for sym in C.PAIRS:
        m15 = load_local_m15(sym)
        if at:
            m15 = m15.loc[:pd.Timestamp(at)]
        m15 = m15.tail(C.HISTORY_DAYS * 96)
        res = desired_orders(m15, sym)
        out.append(dict(symbol=sym, in_window=res["in_window"],
                        has_position=res["position"] is not None,
                        orders=res["orders"]))
    n_orders = sum(len(x["orders"]) for x in out)
    print(f"as-of: {at or 'latest local bar'}  |  desired orders: {n_orders}")
    for x in out:
        for o in x["orders"]:
            print(f"  {o['symbol']:7} {o['side']:4} limit @{o['price']:.0f} "
                  f"SL {o['sl']:.0f} TP {o['tp']:.0f} risk {o['risk_points']:.0f}pt")
        if x["has_position"]:
            print(f"  {x['symbol']:7} [open position in engine state]")
    log_dir = ROOT / C.PAPER_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (log_dir / f"dryrun_{stamp}.json").write_text(json.dumps(out, default=str, indent=1))


def accounts():
    """Print account ids authorised by the token (no account id needed yet)."""
    from bot.ctrader import CTraderAdapter
    for a in CTraderAdapter(require_account=False).get_accounts():
        kind = "LIVE" if a["is_live"] else "DEMO"
        print(f"  ctidTraderAccountId={a['account_id']}  ({kind})")
    print("Put the DEMO id into .env as CTRADER_ACCOUNT_ID")


def check():
    """Connectivity test: auth, balance, symbol availability."""
    from bot.ctrader import CTraderAdapter
    res = CTraderAdapter().check()
    print(f"OK: balance {res['balance']:.2f}, "
          f"pairs found {len(res['symbols_found'])}/{len(C.PAIRS)}: "
          f"{', '.join(res['symbols_found'])}")


def live():
    """One reconcile cycle against the broker; schedule every 15 min."""
    from bot.ctrader import CTraderAdapter
    api = CTraderAdapter()
    state = api.reconcile()
    have_orders = {o.label: o for o in state["orders"] if getattr(o, "label", "")}
    open_symbols = set()
    for pos in state["positions"]:
        open_symbols.add(pos.tradeData.symbolId)
    actions = []
    for sym in C.PAIRS:
        m15 = api.get_m15(sym, C.HISTORY_DAYS)
        res = desired_orders(m15, sym)
        want = {o["zone_id"]: o for o in res["orders"]}
        for zone_id, o in want.items():
            if zone_id not in have_orders:
                api.place_limit(sym, o["side"], o["price"], o["sl"], o["tp"],
                                volume_lots=0.01, label=zone_id)
                actions.append(f"place {zone_id} {o['side']} @{o['price']:.0f}")
        for label, brk in list(have_orders.items()):
            if label.startswith(sym + ":") and label not in want:
                api.cancel(brk.orderId)
                actions.append(f"cancel {label}")
    log_dir = ROOT / C.PAPER_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (log_dir / f"cycle_{stamp}.log").write_text("\n".join(actions) or "no-op")
    print(f"cycle done: {len(actions)} actions")
    for a_ in actions:
        print(" ", a_)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--at", default=None, help="simulate 'now' at this timestamp")
    ap.add_argument("--accounts", action="store_true", help="list account ids for the token")
    ap.add_argument("--check", action="store_true", help="test broker connectivity")
    ap.add_argument("--live", action="store_true", help="one reconcile cycle")
    a = ap.parse_args()
    if a.accounts:
        accounts()
    elif a.check:
        check()
    elif a.live:
        live()
    else:
        dry_run(a.at)
